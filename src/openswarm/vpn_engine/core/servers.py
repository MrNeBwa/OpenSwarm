"""Server import, subscription parsing and the public server cache."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import socket
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schema import (
    Protocol,
    Security,
    Server,
    Settings,
    Transport,
    default_edge_bin_path,
    default_state_dir,
)

MAX_SUBSCRIPTION_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "ninjavpn-client/0.1 (+https://ninjavpn.app/docs/)"


class SubscriptionError(Exception):
    """A subscription/import could not be fetched or parsed."""


@dataclass
class ImportResult:
    servers: list[Server]
    source: str = "subscription"
    fetched_at: float = 0.0
    updated: bool = True

    @property
    def count(self) -> int:
        return len(self.servers)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stable_id(kind: str, host: str, port: int, name: str = "") -> str:
    digest = hashlib.sha256(f"{kind}|{host}|{port}|{name}".encode()).hexdigest()[:24]
    return f"s-{digest}"


def _decode_base64(text: str) -> str | None:
    candidate = "".join(text.split())
    if not candidate:
        return None
    padding = "=" * (-len(candidate) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            if decoder is base64.b64decode:
                raw = decoder(candidate + padding, validate=False)
            else:
                raw = decoder(candidate + padding)
        except (binascii.Error, TypeError, ValueError):
            continue
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded.strip():
            return decoded
    return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _transport(value: Any) -> Transport:
    text = str(value or "tcp").strip().lower()
    if text in {"", "raw", "tcp"}:
        return Transport.TCP if text != "raw" else Transport.RAW
    try:
        return Transport(text)
    except ValueError:
        return Transport.TCP


def _security(value: Any) -> Security:
    try:
        return Security(str(value or "none").strip().lower())
    except ValueError:
        return Security.NONE


def _protocol(value: Any) -> Protocol | None:
    text = str(value or "").strip().lower()
    if text == "ss":
        text = "shadowsocks"
    try:
        return Protocol(text)
    except ValueError:
        return None


def _name_from_fragment(fragment: str, fallback: str) -> str:
    if not fragment:
        return fallback
    return urllib.parse.unquote(fragment).strip() or fallback


# ---------------------------------------------------------------------------
# share links
# ---------------------------------------------------------------------------


def parse_share_link(link: str) -> Server | None:
    """Parse one ``vless://``/``vmess://``/``trojan://``/``ss://``/``wireguard://`` URI."""

    text = str(link or "").strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("vmess://"):
        return _parse_vmess(text)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme == "vless":
        return _parse_vless(parts)
    if scheme == "trojan":
        return _parse_trojan(parts)
    if scheme in {"ss", "shadowsocks"}:
        return _parse_ss(parts)
    if scheme in {"wireguard", "wg"}:
        return _parse_wireguard(parts)
    if scheme.endswith("-uri"):  # ignore plugins/unknown schemes
        return None
    return None


