"""Warm `opencode serve` instances so chat turns can `opencode run --attach` (no MCP cold boot)."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import secrets
import socket
from pathlib import Path

from .config import SwarmConfig
from .safety import maybe_sandbox


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerPool:
    """One server per (directory, permission profile). The server's cwd *is* the chat directory, so an
    attached run can never touch another directory. Any failure just returns None (caller runs plain)."""

    def __init__(self, cfg: SwarmConfig) -> None:
        self.cfg = cfg
        self._procs: dict[tuple, tuple[asyncio.subprocess.Process, str, str]] = {}  # key -> (proc, url, password)
        self._failed: set[tuple] = set()
        self._locks: dict[tuple, asyncio.Lock] = {}
        self.api_bad = False  # set when the HTTP API proved unusable; callers then use `opencode run`

    def _key(self, cwd: Path, env: dict[str, str], sandbox: bool = False) -> tuple:
        return str(cwd), env.get(self.cfg.opencode.permission_env, ""), sandbox

    async def url_for(self, cwd: Path, env: dict[str, str], sandbox: bool = False) -> str | None:
        ep = await self.endpoint(cwd, env, sandbox)
        return ep[0] if ep else None

    async def endpoint(self, cwd: Path, env: dict[str, str], sandbox: bool = False) -> tuple[str, str] | None:
        """(url, password) of a warm server for this directory/permission profile. Password-protected."""
        oc = self.cfg.opencode
        if not oc.use_server:
            return None
        key = self._key(cwd, env, sandbox)
        if key in self._failed:
            return None
        async with self._locks.setdefault(key, asyncio.Lock()):
            cur = self._procs.get(key)
            if cur and cur[0].returncode is None:
                return cur[1], cur[2]
            port = _free_port()
            binary = shutil.which(oc.binary) or oc.binary
            password = secrets.token_urlsafe(24)  # other local processes / web pages cannot drive the server
            srv_env = {**env, "OPENCODE_SERVER_PASSWORD": password}
            try:
                cmd = maybe_sandbox([binary, *oc.serve_command, "--port", str(port), "--hostname", "127.0.0.1"], cwd, self.cfg, sandbox)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(cwd), env=srv_env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, RuntimeError):
                self._failed.add(key)
                return None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + oc.server_start_timeout
            while loop.time() < deadline and proc.returncode is None:
                try:
                    _, w = await asyncio.open_connection("127.0.0.1", port)
                    w.close()
                    url = f"http://127.0.0.1:{port}"
                    self._procs[key] = (proc, url, password)
                    return url, password
                except OSError:
                    await asyncio.sleep(0.25)
            await self._kill(proc)
            self._failed.add(key)
            return None

    def mark_bad(self, cwd: Path, env: dict[str, str], sandbox: bool = False) -> None:
        """An attached run failed to connect: stop using (and kill) this server."""
        key = self._key(cwd, env, sandbox)
        self._failed.add(key)
        cur = self._procs.pop(key, None)
        if cur:
            asyncio.ensure_future(self._kill(cur[0]))

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    async def close(self) -> None:
        for proc, _, _ in list(self._procs.values()):
            await self._kill(proc)
        self._procs.clear()
