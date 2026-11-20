"""Unit tests for the babysit pass: review feedback flows back to the agent."""

import tempfile
import unittest

from factory import babysit
from factory import db as dbmod
from factory import worker as workermod


def comment(cid, login, body):
    return {"id": cid, "user": {"login": login}, "body": body}


class TestIsHumanComment(unittest.TestCase):
    def test_plain_human_comment_passes(self):
        self.assertTrue(babysit.is_human_comment(comment(5, "david", "rename this"), 0))

    def test_factory_marker_is_skipped(self):
        body = babysit.AGENT_MARKER + "\n**GUI preview** ..."
        self.assertFalse(babysit.is_human_comment(comment(6, "david", body), 0))

    def test_bot_login_is_skipped(self):
        self.assertFalse(
            babysit.is_human_comment(comment(7, "dependabot[bot]", "bump"), 0))

    def test_cursor_skips_answered_comments(self):
        self.assertFalse(babysit.is_human_comment(comment(5, "david", "old"), 5))
        self.assertTrue(babysit.is_human_comment(comment(6, "david", "new"), 5))

    def test_garbage_id_is_skipped(self):
        self.assertFalse(babysit.is_human_comment({"id": None, "body": "x"}, 0))


class TestReplyBody(unittest.TestCase):
    def test_marker_first_and_quotes_every_comment(self):
        body = babysit.reply_body(
            [comment(10, "david", "use liters here\nsecond line"),
             comment(11, "david", "drop the supertable column")],
            ["comment 10: switched to liters - ticket says liters only"],
            changed=True, tests_note="Tests were re-run and pass.")
        self.assertTrue(body.startswith(babysit.AGENT_MARKER))
        self.assertIn("> use liters here", body)
        self.assertIn("> drop the supertable column", body)
        self.assertIn("comment 10: switched to liters", body)
        self.assertIn("Tests were re-run and pass.", body)
        self.assertIn("Mutation gate is not re-run", body)

    def test_no_decisions_with_changes_says_read_the_commit(self):
        body = babysit.reply_body([comment(10, "david", "x")], [],
                                  changed=True, tests_note="t")
        self.assertIn("read the pushed commit", body)

    def test_no_changes_is_stated_not_hidden(self):
        body = babysit.reply_body([comment(10, "david", "x")], [],
                                  changed=False, tests_note="No code changed.")
        self.assertIn("no code change this round", body)


class TestFeedbackPrompt(unittest.TestCase):
    def test_prompt_carries_comments_and_contract(self):
        with tempfile.TemporaryDirectory() as td:
            ticket = {"id": 3, "ref": "T-3", "title": "fix totals",
                      "body": "show totals per pump", "max_retries": 3}
            path = babysit.write_feedback_prompt(
                td, ticket, [comment(99, "david", "the diesel row is wrong")],
                out_dir=td)
            text = open(path, encoding="utf-8").read()
        self.assertIn("the diesel row is wrong", text)
        self.assertIn("comment 99", text)
        self.assertIn("Address EVERY comment", text)
        self.assertIn("Do not commit, push", text)


