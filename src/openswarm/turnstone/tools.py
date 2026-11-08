"""Turnstone Real-World Tools Engine.

Provides autonomous web search, page scraping, shell execution, file operations,
Python scratchpad, and Brain memory interaction.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .brain import TurnstoneBrain

logger = logging.getLogger("turnstone.tools")


def clean_html_to_text(raw_html: str) -> str:
    """Strip scripts, styles, and HTML tags to extract clean readable text."""
    # Remove script and style blocks
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    # Convert block elements to newlines
    text = re.sub(r"(?i)<(p|div|h[1-6]|li|blockquote|br)[^>]*>", "\n", text)
    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Unescape HTML entities
    text = html.unescape(text)
    # Compress multiple whitespaces and excessive newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class TurnstoneTools:
    """Executes tools on behalf of the Turnstone autonomous agent."""

    def __init__(self, brain: TurnstoneBrain, workdir: Path | None = None) -> None:
        self.brain = brain
        self.workdir = (workdir or Path.cwd()).resolve()

    def web_search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        """Perform a web search via DuckDuckGo and return titles, URLs, and snippets."""
        q = query.strip()
        if not q:
            return {"error": "Empty search query", "results": []}

        # 1. DuckDuckGo Instant Answers API
        results: list[dict[str, str]] = []
        try:
            url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(q)}&format=json&no_html=1&skip_disambig=1"
            req = urllib.request.Request(url, headers={"User-Agent": "Turnstone/1.0 (Autonomous Research Agent)"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            abstract = (data.get("AbstractText") or "").strip()
            if abstract:
                results.append({
                    "title": data.get("Heading") or q,
                    "url": data.get("AbstractURL") or "https://duckduckgo.com/?q=" + urllib.parse.quote(q),
                    "snippet": abstract,
                })

            for topic in data.get("RelatedTopics", []):
                if isinstance(topic, dict) and "Text" in topic and "FirstURL" in topic:
                    results.append({
                        "title": topic.get("Text", "").split(" - ")[0],
                        "url": topic.get("FirstURL", ""),
                        "snippet": topic.get("Text", ""),
                    })
                if len(results) >= max_results:
                    break
        except Exception as exc:
            logger.debug("DuckDuckGo Instant Answer API note: %s", exc)

        # 2. DuckDuckGo HTML Fallback if API returned few results
        if len(results) < 3:
            try:
                html_url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
                req = urllib.request.Request(
                    html_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    page_html = resp.read().decode("utf-8", errors="replace")

                matches = re.findall(
                    r'<a class="result__url"[^>]*href="([^"]+)"[^>]*>.*?<a class="result__snippet[^"]*"[^>]*>(.*?)</a>',
                    page_html,
                    re.DOTALL,
                )
                for raw_url, raw_snip in matches:
                    actual_url = raw_url.strip()
                    if "uddg=" in actual_url:
                        m = re.search(r"uddg=([^&]+)", actual_url)
                        if m:
                            actual_url = urllib.parse.unquote(m.group(1))

                    snippet = re.sub(r"<[^>]+>", "", raw_snip).strip()
                    snippet = html.unescape(snippet)
                    if actual_url and snippet:
                        results.append({
                            "title": snippet[:60] + "...",
                            "url": actual_url,
                            "snippet": snippet,
                        })
                    if len(results) >= max_results:
                        break
            except Exception as exc:
                logger.debug("DuckDuckGo HTML fallback note: %s", exc)

        # 3. Wikipedia API Fallback if query looks like an encyclopedic concept
        if len(results) < 2:
            try:
                wiki_url = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={urllib.parse.quote(q)}&limit={max_results}&namespace=0&format=json"
                req = urllib.request.Request(wiki_url, headers={"User-Agent": "Turnstone/1.0"})
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    wdata = json.loads(resp.read().decode("utf-8", errors="replace"))
                if len(wdata) >= 4 and isinstance(wdata[1], list):
                    for title, snip, link in zip(wdata[1], wdata[2], wdata[3]):
                        if link and snip:
                            results.append({
                                "title": f"{title} (Wikipedia)",
                                "url": link,
                                "snippet": snip,
                            })
            except Exception:
                pass

        return {
            "query": q,
            "count": len(results[:max_results]),
            "results": results[:max_results],
        }

    def web_browse(self, url: str, max_chars: int = 4000) -> dict[str, Any]:
        """Fetch and extract clean readable text content from a web page."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=7.0) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                raw_bytes = resp.read(1024 * 1024)  # 1MB limit
                raw_html = raw_bytes.decode(charset, errors="replace")

            title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
            title = html.unescape(title_m.group(1).strip()) if title_m else url

            clean_text = clean_html_to_text(raw_html)
            truncated = clean_text[:max_chars]
            return {
                "ok": True,
                "url": url,
                "title": title,
                "chars": len(clean_text),
                "text": truncated,
                "is_truncated": len(clean_text) > max_chars,
            }
        except Exception as exc:
            return {
                "ok": False,
                "url": url,
                "error": str(exc),
            }

    def bash_run(self, command: str, timeout: int = 15) -> dict[str, Any]:
        """Execute a safe shell command in the workspace directory."""
        cmd = command.strip()
        if not cmd:
            return {"error": "Empty command"}

        # Guard against catastrophic destruction
        lowered = cmd.lower()
        if any(bad in lowered for bad in ("rm -rf /", ":(){ :|:& };:", "dd if=/dev/zero of=/dev/sd", "mkfs")):
            return {"error": "Command blocked by Turnstone safety policy", "returncode": -1}

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out = proc.stdout.strip()
            err = proc.stderr.strip()
            return {
                "ok": proc.returncode == 0,
                "command": cmd,
                "returncode": proc.returncode,
                "stdout": out[:4000],
                "stderr": err[:2000],
            }
        except subprocess.TimeoutExpired:
            return {"error": f"Command timed out after {timeout}s", "returncode": -1}
        except Exception as exc:
            return {"error": str(exc), "returncode": -1}

    def file_view(self, path: str, max_lines: int = 250) -> dict[str, Any]:
        """Read a file in the workspace."""
        target = (self.workdir / path).resolve()
        if not target.exists():
            return {"error": f"File not found: {path}"}
        if target.is_dir():
            files = [p.name + ("/" if p.is_dir() else "") for p in list(target.iterdir())[:60]]
            return {"type": "directory", "files": files}

        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            content = "\n".join(lines[:max_lines])
            return {
                "path": str(target.relative_to(self.workdir)),
                "lines_total": len(lines),
                "content": content,
                "truncated": len(lines) > max_lines,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def file_write(self, path: str, content: str) -> dict[str, Any]:
        """Write content to a file in the workspace."""
        target = (self.workdir / path).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(target.relative_to(self.workdir)), "bytes": len(content)}
        except Exception as exc:
            return {"error": str(exc)}

    def python_eval(self, code: str) -> dict[str, Any]:
        """Execute a Python scratchpad code snippet."""
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(self.workdir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return {
                "ok": proc.returncode == 0,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"error": "Python snippet execution timed out", "returncode": -1}
        except Exception as exc:
            return {"error": str(exc)}

    def brain_remember(self, fact: str, category: str = "fact", tags: str = "") -> dict[str, Any]:
        """Store a fact into persistent Turnstone Brain."""
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        item = self.brain.remember(fact, category=category, tags=tag_list)
        return {"ok": True, "id": item.id, "content": item.content}

    def brain_search(self, query: str) -> dict[str, Any]:
        """Query Turnstone Brain long-term memories."""
        recalled = self.brain.recall(query, limit=5)
        return {"query": query, "memories": recalled}

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> Any:
        """Dynamically dispatch a tool execution."""
        registry = {
            "web_search": lambda: self.web_search(args.get("query", ""), int(args.get("max_results", 5))),
            "web_browse": lambda: self.web_browse(args.get("url", ""), int(args.get("max_chars", 4000))),
            "bash_run": lambda: self.bash_run(args.get("command", "")),
            "file_view": lambda: self.file_view(args.get("path", "")),
            "file_write": lambda: self.file_write(args.get("path", ""), args.get("content", "")),
            "python_eval": lambda: self.python_eval(args.get("code", "")),
            "brain_remember": lambda: self.brain_remember(args.get("fact", ""), args.get("category", "fact"), args.get("tags", "")),
            "brain_search": lambda: self.brain_search(args.get("query", "")),
        }
        fn = registry.get(tool_name)
        if not fn:
            return {"error": f"Unknown tool: {tool_name}"}
        try:
            return fn()
        except Exception as exc:
            return {"error": f"Tool execution failed: {exc}"}
