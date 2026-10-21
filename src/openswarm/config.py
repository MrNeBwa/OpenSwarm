"""YAML configuration + Pydantic schemas for OpenSwarm."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

CONFIG_NAME = "swarm_config.yaml"
ROLES = ("planner", "architect", "coder", "reviewer")


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
    planner: str = "opencode/deepseek-r1-free"
    architect: str = "opencode/deepseek-r1-free"
    coder: str = "opencode/qwen-2.5-coder-free"
    reviewer: str = "opencode/gemini-2.5-flash-free"


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
    attach_flag: str = "--attach"
    serve_command: list[str] = Field(default_factory=lambda: ["serve"])
    title_prefix: str = "openswarm"
    # Appended for follow-up chat turns so opencode resumes the previous session in that directory.
    continue_flag: str = "--continue"
    # Parallel chats: each chat resumes its own session by id. The id is scraped from the first turn's
    # output with this regex; if nothing matches, follow-ups fall back to continue_flag.
    session_flag: str = "--session"
    session_id_pattern: str = r"\bses_[A-Za-z0-9]+\b"


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

class SecuritySettings(BaseModel):
    sandbox: Literal["off", "bwrap"] = "off"  # bwrap = bubblewrap: read-only system, writable workdir only
    sandbox_hide: list[str] = Field(default_factory=lambda: [".ssh", ".gnupg", ".aws", ".config/gcloud", ".netrc", ".kube"])
    sandbox_extra_rw: list[str] = Field(default_factory=list)
    sandbox_network: bool = True
    allowed_roots: list[str] = Field(default_factory=list)  # empty = any directory
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


class SwarmConfig(BaseModel):
    default_mode: str = "chat"  # chat|storm|cowork|research|write|review|best|auto|wf:<workflow id>
    max_parallel: int = 8  # agents running at once; 0 = no limit (launch everything simultaneously)
    max_review_rounds: int = 3
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
        _sk("writer", "Technical writing", cat="writing",
            hint="Write clear structured documents: short sentences, headings, concrete examples, no filler."),
        _sk("concise", "Concise style", cat="writing",
            hint="Be concise: cut filler, prefer active voice, one idea per paragraph."),
        _sk("citations", "Cite sources", cat="research",
            hint="Support factual claims with sources (URLs) and clearly separate facts from your own inference."),
        _sk("factcheck", "Cross-check facts", cat="research",
            hint="Verify important claims with at least two independent sources and say when sources disagree or are weak."),
        _sk("data", "Data wrangling (CSV/JSON/SQL)", cat="data",
            hint="For tabular/JSON data use python (csv/json/sqlite3) or jq/awk via bash; show row counts and a sample before and after each transform."),
        _sk("sysadmin", "Safe system administration (Linux/Arch)", cat="ops", hint=(
            "Before any command that changes the system (pacman -S/-R, systemctl, rm, chmod/chown, editing /etc) state what it will do. "
            "Prefer read-only inspection first (pacman -Q/-Si, systemctl status, journalctl, ls, cat). Copy a config file to FILE.bak before editing it. "
            "Never run rm -rf outside the working directory; avoid sudo unless essential.")),
    ]


def builtin_presets() -> list[PresetSpec]:
    base = ["reply_lang", "report"]
    return [
        PresetSpec(id="dev", label="Developer", mode="chat", description="Write and fix code with tests",
                   skills=base + ["auto_approve", "bash", "search", "mcp", "careful", "tests_first", "git_flow", "docs"]),
        PresetSpec(id="dev_team", label="Dev team (parallel)", mode="storm", description="Split big changes across parallel workers",
                   skills=base + ["auto_approve", "bash", "search", "mcp", "careful", "tests_first", "security", "docs"]),
        PresetSpec(id="writer", label="Writer", mode="write", description="Articles, posts, docs: outline → parallel drafts → edit",
                   skills=base + ["writer", "concise", "search", "citations", "docs"]),
        PresetSpec(id="researcher", label="Researcher", mode="research", description="Parallel web/file research with a synthesized report",
                   skills=base + ["search", "citations", "factcheck", "docs"]),
        PresetSpec(id="reviewer", label="Code reviewer", mode="review", description="Parallel review lenses: correctness, security, performance, quality",
                   skills=base + ["search", "bash", "security", "perf"]),
        PresetSpec(id="analyst", label="Data analyst", mode="chat", description="CSV/Excel/JSON analysis",
                   skills=base + ["auto_approve", "data", "bash", "docs", "explain"]),
        PresetSpec(id="office", label="Office assistant", mode="chat", description="Edit Word/Excel/PowerPoint/PDF, draft documents",
                   skills=base + ["auto_approve", "docs", "writer", "concise"]),
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
    ]


def default_config() -> SwarmConfig:
    return SwarmConfig(
        models=[
            ModelSpec(id="opencode/deepseek-r1-free", label="DeepSeek R1", free=True, builtin=True),
            ModelSpec(id="opencode/gemini-2.5-flash-free", label="Gemini 2.5 Flash", free=True, builtin=True),
            ModelSpec(id="opencode/qwen-2.5-coder-free", label="Qwen 2.5 Coder", free=True, builtin=True),
            ModelSpec(id="anthropic/claude-sonnet-4-5", label="Claude Sonnet 4.5"),
            ModelSpec(id="openai/gpt-5", label="GPT-5"),
            ModelSpec(id="ollama/qwen2.5-coder:7b", label="Ollama: qwen2.5-coder 7B (local)"),
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
        return cfg
    except (yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"Invalid {path}: {exc}") from exc


_MODEL_RE = re.compile(r"[\w.\-]+/[\w.\-:/@]+")


async def discover_models(cfg: SwarmConfig) -> list[str]:
    """Ask `opencode models` which models are really available. Returns [] on any failure."""
    binary = shutil.which(cfg.opencode.binary) or cfg.opencode.binary
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "models",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return []
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return []
    lines = (ln.strip() for ln in out.decode(errors="replace").splitlines())
    return [ln for ln in lines if _MODEL_RE.fullmatch(ln)]


def reconcile_models(cfg: SwarmConfig, discovered: list[str]) -> None:
    """Merge the live `opencode models` list into the config (in memory only)."""
    if not discovered:
        return
    known = set(discovered)
    cfg.models = [m for m in cfg.models if not m.builtin or m.id in known]
    have = set(cfg.model_ids())
    cfg.models += [ModelSpec(id=mid) for mid in discovered if mid not in have]
    ids = cfg.model_ids()
    fallback = (
        next((m.id for m in cfg.models if m.free), None)
        or next((i for i in ids if i.startswith("opencode/")), None)
        or (ids[0] if ids else "")
    )
    for role in ROLES:
        if getattr(cfg.roles, role) not in ids:
            setattr(cfg.roles, role, fallback)
