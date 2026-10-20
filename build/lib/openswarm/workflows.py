"""User-defined pipelines (config `workflows:`): agent steps, parallel groups and foreach fan-out.

Placeholders in prompts: {request}, {<earlier step id>}, and {item} inside foreach steps.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

from .config import WorkflowSpec, WorkflowStep
from .runner import strip_ansi

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import Orchestrator, RunSummary


class StepFailed(RuntimeError):
    pass


def parse_items(text: str, cap: int = 12) -> list[str]:
    """Items from a step output: a JSON array (strings or objects) or one item per non-empty line."""
    text = strip_ansi(text)
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, list) and obj:
            out = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in obj]
            return [o for o in out if o.strip()][:cap]
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln][:cap]


def fmt(template: str, data: dict[str, Any]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(data.get(m.group(1), m.group(0))), template)


async def run_workflow(orch: "Orchestrator", run_id: str, wf: WorkflowSpec, prompt: str, summary: "RunSummary") -> None:
    ctx: dict[str, Any] = {"request": prompt}
    cwd = orch.git.workdir
    sem = orch._limit(12)
    last_id, final, final_id = "", "", ""

    async def agent(step: WorkflowStep, sid: str, title: str, extra: dict[str, Any] | None = None) -> str:
        slot = step.role if step.role in ("planner", "coder", "reviewer") else "coder"
        role = f"wf{'ro' if step.readonly else 'rw'}_{slot}"
        async with sem:
            res = await orch._agent(sid, title, role, fmt(step.prompt, {**ctx, **(extra or {})}), cwd)
        if not res.ok:
            raise StepFailed(step.id)
        return res.clean_output.strip()

    try:
        for step in wf.steps:
            sid = f"{run_id}-{step.id}"
            if step.kind == "agent":
                out = await agent(step, sid, step.id)
            elif step.kind == "parallel":
                outs = await asyncio.gather(*(agent(sub, f"{sid}-{sub.id}", f"{step.id}/{sub.id}") for sub in step.steps))
                for sub, o in zip(step.steps, outs):
                    ctx[sub.id] = o
                out = "\n\n".join(f"### {sub.id}\n{o}" for sub, o in zip(step.steps, outs))
            else:  # foreach
                items = parse_items(str(ctx.get(step.over, "")))
                if not items:
                    raise StepFailed(f"{step.id}: nothing to iterate over in '{step.over}'")
                await orch._log(f"{step.id}: {len(items)} item(s) in parallel")
                outs = await asyncio.gather(*(agent(step, f"{sid}-{i}", f"{step.id} #{i}: {it[:28]}", {"item": it}) for i, it in enumerate(items, 1)))
                out = "\n\n".join(f"### {it}\n{o}" for it, o in zip(items, outs))
            ctx[step.id], last_id = out, sid
            if step.result:
                final, final_id = out, sid
    except StepFailed as exc:
        summary.failed.append(f"step {exc}")
        await orch._log(f"Workflow '{wf.id}' stopped: step {exc} failed")
        return
    final, final_id = final or ctx.get(wf.steps[-1].id, ""), final_id or last_id
    summary.result = final
    summary.merged.append(wf.id)
    from .orchestrator import _emit  # local import avoids a cycle

    await _emit(orch.hooks.on_result, final_id, final, "workflow")
