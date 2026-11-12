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
from .capabilities import get_caps, rejected_flag
from .docs import expand_attachments, extract_images
from .safety import SandboxError, audit, create_snapshot
from .git_manager import GitError, GitManager, Worktree
from .runner import OpenCodeRunner, RunResult, find_session_id, strip_ansi
from .keys import KeyRun, get_pool, is_openrouter_model
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
    priority: str = ""  # balanced|quality|fast|free|local (empty = cfg.priority)
    effort: str = ""  # default|none|minimal|low|medium|high|xhigh|max (empty = cfg.effort)
    history_context: str = ""  # summarized multi-turn conversation memory from session events
    target_agents: int = 0  # explicit user-requested agent count (0 = auto/default)
    files: list[str] = field(default_factory=list)  # attached files or context references
    images: list[str] = field(default_factory=list)  # attached screenshots or photos for vision
    focus: str = "auto"  # auto | code | debug | architecture | docs | vision | review | chat


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


def looks_like_toolcall(text: str) -> bool:
    """True when the whole reply is just a JSON tool call (a model that cannot drive tools prints it as text)."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S).strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        obj = json.loads(t)
    except ValueError:
        return False
    return isinstance(obj, dict) and ("name" in obj or "tool" in obj) and any(k in obj for k in ("arguments", "parameters", "input", "args"))


async def gather_strict(*aws):
    """asyncio.gather that CANCELS (and awaits) the siblings when one fails or the caller is cancelled,
    so no agent process keeps running after its run has ended."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class BudgetExceeded(RuntimeError):
    pass


READONLY_MODES = ("research", "write", "review", "debate", "architect")


def suggest_mode(prompt: str, is_git: bool) -> tuple[str, str]:
    """Smart intent classifier for Auto mode (no LLM call required). Returns (mode, reason)."""
    p = prompt.lower()
    if re.search(r"\b(self-?heal|diagnose and fix|root.?cause analysis|auto-?repair|fix error|traceback|crash)\b|самодиагностик|проанализируй ошибк|почини сам|исправь ошибку|почему не работает|устрани баг|упало", p):
        return "heal", "asks for self-healing / root-cause diagnosis and repair"
    if re.search(r"\b(architecture blueprint|system architecture|design spec|architecture decision|schema design)\b|спроектируй|архитектур|дизайн системы|схема базы", p):
        return "architect", "asks for architecture & system design blueprint"
    if re.search(r"\b(code review|security review|audit|review (the|my|this) (code|diff|pr|changes))\b|ревью|проверь код|аудит безопасности|найди уязвимости", p):
        return "review", "asks for a code review or security audit"
    if re.search(r"\b(debate|pros and cons|trade-?offs?|should (i|we) (use|choose|pick)|versus|\bvs\.?\b)\b|дебаты|поспорьте|плюсы и минусы|что выбрать|сравни варианты", p):
        return "debate", "asks to weigh competing options and trade-offs"
    if re.search(r"(write|draft|compose)\s+(an?\s+|the\s+)?(\w+\s+)?(article|blog|post|essay|report|story|newsletter|whitepaper)|напиши\s+(\w+\s+)?(статью|пост|эссе|отчёт|отчет|рассказ|текст|документацию)", p):
        return "write", "asks for long-form writing or documentation"
    if re.search(r"\b(research|investigate|survey|state of the art|market|literature)\b|исследуй|изучи|найди информацию|сделай обзор", p):
        return "research", "asks for deep research or investigation"
    if re.search(r"\b(plan|roadmap|step-by-step plan|break down)\b|составь план|распиши шаги|спланируй", p):
        return "plan", "asks for step-by-step implementation plan"
    steps = len(re.findall(r"(^|\n)\s*(\d+[.)]|[-*])\s", prompt)) + len(re.findall(r"\b(and also|then|plus)\b|, а также|\bзатем\b", p))
    big = re.search(r"\b(refactor|migrate|rewrite|implement|build)\b|реализуй|перепиши|рефактор|мигрируй", p)
    if is_git and (steps >= 3 or (big and len(prompt) > 300)):
        return "storm", "large multi-part change in a git repo"
    # 'fix' + code-noun anywhere in sentence → cowork; "fix typo/spelling" → chat
    if re.search(r"\b(function|class|feature|script|module|add|implement)\b|напиши код|создай файл|добавь функцию|сделай класс|разработай", p):
        return "cowork", "coding and testing task with review loop"
    if re.search(r"\bfix\b.{0,50}\b(bug|error|issue|problem|auth|login|api|test|import|exception|crash|logic|parse|sql|query|async|request|response)\b", p):
        return "cowork", "coding and testing task with review loop"
    return "chat", "standard conversation and rapid answers"
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


