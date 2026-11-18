"""Phase 2: unattended tick, issue intake, webhook dialects, env secrets."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from factory import agent as agentmod
from factory import db as dbmod
from factory import notify as notifymod


def fresh_db(case=None):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = dbmod.connect(path)
    dbmod.init(con)
    if case is not None:
        case.addCleanup(os.unlink, path)
        case.addCleanup(con.close)
    return con


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestNotifyDialect(unittest.TestCase):
    def _sent(self, fmt=None):
        sent = {}

        def fake(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return _Resp()

        kwargs = {"webhook": "https://hooks.invalid/x"}
        if fmt:
            kwargs["fmt"] = fmt
        with patch("urllib.request.urlopen", fake):
            ok = notifymod.notify("T", "hello", **kwargs)
        return ok, sent["body"]

    def test_slack_is_default(self):
        ok, body = self._sent()
        self.assertTrue(ok)
        self.assertIn("text", body)
        self.assertNotIn("content", body)

    def test_discord_uses_content(self):
        ok, body = self._sent(fmt="discord")
        self.assertTrue(ok)
        self.assertIn("content", body)
        self.assertNotIn("text", body)

    def test_post_takes_dialect_from_config(self):
        sent = {}

        def fake(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return _Resp()

        cfg = {"webhook_url": "https://hooks.invalid/y", "webhook_format": "discord"}
        with patch("urllib.request.urlopen", fake):
            ok = notifymod.post(cfg, "T", "x")
        self.assertTrue(ok)
        self.assertIn("content", sent["body"])

    def test_post_without_config_is_print_only(self):
        self.assertFalse(notifymod.post(None, "T", "x"))


class TestConfigEnvSecrets(unittest.TestCase):
    def test_env_prefix_expands_and_missing_is_empty(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"gh_token": "env:FACTORY_TEST_TOKEN",
                       "nested": [{"k": "env:FACTORY_TEST_MISSING"}],
                       "plain": "keep:me"}, fh)
        self.addCleanup(os.unlink, path)
        with patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "tok"}):
            cfg = agentmod.load_config(path)
        self.assertEqual(cfg["gh_token"], "tok")
        self.assertEqual(cfg["nested"][0]["k"], "")
        self.assertEqual(cfg["plain"], "keep:me")


class TestTick(unittest.TestCase):
    def setUp(self):
        from factory import tick as tickmod
        self.tickmod = tickmod
        self.con = fresh_db(self)
        self.cfg = {"repo": "."}

    def _run(self, outcomes, running=(), changes=(), errors=(), board_fail=False,
             max_tickets=3):
        it = iter(outcomes)
        board = Exception("board down") if board_fail else None
        with patch.object(self.tickmod.worker, "watchdog", return_value=0), \
             patch.object(self.tickmod.dbmod, "running_pids",
                          return_value=list(running)), \
             patch.object(self.tickmod.syncmod, "sync",
                          return_value=(list(changes), list(errors))), \
             patch.object(self.tickmod.issuesmod, "import_issues",
                          return_value=("o/r", [], [])), \
             patch.object(self.tickmod.boardmod, "sync_board", side_effect=board), \
             patch.object(self.tickmod.worker, "process_once",
                          side_effect=lambda *a, **k: next(it, "idle")), \
             patch.object(self.tickmod.notify, "post", return_value=True) as post:
            s = self.tickmod.tick(self.cfg, self.con, max_tickets=max_tickets)
        return s, post

    def test_busy_skips_everything(self):
        s, post = self._run([], running=[(1234, "t1")])
        self.assertTrue(s["busy"])
        self.assertEqual(s["outcomes"], [])
        self.assertEqual(s["sync_changes"], 0)

    def test_pr_ready_alerts_once(self):
        s, post = self._run(["pr_open https://github.com/o/r/pull/1", "idle"])
        self.assertEqual(len(s["alerts"]), 1)
        self.assertIn("PR ready", s["alerts"][0])
        post.assert_called_once()

    def test_idle_cycle_is_silent(self):
        s, post = self._run(["idle"])
        self.assertTrue(s["idle"])
        self.assertEqual(s["alerts"], [])
        post.assert_not_called()

    def test_merged_moves_alert(self):
        s, post = self._run(["idle"], changes=[(7, "pr_open", "merged",
                                                "approved", "david", "http://p/1")])
        self.assertIn("ticket 7 merged", s["alerts"][0])

    def test_issue_intake_joins_queue_and_alerts(self):
        with patch.object(self.tickmod.worker, "watchdog", return_value=0), \
             patch.object(self.tickmod.dbmod, "running_pids", return_value=[]), \
             patch.object(self.tickmod.syncmod, "sync", return_value=([], [])), \
             patch.object(self.tickmod.issuesmod, "import_issues",
                          return_value=("o/r", [(42, 7)], [("43", "t3")])) as imp, \
             patch.object(self.tickmod.boardmod, "sync_board"), \
             patch.object(self.tickmod.worker, "process_once", return_value="idle"), \
             patch.object(self.tickmod.notify, "post", return_value=True):
            s = self.tickmod.tick(self.cfg, self.con)
        self.assertEqual(s["issues_imported"], 1)
        self.assertIn("issue #42 -> ticket 7", s["alerts"])

    def test_dead_tracker_does_not_stop_the_queue(self):
        from factory import issues as issuesmod
        with patch.object(self.tickmod.worker, "watchdog", return_value=0), \
             patch.object(self.tickmod.dbmod, "running_pids", return_value=[]), \
             patch.object(self.tickmod.syncmod, "sync", return_value=([], [])), \
             patch.object(self.tickmod.issuesmod, "import_issues",
                          side_effect=issuesmod.IssueError("gh down")), \
             patch.object(self.tickmod.boardmod, "sync_board"), \
             patch.object(self.tickmod.worker, "process_once",
                          return_value="pr_open http://p/1") as proc, \
             patch.object(self.tickmod.notify, "post", return_value=True):
            s = self.tickmod.tick(self.cfg, self.con, max_tickets=1)
        self.assertTrue(any("issue import failed" in a for a in s["alerts"]))
        proc.assert_called_once()

    def test_sync_failure_alerts_not_raises(self):
        from factory import sync as syncmod
        it = iter(["idle"])
        with patch.object(self.tickmod.worker, "watchdog", return_value=0), \
             patch.object(self.tickmod.dbmod, "running_pids", return_value=[]), \
             patch.object(self.tickmod.syncmod, "sync",
                          side_effect=syncmod.SyncError("gh down")), \
             patch.object(self.tickmod.issuesmod, "import_issues",
                          return_value=("o/r", [], [])), \
             patch.object(self.tickmod.boardmod, "sync_board"), \
             patch.object(self.tickmod.worker, "process_once",
                          side_effect=lambda *a, **k: next(it, "idle")), \
             patch.object(self.tickmod.notify, "post", return_value=True):
            s = self.tickmod.tick(self.cfg, self.con)
        self.assertEqual(s["sync_errors"], 1)
        self.assertIn("sync failed", " ".join(s["alerts"]))

    def test_board_failure_does_not_stop_building(self):
        s, _ = self._run(["pr_open http://p/2", "idle"], board_fail=True)
        self.assertIsNotNone(s["board_error"])
        self.assertEqual(len(s["outcomes"]), 2)

    def test_paused_queue_does_not_build(self):
        dbmod.set_paused(self.con, True)
        s, _ = self._run(["pr_open http://p/3"])
        self.assertEqual(s["outcomes"], [])

    def test_max_tickets_caps_the_batch(self):
        s, _ = self._run(["pr_open http://p/1"] * 5, max_tickets=2)
        self.assertEqual(len(s["outcomes"]), 2)


class TestIssueIntake(unittest.TestCase):
    def test_parse_issues_reads_tier_labels(self):
        from factory import issues as issuesmod
        payload = json.dumps([
            {"number": 5, "title": "Fix x", "body": "b",
             "labels": [{"name": "factory"}]},
            {"number": 6, "title": "Hard y", "body": "",
             "labels": [{"name": "factory"}, {"name": "tier:strong"}]},
        ])
        rows = issuesmod.parse_issues(payload)
        self.assertIsNone(rows[0]["tier"])
        self.assertEqual(rows[1]["tier"], "strong")
        self.assertEqual(rows[0]["number"], 5)

    def test_repo_slug_accepts_both_url_forms(self):
        from factory import issues as issuesmod
        for url in ("git@github.com:acme/widgets.git",
                    "https://github.com/acme/widgets",
                    "https://github.com/acme/widgets/"):
            with patch.object(issuesmod.wt, "_git", return_value=url):
                self.assertEqual(issuesmod.repo_slug({"repo": "."}), "acme/widgets")

    def test_bad_origin_url_raises(self):
        from factory import issues as issuesmod
        with patch.object(issuesmod.wt, "_git", return_value=""):
            self.assertRaises(issuesmod.IssueError, issuesmod.repo_slug, {"repo": "."})

    def _gh_patches(self, payload):
        from factory import issues as issuesmod
        return (patch.object(issuesmod.wt, "_git",
                             return_value="git@github.com:a/b.git"),
                patch.object(issuesmod.wt, "_gh_binary", return_value="gh"),
                patch.object(issuesmod.wt, "_token_env", return_value={}),
                patch.object(issuesmod.subprocess, "run", return_value=type(
                    "R", (), {"returncode": 0, "stdout": payload, "stderr": ""})()))

    def test_import_is_idempotent_by_issue_number(self):
        import contextlib
        from factory import issues as issuesmod
        con = fresh_db(self)
        payload = json.dumps([{"number": 9, "title": "T", "body": "", "labels": []}])
        results = []
        with contextlib.ExitStack() as stack:
            for p in self._gh_patches(payload):
                stack.enter_context(p)
            for _ in range(2):
                results.append(issuesmod.import_issues({"repo": "."}, con))
        slug, imported, skipped = results[0]
        self.assertEqual(slug, "a/b")
        self.assertEqual(len(imported), 1)
        self.assertEqual(results[1][1], [])
        self.assertEqual(results[1][2], [(9, imported[0][1])])
        row = con.execute("SELECT issue_number, source, ref FROM tickets").fetchone()
        self.assertEqual(row["issue_number"], 9)
        self.assertEqual(row["source"], "github-issue")
        self.assertEqual(row["ref"], "#9")


class TestTicketIssueNumber(unittest.TestCase):
    def test_add_ticket_stores_issue_number(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "t", issue_number=42)
        self.assertEqual(dbmod.get_ticket(con, tid)["issue_number"], 42)

    def test_plain_ticket_has_null_issue_number(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "t")
        self.assertIsNone(dbmod.get_ticket(con, tid)["issue_number"])


if __name__ == "__main__":
    unittest.main()
