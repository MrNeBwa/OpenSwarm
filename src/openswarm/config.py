"""YAML configuration + Pydantic schemas for OpenSwarm."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

CONFIG_NAME = "swarm_config.yaml"
ROLES = ("planner", "architect", "coder", "reviewer", "diagnostician", "chat")


class ConfigError(RuntimeError):
    pass


class ModelSpec(BaseModel):
    id: str
    label: str = ""
    free: bool = False
    # Shipped defaults are dropped if `opencode models` is reachable and does not list them.
    builtin: bool = False

    @model_validator(mode="after")
    def _default_label(self) -> "ModelSpec":
        if not self.label:
            self.label = self.id
        return self

    def display(self) -> str:
        return f"{self.label}  [free]" if self.free else self.label


class RoleModels(BaseModel):
    planner: str = "opencode/beam-free"
    architect: str = "opencode/beam-free"
    coder: str = "opencode/deepseek-v4.1-flash-free"
    reviewer: str = "opencode/nemotron-3.5-super-vl-free"
    diagnostician: str = "opencode/beam-free"
    chat: str = "opencode/deepseek-v4.1-flash-free"


class SkillSpec(BaseModel):
    """A toggle in the TUI. Adds CLI args / env vars / a prompt hint to every worker."""

    id: str
    label: str
    default: bool = False
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    prompt_hint: str = ""
    category: str = "general"


class PresetSpec(BaseModel):
    """One-click persona: a mode + a set of skills (+ optional model overrides by role)."""

    id: str
    label: str
    description: str = ""
    mode: str = "chat"
    skills: list[str] = Field(default_factory=list)


class OpencodeSettings(BaseModel):
    binary: str = "opencode"
    subcommand: list[str] = Field(default_factory=lambda: ["run"])
    model_flag: str = "--model"
    extra_args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Per-role permission JSON passed through OPENCODE_PERMISSION (planner/architect must not edit).
    role_permissions: dict[str, str] = Field(
        default_factory=lambda: {
            "planner": '{"edit":"deny"}',
            "architect": '{"edit":"deny"}',
            "reviewer": '{"edit":"deny","bash":"allow"}',
            "lens": '{"edit":"deny","bash":"allow"}',
            "researcher": '{"edit":"deny","webfetch":"allow"}',
            "outliner": '{"edit":"deny"}',
            "drafter": '{"edit":"deny","webfetch":"allow"}',
            "editor": '{"edit":"deny"}',
            "synthesizer": '{"edit":"deny"}',
            "judge": '{"edit":"deny"}',
            "debater": '{"edit":"deny","webfetch":"allow"}',
            "moderator": '{"edit":"deny"}',
            "diagnostician": '{"edit":"deny","bash":"allow"}',
            "wfro_planner": '{"edit":"deny","webfetch":"allow"}',
            "wfro_coder": '{"edit":"deny","webfetch":"allow"}',
            "wfro_reviewer": '{"edit":"deny","bash":"allow"}',
        }
    )
    permission_env: str = "OPENCODE_PERMISSION"
    # Files/dirs from the root repo made available inside every worktree (skills, MCP, rules, env).
    inherit_paths: list[str] = Field(
        default_factory=lambda: [
            ".opencode",
            "opencode.json",
            "opencode.jsonc",
            "AGENTS.md",
            ".mcp.json",
            ".env",
            ".env.local",
        ]
    )
    link_mode: Literal["symlink", "copy"] = "symlink"
    discover_models: bool = True
    timeout_seconds: int = 1800
    # Speed: keep a warm `opencode serve` per chat directory and `run --attach` to it (no MCP cold boot).
    use_server: bool = True
    server_start_timeout: int = 25
    # Resilience: retry agents that hit rate limits / overload with exponential backoff.
    retries: int = 2
    retry_backoff_seconds: int = 6
    rate_limit_pattern: str = r"rate.?limit|\b429\b|overloaded|quota|too many requests|temporarily unavailable|try again"
    agent_flag: str = "--agent"
    role_agents: dict[str, str] = Field(default_factory=dict)  # e.g. {"planner": "plan"}
    title_flag: str = "--title"
    use_api: bool = True  # chat via opencode serve HTTP API (permissions, tool cards, usage); falls back to `run`
    api_timeout_minutes: int = 30
    api_idle_poll_seconds: int = 120
    file_flag: str = "--file"
    attach_flag: str = "--attach"  # older opencode; newer versions call it --server (auto-detected)
    server_flag: str = "--server"
    standalone_flag: str = "--standalone"  # newer opencode: run with a private server instead of the background service
    auto_flag: str = "--auto"  # newer opencode: auto-approve permissions that are not explicitly denied
    serve_command: list[str] = Field(default_factory=lambda: ["serve"])
    title_prefix: str = "openswarm"
    # Appended for follow-up chat turns so opencode resumes the previous session in that directory.
    continue_flag: str = "--continue"
    # Parallel chats: each chat resumes its own session by id. The id is scraped from the first turn's
    # output with this regex; if nothing matches, follow-ups fall back to continue_flag.
    session_flag: str = "--session"
    session_id_pattern: str = r"\bses_[A-Za-z0-9]+\b"
    variant_flag: str = "--variant"  # model variant / reasoning effort flag
    thinking_flag: str = "--thinking"  # display thinking blocks in output
    default_effort: str = "medium"  # none|minimal|low|medium|high|xhigh|max


class Prompts(BaseModel):
    planner: str = (
        "You are the PLANNER of a software-engineering swarm. Analyse the repository and the request, "
        "then split the work into subtasks that can be done by independent engineers working in separate "
        "git worktrees. Prefer subtasks that touch disjoint files. DO NOT modify any files.\n"
        "Respond with ONLY a JSON array (no prose, no markdown fences). Each element must be an object with "
        'keys: "id" (short kebab-case string), "title" (short), "prompt" (a complete, self-contained '
        'instruction for the engineer), "recommended_skills" (array of strings, may be empty) and '
        '"depends_on" (array of ids that must finish first; usually empty).'
    )
    architect: str = (
        "You are the ARCHITECT. Analyse the repository and the request and write a concrete implementation "
        "spec: files to create/change, interfaces, edge cases and how to test. DO NOT modify any files. "
        "Respond with the spec in Markdown."
    )
    coder: str = (
        "You are a senior engineer (CODER). Implement the task below directly in the current working "
        "directory using your tools (edit files, run commands, run tests). Keep changes focused. "
        "When finished, reply with a short summary of what you changed."
    )
    reviewer: str = (
        "You are the REVIEWER. Inspect the diff provided below (you may run `git diff` yourself), run the "
        "project's tests/linters with your bash tool when available, and judge whether the change satisfies "
        "the request and spec. DO NOT edit files. End your answer with exactly one line: "
        "`VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`, preceded by concrete, actionable feedback "
        "when changes are requested."
    )


    research_planner: str = (
        "You are a RESEARCH LEAD. Split the question into 3-6 distinct, non-overlapping research angles that "
        "together answer it. DO NOT modify files. Respond with ONLY a JSON array of objects with keys "
        '"id" (kebab-case), "title" and "prompt" (a precise, self-contained research instruction).'
    )
    researcher: str = (
        "You are a RESEARCHER. Investigate the angle below using your search/web tools and the local files. "
        "Cross-check claims across sources, note uncertainty, and cite sources with URLs. Reply with concise "
        "findings in Markdown. DO NOT modify files."
    )
    outliner: str = (
        "You are an EDITOR planning a piece of writing. Produce an outline of 3-8 sections. DO NOT modify files. "
        'Respond with ONLY a JSON array of objects with keys "id", "title" and "prompt" (what this section must '
        "cover, tone, approximate length)."
    )
    drafter: str = (
        "You are a WRITER. Write ONLY the section described below, in polished prose with a consistent voice. "
        "Do not add a title for the whole piece and do not repeat other sections. DO NOT modify files."
    )
    editor: str = (
        "You are the final EDITOR. Merge the drafted sections below into one coherent piece: unify tone, remove "
        "repetition, fix transitions, add a title and a short intro/outro if needed. Output the finished text only."
    )
    synthesizer: str = (
        "You are the SYNTHESIZER. Combine the findings below into one well-structured answer: a short summary first, "
        "then details with headings, resolve conflicts between sources explicitly, list sources, and flag open questions."
    )
    judge: str = (
        "You are an impartial JUDGE. Several candidate solutions to the same request are shown as diffs. Judge correctness first, "
        "then simplicity and fit with the codebase. DO NOT edit files. Explain briefly, then end with exactly one line: WINNER: <number>."
    )
    lens: str = (
        "You are a code REVIEWER with ONE focus: {lens}. Inspect the repository (use `git diff`/`git log` when available), "
        "run tests or linters with your bash tool if useful. DO NOT edit files. Report concrete findings as a list: "
        "severity (high/medium/low), file:line, problem, suggested fix."
    )
    debater: str = (
        "You are a DEBATER advocating for ONE perspective: {stance}. Build the strongest, most concrete case for this position "
        "(cite evidence, code, numbers, failure modes and counter-arguments to rival views). In rebuttal rounds, directly address "
        "weaknesses in the other debaters' arguments. DO NOT edit files."
    )
    moderator: str = (
        "You are an impartial MODERATOR and decision-maker. Review the multi-round debate below, weigh the strongest arguments and "
        "rebuttals from each side, expose weak claims, and deliver a clear, actionable final verdict with trade-offs and next steps. "
        "DO NOT edit files."
    )
    diagnostician: str = (
        "You are the DIAGNOSTICIAN. Inspect the error logs, test output, git status, and relevant files using your bash/search tools. "
        "Pinpoint the exact root cause (file, line, function, and why it fails) and write a concise, step-by-step repair plan for the engineer. "
        "DO NOT edit files."
    )


class SecuritySettings(BaseModel):
    sandbox: Literal["off", "bwrap"] = "off"  # bwrap = bubblewrap: read-only system, writable workdir only
    sandbox_hide: list[str] = Field(default_factory=lambda: [".ssh", ".gnupg", ".aws", ".config/gcloud", ".netrc", ".kube"])
    sandbox_extra_rw: list[str] = Field(default_factory=list)
    sandbox_network: bool = True
    allowed_roots: list[str] = Field(default_factory=list)  # empty = any directory
    confine_agents: bool = True  # tell every agent which directory it works in and not to wander outside it without asking
    audit: bool = True
    snapshots: bool = True  # tar snapshot before in-place runs outside git (enables "Undo")
    snapshot_limit_mb: int = 200
    snapshot_keep: int = 10
    permission_default: Literal["reject", "once"] = "reject"  # answer when nobody responds in time
    permission_timeout_s: int = 600


class WorkflowStep(BaseModel):  # noqa: E501
    id: str
    kind: Literal["agent", "parallel", "foreach"] = "agent"
    role: str = "coder"  # which model slot: planner | coder | reviewer
    prompt: str = ""  # may use {request} and {<earlier step id>}; foreach also {item}
    steps: list["WorkflowStep"] = Field(default_factory=list)  # for kind=parallel
    over: str = ""  # foreach: id of an earlier step whose output holds a JSON array or one item per line
    readonly: bool = True
    result: bool = False  # this step's output is the final answer


try:
    WorkflowStep.model_rebuild()
except AttributeError:  # pragma: no cover
    pass


class WorkflowSpec(BaseModel):
    id: str
    label: str
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)


class ScheduleSpec(BaseModel):
    """Runs while `openswarm web` is running. `every`: "30m" | "2h" | "daily 09:00"."""

    id: str
    every: str
    prompt: str
    workdir: str = "."
    mode: str = "chat"
    preset: str = ""


class TriggerSpec(BaseModel):
    """Runs when a file matching `glob` appears/changes in `watch`. {file} is available in the prompt."""

    id: str
    watch: str
    glob: str = "*"
    prompt: str
    mode: str = "chat"
    preset: str = ""
    debounce_s: int = 5


class OpenRouterSettings(BaseModel):
    """Several OpenRouter keys: used for models `openrouter/...`, switched automatically on limit / no credits / bad key."""

    api_keys: list[str] = Field(default_factory=list)  # also: env OPENROUTER_API_KEYS="k1,k2,k3" (and OPENROUTER_API_KEY)
    strategy: Literal["failover", "round_robin"] = "failover"  # failover: stay on a key until it fails; round_robin: spread load
    rate_limit_cooldown_seconds: int = 60  # a rate-limited key rests this long
    quota_cooldown_seconds: int = 3600  # a key without credits / daily quota rests this long
    max_wait_seconds: int = 120  # if every key is resting, wait at most this long for the first one
    model_prefix: str = "openrouter/"
    env_var: str = "OPENROUTER_API_KEY"  # variable opencode reads the key from
    keys_env_var: str = "OPENROUTER_API_KEYS"


class VpnSettings(BaseModel):
    """Per-app VPN: a private xray instance (proxy mode, no root, system routing untouched) that only OpenSwarm's
    opencode agents use via HTTP(S)_PROXY. When an agent hits a provider limit the exit server is swapped."""

    enabled: bool = False  # default; the UI / `openswarm vpn on|off` toggle is remembered in the vpn state file
    provider: Literal["ninjavpn"] = "ninjavpn"
    ninjavpn_dir: str = ""  # empty = use embedded openswarm.vpn_engine & bundled 65 servers
    python: str = ""  # optional external python interpreter override
    xray_path: str = ""  # empty = `xray` on PATH
    http_port: int = 18089  # 127.0.0.1 only; separate from the system NinjaVPN ports (8080/1080)
    socks_port: int = 18088
    servers: list[str] = Field(default_factory=list)  # server ids to rotate through; empty = all ninjavpn servers
    exclude: list[str] = Field(default_factory=list)
    order: Literal["sequential", "shuffle"] = "shuffle"
    cooldown_seconds: int = 1800  # a server that hit a limit is skipped this long
    settle_delay_seconds: float = 0.8  # brief pause after rotation for socket handshakes to settle
    verify_on_rotate: bool = True  # verify connectivity, latency and exit IP after rotation
    max_rotations_per_request: int = 8
    rotate_pattern: str = (
        r"rate.?limit|\b429\b|too many requests|quota|usage limit|limit (?:reached|exceeded)|exceeded[^\n]{0,40}limit"
        r"|free (?:tier|usage)|requests per (?:minute|hour|day)|\b403\b[^\n]{0,60}(?:region|country|location)"
    )
    health_target: str = "www.gstatic.com:443"  # CONNECTed through the tunnel to prove the exit works
    no_proxy: str = "localhost,127.0.0.1,::1"


class SwarmConfig(BaseModel):
    default_mode: str = "chat"  # chat|storm|cowork|research|write|review|best|debate|heal|architect|auto|wf:<workflow id>
    priority: str = "balanced"  # balanced|quality|fast|free|local
    effort: str = "medium"  # default|none|minimal|low|medium|high|xhigh|max
    auto_heal: bool = True  # automatic self-healing of git locks, servers, VPN and broken flags
    auto_model_failover: bool = True  # automatic failover to the next best healthy model on provider/model failure
    max_parallel: int = 8  # agents running at once; 0 = no limit (launch everything simultaneously)
    max_review_rounds: int = 3
    debate_rounds: int = 2
    # Optional shell command run inside each worktree before merging (e.g. "pytest -q"). Empty = skip.
    verify_command: str = ""
    cleanup_on_success: bool = True
    models: list[ModelSpec] = Field(default_factory=list)
    roles: RoleModels = Field(default_factory=RoleModels)
    skills: list[SkillSpec] = Field(default_factory=list)
    presets: list[PresetSpec] = Field(default_factory=list)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    workflows: list[WorkflowSpec] = Field(default_factory=list)
    schedules: list[ScheduleSpec] = Field(default_factory=list)
    triggers: list[TriggerSpec] = Field(default_factory=list)
    fix_attempts: int = 2  # verify_command failures are fed back to the coder this many times
    best_of_n: int = 3
    max_agents_per_run: int = 40
    run_timeout_minutes: int = 60
    review_lenses: list[str] = Field(
        default_factory=lambda: [
            "correctness and edge cases",
            "security (injection, secrets, unsafe permissions)",
            "performance and scalability",
            "readability, structure and test coverage",
        ]
    )
    opencode: OpencodeSettings = Field(default_factory=OpencodeSettings)
    openrouter: OpenRouterSettings = Field(default_factory=OpenRouterSettings)
    vpn: VpnSettings = Field(default_factory=VpnSettings)
    prompts: Prompts = Field(default_factory=Prompts)

    def model_ids(self) -> list[str]:
        return [m.id for m in self.models]

    def skills_by_id(self, ids: list[str]) -> list[SkillSpec]:
        wanted = set(ids)
        return [s for s in self.skills if s.id in wanted]


def _sk(id: str, label: str, hint: str = "", cat: str = "general", default: bool = False, env: dict | None = None) -> SkillSpec:
    return SkillSpec(id=id, label=label, prompt_hint=hint, category=cat, default=default, env=env or {})


def builtin_skills() -> list[SkillSpec]:
    """Toggles shown in the UI. Each adds a guidance line to every agent's prompt (and optionally env/args)."""
    return [
        _sk("auto_approve", "Auto-approve edits/bash (no prompts)", cat="tools", default=True,
            env={"OPENCODE_PERMISSION": '{"edit":"allow","bash":"allow","webfetch":"allow"}'}),
        _sk("docs", "Office & PDF files (.docx .xlsx .pptx .pdf)", cat="tools", default=True, hint=(
            "For .docx/.xlsx/.pptx/.pdf files never unzip or parse XML by hand. Use your bash tool: "
            "`openswarm doc read FILE`, `openswarm doc replace FILE OLD NEW`, "
            "`openswarm doc append FILE TEXT [--style Heading1]` (docx), `openswarm doc set FILE Sheet1!B3 VALUE` (xlsx), "
            "`openswarm doc create FILE.docx` (text on stdin; # headings, - bullets, **bold**). "
            "Read a file before editing it and read it again afterwards to verify.")),
        _sk("bash", "Shell & tests", cat="tools", default=True,
            hint="Use your bash tool to run builds and tests before declaring the task done."),
        _sk("search", "Code search / web fetch", cat="tools", default=True,
            hint="Use your search/grep/glob and webfetch tools to research before editing."),
        _sk("mcp", "MCP servers & custom skills", cat="tools", default=True,
            hint="Use any MCP servers and custom skills configured for this project when relevant."),
        _sk("team_chat", "Swarm chat & non-blocking questions", cat="tools", default=True, hint=(
            "You can communicate with teammate agents and the user via the Swarm Room bus (`openswarm msg`): "
            "broadcast updates or handoffs with `openswarm msg send \"message\" --to all`, read peer messages with `openswarm msg read`, "
            "and ask questions without stopping work using `openswarm msg ask \"question?\" --to user --default \"sensible default assumption\"` "
            "(or add `--wait 15` to briefly wait for a live reply before continuing with the default).")),
        _sk("reply_lang", "Reply in my language", cat="general", default=True,
            hint="Always answer in the language of the user's last message (keep code, paths and commands unchanged)."),
        _sk("planmode", "Plan first, then act", cat="general",
            hint="Start with a short numbered plan, then execute it step by step and tick items off."),
        _sk("explain", "Explain as you go", cat="general",
            hint="Briefly explain your reasoning and what each command does, as if teaching a colleague."),
        _sk("report", "End with a summary report", cat="general", default=True,
            hint="Finish with a short summary: what changed, which files were touched, how to verify, open issues."),
        _sk("careful", "Minimal, focused edits", cat="code",
            hint="Keep changes minimal and focused; do not reformat or touch unrelated files; match the existing style."),
        _sk("tests_first", "Test-driven development", cat="code",
            hint="Write or update a failing test first, then implement until it passes; run the whole suite at the end."),
        _sk("refactor", "Safe refactoring", cat="code",
            hint="Refactor in small behaviour-preserving steps; run tests after each step; never mix refactors with features."),
        _sk("security", "Security awareness", cat="code",
            hint="Watch for injection, unsafe file/permission handling and secrets; never print, log or commit credentials."),
        _sk("perf", "Performance mindset", cat="code",
            hint="Measure before optimising; prefer algorithmic wins; mention complexity and memory trade-offs."),
        _sk("git_flow", "Clean git history", cat="code",
            hint="Make small logical commits with conventional-commit messages when asked to commit; never force-push."),
        _sk("codedoc", "Document the code", cat="code",
            hint="Add docstrings/comments where intent is not obvious and update README/usage docs affected by the change."),
        _sk("type_strict", "Strict typing & schemas", cat="code",
            hint="Use explicit type annotations, dataclasses/Pydantic models and narrow types; avoid untyped dicts or Any where a concrete type fits."),
        _sk("error_resilience", "Defensive error handling", cat="code",
            hint="Handle edge cases, timeouts, I/O errors and malformed inputs explicitly; fail with actionable error messages rather than silent swallows."),
        _sk("api_design", "Clean API & CLI contracts", cat="code",
            hint="Keep public functions, REST endpoints and CLI flags consistent, backwards-compatible, validated and clearly documented."),
        _sk("concurrency", "Async & concurrency safety", cat="code",
            hint="Avoid race conditions, deadlocks and leaked background tasks; cancel child tasks cleanly and guard shared state with locks."),
        _sk("a11y_ux", "Accessible, responsive UI/UX", cat="code",
            hint="Build accessible UIs: semantic HTML, keyboard navigation, focus states, ARIA labels, dark/light contrast and reduced-motion support."),
        _sk("i18n", "Localization & i18n readiness", cat="code",
            hint="Keep user-facing strings clean, Unicode/UTF-8 safe, locale-aware and easy to translate without hardcoded layout assumptions."),
        _sk("writer", "Technical writing", cat="writing",
            hint="Write clear structured documents: short sentences, headings, concrete examples, no filler."),
        _sk("concise", "Concise style", cat="writing",
            hint="Be concise: cut filler, prefer active voice, one idea per paragraph."),
        _sk("diagrams", "Mermaid & ASCII diagrams", cat="writing",
            hint="Include clear Mermaid (`flowchart`, `sequenceDiagram`, `architecture`) or ASCII diagrams when explaining architecture, workflows or data flow."),
        _sk("citations", "Cite sources", cat="research",
            hint="Support factual claims with sources (URLs) and clearly separate facts from your own inference."),
        _sk("factcheck", "Cross-check facts", cat="research",
            hint="Verify important claims with at least two independent sources and say when sources disagree or are weak."),
        _sk("socratic", "Challenge assumptions & trade-offs", cat="research",
            hint="Question hidden assumptions, compare at least 2-3 alternatives with explicit pros/cons, and highlight failure modes before concluding."),
        _sk("data", "Data wrangling (CSV/JSON/SQL)", cat="data",
            hint="For tabular/JSON data use python (csv/json/sqlite3) or jq/awk via bash; show row counts and a sample before and after each transform."),
        _sk("sql_expert", "SQL & database optimization", cat="data",
            hint="Write safe, parameterized SQL; inspect schemas and query plans (`EXPLAIN`), use transactions and indexes, and avoid N+1 queries."),
        _sk("sysadmin", "Safe system administration (Linux/Arch)", cat="ops", hint=(
            "Before any command that changes the system (pacman -S/-R, systemctl, rm, chmod/chown, editing /etc) state what it will do. "
            "Prefer read-only inspection first (pacman -Q/-Si, systemctl status, journalctl, ls, cat). Copy a config file to FILE.bak before editing it. "
            "Never run rm -rf outside the working directory; avoid sudo unless essential.")),
        _sk("devops", "Containers, CI/CD & systemd", cat="ops",
            hint="Follow production DevOps practices: reproducible builds, minimal non-root containers, fast CI stages, health checks and clear rollback steps."),
    ]


