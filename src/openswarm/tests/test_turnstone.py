"""Unit tests for Turnstone autonomous research engine, brain, and tools."""

import asyncio
from pathlib import Path
import tempfile
import pytest

from openswarm.turnstone.brain import TurnstoneBrain, MemoryItem
from openswarm.turnstone.tools import TurnstoneTools, clean_html_to_text
from openswarm.turnstone.engine import TurnstoneEngine, AgentEvent


def test_clean_html_to_text():
    raw = """
    <html>
      <head><title>Test Title</title><style>.test{color:red;}</style></head>
      <body>
        <script>console.log("ignore");</script>
        <h1>Main Heading</h1>
        <p>This is a <b>bold</b> paragraph with &amp; entity.</p>
        <div>Second section.</div>
      </body>
    </html>
    """
    text = clean_html_to_text(raw)
    assert "Main Heading" in text
    assert "This is a bold paragraph with & entity." in text
    assert "Second section." in text
    assert "console.log" not in text
    assert ".test" not in text


def test_turnstone_brain_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        bdir = Path(tmp)
        brain = TurnstoneBrain(root=bdir)

        # 1. Remember facts
        m1 = brain.remember("User prefers Google Gemini 3.5 Flash", category="preference", tags=["model", "gemini"])
        assert m1.id
        assert m1.category == "preference"

        m2 = brain.remember("OpenSwarm is focused on OpenCode instead of Kilo", category="fact", tags=["openswarm", "opencode"])

        # 2. Recall
        recalled = brain.recall("What model does user prefer?")
        assert len(recalled) >= 1
        assert any("Gemini 3.5" in r["content"] for r in recalled)

        # 3. Context Prompt
        ctx = brain.context_prompt("opencode focus")
        assert "Turnstone Brain Memory" in ctx
        assert "OpenCode instead of Kilo" in ctx

        # 4. Save Research Brief
        res_meta = brain.save_research("Quantum Computing Advancements", "# Overview\n\nQuantum supremacy achieved.")
        assert res_meta["topic"] == "Quantum Computing Advancements"
        assert res_meta["file"].endswith(".md")

        researches = brain.list_researches()
        assert len(researches) == 1
        assert "Quantum" in researches[0]["title"]

        content = brain.get_research_content(res_meta["file"])
        assert "Quantum supremacy achieved" in content

        # 5. Forget
        ok = brain.forget(m1.id)
        assert ok
        assert not any(m.id == m1.id for m in brain._memories)


def test_turnstone_tools_filesystem_and_bash():
    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        brain = TurnstoneBrain(root=tdir / "brain")
        tools = TurnstoneTools(brain=brain, workdir=tdir)

        # 1. File Write and View
        w_res = tools.file_write("hello.txt", "Turnstone was here.\nSecond line.")
        assert w_res.get("ok") is True

        v_res = tools.file_view("hello.txt")
        assert "Turnstone was here." in v_res.get("content", "")

        # 2. Python Eval
        py_res = tools.python_eval("print(7 * 6)")
        assert py_res.get("ok") is True
        assert py_res.get("stdout") == "42"

        # 3. Bash Run
        sh_res = tools.bash_run("echo 'tool test'")
        assert sh_res.get("ok") is True
        assert sh_res.get("stdout") == "tool test"

        # 4. Dangerous Bash blocked
        dang_res = tools.bash_run("rm -rf /")
        assert dang_res.get("error") is not None
        assert dang_res.get("returncode") == -1


def test_turnstone_tools_dispatch():
    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        brain = TurnstoneBrain(root=tdir / "brain")
        tools = TurnstoneTools(brain=brain, workdir=tdir)

        # Dispatch brain remember
        r = tools.dispatch("brain_remember", {"fact": "Turnstone is fast", "category": "insight", "tags": "speed"})
        assert r.get("ok") is True

        # Dispatch brain search
        s = tools.dispatch("brain_search", {"query": "fast"})
        assert len(s.get("memories", [])) >= 1

        # Dispatch unknown tool
        u = tools.dispatch("non_existent_tool", {})
        assert "Unknown tool" in u.get("error", "")


@pytest.mark.asyncio
async def test_turnstone_engine_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        brain = TurnstoneBrain(root=tdir / "brain")
        engine = TurnstoneEngine(workdir=tdir, brain=brain)

        # Verify engine initializes with OpenCode default
        assert "google/" in engine.model or "gemini" in engine.model or "opencode/" in engine.model
        assert engine.brain is brain
        assert engine.tools is not None


@pytest.mark.asyncio
async def test_turnstone_server_models_api():
    from openswarm.turnstone.server import TurnstoneServer
    import json

    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        server = TurnstoneServer(workdir=tdir, port=0)
        srv = await asyncio.start_server(server.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]

        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /api/turnstone/models HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await w.drain()

            status_line = await r.readline()
            assert b"200 OK" in status_line

            # Read headers
            content_len = 0
            while True:
                line = await r.readline()
                if line in (b"\r\n", b""):
                    break
                if line.lower().startswith(b"content-length:"):
                    content_len = int(line.split(b":")[1].strip())

            body = await r.readexactly(content_len)
            data = json.loads(body.decode())
            assert "categories" in data
            all_ids = [m["id"] for c in data["categories"] for m in c["models"]]
            assert "opencode/deepseek-v4.1-flash-free" in all_ids
            assert "google/gemini-3.5-flash" in all_ids
            w.close()
            await w.wait_closed()
        finally:
            srv.close()
            await srv.wait_closed()


@pytest.mark.asyncio
async def test_turnstone_engine_free_limit_fallback(monkeypatch):
    from openswarm.runner import RunResult

    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        engine = TurnstoneEngine(workdir=tdir, model="opencode/deepseek-v4.1-flash-free")

        # Mock OpenCodeRunner.run to simulate free usage limit on opencode/*-free
        # and successful response on google/gemini-3.5-flash
        called_models = []

        async def fake_run(self, prompt: str):
            called_models.append(self.model)
            if self.model == "opencode/deepseek-v4.1-flash-free":
                return RunResult(1, "", "Error: Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going.")
            elif self.model == "google/gemini-3.5-flash":
                return RunResult(0, "Answer generated by Google Gemini 3.5 Flash via OpenCode", "")
            return RunResult(1, "", "Unknown model")

        monkeypatch.setattr("openswarm.turnstone.engine.OpenCodeRunner.run", fake_run)

        out, err, effective = await engine._execute_llm("test prompt", "opencode/deepseek-v4.1-flash-free")
        assert not err
        assert "Google Gemini 3.5 Flash" in out
        assert effective == "google/gemini-3.5-flash"
        assert called_models == ["opencode/deepseek-v4.1-flash-free", "google/gemini-3.5-flash"]

