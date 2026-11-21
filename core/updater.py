"""Safe background auto-update checker & dual-mode updater (Git fast-forward + ZIP archive).

Design guarantees:
- Non-blocking background checks (never touches the 2.5s telemetry poll hot path).
- Strict per-hop HTTPS + host/port/userinfo allowlist via custom HTTPRedirectHandler.
- Dual install-mode support:
  1. Git clones with local `git`: validates clean working tree + default branch + ancestor fast-forward (`--ff-only`).
  2. Non-Git / ZIP downloads without `git`: downloads official GitHub branch ZIP archive, enforces entry count
     and real uncompressed byte caps, blocks Zip-Slip / drive-prefix / symlink attacks, writes `version.json` LAST,
     backs up replaced files to `.update_backup`, retries transient Windows PermissionError locks, and performs
     atomic rollback with explicit error reporting if any write fails.
- Never overwrites dirty developer working trees; protects in-flight apply threads on shutdown.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from core.version import (
    APP_VERSION,
    DEFAULT_UPDATE_BRANCH,
    DEFAULT_UPDATE_REPO,
    is_newer_version,
    read_local_version_info,
)

ALLOWED_UPDATE_HOSTS: Set[str] = {
    "raw.githubusercontent.com",
    "api.github.com",
    "codeload.github.com",
    "github.com",
}

MAX_ZIP_BYTES = 32 * 1024 * 1024  # 32 MiB compressed cap
MAX_UNCOMPRESSED_BYTES = MAX_ZIP_BYTES * 3  # 96 MiB real extracted byte cap
MAX_ZIP_ENTRIES = 500
MAX_VERSION_JSON_BYTES = 64 * 1024  # 64 KiB cap
MAX_REDIRECT_HOPS = 5
HTTP_TIMEOUT_SEC = 10.0
MAX_TRANSIENT_AUTO_RETRIES = 3

# User-owned local runtime files/dirs that MUST NEVER be overwritten or deleted during ZIP update
PRESERVED_TOP_NAMES: Set[str] = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "config.json",
    "account_ledger.json",
    "logs",
    "preview_shots",
    "plans",
    "plans_agy",
    ".env",
    ".update_backup",
    ".update_staging",
}

PRESERVED_SUFFIXES: Tuple[str, ...] = (
    ".lnk",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".jsonl",
    ".log",
    ".tmp",
)

REQUIRED_ARCHIVE_FILES: Tuple[str, ...] = (
    "main.py",
    "version.json",
    "core/log_monitor.py",
    "core/config_store.py",
    "ui/capsule_app.py",
)

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")
_PIP_IMPORT_MAP: Dict[str, str] = {
    "pillow": "PIL",
    "pystray": "pystray",
    "pyyaml": "yaml",
    "requests": "requests",
}


def _validate_https_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"只允许 HTTPS 更新源: {url}")
    if parsed.username or parsed.password:
        raise ValueError("更新地址不允许携带凭据字段")
    if parsed.port not in (None, 443):
        raise ValueError(f"更新地址不允许使用非 443 端口: {parsed.port}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_UPDATE_HOSTS:
        raise ValueError(f"未授权的更新域名: {host}")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect hop BEFORE sending any request over the network."""

    def __init__(self, max_hops: int = MAX_REDIRECT_HOPS) -> None:
        super().__init__()
        self._max_hops = max(1, int(max_hops))
        self._hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._hops += 1
        if self._hops > self._max_hops:
            raise ValueError(f"更新重定向次数超过上限 ({self._max_hops})")
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        _validate_https_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _is_preserved_relpath(rel_posix: str) -> bool:
    clean = rel_posix.strip("/")
    if not clean:
        return True
    parts = clean.split("/")
    first = parts[0]
    if first in PRESERVED_TOP_NAMES:
        return True
    if any(p in ("__pycache__", ".git", ".venv", "venv") for p in parts):
        return True
    name_low = parts[-1].lower()
    if ".local." in name_low:
        return True
    if name_low.endswith(PRESERVED_SUFFIXES):
        return True
    return False