def builtin_presets() -> list[PresetSpec]:
    base = ["reply_lang", "report", "team_chat"]
    return [
        PresetSpec(id="dev", label="Developer", mode="chat", description="Write and fix code with tests",
                   skills=base + ["auto_approve", "bash", "search", "mcp", "careful", "tests_first", "git_flow", "docs"]),
        PresetSpec(id="healer", label="Self-Heal & Fix", mode="heal", description="Diagnose errors, auto-repair system & codebase, and verify fixes",
                   skills=base + ["auto_approve", "bash", "search", "mcp", "careful", "tests_first", "error_resilience"]),
        PresetSpec(id="blueprint", label="Architecture Blueprint", mode="architect", description="Deep system design, Mermaid diagrams, trade-offs & implementation plan",
                   skills=base + ["search", "planmode", "diagrams", "socratic", "api_design", "type_strict"]),
        PresetSpec(id="dev_team", label="Dev team (parallel)", mode="storm", description="Split big changes across parallel workers",
                   skills=base + ["auto_approve", "bash", "search", "mcp", "careful", "tests_first", "security", "docs"]),
        PresetSpec(id="architect", label="System architect", mode="cowork", description="Architect spec → coder → reviewer with diagrams & trade-offs",
                   skills=base + ["auto_approve", "bash", "search", "planmode", "diagrams", "socratic", "api_design", "type_strict"]),
        PresetSpec(id="sec_auditor", label="Security auditor", mode="review", description="Deep vulnerability, auth, secrets & injection audit",
                   skills=base + ["search", "bash", "security", "error_resilience", "factcheck"]),
        PresetSpec(id="debugger", label="Bug hunter", mode="best", description="Parallel root-cause hypotheses, verified & judged",
                   skills=base + ["auto_approve", "bash", "search", "careful", "tests_first", "error_resilience", "explain"]),
        PresetSpec(id="debater", label="Council debate", mode="debate", description="Adversarial multi-perspective debate with moderator verdict",
                   skills=base + ["search", "socratic", "citations", "factcheck", "concise"]),
        PresetSpec(id="writer", label="Writer", mode="write", description="Articles, posts, docs: outline → parallel drafts → edit",
                   skills=base + ["writer", "concise", "search", "citations", "docs"]),
        PresetSpec(id="researcher", label="Researcher", mode="research", description="Parallel web/file research with a synthesized report",
                   skills=base + ["search", "citations", "factcheck", "docs"]),
        PresetSpec(id="reviewer", label="Code reviewer", mode="review", description="Parallel review lenses: correctness, security, performance, quality",
                   skills=base + ["search", "bash", "security", "perf"]),
        PresetSpec(id="analyst", label="Data analyst", mode="chat", description="CSV/Excel/JSON/SQL analysis",
                   skills=base + ["auto_approve", "data", "sql_expert", "bash", "docs", "explain"]),
        PresetSpec(id="office", label="Office assistant", mode="chat", description="Edit Word/Excel/PowerPoint/PDF, draft documents",
                   skills=base + ["auto_approve", "docs", "writer", "concise"]),
        PresetSpec(id="devops", label="DevOps engineer", mode="chat", description="CI/CD, Docker, systemd & infrastructure automation",
                   skills=base + ["auto_approve", "devops", "sysadmin", "bash", "security", "careful"]),
        PresetSpec(id="sysadmin", label="Sysadmin (careful)", mode="chat", description="Linux administration with confirmations (auto-approve OFF)",
                   skills=["reply_lang", "sysadmin", "bash", "explain", "careful", "planmode"]),
    ]


