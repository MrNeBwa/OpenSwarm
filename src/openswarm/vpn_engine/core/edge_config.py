"""Native Ninja VPN edge configuration (``edge.bin`` / ``edge.json``).

The container is an obfuscation wrapper, not a security boundary: the AES key
travels in the same file.  The real trust anchors are the TLS SPKI pins and the
ECDSA ``cert`` used by :mod:`ninjavpn_client.core.edge_client`.

Nothing in this module logs or includes raw payload bytes in error messages.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

MAX_EDGE_FILE_BYTES = 1024 * 1024
MAX_CONTAINER_PAD = 255
GCM_IV_BYTES = 12
GCM_KEY_BYTES = 32
GCM_TAG_BYTES = 16


class EdgeConfigError(ValueError):
    """Raised when an edge config is missing, malformed or unsafe."""

    def __init__(self, message: str, *, code: str = "EDGE_CONFIG_INVALID"):
        self.code = code
        super().__init__(message)


class EdgePathType(StrEnum):
    TEST = "test"
    MAIN = "main"
    RAND = "rand"
    MASK = "mask"


@dataclass(frozen=True)
class EdgePath:
    type: EdgePathType
    host: str
    addrs: tuple[str, ...]
    pins: frozenset[bytes]
    find: Mapping[str, Any] | None = None
    xray: Mapping[str, Any] | None = None
    #: Original front host for MASK paths (the generated ``*.net`` name is only
    #: used for diagnostics/URLs).
    sni: str = ""

    @property
    def tls_host(self) -> str:
        return self.sni or self.host


@dataclass(frozen=True)
class EdgeConfig:
    paths: Mapping[EdgePathType, EdgePath]
    cert_der: bytes
    time: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", dict(self.paths))

    @property
    def cert_public_key(self) -> ec.EllipticCurvePublicKey:
        try:
            key = x509.load_der_x509_certificate(self.cert_der).public_key()
        except (ValueError, TypeError):
            # ``cert`` in the APK is a bare SubjectPublicKeyInfo, not an X.509
            # certificate.  Support both forms.
            from cryptography.hazmat.primitives.serialization import (
                load_der_public_key,
            )

            key = load_der_public_key(self.cert_der)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise EdgeConfigError("edge cert must be a P-256 public key")
        return key


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------


def _u8(data: bytes, offset: int) -> int:
    if offset >= len(data):
        raise EdgeConfigError("truncated edge container")
    return data[offset]


def decode_edge_container(raw: bytes) -> bytes:
    """Decrypt the ``edge.bin`` container and return the JSON bytes."""

    if not isinstance(raw, (bytes, bytearray)):
        raise EdgeConfigError("edge container must be bytes")
    payload = bytes(raw)
    if len(payload) > MAX_EDGE_FILE_BYTES:
        raise EdgeConfigError("edge file is larger than 1 MiB")
    if not payload:
        raise EdgeConfigError("edge container is empty")

    offset = 0
    pad_len = _u8(payload, offset)
    offset += 1
    if pad_len > MAX_CONTAINER_PAD:
        raise EdgeConfigError("edge container padding is too large")
    if offset + pad_len > len(payload):
        raise EdgeConfigError("truncated edge container padding")
    offset += pad_len

    iv_len = _u8(payload, offset)
    offset += 1
    if iv_len != GCM_IV_BYTES:
        raise EdgeConfigError("edge container IV must be 12 bytes")
    if offset + iv_len > len(payload):
        raise EdgeConfigError("truncated edge container IV")
    iv = payload[offset : offset + iv_len]
    offset += iv_len

    if offset + GCM_KEY_BYTES > len(payload):
        raise EdgeConfigError("truncated edge container key")
    key = payload[offset : offset + GCM_KEY_BYTES]
    offset += GCM_KEY_BYTES

    ciphertext = payload[offset:]
    if len(ciphertext) < GCM_TAG_BYTES:
        raise EdgeConfigError("edge container ciphertext is too short")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    except Exception as exc:  # InvalidTag and cryptography internals
        raise EdgeConfigError("edge container authentication failed") from exc
    if len(plaintext) > MAX_EDGE_FILE_BYTES:
        raise EdgeConfigError("decoded edge config is larger than 1 MiB")
    return plaintext


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _normalise_addr(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise EdgeConfigError("edge address is empty")
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    if "/" in text:
        # ``addr`` entries may carry an IPv4/IPv6 prefix; the socket only
        # needs the bare literal.
        text = text.split("/", 1)[0]
    try:
        ipaddress.ip_address(text)
    except ValueError as exc:
        raise EdgeConfigError("edge addr must be an IP literal") from exc
    return text


def _pins(value: Any) -> frozenset[bytes]:
    if not isinstance(value, (list, tuple)) or not value:
        raise EdgeConfigError("edge pins must be a non-empty list")
    result: set[bytes] = set()
    for item in value:
        if not isinstance(item, str):
            raise EdgeConfigError("edge pin must be a Base64 string")
        try:
            decoded = base64.b64decode(item, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EdgeConfigError("edge pin is not valid Base64") from exc
        if len(decoded) != 32:
            raise EdgeConfigError("edge pin must decode to 32 bytes")
        result.add(decoded)
    return frozenset(result)


def _cert_der(value: Any) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise EdgeConfigError("edge cert is missing")
    try:
        der = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EdgeConfigError("edge cert is not valid Base64") from exc
    if not der or len(der) > 4096:
        raise EdgeConfigError("edge cert has an invalid size")
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        PublicFormat,
        load_der_public_key,
    )

    try:
        key = load_der_public_key(der)
    except (ValueError, TypeError):
        try:
            key = x509.load_der_x509_certificate(der).public_key()
        except (ValueError, TypeError) as exc:
            raise EdgeConfigError("edge cert is not a DER public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise EdgeConfigError("edge cert must be a P-256 public key")
    # Normalise X.509 certificates to a bare SubjectPublicKeyInfo so the edge
    # client always signs against the same representation.
    return key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _uuid3_name(value: str) -> str:
    """Java ``UUID.nameUUIDFromBytes`` (MD5, version 3, RFC 4122 variant)."""

    digest = bytearray(hashlib.md5(value.encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def generate_path_host(record: Mapping[str, Any], device_id: str, suffix: str) -> str:
    """Deterministic ``*.com``/``*.net`` host for RAND/MASK paths."""

    inner = _uuid3_name(_canonical(dict(record)))
    seed = f"{device_id}:{inner}"
    return _uuid3_name(seed) + suffix


def _path_from_record(
    kind: EdgePathType,
    record: Any,
    *,
    device_id: str,
) -> EdgePath:
    if not isinstance(record, Mapping):
        raise EdgeConfigError(f"edge path {kind.value} must be an object")
    host = str(record.get("host") or "").strip()
    addrs_raw = record.get("addr")
    if not isinstance(addrs_raw, (list, tuple)) or not addrs_raw:
        raise EdgeConfigError(f"edge path {kind.value} has no addresses")
    addrs = tuple(_normalise_addr(item) for item in addrs_raw)
    pins = _pins(record.get("pins"))

    find = record.get("find")
    xray = record.get("xray")
    if kind in (EdgePathType.TEST, EdgePathType.MAIN):
        if not host:
            raise EdgeConfigError(f"edge path {kind.value} has no host")
        generated = host
    elif kind is EdgePathType.RAND:
        generated = generate_path_host(record, device_id, ".com")
    else:
        if not host:
            raise EdgeConfigError("edge path mask has no front host")
        if not isinstance(find, Mapping) or not isinstance(xray, Mapping):
            raise EdgeConfigError("edge path mask requires find and xray")
        generated = generate_path_host(record, device_id, ".net")

    return EdgePath(
        type=kind,
        host=generated,
        addrs=addrs,
        pins=pins,
        find=dict(find) if isinstance(find, Mapping) else None,
        xray=dict(xray) if isinstance(xray, Mapping) else None,
        sni=host if kind is EdgePathType.MASK else "",
    )


def parse_edge_config(raw: bytes | str, *, device_id: str) -> EdgeConfig:
    """Parse a decrypted (or plain JSON) edge config."""

    if not isinstance(device_id, str) or not device_id.strip():
        raise EdgeConfigError("device_id is required")
    if isinstance(raw, str):
        text = raw.lstrip("\ufeff \t\r\n")
        if len(text.encode("utf-8")) > MAX_EDGE_FILE_BYTES:
            raise EdgeConfigError("edge file is larger than 1 MiB")
    else:
        data = bytes(raw)
        if len(data) > MAX_EDGE_FILE_BYTES:
            raise EdgeConfigError("edge file is larger than 1 MiB")
        stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if stripped.startswith(b"{"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise EdgeConfigError("edge JSON is not UTF-8") from exc
        else:
            plain = decode_edge_container(data)
            try:
                text = plain.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise EdgeConfigError("decoded edge config is not UTF-8") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EdgeConfigError("edge config is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise EdgeConfigError("edge config root must be an object")

    paths: dict[EdgePathType, EdgePath] = {}
    for kind in EdgePathType:
        record = parsed.get(kind.value)
        if record is None:
            continue
        paths[kind] = _path_from_record(kind, record, device_id=device_id)
    if not paths:
        raise EdgeConfigError("edge config has no paths", code="EDGE_PATH_UNAVAILABLE")

    cert = _cert_der(parsed.get("cert"))
    time_value = parsed.get("time", 0)
    if not isinstance(time_value, int) or isinstance(time_value, bool) or time_value < 0:
        time_value = 0
    return EdgeConfig(paths=paths, cert_der=cert, time=time_value)


def load_edge_config(path: Path | str, *, device_id: str) -> EdgeConfig:
    """Read and parse an edge file without following symlinks."""

    target = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError as exc:
        raise EdgeConfigError(
            f"edge config not found: {target}", code="EDGE_CONFIG_MISSING"
        ) from exc
    except OSError as exc:
        raise EdgeConfigError("cannot open edge config", code="EDGE_CONFIG_INVALID") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EdgeConfigError("edge config must be a regular file")
        if info.st_uid != os.getuid():
            raise EdgeConfigError("edge config must be owned by the current user")
        if info.st_size > MAX_EDGE_FILE_BYTES:
            raise EdgeConfigError("edge file is larger than 1 MiB")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_EDGE_FILE_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(payload) > MAX_EDGE_FILE_BYTES:
        raise EdgeConfigError("edge file is larger than 1 MiB")
    return parse_edge_config(payload, device_id=device_id)


def choose_path(
    config: EdgeConfig,
    preferred: EdgePathType | str | None,
    *,
    mask_enabled: bool = False,
) -> EdgePath:
    """Return the path to use, following the documented failover order."""

    if preferred is not None and not isinstance(preferred, EdgePathType):
        try:
            preferred = EdgePathType(str(preferred).strip().lower())
        except ValueError as exc:
            raise EdgeConfigError(
                "unknown edge path", code="EDGE_PATH_UNAVAILABLE"
            ) from exc

    if preferred is not None:
        if preferred is EdgePathType.TEST:
            path = config.paths.get(EdgePathType.TEST)
            if path is None:
                raise EdgeConfigError(
                    "test edge path is not configured", code="EDGE_PATH_UNAVAILABLE"
                )
            return path
        path = config.paths.get(preferred)
        if path is None:
            raise EdgeConfigError(
                f"edge path {preferred.value} is not configured",
                code="EDGE_PATH_UNAVAILABLE",
            )
        return path

    for kind in (EdgePathType.MAIN, EdgePathType.RAND):
        path = config.paths.get(kind)
        if path is not None:
            return path
    if mask_enabled:
        path = config.paths.get(EdgePathType.MASK)
        if path is not None:
            return path
    raise EdgeConfigError("no usable edge path", code="EDGE_PATH_UNAVAILABLE")


__all__ = [
    "MAX_EDGE_FILE_BYTES",
    "EdgeConfig",
    "EdgeConfigError",
    "EdgePath",
    "EdgePathType",
    "choose_path",
    "decode_edge_container",
    "generate_path_host",
    "load_edge_config",
    "parse_edge_config",
]