def extract_subtasks(text: str) -> list[Subtask]:
    """Find the first JSON array (or {"subtasks": [...]}) of task objects in noisy LLM output."""
    text = strip_ansi(text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
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
        self._run_id = ""
        self._last_bus_id = 0
        self._clarification_futures: dict[str, asyncio.Future[str]] = {}

    def resolve_clarification(self, qid: str, answer: str) -> bool:
        fut = self._clarification_futures.get(qid)
        if fut and not fut.done():
            fut.set_result(answer)
            return True
        return False

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
        if self._run_id and not is_err:
            clean = strip_ansi(line).strip()
            peer_m = re.search(r"@(\w+):\s*(.+)", clean) or re.search(r"\[PEER_MSG to=\"?(\w+)\"?\](.+?)\[/PEER_MSG\]", clean)
            if peer_m:
                target, text = peer_m.group(1).lower(), peer_m.group(2).strip()
                if target in ("coder", "architect", "reviewer", "planner", "all"):
                    mid = self.broker.post_message(self._run_id, agent_id, target, text, kind="peer_request")
                    if self.hooks.on_event:
                        asyncio.ensure_future(_emit(self.hooks.on_event, "bus", {
                            "mid": mid, "sender": agent_id, "recipient": target, "kind": "peer_request", "content": text
                        }))
            q_m = re.search(r"<question(?: title=\"([^\"]+)\")?>([\s\S]+?)</question>", clean) or re.search(r"\[USER_QUESTION\](.+?)\[/USER_QUESTION\]", clean)
            if q_m:
                q_text = (q_m.group(2) if len(q_m.groups()) > 1 and q_m.group(2) else q_m.group(1)).strip()
                mid = self.broker.post_message(self._run_id, agent_id, "user", q_text, kind="question")
                if self.hooks.on_event:
                    asyncio.ensure_future(_emit(self.hooks.on_event, "bus", {
                        "mid": mid, "sender": agent_id, "recipient": "user", "kind": "question", "content": q_text
                    }))

        # Runner callbacks are sync; schedule the (possibly async) hook without blocking the stream.
        res = self.hooks.on_line(agent_id, line, is_err) if self.hooks.on_line else None
        if inspect.isawaitable(res):
            asyncio.ensure_future(res)

    def _model_for(self, role: str) -> str:
        if role.startswith("wf"):  # workflow step roles: wfro_planner / wfrw_coder ...
            slot = role.rsplit("_", 1)[-1]
            return {"planner": self.sel.planner_model, "reviewer": self.sel.reviewer_model}.get(slot, self.sel.coder_model) or getattr(self.cfg.roles, slot, self.cfg.roles.coder)
        if role in ("planner", "architect", "research_planner", "outliner", "synthesizer", "editor", "judge", "moderator", "diagnostician"):
            base = "planner" if role not in ("architect",) else "architect"
            return self.sel.planner_model or getattr(self.cfg.roles, base)
        if role in ("reviewer", "lens"):
            return self.sel.reviewer_model or self.cfg.roles.reviewer
        return self.sel.coder_model or self.cfg.roles.coder

    def _boundary_block(self, cwd: Path | None) -> str:
        """Tells every model where it works so it does not roam through other folders without permission."""
        if cwd is None or not self.cfg.security.confine_agents:
            return ""
        try:
            wd = Path(cwd).resolve()
            root = Path(self.git.root).resolve()
        except (OSError, RuntimeError):
            return ""
        lines = [
            "\n## Working directory boundary (IMPORTANT)\n",
            f"- Your working directory is: {wd}\n",
        ]
        if root != wd:
            lines.append(f"- It is an isolated copy of the project at: {root} (do not touch the original directly; OpenSwarm merges your work).\n")
        lines += [
            "- Work ONLY inside this directory. Do not read, list, search, edit, create or delete files outside it: "
            "no `cd ..`, no absolute paths into other folders, no `find /`, no scanning of the home directory or other projects.\n",
            "- If you truly need something outside (another project, a dotfile, a system config), STOP and ask the user for permission first, "
            "naming the exact path and why you need it. Continue only after the user agrees.\n",
            "- Running installed tools (python, git, npm, compilers) is fine; just don't browse their install locations.\n",
        ]
        return "".join(lines)

    def _compose(self, role: str, body: str, extra_hints: list[str] | None = None, override: str = "", cwd: Path | None = None) -> str:
        from .adaptive_prompt import get_adaptive_instructions
        from .optimizer import get_optimizer_settings, get_token_saving_directive, optimize_prompt_for_model

        opt_cfg = get_optimizer_settings()
        bus_txt = self.broker.chat_transcript(self._run_id) if self._run_id else ""
        bus_block = f"\n## Swarm Room Messages (live team & user feed)\n{bus_txt}\n" if bus_txt else ""
        boundary = self._boundary_block(cwd)
        eco_dir = get_token_saving_directive(role)

        if opt_cfg.mode == "ultra":
            proactive_guidance = f"\n## Rules\n- Ask clarifying questions if ambiguous. Do not output internal reasoning/thinking.\n{boundary}"
            output_guide = "\n## Presentation\n- Answer concisely and directly. Use fenced code blocks for code.\n" + eco_dir
        else:
            proactive_guidance = (
                "\n## Interaction & Proactive Clarification\n"
                "- If requirements are ambiguous, state assumptions briefly and ask clarifying questions.\n"
                "- Subagent delegation: Decompose complex subtasks when appropriate.\n"
                "- Do NOT output internal chain-of-thought, `Thinking:` prefixes, or `<think>` blocks in your response.\n"
            ) + boundary
            output_guide = (
                "\n## Правила ответа:\n"
                "- Отвечай кратко, чётко и сразу по сути вопроса, без длинных вступлений, воды и лишних секций.\n"
                "- Не выводи внутренние рассуждения (`Thinking:`, `<think>`) и сырые логи — только готовый ответ.\n"
                "- Код и команды оформляй в блоки с указанием языка (```python, ```bash и т.д.).\n"
            ) + eco_dir

        adaptive = get_adaptive_instructions(
            body, cwd or self.sel.workdir,
            focus_override=getattr(self.sel, "focus", "auto"),
            has_images=bool(getattr(self.sel, "images", None)),
            role=role,
        )

        if role == "chat":  # plain conversation: keep prompt lightweight & natural
            hints = [s.prompt_hint for s in self.skills if s.prompt_hint]
            hint_sfx = ("\n\n[OpenSwarm guidance: " + " ".join(hints) + "]") if hints else ""
            raw = f"{body}{bus_block}{adaptive}{output_guide}{hint_sfx}"
            return optimize_prompt_for_model(raw, role="chat")
        hints = [s.prompt_hint for s in self.skills if s.prompt_hint]
        if extra_hints:
            hints.append("Planner-recommended skills/tools for this task: " + ", ".join(extra_hints))
        hint_block = ("\n## Tooling guidance\n" + "\n".join(f"- {h}" for h in hints) + "\n") if hints else ""
        head = override or getattr(self.cfg.prompts, role, "")
        raw = f"{head}\n{hint_block}{proactive_guidance}{output_guide}{adaptive}{bus_block}\n## Task\n{body}" if head else f"{hint_block}{proactive_guidance}{output_guide}{adaptive}{bus_block}\n{body}".strip()
        return optimize_prompt_for_model(raw, role=role)

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

    async def _chat_via_api(self, agent_id: str, title: str, message: str, cwd: Path, session_id: str, stitle: str, sandbox: bool,
                            key_env: dict[str, str] | None = None) -> RunResult | None:
        """Chat through the opencode HTTP API (permissions, tool cards, usage). None -> caller falls back to `run`."""
        from .dsh import resolve_dsh_model
        from .kilo import normalize_kilo_model
        oc = self.cfg.opencode
        chat_mdl = normalize_kilo_model(resolve_dsh_model(self._model_for("chat")))
        probe = OpenCodeRunner(self.cfg, cwd, agent_id=agent_id, role="chat", model=chat_mdl, skills=self.skills,
                              extra_env=key_env)
        ep = await self.pool.endpoint(cwd, probe.build_env(), sandbox)  # type: ignore[union-attr]
        if not ep:
            return None
        all_img_paths = list(self._images)
        for img_rel in (getattr(self.sel, "images", None) or []):
            try:
                ip = (cwd / img_rel).resolve() if not Path(img_rel).is_absolute() else Path(img_rel)
                if ip.is_file() and ip not in all_img_paths:
                    all_img_paths.append(ip)
            except Exception:
                pass
        files = [{"type": "file", "mime": {"png": "image/png", "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}.get(p.suffix.lower()[1:], "image/jpeg"),
                  "url": p.resolve().as_uri(), "filename": p.name} for p in all_img_paths]
        self._line_cb(agent_id, f"(opencode API {ep[0]})", False)
        try:
            from .capabilities import model_supports_variant
            eff = self.sel.effort or getattr(self.cfg, "effort", "")
            eff_for_chat = eff if model_supports_variant(chat_mdl) else ""
            r = await run_chat_api(
                Http(*ep), text=message, title=stitle or title, session_id=session_id, model=chat_mdl,
                agent=oc.role_agents.get("chat", ""), variant=eff_for_chat, files=files, on_line=lambda t, e: self._line_cb(agent_id, t, e),
                on_event=lambda k, p: self._api_event(agent_id, k, p), on_permission=lambda req: self._ask_permission(agent_id, req),
                timeout=oc.api_timeout_minutes * 60, idle_poll=oc.api_idle_poll_seconds,
            )
        except ApiUnavailable as exc:
            self.pool.api_bad = True  # type: ignore[union-attr]
            await self._log(f"opencode HTTP API unavailable ({str(exc)[:160]}); using `opencode run` instead")
            self._line_cb(agent_id, f"HTTP API unavailable: {str(exc)[:300]}", True)  # visible in the agent log, not only the swarm log
            self._line_cb(agent_id, "-> falling back to `opencode run` (run `openswarm --doctor --live` for details)", True)
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
    ) -> RunResult:
        self._agent_count += 1
        if self.cfg.max_agents_per_run and self._agent_count > self.cfg.max_agents_per_run:
            raise BudgetExceeded(f"max_agents_per_run ({self.cfg.max_agents_per_run}) reached")
        await self._announce(agent_id, title, role)
        await self._set_status(agent_id, RUNNING)
        from .dsh import (
            DSH_FREE_FALLBACK_MODEL,
            is_dsh_model,
            is_opencode_free_model,
            mark_opencode_free_limited,
            next_free_fallback_model,
            resolve_dsh_model,
        )
        from .kilo import KILO_FALLBACK_FREE_MODEL, is_kilo_model, normalize_kilo_model
        from .runner import is_soft_success
        oc = self.cfg.opencode
        message = self._compose(role, body, hints, prompt_override, cwd)
        sandbox = self._sandbox_on()
        mdl = normalize_kilo_model(resolve_dsh_model(model or self._model_for(role)))
        audit(self.cfg, "agent", agent=agent_id, role=role, model=mdl, cwd=str(cwd), sandbox=sandbox)
        res = RunResult(1)
        done_api = False
        kp = get_pool(self.cfg) if is_openrouter_model(self.cfg, mdl) else None
        keyrun = KeyRun(kp, self._log) if kp else None  # several OpenRouter keys -> rotate on limit / no credits
        from .vpn import get_vpn
        vpn = get_vpn(self.cfg)
        if vpn.enabled:
            await asyncio.to_thread(vpn.ensure)
        bus_env = {"OPENSWARM_DB": str(self.git.swarm_dir / "swarm.db"), "OPENSWARM_RUN_ID": self._run_id}
        if (
            role == "chat"
            and oc.use_api
            and oc.use_server
            and self.pool
            and not self.pool.api_bad
            and not vpn.enabled
            and not is_kilo_model(mdl)
            and not is_dsh_model(mdl)
        ):
            sid = session_id
            api_vpn_rotations = 0
            while True:
                if keyrun:
                    await keyrun.next_key()
                used_vpn = vpn.current
                api_res = await self._chat_via_api(agent_id, title, message, cwd, sid, session_title, sandbox,
                                                   {**bus_env, **(keyrun.env if keyrun else {})})
                if api_res is None:
                    break
                res, done_api = api_res, api_res.ok
                err_combo = res.errors + "\n" + res.output
                if not res.ok and re.search(r"Free usage limit reached|FreeUsageLimitError|QuotaExceeded", err_combo, re.I):
                    can_try_vpn = bool(
                        vpn.auto_switch_on_limits
                        and getattr(self.cfg.vpn, "prefer_vpn_before_model_switch", True)
                        and (vpn.enabled or getattr(self.cfg.vpn, "auto_enable_on_limit", True))
                    )
                    if is_opencode_free_model(mdl) and not can_try_vpn:
                        mark_opencode_free_limited()
                    done_api = False
                    break
                if keyrun and not res.ok and await keyrun.failed(err_combo):
                    self._line_cb(agent_id, "-> OpenRouter key switched, repeating the request", True)
                    sid = self._api_session or sid  # resume the session the failed attempt created
                    continue
                if (
                    not res.ok
                    and vpn.enabled
                    and vpn.auto_switch_on_limits
                    and vpn.is_limit(err_combo)
                    and api_vpn_rotations < self.cfg.vpn.max_rotations_per_request
                ):
                    api_vpn_rotations += 1
                    self._line_cb(agent_id, "-> Provider limit detected! Swapping VPN server...", True)
                    rotated = await asyncio.to_thread(vpn.rotate, "provider limit (api)", True, used_vpn)
                    if rotated:
                        if self.pool:
                            await self.pool.close()
                            self.pool.api_bad = False
                        chk = getattr(vpn, "last_check", {})
                        info_parts = []
                        if chk.get("latency_ms") is not None:
                            info_parts.append(f"{chk['latency_ms']}ms")
                        if chk.get("exit_ip"):
                            info_parts.append(f"IP {chk['exit_ip']}")
                        diag = f" ({', '.join(info_parts)})" if info_parts else ""
                        self._line_cb(agent_id, f"-> Swapped VPN exit server to {vpn.current}{diag}, repeating request", False)
                        await self._log(f"{title}: swapped VPN server to {vpn.current}{diag} after limit")
                        sid = self._api_session or sid
                        continue
                break
        mdl = normalize_kilo_model(resolve_dsh_model(mdl))
        rate_re = re.compile(oc.rate_limit_pattern, re.I) if oc.rate_limit_pattern else None
        caps = await get_caps(self.cfg)
        att_flag = caps.attach_flag(oc)
        use_server = bool(
            self.pool
            and oc.use_server
            and not done_api
            and att_flag
            and not vpn.enabled
            and not is_kilo_model(mdl)
            and not is_dsh_model(mdl)
        )
        auto = "auto_approve" in self.sel.skills and caps.can_auto(oc)
        extra_rw = [self.git.root / ".git"] if self.git.is_git else []
        if self.git.swarm_dir.exists():
            extra_rw.append(self.git.swarm_dir)
        files = [str(p) for p in self._images] if role == "chat" else []
        for im in (getattr(self.sel, "images", None) or []):
            imp = str((cwd / im).resolve()) if not Path(im).is_absolute() else str(im)
            if imp not in files:
                files.append(imp)
        for f in (getattr(self.sel, "files", None) or []):
            fp = str((cwd / f).resolve()) if not Path(f).is_absolute() else str(f)
            if fp not in files:
                files.append(fp)
        attempt, vpn_rotations, flag_fixes, model_switches = 0, 0, 0, 0
        initial_mdl = mdl
        tried_models: set[str] = {mdl}
        from .healer import analyze_error, heal_git_workspace
        from .plugins import hook_enabled
        from .router import get_router

        router = get_router()
        t_start = asyncio.get_running_loop().time()
        eff = self.sel.effort or getattr(self.cfg, "effort", "")
        while not done_api and attempt <= oc.retries:
            att_flag = caps.attach_flag(oc)
            if vpn.enabled or is_kilo_model(mdl) or is_dsh_model(mdl):
                use_server = False
            if keyrun:
                await keyrun.next_key()
            used_vpn = vpn.current
            runner = OpenCodeRunner(
                self.cfg, cwd, agent_id=agent_id, role=role, model=mdl, skills=self.skills,
                on_line=self._line_cb, continue_session=continue_session, session_id=session_id,
                title=session_title, agent=oc.role_agents.get(role, ""), files=files, sandbox=sandbox, extra_rw=extra_rw,
                attach_flag=att_flag, standalone=True if (vpn.enabled or not use_server) else caps.can_standalone(oc),
                auto=auto and caps.can_auto(oc), thinking=False,
                effort=eff, variant_flag=caps.variant_flag(oc), thinking_flag=caps.thinking_flag(oc),
                extra_env={**bus_env, **(keyrun.env if keyrun else {})},
            )
            env = runner.build_env()
            if use_server and att_flag:
                ep = await self.pool.endpoint(cwd, env, sandbox)  # type: ignore[union-attr]
                if ep:
                    runner.server_url, runner.extra_env = ep[0], {**runner.extra_env, "OPENCODE_SERVER_PASSWORD": ep[1]}
                    self._line_cb(agent_id, f"(warm server {ep[0]})", False)
            head_before = ""
            if self.git.is_git:
                try:
                    from .git_manager import run_git
                    rc_h, out_h, _ = await run_git(cwd, "rev-parse", "HEAD", check=False)
                    if rc_h == 0:
                        head_before = out_h.strip()
                except Exception:
                    pass
            res = await runner.run(message)
            text = res.errors + "\n" + res.output
            if not res.ok:
                soft_ok, soft_reason = is_soft_success(res.output, res.errors, res.returncode, cwd)
                if not soft_ok and self.git.is_git and head_before and res.returncode == 1 and res.clean_output.strip():
                    try:
                        from .git_manager import run_git
                        rc_h2, out_h2, _ = await run_git(cwd, "rev-parse", "HEAD", check=False)
                        if rc_h2 == 0 and out_h2.strip() and out_h2.strip() != head_before:
                            diag_check = analyze_error(text[-3000:])
                            if diag_check["kind"] == "runtime_error":
                                soft_ok, soft_reason = True, "new git commit + valid output"
                    except Exception:
                        pass
                if soft_ok:
                    res.returncode = 0
                    self._line_cb(agent_id, f"✓ [exit 1 -> 0: ответ принят ({soft_reason})]", False)
            bad = rejected_flag(text) if not res.ok else ""
            if bad and flag_fixes < 4:  # this opencode does not know a flag we used: learn it and retry without it
                flag_fixes += 1
                caps.disable(bad)
                if bad in (oc.attach_flag, oc.server_flag):
                    self.pool.mark_bad(cwd, env, sandbox)  # type: ignore[union-attr]
                    use_server = False
                self._line_cb(agent_id, f"this opencode does not support {bad}; retrying without it", True)
                await self._log(f"opencode does not support {bad} - disabled for this session")
                continue
            from .capabilities import mark_model_variant_unsupported, rejected_variant_model

            bad_var = rejected_variant_model(text) if not res.ok else ""
            if not res.ok and (bad_var or "Variant unavailable" in text) and attempt <= oc.retries:
                target_mdl = bad_var or mdl
                mark_model_variant_unsupported(target_mdl)
                self._line_cb(agent_id, f"Model '{target_mdl}' does not support reasoning effort ({eff}); retrying without variant...", True)
                await self._log(f"Model {target_mdl} does not support variant - retried without variant")
                continue
            if not res.ok and keyrun and await keyrun.failed(text[-3000:]):
                self._line_cb(agent_id, "OpenRouter key limit/credits/auth problem - retrying with the next key", True)
                continue
            is_free_quota_exceeded = bool(
                not res.ok and re.search(r"Free usage limit reached|FreeUsageLimitError|QuotaExceeded|model_not_found", text, re.I)
            )
            prefer_vpn_first = bool(getattr(self.cfg.vpn, "prefer_vpn_before_model_switch", True))
            can_auto_vpn = bool(vpn.enabled or getattr(self.cfg.vpn, "auto_enable_on_limit", True))
            if (
                not res.ok
                and can_auto_vpn
                and vpn.auto_switch_on_limits
                and (vpn.is_limit(text[-3000:]) or is_free_quota_exceeded)
                and (prefer_vpn_first or not is_free_quota_exceeded)
                and vpn_rotations < min(3, max(1, self.cfg.vpn.max_rotations_per_request))
            ):
                vpn_rotations += 1
                self._line_cb(agent_id, "-> Лимит тарифа / провайдера обнаружен! Переключаю VPN-сервер для обхода...", True)
                if not vpn.enabled:
                    rotated = await asyncio.to_thread(vpn.set_enabled, True)
                else:
                    rotated = await asyncio.to_thread(vpn.rotate, "provider/free limit (runner)", True, used_vpn)
                if rotated:
                    use_server = False
                    if self.pool:
                        await self.pool.close()
                        self.pool.api_bad = False
                    if is_free_quota_exceeded:
                        from .dsh import clear_opencode_free_limited
                        clear_opencode_free_limited()
                        session_id = ""
                        continue_session = False
                    chk = getattr(vpn, "last_check", {})
                    info_parts = []
                    if chk.get("latency_ms") is not None:
                        info_parts.append(f"{chk['latency_ms']}ms")
                    if chk.get("exit_ip"):
                        info_parts.append(f"IP {chk['exit_ip']}")
                    diag = f" ({', '.join(info_parts)})" if info_parts else ""
                    self._line_cb(agent_id, f"-> VPN-узел переключен на {vpn.current}{diag}, повторяю запрос на {mdl}", False)
                    await self._log(f"{title}: swapped VPN server to {vpn.current}{diag} after limit")
                    continue
            if not res.ok:
                if is_free_quota_exceeded and is_opencode_free_model(mdl):
                    mark_opencode_free_limited()
                should_fallback_free = is_free_quota_exceeded or (
                    is_dsh_model(mdl) or is_kilo_model(mdl)
                )
                if should_fallback_free and getattr(self.cfg, "auto_model_failover", True):
                    router.record_failure(mdl, text[-300:], "rate_limit" if is_free_quota_exceeded else "model_broken")
                    next_free = next_free_fallback_model(mdl, tried_models)
                    if next_free:
                        old_mdl = mdl
                        mdl = next_free
                        tried_models.add(mdl)
                        model_switches += 1
                        self._line_cb(
                            agent_id,
                            f"🔄 Лимит бесплатного тарифа на {old_mdl} -> мгновенный обход через {mdl}",
                            True,
                        )
                        await self._log(f"{title}: free limit/failure on {old_mdl} -> switched to {mdl}")
                        use_server = False
                        continue
            if not res.ok and runner.server_url and re.search(r"ECONNREFUSED|fetch failed|unable to connect|failed to connect|\b401\b|unauthori", text, re.I):
                self.pool.mark_bad(cwd, env, sandbox)  # type: ignore[union-attr]
                self._line_cb(agent_id, "warm server unreachable; retrying without it", True)
                use_server = False
                continue
            if not res.ok:
                diag = analyze_error(text[-3000:])
                router.record_failure(mdl, diag["snippet"], diag["kind"])
                if diag["kind"] != "unknown":
                    self._line_cb(agent_id, f"🩺 Auto-Diagnosis [{diag['title']}]: {diag['diagnosis']}", True)
                if self.cfg.auto_heal and hook_enabled("auto_self_heal") and diag["kind"] == "git_worktree":
                    healed = await asyncio.to_thread(heal_git_workspace, cwd)
                    if healed:
                        await self._log(f"{title}: self-healed git workspace ({'; '.join(healed)})")
                if (
                    self.cfg.auto_model_failover
                    and hook_enabled("auto_model_failover")
                    and not model
                    and not keyrun
                    and model_switches < 2
                    and diag["kind"] in ("model_broken", "auth_error")
                ):
                    alt = router.fallback_model(self.cfg, role, mdl, self.sel.priority or self.cfg.priority, tried_models)
                    if alt and alt != mdl:
                        old_mdl = mdl
                        mdl = alt
                        tried_models.add(mdl)
                        model_switches += 1
                        self._line_cb(agent_id, f"🔄 Smart Model Failover: {old_mdl} -> {mdl}", True)
                        await self._log(f"{title}: model {old_mdl} failed ({diag['title']}) -> switched to {mdl}")
                        continue
            if res.ok or not rate_re or attempt >= oc.retries:
                break
            if not rate_re.search(text[-3000:]):
                break
            delay = oc.retry_backoff_seconds * (3 ** attempt)
            await self._log(f"{title}: rate limited / overloaded - retry {attempt + 1}/{oc.retries} in {delay}s")
            await asyncio.sleep(delay)
            attempt += 1
        if res.ok:
            router.record_success(mdl, max(0.01, asyncio.get_running_loop().time() - t_start))
            if mdl != initial_mdl:
                target_role = "coder" if role == "chat" else role
                if hasattr(self.cfg.roles, target_role):
                    setattr(self.cfg.roles, target_role, mdl)
                if role == "chat" and hasattr(self.cfg.roles, "chat"):
                    self.cfg.roles.chat = mdl
                if isinstance(getattr(self.sel, "models", None), dict):
                    self.sel.models[target_role] = mdl
                try:
                    from .state import load_state, save_state
                    st = load_state()
                    roles_dict = dict(st.get("roles") or {})
                    roles_dict[target_role] = mdl
                    if target_role == "planner":
                        roles_dict["architect"] = mdl
                    st["roles"] = roles_dict
                    save_state(st)
                except Exception:
                    pass
                await _emit(
                    self.hooks.on_event,
                    agent_id,
                    "role_model_updated",
                    {
                        "role": target_role,
                        "old_model": initial_mdl,
                        "model": mdl,
                        "roles": {
                            "planner": self.cfg.roles.planner,
                            "coder": self.cfg.roles.coder,
                            "reviewer": self.cfg.roles.reviewer,
                        },
                    },
                )
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

    async def _pump_bus(self, run_id: str) -> None:
        """Stream newly posted inter-agent chat messages and questions from the SQLite broker to UI hooks."""
        while True:
            await asyncio.sleep(0.25)
            for m in self.broker.get_messages(run_id, since_id=self._last_bus_id):
                self._last_bus_id = max(self._last_bus_id, int(m["id"]))
                await _emit(self.hooks.on_event, str(m["sender"]), "bus", m)

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
        if mode in ("storm", "cowork", "best") and await self.git.status(untracked=False):
            await self._log(
                "Note: the main repo has uncommitted changes. Worktrees start from HEAD, so workers "
                "will NOT see them (and merges may be refused). Commit or stash first for best results."
            )
        if not self.git.is_git and mode in ("storm", "cowork"):
            await self._log(self.git.note)
        if self.sel.history_context:
            history_intro = (
                "[CONVERSATION CONTEXT - PREVIOUS TURNS IN THIS SESSION]\n"
                f"{self.sel.history_context.strip()}\n\n"
                "[CURRENT USER REQUEST]\n"
            )
            if mode != "chat" or not (self.sel.session_id or self.sel.continue_session):
                prompt = history_intro + prompt
        prompt, attached = expand_attachments(prompt, cwd)
        self._images = extract_images(prompt, cwd)
        if attached or self._images:
            await self._log("Attached: " + ", ".join(Path(a).name for a in [*attached, *map(str, self._images)]))
        run_id = self.broker.create_run(mode, prompt)
        self._run_id = run_id
        bus_task = asyncio.create_task(self._pump_bus(run_id))
        await self._log(f"Run {run_id} started in {mode.upper()} mode")
        summary = RunSummary(run_id, mode)
        audit(self.cfg, "run_start", run=run_id, mode=mode, cwd=str(cwd), sandbox=self._sandbox_on(), prompt=prompt[:200])
        sec = self.cfg.security
        if sec.snapshots and not self.git.is_git and mode not in READONLY_MODES:
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
                elif mode == "heal":
                    await self._heal(run_id, prompt, summary)
                elif mode == "architect":
                    await self._architect(run_id, prompt, summary)
                elif mode == "debate":
                    await self._debate(run_id, prompt, summary)
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
        finally:
            bus_task.cancel()
            for m in self.broker.get_messages(run_id, since_id=self._last_bus_id):
                self._last_bus_id = max(self._last_bus_id, int(m["id"]))
                await _emit(self.hooks.on_event, str(m["sender"]), "bus", m)
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
        elif res.ok and not self.sel.session_id and getattr(res, "session_id", ""):
            summary.session_id = res.session_id
        elif res.ok and not self.sel.session_id:
            sid = await find_session_id(self.cfg, cwd, stitle) if stitle else ""
            pat = self.cfg.opencode.session_id_pattern
            if not sid and pat:
                m = re.search(pat, strip_ansi(res.output + "\n" + res.errors))
                sid = (m.group(1) if m.groups() else m.group(0)) if m else ""
            summary.session_id = sid
        elif self.sel.session_id:
            summary.session_id = self.sel.session_id
        text = res.clean_output.strip()
        summary.result = text
        if looks_like_toolcall(text):
            await self._log("The model printed a tool call as plain text instead of running it. Small local models often cannot use tools "
                            "(try a model with tool support, and for Ollama raise num_ctx to 16k+).")
        if text:
            await _emit(self.hooks.on_result, self.sel.chat_id, text, "chat")
        (summary.merged if res.ok else summary.failed).append("chat")

    # ---------------------------------------------------- fan-out report modes
    def _limit(self, n: int) -> asyncio.Semaphore:
        requested = self.sel.target_agents
        cap = requested or self.cfg.max_parallel if (self.git.is_git or self.sel.mode not in ("storm", "cowork")) else 1
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
        if self.sel.target_agents > 0 and len(tasks) > self.sel.target_agents:
            tasks = tasks[:self.sel.target_agents]
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

        await gather_strict(*(work(t) for t in tasks))
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

    # ------------------------------------------------------------------ debate
    async def _debate(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        """Multi-round adversarial debate: debaters argue distinct stances in parallel, rebut each other, then a moderator decides."""
        cwd = self.git.workdir
        stances = [
            ("advocate", "Advocate (Pro / Bold)", "Champion the primary proposal or boldest architecture; highlight upside, speed and strategic value."),
            ("skeptic", "Skeptic (Risk / Edge Cases)", "Challenge hidden assumptions; expose failure modes, security/operational risks, complexity and cost."),
            ("pragmatist", "Pragmatist (Minimal / Simple)", "Advocate the simplest, lowest-maintenance alternative that solves 90% of the problem today."),
        ]
        if self.sel.target_agents > 0:
            if self.sel.target_agents == 1:
                stances = stances[:1]
            elif self.sel.target_agents == 2:
                stances = stances[:2]
            elif self.sel.target_agents >= 4:
                stances.append(("innovator", "Innovator (Lateral / Out-of-the-box)", "Propose novel, unconventional technical architectures and future-proof paradigms."))
            if len(stances) > self.sel.target_agents:
                stances = stances[:self.sel.target_agents]
        rounds = max(1, self.cfg.debate_rounds)
        sem = self._limit(len(stances))
        transcript: list[str] = []

        for rnd in range(1, rounds + 1):
            r_label = "Opening arguments" if rnd == 1 else f"Rebuttal round {rnd}"
            await self._log(f"Debate ({rnd}/{rounds}): {r_label}")
            round_outs: dict[str, str] = {}
            prior = "\n\n".join(transcript)

            async def speak(sid_slug: str, title: str, stance_desc: str) -> None:
                aid = f"{run_id}-{sid_slug}-r{rnd}"
                label = f"{title}" if rounds == 1 else f"{title} (R{rnd})"
                await self._announce(aid, label, "debater", IDLE)
                async with sem:
                    override = self.cfg.prompts.debater.replace("{stance}", f"{title} — {stance_desc}")
                    body = f"Topic / question:\n{prompt}"
                    if prior:
                        body += f"\n\n## Arguments from earlier rounds (rebut weak points and sharpen your case)\n{prior[-24_000:]}"
                    res = await self._agent(aid, label, "debater", body, cwd, prompt_override=override)
                    if res.ok and res.clean_output.strip():
                        round_outs[sid_slug] = res.clean_output.strip()
                    elif res.ok:
                        await self._set_status(aid, FAILED)

            await gather_strict(*(speak(s, t, d) for s, t, d in stances))
            if not round_outs:
                break
            block = f"## Round {rnd}: {r_label}\n\n" + "\n\n".join(
                f"### {t}\n{round_outs[s][-6000:]}" for s, t, _ in stances if s in round_outs
            )
            transcript.append(block)
            self.broker.add_note(run_id, f"round-{rnd}", "debate", block)

        if not transcript:
            summary.failed.append("debate")
            await self._log("debate: no debater produced output.")
            return

        mod_id = f"{run_id}-moderator"
        full_debate = "\n\n".join(transcript)
        res = await self._agent(mod_id, "Moderator Verdict", "moderator",
                                f"Original question:\n{prompt}\n\n{full_debate}", cwd)
        text = res.clean_output.strip() if res.ok else ""
        if not text:
            text = full_debate
            await self._log("debate: moderator failed - showing debate transcript.")
        summary.result = text
        self.broker.add_note(run_id, mod_id, "final", text)
        await _emit(self.hooks.on_result, mod_id, text, "debate")
        (summary.merged if res.ok else summary.failed).append("debate")

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
            except (asyncio.CancelledError, BudgetExceeded):
                raise
            except Exception as exc:  # keep the swarm alive when a single worker blows up
                await self._log(f"{t.title}: crashed: {exc!r}")
                await self._set_status(t.id, FAILED)
            finally:
                outcome[t.id] = ok
                events[t.id].set()

        await gather_strict(*(worker(t) for t in tasks))
        for t in tasks:
            (summary.merged if outcome.get(t.id) else summary.failed).append(t.title)

    async def _storm_worker(self, run_id: str, t: Subtask) -> bool:
        wt = await self.git.create_worktree(t.id)
        self.broker.update_task(t.id, status="running", branch=wt.branch, worktree=str(wt.path))
        context = self.broker.context_block(run_id, topics=("result",))
        body = t.prompt + (f"\n\n## Results of completed tasks\n{context}" if context else "")
        res = await self._agent(t.id, t.title, "coder", body, wt.path, hints=t.recommended_skills, final_status=None)
        await self.git.commit_all(wt, f"swarm: {t.title}")
        if not res.ok:
            if self.git.is_git and await self.git.has_changes(wt) and not res.errors.strip():
                await self._log(f"{t.title}: opencode exited {res.returncode}, but worktree has committed changes — continuing verification & merge")
            else:
                self.broker.update_task(t.id, status="failed", result=res.errors[-2000:])
                await self._log(f"{t.title}: opencode exited {res.returncode}; worktree kept at {wt.path}")
                return False
        self.broker.add_note(run_id, t.id, "result", f"{t.title}: {strip_ansi(res.output)[-1500:]}", t.id)
        ok, vout = await self._verify(t.id, wt.path)
        attempts = 0
        while not ok and attempts < self.cfg.fix_attempts:
            attempts += 1
            await self._log(f"{t.title}: verification failed - asking the coder to fix it ({attempts}/{self.cfg.fix_attempts})")
            fix = await self._agent(
                t.id, t.title, "coder",
                f"Your change fails the project's verification command (`{self.cfg.verify_command}`).\n\nOutput:\n{vout}\n\n"
                f"Fix it without breaking the task.\nOriginal task:\n{t.prompt}",
                wt.path, final_status=None,
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

        await gather_strict(*(cand(i) for i in range(1, n + 1)))
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
        await self.git.commit_all(wt, f"swarm: cowork round {rnd}")
        if not res.ok:
            if self.git.is_git and await self.git.has_changes(wt) and not res.errors.strip():
                await self._log(f"Round {rnd}: coder exited {res.returncode}, but worktree has committed changes — continuing verification.")
                await self._set_status(coder_id, DONE)
            else:
                await self._log(f"Round {rnd}: coder crashed (exit {res.returncode}); will retry if rounds remain.")
                self.broker.add_note(run_id, "coder", "feedback", "The previous coder run crashed; retry.")
                return False

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

    # ----------------------------------------------------------- heal & architect
    async def _heal(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        """Self-Heal & Auto-Repair pipeline: system doctor -> root-cause diagnostician -> coder fix -> verification."""
        from .healer import diagnose_and_heal

        cwd = self.sel.workdir or self.git.workdir
        await self._log("🩺 Running OpenSwarm Self-Heal & System Diagnostics...")
        sys_report = await diagnose_and_heal(self.cfg, cwd, self.pool, auto_fix=True)
        for fix in sys_report.get("fixes") or []:
            await self._log(f"✔ Auto-repaired: {fix}")

        diag_id = f"{run_id}-diagnostician"
        recent_txt = "\n".join(
            f"- [{i['title']}] {i['diagnosis']} ({i['snippet']})" for i in (sys_report.get("recent_issues") or [])
        )
        diag_body = (
            f"User request / issue:\n{prompt}\n\n"
            f"## System Self-Heal Report\nFixes applied: {', '.join(sys_report.get('fixes') or ['none needed'])}\n"
            + (f"Recent session errors:\n{recent_txt}\n" if recent_txt else "")
        )
        dres = await self._agent(diag_id, "Root-Cause Diagnostician", "diagnostician", diag_body, cwd)
        diagnosis = dres.clean_output.strip() if dres.ok else "Inspect the repository directly and fix the reported issue."
        self.broker.add_note(run_id, "diagnostician", "diagnosis", diagnosis)

        fix_id = f"{run_id}-healer"
        fix_body = (
            f"Original issue:\n{prompt}\n\n"
            f"## Root-Cause Diagnosis & Repair Plan\n{diagnosis}\n\n"
            "Implement the fix, run relevant tests/commands to verify it works, and summarize the root cause and fix."
        )
        fres = await self._agent(fix_id, "Auto-Healer & Verifier", "coder", fix_body, cwd)
        ok, vout = await self._verify(fix_id, cwd)
        if not ok and fres.ok and self.cfg.fix_attempts > 0:
            await self._log("Heal verification failed - running follow-up repair pass...")
            fres = await self._agent(
                fix_id,
                "Auto-Healer & Verifier",
                "coder",
                f"Verification (`{self.cfg.verify_command}`) failed:\n{vout}\n\nFix the remaining issue.",
                cwd,
            )
            ok, _ = await self._verify(fix_id, cwd)

        text = fres.clean_output.strip() if fres.ok else diagnosis
        if sys_report.get("fixes"):
            sys_block = "### 🩺 System Self-Heal Actions\n" + "\n".join(f"- {x}" for x in sys_report["fixes"]) + "\n\n"
            text = sys_block + text
        summary.result = text
        if text:
            await _emit(self.hooks.on_result, fix_id, text, "heal")
        (summary.merged if (fres.ok and ok) else summary.failed).append("heal")

    async def _architect(self, run_id: str, prompt: str, summary: RunSummary) -> None:
        """Architecture & Blueprint mode: parallel System Architect + Verification/Risk Strategist -> Blueprint Synthesizer."""
        cwd = self.git.workdir
        sem = self._limit(2)
        outs: dict[str, str] = {}

        async def run_part(slug: str, title: str, role: str, instructions: str) -> None:
            aid = f"{run_id}-{slug}"
            await self._announce(aid, title, role, IDLE)
            async with sem:
                res = await self._agent(aid, title, role, f"{instructions}\n\n## Request\n{prompt}", cwd)
                if res.ok and res.clean_output.strip():
                    outs[slug] = res.clean_output.strip()

        await gather_strict(
            run_part(
                "sys-arch",
                "System Architect",
                "architect",
                "Design the system architecture: module boundaries, data schemas, interfaces, Mermaid architecture/sequence diagrams, and 2-3 trade-off options.",
            ),
            run_part(
                "risk-test",
                "Risk & Verification Strategist",
                "reviewer",
                "Analyze failure modes, security/performance bottlenecks, edge cases, migration risks, and a concrete test/verification strategy. DO NOT edit files.",
            ),
        )
        if not outs:
            summary.failed.append("architect")
            return
        syn_id = f"{run_id}-blueprint"
        combined = "\n\n".join(f"### {k}\n{v}" for k, v in outs.items())
        sres = await self._agent(
            syn_id,
            "Blueprint Synthesizer",
            "synthesizer",
            f"Combine the architecture design and risk/verification strategy into a complete, actionable Architecture Blueprint (with Mermaid diagrams and ordered implementation milestones).\n\nOriginal request:\n{prompt}\n\n{combined}",
            cwd,
        )
        text = sres.clean_output.strip() if sres.ok else combined
        summary.result = text
        await _emit(self.hooks.on_result, syn_id, text, "architect")
        (summary.merged if sres.ok else summary.failed).append("architect")
