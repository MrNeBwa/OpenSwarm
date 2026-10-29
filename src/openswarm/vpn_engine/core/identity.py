"""Locally generated, pseudonymous device identifiers.

``hard``/``soft`` belong to *this* Linux installation.  Android identifiers,
APK signatures and Play Integrity tickets are never copied or emulated.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..schema import default_config_dir

IDENTITY_FILENAME = "identity.json"
MACHINE_ID_PATH = Path("/etc/machine-id")


@dataclass(frozen=True)
class DeviceIdentity:
    hard: str
    soft: str
    apksign: str = "none"
    premium: str = "none"
    protect: str = "none"


def _uuid3_name(value: str) -> str:
    digest = bytearray(hashlib.md5(value.encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _with_prefix(value: str) -> str:
    text = _uuid3_name(value)
    return "1" + text[1:]


def _machine_id() -> str:
    try:
        info = MACHINE_ID_PATH.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_size <= 4096:
            text = MACHINE_ID_PATH.read_text(encoding="ascii", errors="ignore").strip()
            if text:
                return text
    except OSError:
        pass
    return secrets.token_hex(16)


def _default_path() -> Path:
    return default_config_dir() / IDENTITY_FILENAME


def _ensure_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("identity directory is not a safe directory")


def _atomic_write(path: Path, payload: bytes) -> None:
    _ensure_directory(path.parent)
    if path.is_symlink():
        raise OSError("refusing to replace symlinked identity file")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _read_existing(path: Path) -> dict | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise OSError("identity file is not a small regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            raw = handle.read(64 * 1024 + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("identity file is corrupt") from exc
    if not isinstance(data, dict):
        raise OSError("identity file must contain an object")
    return data


def _valid_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36 or not value.startswith("1"):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def load_identity(path: Path | str | None = None) -> DeviceIdentity:
    """Load or create ``identity.json`` with mode ``0600``.

    The identifier is derived from ``/etc/machine-id`` plus a random install
    seed.  If the machine id is unavailable, a random seed is stored in the
    same private file so identifiers stay stable across restarts.
    """

    target = Path(path).expanduser() if path is not None else _default_path()
    existing = _read_existing(target)
    if existing is not None:
        hard = existing.get("hard")
        soft = existing.get("soft")
        if _valid_id(hard) and _valid_id(soft):
            return DeviceIdentity(hard=str(hard), soft=str(soft))

    machine = existing.get("machine") if existing else None
    if not isinstance(machine, str) or not machine:
        machine = _machine_id()
    install_seed = existing.get("seed") if existing else None
    if not isinstance(install_seed, str) or not install_seed:
        install_seed = secrets.token_hex(16)
    install_id = existing.get("install") if existing else None
    if not isinstance(install_id, str) or not install_id:
        install_id = secrets.token_hex(16)

    identity = DeviceIdentity(
        hard=_with_prefix(f"{machine}:{install_id}"),
        soft=_with_prefix(install_seed),
    )
    payload = {
        "version": 1,
        "hard": identity.hard,
        "soft": identity.soft,
        "seed": install_seed,
        "install": install_id,
        "machine": machine,
    }
    _atomic_write(
        target,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return identity


__all__ = ["IDENTITY_FILENAME", "DeviceIdentity", "load_identity"]