def _is_valid_repo_slug(repo: str) -> bool:
    parts = (repo or "").strip().split("/")
    if len(parts) != 2:
        return False
    return all(bool(p) and all(c.isalnum() or c in "._-" for c in p) for p in parts)


def _is_valid_branch_name(branch: str) -> bool:
    b = (branch or "").strip()
    if not b or ".." in b or b.startswith("/") or b.endswith("/") or "\\" in b:
        return False
    return all(c.isalnum() or c in "._-/" for c in b)


def _subprocess_no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _clear_readonly(path: Path) -> None:
    try:
        st = path.stat()
        if not (st.st_mode & stat.S_IWRITE):
            os.chmod(path, st.st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _replace_with_retry(src: Path, dst: Path, attempts: int = 3, delay_sec: float = 0.15) -> None:
    """Atomic os.replace with short backoff for transient Windows AV / indexer locks."""
    last_exc: Optional[Exception] = None
    for idx in range(max(1, attempts)):
        try:
            if dst.exists():
                _clear_readonly(dst)
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if idx + 1 < attempts:
                time.sleep(delay_sec)
    assert last_exc is not None
    raise last_exc


def check_requirements_satisfied(requirements_path: Path) -> Tuple[bool, List[str]]:
    """Verify whether all packages listed in requirements.txt are importable in current Python."""
    if not requirements_path.is_file():
        return True, []
    try:
        lines = requirements_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True, []

    mods: List[Tuple[str, str]] = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_NAME_RE.match(line)
        if not m:
            continue
        pkg = m.group(1)
        mod = _PIP_IMPORT_MAP.get(pkg.lower(), pkg.replace("-", "_"))
        mods.append((pkg, mod))

    if not mods:
        return True, []

    code = (
        "import importlib, json, sys\n"
        f"pairs = {mods!r}\n"
        "missing = []\n"
        "for pkg, mod in pairs:\n"
        "    try:\n"
        "        importlib.import_module(mod)\n"
        "    except Exception:\n"
        "        missing.append(pkg)\n"
        "print(json.dumps(missing))\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONUTF8", None)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=10.0,
            creationflags=_subprocess_no_window_flags(),
        )
        if proc.returncode != 0:
            return True, []
        missing = json.loads(proc.stdout.strip() or "[]")
        if isinstance(missing, list) and missing:
            return False, [str(x) for x in missing]
        return True, []
    except Exception:
        return True, []


@dataclass(frozen=True)
class UpdateCheckResult:
    checked_at: float
    local_version: str
    remote_version: str = ""
    has_update: bool = False
    summary: str = ""
    released_at: str = ""
    repo: str = DEFAULT_UPDATE_REPO
    branch: str = DEFAULT_UPDATE_BRANCH
    install_mode: str = "zip"  # "git" | "zip"
    remote_sha: str = ""
    local_sha: str = ""
    error: str = ""
    is_manual: bool = False


@dataclass(frozen=True)
class UpdateApplyResult:
    ok: bool
    method: str  # "git" | "zip" | "none"
    from_version: str
    to_version: str
    message: str = ""
    error: str = ""
    needs_restart: bool = False
    dirty_blocked: bool = False
    files_updated: int = 0
    requirements_changed: bool = False
    missing_requirements: Tuple[str, ...] = ()


