# OpenSwarm

Multi-agent TUI orchestrator around the [`opencode`](https://opencode.ai) CLI. It runs several `opencode`
instances in isolated git worktrees, in parallel (**Storm**) or as an architect → coder → reviewer loop (**Cowork**).

## Install (Arch Linux)

```bash
./install.sh            # pacman deps, opencode (repo/npm/official script), venv, ~/.local/bin/openswarm
opencode auth login     # once; free opencode models need no key
cd anywhere             # any directory: git repo or not (even ~ or /etc)
openswarm               # TUI, starts in Chat mode
openswarm --headless --mode chat "why is my wifi dropping?"      # add -c to continue the last session
openswarm --headless --mode cowork "add a --verbose flag to the CLI"
openswarm --doctor      # check git / opencode / config
openswarm --list-models # what `opencode models` reports
```

Standalone use of the script: `OPENSWARM_SRC_URL=<git url> bash install.sh`. Remove with `./install.sh --uninstall`.

Manual install: `pip install .` (Python 3.11+).

## Native window + activity sphere (Linux)

```bash
openswarm gui                 # a real application window (also in your app menu: "OpenSwarm")
openswarm gui --check         # which backend is used and what is missing
openswarm gui --gpu           # GPU compositing (faster, but a grey window on some drivers); software rendering is the default
openswarm desktop-install     # (re)create the icon + menu entry
```
Backends, in order: **GTK4 + WebKitGTK 6.0** (`sudo pacman -S python-gobject gtk4 webkitgtk-6.0`; installed by `install.sh`,
the venv is created with `--system-site-packages` so it can see GTK) → a chromeless **Chromium/Chrome/Brave** app window →
your browser. External links open in your default browser; desktop notifications ("run finished") go through the system.

**The sphere** (left sidebar, collapsible) shows what is going on at a glance: a point-cloud globe whose **colour = mode**
(Chat blue, Auto violet, Storm gold, Best-of-N pink, Cowork teal, Research green, Write orange, Review rose, workflows magenta),
**speed/brightness = activity**, one **orbiting dot per agent** (yellow running, green done, red failed, grey queued), a pulse ring on
every burst of output, a **green flash** on success, **red** on failure and a slow **amber** breathing arc while it waits for your
permission. Mode-specific motion: Research scans, Write ripples, Review wears shield rings, Cowork counter-rotates two shells.
Below it: phase, running/done/failed counts, the current tool call, elapsed time and tokens/cost (Russian or English by locale).
It honours `prefers-reduced-motion`, pauses when the window is hidden, idles at ~24 fps and lowers its detail by itself on slow GPUs.

## Web app (the same UI in any browser)

```bash
openswarm web --app      # standalone window (Chromium/Chrome/Brave); closing it stops the server
openswarm web            # or the private URL in your normal browser
```
Also installed as **OpenSwarm** in your app menu. Session sidebar with persistent history, mode dropdown, models,
parallelism (`All at once`), skills popover with one-click presets, live agent cards (tool calls, streaming logs), markdown
answers with copy/download and **▶ Preview** for ```html / ```svg blocks (sandboxed iframe), file browser/preview,
drag-and-drop upload, `@file` attachments (docs inlined, images sent as real attachments), 📝 **Instructions** (edits the
project's `AGENTS.md`, which opencode reads), 🧾 **Audit** viewer, 🔒 **Sandbox** toggle, token/cost meter, **↩ Undo this run**
and **± Show changes** after runs. **Many sessions run at the same time.**

## Modes

| Mode | What happens | Needs git? |
|---|---|---|
| **Chat** | One agent, continuous session in any directory (warm server + HTTP API, session resumed by id) | no |
| **Auto** | Picks Chat/Storm/Research/Write/Review from your request (cheap heuristic, no extra LLM call) | no |
| **Storm** | Planner → parallel workers in worktrees → verify (failures are fed back to the coder) → merge | optional |
| **Best of N** | N independent attempts → verify → a judge picks one → only the winner is merged | yes |
| **Cowork** | Architect → coder → reviewer loop until approved | optional |
| **Debate** | Multi-round adversarial debate (Advocate / Skeptic / Pragmatist) → Moderator verdict | no |
| **Research** | Planner → N parallel read-only researchers → synthesized, cited report | no |
| **Write** | Outline → parallel section drafts → editor merges | no |
| **Review** | One parallel reviewer per lens → prioritized report | no |
| **Workflows** | Built-in (`article`, `deep_dive`, `security_audit`, `bug_hunt`, `spec_to_pr`) + your own pipelines from `workflows:` in the config | no |

`Parallel` = agents running at once (default 8, **0 = all simultaneously**). Rate-limited agents are retried with backoff;
`max_agents_per_run` and `run_timeout_minutes` cap runaway runs.

## OpenRouter with several API keys

Easiest: click **🔑 OpenRouter keys** at the bottom of the left sidebar, paste keys (one per line), done. The window shows each key's
state (ready / resting / disabled), **Check** asks OpenRouter for its balance, **Use again** ends a pause early. Keys added there are stored in
`~/.config/openswarm/openrouter_keys.json` (mode 600), not in the project. Alternatively put the keys in `swarm_config.yaml` (or `export OPENROUTER_API_KEYS="k1,k2,k3"`) and pick `openrouter/<vendor>/<model>` models:

```yaml
openrouter:
  api_keys: [sk-or-v1-aaa, sk-or-v1-bbb, sk-or-v1-ccc]
  strategy: failover      # or round_robin
```
Every agent starts with one key (passed to opencode as `OPENROUTER_API_KEY`). If a run fails with a rate limit (429), no credits /
daily quota (402) or an invalid key (401), that key rests (`rate_limit_cooldown_seconds` / `quota_cooldown_seconds`, or is disabled
for good if rejected) and the same request is repeated **immediately** with the next key - in Chat (HTTP API and `run`) and in every
multi-agent mode. Keys are never written to logs (only `#2 (…abcd)`). Each key gets its own warm opencode server. Don't also run
`opencode auth login` for openrouter - a key stored there would override these.

## Safety

- **Permissions UI:** in Chat the opencode HTTP API asks for permission and the web UI shows *Allow once / Always / Deny*.
  With `auto-approve` on (default skill) agents act without asking - turn it off for careful work (Sysadmin preset does).
- **Sandbox (opt-in):** `security.sandbox: bwrap` or the 🔒 toggle runs agents in bubblewrap: whole system read-only, only the
  project (+opencode state) writable, `~/.ssh ~/.gnupg ~/.aws …` hidden. Fails closed if bubblewrap is missing.
  `openswarm doctor --live` smoke-tests it on your machine.
- **Undo:** outside git a tar snapshot is taken before in-place runs (≤ 200 MB) → **Undo this run**. In git use **Show changes**
  or normal git tools. `openswarm snapshots list|restore ID`.
- **Audit log:** runs, agents (model/sandbox), permission decisions, undo → `~/.local/state/openswarm/audit.log`.
- **allowed_roots:** limit which directories sessions may use. Warm servers are password-protected; the web app needs its token.

## Scripting

```bash
openswarm ask "compare Postgres and SQLite for my case" --mode research --json     # result as JSON on stdout
OPENSWARM_TOKEN=secret openswarm web --no-open --port 8787                          # fixed token for HTTP clients
curl -H "X-Token: secret" http://127.0.0.1:8787/api/state                           # REST + SSE API used by the UI
```
Schedules and file triggers (`schedules:` / `triggers:` in the config) run while `openswarm web` is open.

## Verify your setup

```bash
openswarm selftest            # 49 offline tests against a bundled fake opencode (~30 s)
openswarm --doctor --live     # checks YOUR opencode: run flags, serve, password, HTTP API, sessions, sandbox, helper tools
```
Run `--doctor --live` first: it confirms the assumptions OpenSwarm makes about opencode's CLI/API on your version.

## Speed

Warm password-protected `opencode serve` per chat directory (no MCP cold boot), HTTP API instead of re-parsing CLI output,
massive parallelism, strong model only for planning/judging and cheap ones for workers (remembered per role).

## Swarm Room: Inter-Agent Chat & Non-Blocking Questions

Agents share a live SQLite-backed message bus (`Swarm Room`) so they can coordinate with each other and ask you questions **without stopping their work** (Claude-style non-blocking questions):

```bash
openswarm msg send "API contract ready at /v1/users" --to all --kind handoff
openswarm msg ask "Use JWT or session cookies?" --to user --default "Proceeding with JWT"
openswarm msg ask "Which port does the auth service use?" --to architect --wait 10 --default "8080"
openswarm msg reply 1 "Use HTTP-only session cookies"
openswarm msg read
```

- **Web / GUI:** Click **💬 Swarm Room** in the top bar to view the live inter-agent feed and post messages (`@all`, `@planner`, `@coder`, `@reviewer`, or any active agent). When an agent asks a question, a **Non-blocking Question Card** appears directly in the conversation stream so you can reply anytime or click *Keep default assumption*.
- **TUI:** Switch to the **💬 Team Chat** tab or type `/bus <message>` (`@agent message`) or `/reply <id> <answer>` in the prompt box while agents are running.

## Skills, Presets & Workflows

(1) **33 skill toggles** add guidance lines (and optional CLI args/env) to every prompt; (2) **13 one-click presets** (`dev`, `dev_team`, `architect`, `sec_auditor`, `debugger`, `debater`, `writer`, `researcher`, `reviewer`, `analyst`, `office`, `devops`, `sysadmin`); (3) **15 native opencode skills** (`SKILL.md`) are loaded on demand and cost almost no tokens: `openswarm skills install` installs them; `openswarm skills list` and `openswarm skills show <name>` inspect them; `openswarm presets` and `openswarm workflows` list all personas and pipelines.

**Memory:** mode, role models, parallelism and skill toggles are saved to `~/.local/state/openswarm/state.json` whenever you change them (`--no-memory` / `--reset-memory`).

## Documents

`openswarm doc read|replace|append|set|create|table|image|export|ocr|info`: `.docx/.xlsx/.pptx` creation (`create FILE.docx|xlsx|pptx`) and edits are done on the XML text (styles and formulas stay intact; verified by round-tripping through LibreOffice), `table`/`image` add tables and pictures to Word files, `export` converts (`md↔docx`, `csv↔xlsx`, `md↔pptx` built in; office→PDF via LibreOffice; more via pandoc), `ocr` uses tesseract.
Optional tools: `sudo pacman -S pandoc libreoffice-fresh tesseract tesseract-data-eng tesseract-data-rus` (or `OPENSWARM_EXTRAS=1 ./install.sh`).
Not supported: editing PDFs, Excel charts, tables nested in tables, text boxes.

## Ready-made extensions worth knowing (not installed automatically - review before enabling)

`opencode-skills`, `oh-my-opencode` (multi-agent orchestration), `cc-safety-net` (blocks destructive git/file commands - a good
companion for the Sysadmin preset). Add under `"plugin"` in `opencode.json`; OpenSwarm workers inherit that config.

## Troubleshooting

- **The model answers with a JSON blob like `{"name":"shell","arguments":{...}}` and nothing happens.** The model printed a tool call as text
  instead of making it: small local models (e.g. a 7B coder under Ollama) often cannot drive opencode's tools, and Ollama's default context
  (4k) cuts the tool definitions off. OpenSwarm warns about this. Use a model with tool support, and for Ollama raise `num_ctx` to 16k+.
- **`Unrecognized flag: --attach`.** Newer opencode versions call it `--server` and have a built-in background service. OpenSwarm detects the
  flags from `opencode run --help` (and learns from rejections), so nothing to configure; `openswarm --doctor --live` shows what it found.
- **"HTTP API unavailable"** appears in the agent log with the reason; chat then continues through `opencode run`. Paste the
  `openswarm --doctor --live` output if you want that path (permission buttons, tool cards, token meter) fixed for your version.
- **Blank window:** `openswarm gui --safe-render`. **Which window backend?** `openswarm gui --check`.

## How it works

| Piece | File | Role |
|---|---|---|
| Config | `config.py`, `swarm_config.yaml` | Pydantic schema, auto-generated defaults, live model discovery |
| Runner | `runner.py` | `asyncio` subprocess wrapper, streams stdout/stderr line by line, kills the process group on cancel/timeout |
| Git | `git_manager.py` | `git worktree add -b swarm/<id> .swarm/worktrees/<id> HEAD`, injects `.opencode/` etc., commit/diff/merge/cleanup |
| Broker | `broker.py` | SQLite (`.swarm/swarm.db`): runs, task DAG state, shared notes (spec, results, review feedback) |
| Orchestrator | `orchestrator.py` | Storm (planner → JSON plan → DAG-scheduled parallel workers → merge) and Cowork |
| TUI | `tui.py`, `styles.tcss` | Sidebar (mode, models, skills, agent tree), one live log tab per agent, launch bar |

**Storm:** the planner (read-only) returns a JSON list of subtasks (`id`, `title`, `prompt`, `recommended_skills`,
optional `depends_on`). Workers run up to `max_parallel` at a time, each in its own worktree. A finished worker is
committed, optionally verified (`verify_command`), then merged into your current branch under a lock. Dependent
tasks start only after their dependencies have merged. Conflicting or failing branches are kept for inspection.

**Cowork:** the architect writes a spec into the broker → the coder implements it in one worktree → the reviewer gets
the diff, runs tests, and ends with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`. Feedback goes back to the coder
for up to `max_review_rounds`; approval triggers the merge.

**Skills pass-through:** workers keep your real environment (so `~/.config/opencode` global config applies) and
get your project's `.opencode/`, `opencode.json`, `AGENTS.md`, `.mcp.json` and `.env*` symlinked (or copied) into each
worktree. They are added to `.git/info/exclude`, so they are never committed.

## Things to know

- **Git repos only (Storm/Cowork):** worktrees start from `HEAD`, so uncommitted work is invisible to workers; commit or stash first.
- **Outside git there is no isolation or undo.** With auto-approve on, an agent can run any command with your user's rights. For system work, consider turning auto-approve off or using a throwaway directory.
- **Not a 1:1 opencode clone.** OpenSwarm drives `opencode run`, so interactive permission prompts, the session picker and slash commands are not replicated.
- **Verify the opencode assumptions** (all overridable under `opencode:` in the YAML): the command shape
  `opencode run --model provider/model "<prompt>"`, `opencode models` for discovery, and the `OPENCODE_PERMISSION`
  env var for auto-approve / read-only roles. The default free-model IDs come from your spec and may not match what
  your opencode version offers; with discovery on, missing built-in ones are replaced by what `opencode models` lists.
- Keep **auto-approve** on for unattended runs; otherwise opencode may wait for permissions that nobody can answer.
- The planner/architect run in your main checkout with edits denied; OpenSwarm warns if its `git status` changed anyway.
- TUI keys: `Enter` launch, `Ctrl+N` new chat, `Alt+←/→` switch tab, `Ctrl+X` cancel, `Ctrl+R` refresh models, `Ctrl+Q` quit.
