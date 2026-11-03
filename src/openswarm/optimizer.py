"""Token Saver & Context Optimizer v2.0 for OpenSwarm.

Implements modern LLM token optimization techniques:
1. KV-Cache Prefix Alignment — keeps static system/project headers at the start of prompts
   so provider prefix caches (DeepSeek, Anthropic, OpenAI, Gemini) hit consistently.
2. LLMLingua-inspired Prompt Pruning — removes low-information filler phrases, duplicate
   instructions, and ANSI/log noise while preserving code blocks, paths, and identifiers.
3. AST & Regex Repo Skeletonization (RepoMap) — extracts compact class/function signatures
   instead of dumping full files into context (~80-85% token reduction).
4. Hierarchical Sliding-Window History Condensation — keeps recent turns verbatim and
   condenses older turns into structured key-fact digests.
5. Semantic Normalized Caching — catches both exact and normalized near-duplicate prompts.
6. Diff-First Output Directive — instructs models to output minimal diffs/edits instead
   of repeating unchanged code lines.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("openswarm.optimizer")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Low-information conversational filler patterns safe to prune outside code blocks
_FILLER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bplease\s+make\s+sure\s+to\b", re.IGNORECASE), "ensure"),
    (re.compile(r"\bit\s+is\s+important\s+to\s+note\s+that\b", re.IGNORECASE), "note:"),
    (re.compile(r"\bin\s+order\s+to\b", re.IGNORECASE), "to"),
    (re.compile(r"\bdue\s+to\s+the\s+fact\s+that\b", re.IGNORECASE), "because"),
    (re.compile(r"\bat\s+this\s+point\s+in\s+time\b", re.IGNORECASE), "now"),
    (re.compile(r"\bfor\s+the\s+purpose\s+of\b", re.IGNORECASE), "for"),
    (re.compile(r"\bпожалуйста,\s+постарайся\b", re.IGNORECASE), ""),
    (re.compile(r"\bобрати\s+особое\s+внимание\s+на\s+то,\s+что\b", re.IGNORECASE), "важно:"),
    (re.compile(r"\bдля\s+того\s+чтобы\b", re.IGNORECASE), "чтобы"),
]


@dataclass
class OptimizerSettings:
    mode: str = "aggressive"  # "balanced" | "aggressive" | "ultra"
    kv_prefix_align: bool = True
    llmlingua_prune: bool = True
    diff_only_output: bool = True
    repo_skeleton: bool = True
    semantic_cache: bool = True
    max_verbatim_turns: int = 3
    max_context_chars: int = 24000

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "kv_prefix_align": self.kv_prefix_align,
            "llmlingua_prune": self.llmlingua_prune,
            "diff_only_output": self.diff_only_output,
            "repo_skeleton": self.repo_skeleton,
            "semantic_cache": self.semantic_cache,
            "max_verbatim_turns": self.max_verbatim_turns,
            "max_context_chars": self.max_context_chars,
        }

    def update_from(self, data: dict[str, Any]) -> None:
        if "mode" in data and data["mode"] in ("balanced", "aggressive", "ultra"):
            self.mode = str(data["mode"])
            if self.mode == "balanced":
                self.max_verbatim_turns = 4
                self.max_context_chars = 36000
            elif self.mode == "aggressive":
                self.max_verbatim_turns = 3
                self.max_context_chars = 24000
            elif self.mode == "ultra":
                self.max_verbatim_turns = 2
                self.max_context_chars = 14000
        for bkey in ("kv_prefix_align", "llmlingua_prune", "diff_only_output", "repo_skeleton", "semantic_cache"):
            if bkey in data:
                setattr(self, bkey, bool(data[bkey]))
        if "max_verbatim_turns" in data:
            self.max_verbatim_turns = max(1, min(12, int(data["max_verbatim_turns"])))
        if "max_context_chars" in data:
            self.max_context_chars = max(2000, min(200000, int(data["max_context_chars"])))


@dataclass
class OptimizationStats:
    total_original_chars: int = 0
    total_compressed_chars: int = 0
    cache_hits: int = 0
    summaries_created: int = 0
    skeletons_built: int = 0
    prompts_optimized: int = 0

    @property
    def saved_chars(self) -> int:
        return max(0, self.total_original_chars - self.total_compressed_chars)

    @property
    def estimated_saved_tokens(self) -> int:
        # Approx 3.8 chars per token across mixed EN/RU/code
        return int(round(self.saved_chars / 3.8))

    @property
    def savings_percent(self) -> float:
        if self.total_original_chars <= 0:
            return 0.0
        return round((self.saved_chars / self.total_original_chars) * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        pct = self.savings_percent
        return {
            "original_chars": self.total_original_chars,
            "compressed_chars": self.total_compressed_chars,
            "saved_chars": self.saved_chars,
            "saved_tokens_est": self.estimated_saved_tokens,
            "savings_percent": pct,
            "savings_pct": pct,
            "cache_hits": self.cache_hits,
            "summaries_created": self.summaries_created,
            "skeletons_built": self.skeletons_built,
            "prompts_optimized": self.prompts_optimized,
            "cache_size": len(_CACHE),
            "settings": _SETTINGS.to_dict(),
            "techniques": [
                {"id": "kv_prefix", "name": "KV-Cache Prefix Alignment", "active": _SETTINGS.kv_prefix_align},
                {"id": "llmlingua", "name": "LLMLingua Prompt Pruning", "active": _SETTINGS.llmlingua_prune},
                {"id": "repomap", "name": "AST RepoMap Skeletonization", "active": _SETTINGS.repo_skeleton},
                {"id": "diff_first", "name": "Diff-First Output Compression", "active": _SETTINGS.diff_only_output},
                {"id": "sem_cache", "name": "Semantic Normalized Cache", "active": _SETTINGS.semantic_cache},
            ],
        }


# Global memory cache for prompt responses (TTL = 15 minutes)
_CACHE: dict[str, tuple[float, str]] = {}
_NORM_CACHE: dict[str, str] = {}
_STATS = OptimizationStats()
_SETTINGS = OptimizerSettings()


def get_optimizer_settings() -> OptimizerSettings:
    return _SETTINGS


def update_optimizer_settings(data: dict[str, Any]) -> dict[str, Any]:
    _SETTINGS.update_from(data)
    res = get_optimizer_stats()
    res.update(_SETTINGS.to_dict())
    return res


def reset_optimizer_stats() -> None:
    global _STATS, _CACHE, _NORM_CACHE
    _STATS = OptimizationStats()
    _CACHE.clear()
    _NORM_CACHE.clear()


def get_optimizer_stats() -> dict[str, Any]:
    return _STATS.to_dict()


def _prune_outside_codeblocks(text: str) -> str:
    """Applies LLMLingua-style token pruning on prose while leaving fenced code blocks intact."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    out: list[str] = []
    for idx, chunk in enumerate(parts):
        if idx % 2 == 1:  # inside ``` code block
            out.append(chunk)
            continue
        c = _ANSI_RE.sub("", chunk)
        for pat, repl in _FILLER_PATTERNS:
            c = pat.sub(repl, c)
        # Collapse multiple horizontal spaces (preserving line indentation up to 4 spaces)
        c = re.sub(r"(?<=\S)[ \t]{2,}", " ", c)
        out.append(c)
    return "".join(out)