class UpdateManager:
    """Handles remote version checks, safe updates, and periodic background polling."""

    def __init__(
        self,
        app_root: Path | str,
        repo: str = "",
        branch: str = "",
        http_get_fn: Optional[Callable[[str, float, int], bytes]] = None,
    ) -> None:
        self.app_root = Path(app_root).resolve()
        local_meta = read_local_version_info(self.app_root)
        cand_repo = repo.strip() or str(local_meta.get("repo") or DEFAULT_UPDATE_REPO)
        cand_branch = branch.strip() or str(local_meta.get("branch") or DEFAULT_UPDATE_BRANCH)
        self.repo = cand_repo if _is_valid_repo_slug(cand_repo) else DEFAULT_UPDATE_REPO
        self.branch = cand_branch if _is_valid_branch_name(cand_branch) else DEFAULT_UPDATE_BRANCH
        self._http_get = http_get_fn or self._default_http_get

        self._apply_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._apply_thread: Optional[threading.Thread] = None
        self._manual_check_thread: Optional[threading.Thread] = None
        self._last_result: Optional[UpdateCheckResult] = None
        self._blocked_auto_versions: Set[str] = set()  # permanent lock (dirty tree / fork / mismatch)
        self._transient_fail_counts: Dict[str, int] = {}

    @property
    def local_version(self) -> str:
        return str(read_local_version_info(self.app_root).get("version") or "0.0.0")

    @property
    def last_result(self) -> Optional[UpdateCheckResult]:
        return self._last_result

    def has_failed_auto_version(self, version: str) -> bool:
        if not version:
            return False
        with self._state_lock:
            if version in self._blocked_auto_versions:
                return True
            return self._transient_fail_counts.get(version, 0) >= MAX_TRANSIENT_AUTO_RETRIES

    def mark_auto_version_failed(self, version: str, permanent: bool = True) -> None:
        if not version:
            return
        with self._state_lock:
            if permanent:
                self._blocked_auto_versions.add(version)
            else:
                self._transient_fail_counts[version] = self._transient_fail_counts.get(version, 0) + 1

    def clear_failed_auto_version(self, version: str = "") -> None:
        with self._state_lock:
            if version:
                self._blocked_auto_versions.discard(version)
                self._transient_fail_counts.pop(version, None)
            else:
                self._blocked_auto_versions.clear()
                self._transient_fail_counts.clear()

    @staticmethod
    def _default_http_get(url: str, timeout: float = HTTP_TIMEOUT_SEC, max_bytes: int = MAX_ZIP_BYTES) -> bytes:
        safe_url = _validate_https_url(url)
        req = urllib.request.Request(
            safe_url,
            headers={
                "User-Agent": f"AntigravityMonitor-Updater/{APP_VERSION}",
                "Cache-Control": "no-cache",
            },
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler(MAX_REDIRECT_HOPS))
        with opener.open(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            if final_url:
                _validate_https_url(final_url)
            buf = io.BytesIO()
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"下载内容超过安全体积上限 ({max_bytes} bytes)")
                buf.write(chunk)
            return buf.getvalue()

    def _run_git(self, args: List[str], timeout: float = 15.0) -> Tuple[int, str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(self.app_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=timeout,
                creationflags=_subprocess_no_window_flags(),
            )
            return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
        except FileNotFoundError:
            return 127, "", "git_not_found"
        except subprocess.TimeoutExpired:
            return 124, "", "git_timeout"
        except OSError as exc:
            return 1, "", str(exc)

    def detect_install_mode(self) -> str:
        """Detect whether this folder is a usable Git checkout or a standalone/ZIP copy."""
        if not (self.app_root / ".git").exists():
            return "zip"
        rc, out, _ = self._run_git(["rev-parse", "--is-inside-work-tree"], timeout=5.0)
        if rc == 0 and out.lower() == "true":
            return "git"
        return "zip"

    def get_local_git_sha(self) -> str:
        if not (self.app_root / ".git").exists():
            return ""
        rc, out, _ = self._run_git(["rev-parse", "--short=7", "HEAD"], timeout=5.0)
        return out if rc == 0 else ""

    def is_git_working_tree_dirty(self) -> bool:
        """Return True when tracked OR unignored untracked files exist (prevents git merge overwrite collisions)."""
        if not (self.app_root / ".git").exists():
            return False
        rc, out, _ = self._run_git(["status", "--porcelain"], timeout=5.0)
        if rc != 0:
            return True
        return bool(out.strip())

    def version_manifest_url(self, bust_cache: bool = False) -> str:
        base = f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/version.json"
        if bust_cache:
            return f"{base}?_ts={int(time.time())}"
        return base

    def zip_archive_url(self) -> str:
        return f"https://codeload.github.com/{self.repo}/zip/refs/heads/{self.branch}"

    def check_for_update(self, is_manual: bool = False) -> UpdateCheckResult:
        """Query remote version.json (no API rate-limit) and compare with local version."""
        with self._check_lock:
            now = time.time()
            local_ver = self.local_version
            mode = self.detect_install_mode()
            local_sha = self.get_local_git_sha() if mode == "git" else ""

            try:
                raw_bytes = self._http_get(
                    self.version_manifest_url(bust_cache=is_manual),
                    HTTP_TIMEOUT_SEC,
                    MAX_VERSION_JSON_BYTES,
                )
                payload = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("远端 version.json 格式无效")
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
                res = UpdateCheckResult(
                    checked_at=now,
                    local_version=local_ver,
                    repo=self.repo,
                    branch=self.branch,
                    install_mode=mode,
                    local_sha=local_sha,
                    error=f"检查更新失败: {exc}",
                    is_manual=is_manual,
                )
                self._last_result = res
                return res

            remote_ver = str(payload.get("version") or "").strip()
            summary = str(payload.get("summary") or "").strip()
            released_at = str(payload.get("released_at") or "").strip()
            has_upd = is_newer_version(remote_ver, local_ver)

            res = UpdateCheckResult(
                checked_at=now,
                local_version=local_ver,
                remote_version=remote_ver or local_ver,
                has_update=has_upd,
                summary=summary,
                released_at=released_at,
                repo=self.repo,
                branch=self.branch,
                install_mode=mode,
                local_sha=local_sha,
                is_manual=is_manual,
            )
            self._last_result = res
            return res

    def apply_update(self, expected_remote_version: str = "") -> UpdateApplyResult:
        """Safely apply update using Git fast-forward or atomic ZIP replacement."""
        if not self._apply_lock.acquire(blocking=False):
            return UpdateApplyResult(
                ok=False,
                method="none",
                from_version=self.local_version,
                to_version=expected_remote_version or self.local_version,
                error="已有更新任务正在执行，请稍候",
            )
        try:
            local_ver = self.local_version
            target_ver = expected_remote_version.strip()
            if not target_ver:
                chk = self.check_for_update(is_manual=True)
                if chk.error:
                    return UpdateApplyResult(
                        ok=False,
                        method=chk.install_mode,
                        from_version=local_ver,
                        to_version=local_ver,
                        error=chk.error,
                    )
                if not chk.has_update:
                    return UpdateApplyResult(
                        ok=True,
                        method="none",
                        from_version=local_ver,
                        to_version=local_ver,
                        message=f"当前已是最新版本 v{local_ver}",
                        needs_restart=False,
                    )
                target_ver = chk.remote_version

            mode = self.detect_install_mode()
            if mode == "git":
                return self._apply_via_git(local_ver, target_ver)
            return self._apply_via_zip(local_ver, target_ver)
        finally:
            self._apply_lock.release()

    def _apply_via_git(self, local_ver: str, target_ver: str) -> UpdateApplyResult:
        if self.is_git_working_tree_dirty():
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error="检测到本地仓库存在未提交改动或未跟踪文件，已暂停自动更新以保护代码",
                dirty_blocked=True,
            )

        rc_br, cur_branch, err_br = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0)
        if rc_br != 0 or not cur_branch or cur_branch == "HEAD" or cur_branch != self.branch:
            reason = cur_branch if rc_br == 0 and cur_branch else (err_br or "detached/unknown")
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error=f"当前处于非默认分支 ({reason})，已跳过自动覆盖更新",
                dirty_blocked=True,
            )

        req_before = self._read_file_bytes_safe(self.app_root / "requirements.txt")

        rc_fetch, _, err_fetch = self._run_git(["fetch", "origin", self.branch], timeout=25.0)
        if rc_fetch != 0:
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error=f"git fetch 失败: {err_fetch or f'exit {rc_fetch}'}",
            )

        if self._stop_event.is_set():
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error="应用正在退出，已中止本次更新",
            )

        # Ensure strict fast-forward safety (local HEAD must be an ancestor of FETCH_HEAD)
        rc_anc, _, _ = self._run_git(["merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"], timeout=8.0)
        if rc_anc != 0:
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error="本地提交历史与远端分叉，无法安全快进合并，请手动 git pull",
                dirty_blocked=True,
            )

        rc_head, old_head, _ = self._run_git(["rev-parse", "HEAD"], timeout=5.0)
        rc_fhead, new_head, _ = self._run_git(["rev-parse", "FETCH_HEAD"], timeout=5.0)
        if rc_head == 0 and rc_fhead == 0 and old_head == new_head:
            if is_newer_version(target_ver, self.local_version):
                return UpdateApplyResult(
                    ok=False,
                    method="git",
                    from_version=local_ver,
                    to_version=target_ver,
                    error=f"远端 origin/{self.branch} 尚未同步至 v{target_ver}，请稍后再试或检查 remote",
                    dirty_blocked=True,
                )
            return UpdateApplyResult(
                ok=True,
                method="git",
                from_version=local_ver,
                to_version=self.local_version,
                message=f"当前已是最新版本 v{self.local_version}",
                needs_restart=False,
            )

        rc_merge, _, err_merge = self._run_git(["merge", "--ff-only", "FETCH_HEAD"], timeout=20.0)
        if rc_merge != 0:
            is_dirty = "untracked working tree files" in err_merge.lower() or "overwritten by merge" in err_merge.lower()
            return UpdateApplyResult(
                ok=False,
                method="git",
                from_version=local_ver,
                to_version=target_ver,
                error=f"git 快进更新失败: {err_merge or f'exit {rc_merge}'}",
                dirty_blocked=is_dirty,
            )

        req_after = self._read_file_bytes_safe(self.app_root / "requirements.txt")
        req_changed = req_before != req_after
        missing_reqs: Tuple[str, ...] = ()
        if req_changed:
            ok_req, miss = check_requirements_satisfied(self.app_root / "requirements.txt")
            if not ok_req:
                missing_reqs = tuple(miss)
        new_ver = self.local_version
        return UpdateApplyResult(
            ok=True,
            method="git",
            from_version=local_ver,
            to_version=new_ver,
            message=f"已通过 Git 更新至 v{new_ver}",
            needs_restart=True,
            requirements_changed=req_changed,
            missing_requirements=missing_reqs,
        )

    def _apply_via_zip(self, local_ver: str, target_ver: str) -> UpdateApplyResult:
        try:
            zip_bytes = self._http_get(self.zip_archive_url(), 30.0, MAX_ZIP_BYTES)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return UpdateApplyResult(
                ok=False,
                method="zip",
                from_version=local_ver,
                to_version=target_ver,
                error=f"下载更新包失败: {exc}",
            )

        if self._stop_event.is_set():
            return UpdateApplyResult(
                ok=False,
                method="zip",
                from_version=local_ver,
                to_version=target_ver,
                error="应用正在退出，已中止写入更新包",
            )

        req_before = self._read_file_bytes_safe(self.app_root / "requirements.txt")
        staging_root = Path(tempfile.mkdtemp(prefix="agy_upd_stage_"))
        backup_dir = self.app_root / ".update_backup"
        replaced_files: List[Path] = []
        created_files: List[Path] = []
        created_dirs: List[Path] = []

        try:
            try:
                extracted_root = self._extract_and_validate_zip(zip_bytes, staging_root)
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=target_ver,
                    error=f"更新包安全校验失败: {exc}",
                )
            if extracted_root is None:
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=target_ver,
                    error="更新包校验失败：缺少必要程序文件",
                )

            # Strictly parse version.json inside the archive (never silently fall back to local APP_VERSION)
            vf_staged = extracted_root / "version.json"
            try:
                staged_raw = json.loads(vf_staged.read_text(encoding="utf-8"))
                staged_ver = str(staged_raw.get("version") or "").strip() if isinstance(staged_raw, dict) else ""
            except (OSError, json.JSONDecodeError):
                staged_ver = ""
            if not staged_ver:
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=target_ver,
                    error="更新包 version.json 无效或缺失版本号",
                )
            if not is_newer_version(staged_ver, local_ver):
                return UpdateApplyResult(
                    ok=True,
                    method="zip",
                    from_version=local_ver,
                    to_version=local_ver,
                    message=f"当前已是最新版本 v{local_ver}",
                    needs_restart=False,
                )

            # Collect safe managed files and force version.json to be written LAST (commit marker)
            managed_relpaths: List[str] = []
            for item in sorted(extracted_root.rglob("*")):
                if not item.is_file() or item.is_symlink():
                    continue
                rel_posix = item.relative_to(extracted_root).as_posix()
                if _is_preserved_relpath(rel_posix):
                    continue
                managed_relpaths.append(rel_posix)

            if not managed_relpaths:
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=target_ver,
                    error="更新包中未发现可更新文件",
                )

            if "version.json" in managed_relpaths:
                managed_relpaths = [p for p in managed_relpaths if p != "version.json"] + ["version.json"]

            # Pre-flight collision check: never try to replace an existing directory with a file
            for rel_posix in managed_relpaths:
                dst_check = self.app_root / Path(rel_posix)
                self._ensure_within_root(dst_check, self.app_root)
                if dst_check.is_dir():
                    return UpdateApplyResult(
                        ok=False,
                        method="zip",
                        from_version=local_ver,
                        to_version=staged_ver,
                        error=f"目标路径冲突（存在同名目录）: {rel_posix}",
                        dirty_blocked=True,
                    )

            if self._stop_event.is_set():
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=staged_ver,
                    error="应用正在退出，已中止写入更新包",
                )

            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            backup_dir.mkdir(parents=True, exist_ok=True)

            # Apply files atomically with full rollback on any OS/write failure
            changed_count = 0
            try:
                for rel_posix in managed_relpaths:
                    src = extracted_root / Path(rel_posix)
                    dst = self.app_root / Path(rel_posix)
                    self._ensure_within_root(dst, self.app_root)
                    if not dst.parent.exists():
                        # Track newly created directories so rollback leaves workspace spotless
                        cur = dst.parent
                        to_track: List[Path] = []
                        while cur != self.app_root and not cur.exists():
                            to_track.append(cur)
                            cur = cur.parent
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        created_dirs.extend(to_track)

                    if dst.is_file():
                        try:
                            if dst.read_bytes() == src.read_bytes():
                                continue
                        except OSError:
                            pass
                        bak = backup_dir / Path(rel_posix)
                        bak.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(dst, bak)
                        replaced_files.append(Path(rel_posix))
                    else:
                        created_files.append(Path(rel_posix))

                    tmp_dst = dst.with_name(f"{dst.name}.upd.{os.getpid()}.tmp")
                    try:
                        shutil.copy2(src, tmp_dst)
                        _replace_with_retry(tmp_dst, dst)
                        changed_count += 1
                    finally:
                        if tmp_dst.exists():
                            try:
                                tmp_dst.unlink()
                            except OSError:
                                pass
            except Exception as apply_exc:
                rollback_errors: List[str] = []
                for rel_p in reversed(created_files):
                    try:
                        target_f = self.app_root / rel_p
                        if target_f.exists():
                            _clear_readonly(target_f)
                            target_f.unlink()
                    except OSError as exc:
                        rollback_errors.append(f"{rel_p}: {exc}")
                for rel_p in reversed(replaced_files):
                    bak = backup_dir / rel_p
                    dst_p = self.app_root / rel_p
                    if bak.is_file():
                        rb_tmp = dst_p.with_name(f"{dst_p.name}.rb.{os.getpid()}.tmp")
                        try:
                            shutil.copy2(bak, rb_tmp)
                            _replace_with_retry(rb_tmp, dst_p)
                        except OSError as exc:
                            rollback_errors.append(f"{rel_p}: {exc}")
                        finally:
                            if rb_tmp.exists():
                                try:
                                    rb_tmp.unlink()
                                except OSError:
                                    pass
                for d_path in sorted(created_dirs, key=lambda p: len(p.parts), reverse=True):
                    try:
                        if d_path.is_dir():
                            d_path.rmdir()
                    except OSError:
                        pass

                if not rollback_errors:
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    err_msg = f"写入更新文件失败并已自动回滚: {apply_exc}"
                else:
                    err_msg = (
                        f"写入更新文件失败({apply_exc})，部分文件回滚未完成（已保留 .update_backup）: "
                        + "; ".join(rollback_errors[:2])
                    )
                return UpdateApplyResult(
                    ok=False,
                    method="zip",
                    from_version=local_ver,
                    to_version=staged_ver,
                    error=err_msg,
                )

            req_after = self._read_file_bytes_safe(self.app_root / "requirements.txt")
            req_changed = req_before != req_after
            missing_reqs: Tuple[str, ...] = ()
            if req_changed:
                ok_req, miss = check_requirements_satisfied(self.app_root / "requirements.txt")
                if not ok_req:
                    missing_reqs = tuple(miss)
            shutil.rmtree(backup_dir, ignore_errors=True)
            return UpdateApplyResult(
                ok=True,
                method="zip",
                from_version=local_ver,
                to_version=staged_ver,
                message=f"已下载并更新至 v{staged_ver}",
                needs_restart=True,
                files_updated=changed_count,
                requirements_changed=req_changed,
                missing_requirements=missing_reqs,
            )
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    @staticmethod
    def _ensure_within_root(target: Path, root: Path) -> None:
        resolved_root = root.resolve()
        resolved_target = target.resolve()
        if resolved_root != resolved_target and resolved_root not in resolved_target.parents:
            raise ValueError(f"路径越界被拦截: {target}")

    @staticmethod
    def _read_file_bytes_safe(path: Path) -> bytes:
        try:
            return path.read_bytes() if path.is_file() else b""
        except OSError:
            return b""

    def _extract_and_validate_zip(self, zip_bytes: bytes, staging_root: Path) -> Optional[Path]:
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            return None

        with zf:
            declared_uncompressed = 0
            entries: List[Tuple[zipfile.ZipInfo, str]] = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if len(entries) >= MAX_ZIP_ENTRIES:
                    raise ValueError(f"ZIP 文件条目超过安全上限 ({MAX_ZIP_ENTRIES})")
                if int(info.file_size) < 0:
                    raise ValueError("ZIP 文件头大小字段非法")
                # Reject symlinks encoded in POSIX external_attr
                unix_mode = (info.external_attr >> 16) & 0xF000
                if unix_mode == 0xA000:
                    continue
                raw_name = info.filename.replace("\\", "/")
                if any(ord(ch) < 32 for ch in raw_name):
                    raise ValueError(f"ZIP 包含控制字符路径: {info.filename!r}")
                norm_name = posixpath.normpath(raw_name)
                segs = norm_name.split("/")
                if (
                    norm_name.startswith("/")
                    or norm_name.startswith("..")
                    or ".." in segs
                    or any(":" in seg for seg in segs)
                ):
                    raise ValueError(f"ZIP 包含非法路径: {info.filename}")
                declared_uncompressed += int(info.file_size)
                if declared_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("ZIP 解压体积超过安全上限")
                entries.append((info, norm_name))

            if not entries:
                return None

            # Strip GitHub's single top-level wrapper folder (e.g. antigravity-monitor-main/)
            top_dirs = {name.split("/", 1)[0] for _, name in entries if "/" in name}
            strip_prefix = ""
            if len(top_dirs) == 1 and all("/" in name for _, name in entries):
                strip_prefix = next(iter(top_dirs)) + "/"

            out_root = staging_root / "pkg"
            out_root.mkdir(parents=True, exist_ok=True)
            actual_extracted_bytes = 0
            for info, norm_name in entries:
                rel = norm_name[len(strip_prefix) :] if strip_prefix else norm_name
                if not rel or rel.startswith("/") or rel.startswith(".."):
                    continue
                dst = out_root / Path(rel)
                self._ensure_within_root(dst, out_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_fh, dst.open("wb") as dst_fh:
                    while True:
                        block = src_fh.read(65536)
                        if not block:
                            break
                        actual_extracted_bytes += len(block)
                        if actual_extracted_bytes > MAX_UNCOMPRESSED_BYTES:
                            raise ValueError("ZIP 真实解压体积超过安全上限")
                        dst_fh.write(block)

            for req_rel in REQUIRED_ARCHIVE_FILES:
                if not (out_root / Path(req_rel)).is_file():
                    return None
            return out_root

    def start_background_loop(
        self,
        is_enabled_fn: Callable[[], bool],
        interval_min_fn: Callable[[], int],
        on_result: Callable[[UpdateCheckResult], None],
        initial_delay_sec: float = 8.0,
    ) -> None:
        """Start a single low-overhead daemon thread for periodic non-blocking update checks."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._wakeup_event.clear()

        def _loop() -> None:
            if initial_delay_sec > 0:
                if self._stop_event.wait(timeout=initial_delay_sec):
                    return
            first_retry_done = False
            while not self._stop_event.is_set():
                retry_soon = False
                try:
                    if is_enabled_fn():
                        res = self.check_for_update(is_manual=False)
                        if res.error and not first_retry_done:
                            # Boot-time Wi-Fi / proxy warm-up grace: retry once after 60s
                            first_retry_done = True
                            retry_soon = True
                        elif not res.error:
                            first_retry_done = True
                        try:
                            on_result(res)
                        except Exception:
                            pass
                except Exception:
                    pass
                interval_sec = 60.0 if retry_soon else max(600.0, float(interval_min_fn()) * 60.0)
                deadline = time.monotonic() + interval_sec
                while not self._stop_event.is_set():
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        break
                    if self._wakeup_event.wait(timeout=min(rem, 5.0)):
                        self._wakeup_event.clear()
                        break

        self._thread = threading.Thread(target=_loop, name="MonitorUpdateCheckThread", daemon=True)
        self._thread.start()

    def trigger_async_check(
        self,
        on_result: Callable[[UpdateCheckResult], None],
        is_manual: bool = True,
    ) -> Optional[threading.Thread]:
        """Run a one-shot non-blocking update check; coalesces overlapping manual clicks."""
        if self._manual_check_thread is not None and self._manual_check_thread.is_alive():
            return self._manual_check_thread

        def _run() -> None:
            res = self.check_for_update(is_manual=is_manual)
            try:
                on_result(res)
            except Exception:
                pass

        t = threading.Thread(target=_run, name="MonitorManualUpdateCheck", daemon=True)
        self._manual_check_thread = t
        t.start()
        return t

    def trigger_async_apply(
        self,
        expected_remote_version: str,
        on_done: Callable[[UpdateApplyResult], None],
    ) -> threading.Thread:
        """Run update installation on a tracked worker thread joined cleanly during shutdown."""

        def _run() -> None:
            res = self.apply_update(expected_remote_version=expected_remote_version)
            try:
                on_done(res)
            except Exception:
                pass

        t = threading.Thread(target=_run, name="MonitorApplyUpdate", daemon=True)
        self._apply_thread = t
        t.start()
        return t

    def stop(self) -> None:
        self._stop_event.set()
        self._wakeup_event.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        # Wait for any in-flight file replacement / git merge critical section to finish safely
        ap = self._apply_thread
        self._apply_thread = None
        if ap is not None and ap.is_alive() and ap is not threading.current_thread():
            ap.join(timeout=15.0)
