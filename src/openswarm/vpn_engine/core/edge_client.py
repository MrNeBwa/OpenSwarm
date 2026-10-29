"""Pinned HTTPS client for the native Ninja VPN edge protocol."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import hashlib
import io
import ipaddress
import json
import locale as locale_module
import os
import secrets
import socket
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .edge_config import (
    EdgeConfig,
    EdgeConfigError,
    EdgePath,
    EdgePathType,
    choose_path,
)
from .edge_crypto import (
    EdgeCryptoError,
    build_request,
    derive_key,
    generate_keys,
    parse_response,
)
from .identity import DeviceIdentity

DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REDIRECT_STATUS = 399
_RETRY_ATTEMPTS = 3


class EdgeError(RuntimeError):
    code = "EDGE_CONFIG_INVALID"

    def __init__(self, message: str, *, code: str | None = None):
        if code is not None:
            self.code = code
        super().__init__(message)


class EdgeConfigMissing(EdgeError):
    code = "EDGE_CONFIG_MISSING"


class EdgePathUnavailable(EdgeError):
    code = "EDGE_PATH_UNAVAILABLE"


class EdgePinMismatch(EdgeError):
    code = "EDGE_TLS_PIN_MISMATCH"


class EdgeInstallRejected(EdgeError):
    code = "EDGE_INSTALL_REJECTED"


class EdgeHTTPError(EdgeError):
    code = "EDGE_HTTP_ERROR"

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = int(status)


@dataclass(frozen=True)
class EdgeHTTPResponse:
    status: int
    body: bytes
    peer_pins: tuple[str, ...] = ()


class EdgeTransport(Protocol):
    def post(
        self,
        *,
        host: str,
        addr: str,
        body: bytes,
        pins: frozenset[bytes],
        trust_chain: bool,
        timeout: float,
        max_bytes: int,
    ) -> EdgeHTTPResponse: ...


def _ip_literal(value: object) -> bool:
    text = str(value or "").strip().strip("[]")
    if "/" in text:
        text = text.split("/", 1)[0]
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _b64_pins(pins: Sequence[bytes] | Sequence[str] | None) -> set[bytes]:
    result: set[bytes] = set()
    for item in pins or ():
        if isinstance(item, str):
            try:
                result.add(base64.b64decode(item, validate=True))
            except Exception:  # malformed peer pin is simply not a match
                continue
        elif isinstance(item, (bytes, bytearray)):
            result.add(bytes(item))
    return result


def _spki_sha256(cert_der: bytes) -> bytes:
    cert = x509.load_der_x509_certificate(cert_der)
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(spki).digest()


def _peer_cert_chain(sock: ssl.SSLSocket) -> list[bytes]:
    getter = getattr(sock, "get_unverified_chain", None)
    if callable(getter):
        try:
            chain = getter()
            if chain:
                return [cert.public_bytes(serialization.Encoding.DER) for cert in chain]
        except Exception:
            pass
    getter = getattr(sock, "get_verified_chain", None)
    if callable(getter):
        try:
            chain = getter()
            if chain:
                return [cert.public_bytes(serialization.Encoding.DER) for cert in chain]
        except Exception:
            pass
    leaf = sock.getpeercert(binary_form=True)
    return [leaf] if leaf else []


def _check_certificate_validity(cert_der: bytes) -> None:
    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except ValueError as exc:
        raise EdgePinMismatch("edge certificate cannot be parsed") from exc
    now = time.time()
    not_before = cert.not_valid_before_utc.timestamp()
    not_after = cert.not_valid_after_utc.timestamp()
    if now < not_before or now > not_after:
        raise EdgePinMismatch("edge certificate is expired or not yet valid")


def _build_http_request(host: str, body: bytes) -> bytes:
    return (
        "POST / HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Content-Type: application/octet-stream\r\n"
        "User-Agent: okhttp/5.4.0\r\n"
        "Accept-Encoding: gzip\r\n"
        "gzip: 2\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Accept: */*\r\n"
        "\r\n"
    ).encode("ascii") + bytes(body)


def _gunzip_limited(payload: bytes, limit: int) -> bytes:
    output = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            while True:
                chunk = stream.read(min(65536, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise EdgeHTTPError("edge response is too large")
    except (OSError, EOFError) as exc:
        raise EdgeHTTPError("invalid gzip response") from exc
    return bytes(output)


def _decode_response_body(
    headers: Mapping[str, str], body: bytes, *, max_bytes: int
) -> bytes:
    data = bytes(body)
    if "gzip" in headers.get("content-encoding", "").lower():
        data = _gunzip_limited(data, max_bytes)
    if len(data) > max_bytes:
        raise EdgeHTTPError("edge response is too large")
    return data


class HttpsTransport:
    """Minimal HTTPS POST pinned to an SPKI set, connecting to a fixed IP."""

    def __init__(self, *, ca_file: Path | None = None) -> None:
        self.ca_file = Path(ca_file).expanduser() if ca_file is not None else None

    def _context(self, *, trust_chain: bool) -> ssl.SSLContext:
        if trust_chain:
            context = ssl.create_default_context(
                cafile=str(self.ca_file) if self.ca_file is not None else None
            )
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            return context
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def post(
        self,
        *,
        host: str,
        addr: str,
        body: bytes,
        pins: frozenset[bytes],
        trust_chain: bool,
        timeout: float,
        max_bytes: int,
    ) -> EdgeHTTPResponse:
        if not pins:
            raise EdgePinMismatch("edge path has no TLS pins")
        if not isinstance(body, (bytes, bytearray)):
            raise EdgeError("edge request body must be bytes")
        target = addr.strip("[]")
        context = self._context(trust_chain=trust_chain)
        try:
            raw = socket.create_connection((target, 443), timeout=timeout)
        except OSError as exc:
            raise EdgePathUnavailable("cannot connect to edge address") from exc
        sock: ssl.SSLSocket | None = None
        try:
            try:
                sock = context.wrap_socket(raw, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                raise EdgePinMismatch("edge TLS chain or hostname verification failed") from exc
            except (ssl.SSLError, OSError) as exc:
                raise EdgePathUnavailable("edge TLS handshake failed") from exc
            chain = _peer_cert_chain(sock)
            if not chain:
                raise EdgePinMismatch("edge server did not present a certificate")
            if not trust_chain:
                _check_certificate_validity(chain[0])
            peer_pins: list[str] = []
            matched = False
            for cert_der in chain:
                try:
                    digest = _spki_sha256(cert_der)
                except Exception as exc:
                    raise EdgePinMismatch("edge certificate is malformed") from exc
                peer_pins.append(base64.b64encode(digest).decode("ascii"))
                if digest in pins:
                    matched = True
            if not matched:
                raise EdgePinMismatch("edge certificate SPKI does not match any pin")

            sock.sendall(_build_http_request(host, body))
            return _read_http_response(sock, max_bytes=max_bytes, peer_pins=peer_pins)
        finally:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
            else:
                with contextlib.suppress(OSError):
                    raw.close()


# Backwards-friendly alias for callers that named the transport after urllib.
UrllibTransport = HttpsTransport


def _read_http_response(
    sock: ssl.SSLSocket,
    *,
    max_bytes: int,
    peer_pins: Sequence[str],
) -> EdgeHTTPResponse:
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > 64 * 1024:
            raise EdgeHTTPError("edge response headers are too large")
        chunk = sock.recv(8192)
        if not chunk:
            raise EdgeHTTPError("edge server closed before sending headers")
        buffer.extend(chunk)
    header, _, rest = bytes(buffer).partition(b"\r\n\r\n")
    lines = header.split(b"\r\n")
    try:
        status_parts = lines[0].split()
        status = int(status_parts[1])
    except (IndexError, ValueError) as exc:
        raise EdgeHTTPError("invalid HTTP status line") from exc

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(b":")
        if not sep:
            continue
        headers[key.decode("ascii", "ignore").strip().lower()] = value.decode(
            "latin-1"
        ).strip()
    if "transfer-encoding" in headers and "chunked" in headers["transfer-encoding"].lower():
        body = _read_chunked(sock, rest, max_bytes=max_bytes)
    else:
        length_text = headers.get("content-length")
        if length_text is not None:
            try:
                length = int(length_text)
            except ValueError as exc:
                raise EdgeHTTPError("invalid Content-Length") from exc
            if length < 0 or length > max_bytes:
                raise EdgeHTTPError("edge response is too large")
            body = bytearray(rest)
            while len(body) < length:
                chunk = sock.recv(min(65536, length - len(body)))
                if not chunk:
                    raise EdgeHTTPError("edge response was truncated")
                body.extend(chunk)
            del body[length:]
        else:
            body = bytearray(rest)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise EdgeHTTPError("edge response is too large")
    body = _decode_response_body(headers, bytes(body), max_bytes=max_bytes)
    return EdgeHTTPResponse(
        status=status,
        body=body,
        peer_pins=tuple(peer_pins),
    )


def _read_chunked(sock: ssl.SSLSocket, initial: bytes, *, max_bytes: int) -> bytes:
    buffer = bytearray(initial)
    body = bytearray()

    def need(marker: bytes) -> tuple[bytes, bytes]:
        while marker not in buffer:
            if len(buffer) > max_bytes + 64 * 1024:
                raise EdgeHTTPError("edge response is too large")
            chunk = sock.recv(65536)
            if not chunk:
                raise EdgeHTTPError("chunked response was truncated")
            buffer.extend(chunk)
        head, _, tail = bytes(buffer).partition(marker)
        return head, tail

    while True:
        line, rest = need(b"\r\n")
        del buffer[:]
        buffer.extend(rest)
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise EdgeHTTPError("invalid chunk size") from exc
        if size == 0:
            break
        while len(buffer) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                raise EdgeHTTPError("chunked response was truncated")
            buffer.extend(chunk)
        body.extend(buffer[:size])
        if len(body) > max_bytes:
            raise EdgeHTTPError("edge response is too large")
        del buffer[: size + 2]
    return bytes(body)


def _two_letter_country() -> str:
    try:
        language, _encoding = locale_module.getlocale()
    except (ValueError, TypeError):
        language = None
    country = ""
    if language and "_" in language:
        country = language.split("_", 1)[1]
    return (country or "RU")[:3].upper()


class EdgeClient:
    """One long-lived P-256 key, reused for every edge operation."""

    def __init__(
        self,
        config: EdgeConfig,
        identity: DeviceIdentity,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_response: int = MAX_RESPONSE_BYTES,
        ca_file: Path | None = None,
        transport: EdgeTransport | None = None,
        mask_enabled: bool = False,
        retry_attempts: int = _RETRY_ATTEMPTS,
        backoff_base: float = 0.5,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response <= 0:
            raise ValueError("max_response must be positive")
        self.config = config
        self.identity = identity
        self.timeout = float(timeout)
        self.max_response = int(max_response)
        self.mask_enabled = bool(mask_enabled)
        self.retry_attempts = max(1, int(retry_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.transport: EdgeTransport = transport or HttpsTransport(ca_file=ca_file)
        self._keys = generate_keys(config.cert_der)
        self._closed = False
        self.last_path: EdgePath | None = None

    async def close(self) -> None:
        self._closed = True

    # -- path selection -----------------------------------------------------
    def candidate_paths(self, preferred: EdgePathType | str | None) -> list[EdgePath]:
        if preferred is not None:
            try:
                return [choose_path(self.config, preferred, mask_enabled=self.mask_enabled)]
            except EdgeConfigError as exc:
                raise EdgePathUnavailable(str(exc)) from exc
        order: list[EdgePathType] = [EdgePathType.MAIN, EdgePathType.RAND]
        if self.mask_enabled:
            order.append(EdgePathType.MASK)
        paths = [self.config.paths[kind] for kind in order if kind in self.config.paths]
        if not paths:
            raise EdgePathUnavailable("no usable edge path is configured")
        return paths

    def _candidate_addresses(self, path: EdgePath) -> tuple[str, ...]:
        """Baked-in addresses plus DNS fallback for real MAIN/TEST domains.

        RAND/MASK hosts are synthetic names derived from the device id; they
        must never be sent to a resolver.
        """

        addresses = list(path.addrs)
        if path.type not in (EdgePathType.MAIN, EdgePathType.TEST):
            return tuple(addresses)
        if _ip_literal(path.host):
            return tuple(addresses)
        try:
            infos = socket.getaddrinfo(path.host, 443, type=socket.SOCK_STREAM)
        except OSError:
            infos = []
        for info in infos:
            try:
                addr = str(info[4][0]).strip()
            except (IndexError, TypeError):
                continue
            if _ip_literal(addr) and addr not in addresses:
                addresses.append(addr)
        return tuple(addresses)

    # -- metadata -----------------------------------------------------------
    def _meta(self, origin: str) -> dict[str, Any]:
        country = _two_letter_country()
        return {
            "unix": int(time.time() * 1000),
            "from": origin,
            "hard": self.identity.hard,
            "soft": self.identity.soft,
            "premium": self.identity.premium,
            "protect": self.identity.protect,
            "apksign": self.identity.apksign,
            "version": {"sys": 0, "app": 44},
            "manufacturer": {"brand": "Linux", "model": "desktop"},
            "locale": {"country": country, "language": (country[:2] or "ru").lower()},
        }

    # -- low-level request --------------------------------------------------
    async def _request_once(
        self,
        *,
        path: EdgePath,
        addr: str,
        name: str,
        body: Mapping[str, Any],
        origin: str,
    ) -> tuple[int, bytes, EdgeHTTPResponse]:
        salt1 = secrets.token_bytes(32)
        salt2 = secrets.token_bytes(32)
        iv = secrets.token_bytes(12)
        request = build_request(
            name,
            self._meta(origin),
            body,
            keys=self._keys,
            salt1=salt1,
            salt2=salt2,
            iv=iv,
        )
        key = derive_key(
            self._keys.private_key.exchange(ec.ECDH(), self._keys.server_public),
            salt1,
            salt2,
        )
        try:
            response = await asyncio.to_thread(
                self.transport.post,
                host=path.tls_host,
                addr=addr,
                body=request,
                pins=path.pins,
                trust_chain=path.type in (EdgePathType.MAIN, EdgePathType.TEST),
                timeout=self.timeout,
                max_bytes=self.max_response,
            )
        except EdgePinMismatch:
            raise
        except EdgeError:
            raise
        except (OSError, TimeoutError) as exc:
            raise EdgePathUnavailable("edge request failed") from exc

        peer_pins = _b64_pins(response.peer_pins)
        if peer_pins and not (peer_pins & set(path.pins)):
            raise EdgePinMismatch("edge transport reported an unexpected SPKI pin")
        if response.status == 200:
            if len(response.body) > self.max_response:
                raise EdgeHTTPError("edge response is too large")
            try:
                plaintext = parse_response(
                    response.body,
                    key=key,
                    server_public_der=self.config.cert_der,
                    max_plaintext=self.max_response,
                )
            except EdgeCryptoError as exc:
                raise EdgeError(str(exc), code="EDGE_CRYPTO_ERROR") from exc
            return response.status, plaintext, response
        return response.status, b"", response

    async def _request(
        self,
        *,
        name: str,
        body: Mapping[str, Any],
        origin: str,
        path: EdgePathType | str | None,
        install: bool = False,
        validate: Callable[[bytes], Any] | None = None,
    ) -> Any:
        if self._closed:
            raise EdgeError("edge client is closed")
        candidates = self.candidate_paths(path)
        last_error: EdgeError | None = None
        for candidate in candidates:
            skip_path = False
            for addr in self._candidate_addresses(candidate):
                attempt = 0
                while attempt < self.retry_attempts:
                    try:
                        status, plaintext, _response = await self._request_once(
                            path=candidate,
                            addr=addr,
                            name=name,
                            body=body,
                            origin=origin,
                        )
                    except EdgePinMismatch as exc:
                        last_error = exc
                        attempt = self.retry_attempts
                        skip_path = True
                        break  # never retry a bad pin on this path
                    except EdgePathUnavailable as exc:
                        last_error = exc
                        attempt += 1
                        if attempt >= self.retry_attempts:
                            break
                        await self._backoff(attempt)
                        continue
                    except EdgeHTTPError as exc:
                        last_error = exc
                        attempt += 1
                        if attempt >= self.retry_attempts:
                            break
                        await self._backoff(attempt)
                        continue
                    except EdgeError as exc:
                        last_error = exc
                        attempt = self.retry_attempts
                        break

                    if 300 <= status <= MAX_REDIRECT_STATUS:
                        last_error = EdgeHTTPError(
                            "edge server returned a redirect; refusing to follow",
                        )
                        attempt = self.retry_attempts
                        skip_path = True
                        break
                    if status == 200:
                        if validate is not None:
                            try:
                                result = validate(plaintext)
                            except EdgePathUnavailable as exc:
                                # A path that fails its own liveness check (for
                                # example a mismatched ping) is skipped without
                                # retrying the same address.
                                last_error = exc
                                attempt = self.retry_attempts
                                break
                            except EdgeHTTPError as exc:
                                # A syntactically valid 200 with a broken body
                                # is retried, then the next path is tried.
                                last_error = exc
                                attempt += 1
                                if attempt >= self.retry_attempts:
                                    break
                                await self._backoff(attempt)
                                continue
                            self.last_path = candidate
                            return result
                        self.last_path = candidate
                        return plaintext
                    if status in (404, 405):
                        # A missing route is local to this path; another path
                        # may still serve the same request.
                        last_error = EdgeHTTPError(
                            f"edge server returned HTTP {status}", status=status
                        )
                        skip_path = True
                        break
                    if status >= 500:
                        last_error = EdgeHTTPError(
                            f"edge server returned HTTP {status}", status=status
                        )
                        attempt += 1
                        if attempt >= self.retry_attempts:
                            break
                        await self._backoff(attempt)
                        continue
                    if status == 409 and install:
                        last_error = EdgeInstallRejected(
                            "edge install rejected the request (HTTP 409)"
                        )
                        attempt += 1
                        if attempt >= self.retry_attempts:
                            break
                        await self._backoff(attempt)
                        continue
                    if status in (401, 403):
                        raise EdgeInstallRejected(
                            f"edge server denied the request (HTTP {status})"
                        )
                    raise EdgeHTTPError(
                        f"edge server returned HTTP {status}", status=status
                    )
                if skip_path:
                    break
                # Next address on the same path.
            # Next path.
        if last_error is not None:
            if isinstance(last_error, EdgeHTTPError) and last_error.status in (404, 405):
                raise EdgePathUnavailable(
                    f"edge request is not available (HTTP {last_error.status})"
                ) from last_error
            raise last_error
        raise EdgePathUnavailable("no edge path is available")

    async def _backoff(self, attempt: int) -> None:
        delay = min(2**attempt, 10)
        jitter = 0.1 * delay * (hashlib.sha256(os.urandom(8)).digest()[0] / 255.0)
        await asyncio.sleep(min(self.backoff_base * delay + jitter, 10.0))

    # -- operations ---------------------------------------------------------
    async def ping(self, path: EdgePathType | str | None = None) -> bool:
        value = str(secrets.randbelow(900_000_000) + 100_000_000)

        def _check(payload: bytes) -> bool:
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EdgePathUnavailable(
                    "edge ping response is not valid UTF-8"
                ) from exc
            if text != value:
                raise EdgePathUnavailable("edge ping response did not match")
            return True

        try:
            return bool(
                await self._request(
                    name="ping",
                    body={"rand": value},
                    origin="vpn",
                    path=path,
                    validate=_check,
                )
            )
        except EdgePathUnavailable:
            return False
        except EdgeError:
            raise

    async def manifest(self, *, path: EdgePathType | str | None = None) -> dict[str, Any]:
        return await self._request(
            name="manifest",
            body={},
            origin="app",
            path=path,
            validate=lambda payload: self._json_object(payload, "manifest"),
        )

    async def install(
        self,
        country: str,
        wg_public_key: str,
        *,
        path: EdgePathType | str | None = None,
    ) -> dict[str, Any]:
        country = country.strip().upper() if isinstance(country, str) else ""
        if len(country) != 2 or not country.isalpha():
            raise EdgeError("country must be a two-letter code", code="INVALID_PARAMS")
        if not isinstance(wg_public_key, str) or not wg_public_key:
            raise EdgeError("WireGuard public key is required", code="INVALID_PARAMS")
        return await self._request(
            name="install",
            body={
                "protocol": "WIREGUARD",
                "country": country,
                "auth": wg_public_key,
            },
            origin="vpn",
            path=path,
            install=True,
            validate=self._validate_install,
        )

    @classmethod
    def _validate_install(cls, payload: bytes) -> dict[str, Any]:
        data = cls._json_object(payload, "install")
        required = ("country", "edges", "addresses", "config")
        if any(key not in data for key in required):
            raise EdgeHTTPError("edge install response is incomplete")
        country = data.get("country")
        if not isinstance(country, str) or len(country.strip()) != 2:
            raise EdgeHTTPError("edge install response has an invalid country")
        edges = data.get("edges")
        if not isinstance(edges, list) or not edges:
            raise EdgeHTTPError("edge install response has no edges")
        if not any(_ip_literal(edge) for edge in edges):
            raise EdgeHTTPError("edge install response has no IP edge")
        if not isinstance(data.get("addresses"), Mapping):
            raise EdgeHTTPError("edge install response has no addresses")
        config = data.get("config")
        if not isinstance(config, Mapping):
            raise EdgeHTTPError("edge install response has no config")
        peer = config.get("peer")
        if not isinstance(peer, str) or not peer.strip():
            raise EdgeHTTPError("edge install response has no peer")
        try:
            port = int(config.get("port") or 0)
        except (TypeError, ValueError) as exc:
            raise EdgeHTTPError("edge install response has an invalid port") from exc
        if not 1 <= port <= 65535:
            raise EdgeError(
                "edge install response has an invalid WireGuard port",
                code="EDGE_CONFIG_INVALID",
            )
        if not isinstance(config.get("addr"), Mapping):
            raise EdgeHTTPError("edge install response has no tunnel address")
        return data

    @staticmethod
    def _json_object(payload: bytes, name: str) -> dict[str, Any]:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EdgeHTTPError(f"edge {name} response is not valid JSON") from exc
        if not isinstance(data, Mapping):
            raise EdgeHTTPError(f"edge {name} response must be a JSON object")
        return dict(data)


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_RESPONSE_BYTES",
    "EdgeClient",
    "EdgeConfigMissing",
    "EdgeError",
    "EdgeHTTPError",
    "EdgeHTTPResponse",
    "EdgeInstallRejected",
    "EdgePathUnavailable",
    "EdgePinMismatch",
    "EdgeTransport",
    "HttpsTransport",
    "UrllibTransport",
]
