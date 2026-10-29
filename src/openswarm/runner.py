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


_FATAL_ERR_RE = re.compile(
    r"(?:^|\b)(?:ERROR|Error:|Traceback \(most recent call last\)|Exception:|Fatal|Panic|"
    r"APIError|ProviderError|ModelNotFoundError|ProviderModelNotFoundError|FreeUsageLimitError|"
    r"SyntaxError|ReferenceError|TypeError|unrecognized flag|unknown flag|unknown option|"
    r"Variant unavailable|ECONNREFUSED|fetch failed|unable to connect|boom)\b",
    re.I,
)
_TURN_HEADER_RE = re.compile(r"^>\s+\S+\s+·\s+\S+", re.M)
_GIT_COMMIT_RE = re.compile(r"\[[\w/.-]+\s+[0-9a-f]{7,40}\]|\b\d+\s+files?\s+changed\b")


def is_soft_success(output: str, errors: str, returncode: int, cwd: Path | None = None) -> tuple[bool, str]:
    """Detect when `opencode run` exited with status 1 after already delivering a valid final response.

    Some providers/models or post-turn hooks in `opencode` exit with code 1 on stream teardown
    even though the agent finished its tool calls, committed files, and printed its final answer.
    """
    if returncode != 1:
        return False, ""
    clean_err = strip_ansi(errors or "").strip()
    clean_out = strip_ansi(output or "").strip()
    if not clean_out:
        return False, ""

    # 1. Any explicit error in stderr disqualifies soft-success recovery
    if clean_err and _FATAL_ERR_RE.search(clean_err):
        return False, ""

    # 2. Check known healer error patterns on stderr + tail of stdout
    tail_lines = [ln.strip() for ln in clean_out.splitlines() if ln.strip()][-30:]
    tail_text = clean_err + "\n" + "\n".join(tail_lines)
    from .healer import ERROR_PATTERNS
    for _, pat, _, _ in ERROR_PATTERNS:
        if re.search(pat, tail_text, re.I):
            return False, ""

    # 3. Check if the last few lines of stdout look like a crash/error
    last_few = "\n".join(tail_lines[-6:])
    if _FATAL_ERR_RE.search(last_few):
        return False, ""

    # 4. Extract text from the final assistant turn (after the last `> agent · model` header if present)
    turns = _TURN_HEADER_RE.split(clean_out)
    final_turn = turns[-1].strip() if turns else clean_out
    body_lines: list[str] = []
    for ln in final_turn.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("Thinking:") or s.startswith("$ "):
            body_lines.clear()  # only count substantive text produced AFTER the last thinking/command step
            continue
        body_lines.append(s)
    final_body = "\n".join(body_lines).strip()

    has_commit = bool(_GIT_COMMIT_RE.search(clean_out))
    has_turn_header = bool(_TURN_HEADER_RE.search(clean_out))

    if has_commit and len(final_body) >= 20:
        return True, "git commit + final summary"
    if has_turn_header and len(final_body) >= 30:
        return True, "final assistant response"
    if not clean_err and len(final_body) >= 100:
        return True, "complete response output"
    return False, ""


@dataclass
class RunResult:
    returncode: int
    output: str = ""
    errors: str = ""
    command: list[str] = field(default_factory=list)
    session_id: str = ""

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
        attach_flag: str = "",
        standalone: bool = False,
        auto: bool = False,
        effort: str = "",
        thinking: bool = True,
        variant_flag: str | None = None,
        thinking_flag: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.cwd = cwd
        self.agent_id = agent_id
        self.role = role
        from .dsh import resolve_dsh_model
        from .kilo import normalize_kilo_model
        self.model = normalize_kilo_model(resolve_dsh_model(model))
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
        self.attach_flag = attach_flag
        self.standalone = standalone
        self.auto = auto
        self.effort = effort or getattr(cfg, "effort", "")
        self.thinking = thinking
        if variant_flag is None or thinking_flag is None:
            from .capabilities import probe
            caps = probe(cfg)
            if variant_flag is None:
                variant_flag = caps.variant_flag(cfg.opencode)
            if thinking_flag is None:
                thinking_flag = caps.thinking_flag(cfg.opencode)
        self.variant_flag = variant_flag
        self.thinking_flag = thinking_flag
        self.extracted_session_id = session_id

    # ---------------------------------------------------------------- build
    def build_command(self, message: str) -> list[str]:
        from .capabilities import model_supports_variant

        oc = self.cfg.opencode
        cmd = [shutil.which(oc.binary) or oc.binary, *oc.subcommand]
        model_str = self.model
        can_variant = model_supports_variant(model_str)
        if can_variant and self.effort and self.effort != "default":
            if self.variant_flag and "#" not in model_str:
                cmd += [self.variant_flag, self.effort]
            elif model_str and "#" not in model_str:
                model_str = f"{model_str}#{self.effort}"
        if model_str:
            cmd += [oc.model_flag, model_str]
        if self.agent and oc.agent_flag:
            cmd += [oc.agent_flag, self.agent]
        if self.server_url and (self.attach_flag or oc.attach_flag):
            cmd += [self.attach_flag or oc.attach_flag, self.server_url]
        elif self.standalone and oc.standalone_flag:
            cmd.append(oc.standalone_flag)
        if self.auto and oc.auto_flag:
            cmd.append(oc.auto_flag)
        if self.thinking and self.thinking_flag:
            cmd.append(self.thinking_flag)
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
        from .keys import CONFIG_ENV, inline_model_config  # lazy: keys imports config

        inline = inline_model_config(self.cfg, self.model, env.get(CONFIG_ENV, ""))
        if inline:  # openrouter/* model that opencode's bundled catalogue may not list yet
            env[CONFIG_ENV] = inline
        env["OPENSWARM_AGENT"] = self.agent_id
        env["OPENSWARM_ROLE"] = self.role
        env.setdefault("OPENSWARM_DB", str(self.cwd / ".swarm" / "swarm.db"))
        from .vpn import vpn_env
        env.update(vpn_env(self.cfg))
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

        sess_pat = re.compile(self.cfg.opencode.session_id_pattern) if self.cfg.opencode.session_id_pattern else None

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
                if sess_pat and not self.extracted_session_id:
                    m = sess_pat.search(line)
                    if m:
                        self.extracted_session_id = m.group(1) if m.groups() else m.group(0)
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
            return RunResult(124, "\n".join(out_lines), "\n".join(err_lines), cmd[:-1], session_id=self.extracted_session_id)
        except asyncio.CancelledError:
            await self._kill(proc)
            raise

        rc = proc.returncode if proc.returncode is not None else 1
        joined_out = "\n".join(out_lines)
        joined_err = "\n".join(err_lines)
        soft_ok, soft_reason = is_soft_success(joined_out, joined_err, rc)
        if soft_ok:
            self._emit(f"✓ [exit {rc} -> 0: ответ принят ({soft_reason})]", False)
            rc = 0
        else:
            self._emit(f"[exit {rc}]", rc != 0)
        return RunResult(rc, joined_out, joined_err, cmd[:-1], session_id=self.extracted_session_id)

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
