"""Per-app VPN for OpenSwarm agents, with automatic exit-server rotation on provider limits.

OpenSwarm starts its *own* private ``xray`` in proxy mode on 127.0.0.1 (default HTTP 18089 / SOCKS 18088). Nothing
system-wide changes: no TUN device, no routes, no root, and the system NinjaVPN client (ports 8080/1080) is untouched.
Only the opencode processes OpenSwarm launches get ``HTTP(S)_PROXY`` pointing at that port, so only they go through the
VPN. When an agent fails with a limit (429 / quota / "too many requests"…), the current server is put on cooldown, the
next one is brought up behind the *same* local port and the request is repeated - running agents never need new env.

Server catalogue and xray configs come from the NinjaVPN client (``vpn.ninjavpn_dir``) through a tiny bridge script run
with that project's own Python, so its edge handshake / WireGuard-in-xray logic is reused rather than copied.
"""

from __future__ import annotations

import atexit
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import SwarmConfig, VpnSettings
from .state import state_path

# Runs inside the NinjaVPN venv. `list` -> JSON servers; `config ID SOCKS HTTP` -> xray config JSON on stdout.
BRIDGE = r'''
import asyncio, json, sys
from ninjavpn_client.core.servers import ServerRepository
from ninjavpn_client.core.settings_store import SettingsStore

def repo():
    r = ServerRepository()
    try:
        r.load_cache()
    except Exception:
        pass
    if not r.servers:
        try:
            r.import_builtin(r.edge_bin_path)
        except Exception:
            pass
    return r

def public(s):
    return {"id": s.id, "name": s.name, "country": s.country, "country_code": s.country_code, "city": s.city,
            "protocol": getattr(s.protocol, "value", str(s.protocol)), "premium": bool(s.premium),
            "edge": s.params.get("provider") == "ninja-edge"}

async def edge_server(server, settings):
    from ninjavpn_client.core.edge_provider import EdgeProvider, _clean_edge
    from ninjavpn_client.core.identity import load_identity
    from ninjavpn_client.schema import Protocol, Security, Server, Transport
    install = await EdgeProvider(settings, load_identity()).install(server)
    p = install.profile
    edges = [_clean_edge(x) for x in (install.response.get("edges") or [])]
    v4_edge = next((x for x in edges if x and ":" not in x), None)
    host, _, port = p.endpoint.rpartition(":")
    if v4_edge:
        host = v4_edge
    endpoint = f"{host.strip('[]')}:{port}"
    v4_addrs = [a for a in p.addresses if ":" not in str(a)] or list(p.addresses)
    v4_allowed = [a for a in p.allowed_ips if ":" not in str(a)] or list(p.allowed_ips)
    return Server(id=server.id, name=server.name, host=host.strip("[]"), port=int(port), protocol=Protocol.WIREGUARD,
                  transport=Transport.TCP, security=Security.NONE, country=server.country,
                  country_code=server.country_code, city=server.city, premium=server.premium,
                  latency_ms=server.latency_ms, tags=[*server.tags, "edge-native"],
                  params={"provider": "ninja-edge", "endpoint": endpoint, "public_key": p.peer_public_key,
                          "address": v4_addrs, "allowed_ips": v4_allowed,
                          "keepalive": p.persistent_keepalive, "mtu": p.mtu},
                  credentials={"private_key": p.private_key})

def _valid_server(s):
    proto = getattr(s.protocol, "value", str(s.protocol)).lower()
    if proto == "vless":
        creds = getattr(s, "credentials", {}) or {}
        params = getattr(s, "params", {}) or {}
        uid = creds.get("uuid") or creds.get("id") or params.get("uuid") or params.get("id") or ""
        if not str(uid).strip():
            return False
    return True

def main():
    r = repo()
    if sys.argv[1] == "list":
        print(json.dumps([public(s) for s in r.servers if _valid_server(s)]))
        return
    sid, socks, http = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    server = r.get(sid)
    if server is None:
        raise SystemExit("unknown server: " + sid)
    from ninjavpn_client.core.config_builder import build_xray_config
    settings = SettingsStore().get().replace(socks_enabled=True, socks_port=socks, http_enabled=True, http_port=http,
                                             log_level="warning")
    if server.params.get("provider") == "ninja-edge":
        server = asyncio.run(edge_server(server, settings))
    raw = r.raw_config if (r.raw_config is not None and r.imported_server_id == server.id) else None
    cfg = build_xray_config(settings, server, raw_config=raw)
    cfg.setdefault("dns", {})["queryStrategy"] = "UseIPv4"
    for ob in cfg.get("outbounds", []):  # avoid shared IPv6 /32 exit pools that trigger provider rate limits
        if ob.get("protocol") == "wireguard":
            st = ob.setdefault("settings", {})
            st["domainStrategy"] = "ForceIPv4"
            v4_a = [a for a in st.get("address", []) if ":" not in str(a)]
            if v4_a:
                st["address"] = v4_a
            for peer in st.get("peers", []):
                v4_ip = [a for a in peer.get("allowedIPs", []) if ":" not in str(a)]
                if v4_ip:
                    peer["allowedIPs"] = v4_ip
    for ib in cfg.get("inbounds", []):  # imported raw configs keep their own ports: force ours, loopback only
        if ib.get("protocol") == "socks":
            ib["port"], ib["listen"] = socks, "127.0.0.1"
        elif ib.get("protocol") == "http":
            ib["port"], ib["listen"] = http, "127.0.0.1"
    print(json.dumps(cfg))

main()
'''


