"""Read-only SQLite + Protobuf telemetry extractor for Antigravity conversations/*.db.

Uses (db + wal) inode/size/mtime stamp caching with a bounded LRU cache so unchanged
conversation databases incur zero SQLite opens during steady-state polling.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_DATACLASS_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(frozen=True, **_DATACLASS_SLOTS)
class GenTelemetry:
    idx: int
    uncached_in: int
    cached_in: int
    total_in: int
    output_tokens: int
    thinking_tokens: int
    reply_tokens: int
    ttft_sec: Optional[float]
    streaming_sec: Optional[float]
    tps: Optional[float]
    max_context_tokens: int
    response_id: str
    shard_tag: str
    retry_error: str
    model_label: str


@dataclass(frozen=True, **_DATACLASS_SLOTS)
class ConversationDbSnapshot:
    conversation_id: str
    latest: Optional[GenTelemetry]
    recent: Tuple[GenTelemetry, ...]
    max_context_tokens: int
    last_step_epoch_ms: int
    is_subagent_hint: bool = False


_TailRowSig = Tuple[int, int, bytes, bytes]
_FullCacheEntry = Tuple[
    str,                      # combined_key
    int,                      # db_ino
    int,                      # total_row_count
    Optional[int],            # min_idx
    Optional[int],            # last_raw_idx
    Tuple[_TailRowSig, ...],  # tail_sig (ORDER BY idx DESC LIMIT 2)
    List[GenTelemetry],       # gens_list
]


def _read_varint(buf: bytes | memoryview, pos: int) -> Tuple[int, int]:
    val = 0
    shift = 0
    length = len(buf)
    while pos < length:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return val, pos
        shift += 7
        if shift > 64:
            break
    return val, pos


def parse_pb_map(buf: bytes | memoryview) -> Dict[int, List[object]]:
    """Decode wire-format protobuf fields into {field_number: [values...]}."""
    fields: Dict[int, List[object]] = {}
    pos = 0
    length = len(buf)
    view = memoryview(buf)
    while pos < length:
        tag, next_pos = _read_varint(view, pos)
        pos = next_pos
        fn = tag >> 3
        wt = tag & 7
        if fn == 0:
            break
        bucket = fields.setdefault(fn, [])
        if wt == 0:
            val, pos = _read_varint(view, pos)
            bucket.append(val)
        elif wt == 2:
            size, pos = _read_varint(view, pos)
            if size < 0 or pos + size > length:
                break
            bucket.append(bytes(view[pos : pos + size]))
            pos += size
        elif wt == 5:
            if pos + 4 > length:
                break
            bucket.append(bytes(view[pos : pos + 4]))
            pos += 4
        elif wt == 1:
            if pos + 8 > length:
                break
            bucket.append(bytes(view[pos : pos + 8]))
            pos += 8
        else:
            break
    return fields


def _parse_duration_sec(raw: bytes | None) -> Optional[float]:
    if not raw or not isinstance(raw, (bytes, bytearray)):
        return None
    fields = parse_pb_map(raw)
    sec = int(fields.get(1, [0])[0]) if fields.get(1) else 0
    nanos = int(fields.get(2, [0])[0]) if fields.get(2) else 0
    # Guard against negative two's-complement varints decoded as huge unsigned ints
    if sec < 0 or sec > 3600 or nanos < 0 or nanos >= 1_000_000_000:
        return None
    val = sec + nanos / 1e9
    return val if 0.0 <= val <= 3600.0 else None


def parse_gen_metadata_row(idx: int, blob: bytes) -> Optional[GenTelemetry]:
    """Extract token counts, TTFT, streaming TPS, retry error, and shard tag from one row."""
    if not blob:
        return None
    top = parse_pb_map(blob)
    if 1 not in top or not isinstance(top[1][0], bytes):
        return None

    m1 = parse_pb_map(top[1][0])
    usage_bytes = m1.get(4, [b""])[0]
    usage = parse_pb_map(usage_bytes) if isinstance(usage_bytes, bytes) else {}

    uncached_in = int(usage.get(2, [0])[0]) if usage.get(2) else 0
    total_out = int(usage.get(3, [0])[0]) if usage.get(3) else 0
    cached_in = int(usage.get(5, [0])[0]) if usage.get(5) else 0
    think_tokens = int(usage.get(9, [0])[0]) if usage.get(9) else 0
    reply_tokens = int(usage.get(10, [0])[0]) if usage.get(10) else 0
    total_in = uncached_in + cached_in

    response_id = ""
    if usage.get(11) and isinstance(usage[11][0], bytes):
        response_id = usage[11][0].decode("utf-8", errors="ignore").strip()
    shard_tag = response_id[12:16] if len(response_id) >= 16 else ""

    ttft_sec = _parse_duration_sec(m1.get(11, [None])[0] if m1.get(11) else None)
    streaming_sec = _parse_duration_sec(m1.get(12, [None])[0] if m1.get(12) else None)

    # Compute pure streaming TPS only when streaming_sec >= 0.15s; never divide by (ttft + streaming)
    # on instant burst tool-calls (<0.15s), which would mix multi-second TTFT into fake low TPS.
    tps: Optional[float] = None
    if total_out > 0 and streaming_sec is not None and streaming_sec >= 0.15:
        tps = total_out / streaming_sec

    max_context = 0
    if m1.get(9) and isinstance(m1[9][0], bytes):
        m9 = parse_pb_map(m1[9][0])
        if m9.get(10) and isinstance(m9[10][0], bytes):
            m10 = parse_pb_map(m9[10][0])
            if m10.get(4) and isinstance(m10[4][0], int):
                max_context = int(m10[4][0])

    retry_error = ""
    if m1.get(17):
        last_retry = m1[17][-1]
        if isinstance(last_retry, bytes):
            retry_map = parse_pb_map(last_retry)
            err_val = retry_map.get(3, [b""])[0]
            if isinstance(err_val, bytes):
                retry_error = err_val.decode("utf-8", errors="ignore").strip()

    # Model label priority: m1[21] (display label) -> m1[20]["model_enum"] -> m1[19] (slug) -> top[21]/top[19] (legacy/tests)
    model_label = ""
    m1_21 = m1.get(21)
    if m1_21 and isinstance(m1_21[0], bytes):
        cand = m1_21[0].decode("utf-8", errors="ignore").strip()
        if cand:
            model_label = cand
    if not model_label and m1.get(20):
        for entry in m1[20]:
            if isinstance(entry, bytes):
                em = parse_pb_map(entry)
                if em.get(1) and em[1][0] == b"model_enum" and em.get(2) and isinstance(em[2][0], bytes):
                    cand = em[2][0].decode("utf-8", errors="ignore").strip()
                    if cand:
                        model_label = cand
                        break
    if not model_label:
        m1_19 = m1.get(19)
        if m1_19 and isinstance(m1_19[0], bytes):
            cand = m1_19[0].decode("utf-8", errors="ignore").strip()
            if cand:
                model_label = cand
    if not model_label:
        for fn in (21, 19):
            vals = top.get(fn)
            if vals and isinstance(vals[0], bytes):
                cand = vals[0].decode("utf-8", errors="ignore").strip()
                if cand:
                    model_label = cand
                    break

    return GenTelemetry(
        idx=idx,
        uncached_in=uncached_in,
        cached_in=cached_in,
        total_in=total_in,
        output_tokens=total_out,
        thinking_tokens=think_tokens,
        reply_tokens=reply_tokens,
        ttft_sec=ttft_sec,
        streaming_sec=streaming_sec,
        tps=tps,
        max_context_tokens=max_context,
        response_id=response_id,
        shard_tag=shard_tag,
        retry_error=retry_error,
        model_label=model_label,
    )


def _file_stamp_and_ino(path: Path | str) -> Optional[Tuple[str, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}", int(st.st_ino)


def _file_stamp(path: Path | str) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _as_sig_bytes(val: object) -> bytes:
    if val is None:
        return b""
    if isinstance(val, bytes):
        return val
    if isinstance(val, (bytearray, memoryview)):
        return bytes(val)
    if isinstance(val, str):
        return val.encode("utf-8", errors="replace")
    return bytes(val)  # type: ignore[arg-type]


def _make_raw_row_sig(idx: int, data: object) -> _TailRowSig:
    if data is None:
        return (int(idx), -1, b"", b"")
    raw = _as_sig_bytes(data)
    return (int(idx), len(raw), raw[:32], raw[-32:])


def _make_sql_row_sig(row: Tuple[object, object, object, object]) -> _TailRowSig:
    idx_v, len_v, head_v, tail_v = row
    if len_v is None:
        return (int(idx_v), -1, b"", b"")  # type: ignore[arg-type]
    return (
        int(idx_v),  # type: ignore[arg-type]
        int(len_v),  # type: ignore[arg-type]
        _as_sig_bytes(head_v),
        _as_sig_bytes(tail_v),
    )


def _sqlite_ro_uri(db_path: Path) -> str:
    resolved = db_path.resolve()
    posix_path = resolved.as_posix()
    encoded = urllib.parse.quote(posix_path, safe="/:")
    return f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"


class ConversationDbReader:
    """Bounded LRU reader over Antigravity conversations/<id>.db files."""

    def __init__(self, conversations_dir: Path | str, max_cache_entries: int = 36, tail_rows: int = 16) -> None:
        self.conversations_dir = Path(conversations_dir)
        self._conversations_dir_str: str = str(self.conversations_dir)
        self.max_cache_entries = max(2, int(max_cache_entries))
        self.tail_rows = max(4, int(tail_rows))
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, Tuple[str, ConversationDbSnapshot]] = OrderedDict()
        self._full_cache: OrderedDict[str, _FullCacheEntry] = OrderedDict()
        self._max_full_cache_entries: int = min(48, max(12, int(max_cache_entries) * 3 // 4))

    def read(self, conversation_id: str) -> Optional[ConversationDbSnapshot]:
        if not conversation_id:
            return None
        with self._lock:
            db_path_str = os.path.join(self._conversations_dir_str, f"{conversation_id}.db")
            db_stamp = _file_stamp(db_path_str)
            if not db_stamp:
                self._cache.pop(conversation_id, None)
                return None

            wal_stamp = _file_stamp(f"{db_path_str}-wal") or "no-wal"
            combined_key = f"{db_stamp}|{wal_stamp}"
            cached = self._cache.get(conversation_id)
            if cached is not None and cached[0] == combined_key:
                self._cache.move_to_end(conversation_id)
                return cached[1]

            try:
                snap = self._read_from_disk(conversation_id, Path(db_path_str))
            except sqlite3.Error:
                # Preserve last good snapshot during transient WAL write lock contention.
                return cached[1] if cached is not None else None

            self._cache[conversation_id] = (combined_key, snap)
            self._cache.move_to_end(conversation_id)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
            return snap

    def _read_from_disk(self, conversation_id: str, db_path: Path) -> ConversationDbSnapshot:
        uri = _sqlite_ro_uri(db_path)
        conn = sqlite3.connect(uri, uri=True, timeout=0.35)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT idx, data FROM gen_metadata ORDER BY idx DESC LIMIT ?",
                (self.tail_rows,),
            ).fetchall()
            parsed: List[GenTelemetry] = []
            max_ctx = 0
            for idx, data in reversed(rows):
                item = parse_gen_metadata_row(int(idx), bytes(data) if data else b"")
                if item is None:
                    continue
                if item.max_context_tokens > max_ctx:
                    max_ctx = item.max_context_tokens
                parsed.append(item)

            last_step_ms = 0
            try:
                step_row = cur.execute("SELECT metadata FROM steps ORDER BY idx DESC LIMIT 1").fetchone()
                if step_row and step_row[0]:
                    sm = parse_pb_map(bytes(step_row[0]))
                    if sm.get(1) and isinstance(sm[1][0], bytes):
                        ts_map = parse_pb_map(sm[1][0])
                        if ts_map.get(1):
                            last_step_ms = int(ts_map[1][0]) * 1000
            except sqlite3.Error:
                pass

            is_subagent_hint = False
            try:
                tmb_row = cur.execute("SELECT length(data), data FROM trajectory_metadata_blob LIMIT 1").fetchone()
                if tmb_row and tmb_row[0] and int(tmb_row[0]) > 16384 and tmb_row[1]:
                    low_blob = bytes(tmb_row[1]).lower()
                    if b"subagent" in low_blob or b"swarm" in low_blob or b"worker" in low_blob:
                        is_subagent_hint = True
            except sqlite3.Error:
                pass
        finally:
            conn.close()

        latest = None
        for item in reversed(parsed):
            if item.total_in > 0 or item.output_tokens > 0:
                latest = item
                break
        if latest is None and parsed:
            latest = parsed[-1]

        return ConversationDbSnapshot(
            conversation_id=conversation_id,
            latest=latest,
            recent=tuple(parsed),
            max_context_tokens=max_ctx,
            last_step_epoch_ms=last_step_ms,
            is_subagent_hint=is_subagent_hint,
        )

    def read_full_history(self, conversation_id: str) -> List[GenTelemetry]:
        """Bounded (db+wal) stamp-cached incremental read of all gen_metadata rows in ascending idx order."""
        if not conversation_id:
            return []
        with self._lock:
            db_path = self.conversations_dir / f"{conversation_id}.db"
            stamp_info = _file_stamp_and_ino(db_path)
            if stamp_info is None:
                self._full_cache.pop(conversation_id, None)
                return []
            db_stamp, db_ino = stamp_info

            wal_stamp = _file_stamp(Path(f"{db_path}-wal")) or "no-wal"
            combined_key = f"{db_stamp}|{wal_stamp}"
            cached = self._full_cache.get(conversation_id)
            if cached is not None and cached[0] == combined_key:
                self._full_cache.move_to_end(conversation_id)
                return cached[6]

            uri = _sqlite_ro_uri(db_path)
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = sqlite3.connect(uri, uri=True, timeout=0.5)
                cur = conn.cursor()

                # Safe incremental path when db inode is unchanged and we have a prior cached snapshot.
                # Never relies on PRAGMA data_version (useless on fresh short-lived connections).
                if cached is not None and db_ino > 0 and cached[1] == db_ino:
                    _, c_ino, c_cnt, c_min, c_last, c_tail_sig, c_gens = cached
                    meta_row = cur.execute(
                        "SELECT COUNT(*), MIN(idx), MAX(idx) FROM gen_metadata"
                    ).fetchone()
                    if meta_row is not None:
                        cnt = int(meta_row[0] or 0)
                        mn = int(meta_row[1]) if meta_row[1] is not None else None
                        mx = int(meta_row[2]) if meta_row[2] is not None else None

                        # Case 1: Empty table remained empty
                        if cnt == 0 and c_cnt == 0 and c_last is None:
                            self._full_cache[conversation_id] = (
                                combined_key, c_ino, 0, None, None, (), c_gens
                            )
                            self._full_cache.move_to_end(conversation_id)
                            return c_gens

                        # Case 2: Non-empty prior snapshot -> verify last 2 raw rows at idx <= c_last
                        if (
                            c_cnt > 0
                            and c_last is not None
                            and mn == c_min
                            and mx is not None
                            and cnt >= c_cnt
                            and mx >= c_last
                        ):
                            tail_rows = cur.execute(
                                "SELECT idx, length(data), substr(data, 1, 32), substr(data, -32) "
                                "FROM gen_metadata WHERE idx <= ? ORDER BY idx DESC LIMIT 2",
                                (c_last,),
                            ).fetchall()
                            cur_tail_sig = tuple(_make_sql_row_sig(r) for r in tail_rows)

                            # 2a: WAL heartbeat / checkpoint flush without any row mutation -> 0 deserialization!
                            if cnt == c_cnt and mx == c_last and cur_tail_sig == c_tail_sig:
                                self._full_cache[conversation_id] = (
                                    combined_key, c_ino, c_cnt, c_min, c_last, c_tail_sig, c_gens
                                )
                                self._full_cache.move_to_end(conversation_id)
                                return c_gens

                            # 2b: Pure tail append -> deserialize ONLY new rows with idx > c_last into a new list copy
                            if cnt > c_cnt and mx > c_last and cur_tail_sig == c_tail_sig:
                                inc_rows = cur.execute(
                                    "SELECT idx, data FROM gen_metadata WHERE idx > ? ORDER BY idx ASC",
                                    (c_last,),
                                ).fetchall()
                                if (
                                    len(inc_rows) == (cnt - c_cnt)
                                    and inc_rows
                                    and int(inc_rows[-1][0]) == mx
                                ):
                                    new_gens = list(c_gens)
                                    for idx, data in inc_rows:
                                        item = parse_gen_metadata_row(int(idx), bytes(data) if data else b"")
                                        if item is not None and (item.total_in > 0 or item.output_tokens > 0):
                                            new_gens.append(item)
                                    if len(inc_rows) >= 2:
                                        new_tail_sig: Tuple[_TailRowSig, ...] = (
                                            _make_raw_row_sig(inc_rows[-1][0], inc_rows[-1][1]),
                                            _make_raw_row_sig(inc_rows[-2][0], inc_rows[-2][1]),
                                        )
                                    elif c_tail_sig:
                                        new_tail_sig = (
                                            _make_raw_row_sig(inc_rows[-1][0], inc_rows[-1][1]),
                                            c_tail_sig[0],
                                        )
                                    else:
                                        new_tail_sig = (
                                            _make_raw_row_sig(inc_rows[-1][0], inc_rows[-1][1]),
                                        )
                                    self._full_cache[conversation_id] = (
                                        combined_key, c_ino, cnt, mn, mx, new_tail_sig, new_gens
                                    )
                                    self._full_cache.move_to_end(conversation_id)
                                    while len(self._full_cache) > self._max_full_cache_entries:
                                        self._full_cache.popitem(last=False)
                                    return new_gens

                            # 2c: Previous c_last was a 0-token placeholder row (not present in c_gens) that got
                            # updated in-place (optionally with newer rows appended), while penultimate row is intact
                            last_was_placeholder = (not c_gens) or (c_gens[-1].idx < c_last)
                            penultimate_ok = (
                                len(c_tail_sig) < 2
                                or (len(cur_tail_sig) >= 2 and cur_tail_sig[1] == c_tail_sig[1])
                            )
                            if (
                                last_was_placeholder
                                and penultimate_ok
                                and cur_tail_sig
                                and cur_tail_sig[0][0] == c_last
                            ):
                                inc_rows = cur.execute(
                                    "SELECT idx, data FROM gen_metadata WHERE idx >= ? ORDER BY idx ASC",
                                    (c_last,),
                                ).fetchall()
                                expected_len = (cnt - c_cnt) + 1
                                if (
                                    len(inc_rows) == expected_len
                                    and inc_rows
                                    and int(inc_rows[0][0]) == c_last
                                    and int(inc_rows[-1][0]) == mx
                                ):
                                    new_gens = list(c_gens)
                                    for idx, data in inc_rows:
                                        item = parse_gen_metadata_row(int(idx), bytes(data) if data else b"")
                                        if item is not None and (item.total_in > 0 or item.output_tokens > 0):
                                            new_gens.append(item)
                                    if len(inc_rows) >= 2:
                                        new_tail_sig = (
                                            _make_raw_row_sig(inc_rows[-1][0], inc_rows[-1][1]),
                                            _make_raw_row_sig(inc_rows[-2][0], inc_rows[-2][1]),
                                        )
                                    elif len(c_tail_sig) >= 2:
                                        new_tail_sig = (
                                            _make_raw_row_sig(inc_rows[0][0], inc_rows[0][1]),
                                            c_tail_sig[1],
                                        )
                                    else:
                                        new_tail_sig = (
                                            _make_raw_row_sig(inc_rows[0][0], inc_rows[0][1]),
                                        )
                                    self._full_cache[conversation_id] = (
                                        combined_key, c_ino, cnt, mn, mx, new_tail_sig, new_gens
                                    )
                                    self._full_cache.move_to_end(conversation_id)
                                    while len(self._full_cache) > self._max_full_cache_entries:
                                        self._full_cache.popitem(last=False)
                                    return new_gens

                # Safe full re-read fallback (first load, inode changed, rollback, rewrite, or mismatch)
                rows = cur.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC").fetchall()
                parsed: List[GenTelemetry] = []
                for idx, data in rows:
                    item = parse_gen_metadata_row(int(idx), bytes(data) if data else b"")
                    if item is not None and (item.total_in > 0 or item.output_tokens > 0):
                        parsed.append(item)
                total_cnt = len(rows)
                if total_cnt > 0:
                    f_min: Optional[int] = int(rows[0][0])
                    f_last: Optional[int] = int(rows[-1][0])
                    f_tail_sig: Tuple[_TailRowSig, ...] = tuple(
                        _make_raw_row_sig(r[0], r[1]) for r in reversed(rows[-2:])
                    )
                else:
                    f_min = None
                    f_last = None
                    f_tail_sig = ()
                self._full_cache[conversation_id] = (
                    combined_key, db_ino, total_cnt, f_min, f_last, f_tail_sig, parsed
                )
                self._full_cache.move_to_end(conversation_id)
                while len(self._full_cache) > self._max_full_cache_entries:
                    self._full_cache.popitem(last=False)
                return parsed
            except sqlite3.Error:
                return cached[6] if cached is not None else []
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    def check_subagent_hint(self, conversation_id: str) -> bool:
        """Lightweight read-only subagent hint lookup without displacing the full snapshot LRU."""
        if not conversation_id:
            return False
        with self._lock:
            cached = self._cache.get(conversation_id)
            if cached is not None:
                return cached[1].is_subagent_hint
        db_path = self.conversations_dir / f"{conversation_id}.db"
        if not db_path.is_file():
            return False
        uri = _sqlite_ro_uri(db_path)
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.25)
            tmb_row = conn.execute("SELECT length(data), data FROM trajectory_metadata_blob LIMIT 1").fetchone()
            if tmb_row and tmb_row[0] and int(tmb_row[0]) > 16384 and tmb_row[1]:
                low_blob = bytes(tmb_row[1]).lower()
                return (b"subagent" in low_blob) or (b"swarm" in low_blob) or (b"worker" in low_blob)
            return False
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
