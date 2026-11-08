"""Turnstone Autonomous Agent Engine — Powered by OpenCode.

Executes autonomous investigations, tool-calling loops (web search, browser, shell, python),
Deep Research workflows, and persistent Brain synchronization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from ..config import default_config, SwarmConfig
from ..runner import OpenCodeRunner
from .brain import TurnstoneBrain
from .tools import TurnstoneTools

logger = logging.getLogger("turnstone.engine")

TURNSTONE_SYSTEM_PROMPT = """You are Turnstone — an autonomous intelligence teammate and research agent.
Like the Ruddy Turnstone shorebird that flips stones to reveal what lies beneath, your mission is to dig beneath the surface, verify claims, search the web, inspect systems, and uncover deep truth.

Core Principles:
1. Direct, sharp, and candid: No corporate fluff, no canned disclaimers, no preachy lecturing. Answer directly with clarity and wit.
2. Evidence-first: When investigating or answering factual questions, cite specific sources, numbers, dates, and verifiable evidence.
3. Autonomous Tool-Use: You have access to real tools:
   - web_search(query: str, max_results: int = 5)
   - web_browse(url: str, max_chars: int = 4000)
   - bash_run(command: str)
   - file_view(path: str)
   - file_write(path: str, content: str)
   - python_eval(code: str)
   - brain_remember(fact: str, category: str = "fact", tags: str = "")
   - brain_search(query: str)

