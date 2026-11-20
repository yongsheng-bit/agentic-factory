import os
import tempfile
import unittest
from unittest.mock import patch

from factory import db as dbmod
from factory import worker as workermod


def fresh_db():
    con = dbmod.connect(os.path.join(tempfile.mkdtemp(), "f.db"))
    dbmod.init(con)
    return con


class CountByStatus(unittest.TestCase):
    def test_counts_only_that_status(self):
        con = fresh_db()
        a = dbmod.add_ticket(con, "a")
        dbmod.add_ticket(con, "b")
        dbmod.finish(con, a, "pr_open")
        self.assertEqual(dbmod.count_by_status(con, "pr_open"), 1)
        self.assertEqual(dbmod.count_by_status(con, "queued"), 1)


class CapColumnMigration(unittest.TestCase):
    def test_old_control_table_gains_column(self):
        con = dbmod.connect(os.path.join(tempfile.mkdtemp(), "old.db"))
        con.executescript(
            "CREATE TABLE control (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " paused INTEGER NOT NULL DEFAULT 0);")
        dbmod.init(con)
        self.assertFalse(dbmod.get_cap_notified(con))
        dbmod.set_cap_notified(con, True)
        self.assertTrue(dbmod.get_cap_notified(con))


class ReviewCap(unittest.TestCase):
    def cfg(self, cap):
        cfg = {"repo": ".", "gates": {"require_tests": False}}
        if cap is not None:
            cfg["review"] = {"max_pending_reviews": cap}
        return cfg

    def test_cap_blocks_claim_and_notifies_once(self):
        con = fresh_db()
        held = dbmod.add_ticket(con, "held")
        dbmod.finish(con, held, "pr_open")
        waiting = dbmod.add_ticket(con, "waiting")
        with patch.object(workermod.notify, "post", return_value=False) as post:
            out1 = workermod._process_once(self.cfg(1), con)
            out2 = workermod._process_once(self.cfg(1), con)
        self.assertEqual(out1, "review-cap (1/1)")
        self.assertEqual(out2, "review-cap (1/1)")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(dbmod.get_ticket(con, waiting)["status"], "queued")

    def test_below_cap_resets_flag_and_flows(self):
        con = fresh_db()
        dbmod.set_cap_notified(con, True)
        self.assertEqual(workermod._process_once(self.cfg(3), con), "idle")
        self.assertFalse(dbmod.get_cap_notified(con))

    def test_no_cap_key_means_no_check(self):
        con = fresh_db()
        held = dbmod.add_ticket(con, "held")
        dbmod.finish(con, held, "pr_open")
        self.assertEqual(workermod._process_once(self.cfg(None), con), "idle")


class GatesMisconfigured(unittest.TestCase):
    def test_missing_test_command_pauses_queue(self):
        con = fresh_db()
        t = dbmod.add_ticket(con, "work")
        with patch.object(workermod.notify, "post", return_value=False) as post:
            out = workermod._process_once({"repo": ".", "gates": {}}, con)
            again = workermod._process_once({"repo": ".", "gates": {}}, con)
        self.assertEqual(out, "gates misconfigured")
        self.assertEqual(again, "paused")
        self.assertEqual(post.call_count, 1)
        self.assertTrue(dbmod.is_paused(con))
        self.assertEqual(dbmod.get_ticket(con, t)["status"], "queued")

    def test_whitespace_test_command_is_misconfigured(self):
        con = fresh_db()
        cfg = {"repo": ".", "gates": {"test_command": "   "}}
        with patch.object(workermod.notify, "post", return_value=False):
            out = workermod._process_once(cfg, con)
        self.assertEqual(out, "gates misconfigured")

    def test_require_tests_false_opts_out(self):
        con = fresh_db()
        cfg = {"repo": ".", "gates": {"require_tests": False}}
        with patch.object(workermod.notify, "post", return_value=False) as post:
            out = workermod._process_once(cfg, con)
        self.assertEqual(out, "idle")
        self.assertFalse(dbmod.is_paused(con))
        self.assertEqual(post.call_count, 0)

    def test_present_test_command_passes_check(self):
        con = fresh_db()
        cfg = {"repo": ".", "gates": {"test_command": "python -m unittest"}}
        with patch.object(workermod.notify, "post", return_value=False):
            out = workermod._process_once(cfg, con)
        self.assertEqual(out, "idle")
        self.assertFalse(dbmod.is_paused(con))


if __name__ == "__main__":
    unittest.main()
