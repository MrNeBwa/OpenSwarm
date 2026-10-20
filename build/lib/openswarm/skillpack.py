"""Native opencode skills (SKILL.md) bundled with OpenSwarm + discovery of the ones you already have.

Native skills are loaded by opencode *on demand* (progressive disclosure), so they cost far fewer tokens than
pasting guidance into every prompt. Install once: `openswarm skills install`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BUNDLE: dict[str, tuple[str, str]] = {
    "openswarm-office": (
        "Read and edit Word, Excel, PowerPoint and PDF files from the shell with the `openswarm doc` tool.",
        """# Office & PDF files

Never unzip or hand-edit the XML of .docx/.xlsx/.pptx. Use `openswarm doc`:

- `openswarm doc read FILE` - text/markdown (xlsx: grid, pptx: per slide, pdf: text). `--sheet NAME`, `--pages 2-5`, `--max-rows N`.
- `openswarm doc replace FILE OLD NEW [--count N]` - works even when the text is split across formatting runs.
- `openswarm doc append FILE.docx "text" [--style Heading1]` - add paragraph(s) at the end.
- `openswarm doc set FILE.xlsx "Sheet1!B3" VALUE` - number, text, or `=formula`.
- `printf '# Title\\n- point\\n**bold**' | openswarm doc create FILE.docx` - new document.
- `openswarm doc info FILE`.

Workflow: read first, make the smallest edit, read again to verify, and report exactly what changed.
PDFs are read-only: edit the source document and re-export. A `.bak` copy is kept outside git repos.
""",
    ),
    "openswarm-research": (
        "Method for deep, cited research: split the question, gather sources, cross-check, synthesize with uncertainty.",
        """# Deep research

1. Restate the question and list 3-6 angles that together answer it.
2. For each angle search broadly, then open the best primary sources (docs, papers, official sites) rather than summaries.
3. Cross-check every important claim with a second independent source; note disagreements and dates.
4. Separate facts (with URL) from inference (label it).
5. Output: short answer first, then findings by angle, then a sources list and open questions.
Never invent a citation; say "not found" instead.
""",
    ),
    "openswarm-writing": (
        "Workflow for articles, docs and posts: audience, outline, draft, edit pass; clear structure and voice.",
        """# Writing workflow

1. Pin down audience, goal, tone, length. If missing, assume sensible defaults and state them.
2. Outline: thesis, 3-8 sections, one idea each.
3. Draft section by section in a consistent voice: concrete examples over abstractions, short sentences, active voice.
4. Edit pass: cut filler, fix transitions, check facts and names, make the opening and the ending strong.
5. Deliver the finished text only (no meta commentary) unless asked for notes. Use `openswarm doc create` for .docx output.
""",
    ),
    "openswarm-code-review": (
        "Checklist for reviewing code changes: correctness, security, performance, tests, readability; actionable findings.",
        """# Code review

Inspect `git diff`/`git log` (or the files directly). Check, in order: correctness and edge cases, error handling,
security (injection, secrets, permissions), performance/complexity, tests (missing or weak), readability and naming.
Run the test suite or linters when available. Report each finding as: severity (high/medium/low), file:line,
problem, suggested fix. Do not edit files unless asked. End with a verdict and the top 3 things to fix.
""",
    ),
    "openswarm-sysadmin": (
        "Safe Linux/Arch administration playbook: inspect first, announce changes, back up configs, avoid destructive commands.",
        """# Safe system administration (Linux / Arch)

- Inspect before changing: `pacman -Q/-Qi/-Si`, `systemctl status`, `journalctl -xe`, `ls`, `cat`, `df -h`, `ss -tulpn`.
- Before any change (pacman -S/-R, systemctl enable/restart, chmod/chown, editing /etc, rm) say what it does and why.
- Copy a config to `FILE.bak` before editing; show a diff afterwards.
- Never `rm -rf` outside the working directory; never pipe unknown scripts into a shell; avoid sudo unless required.
- Prefer reversible steps; after the change verify (service status, logs) and report how to roll back.
""",
    ),
    "openswarm-data-analysis": (
        "Analyse CSV/JSON/SQLite/Excel data with python or shell tools; show samples, counts and checks at each step.",
        """# Data analysis

Load with python (csv/json/sqlite3; for .xlsx use `openswarm doc read`). First print shape, columns, dtypes and 5 sample rows.
Validate: duplicates, nulls, ranges. For each transform print before/after row counts. Keep the analysis in a script file so it is
reproducible, write results to a new file instead of overwriting the source, and finish with the key numbers and caveats.
""",
    ),
    "openswarm-tdd-refactor": (
        "Test-driven development and safe refactoring playbook.",
        """# TDD and refactoring

Feature: write a failing test, make it pass with the simplest code, then clean up; run the full suite at the end.
Refactor: first make sure tests cover the behaviour; change structure in small steps, run tests after each step,
never mix refactoring with behaviour changes. Keep commits small and meaningful.
""",
    ),
}


def _dirs(workdir: Path | None) -> list[tuple[str, Path]]:
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    out: list[tuple[str, Path]] = []
    if workdir:
        out += [("project", workdir / ".opencode" / "skill"), ("project", workdir / ".opencode" / "skills"),
                ("project", workdir / ".claude" / "skills")]
    out += [("global", xdg / "opencode" / "skill"), ("global", xdg / "opencode" / "skills"),
            ("global", home / ".opencode" / "skills"), ("global", home / ".claude" / "skills"),
            ("global", home / ".agents" / "skills")]
    return out


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    meta: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v:
                meta[k.strip()] = v.strip().strip("\"'")
    return meta


def discover_skills(workdir: Path | None = None) -> list[dict[str, str]]:
    """List native skills visible to opencode (project, global, Claude-compatible, npx-skills dirs)."""
    seen: set[str] = set()
    found: list[dict[str, str]] = []
    for scope, base in _dirs(workdir):
        if not base.is_dir():
            continue
        for md in sorted(base.glob("**/SKILL.md")):
            try:
                meta = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = meta.get("name") or md.parent.name
            if name in seen:
                continue
            seen.add(name)
            found.append({"name": name, "description": meta.get("description", ""), "scope": scope, "path": str(md)})
    return found


def install_skills(project_dir: Path | None = None, force: bool = False) -> list[str]:
    """Write the bundled skills to ~/.config/opencode/skill (or <project>/.opencode/skill). Returns names written."""
    if project_dir:
        base = project_dir / ".opencode" / "skill"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode" / "skill"
    written = []
    for name, (desc, body) in BUNDLE.items():
        target = base / name / "SKILL.md"
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}", encoding="utf-8")
        written.append(name)
    return written