Tool Calling Convention:
To use a tool, output a block in this exact format:
```tool:tool_name
{"param": "value"}
```
You can invoke multiple tools across turns. When you receive the tool observation, synthesize the answer and present a well-structured, beautiful response.
"""


@dataclass
class AgentEvent:
    type: str  # thought, tool_start, tool_end, message, research_plan, done, error
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class TurnstoneEngine:
    """Autonomous agent coordinator driving Turnstone research and tool execution."""

    def __init__(
        self,
        workdir: Path | None = None,
        model: str = "opencode/deepseek-v4.1-flash-free",
        brain: TurnstoneBrain | None = None,
    ) -> None:
        self.workdir = (workdir or Path.cwd()).resolve()
        self.model = model or "opencode/deepseek-v4.1-flash-free"
        self.brain = brain or TurnstoneBrain()
        self.tools = TurnstoneTools(self.brain, self.workdir)
        try:
            self.cfg: SwarmConfig | None = default_config()
        except Exception:
            self.cfg = None

    async def _execute_llm(
        self,
        prompt: str,
        target_model: str,
    ) -> tuple[str, str, str]:
        """Runs OpenCode with target_model. If a free pool limit is hit on an opencode/*-free model,
        it automatically falls back to google/gemini-3.5-flash or google/gemini-3.1-flash-lite in OpenCode.
        Returns: (output_text, error_message, effective_model)
        """
        cfg = self.cfg or default_config()

        async def _call_one(m: str) -> tuple[int, str, str]:
            runner = OpenCodeRunner(
                cfg=cfg,
                cwd=self.workdir,
                agent_id="turnstone",
                role="coder",
                model=m,
                standalone=True,
                sandbox=False,
            )
            res = await runner.run(prompt)
            clean = res.clean_output or res.output
            return res.returncode, clean.strip(), res.errors.strip()

        rc, out, err = await _call_one(target_model)
        if rc == 0 and out:
            return out, "", target_model

        err_full = (err + "\n" + out).strip()
        is_limit = bool(re.search(r"Free usage limit reached|FreeUsageLimitError|QuotaExceeded|rate.?limit|limit reached|not available", err_full, re.I))

        if is_limit and target_model != "google/gemini-3.5-flash":
            logger.warning("Turnstone model %s hit limit: %s. Falling back to google/gemini-3.5-flash", target_model, err_full)
            rc_fb, out_fb, err_fb = await _call_one("google/gemini-3.5-flash")
            if rc_fb == 0 and out_fb:
                return out_fb, "", "google/gemini-3.5-flash"
            rc_fb2, out_fb2, err_fb2 = await _call_one("google/gemini-3.1-flash-lite")
            if rc_fb2 == 0 and out_fb2:
                return out_fb2, "", "google/gemini-3.1-flash-lite"

        return out, err_full or f"OpenCode exited with returncode {rc}", target_model

    async def run(
        self,
        prompt: str,
        history: list[dict[str, str]] | None = None,
        use_web: bool = True,
        use_brain: bool = True,
        deep_research: bool = False,
        model: str = "",
        max_tool_iterations: int = 6,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a full conversational turn or autonomous deep research investigation."""
        target_model = model or self.model

        # 1. Recall Brain memory
        brain_context = ""
        if use_brain:
            brain_context = self.brain.context_prompt(prompt)
            if brain_context:
                yield AgentEvent(
                    type="thought",
                    content="Recalling persistent memories from Turnstone Brain...",
                    metadata={"brain_context": brain_context},
                )

        # 2. If Deep Research is requested, execute the multi-step investigation pipeline
        if deep_research:
            async for ev in self._execute_deep_research(prompt, target_model):
                yield ev
            return

        # 3. Standard interactive / tool-using conversational turn
        convo_text = []
        if history:
            for msg in history[-8:]:
                role = "User" if msg.get("role") == "user" else "Turnstone"
                convo_text.append(f"{role}: {msg.get('content', '')}")

        current_prompt = prompt
        history_context = "\n".join(convo_text)
        iteration = 0

        collected_tools_used: list[dict[str, Any]] = []

        while iteration < max_tool_iterations:
            iteration += 1

            # Build full augmented prompt for OpenCode
            parts = [TURNSTONE_SYSTEM_PROMPT]
            if brain_context:
                parts.append(brain_context)
            if history_context:
                parts.append(f"Conversation History:\n{history_context}")

            parts.append(f"User Request: {current_prompt}")
            full_input = "\n\n".join(parts)

            yield AgentEvent(type="thought", content=f"Analyzing query via OpenCode ({target_model}, step {iteration})...")

            raw_response, err, effective_model = await self._execute_llm(full_input, target_model)

            if effective_model != target_model:
                yield AgentEvent(
                    type="thought",
                    content=f"⚡ Лимит OpenCode для {target_model} исчерпан. Ответ успешно обработан через бесплатную {effective_model}!",
                    metadata={"fallback_from": target_model, "model": effective_model},
                )

            if not raw_response:
                yield AgentEvent(type="error", content=err or "No response from OpenCode model")
                return

            # Check if model wants to call tools: ```tool:name\n{args}\n```
            tool_matches = re.findall(r"```tool:(\w+)\s*\n(.*?)\n```", raw_response, re.DOTALL)

            if not tool_matches:
                # No more tools called; yield final answer
                yield AgentEvent(
                    type="message",
                    content=raw_response,
                    metadata={"tools_used": collected_tools_used, "model": effective_model},
                )
                yield AgentEvent(type="done", content="Completed")
                return

            # Execute found tools sequentially
            tool_observations: list[str] = []
            for tool_name, raw_args in tool_matches:
                yield AgentEvent(
                    type="tool_start",
                    content=f"Calling tool `{tool_name}`...",
                    metadata={"tool": tool_name, "raw_args": raw_args},
                )

                try:
                    args = json.loads(raw_args.strip()) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {"input": raw_args.strip()}

                # Run tool in worker thread
                tool_output = await asyncio.to_thread(self.tools.dispatch, tool_name, args)
                tool_json = json.dumps(tool_output, ensure_ascii=False, indent=2)

                collected_tools_used.append({"tool": tool_name, "args": args, "output": tool_output})

                yield AgentEvent(
                    type="tool_end",
                    content=f"Tool `{tool_name}` completed.",
                    metadata={"tool": tool_name, "output": tool_output},
                )

                tool_observations.append(f"Observation from {tool_name}:\n{tool_json}")

            # Append tool observations and continue the loop
            obs_block = "\n\n".join(tool_observations)
            current_prompt += f"\n\n[System Tool Observations]:\n{obs_block}\nNow synthesize your answer or call the next tool if more evidence is needed."

        # If iterations exhausted, deliver what we have
        yield AgentEvent(
            type="message",
            content=raw_response or "Research completed after multiple tool steps.",
            metadata={"tools_used": collected_tools_used, "model": effective_model},
        )
        yield AgentEvent(type="done", content="Completed")

    async def _execute_deep_research(self, topic: str, target_model: str) -> AsyncIterator[AgentEvent]:
        """Multi-stage autonomous deep research investigation."""
        yield AgentEvent(type="thought", content=f"Initiating Autonomous Deep Research on: '{topic}'")

        # Stage 1: Generate Search Plan
        plan_prompt = f"""You are Turnstone Deep Research.
Topic: "{topic}"
Generate 3 to 4 distinct, highly effective search queries to thoroughly investigate this topic from different angles (latest facts, technical details, controversies, real-world data).
Return ONLY a JSON list of query strings, e.g. ["query 1", "query 2", "query 3"]."""

        yield AgentEvent(type="thought", content="Generating targeted search queries...")
        raw_plan, plan_err, eff_model = await self._execute_llm(plan_prompt, target_model)

        queries = [topic]
        try:
            m = re.search(r"\[.*?\]", raw_plan, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                    queries = parsed[:4]
        except Exception:
            queries = [topic, f"{topic} latest news analysis", f"{topic} overview guide"]

        yield AgentEvent(
            type="research_plan",
            content=f"Formulated search queries: {', '.join(f'`{q}`' for q in queries)}",
            metadata={"queries": queries},
        )

        # Stage 2: Execute Web Searches
        all_results: list[dict[str, Any]] = []
        visited_urls: set[str] = set()

        for q in queries:
            yield AgentEvent(type="tool_start", content=f"Searching: `{q}`...", metadata={"query": q})
            s_res = await asyncio.to_thread(self.tools.web_search, q, max_results=4)
            found = s_res.get("results", [])
            for item in found:
                u = item.get("url")
                if u and u not in visited_urls:
                    visited_urls.add(u)
                    all_results.append(item)
            yield AgentEvent(
                type="tool_end",
                content=f"Found {len(found)} sources for `{q}`",
                metadata={"results": found},
            )

        # Stage 3: Fetch Deep Content for Top Sources
        top_sources = all_results[:4]
        source_texts: list[str] = []

        for src in top_sources:
            u = src.get("url", "")
            title = src.get("title", u)
            yield AgentEvent(type="tool_start", content=f"Browsing source: {title}...", metadata={"url": u})
            b_res = await asyncio.to_thread(self.tools.web_browse, u, max_chars=3500)
            if b_res.get("ok"):
                text_snip = b_res.get("text", "")[:3000]
                source_texts.append(f"### Source: [{title}]({u})\n{text_snip}")
                yield AgentEvent(type="tool_end", content=f"Extracted content from {title}", metadata={"url": u})
            else:
                source_texts.append(f"### Source: [{title}]({u})\nSnippet: {src.get('snippet', '')}")
                yield AgentEvent(type="tool_end", content=f"Used summary snippet for {title}", metadata={"url": u})

        # Stage 4: Synthesize Deep Research Report
        yield AgentEvent(type="thought", content="Synthesizing final comprehensive research report...")

        synthesis_input = f"""You are Turnstone Deep Research.
Topic: "{topic}"

Collected Source Materials:
{"\n\n".join(source_texts)}

Write a comprehensive, authoritative Deep Research Report with the following structure:
1. Executive Summary (Direct, high-impact summary of key conclusions)
2. Core Findings & Technical Analysis (Detailed breakdown, metrics, chronological facts)
3. Nuances, Contradictions & Open Questions (What remains uncertain or contested)
4. Strategic Takeaways / Recommendations
5. Cited Sources (List of links examined)

Format cleanly in GitHub Flavored Markdown with bold highlights, tables if applicable, and callout quotes."""

        report, synth_err, eff_synth_model = await self._execute_llm(synthesis_input, target_model)

        if not report:
            report = f"# Research Brief: {topic}\n\nUnable to generate report: {synth_err or 'unknown error'}"

        # Stage 5: Save to Turnstone Brain Archive
        saved_meta = self.brain.save_research(topic, report, sources=list(visited_urls)[:8])

        yield AgentEvent(
            type="message",
            content=report,
            metadata={
                "deep_research": True,
                "saved_to_brain": saved_meta.get("file"),
                "sources_count": len(visited_urls),
                "model": eff_synth_model,
            },
        )
        yield AgentEvent(type="done", content=f"Deep research archived to Turnstone Brain as `{saved_meta.get('file')}`")
