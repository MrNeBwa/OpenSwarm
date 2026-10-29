"""Cryptographic envelope used by the native Ninja VPN edge protocol.

Request::

    SHA256(rest) | padLen | pad | serverPubLen | serverPub | clientPubLen |
    clientPub | salt1[32] | salt2[32] | iv[12] | ctLen(u32) | ct | tail

Response::

    hmac[32] | padLen | pad | iv[12] | ctLen(u32) | ct

The ECDH secret is never logged nor included in exception messages.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import secrets
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAX_PLAINTEXT_BYTES = 4 * 1024 * 1024
MAX_CT_BYTES = MAX_PLAINTEXT_BYTES + 1024
_TAIL_MIN = 50
_TAIL_MAX = 250
_PAD_MIN = 50
_PAD_MAX = 250


class EdgeCryptoError(ValueError):
    """Envelope/signature failure (never contains key material)."""


@dataclass(frozen=True)
class EdgeKeys:
    private_key: ec.EllipticCurvePrivateKey
    public_der: bytes
    server_public: ec.EllipticCurvePublicKey

    def __repr__(self) -> str:  # avoid accidental key logging
        return "EdgeKeys(private_key=<redacted>, public_der=<redacted>)"

    def server_public_der(self) -> bytes:
        return self.server_public.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )


def generate_keys(server_public_der: bytes) -> EdgeKeys:
    """Create a fresh P-256 client key and bind it to the server key."""

    try:
        server_key = serialization.load_der_public_key(server_public_der)
    except (ValueError, TypeError) as exc:
        raise EdgeCryptoError("server public key is not valid DER") from exc
    if not isinstance(server_key, ec.EllipticCurvePublicKey) or not isinstance(
        server_key.curve, ec.SECP256R1
    ):
        raise EdgeCryptoError("server public key must be P-256")
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return EdgeKeys(
        private_key=private_key,
        public_der=public_der,
        server_public=server_key,
    )


def derive_key(shared: bytes, salt1: bytes, salt2: bytes) -> bytes:
    """HKDF-SHA256 with extract bound to ``salt1`` and ``info=salt2``."""

    if not isinstance(shared, (bytes, bytearray)) or not shared:
        raise EdgeCryptoError("empty ECDH secret")
    if len(salt1) != 32 or len(salt2) != 32:
        raise EdgeCryptoError("salts must be 32 bytes")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=bytes(salt1),
        info=bytes(salt2),
    ).derive(bytes(shared))


def _shared_secret(keys: EdgeKeys) -> bytes:
    try:
        return keys.private_key.exchange(ec.ECDH(), keys.server_public)
    except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise EdgeCryptoError("ECDH failed") from exc


def _encode_plaintext(
    name: str,
    meta: Mapping[str, Any],
    body: Mapping[str, Any],
) -> bytes:
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255:
        raise EdgeCryptoError("operation name must be 1..255 UTF-8 bytes")
    try:
        name_bytes = name.encode("utf-8")
        meta_bytes = json.dumps(dict(meta), separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        body_bytes = json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise EdgeCryptoError("envelope metadata is not JSON-serializable") from exc
    if len(meta_bytes) > 0xFFFF or len(body_bytes) > 0xFFFFFFFF:
        raise EdgeCryptoError("envelope payload is too large")
    return (
        bytes([len(name_bytes)])
        + name_bytes
        + struct.pack(">H", len(meta_bytes))
        + meta_bytes
        + struct.pack(">I", len(body_bytes))
        + body_bytes
    )


def build_request(
    name: str,
    meta: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    keys: EdgeKeys,
    salt1: bytes | None = None,
    salt2: bytes | None = None,
    iv: bytes | None = None,
    pad: bytes | None = None,
    tail: bytes | None = None,
) -> bytes:
    """Serialize and encrypt one edge request."""

    salt1 = secrets.token_bytes(32) if salt1 is None else bytes(salt1)
    salt2 = secrets.token_bytes(32) if salt2 is None else bytes(salt2)
    iv = secrets.token_bytes(12) if iv is None else bytes(iv)
    if len(salt1) != 32 or len(salt2) != 32:
        raise EdgeCryptoError("salts must be 32 bytes")
    if len(iv) != 12:
        raise EdgeCryptoError("IV must be 12 bytes")
    if pad is None:
        pad = secrets.token_bytes(secrets.randbelow(_PAD_MAX - _PAD_MIN + 1) + _PAD_MIN)
    if tail is None:
        tail = secrets.token_bytes(secrets.randbelow(_TAIL_MAX - _TAIL_MIN + 1) + _TAIL_MIN)
    if not _PAD_MIN <= len(pad) <= _PAD_MAX:
        raise EdgeCryptoError("padding must be 50..250 bytes")
    if not _TAIL_MIN <= len(tail) <= _TAIL_MAX:
        raise EdgeCryptoError("tail must be 50..250 bytes")

    plaintext = _encode_plaintext(name, meta, body)
    compressed = gzip.compress(plaintext, mtime=0)
    key = derive_key(_shared_secret(keys), salt1, salt2)
    ciphertext = AESGCM(key).encrypt(iv, compressed, None)
    if len(ciphertext) > MAX_CT_BYTES:
        raise EdgeCryptoError("ciphertext is too large")
    if len(keys.public_der) > 255 or len(keys.server_public_der()) > 255:
        raise EdgeCryptoError("public key is too long")

    rest = (
        bytes([len(pad)])
        + pad
        + bytes([len(keys.server_public_der())])
        + keys.server_public_der()
        + bytes([len(keys.public_der)])
        + keys.public_der
        + salt1
        + salt2
        + iv
        + struct.pack(">I", len(ciphertext))
        + ciphertext
        + tail
    )
    return hashlib.sha256(rest).digest() + rest


# ``EdgeKeys`` carries the server key as an object; keep the DER encoding
# reachable without storing another public copy.
def _decompress_limited(data: bytes, limit: int) -> bytes:
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        output = decompressor.decompress(data, limit + 1)
    except zlib.error as exc:
        raise EdgeCryptoError("response is not valid gzip") from exc
    if len(output) > limit:
        raise EdgeCryptoError("response exceeds the plaintext limit")
    if decompressor.unconsumed_tail:
        raise EdgeCryptoError("response exceeds the plaintext limit")
    if not decompressor.eof:
        raise EdgeCryptoError("truncated gzip payload")
    return output


def parse_response(
    payload: bytes,
    *,
    key: bytes,
    server_public_der: bytes,
    max_plaintext: int = MAX_PLAINTEXT_BYTES,
) -> bytes:
    """Verify HMAC, decrypt, verify ECDSA and gunzip a server response."""

    if not isinstance(payload, (bytes, bytearray)):
        raise EdgeCryptoError("response must be bytes")
    data = bytes(payload)
    if len(data) < 32 + 1 + 12 + 4 + 16:
        raise EdgeCryptoError("response is truncated")
    if len(key) != 32:
        raise EdgeCryptoError("AES key must be 32 bytes")
    if max_plaintext <= 0:
        raise EdgeCryptoError("plaintext limit must be positive")

    received_hmac = data[:32]
    rest = data[32:]
    expected_hmac = hmac.new(key, rest, hashlib.sha256).digest()
    if not hmac.compare_digest(received_hmac, expected_hmac):
        raise EdgeCryptoError("response HMAC mismatch")

    offset = 0
    pad_len = rest[offset]
    offset += 1
    if offset + pad_len > len(rest):
        raise EdgeCryptoError("response padding is truncated")
    offset += pad_len
    if offset + 12 + 4 > len(rest):
        raise EdgeCryptoError("response header is truncated")
    iv = rest[offset : offset + 12]
    offset += 12
    (ct_len,) = struct.unpack(">I", rest[offset : offset + 4])
    offset += 4
    # The Android client stops reading after the declared ciphertext and
    # ignores the rest of the payload.  The current server signs a random tail
    # after it, so requiring an exact end rejects otherwise valid responses.
    if ct_len > MAX_CT_BYTES or offset + ct_len > len(rest):
        raise EdgeCryptoError("response ciphertext length is invalid")
    ciphertext = rest[offset : offset + ct_len]
    try:
        decrypted = AESGCM(key).decrypt(iv, ciphertext, None)
    except InvalidTag as exc:
        raise EdgeCryptoError("response authentication failed") from exc

    if not decrypted:
        raise EdgeCryptoError("response signature is missing")
    sig_len = decrypted[0]
    if sig_len == 0 or 1 + sig_len > len(decrypted):
        raise EdgeCryptoError("response signature is truncated")
    signature = decrypted[1 : 1 + sig_len]
    signed = decrypted[1 + sig_len :]

    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        server_key = load_der_public_key(server_public_der)
    except (ValueError, TypeError) as exc:
        raise EdgeCryptoError("server public key is invalid") from exc
    if not isinstance(server_key, ec.EllipticCurvePublicKey):
        raise EdgeCryptoError("server public key is not an EC key")
    try:
        server_key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise EdgeCryptoError("response signature mismatch") from exc

    return _decompress_limited(signed, max_plaintext)


def sign_payload(
    payload: bytes,
    *,
    private_key: ec.EllipticCurvePrivateKey,
) -> bytes:
    """Create ``sigLen | sig | payload`` (test/server helper)."""

    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    if len(signature) > 255:
        raise EdgeCryptoError("signature is too long")
    return bytes([len(signature)]) + signature + payload


def build_response(
    plaintext: bytes,
    *,
    key: bytes,
    signing_key: ec.EllipticCurvePrivateKey,
    iv: bytes | None = None,
    pad: bytes | None = None,
) -> bytes:
    """Build a signed/encrypted response (used by tests and fake servers)."""

    iv = secrets.token_bytes(12) if iv is None else bytes(iv)
    if len(iv) != 12:
        raise EdgeCryptoError("IV must be 12 bytes")
    pad = secrets.token_bytes(32) if pad is None else bytes(pad)
    if len(pad) > 255:
        raise EdgeCryptoError("padding is too long")
    signed = sign_payload(plaintext, private_key=signing_key)
    ciphertext = AESGCM(key).encrypt(iv, signed, None)
    rest = bytes([len(pad)]) + pad + iv + struct.pack(">I", len(ciphertext)) + ciphertext
    return hmac.new(key, rest, hashlib.sha256).digest() + rest


# Avoid a lint complaint about the imported helper in the public surface.
_ = secrets

__all__ = [
    "MAX_PLAINTEXT_BYTES",
    "EdgeCryptoError",
    "EdgeKeys",
    "build_request",
    "build_response",
    "derive_key",
    "generate_keys",
    "parse_response",
    "sign_payload",
]
