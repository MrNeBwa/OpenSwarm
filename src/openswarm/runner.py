"""Async wrapper around the `opencode` CLI with line-by-line output streaming."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import SkillSpec, SwarmConfig
from .safety import SandboxError, maybe_sandbox

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
MAX_ARG_BYTES = 100_000  # Linux caps a single argv entry at 128 KiB

LineCallback = Callable[[str, str, bool], None]  # (agent_id, line, is_stderr)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


@dataclass
class RunResult:
    returncode: int
    output: str = ""
    errors: str = ""
    command: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def clean_output(self) -> str:
        return strip_ansi(self.output)


class OpenCodeRunner:
    """Runs one `opencode run` invocation inside `cwd`.

    Workers inherit the user's global opencode config (~/.config/opencode) automatically because we
    keep the real environment, and the project-level `.opencode/`, `opencode.json`, MCP and rules
    files are injected into each worktree by GitManager.
    """

    def __init__(
        self,
        cfg: SwarmConfig,
        cwd: Path,
        *,
        agent_id: str,
        role: str,
        model: str,
        skills: list[SkillSpec] | None = None,
        on_line: LineCallback | None = None,
        continue_session: bool = False,
        session_id: str = "",
        title: str = "",
        server_url: str = "",
        agent: str = "",
        files: list[str] | None = None,
        sandbox: bool | None = None,
        extra_rw: list[Path] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.cwd = cwd
        self.agent_id = agent_id
        self.role = role
        self.model = model
        self.skills = skills or []
        self.on_line = on_line
        self.continue_session = continue_session
        self.session_id = session_id
        self.title = title
        self.server_url = server_url
        self.agent = agent
        self.files = files or []
        self.sandbox = sandbox
        self.extra_rw = extra_rw or []
        self.extra_env = extra_env or {}

    # ---------------------------------------------------------------- build
    def build_command(self, message: str) -> list[str]:
        oc = self.cfg.opencode
        cmd = [shutil.which(oc.binary) or oc.binary, *oc.subcommand]
        if self.model:
            cmd += [oc.model_flag, self.model]
        if self.agent and oc.agent_flag:
            cmd += [oc.agent_flag, self.agent]
        if self.server_url and oc.attach_flag:
            cmd += [oc.attach_flag, self.server_url]
        if self.session_id and oc.session_flag:
            cmd += [oc.session_flag, self.session_id]
        elif self.continue_session and oc.continue_flag:
            cmd.append(oc.continue_flag)
        elif self.title and oc.title_flag:  # new session: tag it so we can find its id afterwards
            cmd += [oc.title_flag, self.title]
        cmd += oc.extra_args
        for skill in self.skills:
            cmd += skill.args
        raw = message.encode("utf-8")
        if len(raw) > MAX_ARG_BYTES:
            message = raw[:MAX_ARG_BYTES].decode("utf-8", errors="ignore") + "\n...[prompt truncated]..."
        for f in self.files:
            cmd += [oc.file_flag, str(f)]
        if self.files:
            cmd.append("--")  # --file is an array option: stop it from swallowing the message
        cmd.append(message)
        return cmd

    def build_env(self) -> dict[str, str]:
        oc = self.cfg.opencode
        env = {**os.environ, **oc.env}
        for skill in self.skills:
            env.update(skill.env)
        role_perm = oc.role_permissions.get(self.role)
        if role_perm:  # role restrictions (e.g. planner = no edits) beat the auto-approve toggle
            env[oc.permission_env] = role_perm
        env.update(self.extra_env)
        env["OPENSWARM_AGENT"] = self.agent_id
        env["OPENSWARM_ROLE"] = self.role
        return env

    # ------------------------------------------------------------------ run
    def _emit(self, line: str, is_err: bool) -> None:
        if self.on_line:
            self.on_line(self.agent_id, line, is_err)

    async def run(self, message: str) -> RunResult:
        cmd = self.build_command(message)
        try:
            cmd = maybe_sandbox(cmd, self.cwd, self.cfg, self.sandbox, self.extra_rw)
        except SandboxError as exc:
            self._emit(str(exc), True)
            return RunResult(126, errors=str(exc))
        shown = " ".join(cmd[:-1]) + " <prompt>"
        self._emit(f"$ {shown}   (cwd: {self.cwd})", False)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.cwd),
                env=self.build_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
                start_new_session=True,  # own process group so we can kill tool subprocesses too
            )
        except (FileNotFoundError, PermissionError) as exc:
            self._emit(f"cannot start '{cmd[0]}': {exc} - is opencode installed and on PATH?", True)
            return RunResult(127, errors=str(exc), command=cmd[:-1])

        out_lines: list[str] = []
        err_lines: list[str] = []

        async def pump(stream: asyncio.StreamReader, sink: list[str], is_err: bool) -> None:
            while True:
                try:
                    raw = await stream.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    raw = await stream.read(65536)
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                sink.append(line)
                self._emit(line, is_err)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    pump(proc.stdout, out_lines, False),  # type: ignore[arg-type]
                    pump(proc.stderr, err_lines, True),  # type: ignore[arg-type]
                    proc.wait(),
                ),
                timeout=self.cfg.opencode.timeout_seconds or None,
            )
        except asyncio.TimeoutError:
            await self._kill(proc)
            self._emit(f"timed out after {self.cfg.opencode.timeout_seconds}s", True)
            return RunResult(124, "\n".join(out_lines), "\n".join(err_lines), cmd[:-1])
        except asyncio.CancelledError:
            await self._kill(proc)
            raise

        rc = proc.returncode if proc.returncode is not None else 1
        self._emit(f"[exit {rc}]", rc != 0)
        return RunResult(rc, "\n".join(out_lines), "\n".join(err_lines), cmd[:-1])

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
                return
            except asyncio.TimeoutError:
                continue


async def find_session_id(cfg: SwarmConfig, cwd: Path, title: str) -> str:
    """Look up the id of the session we created with `--title <title>` via `opencode session list --format json`."""
    import json

    oc = cfg.opencode
    try:
        proc = await asyncio.create_subprocess_exec(
            shutil.which(oc.binary) or oc.binary, "session", "list", "--format", "json", "-n", "50",
            cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (OSError, asyncio.TimeoutError):
        return ""
    try:
        data = json.loads(out.decode(errors="replace"))
    except ValueError:
        return ""
    items = data.get("sessions", []) if isinstance(data, dict) else data
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("title") == title:
            return str(it.get("id") or it.get("sessionID") or "")
    return ""
