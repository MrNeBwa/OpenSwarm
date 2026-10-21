"""Storm (parallel DAG) and Cowork (architect -> coder -> reviewer loop) pipelines."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from .broker import Broker
from .config import SwarmConfig
from .apiclient import ApiUnavailable, Http, run_chat_api
from .docs import expand_attachments, extract_images
from .safety import SandboxError, audit, create_snapshot
from .git_manager import GitError, GitManager, Worktree
from .runner import OpenCodeRunner, RunResult, find_session_id, strip_ansi
from .servers import ServerPool

IDLE, RUNNING, MERGING, DONE, FAILED = "Idle", "Running", "Merging", "Done", "Failed"


async def _emit(fn: Callable[..., Any] | None, *args: Any) -> None:
    if fn is None:
        return
    res = fn(*args)
    if inspect.isawaitable(res):
        await res


@dataclass
class Hooks:
    """UI callbacks; each may be sync or async."""

    on_agent: Callable[[str, str, str], Any] | None = None  # agent_id, title, role
    on_status: Callable[[str, str], Any] | None = None  # agent_id, status
    on_line: Callable[[str, str, bool], Any] | None = None  # agent_id, line, is_stderr
    on_log: Callable[[str], Any] | None = None  # orchestrator-level message
    on_result: Callable[[str, str, str], Any] | None = None  # agent_id, final text, kind
    on_event: Callable[[str, str, dict], Any] | None = None  # agent_id, kind (tool|usage|permission|run), payload
    ask_permission: Callable[[str, dict], Any] | None = None  # agent_id, request -> 'once'|'always'|'reject'


@dataclass
class Selection:
    mode: str = "storm"
    planner_model: str = ""
    coder_model: str = ""
    reviewer_model: str = ""
    skills: list[str] = field(default_factory=list)
    chat_id: str = "chat"  # stable id => one tab / one opencode session per conversation
    continue_session: bool = False
    session_id: str = ""  # known opencode session to resume (parallel chats)
    workdir: Path | None = None  # chat directory (each chat tab has its own)
    chat_title: str = "Chat"
    sandbox: bool | None = None  # None = security.sandbox from config
    candidate_models: list[str] = field(default_factory=list)  # best-of-N: models to rotate through


@dataclass
class RunSummary:
    run_id: str
    mode: str
    merged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    session_id: str = ""  # session id detected in a chat's first turn
    result: str = ""  # final text (chat reply / research report / article / review)
    snapshot: str = ""  # id of the pre-run snapshot (non-git dirs) -> enables Undo
    usage: dict = field(default_factory=dict)  # {'cost':..., 'tokens': {...}} when the API reports it

    @property
    def ok(self) -> bool:
        return bool(self.merged) and not self.failed


class BudgetExceeded(RuntimeError):
    pass


READONLY_MODES = ("research", "write", "review")


def suggest_mode(prompt: str, is_git: bool) -> tuple[str, str]:
    """Cheap heuristic router for Auto mode (no LLM call). Returns (mode, reason)."""
    p = prompt.lower()
    if re.search(r"\b(code review|security review|audit|review (the|my|this) (code|diff|pr|changes))\b|ревью|проверь код|аудит", p):
        return "review", "asks for a review"
    if re.search(r"(write|draft|compose)\s+(an?\s+|the\s+)?(\w+\s+)?(article|blog|post|essay|report|story|newsletter|whitepaper)|напиши\s+(\w+\s+)?(статью|пост|эссе|отчёт|отчет|рассказ|текст)", p):
        return "write", "asks for long-form writing"
    if re.search(r"\b(research|investigate|compare|survey|state of the art|market|literature)\b|исследуй|изучи|сравни|обзор|найди информацию", p):
        return "research", "asks for research"
    steps = len(re.findall(r"(^|\n)\s*(\d+[.)]|[-*])\s", prompt)) + len(re.findall(r"\b(and also|then|plus)\b|, а также|\bзатем\b", p))
    big = re.search(r"\b(refactor|migrate|rewrite|implement|build)\b|реализуй|перепиши|рефактор|мигрируй", p)
    if is_git and (steps >= 3 or (big and len(prompt) > 300)):
        return "storm", "large multi-part change in a git repo"
    return "chat", "default"


class Subtask(BaseModel):
    id: str = ""
    title: str = ""
    prompt: str
    recommended_skills: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- helpers
def slugify(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30].strip("-")
    return s or fallback


def _escape_raw_controls(text: str) -> str:
    """Escape literal control characters inside JSON strings.

    LLMs frequently emit multi-line prompt strings with real newlines instead of ``\\n``; Python's
    json module rejects that. Escaping only characters inside quotes keeps the rest of the document
    (including pretty-printed whitespace) untouched.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                out.append(ch)
                in_string = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_string = True
    return "".join(out)