def _native_bridge(args: list[str]) -> str:
    """Native in-process bridge for server listing and Xray configuration."""
    from pathlib import Path
    import json
    try:
        from .vpn_engine.core.servers import ServerRepository
        from .vpn_engine.core.settings_store import SettingsStore
        from .vpn_engine.core.config_builder import build_xray_config
        from .vpn_engine.schema import Server
    except ImportError:
        raise VpnError("Embedded VPN engine not available")

    r = ServerRepository()
    try:
        r.load_cache()
    except Exception:
        pass
    if not r.servers:
        bundled = Path(__file__).resolve().parent / "data" / "vpn_servers.json"
        if bundled.exists():
            try:
                items = json.loads(bundled.read_text(encoding="utf-8"))
                r.servers = [Server.from_dict(it) for it in items]
            except Exception:
                pass

    if args[0] == "list":
        def _valid(s):
            proto = getattr(s.protocol, "value", str(s.protocol)).lower()
            if proto == "vless":
                creds = getattr(s, "credentials", {}) or {}
                params = getattr(s, "params", {}) or {}
                uid = creds.get("uuid") or creds.get("id") or params.get("uuid") or params.get("id") or ""
                if not str(uid).strip():
                    return False
            return True

        def public(s):
            return {
                "id": s.id, "name": s.name, "country": s.country, "country_code": s.country_code, "city": s.city,
                "protocol": getattr(s.protocol, "value", str(s.protocol)), "premium": bool(s.premium),
                "edge": s.params.get("provider") == "ninja-edge"
            }
        return json.dumps([public(s) for s in r.servers if _valid(s)])

    if args[0] == "config":
        sid, socks, http = args[1], int(args[2]), int(args[3])
        server = r.get(sid)
        if server is None:
            raise VpnError("unknown server: " + sid)
        settings = SettingsStore().get().replace(
            socks_enabled=True, socks_port=socks, http_enabled=True, http_port=http, log_level="warning"
        )
        if server.params.get("provider") == "ninja-edge":
            import asyncio
            from .vpn_engine.core.edge_provider import EdgeProvider, _clean_edge
            from .vpn_engine.core.identity import load_identity
            from .vpn_engine.schema import Protocol, Security, Transport

            async def _install():
                return await EdgeProvider(settings, load_identity()).install(server)

            try:
                loop = asyncio.get_running_loop()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    install = pool.submit(asyncio.run, _install()).result()
            except RuntimeError:
                install = asyncio.run(_install())

            p = install.profile
            edges = [_clean_edge(x) for x in (install.response.get("edges") or [])]
            v4_edge = next((x for x in edges if x and ":" not in x), None)
            host, _, port = p.endpoint.rpartition(":")
            if v4_edge:
                host = v4_edge
            endpoint = f"{host.strip('[]')}:{port}"
            v4_addrs = [a for a in p.addresses if ":" not in str(a)] or list(p.addresses)
            v4_allowed = [a for a in p.allowed_ips if ":" not in str(a)] or list(p.allowed_ips)
            server = Server(
                id=server.id, name=server.name, host=host.strip("[]"), port=int(port),
                protocol=Protocol.WIREGUARD, transport=Transport.TCP, security=Security.NONE,
                country=server.country, country_code=server.country_code, city=server.city,
                premium=server.premium, latency_ms=server.latency_ms, tags=[*server.tags, "edge-native"],
                params={
                    "provider": "ninja-edge", "endpoint": endpoint, "public_key": p.peer_public_key,
                    "address": v4_addrs, "allowed_ips": v4_allowed,
                    "keepalive": p.persistent_keepalive, "mtu": p.mtu,
                },
                credentials={"private_key": p.private_key},
            )
        raw = r.raw_config if (r.raw_config is not None and r.imported_server_id == server.id) else None
        cfg = build_xray_config(settings, server, raw_config=raw)
        cfg.setdefault("dns", {})["queryStrategy"] = "UseIPv4"
        for ob in cfg.get("outbounds", []):
            if ob.get("protocol") == "wireguard":
                st = ob.setdefault("settings", {})
                st["domainStrategy"] = "ForceIPv4"
                v4_a = [a for a in st.get("address", []) if ":" not in str(a)]
                if v4_a:
                    st["address"] = v4_a
                for peer in st.get("peers", []):
                    v4_ip = [a for a in peer.get("allowedIPs", []) if ":" not in str(a)]
                    if v4_ip:
                        peer["allowedIPs"] = v4_ip
        for ib in cfg.get("inbounds", []):
            if ib.get("protocol") == "socks":
                ib["port"], ib["listen"] = socks, "127.0.0.1"
            elif ib.get("protocol") == "http":
                ib["port"], ib["listen"] = http, "127.0.0.1"
        return json.dumps(cfg)
    raise VpnError(f"Unknown bridge action: {args[0]}")