class TestAddress(unittest.TestCase):
    def setUp(self):
        self.con = dbmod.connect(":memory:")
        dbmod.init(self.con)
        self.cfg = {"babysit": {"enabled": True}, "repo": ".",
                    "agent": {"cheap": {}, "strong": {}}}
        self._orig = (babysit.fetch_comments, babysit.run_round,
                      babysit.post_reply, workermod._engine_lock,
                      workermod._release_engine_lock, babysit.wt._gh_binary)
        workermod._engine_lock = lambda *a, **k: object()
        workermod._release_engine_lock = lambda fh: None
        babysit.wt._gh_binary = lambda cfg_path=None: "gh"

    def tearDown(self):
        (babysit.fetch_comments, babysit.run_round, babysit.post_reply,
         workermod._engine_lock, workermod._release_engine_lock,
         babysit.wt._gh_binary) = self._orig

    def _ticket(self, url="https://github.com/o/r/pull/26", status="pr_open"):
        tid = dbmod.add_ticket(self.con, "dispenser table", "body")
        dbmod.set_pr(self.con, tid, url)
        dbmod.finish(self.con, tid, status)
        return tid

    def _feed(self, comments):
        babysit.fetch_comments = lambda gh, o, r, n, token=None: comments

    def test_disabled_config_does_nothing(self):
        self.cfg["babysit"]["enabled"] = False
        self.assertEqual(babysit.address(self.cfg, self.con), [])

    def test_no_new_comments_is_silent(self):
        tid = self._ticket()
        self._feed([comment(1, "david", babysit.AGENT_MARKER + "\nanswered"),
                    comment(2, "ci[bot]", "beep")])
        self.con.execute("UPDATE tickets SET feedback_after=0 WHERE id=?", (tid,))
        babysit.run_round = lambda *a, **k: self.fail("must not run")
        self.assertEqual(babysit.address(self.cfg, self.con), [])

    def test_human_comment_replies_and_advances_cursor(self):
        tid = self._ticket()
        self._feed([comment(5, "david", "use liters")])
        babysit.run_round = lambda cfg, con, t, c: (
            "replied", "", ["comment 5: done - liters only"], True)
        posted = []
        babysit.post_reply = lambda url, body, gh_path=None, token=None: (
            posted.append((url, body)))

        out = babysit.address(self.cfg, self.con)
        self.assertEqual(out, [(tid, "replied")])
        self.assertEqual(posted[0][0], "https://github.com/o/r/pull/26")
        self.assertIn(babysit.AGENT_MARKER, posted[0][1])
        row = dbmod.get_ticket(self.con, tid)
        self.assertEqual(row["feedback_after"], 5)
        self.assertEqual(row["status"], "pr_open")

        self.assertEqual(babysit.address(self.cfg, self.con), [])

    def test_failure_escalates_and_still_advances_cursor(self):
        tid = self._ticket()
        self._feed([comment(7, "david", "broken")])
        babysit.run_round = lambda cfg, con, t, c: (
            "failed", "tests failing after feedback round", [], True)
        babysit.post_reply = lambda *a, **k: self.fail("must not reply")

        out = babysit.address(self.cfg, self.con)
        self.assertEqual(out, [(tid, "escalated")])
        row = dbmod.get_ticket(self.con, tid)
        self.assertEqual(row["feedback_after"], 7)
        esc = self.con.execute(
            "SELECT why FROM escalations WHERE ticket_id=?", (tid,)).fetchall()
        self.assertIn("tests failing after feedback round", esc[-1]["why"])

    def test_deferred_round_keeps_the_cursor_for_retry(self):
        tid = self._ticket()
        self._feed([comment(8, "david", "please fix")])
        babysit.run_round = lambda cfg, con, t, c: (
            "deferred", "engine unreachable", [], False)
        babysit.post_reply = lambda *a, **k: self.fail("must not reply")

        out = babysit.address(self.cfg, self.con)
        self.assertEqual(out, [(tid, "deferred (engine unreachable)")])
        self.assertEqual(dbmod.get_ticket(self.con, tid)["feedback_after"], 0)

    def test_over_budget_escalates_without_running_the_agent(self):
        tid = self._ticket()
        self.con.execute("UPDATE tickets SET budget_usd=0.01 WHERE id=?", (tid,))
        dbmod.start_run(self.con, tid, 1)
        self.con.execute("UPDATE runs SET cost_usd=1.0 WHERE ticket_id=?", (tid,))
        self._feed([comment(9, "david", "one more thing")])
        babysit.run_round = lambda *a, **k: self.fail("must not run")

        out = babysit.address(self.cfg, self.con)
        self.assertEqual(out, [(tid, "escalated (budget)")])
        self.assertEqual(dbmod.get_ticket(self.con, tid)["feedback_after"], 9)

    def test_one_round_per_pass_leaves_the_rest_queued(self):
        first = self._ticket("https://github.com/o/r/pull/24")
        second = self._ticket("https://github.com/o/r/pull/26")
        babysit.fetch_comments = lambda gh, o, r, n, token=None: [
            comment(100 + n, "david", "feedback")]
        babysit.run_round = lambda cfg, con, t, c: ("replied", "", [], False)
        babysit.post_reply = lambda *a, **k: None

        out = dict(babysit.address(self.cfg, self.con, limit=1))
        self.assertEqual(out[first], "replied")
        self.assertEqual(out[second], "waiting (one round per pass)")
        self.assertEqual(dbmod.get_ticket(self.con, second)["feedback_after"], 0)

    def test_fetch_failure_is_reported_not_raised(self):
        tid = self._ticket()
        def boom(*a, **k):
            raise babysit.BabysitError("gh api comments: 404")
        babysit.fetch_comments = boom
        out = babysit.address(self.cfg, self.con)
        self.assertEqual(out, [(tid, "fetch failed: gh api comments: 404")])


class TestCheckoutGuard(unittest.TestCase):
    def test_checkout_refuses_missing_remote_branch(self):
        import os
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init", "-q", "--bare", os.path.join(td, "origin.git")],
                           check=True)
            work = os.path.join(td, "work")
            subprocess.run(["git", "clone", "-q", os.path.join(td, "origin.git"), work],
                           check=True)
            from factory import worktree as wt
            with self.assertRaises(wt.WorktreeError) as ctx:
                wt.checkout(work, "agent/never-pushed-1")
            self.assertIn("does not exist", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
