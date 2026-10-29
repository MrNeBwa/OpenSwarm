"""In-memory log ring buffer with redaction and optional rotation.

The daemon keeps the last :data:`DEFAULT_CAPACITY` entries for ``log.tail``
and mirrors them to a private file when a path is configured.  Secrets are
masked before an entry is stored, so no caller can accidentally expose a
token through the IPC API or the log file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path

from ..ipc import LogEntry, LogTailResult
from ..schema import LogLevel

DEFAULT_CAPACITY = 2000
DEFAULT_MAX_BYTES = 1024 * 1024
DEFAULT_BACKUPS = 5
MAX_MESSAGE_LENGTH = 16_384

_LEVEL_SEVERITY = {
    LogLevel.NONE: 0,
    LogLevel.TRACE: 1,
    LogLevel.DEBUG: 2,
    LogLevel.INFO: 3,
    LogLevel.WARNING: 4,
    LogLevel.ERROR: 5,
}

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SECRET_PAIR_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|uuid|private_key|public_key|short_id|pbk|sid)"
    r"\s*=\s*([^\s,;&]+)"
)
_URL_CREDENTIALS_RE = re.compile(r"(?i)\b(https?|socks5?)://([^/@\s]+)@")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_-]{40,}={0,2}\b")


def redact(text: str) -> str:
    """Mask credentials and identifiers that must never reach logs."""

    if not text:
        return text
    result = _UUID_RE.sub("[redacted-uuid]", str(text))
    result = _SECRET_PAIR_RE.sub(lambda match: f"{match.group(1)}=[redacted]", result)
    result = _URL_CREDENTIALS_RE.sub(r"\1://[redacted]@", result)
    result = _LONG_TOKEN_RE.sub("[redacted-token]", result)
    return result


def _coerce_level(value: str | LogLevel | None) -> LogLevel:
    if isinstance(value, LogLevel):
        return value
    if value is None:
        return LogLevel.INFO
    try:
        return LogLevel(str(value).strip().lower())
    except ValueError:
        return LogLevel.INFO


class LogBuffer:
    """Thread-safe bounded log store used by the core service."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        file_path: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backups: int = DEFAULT_BACKUPS,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(0, int(backups))
        self.file_path = Path(file_path).expanduser() if file_path is not None else None
        self._entries: deque[LogEntry] = deque(maxlen=self.capacity)
        self._lock = threading.RLock()
        self._dropped = 0

    # -- writing -----------------------------------------------------------
    def add(
        self,
        level: str | LogLevel,
        message: str,
        source: str = "core",
    ) -> LogEntry:
        entry = LogEntry(
            ts=time.time(),
            level=_coerce_level(level),
            message=redact(str(message))[:MAX_MESSAGE_LENGTH],
            source=str(source or "core")[:32],
        )
        with self._lock:
            if len(self._entries) == self.capacity:
                self._dropped += 1
            self._entries.append(entry)
            self._append_file(entry)
        return entry

    def _append_file(self, entry: LogEntry) -> None:
        if self.file_path is None:
            return
        path = self.file_path
        try:
            self._ensure_file(path)
            self._rotate_if_needed(path)
            line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            # Logging must never break the daemon.
            return

    def _ensure_file(self, path: Path) -> None:
        parent = path.parent
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"unsafe log directory: {parent}")
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)

    def _rotate_if_needed(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        for index in range(self.backups, 0, -1):
            source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
            target = path.with_name(f"{path.name}.{index}")
            if source.exists():
                with contextlib.suppress(OSError):
                    os.replace(source, target)
        with contextlib.suppress(OSError):
            os.unlink(path)

    # -- reading -----------------------------------------------------------
    def tail(
        self,
        *,
        lines: int = 200,
        level: str | LogLevel | None = None,
        since: float | None = None,
        source: str | None = None,
    ) -> LogTailResult:
        limit = max(1, min(int(lines), 5000))
        minimum = _coerce_level(level) if level is not None else None
        with self._lock:
            selected: list[LogEntry] = []
            for entry in self._entries:
                if (
                    minimum is not None
                    and _LEVEL_SEVERITY[entry.level] < _LEVEL_SEVERITY[minimum]
                ):
                    continue
                if since is not None and entry.ts <= float(since):
                    continue
                if source is not None and entry.source != source:
                    continue
                selected.append(entry)
        truncated = len(selected) > limit
        return LogTailResult(entries=selected[-limit:], truncated=truncated)

    def entries(self) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._dropped = 0

    def extend(self, entries: Iterable[LogEntry]) -> None:
        for entry in entries:
            self.add(entry.level, entry.message, entry.source)


__all__ = [
    "DEFAULT_BACKUPS",
    "DEFAULT_CAPACITY",
    "DEFAULT_MAX_BYTES",
    "LogBuffer",
    "redact",
]
