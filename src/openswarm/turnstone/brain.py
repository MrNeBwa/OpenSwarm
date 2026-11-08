"""Turnstone Persistent Brain — Long-term memory, knowledge store & research archive.

Stores memories, user preferences, facts, and deep research briefs in
`~/.local/share/turnstone/brain/` so Turnstone retains persistent intelligence across sessions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("turnstone.brain")


def brain_dir() -> Path:
    """Return the persistent directory for Turnstone's brain."""
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    d = base / "turnstone" / "brain"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class MemoryItem:
    id: str
    content: str
    category: str = "fact"  # fact, preference, insight, instruction, context
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)
    source: str = "conversation"


class TurnstoneBrain:
    """Local-first persistent memory & knowledge engine for the Turnstone agent."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or brain_dir()
        self.memories_path = self.root / "memories.json"
        self.research_dir = self.root / "researches"
        self.notes_dir = self.root / "notes"
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self._memories: list[MemoryItem] = []
        self._load()

    def _load(self) -> None:
        if not self.memories_path.exists():
            # Seed with friendly introductory memory
            self._memories = [
                MemoryItem(
                    id="seed-intro",
                    content="Turnstone is an autonomous research teammate powered by OpenCode. Flips stones to reveal deep truth.",
                    category="context",
                    tags=["system", "identity"],
                )
            ]
            self._save()
            return
        try:
            data = json.loads(self.memories_path.read_text(encoding="utf-8"))
            self._memories = [MemoryItem(**item) for item in data if isinstance(item, dict)]
        except Exception as exc:
            logger.warning("Failed to load brain memories: %s", exc)
            self._memories = []

    def _save(self) -> None:
        try:
            raw = [asdict(m) for m in self._memories]
            self.memories_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to save brain memories: %s", exc)

    def remember(self, content: str, category: str = "fact", tags: list[str] | None = None, source: str = "conversation") -> MemoryItem:
        """Save a new fact, preference, or context memory to the Brain."""
        content = content.strip()
        if not content:
            raise ValueError("Memory content cannot be empty")

        # Avoid exact duplicates
        for existing in self._memories:
            if existing.content.lower() == content.lower():
                existing.confidence = min(1.0, existing.confidence + 0.1)
                self._save()
                return existing

        item = MemoryItem(
            id=str(uuid.uuid4())[:8],
            content=content,
            category=category,
            tags=tags or [],
            source=source,
            created_at=time.time(),
        )
        self._memories.append(item)
        self._save()
        return item

    def recall(self, query: str = "", limit: int = 5, min_score: float = 0.1) -> list[dict[str, Any]]:
        """Search memories using keyword and token overlap scoring."""
        if not query:
            return [asdict(m) for m in self._memories[-limit:]]

        q_words = set(re.findall(r"\w+", query.lower()))
        if not q_words:
            return [asdict(m) for m in self._memories[-limit:]]

        scored: list[tuple[float, MemoryItem]] = []
        for m in self._memories:
            m_words = set(re.findall(r"\w+", m.content.lower()))
            tag_words = set(re.findall(r"\w+", " ".join(m.tags).lower()))
            overlap = len(q_words & (m_words | tag_words))
            if overlap > 0:
                score = (overlap / len(q_words)) * m.confidence
                if score >= min_score:
                    scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [asdict(m) for _, m in scored[:limit]]

    def forget(self, memory_id: str) -> bool:
        """Remove a memory by ID."""
        orig_len = len(self._memories)
        self._memories = [m for m in self._memories if m.id != memory_id]
        if len(self._memories) != orig_len:
            self._save()
            return True
        return False

    def list_memories(self, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        items = self._memories
        if category:
            items = [m for m in items if m.category == category]
        return [asdict(m) for m in items[-limit:]]

    def save_research(self, topic: str, report_md: str, sources: list[str] | None = None) -> dict[str, Any]:
        """Save a deep research brief to the research archive."""
        slug = re.sub(r"[^\w\-]+", "-", topic.lower()).strip("-")[:40] or "research"
        timestamp = int(time.time())
        filename = f"{timestamp}_{slug}.md"
        path = self.research_dir / filename

        meta = {
            "topic": topic,
            "created_at": timestamp,
            "sources": sources or [],
            "file": filename,
        }
        full_content = (
            f"---\n"
            f"topic: {json.dumps(topic, ensure_ascii=False)}\n"
            f"date: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))}\n"
            f"sources: {json.dumps(sources or [], ensure_ascii=False)}\n"
            f"---\n\n"
            f"# 🔬 Research Brief: {topic}\n\n"
            f"{report_md}\n"
        )
        path.write_text(full_content, encoding="utf-8")

        # Also store high-level insight in memory
        summary_snip = report_md.split("\n\n")[0][:250]
        self.remember(
            f"Researched '{topic}': {summary_snip}",
            category="insight",
            tags=["research", slug],
            source=filename,
        )
        return meta

    def list_researches(self) -> list[dict[str, Any]]:
        """List all completed research briefs."""
        res: list[dict[str, Any]] = []
        for p in sorted(self.research_dir.glob("*.md"), reverse=True):
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                first_line = txt.splitlines()[0] if txt.splitlines() else p.name
                res.append({
                    "filename": p.name,
                    "title": p.stem.replace("_", " ").title(),
                    "size": p.stat().st_size,
                    "modified": p.stat().st_mtime,
                    "path": str(p),
                })
            except Exception:
                continue
        return res

    def get_research_content(self, filename: str) -> str:
        safe_name = Path(filename).name
        target = self.research_dir / safe_name
        if target.exists():
            return target.read_text(encoding="utf-8", errors="replace")
        return ""

    def context_prompt(self, query: str = "", max_items: int = 5) -> str:
        """Format recalled memories into a concise block for LLM prompt injection."""
        recalled = self.recall(query, limit=max_items)
        if not recalled:
            return ""
        lines = [f"• [{m['category']}] {m['content']}" for m in recalled]
        return "🧠 [Turnstone Brain Memory]:\n" + "\n".join(lines)
