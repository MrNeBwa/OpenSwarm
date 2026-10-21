import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from openswarm.broker import Broker
from openswarm.comm import comm_main


class MessageBus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "swarm.db"
        self.db = Broker(self.path)
        self.run = self.db.create_run("storm", "test")
        for aid in ("b", "c", "target"):
            self.db.add_task(self.run, aid, aid, "test task", "coder")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_send_claim_and_history(self):
        mid = self.db.send_message(self.run, "a", "b", "hello", "question")
        self.assertGreater(mid, 0)
        first = self.db.inbox(self.run, "b")
        self.assertEqual([m["body"] for m in first], ["hello"])
        self.assertEqual(self.db.inbox(self.run, "b"), [])  # claimed exactly once
        history = self.db.message_history(self.run)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["read_by"], "b")

    def test_peek_does_not_claim_and_broadcast_is_per_agent(self):
        self.db.send_message(self.run, "a", "*", "all hands")
        self.assertEqual(len(self.db.inbox(self.run, "b", peek=True)), 1)
        self.assertEqual(len(self.db.inbox(self.run, "b")), 1)
        self.assertEqual(len(self.db.inbox(self.run, "c")), 1)  # each agent claims its own copy

    def test_lookup_and_messages_since_are_read_only(self):
        first = self.db.send_message(self.run, "a", "b", "q1", "question")
        second = self.db.send_message(self.run, "a", "c", "q2", "question")
        self.assertEqual(self.db.get_message(self.run, first)["body"], "q1")
        self.assertIsNone(self.db.get_message(self.run, 99999))
        since = self.db.messages_since(self.run, first)
        self.assertEqual([m["id"] for m in since], [second])
        self.assertEqual(self.db.messages_since(self.run, 0)[0]["body"], "q1")
        # Viewing history must not acknowledge anything for the recipients.
        self.assertEqual(len(self.db.inbox(self.run, "b")), 1)
        self.assertEqual(len(self.db.inbox(self.run, "c")), 1)

    def test_reply_uses_original_sender(self):
        question = self.db.get_message(self.run, self.db.send_message(self.run, "a", "b", "q", "question"))
        self.assertEqual(question["sender"], "a")
        mid = self.db.send_message(self.run, "b", question["sender"], "a1", "answer", question["id"])
        reply = self.db.get_message(self.run, mid)
        self.assertEqual((reply["recipient"], reply["kind"], reply["reply_to"]), ("a", "answer", question["id"]))

    def test_cli_ask_check_reply_is_non_blocking(self):
        def cli(*argv, agent):
            buf, saved = io.StringIO(), {k: os.environ.get(k) for k in ("OPENSWARM_BROKER_DB", "OPENSWARM_RUN_ID", "OPENSWARM_AGENT")}
            os.environ.update(OPENSWARM_BROKER_DB=str(self.path), OPENSWARM_RUN_ID=self.run, OPENSWARM_AGENT=agent)
            try:
                with redirect_stdout(buf):
                    rc = comm_main(list(argv))
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            return rc, json.loads(buf.getvalue())

        rc, asked = cli("ask", "--to", "b", "--body", "what shape?", agent="a")
        self.assertEqual(rc, 0)
        self.assertTrue(asked["non_blocking"])
        rc, got = cli("check", agent="b")
        self.assertEqual((rc, got["count"], got["messages"][0]["body"]), (0, 1, "what shape?"))
        rc, answered = cli("reply", str(asked["id"]), "--body", "a tuple", agent="b")
        self.assertEqual((rc, answered["to"], answered["reply_to"]), (0, "a", asked["id"]))
        rc, got = cli("check", agent="a")
        self.assertEqual((rc, got["count"], got["messages"][0]["kind"]), (0, 1, "answer"))
        rc, empty = cli("check", agent="a")
        self.assertEqual((rc, empty["count"]), (0, 0))

    def test_unknown_recipient_and_validation(self):
        with self.assertRaises(ValueError):
            self.db.send_message(self.run, "a", "b", "   ")
        self.db.send_message(self.run, "a", "nobody", "ok")
        self.assertEqual(len(self.db.inbox(self.run, "nobody")), 1)

    def test_concurrent_senders_lose_nothing(self):
        def send(i):
            db = Broker(self.path)
            try:
                db.send_message(self.run, f"s{i}", "target", f"m{i}")
            finally:
                db.close()

        threads = [threading.Thread(target=send, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = self.db.inbox(self.run, "target", limit=100)
        self.assertEqual(len(got), 16)
        self.assertEqual(len({m["body"] for m in got}), 16)


if __name__ == "__main__":
    unittest.main()
