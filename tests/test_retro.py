"""Unit tests for the factory retro digest."""

import sqlite3
import unittest

from scripts import retro

SCHEMA = """
create table tickets (id integer primary key, title text, status text,
                      pr_url text);
create table runs (id integer primary key, ticket_id integer, status text,
                   cost_usd real, detail text, tier text,
                   started_at real not null default
                   (cast(strftime('%s','now') as real)));
create table escalations (id integer primary key, ticket_id integer,
                          why text, created_at text);
"""


def make_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executemany(
        "insert into tickets (id, title, status, pr_url) values (?,?,?,?)",
        [(1, "merged thing", "merged", "http://x/1"),
         (2, "awaiting review", "pr_open", "http://x/2"),
         (3, "needs a human", "escalated", ""),
         (4, "bad contract", "needs_info", "")])
    con.executemany(
        "insert into runs (ticket_id, status, cost_usd, detail, tier)"
        " values (?,?,?,?,?)",
        [(1, "ok", 0.10, "", "cheap"),
         (2, "ok", 0.05, "", "cheap"),
         (3, "fail", 0.20, "tests failing", "cheap"),
         (3, "fail", 0.20, "tests failing", "cheap"),
         (3, "escalated", 1.00, "budget exhausted", "strong")])
    con.execute("insert into escalations (ticket_id, why) values (3,"
                " 'three cheap failures')")
    con.commit()
    return con


class TestAggregations(unittest.TestCase):
    def setUp(self):
        self.con = make_db()

    def test_status_counts(self):
        self.assertEqual(retro.status_counts(self.con),
                         {"escalated": 1, "merged": 1, "needs_info": 1,
                          "pr_open": 1})

    def test_spend_groups_by_tier(self):
        spend = dict((t, (u, n)) for t, u, n
                     in retro.spend_by_tier(self.con, 7))
        self.assertEqual(spend["cheap"], (0.55, 4))
        self.assertEqual(spend["strong"], (1.0, 1))

    def test_spend_window_excludes_old_runs(self):
        self.con.execute("insert into runs (ticket_id, status, cost_usd,"
                         " detail, tier, started_at) values (9, 'ok', 5.0,"
                         " '', 'cheap', 1577836800.0)")
        tiers = [t for t, _, _ in retro.spend_by_tier(self.con, 7)]
        total = sum(u for _, u, _ in retro.spend_by_tier(self.con, 7))
        self.assertEqual(total, 1.55)
        self.assertEqual(sorted(tiers), ["cheap", "strong"])

    def test_failure_classes_group_identical_details(self):
        classes = retro.failure_classes(self.con, 7)
        self.assertEqual(classes[0][0], "tests failing (see run detail)")
        self.assertEqual(classes[0][1], 2)
        self.assertEqual(classes[0][2], 1)

    def test_classify_collapses_run_specific_noise(self):
        self.assertEqual(
            retro.classify('{"status": "budget", "result": "", '
                           '"input_tokens": 123456}'),
            "agent stopped: budget")
        self.assertEqual(
            retro.classify("tests do not constrain the new code - "
                           "mutation: 5/12 killed (survived: a.py:1)"),
            "tests do not constrain the new code - mutation gate")
        self.assertEqual(retro.classify(""), "(no detail)")

    def test_human_queue_lists_pending_with_escalation_reason(self):
        queue = retro.human_queue(self.con)
        by_id = dict((item["id"], item) for item in queue)
        self.assertEqual(sorted(by_id), [2, 3, 4])
        self.assertEqual(by_id[2]["pr_url"], "http://x/2")
        self.assertEqual(by_id[3]["why"], "three cheap failures")


class TestRender(unittest.TestCase):
    def test_markdown_covers_every_section(self):
        text = retro.render_markdown("board.db", make_db(), 7)
        self.assertIn("## board.db (last 7 days)", text)
        self.assertIn("tickets: 4 total", text)
        self.assertIn("cheap $0.55 (4 runs)", text)
        self.assertIn("tests failing (see run detail) — 2 run(s) "
                      "across 1 ticket(s)", text)
        self.assertIn("#2 [pr_open] awaiting review — http://x/2", text)
        self.assertIn("#3 [escalated] needs a human — three cheap failures",
                      text)

    def test_empty_board_renders_quiet_sections(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        text = retro.render_markdown("empty.db", con, 7)
        self.assertIn("nothing spent in the window", text)
        self.assertIn("no failed runs in the window", text)
        self.assertIn("nothing waiting on a human", text)


if __name__ == "__main__":
    unittest.main()
