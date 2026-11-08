"""Turnstone Async HTTP Server & Streaming API.

Serves Turnstone Web UI and provides real-time Server-Sent Events (SSE)
for autonomous investigation streams, Brain memories, and tool observations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .brain import TurnstoneBrain
from .engine import TurnstoneEngine

logger = logging.getLogger("turnstone.server")


class TurnstoneServer:
    """Lightweight asyncio HTTP server for Turnstone."""

    def __init__(self, workdir: Path | None = None, model: str = "opencode/deepseek-v4.1-flash-free", port: int = 0) -> None:
        self.workdir = (workdir or Path.cwd()).resolve()
        self.model = model or "opencode/deepseek-v4.1-flash-free"
        self.port = port
        self.brain = TurnstoneBrain()
        self.engine = TurnstoneEngine(self.workdir, self.model, self.brain)
        self.html_path = Path(__file__).parent / "turnstone_ui.html"

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                writer.close()
                return

            parts = line.decode("utf-8", errors="replace").split()
            if len(parts) < 2:
                writer.close()
                return

            method, raw_path = parts[0], parts[1]
            parsed = urllib.parse.urlparse(raw_path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            # Read headers
            headers: dict[str, str] = {}
            content_length = 0
            while True:
                h_line = await reader.readline()
                if not h_line or h_line == b"\r\n":
                    break
                h_str = h_line.decode("utf-8", errors="replace").strip()
                if ":" in h_str:
                    k, v = h_str.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                    if k.strip().lower() == "content-length":
                        content_length = int(v.strip())

            # Read body
            body_bytes = b""
            if content_length > 0:
                body_bytes = await reader.readexactly(content_length)

            body_json: dict[str, Any] = {}
            if body_bytes:
                try:
                    body_json = json.loads(body_bytes.decode("utf-8", errors="replace"))
                except Exception:
                    pass

            # Route requests
            if path in ("/", "/index.html"):
                await self._serve_ui(writer)
            elif path == "/api/turnstone/status":
                await self._json(writer, {
                    "ok": True,
                    "model": self.model,
                    "workdir": str(self.workdir),
                    "workdir_name": self.workdir.name or str(self.workdir),
                    "memories_count": len(self.brain._memories),
                    "researches_count": len(self.brain.list_researches()),
                })
            elif path == "/api/turnstone/models":
                await self._json(writer, {
                    "current": self.model,
                    "categories": [
                        {
                            "group": "⚡ OpenCode Free Pool (Встроенные)",
                            "models": [
                                {"id": "opencode/deepseek-v4.1-flash-free", "name": "DeepSeek V4.1 Flash Free", "badge": "Быстрый · 128k"},
                                {"id": "opencode/beam-free", "name": "Beam Free (1M)", "badge": "Глубокие рассуждения · 1M"},
                                {"id": "opencode/nemotron-3.5-super-vl-free", "name": "Nemotron 3.5 Super VL Free", "badge": "Мультимодал · 1M"},
                                {"id": "opencode/nemotron-3.5-lightning-free", "name": "Nemotron 3.5 Lightning Free", "badge": "Ультра-скорость · 1M"},
                                {"id": "opencode/glm-5.3-flash-free", "name": "GLM 5.3 Flash Free", "badge": "Быстрый ответ · 128k"},
                                {"id": "opencode/flint-40b-free", "name": "Flint 40B Free", "badge": "Код & Анализ · 128k"},
                                {"id": "opencode/mimo-v2.6-flash-free", "name": "MiMo-V2.6-Flash Free", "badge": "Легковесный · 128k"},
                                {"id": "opencode/big-pickle", "name": "Big Pickle Free", "badge": "Баланс"},
                                {"id": "opencode/crouton-alpha-free", "name": "Crouton Alpha Free", "badge": "Экспериментальный"},
                                {"id": "opencode/pulse-alpha-free", "name": "Pulse Alpha Free", "badge": "Быстрый"},
                            ],
                        },
                        {
                            "group": "💎 Google Gemini в OpenCode",
                            "models": [
                                {"id": "google/gemini-3.5-flash", "name": "Google Gemini 3.5 Flash", "badge": "Рекомендуется · 1M"},
                                {"id": "google/gemini-3.1-flash-lite", "name": "Google Gemini 3.1 Flash-Lite", "badge": "Сверхбыстрый · 1M"},
                                {"id": "google/gemini-3-flash-preview", "name": "Google Gemini 3 Flash Preview", "badge": "Рассуждения · 1M"},
                                {"id": "google/gemini-flash-latest", "name": "Google Gemini Flash Latest", "badge": "Актуальная версия · 1M"},
                                {"id": "google/gemini-pro-latest", "name": "Google Gemini Pro Latest", "badge": "Максимальное качество · 2M"},
                            ],
                        },
                        {
                            "group": "🏠 Локальные модели (Ollama в OpenCode)",
                            "models": [
                                {"id": "ollama/qwen2.5-coder:7b", "name": "Qwen 2.5 Coder 7B (Local)", "badge": "Оффлайн · Код"},
                                {"id": "ollama/deepseek-r1:8b", "name": "DeepSeek R1 8B (Local)", "badge": "Оффлайн · Рассуждения"},
                            ],
                        },
                    ],
                })
            elif path == "/api/turnstone/run" and method == "POST":
                await self._stream_run(writer, body_json)
            elif path == "/api/turnstone/brain/memories":
                await self._json(writer, {"memories": self.brain.list_memories()})
            elif path == "/api/turnstone/brain/recall":
                q_str = (query.get("q") or [""])[0]
                await self._json(writer, {"memories": self.brain.recall(q_str)})
            elif path == "/api/turnstone/brain/remember" and method == "POST":
                fact = body_json.get("fact") or body_json.get("content", "")
                cat = body_json.get("category", "fact")
                item = self.brain.remember(fact, category=cat)
                await self._json(writer, {"ok": True, "memory": asdict(item)})
            elif path == "/api/turnstone/brain/forget" and method == "POST":
                m_id = (query.get("id") or [body_json.get("id")])[0] or ""
                deleted = self.brain.forget(m_id)
                await self._json(writer, {"ok": deleted})
            elif path == "/api/turnstone/brain/researches":
                await self._json(writer, {"researches": self.brain.list_researches()})
            elif path == "/api/turnstone/brain/research":
                filename = (query.get("file") or [""])[0]
                content = self.brain.get_research_content(filename)
                await self._json(writer, {"content": content})
            else:
                await self._not_found(writer)

        except Exception as exc:
            logger.error("Client handle error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_ui(self, writer: asyncio.StreamWriter) -> None:
        content = self.html_path.read_bytes()
        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n"
            b"Content-Length: " + str(len(content)).encode() + b"\r\n"
            b"Cache-Control: no-cache\r\n\r\n"
        )
        writer.write(headers + content)
        await writer.drain()

    async def _json(self, writer: asyncio.StreamWriter, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        status_line = b"HTTP/1.1 200 OK\r\n" if status == 200 else f"HTTP/1.1 {status} Error\r\n".encode()
        headers = (
            status_line +
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n"
        )
        writer.write(headers + raw)
        await writer.drain()

    async def _not_found(self, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found")
        await writer.drain()

    async def _stream_run(self, writer: asyncio.StreamWriter, body: dict[str, Any]) -> None:
        prompt = str(body.get("prompt", "")).strip()
        history = body.get("history", [])
        use_web = bool(body.get("use_web", True))
        use_brain = bool(body.get("use_brain", True))
        deep_research = bool(body.get("deep_research", False))
        model = str(body.get("model", self.model))

        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"X-Accel-Buffering: no\r\n\r\n"
        )
        writer.write(headers)
        await writer.drain()

        try:
            async for event in self.engine.run(
                prompt=prompt,
                history=history,
                use_web=use_web,
                use_brain=use_brain,
                deep_research=deep_research,
                model=model,
            ):
                payload = json.dumps({
                    "type": event.type,
                    "content": event.content,
                    "metadata": event.metadata,
                }, ensure_ascii=False)
                writer.write(f"data: {payload}\n\n".encode("utf-8"))
                await writer.drain()
        except Exception as exc:
            err_payload = json.dumps({"type": "error", "content": str(exc)})
            writer.write(f"data: {err_payload}\n\n".encode("utf-8"))
            await writer.drain()