def builtin_workflows() -> list[WorkflowSpec]:
    S = WorkflowStep
    return [
        WorkflowSpec(
            id="article", label="Article with fact-check", description="Draft → fact-check → final edit",
            steps=[
                S(id="draft", role="coder", prompt="Write a complete, well-structured article for this request:\n{request}"),
                S(id="facts", role="planner", prompt="Fact-check the draft below. List every doubtful or wrong claim with a correction and a source URL if possible.\n\nDRAFT:\n{draft}"),
                S(id="final", role="planner", result=True, prompt="Rewrite the draft fixing every issue from the fact-check. Output the final article only.\n\nDRAFT:\n{draft}\n\nFACT-CHECK:\n{facts}"),
            ],
        ),
        WorkflowSpec(
            id="deep_dive", label="Deep dive (angles → parallel → brief)", description="List angles, research each in parallel, write an executive brief",
            steps=[
                S(id="angles", role="planner", prompt='List 4-6 research angles for the request below as a JSON array of strings, nothing else.\n{request}'),
                S(id="research", kind="foreach", over="angles", role="coder", prompt="Research this angle in depth, cite sources.\nAngle: {item}\nContext: {request}"),
                S(id="brief", role="planner", result=True, prompt="Write a one-page executive brief from these findings.\n\n{research}"),
            ],
        ),
        WorkflowSpec(
            id="security_audit", label="Security audit (surface → parallel probes → report)", description="Map attack surface, audit categories in parallel, produce prioritized remediation report",
            steps=[
                S(id="surface", role="planner", prompt="Map the codebase's attack surface and list 4-5 specific security audit areas as a JSON array of strings, nothing else.\nContext: {request}"),
                S(id="probes", kind="foreach", over="surface", role="reviewer", prompt="Audit this area for vulnerabilities (injection, auth, secrets, traversal, permissions). Report concrete file:line findings.\nArea: {item}\nRequest: {request}"),
                S(id="report", role="planner", result=True, prompt="Synthesize a prioritized security audit report (Critical/High/Medium/Low) with exact locations, exploit scenarios and remediations.\n\n{probes}"),
            ],
        ),
        WorkflowSpec(
            id="bug_hunt", label="Bug hunt (hypotheses → parallel investigation → root cause)", description="Generate root-cause hypotheses, investigate in parallel, deliver verified fix plan",
            steps=[
                S(id="hypotheses", role="planner", prompt='List 3-4 distinct, testable root-cause hypotheses for the bug/issue below as a JSON array of strings, nothing else.\n{request}'),
                S(id="checks", kind="foreach", over="hypotheses", role="reviewer", prompt="Investigate this hypothesis in the repository (inspect code, git log, tests). Gather evidence for or against it.\nHypothesis: {item}\nIssue: {request}"),
                S(id="diagnosis", role="planner", result=True, prompt="Weigh the evidence from all investigations, identify the true root cause, and provide a concrete fix + regression test plan.\n\n{checks}"),
            ],
        ),
        WorkflowSpec(
            id="spec_to_pr", label="Spec → Architecture & Test Plan → Review", description="Draft product spec, design architecture and test plan in parallel, synthesize implementation blueprint",
            steps=[
                S(id="spec", role="planner", prompt="Write a concise product & technical specification for:\n{request}"),
                S(id="design", kind="parallel", steps=[
                    S(id="arch", role="planner", prompt="Design the module boundaries, data structures and API contracts for this spec:\n{spec}"),
                    S(id="tests", role="reviewer", prompt="Design a comprehensive test and verification plan (unit, integration, edge cases) for this spec:\n{spec}"),
                    S(id="risks", role="reviewer", prompt="Identify security, performance and migration risks for this spec and how to mitigate them:\n{spec}"),
                ]),
                S(id="blueprint", role="planner", result=True, prompt="Combine the spec, architecture, test plan and risk mitigations into one actionable implementation blueprint.\n\nSPEC:\n{spec}\n\nDESIGN:\n{design}"),
            ],
        ),
    ]