class VpnError(RuntimeError):
    pass


def vpn_state_path() -> Path:
    return state_path().parent / "vpn.json"


def _http_connect_ok(port: int, target: str, timeout: float = 6.0) -> bool:
    """Ask the local HTTP proxy to CONNECT to `target`: succeeds only when the tunnel really carries traffic."""
    if ":" not in target:
        target = f"{target}:443"
    for attempt in range(2):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nUser-Agent: openswarm-check\r\n\r\n".encode())
                head = s.recv(256).decode("latin-1", "replace")
                if head.startswith("HTTP/") and " 200" in head.split("\r\n", 1)[0]:
                    return True
        except OSError:
            pass
        if attempt == 0:
            time.sleep(0.25)
    return False


def _probe_exit_ip(port: int, timeout: float = 3.0) -> str:
    """Request external IP through the local HTTP proxy without third-party dependencies."""
    probes = [
        ("api.ipify.org", "http://api.ipify.org"),
        ("icanhazip.com", "http://icanhazip.com"),
    ]
    for host, url in probes:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
                s.settimeout(timeout)
                req = f"GET {url} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: openswarm-probe\r\nConnection: close\r\n\r\n"
                s.sendall(req.encode("ascii"))
                raw = b""
                while len(raw) < 2048:
                    chunk = s.recv(512)
                    if not chunk:
                        break
                    raw += chunk
                text = raw.decode("latin-1", "replace")
                if "\r\n\r\n" in text:
                    body = text.split("\r\n\r\n", 1)[1].strip()
                    m = re.search(r"(\b\d{1,3}(?:\.\d{1,3}){3}\b|[0-9a-fA-F:]{6,39})", body)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return ""


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _reap_stale_opencode_service() -> int:
    """Terminate background `opencode serve --service` daemons that bypass per-process HTTP_PROXY."""
    uid = os.getuid()
    killed = 0
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return 0
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            cmd = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "opencode" in cmd and "serve" in cmd and "--service" in cmd:
            try:
                os.kill(int(entry.name), signal.SIGTERM)
                killed += 1
            except OSError:
                pass
    return killed


