"""Shared fixtures: temp dirs, a git repo, and a config pointing at the bundled fake `opencode`."""
import asyncio, os, subprocess, tempfile, unittest
from pathlib import Path

from openswarm.config import default_config
from openswarm.git_manager import GitManager
from openswarm.broker import Broker
from openswarm.orchestrator import Hooks, Orchestrator, Selection
from openswarm.servers import ServerPool
import openswarm.testing.fake_opencode as _fake

FAKE = _fake.__file__


class Env(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oswarm-test-"))
        self._env = dict(os.environ)
        for k, d in (("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "cfg"), ("HOME", "home")):
            os.environ[k] = str(self.tmp / d)
            (self.tmp / d).mkdir()
        os.environ.update(FAKE_LOG=str(self.tmp / "fake.log"), FAKE_TITLE_FILE=str(self.tmp / "title"),
                          FAKE_FLAKY=str(self.tmp / "flaky"), FAKE_REVIEW_MARK=str(self.tmp / "rej"), FAKE_DELAY="0.1")
        os.environ.pop("FAKE_API_STYLE", None)
        os.environ.pop("FAKE_PLAN", None)
        self.cfg = default_config()
        self.cfg.opencode.binary = FAKE
        self.cfg.opencode.retry_backoff_seconds = 0
        self.cfg.opencode.role_permissions = {}
        self.cfg.opencode.discover_models = False
        self.pools: list[ServerPool] = []

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._env)

    def plain(self, name="plain") -> Path:
        d = self.tmp / name; d.mkdir(); return d

    def repo(self, name="repo") -> Path:
        d = self.plain(name)
        for c in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *c], cwd=d, check=True)
        (d / "README").write_text("hi")
        subprocess.run(["git", "add", "."], cwd=d, check=True); subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
        return d

    async def run_mode(self, workdir: Path, mode: str, prompt="do the thing", pool=None, hooks=None, **sel):
        git = await GitManager.discover(workdir); await git.ensure_ready()
        broker = Broker(git.swarm_dir / "swarm.db")
        logs, results, events = [], [], []
        h = hooks or Hooks()
        h.on_log = logs.append
        h.on_result = lambda a, t, k: results.append((k, t))
        h.on_event = lambda a, k, p: events.append((a, k, p))
        sel.setdefault("skills", [])
        for k in ("planner_model", "coder_model", "reviewer_model"):
            sel.setdefault(k, "opencode/fake-free")
        s = await Orchestrator(self.cfg, git, broker, Selection(mode=mode, workdir=workdir, **sel), h, pool).run(prompt)
        broker.close()
        return s, logs, results, events