def default_config() -> SwarmConfig:
    return SwarmConfig(
        models=[
            ModelSpec(id="google/gemini-3.5-flash", label="Google Gemini 3.5 Flash · OpenCode (1M)", free=True, builtin=True),
            ModelSpec(id="google/gemini-3.1-flash-lite", label="Google Gemini 3.1 Flash-Lite · OpenCode", free=True, builtin=True),
            ModelSpec(id="google/gemini-3-flash-preview", label="Google Gemini 3 Flash Preview · OpenCode (1M)", free=True, builtin=True),
            ModelSpec(id="google/gemini-flash-latest", label="Google Gemini Flash Latest · OpenCode (1M)", free=True, builtin=True),
            ModelSpec(id="google/gemini-pro-latest", label="Google Gemini Pro Latest · OpenCode (2M)", free=True, builtin=True),
            ModelSpec(id="ollama/qwen2.5-coder:7b", label="Ollama: qwen2.5-coder 7B (local · OpenCode)", free=True, builtin=True),
            ModelSpec(id="ollama/deepseek-r1:8b", label="Ollama: deepseek-r1 8B (local · OpenCode)", free=True, builtin=True),
            ModelSpec(id="deepseek-harness/deepseek-flash", label="DeepSeek V4.1 Flash · DeepSeek Harness (Free Unlimited 1M)", free=True, builtin=False),
            ModelSpec(id="opencode/deepseek-v4.1-flash-free", label="DeepSeek V4.1 Flash Free", free=True, builtin=True),
            ModelSpec(id="opencode/beam-free", label="Beam Free (1M ctx)", free=True, builtin=True),
            ModelSpec(id="opencode/nemotron-3.5-super-vl-free", label="Nemotron 3.5 Super VL Free (1M ctx)", free=True, builtin=True),
            ModelSpec(id="opencode/flint-40b-free", label="Flint 40B Free", free=True, builtin=True),
            ModelSpec(id="opencode/nemotron-3.5-lightning-free", label="Nemotron 3.5 Lightning Free", free=True, builtin=True),
            ModelSpec(id="opencode/mimo-v2.6-flash-free", label="MiMo-V2.6-Flash Free", free=True, builtin=True),
            ModelSpec(id="opencode/glm-5.3-flash-free", label="GLM-5.3-Flash Free", free=True, builtin=True),
            ModelSpec(id="opencode/nemotron-3-ultra-free", label="Nemotron 3 Ultra Free (1M ctx)", free=True, builtin=True),
            ModelSpec(id="opencode/big-pickle", label="Big Pickle", free=True, builtin=True),
            ModelSpec(id="kilo/cohere/north-mini-code:free", label="Cohere North Mini Code Free · Kilo Gateway (256K)", free=True, builtin=False),
            ModelSpec(id="kilo/nvidia/nemotron-3.5-super-vl:free", label="NVIDIA Nemotron 3.5 Super VL Free · Kilo Gateway (1M)", free=True, builtin=False),
            ModelSpec(id="kilo/reflection/beam-free", label="Reflection Beam 1.0 Free · Kilo Gateway (1M)", free=True, builtin=False),
            ModelSpec(id="kilo/nvidia/nemotron-3.5-lightning:free", label="NVIDIA Nemotron 3.5 Lightning Free · Kilo Gateway (1M)", free=True, builtin=False),
            ModelSpec(id="kilo/kilo-auto/free", label="Kilo Auto Free Router · Kilo Gateway (256K)", free=True, builtin=False),
            ModelSpec(id="kilo/openrouter/free", label="OpenRouter Free Router · Kilo Gateway (200K)", free=True, builtin=False),
            ModelSpec(id="kilo/nvidia/nemotron-3-ultra-550b-a55b:free", label="NVIDIA Nemotron 3 Ultra 550B Free · Kilo Gateway (256K)", free=True, builtin=False),
            ModelSpec(id="kilo/stealth/glyph-cluster", label="Stealth Glyph Cluster Free · Kilo Gateway (256K)", free=True, builtin=False),
            ModelSpec(id="anthropic/claude-sonnet-4-5", label="Claude Sonnet 4.5"),
            ModelSpec(id="openai/gpt-5", label="GPT-5"),
        ],
        skills=builtin_skills(),
        presets=builtin_presets(),
        workflows=builtin_workflows(),
    )