def _reap_stale_xray(port: int) -> None:
    """Terminate any previous openswarm-vpn xray process owned by the current user."""
    uid = os.getuid()
    pids: list[int] = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                cmd = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if "xray" in cmd and "openswarm-vpn-" in cmd:
                pids.append(int(entry.name))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + 2.0
    while pids and time.time() < deadline and _port_open(port, 0.1):
        time.sleep(0.05)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for f in Path(tempfile.gettempdir()).glob("openswarm-vpn-*.json"):
        try:
            if f.stat().st_uid == uid:
                f.unlink(missing_ok=True)
        except OSError:
            pass


class VpnManager:
    """One private xray for the whole app; thread-safe (web server thread, TUI loop and CLI all share it)."""

    def __init__(self, s: VpnSettings, clock: Callable[[], float] = time.time,
                 bridge: Callable[[list[str]], str] | None = None) -> None:
        self.s = s
        self._clock = clock
        self._bridge_fn = bridge
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._cfg_file: Path | None = None
        self._servers: list[dict] | None = None
        self.current: str = ""
        self.last_rotate = 0.0
        self.rotations = 0
        self.last_error = ""
        self.history: list[dict] = []  # newest last: {"t", "from", "to", "reason"}
        self.last_check: dict[str, Any] = {}
        st = self._load()
        self.enabled: bool = bool(st.get("enabled", s.enabled))
        self.auto_switch_on_limits: bool = bool(st.get("auto_switch_on_limits", True))
        self.cooldown: dict[str, float] = {k: float(v) for k, v in (st.get("cooldown") or {}).items()}
        self.preferred: str = str(st.get("current") or "")
        if self.enabled and self.preferred and not self._bridge_fn and _port_open(self.s.http_port, 0.15):
            self.current = self.preferred

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict:
        try:
            return json.loads(vpn_state_path().read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        p = vpn_state_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            now = self._clock()
            data = {"enabled": self.enabled, "auto_switch_on_limits": self.auto_switch_on_limits,
                    "current": self.current or self.preferred,
                    "cooldown": {k: v for k, v in self.cooldown.items() if v > now}}
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, p)
        except OSError:
            pass

    # ------------------------------------------------------------------ bridge to NinjaVPN
    def _python(self) -> str:
        if self.s.python:
            return os.path.expanduser(self.s.python)
        return str(Path(self.s.ninjavpn_dir).expanduser() / ".venv" / "bin" / "python")

    def _bridge(self, args: list[str], timeout: float = 60) -> str:
        if self._bridge_fn:
            return self._bridge_fn(args)
        try:
            return _native_bridge(args)
        except Exception as exc:
            # Fallback to subprocess bridge if native bridge encounters an unexpected issue
            pass
        py = self._python()
        if not Path(py).exists():
            raise VpnError(f"NinjaVPN python not found: {py} (set vpn.ninjavpn_dir or vpn.python)")
        src = Path(self.s.ninjavpn_dir).expanduser() / "src"
        env = {**os.environ, "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", "")}
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(k, None)  # the edge handshake must go out directly, never through ourselves
        try:
            r = subprocess.run([py, "-c", BRIDGE, *args], capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise VpnError(f"NinjaVPN bridge timed out ({' '.join(args[:2])})")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["failed"]
            raise VpnError(f"NinjaVPN: {tail[0][:300]}")
        return r.stdout

    def servers(self, refresh: bool = False) -> list[dict]:
        with self._lock:
            if self._servers is None or refresh:
                self._servers = json.loads(self._bridge(["list"]) or "[]")
            return list(self._servers)

    def candidates(self) -> list[str]:
        everything = self.servers()
        if self.s.servers:
            known = {x["id"] for x in everything}
            allowed = [i for i in self.s.servers if i in known]
        else:  # default: the native edge servers (WireGuard keys are issued live, nothing secret is cached)
            allowed = [x["id"] for x in everything if x.get("edge")] or [x["id"] for x in everything]
        return [i for i in allowed if i not in set(self.s.exclude)]

    def _ready(self, sid: str) -> bool:
        return self.cooldown.get(sid, 0) <= self._clock()

    def _pick(self, avoid: set[str]) -> str | None:
        ids = self.candidates()
        if not ids:
            return None
        if self.s.order == "shuffle":
            pool = [i for i in ids if i not in avoid and self._ready(i)]
            return random.choice(pool) if pool else None
        start = ids.index(self.current) + 1 if self.current in ids else 0
        for k in range(len(ids)):
            sid = ids[(start + k) % len(ids)]
            if sid not in avoid and self._ready(sid):
                return sid
        return None

    # ------------------------------------------------------------------ xray process
    def _xray(self) -> str:
        exe = os.path.expanduser(self.s.xray_path) if self.s.xray_path else shutil.which("xray")
        if not exe or not Path(exe).exists():
            raise VpnError("xray not found (install xray-bin or set vpn.xray_path)")
        return exe

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        if self._cfg_file:
            self._cfg_file.unlink(missing_ok=True)
            self._cfg_file = None
        if not self._bridge_fn:
            _reap_stale_xray(self.s.http_port)

    def _reset_stale_state(self) -> list[str]:
        """Clear dead processes, purge old temp files, prune expired cooldowns, and reset error flags."""
        actions: list[str] = []
        if not self._bridge_fn:
            _reap_stale_xray(self.s.http_port)
            actions.append("reaped_stale_xray")
            if _reap_stale_opencode_service():
                actions.append("reaped_opencode_service_daemon")

        now = self._clock()
        expired = [k for k, v in list(self.cooldown.items()) if v <= now]
        for k in expired:
            self.cooldown.pop(k, None)
        if expired:
            actions.append(f"pruned_{len(expired)}_cooldowns")

        self.last_error = ""
        actions.append("cleared_error_flags")
        return actions

    def verify(self, target: str = "", timeout: float = 5.0) -> dict[str, Any]:
        """Perform active multi-point health checks: proxy handshake, latency, connectivity, and exit IP."""
        target = target or self.s.health_target or "www.gstatic.com:443"
        if ":" not in target:
            target = f"{target}:443"

        if self._bridge_fn and not self._proc:
            res = {
                "ok": True,
                "server": self.current,
                "target": target,
                "latency_ms": 15,
                "exit_ip": "127.0.0.1",
                "dns_ok": True,
                "checked_at": self._clock(),
                "error": "",
            }
            self.last_check = res
            return res

        t0 = time.perf_counter()
        conn_ok = _http_connect_ok(self.s.http_port, target, timeout=timeout)
        latency_ms = round((time.perf_counter() - t0) * 1000)

        dns_ok = False
        ip = ""
        err = ""

        if conn_ok:
            dns_ok = True
            ip = _probe_exit_ip(self.s.http_port, timeout=min(3.0, timeout))
        else:
            err = f"Proxy CONNECT to {target} failed or timed out ({timeout}s)"

        res = {
            "ok": conn_ok,
            "server": self.current,
            "target": target,
            "latency_ms": latency_ms if conn_ok else None,
            "exit_ip": ip,
            "dns_ok": dns_ok,
            "checked_at": self._clock(),
            "error": err,
        }
        self.last_check = res
        return res

    def _start_on(self, sid: str) -> None:
        cfg = self._bridge(["config", sid, str(self.s.socks_port), str(self.s.http_port)])
        json.loads(cfg)  # validate before handing it to xray
        self._kill()
        fd, name = tempfile.mkstemp(prefix="openswarm-vpn-", suffix=".json")
        with os.fdopen(fd, "w") as f:  # contains the WireGuard private key: 0600 via mkstemp, deleted on stop
            f.write(cfg)
        self._cfg_file = Path(name)
        if _port_open(self.s.http_port):
            raise VpnError(f"port {self.s.http_port} is already in use (set vpn.http_port)")
        self._proc = subprocess.Popen([self._xray(), "run", "-c", name], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        deadline = self._clock() + 10
        while self._clock() < deadline and not _port_open(self.s.http_port):
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or b"").decode(errors="replace").strip().splitlines()[-1:] if self._proc.stderr else []
                raise VpnError("xray exited: " + (err[0] if err else f"code {self._proc.returncode}"))
            time.sleep(0.1)
        if not _port_open(self.s.http_port):
            raise VpnError("xray did not open its port")

        settle = getattr(self.s, "settle_delay_seconds", 0.8)
        if settle > 0 and not self._bridge_fn:
            time.sleep(settle)

        if self.s.health_target:
            check_res = self.verify(target=self.s.health_target)
            if getattr(self.s, "verify_on_rotate", True) and not check_res.get("ok"):
                err_msg = check_res.get("error") or f"tunnel via {sid} does not pass traffic"
                raise VpnError(err_msg)

        self.current = sid

    def running(self) -> bool:
        if self._proc is not None:
            if self._proc.poll() is None and self.current:
                return True
            self._proc = None
        if not self._bridge_fn and self.enabled and _port_open(self.s.http_port, 0.15):
            if not self.current:
                st = self._load()
                self.current = str(st.get("current") or self.preferred)
            return bool(self.current)
        return False

    def _bring_up(self, avoid: set[str], tries: int = 6) -> bool:
        for _ in range(tries):
            sid = self._pick(avoid)
            if not sid:
                self.last_error = "no VPN server available (all cooling down)"
                return False
            try:
                self._start_on(sid)
                self.last_error = ""
                self._save()
                return True
            except VpnError as exc:
                self.last_error = str(exc)
                self.cooldown[sid] = self._clock() + min(300, self.s.cooldown_seconds)  # broken now; retry later
                avoid.add(sid)
        self._kill()
        self.current = ""
        return False

    # ------------------------------------------------------------------ public API (blocking; call via to_thread)
    def ensure(self) -> bool:
        """Start the tunnel if enabled and not running. Prefers the last used server."""
        with self._lock:
            if not self.enabled:
                return False
            if self.running():
                return True
            if self.preferred and self.preferred in self.candidates() and self._ready(self.preferred):
                try:
                    self._start_on(self.preferred)
                    self._save()
                    return True
                except VpnError as exc:
                    self.last_error = str(exc)
            return self._bring_up({self.preferred} if self.preferred else set())

    def rotate(self, reason: str = "manual", limited: bool = True, from_server: str = "") -> bool:
        """Swap the exit server. Concurrent agents that all hit the limit on `from_server` cause a single swap."""
        with self._lock:
            if not self.enabled:
                return False
            if limited and self.running() and from_server and self.current and self.current != from_server:
                return True  # another concurrent agent already rotated away from `from_server`
            old = self.current
            if old and limited:
                self.cooldown[old] = self._clock() + self.s.cooldown_seconds
            reset_actions = self._reset_stale_state()
            ok = self._bring_up({old} if old else set())
            self.last_rotate = self._clock()
            if ok:
                self.rotations += 1
                if getattr(self.s, "verify_on_rotate", True):
                    self.verify()
                rec = {
                    "t": self.last_rotate,
                    "from": old,
                    "to": self.current,
                    "reason": reason[:160],
                    "verification": getattr(self, "last_check", {}),
                    "reset_actions": reset_actions,
                }
                self.history = (self.history + [rec])[-30:]
            return ok

    def use(self, sid: str) -> bool:
        with self._lock:
            if sid not in [x["id"] for x in self.servers()]:
                raise VpnError(f"unknown server: {sid}")
            self.cooldown.pop(sid, None)
            self.enabled = True
            self._reset_stale_state()
            try:
                self._start_on(sid)
            except VpnError as exc:
                self.last_error = str(exc)
                return False
            self.last_error, self.preferred = "", sid
            if getattr(self.s, "verify_on_rotate", True):
                self.verify()
            self._save()
            return True

    def set_enabled(self, on: bool) -> bool:
        with self._lock:
            self.enabled = on
            self._save()
            if not on:
                self.stop()
                return True
            if not self._bridge_fn:
                _reap_stale_opencode_service()
            return self.ensure()

    def set_auto_switch(self, on: bool) -> bool:
        with self._lock:
            self.auto_switch_on_limits = on
            self._save()
            return self.auto_switch_on_limits

    def stop(self) -> None:
        with self._lock:
            if self.current:
                self.preferred = self.current
            self._kill()
            self.current = ""

    def is_limit(self, text: str) -> bool:
        return bool(self.s.rotate_pattern and re.search(self.s.rotate_pattern, text or "", re.I))

    def env(self) -> dict[str, str]:
        """Proxy env for opencode children. Empty when the tunnel is not up (agents then go direct... unless enabled
        but broken: then a dead proxy is better than silently leaking - see `env_for_run`)."""
        if not self.enabled:
            return {}
        http = f"http://127.0.0.1:{self.s.http_port}"
        return {"HTTP_PROXY": http, "HTTPS_PROXY": http, "http_proxy": http, "https_proxy": http,
                "ALL_PROXY": f"socks5h://127.0.0.1:{self.s.socks_port}", "all_proxy": f"socks5h://127.0.0.1:{self.s.socks_port}",
                "NO_PROXY": self.s.no_proxy, "no_proxy": self.s.no_proxy}

    def status(self, with_servers: bool = False) -> dict[str, Any]:
        now = self._clock()
        d: dict[str, Any] = {"enabled": self.enabled, "auto_switch_on_limits": self.auto_switch_on_limits,
                             "running": self.running(), "current": self.current,
                             "http_port": self.s.http_port, "socks_port": self.s.socks_port, "rotations": self.rotations,
                             "error": self.last_error, "history": self.history[-10:],
                             "cooling": {k: round(v - now) for k, v in self.cooldown.items() if v > now},
                             "last_check": getattr(self, "last_check", {}),
                             "settle_delay_seconds": getattr(self.s, "settle_delay_seconds", 0.8)}
        cur = next((x for x in (self._servers or []) if x["id"] == self.current), None)
        d["current_label"] = (f"{cur.get('country') or cur.get('country_code') or ''} · {cur['id']}".strip(" ·") if cur else self.current)
        if with_servers:
            try:
                d["servers"] = [{**x, "cooling": max(0, round(self.cooldown.get(x["id"], 0) - now))} for x in self.servers()]
                d["candidates"] = len(self.candidates())
            except VpnError as exc:
                d["servers"], d["error"] = [], str(exc)
        return d


_MGR: tuple[tuple, VpnManager] | None = None


def get_vpn(cfg: SwarmConfig) -> VpnManager:
    global _MGR
    s = cfg.vpn
    sig = (s.provider, s.ninjavpn_dir, s.python, s.xray_path, s.http_port, s.socks_port)
    if _MGR is None or _MGR[0] != sig:
        if _MGR is not None:
            _MGR[1].stop()
        _MGR = (sig, VpnManager(s))
    else:
        _MGR[1].s = s
    return _MGR[1]


def reset_vpn() -> None:
    global _MGR
    if _MGR is not None:
        _MGR[1].stop()
    _MGR = None


def vpn_env(cfg: SwarmConfig) -> dict[str, str]:
    """Env for an opencode child: proxy vars when the per-app VPN is on, nothing otherwise."""
    if _MGR is None and not cfg.vpn.enabled and not vpn_state_path().exists():
        return {}
    return get_vpn(cfg).env()


# ---------------------------------------------------------------------- CLI: `openswarm vpn ...`
def vpn_main(cfg: SwarmConfig, argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="openswarm vpn", description="Per-app VPN for OpenSwarm agents (NinjaVPN + private xray)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("list", help="servers and their cooldowns")
    sub.add_parser("on", help="enable for OpenSwarm agents (remembered)")
    sub.add_parser("off")
    sub.add_parser("rotate", help="switch to the next server now")
    auto_p = sub.add_parser("auto", help="turn automatic switching on limits on or off")
    auto_p.add_argument("state", choices=["on", "off"])
    u = sub.add_parser("use", help="switch to a specific server")
    u.add_argument("id")
    sub.add_parser("verify", help="run active health, latency and exit IP check on the current tunnel")
    sub.add_parser("test", help="bring the tunnel up, check it and print the exit IP")
    sub.add_parser("reset", help="clear all cooldowns")
    a = p.parse_args(argv or ["status"])
    m = get_vpn(cfg)
    try:
        if a.cmd in (None, "status"):
            print(json.dumps(m.status(), indent=1))
        elif a.cmd == "list":
            st = m.status(with_servers=True)
            for x in st["servers"]:
                mark = "*" if x["id"] == m.current else " "
                cool = f"  cooling {x['cooling']}s" if x["cooling"] else ""
                print(f"{mark} {x['id']:<28} {x.get('country') or x.get('country_code') or '':<18} {x['protocol']}{cool}")
            print(f"{st.get('candidates', 0)} usable for rotation")
        elif a.cmd == "on":
            ok = m.set_enabled(True)
            print(f"VPN on via {m.current}" if ok else f"enabled, but not up: {m.last_error}")
            return 0 if ok else 1
        elif a.cmd == "off":
            m.set_enabled(False)
            print("VPN off: agents connect directly")
        elif a.cmd == "rotate":
            m.enabled = True
            ok = m.rotate("manual (cli)", limited=False)
            if ok:
                chk = getattr(m, "last_check", {})
                lat = f", latency {chk['latency_ms']}ms" if chk.get("latency_ms") is not None else ""
                ip = f", exit IP {chk['exit_ip']}" if chk.get("exit_ip") else ""
                print(f"now via {m.current} (verified{lat}{ip})")
            else:
                print(f"rotation failed: {m.last_error}")
            return 0 if ok else 1
        elif a.cmd == "verify":
            if not m.running():
                print("VPN is not running (enable via `openswarm vpn on`)")
                return 1
            res = m.verify()
            print(json.dumps(res, indent=1))
            return 0 if res.get("ok") else 1
        elif a.cmd == "auto":
            turn_on = a.state == "on"
            m.set_auto_switch(turn_on)
            print(f"VPN auto-swap on limits: {'on' if turn_on else 'off'}")
        elif a.cmd == "use":
            ok = m.use(a.id)
            print(f"now via {m.current}" if ok else f"failed: {m.last_error}")
            return 0 if ok else 1
        elif a.cmd == "reset":
            m.cooldown.clear()
            m._save()
            print("cooldowns cleared")
        elif a.cmd == "test":
            was_enabled = m.enabled
            m.enabled = True
            if not m.ensure():
                print(f"not up: {m.last_error}")
                return 1
            ip = subprocess.run(["curl", "-s", "--max-time", "15", "-x", f"http://127.0.0.1:{m.s.http_port}", "https://api.ipify.org"],
                                capture_output=True, text=True).stdout.strip() if shutil.which("curl") else "?"
            print(f"OK via {m.current}; exit IP {ip or '?'} (HTTP proxy 127.0.0.1:{m.s.http_port})")
            if not was_enabled:
                m.stop()
    except VpnError as exc:
        print(f"error: {exc}")
        return 1
    return 0
