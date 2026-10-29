"""Plugins, MCP Servers, Lifecycle Hooks & Cross-Agent Skill Sync (Claude Code / Antigravity / OpenCode).

Manages:
1. MCP server integrations (`.mcp.json` and `opencode.json`) with a 1-click catalog of popular MCP servers.
2. Cross-ecosystem skill & plugin discovery (Antigravity `~/.gemini/config/plugins`, Claude Code `~/.claude/skills`,
   OpenCode `~/.config/opencode/skill`) and 1-click import/creation of custom `SKILL.md` plugins.
3. Lifecycle hooks (`auto_self_heal`, `auto_model_failover`, `git_auto_prune`, `desktop_notify`, plus custom shell hooks).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .skillpack import BUNDLE, discover_skills, install_skills

MCP_CATALOG: list[dict[str, Any]] = [
    {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "category": "reasoning",
        "description": "Structured step-by-step reflective reasoning & hypothesis refinement for complex tasks.",
        "command": ["npx", "-y", "@modelcontextprotocol/server-sequential-thinking"],
        "env": {},
    },
    {
        "id": "context7",
        "name": "Context7 Docs",
        "category": "docs",
        "description": "Up-to-date official documentation & code examples for libraries and frameworks.",
        "command": ["npx", "-y", "@upstash/context7-mcp"],
        "env": {},
    },
    {
        "id": "memory",
        "name": "Knowledge Graph Memory",
        "category": "memory",
        "description": "Persistent entity-relation knowledge graph memory across agent sessions.",
        "command": ["npx", "-y", "@modelcontextprotocol/server-memory"],
        "env": {},
    },
    {
        "id": "fetch",
        "name": "Web Fetch & Markdown",
        "category": "web",
        "description": "Fetches web pages and converts HTML into clean, LLM-friendly Markdown.",
        "command": ["uvx", "mcp-server-fetch"],
        "env": {},
    },
    {
        "id": "git",
        "name": "Git Inspector",
        "category": "dev",
        "description": "Rich Git repository history, blame, diff, and branch analysis tools.",
        "command": ["uvx", "mcp-server-git"],
        "env": {},
    },
    {
        "id": "filesystem",
        "name": "Filesystem Access",
        "category": "dev",
        "description": "Structured local filesystem operations, directory trees, and file metadata.",
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
        "env": {},
    },
    {
        "id": "playwright",
        "name": "Playwright Browser",
        "category": "browser",
        "description": "Browser automation, DOM inspection, screenshots, and E2E web testing.",
        "command": ["npx", "-y", "@playwright/mcp@latest"],
        "env": {},
    },
    {
        "id": "github",
        "name": "GitHub API",
        "category": "dev",
        "description": "Manage GitHub Pull Requests, Issues, Repositories, and Actions workflows.",
        "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
        "env": {},
    },
]

BUILTIN_HOOKS: list[dict[str, Any]] = [
    {
        "id": "auto_self_heal",
        "name": "Auto Self-Heal on Error",
        "event": "on_error",
        "default": True,
        "description": "Automatically diagnose and fix stale servers, git locks, broken flags, and VPN tunnels during a run.",
    },
    {
        "id": "auto_model_failover",
        "name": "Smart Model Failover",
        "event": "on_model_error",
        "default": True,
        "description": "Automatically switch to the next highest-ranked healthy model if the chosen model is broken, missing, or exhausted.",
    },
    {
        "id": "git_auto_prune",
        "name": "Git Worktree & Lock Guard",
        "event": "pre_run",
        "default": True,
        "description": "Clean stale .git/index.lock files and prune orphaned git worktrees before parallel runs.",
    },
    {
        "id": "desktop_notify",
        "name": "Desktop Notifications",
        "event": "post_run",
        "default": False,
        "description": "Send a native Linux desktop notification (notify-send) when a swarm run completes.",
    },
]


def plugins_state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "openswarm" / "plugins.json"


def load_plugins_state() -> dict[str, Any]:
    p = plugins_state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {
        "hooks": {h["id"]: bool(h["default"]) for h in BUILTIN_HOOKS},
        "custom_hooks": [],
    }


def save_plugins_state(data: dict[str, Any]) -> None:
    p = plugins_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def hook_enabled(hook_id: str) -> bool:
    st = load_plugins_state()
    hooks = st.get("hooks") or {}
    default = next((bool(h["default"]) for h in BUILTIN_HOOKS if h["id"] == hook_id), True)
    return bool(hooks.get(hook_id, default))


def set_hook(hook_id: str, enabled: bool) -> dict[str, Any]:
    st = load_plugins_state()
    hooks = st.setdefault("hooks", {h["id"]: bool(h["default"]) for h in BUILTIN_HOOKS})
    hooks[hook_id] = bool(enabled)
    save_plugins_state(st)
    return st


# ------------------------------------------------------------------ MCP servers
def _mcp_file(workdir: Path) -> Path:
    return workdir / ".mcp.json"


def _opencode_file(workdir: Path) -> Path:
    return workdir / "opencode.json"


def list_mcp_servers(workdir: Path) -> dict[str, dict[str, Any]]:
    """Read configured MCP servers from `<workdir>/.mcp.json` and `opencode.json`."""
    servers: dict[str, dict[str, Any]] = {}
    mcp_path = _mcp_file(workdir)
    if mcp_path.is_file():
        try:
            raw = json.loads(mcp_path.read_text(encoding="utf-8"))
            for k, v in (raw.get("mcpServers") or {}).items():
                if isinstance(v, dict):
                    cmd = [v.get("command", "")] + list(v.get("args") or []) if v.get("command") else list(v.get("command_list") or [])
                    servers[k] = {"id": k, "command": [x for x in cmd if x], "env": v.get("env") or {}, "enabled": not v.get("disabled", False), "source": ".mcp.json"}
        except (OSError, ValueError):
            pass
    oc_path = _opencode_file(workdir)
    if oc_path.is_file():
        try:
            raw = json.loads(oc_path.read_text(encoding="utf-8"))
            for k, v in (raw.get("mcp") or {}).items():
                if isinstance(v, dict) and k not in servers:
                    cmd = v.get("command") if isinstance(v.get("command"), list) else [str(v.get("command") or "")]
                    servers[k] = {"id": k, "command": [x for x in cmd if x], "env": v.get("environment") or {}, "enabled": bool(v.get("enabled", True)), "source": "opencode.json"}
        except (OSError, ValueError):
            pass
    return servers


def set_mcp_server(workdir: Path, server_id: str, command: list[str], env: dict[str, str] | None = None, enabled: bool = True) -> dict[str, Any]:
    """Configure or toggle an MCP server in both `.mcp.json` (Claude/Antigravity) and `opencode.json` (OpenCode)."""
    server_id = re.sub(r"[^\w\-]", "-", server_id.strip()).strip("-")
    if not server_id or not command:
        raise ValueError("server_id and command are required")
    env = env or {}

    # 1. Update .mcp.json
    mcp_path = _mcp_file(workdir)
    mcp_data: dict[str, Any] = {}
    if mcp_path.is_file():
        try:
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            mcp_data = {}
    mcp_servers = mcp_data.setdefault("mcpServers", {})
    if enabled:
        mcp_servers[server_id] = {"command": command[0], "args": command[1:], **({"env": env} if env else {})}
    else:
        mcp_servers.pop(server_id, None)
    mcp_path.write_text(json.dumps(mcp_data, indent=2) + "\n", encoding="utf-8")

    # 2. Update opencode.json
    oc_path = _opencode_file(workdir)
    oc_data: dict[str, Any] = {}
    if oc_path.is_file():
        try:
            oc_data = json.loads(oc_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            oc_data = {}
    oc_mcp = oc_data.setdefault("mcp", {})
    if enabled:
        oc_mcp[server_id] = {"type": "local", "command": command, "enabled": True, **({"environment": env} if env else {})}
    else:
        oc_mcp.pop(server_id, None)
    oc_path.write_text(json.dumps(oc_data, indent=2) + "\n", encoding="utf-8")
    return list_mcp_servers(workdir)


def toggle_catalog_mcp(workdir: Path, server_id: str, enabled: bool | None = None) -> dict[str, Any]:
    current = list_mcp_servers(workdir)
    is_on = server_id in current and current[server_id].get("enabled", True)
    target = (not is_on) if enabled is None else bool(enabled)
    cat = next((c for c in MCP_CATALOG if c["id"] == server_id), None)
    if not cat and server_id not in current:
        raise ValueError(f"Unknown MCP server: {server_id}")
    cmd = cat["command"] if cat else current[server_id]["command"]
    env = cat.get("env", {}) if cat else current[server_id].get("env", {})
    return set_mcp_server(workdir, server_id, cmd, env, enabled=target)


# ------------------------------------------- Cross-ecosystem skill/plugin discovery
def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    meta: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v:
                meta[k.strip()] = v.strip().strip("\"'")
    return meta


def discover_external_plugins(workdir: Path | None = None) -> list[dict[str, str]]:
    """Discover skills/plugins from Antigravity (~/.gemini), Claude Code (~/.claude), and OpenCode."""
    home = Path.home()
    roots: list[tuple[str, Path]] = [
        ("antigravity", home / ".gemini" / "config" / "plugins"),
        ("antigravity", home / ".gemini" / "antigravity" / "builtin" / "skills"),
        ("claude", home / ".claude" / "skills"),
        ("agents", home / ".agents" / "skills"),
    ]
    if workdir:
        roots.insert(0, ("project-claude", workdir / ".claude" / "skills"))
        roots.insert(0, ("project-opencode", workdir / ".opencode" / "skill"))

    seen: set[str] = set()
    items: list[dict[str, str]] = []
    for source, base in roots:
        if not base.is_dir():
            continue
        for md in sorted(base.glob("**/SKILL.md"))[:120]:
            try:
                raw = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta = _frontmatter(raw)
            name = meta.get("name") or md.parent.name
            if name in seen:
                continue
            seen.add(name)
            items.append({
                "name": name,
                "description": meta.get("description", "")[:180],
                "source": source,
                "path": str(md),
            })
    return items


def import_external_skill(skill_path: str, project_dir: Path | None = None) -> str:
    """Import an external Antigravity/Claude `SKILL.md` into OpenCode's active skill directory."""
    src = Path(skill_path).expanduser().resolve()
    if not src.is_file() or src.name != "SKILL.md":
        raise ValueError("Invalid SKILL.md path")
    raw = src.read_text(encoding="utf-8", errors="replace")
    meta = _frontmatter(raw)
    name = re.sub(r"[^\w\-]", "-", meta.get("name") or src.parent.name).strip("-")
    if project_dir:
        dest_dir = project_dir / ".opencode" / "skill" / name
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        dest_dir = base / "opencode" / "skill" / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / "SKILL.md")
    return name


