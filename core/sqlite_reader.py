"""Read-only SQLite + Protobuf telemetry extractor for Antigravity conversations/*.db.

Uses (db + wal) inode/size/mtime stamp caching with a bounded LRU cache so unchanged
conversation databases incur zero SQLite opens during steady-state polling.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class ConversationDbSnapshot:
    conversation_id: str
    latest: Optional[GenTelemetry]
    recent: Tuple[GenTelemetry, ...]
    max_context_tokens: int
    last_step_epoch_ms: int
    is_subagent_hint: bool = False


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

    # Prefer pure streaming TPS when streaming duration is meaningful (>0.15s); fall back to total duration.
    tps: Optional[float] = None
    if total_out > 0:
        if streaming_sec is not None and streaming_sec >= 0.15:
            tps = total_out / streaming_sec
        elif ttft_sec is not None and streaming_sec is not None and (ttft_sec + streaming_sec) > 0:
            tps = total_out / (ttft_sec + streaming_sec)

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

    model_label = ""
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


def _file_stamp(path: Path) -> Optional[str]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    if st.st_size <= 0:
        return None
    return f"{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _sqlite_ro_uri(db_path: Path) -> str:
    resolved = db_path.resolve()
    posix_path = resolved.as_posix()
    encoded = urllib.parse.quote(posix_path, safe="/:")
    return f"file:///{encoded}?mode=ro" if not encoded.startswith("/") else f"file://{encoded}?mode=ro"


class ConversationDbReader:
    """Bounded LRU reader over Antigravity conversations/<id>.db files."""

    def __init__(self, conversations_dir: Path | str, max_cache_entries: int = 12, tail_rows: int = 16) -> None:
        self.conversations_dir = Path(conversations_dir)
        self.max_cache_entries = max(2, int(max_cache_entries))
        self.tail_rows = max(4, int(tail_rows))
        self._cache: OrderedDict[str, Tuple[str, ConversationDbSnapshot]] = OrderedDict()

    def read(self, conversation_id: str) -> Optional[ConversationDbSnapshot]:
        if not conversation_id:
            return None
        db_path = self.conversations_dir / f"{conversation_id}.db"
        db_stamp = _file_stamp(db_path)
        if not db_stamp:
            self._cache.pop(conversation_id, None)
            return None

        wal_stamp = _file_stamp(Path(f"{db_path}-wal")) or "no-wal"
        combined_key = f"{db_stamp}|{wal_stamp}"
        cached = self._cache.get(conversation_id)
        if cached is not None and cached[0] == combined_key:
            self._cache.move_to_end(conversation_id)
            return cached[1]

        try:
            snap = self._read_from_disk(conversation_id, db_path)
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
        """On-demand read of all gen_metadata rows in ascending idx order for detail charts.

        Opens SQLite strictly read-only and closes immediately without polluting the polling LRU cache.
        """
        if not conversation_id:
            return []
        db_path = self.conversations_dir / f"{conversation_id}.db"
        if not db_path.is_file():
            return []
        uri = _sqlite_ro_uri(db_path)
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            rows = conn.execute("SELECT idx, data FROM gen_metadata ORDER BY idx ASC").fetchall()
            parsed: List[GenTelemetry] = []
            for idx, data in rows:
                item = parse_gen_metadata_row(int(idx), bytes(data) if data else b"")
                if item is not None and (item.total_in > 0 or item.output_tokens > 0):
                    parsed.append(item)
            return parsed
        except sqlite3.Error:
            return []
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