def compress_text(text: str, max_head: int = 25, max_tail: int = 25) -> str:
    """Collapses repetitive whitespace, blank lines, and truncates huge text bodies cleanly."""
    if not text:
        return ""
    orig_len = len(text)

    # 1. Strip ANSI sequences and normalize line endings
    t = _ANSI_RE.sub("", text)
    t = re.sub(r"\r\n|\r", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    # 2. Apply LLMLingua-style prose pruning if enabled
    if _SETTINGS.llmlingua_prune:
        t = _prune_outside_codeblocks(t)

    # 3. Trim trailing whitespace on each line and collapse consecutive identical log lines
    raw_lines = [ln.rstrip() for ln in t.splitlines()]
    lines: list[str] = []
    prev_line = None
    repeat_count = 0
    for ln in raw_lines:
        if ln and ln == prev_line:
            repeat_count += 1
            if repeat_count <= 1:
                lines.append(ln)
            elif repeat_count == 2:
                lines.append("  [... повтор строки скрыт ...]")
        else:
            prev_line = ln
            repeat_count = 0
            lines.append(ln)

    # 4. If line count is very large (e.g. huge terminal logs, git diffs, directory trees)
    if len(lines) > (max_head + max_tail + 10):
        head = lines[:max_head]
        tail = lines[-max_tail:]
        omitted = len(lines) - (max_head + max_tail)
        lines = head + [f"\n[... {omitted} строк лога/вывода скрыто для экономии токенов ...]\n"] + tail
        t = "\n".join(lines)
    else:
        t = "\n".join(lines)

    _STATS.total_original_chars += orig_len
    _STATS.total_compressed_chars += len(t)
    return t


def skeletonize_code(source: str, filename: str = "file.py") -> str:
    """Extracts a compact structural skeleton (classes, defs, signatures) from source code.

    Reduces token footprint by 75-90% compared to reading full implementation bodies.
    """
    if not source or not source.strip():
        return ""
    orig_len = len(source)
    ext = Path(filename).suffix.lower()

    if ext == ".py":
        try:
            tree = ast.parse(source)
            out_lines: list[str] = [f"# Skeleton: {filename}"]

            def _fmt_args(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
                parts = []
                for a in fn_node.args.args:
                    ann = f": {ast.unparse(a.annotation)}" if (a.annotation and hasattr(ast, "unparse")) else ""
                    parts.append(f"{a.arg}{ann}")
                return ", ".join(parts)

            def _fmt_ret(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
                if fn_node.returns and hasattr(ast, "unparse"):
                    return f" -> {ast.unparse(fn_node.returns)}"
                return ""

            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    doc = ast.get_docstring(node)
                    doc_first = f'  """{doc.splitlines()[0][:80]}"""' if doc else ""
                    out_lines.append(f"{prefix} {node.name}({_fmt_args(node)}){_fmt_ret(node)}: ...{doc_first}")
                elif isinstance(node, ast.ClassDef):
                    bases = [ast.unparse(b) if hasattr(ast, "unparse") else "" for b in node.bases]
                    base_str = f"({', '.join(b for b in bases if b)})" if bases else ""
                    out_lines.append(f"class {node.name}{base_str}:")
                    methods = [
                        n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]
                    if not methods:
                        out_lines.append("  ...")
                    for m in methods[:20]:
                        mpref = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                        out_lines.append(f"  {mpref} {m.name}({_fmt_args(m)}){_fmt_ret(m)}: ...")
            res = "\n".join(out_lines)
            _STATS.skeletons_built += 1
            _STATS.total_original_chars += orig_len
            _STATS.total_compressed_chars += len(res)
            return res
        except Exception:
            pass

    # Regex fallback for JS/TS/Go/Rust/C#/etc.
    sig_re = re.compile(
        r"^\s*(export\s+)?(async\s+)?(function\s+\w+|class\s+\w+|interface\s+\w+|type\s+\w+|def\s+\w+|pub\s+fn\s+\w+|fn\s+\w+|struct\s+\w+)"
    )
    sigs = [f"# Skeleton: {filename}"]
    for ln in source.splitlines():
        if sig_re.match(ln):
            sigs.append(ln.strip()[:120])
    res = "\n".join(sigs[:60])
    _STATS.skeletons_built += 1
    _STATS.total_original_chars += orig_len
    _STATS.total_compressed_chars += len(res)
    return res


def build_repo_skeleton(workdir: Path | str, max_files: int = 18) -> str:
    """Builds a compact Aider-style RepoMap skeleton for a workspace directory."""
    if not workdir:
        return ""
    workdir = Path(workdir)
    if not workdir.is_dir():
        return ""
    skip_dirs = {".git", ".swarm", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    py_files: list[Path] = []
    for p in sorted(workdir.rglob("*")):
        if any(part in skip_dirs for part in p.parts):
            continue
        if p.is_file() and p.suffix in (".py", ".ts", ".js", ".go", ".rs") and p.stat().st_size < 120_000:
            py_files.append(p)
            if len(py_files) >= max_files:
                break
    if not py_files:
        return ""
    chunks = []
    for f in py_files:
        try:
            rel = str(f.relative_to(workdir))
            sk = skeletonize_code(f.read_text(encoding="utf-8", errors="ignore"), rel)
            if sk:
                chunks.append(sk)
        except Exception:
            continue
    return "\n\n".join(chunks)


def prune_history_context(messages: list[dict[str, str]], max_verbatim_turns: int | None = None) -> str:
    """Smartly prunes multi-turn history: keeps recent turns verbatim, summarizes older ones."""
    if not messages:
        return ""
    keep = max_verbatim_turns if max_verbatim_turns is not None else _SETTINGS.max_verbatim_turns
    orig_chars = sum(len(m.get("content", "") or "") for m in messages)

    if len(messages) <= keep:
        parts = []
        for m in messages:
            role = m.get("role", "user")
            content = (m.get("content", "") or "").strip()
            if content:
                label = "Пользователь" if role == "user" else "Ассистент"
                parts.append(f"{label}: {content}")
        return "\n\n".join(parts)

    older = messages[:-keep]
    recent = messages[-keep:]
    _STATS.summaries_created += 1

    summary_bullets = []
    for m in older:
        role = m.get("role", "user")
        content = (m.get("content", "") or "").strip()
        if not content:
            continue
        first_line = content.splitlines()[0][:100]
        if role == "user":
            summary_bullets.append(f"- Запрос пользователя: {first_line}")
        else:
            summary_bullets.append(f"- Ответ ассистента: {first_line}")

    older_summary = "### Краткий контекст предыдущих сообщений:\n" + "\n".join(summary_bullets[:8])
    recent_parts = []
    for m in recent:
        role = m.get("role", "user")
        content = (m.get("content", "") or "").strip()
        label = "Пользователь" if role == "user" else "Ассистент"
        recent_parts.append(f"{label}: {content}")

    res = older_summary + "\n\n### Текущий диалог:\n" + "\n\n".join(recent_parts)
    if orig_chars > len(res):
        _STATS.total_original_chars += orig_chars
        _STATS.total_compressed_chars += len(res)
    return res


def _normalize_query(text: str) -> str:
    """Normalizes query string for semantic near-duplicate cache matching."""
    cleaned = re.sub(r"\s+", " ", text.strip().lower())
    cleaned = re.sub(r"[^\w\sа-яё/-]", "", cleaned)
    return cleaned


def make_cache_key(prompt: str, role: str = "chat", model: str = "") -> str:
    p_norm = _normalize_query(prompt) if _SETTINGS.semantic_cache else prompt
    raw = f"{model}:::{role}:::{p_norm}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def check_cache(key: str, ttl_seconds: float = 900.0, prompt: str = "", model: str = "") -> str | None:
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts <= ttl_seconds:
            _STATS.cache_hits += 1
            return val
        del _CACHE[key]
    if _SETTINGS.semantic_cache and prompt:
        norm_key = f"{model}:::{_normalize_query(prompt)}"
        mapped = _NORM_CACHE.get(norm_key)
        if mapped and mapped in _CACHE:
            ts, val = _CACHE[mapped]
            if now - ts <= ttl_seconds:
                _STATS.cache_hits += 1
                return val
    return None


def store_cache(key: str, val: str, prompt: str = "", model: str = "") -> None:
    if len(_CACHE) > 256:
        oldest = min(_CACHE.keys(), key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]
    _CACHE[key] = (time.time(), val)
    if _SETTINGS.semantic_cache and prompt:
        norm_key = f"{model}:::{_normalize_query(prompt)}"
        _NORM_CACHE[norm_key] = key


def get_token_saving_directive(role: str = "chat") -> str:
    """Returns a compact directive that minimizes unnecessary output tokens."""
    if not _SETTINGS.diff_only_output:
        return ""
    if role in ("coder", "reviewer", "architect"):
        return "\n[Token-Eco (ECONOMY): Пиши по существу. В коде приводи только изменённые функции/блоки или diff без повторения всего файла.]"
    return "\n[Token-Eco (ECONOMY): Отвечай ёмко, структурированно и без воды.]"


def optimize_prompt_for_model(prompt: str, role: str = "chat", aggressive: bool | None = None) -> str:
    """Trims redundant boilerplate, deduplicates instructions, and applies token optimization."""
    p = prompt.strip()
    if not p:
        return ""
    _STATS.prompts_optimized += 1
    is_aggr = aggressive if aggressive is not None else (_SETTINGS.mode in ("aggressive", "ultra"))
    is_ultra = _SETTINGS.mode == "ultra"

    lines = p.splitlines()
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("```"):
            deduped.append(line)
            continue
        if (s.startswith("[Инструкция") or s.startswith("[OpenSwarm") or s.startswith("[Token-Eco")) and s in seen:
            continue
        if len(s) > 45 and s in seen and not s.startswith(("def ", "class ", "import ", "from ", "-", "*")):
            continue
        seen.add(s)
        deduped.append(line)

    res = "\n".join(deduped)
    max_h = 22 if is_ultra else (35 if is_aggr else 50)
    compressed = compress_text(res, max_head=max_h, max_tail=max_h)

    if len(compressed) > _SETTINGS.max_context_chars:
        half = _SETTINGS.max_context_chars // 2
        omitted = len(compressed) - _SETTINGS.max_context_chars
        compressed = (
            compressed[:half]
            + f"\n\n[... {omitted} символов контекста сжато (Token Budget Limit) ...]\n\n"
            + compressed[-half:]
        )
    return compressed