def global_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "openswarm" / CONFIG_NAME


def find_config_path(workdir: Path) -> Path:
    """A project-local swarm_config.yaml wins; otherwise the global one (never litters other dirs)."""
    local = workdir / CONFIG_NAME
    return local if local.exists() else global_config_path()


def state_dir_for(workdir: Path) -> Path:
    """Where run history lives when the directory is not a git repo (keeps system dirs clean)."""
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "openswarm" / "state" / hashlib.sha1(str(workdir).encode()).hexdigest()[:12]


def write_config(cfg: SwarmConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def load_config(path: Path) -> SwarmConfig:
    """Load swarm_config.yaml, generating the default file on first run."""
    if not path.exists():
        cfg = default_config()
        write_config(cfg, path)
        return cfg
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg = SwarmConfig.model_validate(raw)
        have = {sk.id for sk in cfg.skills}
        cfg.skills += [sk for sk in builtin_skills() if sk.id not in have]  # new releases add new toggles
        have_p = {p.id for p in cfg.presets}
        cfg.presets += [p for p in builtin_presets() if p.id not in have_p]
        have_w = {w.id for w in cfg.workflows}
        cfg.workflows += [w for w in builtin_workflows() if w.id not in have_w]
        deprecated_kilo = {"kilo/poolside/laguna-s-2.1:free", "kilo/recursive/flint-v1.0:free"}
        cfg.models = [m for m in cfg.models if m.id not in deprecated_kilo]
        have_m = {m.id for m in cfg.models}
        if "deepseek-harness/deepseek-flash" not in have_m:
            cfg.models.insert(
                0,
                ModelSpec(
                    id="deepseek-harness/deepseek-flash",
                    label="DeepSeek V4.1 Flash · DeepSeek Harness (Free Unlimited 1M)",
                    free=True,
                    builtin=False,
                ),
            )
        for km in default_config().models:
            if km.id.startswith("kilo/") and km.id not in have_m:
                cfg.models.append(km)
                have_m.add(km.id)
        return cfg
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid {path}: {exc}") from exc


_MODEL_RE = re.compile(r"[\w.\-]+/[\w.\-:/@]+")
_DISCOVER_CACHE: dict[str, tuple[float, list[str]]] = {}


async def discover_models(cfg: SwarmConfig, max_age: float = 600.0, timeout: float = 4.0) -> list[str]:
    """Ask `opencode models` which models are really available. Cached in-memory for max_age seconds."""
    now = time.monotonic()
    cached = _DISCOVER_CACHE.get(cfg.opencode.binary)
    if cached and (now - cached[0] < max_age):
        return list(cached[1])

    from .keys import configured_keys  # lazy: keys imports config
    from .vpn import _reap_stale_opencode_service

    try:
        _reap_stale_opencode_service()
    except Exception:
        pass

    binary = shutil.which(cfg.opencode.binary) or cfg.opencode.binary
    env = dict(os.environ)
    keys = configured_keys(cfg)
    if keys:  # lets opencode list the openrouter/* models
        env[cfg.openrouter.env_var] = keys[0]
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "models",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return list(cached[1]) if cached else []
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return list(cached[1]) if cached else []
    lines = (ln.strip() for ln in out.decode(errors="replace").splitlines())
    res = [ln for ln in lines if _MODEL_RE.fullmatch(ln)]
    if res:
        _DISCOVER_CACHE[cfg.opencode.binary] = (now, res)
    elif cached:
        return list(cached[1])
    return res


def reconcile_models(cfg: SwarmConfig, discovered: list[str]) -> None:
    """Merge the live `opencode models` list into the config (in memory only)."""
    if not discovered:
        return
    from .router import get_router, is_free_model

    known = set(discovered)
    cfg.models = [m for m in cfg.models if not m.builtin or m.id in known]
    have = set(cfg.model_ids())
    cfg.models += [ModelSpec(id=mid, free=is_free_model(mid)) for mid in discovered if mid not in have]
    ids = set(cfg.model_ids())
    router = get_router()
    for role in ROLES:
        if getattr(cfg.roles, role) not in ids:
            best = router.pick_best_for_role(cfg, role, getattr(cfg, "priority", "balanced"))
            if best:
                setattr(cfg.roles, role, best)