def _query(parts: urllib.parse.SplitResult) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _parse_vless(parts: urllib.parse.SplitResult) -> Server | None:
    host = parts.hostname
    port = parts.port
    uuid = urllib.parse.unquote(parts.username or "")
    if not host or not port or not uuid:
        return None
    query = _query(parts)
    security = _security(query.get("security"))
    transport = _transport(query.get("type"))
    name = _name_from_fragment(parts.fragment, f"{host}:{port}")
    params: dict[str, Any] = {}
    for key, source in (
        ("sni", "sni"),
        ("host", "host"),
        ("path", "path"),
        ("service_name", "serviceName"),
        ("mode", "mode"),
        ("header_type", "headerType"),
        ("fingerprint", "fp"),
        ("flow", "flow"),
        ("alpn", "alpn"),
        ("spider_x", "spx"),
        ("seed", "seed"),
    ):
        if query.get(source):
            params[key] = query[source]
    if _bool(query.get("allowInsecure")):
        params["allow_insecure"] = True
    credentials = {"uuid": uuid}
    if query.get("pbk"):
        credentials["public_key"] = query["pbk"]
    if query.get("sid"):
        credentials["short_id"] = query["sid"]
    return Server(
        id=_stable_id("vless", host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=Protocol.VLESS,
        transport=transport,
        security=security,
        params=params,
        credentials=credentials,
    )


def _parse_vmess(link: str) -> Server | None:
    encoded = link[len("vmess://") :].strip()
    decoded = _decode_base64(encoded)
    if decoded is None:
        return None
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, Mapping):
        return None
    host = str(data.get("add", "")).strip()
    port = _int(data.get("port"))
    uuid = str(data.get("id", "")).strip()
    if not host or not port or not uuid:
        return None
    name = str(data.get("ps") or f"{host}:{port}")
    transport = _transport(data.get("net"))
    security = _security("tls" if _bool(data.get("tls")) else "none")
    params: dict[str, Any] = {
        "alter_id": _int(data.get("aid")),
        "encryption": str(data.get("scy", "auto")),
    }
    for key in ("sni", "host", "path", "alpn", "fp"):
        if data.get(key):
            params[key if key != "fp" else "fingerprint"] = data[key]
    return Server(
        id=_stable_id("vmess", host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=Protocol.VMESS,
        transport=transport,
        security=security,
        params=params,
        credentials={"uuid": uuid},
    )


def _parse_trojan(parts: urllib.parse.SplitResult) -> Server | None:
    host = parts.hostname
    port = parts.port
    password = urllib.parse.unquote(parts.username or "")
    if not host or not port or not password:
        return None
    query = _query(parts)
    name = _name_from_fragment(parts.fragment, f"{host}:{port}")
    params: dict[str, Any] = {}
    for key, source in (
        ("sni", "sni"),
        ("host", "host"),
        ("path", "path"),
        ("service_name", "serviceName"),
        ("alpn", "alpn"),
        ("fingerprint", "fp"),
    ):
        if query.get(source):
            params[key] = query[source]
    if _bool(query.get("allowInsecure")):
        params["allow_insecure"] = True
    return Server(
        id=_stable_id("trojan", host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=Protocol.TROJAN,
        transport=_transport(query.get("type")),
        security=_security(query.get("security", "tls")),
        params=params,
        credentials={"password": password},
    )


def _parse_ss(parts: urllib.parse.SplitResult) -> Server | None:
    host = parts.hostname
    port = parts.port
    userinfo = parts.username or ""
    method = ""
    password = ""
    if host and port and userinfo:
        decoded = _decode_base64(urllib.parse.unquote(userinfo))
        if decoded and ":" in decoded:
            method, password = decoded.split(":", 1)
        elif ":" in userinfo:
            method, password = urllib.parse.unquote(userinfo).split(":", 1)
    else:
        # Legacy form: ss://base64(method:password@host:port)
        payload = urllib.parse.unquote(parts.netloc)
        decoded = _decode_base64(payload)
        if not decoded or "@" not in decoded:
            return None
        credentials_part, endpoint = decoded.rsplit("@", 1)
        if ":" not in credentials_part or ":" not in endpoint:
            return None
        method, password = credentials_part.split(":", 1)
        host, port_text = endpoint.rsplit(":", 1)
        port = _int(port_text)
    if not host or not port or not method:
        return None
    name = _name_from_fragment(parts.fragment, f"{host}:{port}")
    return Server(
        id=_stable_id("ss", host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=Protocol.SHADOWSOCKS,
        transport=Transport.TCP,
        security=Security.NONE,
        params={"method": method},
        credentials={"password": password},
    )


def _parse_wireguard(parts: urllib.parse.SplitResult) -> Server | None:
    host = parts.hostname
    port = parts.port
    private_key = urllib.parse.unquote(parts.username or "")
    if not host or not port or not private_key:
        return None
    query = _query(parts)
    name = _name_from_fragment(parts.fragment, f"{host}:{port}")
    params: dict[str, Any] = {}
    for key in ("public_key", "publickey"):
        if query.get(key):
            params["public_key"] = query[key]
            break
    for key in ("address", "allowed_ips", "allowedips", "endpoint", "keepalive", "mtu"):
        if query.get(key):
            params[key] = query[key]
    return Server(
        id=_stable_id("wg", host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=Protocol.WIREGUARD,
        transport=Transport.TCP,
        security=Security.NONE,
        params=params,
        credentials={"private_key": private_key},
    )


# ---------------------------------------------------------------------------
# Xray JSON / API responses
# ---------------------------------------------------------------------------


def _first(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def _server_from_outbound(outbound: Mapping[str, Any], *, fallback_name: str = "") -> Server | None:
    protocol = _protocol(outbound.get("protocol"))
    if protocol is None or outbound.get("protocol") in {"freedom", "blackhole", "dns"}:
        return None
    settings = outbound.get("settings") or {}
    stream = outbound.get("streamSettings") or {}
    host = ""
    port = 0
    params: dict[str, Any] = {}
    credentials: dict[str, str] = {}

    if protocol in (Protocol.VLESS, Protocol.VMESS):
        vnext = _first(settings.get("vnext")) if isinstance(settings, Mapping) else None
        if not isinstance(vnext, Mapping):
            return None
        host = str(vnext.get("address", ""))
        port = _int(vnext.get("port"))
        user = _first(vnext.get("users")) or {}
        if not isinstance(user, Mapping):
            user = {}
        if user.get("id"):
            credentials["uuid"] = str(user["id"])
        if user.get("flow"):
            params["flow"] = str(user["flow"])
        if protocol is Protocol.VMESS:
            params["alter_id"] = _int(user.get("alterId"))
            params["encryption"] = str(user.get("security", "auto"))
    elif protocol in (Protocol.TROJAN, Protocol.SHADOWSOCKS):
        server = _first(settings.get("servers")) if isinstance(settings, Mapping) else None
        if not isinstance(server, Mapping):
            return None
        host = str(server.get("address", ""))
        port = _int(server.get("port"))
        if server.get("password"):
            credentials["password"] = str(server["password"])
        if protocol is Protocol.SHADOWSOCKS:
            params["method"] = str(server.get("method", "aes-256-gcm"))
    elif protocol is Protocol.WIREGUARD:
        peer = _first(settings.get("peers")) if isinstance(settings, Mapping) else None
        if not isinstance(peer, Mapping):
            return None
        endpoint = str(peer.get("endpoint", ""))
        host, _, port_text = endpoint.rpartition(":")
        port = _int(port_text)
        if peer.get("publicKey"):
            params["public_key"] = str(peer["publicKey"])
        if settings.get("secretKey"):
            credentials["private_key"] = str(settings["secretKey"])
        if settings.get("address"):
            params["address"] = settings["address"]
        if peer.get("allowedIPs"):
            params["allowed_ips"] = peer["allowedIPs"]
    if not host or not port:
        return None

    network = stream.get("network", "tcp") if isinstance(stream, Mapping) else "tcp"
    security = stream.get("security", "none") if isinstance(stream, Mapping) else "none"
    transport = _transport(network)
    if transport is Transport.HTTP:
        transport = Transport.TCP
    params["transport"] = transport.value
    if isinstance(stream, Mapping):
        for source, target in (
            ("tlsSettings", "tls"),
            ("realitySettings", "reality"),
        ):
            block = stream.get(source)
            if isinstance(block, Mapping):
                if block.get("serverName"):
                    params["sni"] = str(block["serverName"])
                if block.get("fingerprint"):
                    params["fingerprint"] = str(block["fingerprint"])
                if block.get("alpn"):
                    params["alpn"] = block["alpn"]
                if target == "reality":
                    if block.get("publicKey"):
                        credentials["public_key"] = str(block["publicKey"])
                    if block.get("shortId"):
                        credentials["short_id"] = str(block["shortId"])
        for source in ("wsSettings", "httpupgradeSettings", "xhttpSettings"):
            block = stream.get(source)
            if isinstance(block, Mapping):
                if block.get("path"):
                    params["path"] = str(block["path"])
                if block.get("host"):
                    params["host"] = str(block["host"])
                headers = block.get("headers")
                if isinstance(headers, Mapping) and headers.get("Host"):
                    params["host"] = str(headers["Host"])
        grpc = stream.get("grpcSettings")
        if isinstance(grpc, Mapping) and grpc.get("serviceName"):
            params["service_name"] = str(grpc["serviceName"])

    tag = outbound.get("tag")
    name = fallback_name or (str(tag) if tag and tag not in {"proxy"} else f"{host}:{port}")
    return Server(
        id=_stable_id(protocol.value, host, port, name),
        name=name,
        host=host,
        port=port,
        protocol=protocol,
        transport=transport,
        security=_security(security),
        params={key: value for key, value in params.items() if key != "transport"},
        credentials=credentials,
    )


def server_from_outbound(
    outbound: Mapping[str, Any], *, fallback_name: str = ""
) -> Server | None:
    """Public wrapper around the Xray outbound projection."""

    return _server_from_outbound(outbound, fallback_name=fallback_name)


def _servers_from_xray_config(config: Mapping[str, Any]) -> list[Server]:
    servers: list[Server] = []
    outbounds = config.get("outbounds")
    if not isinstance(outbounds, list):
        return servers
    for outbound in outbounds:
        if not isinstance(outbound, Mapping):
            continue
        server = _server_from_outbound(outbound)
        if server is not None:
            servers.append(server)
    return servers


def _clean_edge(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("[") and "]" in text:
        text = text[1 : text.index("]")]
    if "/" in text:
        text = text.split("/", 1)[0]
    return text


def _servers_from_manifest(
    data: Mapping[str, Any],
    *,
    host: str = "edge",
    port: int = 443,
) -> list[Server]:
    """Build catalog entries from a native ``manifest`` response."""

    def number(value: Any) -> int:
        try:
            return max(0, int(str(value)))
        except (TypeError, ValueError):
            return 0

    best: dict[str, tuple[int, int, int, bool, bool]] = {}
    items = data.get("datacenters", [])
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        country = str(item.get("country", "")).strip().upper()
        if len(country) != 2 or not (country.isascii() and country.isalpha()):
            continue
        latency = item.get("latency")
        if not isinstance(latency, Mapping):
            latency = {}
        pref = number(latency.get("pref"))
        real = number(latency.get("real"))
        premium = bool(item.get("premium", False))
        expensive = bool(item.get("expensive", False))
        candidate = (1 if premium else 0, pref, real, premium, expensive)
        current = best.get(country)
        if current is None or candidate[:2] < current[:2]:
            best[country] = candidate

    servers: list[Server] = []
    for country in sorted(best, key=lambda code: (best[code][0], best[code][1], code)):
        _premium_rank, pref, real, premium, expensive = best[country]
        tags = ["edge"]
        if premium:
            tags.append("premium")
        if expensive:
            tags.append("expensive")
        servers.append(
            Server(
                id=f"edge-{country.lower()}",
                name=country,
                host=host,
                port=port,
                protocol=Protocol.WIREGUARD,
                transport=Transport.TCP,
                security=Security.TLS,
                country=country,
                country_code=country,
                premium=premium,
                latency_ms=pref or None,
                tags=tags,
                params={
                    "provider": "ninja-edge",
                    "latency_pref": pref,
                    "latency_real": real,
                    "expensive": expensive,
                },
            )
        )
    return servers


def _servers_from_install(data: Mapping[str, Any]) -> list[Server]:
    edges = [
        _clean_edge(item)
        for item in data.get("edges", [])
        if _clean_edge(item)
    ]
    if not edges:
        return []
    country = str(data.get("country", "")).strip().upper()[:2]
    stealth = data.get("stealth")
    xray = stealth.get("xray") if isinstance(stealth, Mapping) else None
    if isinstance(xray, Mapping):
        server = _server_from_outbound(xray, fallback_name=country or "edge")
        if server is not None:
            server.host = edges[0]
            server.params = {
                key: value
                for key, value in server.params.items()
                if value not in {"%addr%", "%host%"}
            }
            server.params["provider"] = "ninja-edge"
            server.country_code = country
            server.tags = sorted(set([*server.tags, "edge"]))
            return [server]
    config = data.get("config")
    if isinstance(config, Mapping):
        try:
            port = int(config.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        peer = str(config.get("peer", ""))
        if port and peer:
            return [
                Server(
                    id=_stable_id("wg", edges[0], port, "WireGuard"),
                    name=country or "WireGuard",
                    host=edges[0],
                    port=port,
                    protocol=Protocol.WIREGUARD,
                    country_code=country,
                    tags=["edge"],
                    params={"public_key": peer, "provider": "ninja-edge"},
                )
            ]
    return []


def _servers_from_mapping(data: Mapping[str, Any]) -> list[Server]:
    if isinstance(data.get("servers"), list):
        return _servers_from_list(data["servers"])
    if isinstance(data.get("outbounds"), list):
        return _servers_from_xray_config(data)
    if isinstance(data.get("edges"), list):
        return _servers_from_install(data)
    if data.get("host") and data.get("port"):
        return _servers_from_list([data])
    return []


def _servers_from_list(items: Iterable[Any]) -> list[Server]:
    servers: list[Server] = []
    for item in items:
        if isinstance(item, str):
            parsed = parse_share_link(item)
            if parsed is not None:
                servers.append(parsed)
            continue
        if not isinstance(item, Mapping):
            continue
        if item.get("protocol") and item.get("vnext") is None and item.get("host"):
            try:
                servers.append(Server.from_dict(item))
                continue
            except Exception:
                pass
        if item.get("protocol"):
            parsed = _server_from_outbound(item)
            if parsed is not None:
                servers.append(parsed)
    return servers


def parse_subscription(payload: bytes | str) -> list[Server]:
    """Parse JSON, base64 or newline-separated share links."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_SUBSCRIPTION_BYTES:
            raise SubscriptionError("subscription is too large")
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SubscriptionError("subscription is not UTF-8") from exc
    else:
        text = str(payload).lstrip("\ufeff")
    if len(text.encode("utf-8")) > MAX_SUBSCRIPTION_BYTES:
        raise SubscriptionError("subscription is too large")
    text = text.strip()
    if not text:
        raise SubscriptionError("subscription is empty")

    candidates = [text]
    decoded = _decode_base64(text)
    if decoded and decoded.strip() != text:
        candidates.append(decoded.strip())

    for candidate in candidates:
        parsed = _parse_text(candidate)
        if parsed:
            return _dedupe(parsed)
    raise SubscriptionError("subscription contains no supported servers")


def _parse_text(text: str) -> list[Server]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if data is not None:
        if isinstance(data, list):
            return _servers_from_list(data)
        if isinstance(data, Mapping):
            return _servers_from_mapping(data)
        return []
    servers: list[Server] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = parse_share_link(line)
        if parsed is not None:
            servers.append(parsed)
    return servers


def _dedupe(servers: list[Server]) -> list[Server]:
    seen: set[str] = set()
    result: list[Server] = []
    for server in servers:
        if server.id in seen:
            continue
        seen.add(server.id)
        result.append(server)
    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme
        if scheme != "https":
            raise SubscriptionError("subscription redirect must use https")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise SubscriptionError("subscription URL is empty")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise SubscriptionError("subscription URL must be http(s)")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SubscriptionError("plain http is allowed only for localhost")
    if parts.username or parts.password:
        raise SubscriptionError("subscription URL must not contain credentials")
    return text


def http_get(url: str, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Download a subscription without following non-HTTPS redirects."""

    target = _validate_url(url)
    request = urllib.request.Request(
        target,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        method="GET",
    )
    opener = urllib.request.build_opener(_SafeRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise SubscriptionError(f"subscription returned HTTP {status}")
            payload = response.read(MAX_SUBSCRIPTION_BYTES + 1)
    except SubscriptionError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SubscriptionError(f"subscription download failed: {exc.__class__.__name__}") from exc
    if len(payload) > MAX_SUBSCRIPTION_BYTES:
        raise SubscriptionError("subscription is too large")
    return payload


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    parent = path.parent
    try:
        info = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SubscriptionError(f"unsafe cache directory: {parent}")
    with contextlib.suppress(OSError):
        os.chmod(parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


class ServerRepository:
    """In-memory servers (with secrets) plus a public on-disk cache."""

    def __init__(
        self,
        cache_path: str | Path | None = None,
        *,
        fetcher=None,
        edge_bin_path: str | Path | None = None,
    ) -> None:
        self.cache_path = (
            Path(cache_path).expanduser()
            if cache_path is not None
            else default_state_dir() / "servers.json"
        )
        self.edge_bin_path = (
            Path(edge_bin_path).expanduser() if edge_bin_path else default_edge_bin_path()
        )
        self.fetcher = fetcher or http_get
        self.servers: list[Server] = []
        self.updated_at: float | None = None
        self.source = "cache"
        self.raw_config: dict[str, Any] | None = None
        self.imported_server_id: str | None = None

    # -- state -------------------------------------------------------------
    def set_servers(self, servers: Iterable[Server], *, source: str = "subscription") -> None:
        unique: dict[str, Server] = {}
        for server in servers:
            if not isinstance(server, Server):
                continue
            try:
                server.validate()
            except Exception as exc:
                raise SubscriptionError(f"invalid server: {exc}") from exc
            unique[server.id] = server
        self.servers = list(unique.values())
        self.updated_at = time.time()
        self.source = source
        self.raw_config = None
        self.imported_server_id = None
        self.save_cache()

    def set_imported_config(self, config: dict[str, Any], *, server: Server) -> None:
        if not isinstance(config, dict):
            raise SubscriptionError("imported config must be a JSON object")
        self.raw_config = config
        self.imported_server_id = server.id
        self.set_servers([server], source="import")
        self.raw_config = config
        self.imported_server_id = server.id

    def merge_servers(
        self, servers: Iterable[Server], *, source: str = "builtin"
    ) -> int:
        """Add missing server ids without replacing existing entries.

        Existing servers always win: a subscription or a manual import must not
        be overwritten by a built-in profile.  The only exception is a cached
        server whose credentials were stripped by :meth:`save_cache`; in that
        case the in-memory credentials are restored from ``servers``.

        Returns the number of newly added servers.  The operation is
        idempotent and never clears ``raw_config``/``imported_server_id``.
        """

        known = {server.id: server for server in self.servers}
        added = 0
        changed = False
        for server in servers:
            if not isinstance(server, Server):
                continue
            try:
                server.validate()
            except Exception as exc:
                raise SubscriptionError(f"invalid server: {exc}") from exc
            existing = known.get(server.id)
            if existing is not None:
                is_cached_builtin = (
                    "builtin" in existing.tags
                    or existing.params.get("source") == "apk"
                    or existing.params.get("provider") == "apk-edge"
                )
                if is_cached_builtin and not existing.credentials and server.credentials:
                    existing.credentials = dict(server.credentials)
                    changed = True
                continue
            server.params.setdefault("source", source)
            self.servers.append(server)
            known[server.id] = server
            added += 1
            changed = True
        if changed:
            if added:
                self.updated_at = time.time()
            self.save_cache()
        return added

    # -- built-in edge servers ---------------------------------------------
    def _load_builtin_servers(
        self, edge_bin_path: str | Path | None = None
    ) -> list[Server]:
        from .builtin_edge import builtin_servers, load_builtin_config
        from .edge_config import EdgeConfigError
        from .identity import load_identity

        path = (
            Path(edge_bin_path).expanduser() if edge_bin_path else self.edge_bin_path
        )
        try:
            config = load_builtin_config(load_identity().hard, path=path)
        except (EdgeConfigError, OSError) as exc:
            raise SubscriptionError(f"cannot load built-in edge config: {exc}") from exc
        servers = builtin_servers(config)
        if not servers:
            raise SubscriptionError("built-in edge config has no usable server")
        return servers

    def import_builtin(
        self, edge_bin_path: str | Path | None = None
    ) -> ImportResult:
        """Merge the server projected from ``edge.bin`` into the catalog."""

        servers = self._load_builtin_servers(edge_bin_path)
        added = self.merge_servers(servers, source="builtin")
        return ImportResult(
            servers=list(self.servers),
            source="builtin",
            fetched_at=self.updated_at or time.time(),
            updated=added > 0,
        )

    def refresh_builtin_servers(
        self, edge_bin_path: str | Path | None = None
    ) -> ImportResult:
        """Re-seed built-in entries, restoring credentials stripped by the cache.

        Matching built-in entries keep their position; entries from other
        sources are never replaced.
        """

        fresh = {
            server.id: server for server in self._load_builtin_servers(edge_bin_path)
        }
        changed = False
        known: set[str] = set()
        for index, server in enumerate(self.servers):
            known.add(server.id)
            if "builtin" not in server.tags:
                continue
            replacement = fresh.pop(server.id, None)
            if replacement is not None:
                self.servers[index] = replacement
                changed = True
        for server in fresh.values():
            if server.id in known:
                continue
            self.servers.append(server)
            known.add(server.id)
            changed = True
        if changed:
            self.updated_at = time.time()
            self.save_cache()
        return ImportResult(
            servers=list(self.servers),
            source="builtin",
            fetched_at=self.updated_at or time.time(),
            updated=changed,
        )

    def get(self, server_id: str) -> Server | None:
        for server in self.servers:
            if server.id == server_id:
                return server
        return None

    def select(self, settings: Settings, server_id: str | None = None) -> Server:
        wanted = server_id or settings.selected_server_id
        if wanted:
            found = self.get(wanted)
            if found is None:
                raise SubscriptionError(f"server not found: {wanted}")
            return found
        if settings.selected_country:
            for server in self.servers:
                if server.country_code.upper() == settings.selected_country.upper():
                    return server
        if not self.servers:
            raise SubscriptionError("no servers available")
        return self.servers[0]

    # -- cache -------------------------------------------------------------
    def save_cache(self) -> None:
        payload = {
            "schema_version": 1,
            "updated_at": self.updated_at,
            "source": self.source,
            "servers": [server.to_public_dict() for server in self.servers],
        }
        _write_private_json(self.cache_path, payload)

    def load_cache(self) -> list[Server]:
        try:
            info = self.cache_path.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SubscriptionError("server cache must be a regular file")
        if info.st_size > MAX_SUBSCRIPTION_BYTES:
            raise SubscriptionError("server cache is too large")
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SubscriptionError(f"cannot read server cache: {exc.__class__.__name__}") from exc
        if not isinstance(data, Mapping):
            raise SubscriptionError("server cache must be an object")
        servers: list[Server] = []
        for item in data.get("servers", []):
            if not isinstance(item, Mapping):
                continue
            try:
                server = Server.from_dict(item)
            except Exception:
                continue
            servers.append(server)
        self.servers = servers
        updated = data.get("updated_at")
        self.updated_at = float(updated) if isinstance(updated, (int, float)) else None
        self.source = str(data.get("source", "cache"))
        return servers

    # -- import ------------------------------------------------------------
    def import_subscription(
        self,
        url: str,
        *,
        force: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        max_age_hours: int = 24,
    ) -> ImportResult:
        target = _validate_url(url)
        if (
            not force
            and self.updated_at is not None
            and self.servers
            and time.time() - self.updated_at < max_age_hours * 3600
        ):
            return ImportResult(
                servers=list(self.servers),
                source=self.source,
                fetched_at=self.updated_at,
                updated=False,
            )
        try:
            payload = self.fetcher(target, timeout)
        except SubscriptionError:
            raise
        except Exception as exc:
            raise SubscriptionError(f"subscription download failed: {exc.__class__.__name__}") from exc
        servers = parse_subscription(payload)
        self.set_servers(servers, source="subscription")
        return ImportResult(
            servers=list(self.servers),
            source="subscription",
            fetched_at=self.updated_at or time.time(),
        )

    def import_file(self, path: str | Path) -> ImportResult:
        target = Path(path).expanduser()
        try:
            info = target.lstat()
        except OSError as exc:
            raise SubscriptionError(f"cannot open import file: {exc.__class__.__name__}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SubscriptionError("import source must be a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise SubscriptionError("import file is too large")
        try:
            payload = target.read_bytes()
        except OSError as exc:
            raise SubscriptionError(f"cannot read import file: {exc.__class__.__name__}") from exc
        return self.import_payload(payload, source="import")

    def import_url(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> ImportResult:
        target = _validate_url(url)
        try:
            payload = self.fetcher(target, timeout)
        except SubscriptionError:
            raise
        except Exception as exc:
            raise SubscriptionError(f"import download failed: {exc.__class__.__name__}") from exc
        return self.import_payload(payload, source="import")

    def import_payload(self, payload: bytes | str, *, source: str = "import") -> ImportResult:
        text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else str(payload)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, Mapping) and isinstance(data.get("outbounds"), list):
            servers = _servers_from_xray_config(data)
            if not servers:
                raise SubscriptionError("imported Xray config has no usable outbound")
            server = servers[0]
            server.name = server.name or "Импортированный конфиг"
            server.tags = sorted(set([*server.tags, "imported"]))
            self.raw_config = data
            self.imported_server_id = server.id
            self.set_servers([server], source=source)
            self.raw_config = data
            self.imported_server_id = server.id
            return ImportResult(
                servers=list(self.servers),
                source=source,
                fetched_at=self.updated_at or time.time(),
            )
        servers = parse_subscription(text)
        self.set_servers(servers, source=source)
        return ImportResult(
            servers=list(self.servers),
            source=source,
            fetched_at=self.updated_at or time.time(),
        )

    def import_text(self, text: str, *, source: str = "import") -> ImportResult:
        return self.import_payload(text, source=source)

    # -- ping --------------------------------------------------------------
    def ping(self, ids: list[str] | None = None, *, timeout: float = 3.0) -> list[dict[str, Any]]:
        timeout = max(0.1, min(float(timeout), 30.0))
        selected = self.servers
        if ids is not None:
            wanted = set(ids[:500])
            selected = [server for server in self.servers if server.id in wanted]
        results: list[dict[str, Any]] = []
        for server in selected:
            started = time.monotonic()
            try:
                with socket.create_connection((server.host, server.port), timeout=timeout):
                    latency = int((time.monotonic() - started) * 1000)
                server.latency_ms = latency
                results.append({"id": server.id, "latency_ms": latency, "error": None})
            except OSError:
                results.append({"id": server.id, "latency_ms": None, "error": "timeout"})
        return results


__all__ = [
    "MAX_SUBSCRIPTION_BYTES",
    "ImportResult",
    "ServerRepository",
    "SubscriptionError",
    "http_get",
    "parse_share_link",
    "parse_subscription",
    "server_from_outbound",
]