def create_custom_skill(name: str, description: str, instructions: str, project_dir: Path | None = None) -> str:
    """Create a new custom SKILL.md plugin (compatible with OpenCode, Claude Code, and Antigravity)."""
    slug = re.sub(r"[^\w\-]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError("Skill name is required")
    desc = description.strip() or f"Custom skill {slug}"
    body = instructions.strip() or f"# {slug}\n\n{desc}\n"
    if project_dir:
        base = project_dir / ".opencode" / "skill"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode" / "skill"
    target = base / slug / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nname: {slug}\ndescription: {desc}\n---\n\n{body}\n", encoding="utf-8")
    return slug


def plugins_overview(workdir: Path) -> dict[str, Any]:
    active_mcp = list_mcp_servers(workdir)
    st = load_plugins_state()
    hook_states = st.get("hooks") or {}
    catalog = []
    for item in MCP_CATALOG:
        sid = item["id"]
        cfg = active_mcp.get(sid)
        catalog.append({
            **item,
            "enabled": bool(cfg and cfg.get("enabled", True)),
            "source": cfg.get("source", "") if cfg else "",
        })
    # Include any custom MCP servers not in the standard catalog
    catalog_ids = {c["id"] for c in MCP_CATALOG}
    for sid, cfg in active_mcp.items():
        if sid not in catalog_ids:
            catalog.append({
                "id": sid,
                "name": sid,
                "category": "custom",
                "description": "Custom MCP server: " + " ".join(cfg.get("command") or []),
                "command": cfg.get("command") or [],
                "env": cfg.get("env") or {},
                "enabled": bool(cfg.get("enabled", True)),
                "source": cfg.get("source", ".mcp.json"),
            })
    hooks = [
        {**h, "enabled": bool(hook_states.get(h["id"], h["default"]))}
        for h in BUILTIN_HOOKS
    ]
    native = discover_skills(workdir)
    external = discover_external_plugins(workdir)
    native_names = {s["name"] for s in native}
    for ext in external:
        ext["installed"] = ext["name"] in native_names
    return {
        "mcp": catalog,
        "hooks": hooks,
        "native_skills": native,
        "bundled_count": len(BUNDLE),
        "external_plugins": external,
    }


def plugins_main(workdir: Path, argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="openswarm plugins", description="Manage MCP servers, hooks, and cross-agent skills/plugins")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show MCP servers, hooks, and discovered skills/plugins")
    m_on = sub.add_parser("mcp-enable", help="enable a catalog MCP server in the project")
    m_on.add_argument("id")
    m_off = sub.add_parser("mcp-disable", help="disable an MCP server in the project")
    m_off.add_argument("id")
    h_cmd = sub.add_parser("hook", help="enable or disable a lifecycle hook")
    h_cmd.add_argument("id")
    h_cmd.add_argument("state", choices=["on", "off"])
    sub.add_parser("sync-skills", help="install bundled OpenSwarm skills + import discovered external skills")
    a = p.parse_args(argv or ["list"])
    if a.cmd in (None, "list"):
        ov = plugins_overview(workdir)
        print("=== MCP Servers ===")
        for m in ov["mcp"]:
            mark = "[ON] " if m["enabled"] else "[off]"
            print(f"  {mark} {m['id']:<22} {m['name']:<24} — {m['description']}")
        print("\n=== Lifecycle Hooks ===")
        for h in ov["hooks"]:
            mark = "[ON] " if h["enabled"] else "[off]"
            print(f"  {mark} {h['id']:<22} ({h['event']}) — {h['description']}")
        print(f"\n=== Skills & Plugins ({len(ov['native_skills'])} active, {len(ov['external_plugins'])} discovered) ===")
        for s in ov["external_plugins"][:20]:
            mark = "*" if s.get("installed") else " "
            print(f"  {mark} [{s['source']:<12}] {s['name']:<28} {s['description'][:60]}")
        return 0
    if a.cmd == "mcp-enable":
        toggle_catalog_mcp(workdir, a.id, True)
        print(f"MCP server '{a.id}' enabled in {workdir}")
        return 0
    if a.cmd == "mcp-disable":
        toggle_catalog_mcp(workdir, a.id, False)
        print(f"MCP server '{a.id}' disabled in {workdir}")
        return 0
    if a.cmd == "hook":
        set_hook(a.id, a.state == "on")
        print(f"Hook '{a.id}' -> {a.state}")
        return 0
    if a.cmd == "sync-skills":
        installed = install_skills(None, force=True)
        print(f"Installed {len(installed)} bundled OpenSwarm skills.")
        return 0
    return 0