def extract_subtasks(text: str) -> list[Subtask]:
    """Find the first JSON array (or {"subtasks": [...]}) of task objects in noisy LLM output."""
    text = strip_ansi(text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        chunk = text[m.start():]
        candidates = (chunk, _escape_raw_controls(chunk))
        for cand in candidates:
            try:
                obj, _ = dec.raw_decode(cand)
            except ValueError:
                continue
            if isinstance(obj, dict):
                obj = obj.get("subtasks") or obj.get("tasks")
            if isinstance(obj, list) and obj and all(isinstance(x, dict) and "prompt" in x for x in obj):
                try:
                    return [Subtask.model_validate(x) for x in obj]
                except ValidationError:
                    continue
    return []


def normalize_plan(run_id: str, tasks: list[Subtask]) -> list[Subtask]:
    """Make ids unique + prefixed with the run id, drop bad deps, break cycles."""
    mapping: dict[str, str] = {}
    for i, t in enumerate(tasks, 1):
        slug = slugify(t.id or t.title, f"t{i}")
        full, n = f"{run_id}-{slug}", 2
        while full in mapping.values():
            full, n = f"{run_id}-{slug}-{n}", n + 1
        mapping[t.id or t.title or f"t{i}"] = full
        t.id = full
        t.title = t.title or slug
    valid = set(mapping.values())
    for t in tasks:
        t.depends_on = [mapping.get(d, d) for d in t.depends_on]
        t.depends_on = [d for d in t.depends_on if d in valid and d != t.id]
    # Kahn's algorithm; on a cycle fall back to fully parallel
    indeg = {t.id: len(t.depends_on) for t in tasks}
    ready = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        cur = ready.pop()
        seen += 1
        for t in tasks:
            if cur in t.depends_on:
                indeg[t.id] -= 1
                if indeg[t.id] == 0:
                    ready.append(t.id)
    if seen != len(tasks):
        for t in tasks:
            t.depends_on = []
    return tasks


def parse_verdict(text: str) -> bool | None:
    found = re.findall(r"VERDICT:\s*(APPROVED|CHANGES_REQUESTED)", strip_ansi(text), flags=re.I)
    if not found:
        return None
    return found[-1].upper() == "APPROVED"


# ---------------------------------------------------------------- orchestrator
class Orchestrator:
    def __init__(
        self,
        cfg: SwarmConfig,
        git: GitManager,
        broker: Broker,
        selection: Selection,
        hooks: Hooks | None = None,
        pool: ServerPool | None = None,
    ) -> None:
        self.pool = pool
        self.cfg = cfg
        self.git = git
        self.broker = broker
        self.sel = selection
        self.hooks = hooks or Hooks()
        self.skills = cfg.skills_by_id(selection.skills)
        self._announced: set[str] = set()
        self._status: dict[str, str] = {}
        self._agent_count = 0
        self._images: list[Path] = []
        self._api_session = ""
        self._usage: dict[str, dict] = {}

    # ------------------------------------------------------------- UI plumbing
    async def _log(self, msg: str) -> None:
        await _emit(self.hooks.on_log, msg)

    async def _announce(self, agent_id: str, title: str, role: str, status: str = IDLE) -> None:
        if agent_id not in self._announced:
            self._announced.add(agent_id)
            await _emit(self.hooks.on_agent, agent_id, title, role)
        await self._set_status(agent_id, status)

    async def _set_status(self, agent_id: str, status: str) -> None:
        self._status[agent_id] = status
        await _emit(self.hooks.on_status, agent_id, status)

    def _line_cb(self, agent_id: str, line: str, is_err: bool) -> None:
        # Runner callbacks are sync; schedule the (possibly async) hook without blocking the stream.
        res = self.hooks.on_line(agent_id, line, is_err) if self.hooks.on_line else None
        if inspect.isawaitable(res):
            asyncio.ensure_future(res)

    def _model_for(self, role: str) -> str:
        if role.startswith("wf"):  # workflow step roles: wfro_planner / wfrw_coder ...
            slot = role.rsplit("_", 1)[-1]
            return {"planner": self.sel.planner_model, "reviewer": self.sel.reviewer_model}.get(slot, self.sel.coder_model) or getattr(self.cfg.roles, slot, self.cfg.roles.coder)
        if role in ("planner", "architect", "research_planner", "outliner", "synthesizer", "editor", "judge"):
            base = "planner" if role not in ("architect",) else "architect"
            return self.sel.planner_model or getattr(self.cfg.roles, base)
        if role in ("reviewer", "lens"):
            return self.sel.reviewer_model or self.cfg.roles.reviewer
        return self.sel.coder_model or self.cfg.roles.coder

    def _compose(self, role: str, body: str, extra_hints: list[str] | None = None, override: str = "") -> str:
        if role == "chat":  # plain conversation: leave opencode's own system prompt in charge
            hints = [s.prompt_hint for s in self.skills if s.prompt_hint]
            return body + ("\n\n[OpenSwarm guidance: " + " ".join(hints) + "]" if hints else "")
        hints = [s.prompt_hint for s in self.skills if s.prompt_hint]
        if extra_hints:
            hints.append("Planner-recommended skills/tools for this task: " + ", ".join(extra_hints))
        hint_block = ("\n## Tooling guidance\n" + "\n".join(f"- {h}" for h in hints) + "\n") if hints else ""
        head = override or getattr(self.cfg.prompts, role, "")
        return f"{head}\n{hint_block}\n## Task\n{body}" if head else f"{hint_block}\n{body}".strip()

    def _sandbox_on(self) -> bool:
        return self.sel.sandbox if self.sel.sandbox is not None else self.cfg.security.sandbox == "bwrap"

    async def _ask_permission(self, agent_id: str, req: dict) -> str:
        sec = self.cfg.security
        await _emit(self.hooks.on_event, agent_id, "permission", req)
        decision = sec.permission_default
        if self.hooks.ask_permission:
            try:
                res = self.hooks.ask_permission(agent_id, req)
                decision = await asyncio.wait_for(res, sec.permission_timeout_s) if inspect.isawaitable(res) else res
            except asyncio.TimeoutError:
                await self._log(f"permission request '{req.get('title')}' timed out -> {sec.permission_default}")
        else:
            await self._log(f"permission '{req.get('title')}' answered '{decision}' (no UI to ask; enable auto-approve to allow)")
        decision = decision if decision in ("once", "always", "reject") else "reject"
        audit(self.cfg, "permission", agent=agent_id, tool=req.get("tool"), title=req.get("title"), detail=req.get("detail"), decision=decision)
        await _emit(self.hooks.on_event, agent_id, "permission_resolved", {"id": req.get("id"), "decision": decision})
        return decision

    def _api_event(self, agent_id: str, kind: str, payload: dict) -> None:
        if kind == "usage":
            self._usage[agent_id] = payload
        if self.hooks.on_event:  # call sync hooks immediately so ordering vs. later events is preserved
            res = self.hooks.on_event(agent_id, kind, payload)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)

    def _usage_total(self) -> dict:
        cost = sum(u.get("cost", 0) for u in self._usage.values())
        toks = {"input": 0, "output": 0, "reasoning": 0}
        for u in self._usage.values():
            for k in toks:
                toks[k] += (u.get("tokens") or {}).get(k, 0)
        return {"cost": round(cost, 6), "tokens": toks} if self._usage else {}

    async def _chat_via_api(self, agent_id: str, title: str, message: str, cwd: Path, session_id: str, stitle: str, sandbox: bool) -> RunResult | None:
        """Chat through the opencode HTTP API (permissions, tool cards, usage). None -> caller falls back to `run`."""
        oc = self.cfg.opencode
        probe = OpenCodeRunner(self.cfg, cwd, agent_id=agent_id, role="chat", model=self._model_for("chat"), skills=self.skills)
        ep = await self.pool.endpoint(cwd, probe.build_env(), sandbox)  # type: ignore[union-attr]
        if not ep:
            return None
        files = [{"type": "file", "mime": {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(p.suffix.lower()[1:], "image/jpeg"),
                  "url": p.resolve().as_uri(), "filename": p.name} for p in self._images]
        self._line_cb(agent_id, f"(opencode API {ep[0]})", False)
        try:
            r = await run_chat_api(
                Http(*ep), text=message, title=stitle or title, session_id=session_id, model=self._model_for("chat"),
                agent=oc.role_agents.get("chat", ""), files=files, on_line=lambda t, e: self._line_cb(agent_id, t, e),
                on_event=lambda k, p: self._api_event(agent_id, k, p), on_permission=lambda req: self._ask_permission(agent_id, req),
                timeout=oc.api_timeout_minutes * 60, idle_poll=oc.api_idle_poll_seconds,
            )
        except ApiUnavailable as exc:
            self.pool.api_bad = True  # type: ignore[union-attr]
            await self._log(f"opencode HTTP API unavailable ({str(exc)[:90]}); using `opencode run` instead")
            return None
        self._api_session = r.session_id
        return RunResult(0 if r.ok else 1, output=r.text, errors=r.error, command=["api"])

    async def _agent(
        self,
        agent_id: str,
        title: str,
        role: str,
        body: str,
        cwd: Path,
        *,
        hints: list[str] | None = None,
        final_status: str | None = DONE,
        continue_session: bool = False,
        session_id: str = "",
        session_title: str = "",
        prompt_override: str = "",
        model: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> RunResult:
        self._agent_count += 1
        if self.cfg.max_agents_per_run and self._agent_count > self.cfg.max_agents_per_run:
            raise BudgetExceeded(f"max_agents_per_run ({self.cfg.max_agents_per_run}) reached")
        await self._announce(agent_id, title, role)
        await self._set_status(agent_id, RUNNING)
        oc = self.cfg.opencode
        message = self._compose(role, body, hints, prompt_override)
        sandbox = self._sandbox_on()
        mdl = model or self._model_for(role)
        audit(self.cfg, "agent", agent=agent_id, role=role, model=mdl, cwd=str(cwd), sandbox=sandbox)
        res = RunResult(1)
        done_api = False
        if role == "chat" and oc.use_api and oc.use_server and self.pool and not self.pool.api_bad:
            api_res = await self._chat_via_api(agent_id, title, message, cwd, session_id, session_title, sandbox)
            if api_res is not None:
                res, done_api = api_res, True
        rate_re = re.compile(oc.rate_limit_pattern, re.I) if oc.rate_limit_pattern else None
        use_server = bool(self.pool and role == "chat" and oc.use_server and not done_api)
        extra_rw = [self.git.root / ".git"] if self.git.is_git else []
        files = [str(p) for p in self._images] if role == "chat" else []
        for attempt in range(0 if done_api else oc.retries + 1):
            runner = OpenCodeRunner(
                self.cfg, cwd, agent_id=agent_id, role=role, model=mdl, skills=self.skills,
                on_line=self._line_cb, continue_session=continue_session, session_id=session_id,
                title=session_title, agent=oc.role_agents.get(role, ""), files=files, sandbox=sandbox, extra_rw=extra_rw,
                extra_env=extra_env,
            )
            env = runner.build_env()
            if use_server:
                ep = await self.pool.endpoint(cwd, env, sandbox)  # type: ignore[union-attr]
                if ep:
                    runner.server_url, runner.extra_env = ep[0], {"OPENCODE_SERVER_PASSWORD": ep[1]}
                    self._line_cb(agent_id, f"(warm server {ep[0]})", False)
            res = await runner.run(message)
            if not res.ok and runner.server_url and re.search(r"ECONNREFUSED|connect|fetch failed|unable to connect|401|unauthori", res.errors + res.output, re.I):
                self.pool.mark_bad(cwd, env, sandbox)  # type: ignore[union-attr]
                self._line_cb(agent_id, "warm server unreachable; retrying without it", True)
                runner.server_url, runner.extra_env = "", {}
                res = await runner.run(message)
            if res.ok or not rate_re or attempt >= oc.retries:
                break
            if not rate_re.search((res.errors + "\n" + res.output)[-3000:]):
                break
            delay = oc.retry_backoff_seconds * (3 ** attempt)
            await self._log(f"{title}: rate limited / overloaded - retry {attempt + 1}/{oc.retries} in {delay}s")
            await asyncio.sleep(delay)
        if not res.ok:
            await self._set_status(agent_id, FAILED)
        elif final_status:
            await self._set_status(agent_id, final_status)
        return res

    async def _verify(self, agent_id: str, cwd: Path) -> tuple[bool, str]:
        cmd = self.cfg.verify_command.strip()
        if not cmd:
            return True, ""
        self._line_cb(agent_id, f"$ {cmd}   (verify)", False)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        for ln in text.splitlines():
            self._line_cb(agent_id, ln, proc.returncode != 0)
        return proc.returncode == 0, text[-4000:]

    async def _read_only_agent(self, run_id: str, agent_id: str, title: str, role: str, body: str) -> RunResult:
        """Planner/architect run in the root repo (they need to see current files); guard against edits."""
        before = await self.git.status()
        res = await self._agent(agent_id, title, role, body, self.git.workdir)
        if await self.git.status() != before:
            await self._log(f"WARNING: {role} modified files in the main repo despite being read-only.")
        return res

    # ----------------------------------------------------------------- entry
    async def run(self, prompt: str) -> RunSummary:
        await self.git.ensure_ready()
        cwd = self.sel.workdir or self.git.start
        mode = self.sel.mode
        if mode == "auto":
            mode, why = suggest_mode(prompt, self.git.is_git)
            await self._log(f"Auto mode -> {mode} ({why})")
        if mode == "best" and not self.git.is_git:
            await self._log("Best-of-N needs a git repository (candidates run in separate worktrees); using Chat instead.")
            mode = "chat"
        self.sel.mode = mode
        needs_worktree = mode in ("storm", "cowork", "best")
        if needs_worktree and await self.git.status(untracked=False):
            await self._log(
                "Note: the main repo has uncommitted changes. Worktrees start from HEAD, so workers "
                "will NOT see them (and merges may be refused). Commit or stash first for best results."
            )
        if not self.git.is_git and mode in ("storm", "cowork"):
            await self._log(self.git.note)
        prompt, attached = expand_attachments(prompt, cwd)
        self._images = extract_images(prompt, cwd)
        if attached or self._images:
            await self._log("Attached: " + ", ".join(Path(a).name for a in [*attached, *map(str, self._images)]))
        run_id = self.broker.create_run(mode, prompt)
        await self._log(f"Run {run_id} started in {mode.upper()} mode")
        await _emit(self.hooks.on_event, "run", "run_started", {"run_id": run_id, "db": str(self.broker.path)})
        summary = RunSummary(run_id, mode)
        audit(self.cfg, "run_start", run=run_id, mode=mode, cwd=str(cwd), sandbox=self._sandbox_on(), prompt=prompt[:200])
        sec = self.cfg.security
        writes = mode not in READONLY_MODES
        if sec.snapshots and not self.git.is_git and writes:
            sid = await asyncio.to_thread(create_snapshot, cwd, run_id, sec.snapshot_limit_mb, sec.snapshot_keep)
            if sid:
                summary.snapshot = sid
                await _emit(self.hooks.on_event, "run", "snapshot", {"id": sid, "dir": str(cwd)})
                await self._log(f"Snapshot {sid} saved (not a git repo) - you can undo this run.")
            else:
                await self._log(f"No snapshot: directory is larger than {sec.snapshot_limit_mb} MB, so this run cannot be undone.")
        try:
            async with asyncio.timeout(self.cfg.run_timeout_minutes * 60 or None):
                if mode == "chat":
                    await self._chat(prompt, summary)
                elif mode in READONLY_MODES:
                    await self._fanout(run_id, prompt, mode, summary)
                elif mode == "cowork":
                    await self._cowork(run_id, prompt, summary)
                elif mode == "best":
                    await self._best(run_id, prompt, summary)
                elif mode.startswith("wf:"):
                    from .workflows import run_workflow

                    wf = next((w for w in self.cfg.workflows if w.id == mode[3:]), None)
                    if wf is None:
                        await self._log(f"Unknown workflow '{mode[3:]}'")
                        summary.failed.append("workflow")
                    else:
                        await run_workflow(self, run_id, wf, prompt, summary)
                else:
                    await self._storm(run_id, prompt, summary)
        except asyncio.CancelledError:
            for aid, st in list(self._status.items()):
                if st in (IDLE, RUNNING, MERGING):
                    await self._set_status(aid, FAILED)
            self.broker.finish_run(run_id, "cancelled")
            audit(self.cfg, "run_end", run=run_id, status="cancelled")
            await self._log("Run cancelled")
            raise
        except TimeoutError:
            await self._log(f"Run stopped: exceeded run_timeout_minutes ({self.cfg.run_timeout_minutes})")
            summary.failed.append("timeout")
        except BudgetExceeded as exc:
            await self._log(f"Run stopped: {exc}")
            summary.failed.append("budget")
        except (GitError, OSError, SandboxError) as exc:
            await self._log(f"Run aborted: {exc}")
            summary.failed.append(str(exc))
        summary.usage = self._usage_total()
        if summary.usage:
            await _emit(self.hooks.on_event, "run", "usage_total", summary.usage)
        status = "done" if summary.ok else "failed"
        self.broker.finish_run(run_id, status)
        audit(self.cfg, "run_end", run=run_id, status=status, merged=summary.merged, failed=summary.failed, usage=summary.usage)
        await self._log(
            f"Run {run_id} finished: {len(summary.merged)} merged, {len(summary.failed)} failed/unmerged"
        )
        return summary

    # ------------------------------------------------------------------- chat
    async def _chat(self, prompt: str, summary: RunSummary) -> None:
        """Single agent working directly in the chosen directory (any directory, git or not)."""
        cwd = self.sel.workdir or self.git.start
        first = not self.sel.session_id and not self.sel.continue_session
        stitle = f"{self.cfg.opencode.title_prefix}:{self.sel.chat_id}" if first else ""
        res = await self._agent(
            self.sel.chat_id, self.sel.chat_title, "chat", prompt, cwd,
            continue_session=self.sel.continue_session, session_id=self.sel.session_id, session_title=stitle,
        )
        if res.ok and not self.sel.session_id and self._api_session:
            summary.session_id = self._api_session
        elif res.ok and not self.sel.session_id:
            sid = await find_session_id(self.cfg, cwd, stitle) if stitle else ""
            pat = self.cfg.opencode.session_id_pattern
            if not sid and pat:
                m = re.search(pat, strip_ansi(res.output + "\n" + res.errors))
                sid = (m.group(1) if m.groups() else m.group(0)) if m else ""
            summary.session_id = sid
        text = res.clean_output.strip()
        summary.result = text
        if text:
            await _emit(self.hooks.on_result, self.sel.chat_id, text, "chat")
        (summary.merged if res.ok else summary.failed).append("chat")

    # ---------------------------------------------------- fan-out report modes
    def _limit(self, n: int) -> asyncio.Semaphore:
        cap = self.cfg.max_parallel if self.git.is_git or self.sel.mode not in ("storm", "cowork") else 1
        return asyncio.Semaphore(max(1, n) if cap <= 0 else max(1, cap))

    async def _fanout(self, run_id: str, prompt: str, kind: str, summary: RunSummary) -> None:
        """research / write / review: plan -> N parallel read-only agents -> one synthesizer."""
        cwd = self.git.workdir
        worker_role = {"research": "researcher", "write": "drafter", "review": "lens"}[kind]
        synth_role = "editor" if kind == "write" else "synthesizer"
        if kind == "review":
            tasks = [Subtask(id=slugify(lens, f"lens{i}"), title=lens.split(" (")[0].capitalize(),
                             prompt=f"Review focus: {lens}\n\nUser request: {prompt}")
                     for i, lens in enumerate(self.cfg.review_lenses, 1)]
        else:
            plan_role = "research_planner" if kind == "research" else "outliner"
            res = await self._agent(f"{run_id}-plan", "Planner", plan_role, f"User request:\n{prompt}", cwd)
            tasks = extract_subtasks(res.output) if res.ok else []
            if not tasks:
                await self._log("Planner returned no valid JSON plan - using a single worker.")
                tasks = [Subtask(id="main", title="Main", prompt=prompt)]
        tasks = normalize_plan(run_id, tasks)
        for t in tasks:
            t.depends_on = []
            await self._announce(t.id, t.title, worker_role, IDLE)
        await self._log(f"{kind}: {len(tasks)} parallel agent(s): " + ", ".join(t.title for t in tasks))
        sem = self._limit(len(tasks))
        outputs: dict[str, str] = {}

        async def work(t: Subtask) -> None:
            async with sem:
                override = self.cfg.prompts.lens.replace("{lens}", t.prompt.splitlines()[0]) if kind == "review" else ""
                res = await self._agent(t.id, t.title, worker_role, t.prompt, cwd, prompt_override=override)
                if res.ok and res.clean_output.strip():
                    outputs[t.id] = res.clean_output.strip()
                    self.broker.add_note(run_id, t.id, "result", f"{t.title}: {outputs[t.id][-1500:]}", t.id)
                elif res.ok:
                    await self._set_status(t.id, FAILED)

        await asyncio.gather(*(work(t) for t in tasks), return_exceptions=False)
        done = [t for t in tasks if t.id in outputs]
        if not done:
            summary.failed.append(kind)
            await self._log(f"{kind}: no agent produced output.")
            return
        budget = max(2000, 80_000 // len(done))
        label = "Sections" if kind == "write" else "Findings"
        merged_in = "\n\n".join(f"### {t.title}\n{outputs[t.id][-budget:]}" for t in done)
        sid = f"{run_id}-{synth_role}"
        res = await self._agent(sid, "Editor" if kind == "write" else "Synthesis", synth_role,
                                f"Original request:\n{prompt}\n\n## {label}\n{merged_in}", cwd)
        text = res.clean_output.strip() if res.ok else ""
        if not text:  # synthesis failed: fall back to the raw material so nothing is lost
            text = merged_in
            await self._log(f"{kind}: synthesis failed - showing raw results.")
        summary.result = text
        self.broker.add_note(run_id, sid, "final", text)
        await _emit(self.hooks.on_result, sid, text, kind)
        (summary.merged if res.ok else summary.failed).append(kind)

    # --------------------------------------------------------- agent messaging
    def _comm_block(self, run_id: str, agent_id: str) -> str:
        peers = [str(r["id"]) for r in self.broker.get_tasks(run_id) if str(r["id"]) != agent_id]
        return (
            "\n\n## Agent communication (best effort, never blocks unless you ask)\n"
            f"You are agent `{agent_id}` in run `{run_id}`. Other agents: "
            + (", ".join(f"`{p}`" for p in peers) if peers else "none")
            + ".\n"
            "Use the shared message bus from your bash tool:\n"
            "  openswarm msg agents\n"
            "  openswarm msg ask --to <agent-id> --body \"...\"   # send a question, keep working\n"
            "  openswarm msg check                              # non-blocking: collect new replies\n"
            "  openswarm msg reply <message-id> --body \"...\"     # answer a peer\n"
            "  openswarm msg history --limit 20\n"
            "Check messages at explicit checkpoints: (1) before committing to an interface or shared "
            "assumption, (2) after a major step, (3) before you finalize. Do not busy-poll. Use "
            "`inbox --wait N` (N <= 30) only when the answer is a hard dependency. Never wait more than "
            "~60 seconds: state an assumption and continue. Do not send secrets."
        )

    # ------------------------------------------------------------------ storm
    async def _plan(self, run_id: str, prompt: str) -> list[Subtask]:
        pid = f"{run_id}-planner"
        res = await self._read_only_agent(run_id, pid, "Planner", "planner", f"User request:\n{prompt}")
        tasks = extract_subtasks(res.output) if res.ok else []
        if not tasks:
            await self._log("Planner returned no valid JSON plan - falling back to a single worker.")
            tasks = [Subtask(id="main", title="Main task", prompt=prompt)]
        tasks = normalize_plan(run_id, tasks)
        self.broker.add_note(run_id, "planner", "plan", json.dumps([t.model_dump() for t in tasks], indent=2))
        await self._log(f"Plan: {len(tasks)} subtask(s): " + ", ".join(t.title for t in tasks))
        return tasks

    async def _storm(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        tasks = await self._plan(run_id, prompt)
        for t in tasks:
            self.broker.add_task(run_id, t.id, t.title, t.prompt, "coder", t.depends_on)
            await self._announce(t.id, t.title, "coder", IDLE)

        sem = self._limit(len(tasks))
        events = {t.id: asyncio.Event() for t in tasks}
        outcome: dict[str, bool] = {}

        async def worker(t: Subtask) -> None:
            ok = False
            try:
                for dep in t.depends_on:
                    await events[dep].wait()
                if not all(outcome.get(d) for d in t.depends_on):
                    await self._log(f"{t.title}: skipped (a dependency failed)")
                    self.broker.update_task(t.id, status="skipped")
                    await self._set_status(t.id, FAILED)
                    return
                async with sem:
                    ok = await self._storm_worker(run_id, t)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the swarm alive when a single worker blows up
                await self._log(f"{t.title}: crashed: {exc!r}")
                await self._set_status(t.id, FAILED)
            finally:
                outcome[t.id] = ok
                events[t.id].set()

        await asyncio.gather(*(worker(t) for t in tasks))
        for t in tasks:
            (summary.merged if outcome.get(t.id) else summary.failed).append(t.title)

    async def _storm_worker(self, run_id: str, t: Subtask) -> bool:
        wt = await self.git.create_worktree(t.id)
        self.broker.update_task(t.id, status="running", branch=wt.branch, worktree=str(wt.path))
        context = self.broker.context_block(run_id, topics=("result",))
        comm = self._comm_block(run_id, t.id)
        body = t.prompt + (f"\n\n## Results of completed tasks\n{context}" if context else "") + comm
        res = await self._agent(
            t.id, t.title, "coder", body, wt.path, hints=t.recommended_skills, final_status=None,
            extra_env={"OPENSWARM_RUN_ID": run_id, "OPENSWARM_BROKER_DB": str(self.broker.path)},
        )
        if not res.ok:
            self.broker.update_task(t.id, status="failed", result=res.errors[-2000:])
            await self._log(f"{t.title}: opencode exited {res.returncode}; worktree kept at {wt.path}")
            return False
        self.broker.add_note(run_id, t.id, "result", f"{t.title}: {strip_ansi(res.output)[-1500:]}", t.id)
        await self.git.commit_all(wt, f"swarm: {t.title}")
        ok, vout = await self._verify(t.id, wt.path)
        attempts = 0
        while not ok and attempts < self.cfg.fix_attempts:
            attempts += 1
            await self._log(f"{t.title}: verification failed - asking the coder to fix it ({attempts}/{self.cfg.fix_attempts})")
            fix = await self._agent(
                t.id, t.title, "coder",
                f"Your change fails the project's verification command (`{self.cfg.verify_command}`).\n\nOutput:\n{vout}\n\n"
                f"Fix it without breaking the task.\nOriginal task:\n{t.prompt}"
                + self._comm_block(run_id, t.id),
                wt.path, final_status=None,
                extra_env={"OPENSWARM_RUN_ID": run_id, "OPENSWARM_BROKER_DB": str(self.broker.path)},
            )
            if not fix.ok:
                break
            await self.git.commit_all(wt, f"swarm: {t.title} (fix {attempts})")
            ok, vout = await self._verify(t.id, wt.path)
        if not ok:
            self.broker.update_task(t.id, status="failed", result=vout)
            await self._set_status(t.id, FAILED)
            await self._log(f"{t.title}: verification failed; branch {wt.branch} kept for inspection")
            return False
        await self._set_status(t.id, MERGING)
        merged, msg = await self.git.merge(wt)
        if not merged:
            self.broker.update_task(t.id, status="failed", result=msg)
            await self._set_status(t.id, FAILED)
            await self._log(f"{t.title}: merge conflict - kept {wt.branch}\n{msg}")
            return False
        self.broker.update_task(t.id, status="done")
        await self._set_status(t.id, DONE)
        if self.cfg.cleanup_on_success:
            await self.git.cleanup(wt)
        await self._log(f"{t.title}: merged ({msg.splitlines()[0] if msg else 'ok'})")
        return True

    # ---------------------------------------------------------------- best-of-N
    async def _best(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        """N independent attempts in separate worktrees -> verify -> a judge picks one -> only that one is merged."""
        n = max(2, self.cfg.best_of_n)
        models = self.sel.candidate_models
        sem = self._limit(n)
        wts: dict[int, Worktree] = {}
        diffs: dict[int, str] = {}

        async def cand(i: int) -> None:
            tid = f"{run_id}-cand{i}"
            await self._announce(tid, f"Candidate {i}", "coder", IDLE)
            async with sem:
                wt = await self.git.create_worktree(tid)
                wts[i] = wt
                res = await self._agent(tid, f"Candidate {i}", "coder",
                                        f"{prompt}\n\n(Candidate {i} of {n}: solve it independently; do not assume other attempts exist.)",
                                        wt.path, final_status=None, model=models[(i - 1) % len(models)] if models else "")
                if not res.ok:
                    return
                await self.git.commit_all(wt, f"swarm: candidate {i}")
                ok, vout = await self._verify(tid, wt.path)
                if not ok:
                    await self._log(f"Candidate {i} failed verification")
                    await self._set_status(tid, FAILED)
                    return
                diffs[i] = await self.git.diff(wt, 12_000)
                await self._set_status(tid, DONE)

        await asyncio.gather(*(cand(i) for i in range(1, n + 1)))
        viable = [i for i in sorted(diffs) if await self.git.has_changes(wts[i])]
        winner = viable[0] if len(viable) == 1 else 0
        if len(viable) > 1:
            body = f"Request:\n{prompt}\n\n" + "\n\n".join(f"### Candidate {i}\n```diff\n{diffs[i]}\n```" for i in viable)
            res = await self._agent(f"{run_id}-judge", "Judge", "judge", body, self.git.workdir)
            m = re.findall(r"WINNER:\s*(\d+)", strip_ansi(res.output))
            pick = int(m[-1]) if m else 0
            winner = pick if pick in viable else viable[0]
            await self._log(f"Judge picked candidate {winner}.")
        if not winner:
            summary.failed.append("best-of-N: no viable candidate")
            await self._log("No candidate produced a verified change.")
        else:
            tid = f"{run_id}-cand{winner}"
            await self._set_status(tid, MERGING)
            merged, msg = await self.git.merge(wts[winner])
            await self._set_status(tid, DONE if merged else FAILED)
            if merged:
                summary.merged.append(f"candidate {winner}")
                summary.result = f"Merged candidate {winner}.\n\n{diffs[winner]}"
                await _emit(self.hooks.on_result, tid, summary.result, "best")
            else:
                summary.failed.append("merge conflict")
                await self._log(f"Winner could not be merged:\n{msg}")
        for i, wt in wts.items():
            if i != winner:
                await self._set_status(f"{run_id}-cand{i}", "Skipped" if i in diffs else FAILED)
            if self.cfg.cleanup_on_success or i != winner:
                await self.git.cleanup(wt)

    # ----------------------------------------------------------------- cowork
    async def _cowork(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        arch_id = f"{run_id}-architect"
        res = await self._read_only_agent(run_id, arch_id, "Architect", "architect", f"User request:\n{prompt}")
        if not res.ok:
            summary.failed.append("architect")
            await self._log("Architect failed; aborting.")
            return
        spec = strip_ansi(res.output).strip()
        self.broker.add_note(run_id, "architect", "spec", spec)
        await self._log("Architect spec stored in the shared context broker.")

        task_id = f"{run_id}-cowork"
        wt = await self.git.create_worktree(task_id)
        self.broker.add_task(run_id, task_id, "cowork", prompt, "coder")
        self.broker.update_task(task_id, status="running", branch=wt.branch, worktree=str(wt.path))

        feedback = ""
        for rnd in range(1, self.cfg.max_review_rounds + 1):
            if await self._cowork_round(run_id, rnd, prompt, spec, wt, feedback, summary):
                return
            feedback = self.broker.latest_note(run_id, "feedback")
        self.broker.update_task(task_id, status="failed")
        summary.failed.append("cowork: review rounds exhausted")
        await self._log(f"Reviewer never approved after {self.cfg.max_review_rounds} rounds; branch {wt.branch} kept.")

    async def _cowork_round(
        self, run_id: str, rnd: int, prompt: str, spec: str, wt: Worktree, feedback: str, summary: RunSummary
    ) -> bool:
        """One coder->(verify)->reviewer round. True when approved *and* merged (or hard-failed)."""
        coder_id = f"{run_id}-coder-{rnd}"
        body = f"Original request:\n{prompt}\n\n## Implementation spec\n{spec}"
        if feedback:
            body += f"\n\n## Reviewer feedback to address (round {rnd - 1})\n{feedback}"
        res = await self._agent(coder_id, f"Coder #{rnd}", "coder", body, wt.path)
        if not res.ok:
            await self._log(f"Round {rnd}: coder crashed (exit {res.returncode}); will retry if rounds remain.")
            self.broker.add_note(run_id, "coder", "feedback", "The previous coder run crashed; retry.")
            return False
        await self.git.commit_all(wt, f"swarm: cowork round {rnd}")

        ok, vout = await self._verify(coder_id, wt.path)
        if not ok:
            self.broker.add_note(run_id, "verify", "feedback", f"verify_command failed:\n{vout}")
            return False

        diff = await self.git.diff(wt)
        if not await self.git.has_changes(wt):
            self.broker.add_note(run_id, "reviewer", "feedback", "No changes were produced. Implement the spec.")
            return False

        rev_id = f"{run_id}-reviewer-{rnd}"
        rbody = f"Original request:\n{prompt}\n\n## Spec\n{spec}\n\n## Diff against base\n```diff\n{diff}\n```"
        rres = await self._agent(rev_id, f"Reviewer #{rnd}", "reviewer", rbody, wt.path)
        text = strip_ansi(rres.output).strip()
        verdict = parse_verdict(text) if rres.ok else None
        if verdict is not True:
            note = text[-3000:] if text else "Reviewer produced no output."
            self.broker.add_note(run_id, "reviewer", "feedback", note)
            await self._log(f"Round {rnd}: changes requested" if verdict is False else f"Round {rnd}: no verdict, retrying")
            return False

        await self._set_status(coder_id, MERGING)
        merged, msg = await self.git.merge(wt)
        if not merged:
            await self._set_status(coder_id, FAILED)
            self.broker.add_note(run_id, "git", "feedback", f"Merge conflict with main; rebase your work:\n{msg}")
            summary.failed.append("merge conflict")
            await self._log(f"Approved but merge failed:\n{msg}")
            return True  # stop looping; needs a human
        await self._set_status(coder_id, DONE)
        self.broker.update_task(wt.task_id, status="done")
        if self.cfg.cleanup_on_success:
            await self.git.cleanup(wt)
        summary.merged.append("cowork")
        await self._log(f"Round {rnd}: approved and merged.")
        return True
