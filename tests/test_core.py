"""Tests for the parts that must not be wrong: claims, caps, gates."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

from factory import agent as agentmod
from factory import blast as blastmod
from factory import db as dbmod
from factory import gates
from factory import sync as syncmod
from factory import worktree


def fresh_db(case=None):
    """A throwaway queue. Pass the TestCase so the connection is closed:
    sqlite handles left open make the suite noisy under -W error::ResourceWarning."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = dbmod.connect(path)
    dbmod.init(con)
    if case is not None:
        case.addCleanup(con.close)
    return con


class TestBlast(unittest.TestCase):
    def test_migrations_are_always_high(self):
        level, why = blastmod.classify(["migrations/0042_add_col.py"], 3, 1)
        self.assertEqual(level, "high")
        self.assertIn("migrations", why)

    def test_auth_is_high(self):
        self.assertEqual(blastmod.classify(["src/auth/session.py"], 5, 0)[0], "high")

    def test_workflow_and_terraform_are_high(self):
        self.assertEqual(blastmod.classify([".github/workflows/deploy.yml"], 2, 0)[0], "high")
        self.assertEqual(blastmod.classify(["infra/main.tf"], 2, 0)[0], "high")

    def test_docs_only_is_low(self):
        self.assertEqual(blastmod.classify(["README.md"], 4, 1)[0], "low")

    def test_lockfile_is_medium(self):
        self.assertEqual(blastmod.classify(["package-lock.json"], 10, 10)[0], "medium")

    def test_size_thresholds(self):
        files = [f"src/f{i}.py" for i in range(30)]
        self.assertEqual(blastmod.classify(files, 10, 10)[0], "high")
        self.assertEqual(blastmod.classify(files[:10], 10, 10)[0], "medium")
        self.assertEqual(blastmod.classify(["a.py", "b.py"], 900, 0)[0], "high")

    def test_suspicious_diff_text(self):
        level, why = blastmod.classify(["a.py"], 2, 0,
                                       diff_text="    drop from users where 1=1")
        self.assertEqual(level, "medium")
        self.assertIn("destructive", why)

    def test_unknown_defaults_away_from_low(self):

        self.assertNotEqual(blastmod.classify(["db/migrate/x.sql"])[0], "low")

    def test_parse_name_status(self):
        out = blastmod.parse_name_status("M\tsrc/a.py\nA\tsrc/b.py\nR100\told.py -> new.py")
        self.assertEqual(out, ["src/a.py", "src/b.py", "new.py"])


class TestQueue(unittest.TestCase):
    def test_claim_is_atomic(self):
        con = fresh_db(self)
        a = dbmod.add_ticket(con, "first")
        b = dbmod.add_ticket(con, "second")
        c1, c2 = dbmod.claim_next(con), dbmod.claim_next(con)
        self.assertEqual({c1["id"], c2["id"]}, {a, b})
        self.assertIsNone(dbmod.claim_next(con))

    def test_no_double_claim(self):
        con = fresh_db(self)
        dbmod.add_ticket(con, "only one")
        first = dbmod.claim_next(con)
        second = dbmod.claim_next(con)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_retry_cap(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "flaky", max_retries=3)
        t = dbmod.get_ticket(con, tid)
        self.assertEqual(t["max_retries"], 3)
        for _ in range(3):
            dbmod.bump_attempt(con, tid)
        self.assertEqual(dbmod.attempt_of(con, tid), 3)
        self.assertGreaterEqual(dbmod.attempt_of(con, tid), t["max_retries"])

    def test_kill_switch(self):
        con = fresh_db(self)
        self.assertFalse(dbmod.is_paused(con))
        dbmod.set_paused(con, True)
        self.assertTrue(dbmod.is_paused(con))
        dbmod.set_paused(con, False)
        self.assertFalse(dbmod.is_paused(con))

    def test_spend_tracking_and_daily_cap(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "costly")
        run = dbmod.start_run(con, tid, 1)
        dbmod.end_run(con, run, "ok", 1.25, "fine")
        self.assertAlmostEqual(dbmod.ticket_spend(con, tid), 1.25)
        self.assertGreaterEqual(dbmod.spend_today(con), 1.25)

    def test_ticket_budget_is_visible_before_start(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "capped", budget_usd=0.5)
        run = dbmod.start_run(con, tid, 1)
        dbmod.end_run(con, run, "fail", 0.6, "boom")
        t = dbmod.get_ticket(con, tid)
        self.assertGreaterEqual(dbmod.ticket_spend(con, tid), t["budget_usd"])

    def test_last_failure_feeds_retry_prompt(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "x")
        run = dbmod.start_run(con, tid, 1)
        dbmod.end_run(con, run, "fail", 0.1, "TypeError: nope")
        self.assertIn("TypeError", dbmod.last_failure(con, tid))

    def test_watchdog_reaps_stale_runs(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "orphan")
        run = dbmod.start_run(con, tid, 1)
        con.execute("UPDATE runs SET started_at = 1 WHERE id=?", (run,))
        self.assertEqual(dbmod.mark_stale_running_failed(con, older_than=60), 1)
        self.assertEqual(dbmod.get_ticket(con, tid)["status"], "escalated")

    def test_concurrent_workers_via_separate_connections(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c1, c2 = dbmod.connect(path), dbmod.connect(path)
        self.addCleanup(c1.close)
        self.addCleanup(c2.close)
        dbmod.init(c1)
        dbmod.add_ticket(c1, "shared queue")
        got = [dbmod.claim_next(c1), dbmod.claim_next(c2)]
        self.assertEqual(sum(1 for g in got if g is not None), 1)


class TestAgent(unittest.TestCase):
    def test_prompt_file_placeholder(self):
        argv = agentmod.build_argv({"command": ["claude", "-p", "{prompt_file}"]},
                                   "prompt text", "/tmp/p.md")
        self.assertEqual(argv, ["claude", "-p", "/tmp/p.md"])

    def test_appends_prompt_when_no_placeholder(self):
        argv = agentmod.build_argv({"command": ["myagent"]}, "hello", "/tmp/p.md")
        self.assertEqual(argv, ["myagent", "hello"])

    def test_string_command_is_split(self):
        argv = agentmod.build_argv({"command": "myagent --flag {prompt_file}"}, "x", "/p")
        self.assertEqual(argv, ["myagent", "--flag", "/p"])

    def test_cost_from_json_total_cost(self):
        out = json.dumps({"type": "result", "total_cost_usd": 0.0421})
        self.assertAlmostEqual(agentmod._extract_cost(out, {})[0], 0.0421)

    def test_cost_from_trailing_json_line(self):
        out = "noise\n" + json.dumps({"total_cost_usd": 0.5})
        self.assertAlmostEqual(agentmod._extract_cost(out, {})[0], 0.5)

    def test_cost_estimated_from_tokens_and_price(self):
        out = json.dumps({"usage": {"input_tokens": 1000, "output_tokens": 500}})
        spec = {"price_per_mtok_in": 1.0, "price_per_mtok_out": 2.0}
        cost, tin, tout = agentmod._extract_cost(out, spec)
        self.assertAlmostEqual(cost, 0.002)
        self.assertEqual((tin, tout), (1000, 500))

    def test_unparseable_output_costs_zero_not_crash(self):
        self.assertEqual(agentmod._extract_cost("not json at all", {})[0], 0.0)

    def test_tier_routing_uses_blast_radius(self):
        cfg = {"agent": {"cheap": {}, "strong": {}}}
        self.assertEqual(agentmod.pick_tier(cfg, {"blast": "high"}), "strong")
        self.assertEqual(agentmod.pick_tier(cfg, {"blast": "medium"}), "strong")
        self.assertEqual(agentmod.pick_tier(cfg, {"blast": "low"}), "cheap")

    def test_single_tier_config_falls_back_to_strong(self):
        self.assertEqual(agentmod.pick_tier({"agent": {"strong": {}}}, {"blast": "low"}),
                         "strong")

    def test_prompt_written_outside_the_worktree(self):

        wd = tempfile.mkdtemp()
        out = tempfile.mkdtemp()
        ticket = {"id": 7, "ref": "X-1", "title": "t", "body": "b", "max_retries": 3}
        p = agentmod.write_prompt(wd, ticket, 1, out_dir=out)
        self.assertTrue(p.startswith(out))
        self.assertEqual(os.listdir(wd), [])
        with open(p, encoding="utf-8") as fh:
            self.assertIn("attempt 1 of 3", fh.read())

    def test_prompt_includes_previous_failure_for_retry(self):
        out = tempfile.mkdtemp()
        ticket = {"id": 8, "ref": "X-2", "title": "t", "body": "b", "max_retries": 3}
        p = agentmod.write_prompt(tempfile.mkdtemp(), ticket, 2,
                                  extra="KeyError: boom", out_dir=out)
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("KeyError: boom", text)
        self.assertIn("attempt 2 of 3", text)

    def test_missing_agent_binary_is_blocked_not_fatal(self):
        cfg = {}
        res = agentmod.run(cfg, {"command": ["definitely-not-installed-xyz"],
                                 "timeout_s": 5}, "p", ".", prompt_path="p")
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["cost_usd"], 0.0)


class TestSecretGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, name, text):
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def test_finds_aws_key(self):
        self._write("src/config.py", 'AWS = "AKIAIOSFODNN7EXAMPLE"\n')
        res = gates.check(self.dir)
        self.assertFalse(res["ok"])
        self.assertTrue(any("AWS" in f.get("reason", "") for f in res["secrets"]))

    def test_finds_github_token(self):
        self._write(".env", "GH_TOKEN=ghp_" + "A" * 36 + "\n")
        self.assertFalse(gates.check(self.dir)["ok"])

    def test_finds_private_key(self):
        self._write("id_rsa", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
        self.assertFalse(gates.check(self.dir)["ok"])

    def test_clean_repo_passes(self):
        self._write("app.py", "def add(a, b):\n    return a + b\n")
        self.assertTrue(gates.check(self.dir)["ok"])

    def test_skips_vendor_dirs(self):
        self._write("node_modules/pkg/x.js", "var k='" + "AKIA" + "X" * 16 + "'\n")
        self.assertTrue(gates.check(self.dir)["ok"])


    FAKE_AKIA = "AKIA" + "Q1" * 8

    def _diff(self, body):
        return ("diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,2 +1,3 @@\n" + body + "\n")

    def test_diff_scope_catches_added_secret(self):
        diff = self._diff(" def add(a, b):\n"
                          f"+    KEY = '{self.FAKE_AKIA}'\n"
                          "     return a + b\n")
        found = gates.scan_added(diff)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["file"], "app.py")
        self.assertEqual(found[0]["line"], 2)

    def test_diff_scope_ignores_context_and_removed_lines(self):

        diff = self._diff(f" KEY = '{self.FAKE_AKIA}'\n"
                          f"-OLD = '{self.FAKE_AKIA}'\n"
                          " return a + b\n")
        self.assertEqual(gates.scan_added(diff), [])

    def test_diff_scope_line_numbers_follow_the_hunk(self):
        diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                "@@ -100,2 +100,4 @@\n"
                " ctx\n"
                f"+    KEY = '{self.FAKE_AKIA}'\n"
                " ctx\n ctx\n")
        self.assertEqual(gates.scan_added(diff)[0]["line"], 101)

    def test_preexisting_fixture_does_not_block_every_pr(self):

        self._write("tests/fake_agent.py", "KEY = '" + self.FAKE_AKIA + "'\n")
        diff = self._diff(" def add(a, b):\n+    return a + b  # sum\n")
        self.assertFalse(gates.check(self.dir)["ok"], "tree scan should still see it")
        res = gates.check(self.dir, diff=diff)
        self.assertTrue(res["ok"])
        self.assertEqual(res["scope"], "diff")


class TestPrIsReal(unittest.TestCase):
    """`pr_open` in the dashboard must mean a reviewer can click a link."""

    def test_missing_gh_raises_instead_of_a_fake_success(self):
        from factory import worktree as wt
        orig, wt._gh_binary = wt._gh_binary, lambda cfg_path=None: None
        try:
            with self.assertRaises(RuntimeError) as cm:
                wt.open_pr(".", "b", "t", "body")
            self.assertIn("no PR could be opened", str(cm.exception))
        finally:
            wt._gh_binary = orig

    def test_gh_exit_zero_without_url_is_a_failure(self):
        from factory import worktree as wt

        class Done:
            returncode, stdout, stderr = 0, "warning: something\n", ""

        orig_bin, orig_run = wt._gh_binary, wt.subprocess.run
        wt._gh_binary = lambda cfg_path=None: "gh"
        wt.subprocess.run = lambda *a, **k: Done()
        try:
            with self.assertRaises(RuntimeError) as cm:
                wt.open_pr(".", "b", "t", "body")
            self.assertIn("no url", str(cm.exception))
        finally:
            wt._gh_binary, wt.subprocess.run = orig_bin, orig_run

    def test_url_is_persisted_and_migrated(self):

        con = fresh_db(self)
        con.execute("CREATE TABLE old_tickets (id INTEGER PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO old_tickets VALUES (1, 'x')")
        con.execute("DROP TABLE tickets")
        con.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT,"
                    " status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,"
                    " max_retries INTEGER DEFAULT 3, budget_usd REAL DEFAULT 1,"
                    " spent_usd REAL DEFAULT 0, created_at REAL DEFAULT 0,"
                    " updated_at REAL DEFAULT 0)")
        con.execute("INSERT INTO tickets (id, title) VALUES (1, 'legacy')")
        dbmod.init(con)
        self.assertIn("pr_url", {r["name"] for r in con.execute("PRAGMA table_info(tickets)")})
        dbmod.set_pr(con, 1, "https://example.invalid/pull/1")
        self.assertEqual(con.execute("SELECT pr_url FROM tickets WHERE id=1").fetchone()["pr_url"],
                         "https://example.invalid/pull/1")


class TestCommitAll(unittest.TestCase):
    """commit_all runs real git, so it is the piece most likely to differ
    between the dev machine and CI. The first real run on Linux proved that."""

    def setUp(self):
        import subprocess
        self.dir = tempfile.mkdtemp()
        self.git = subprocess.run
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.env = env

        def git(*a):
            r = subprocess.run(["git", *a], cwd=self.dir, capture_output=True,
                               text=True, env=env)
            assert r.returncode == 0, r.stderr
        self.g = git
        git("init", "-q", "-b", "main")
        with open(os.path.join(self.dir, ".gitignore"), "w") as fh:
            fh.write(".factory-tmp/\n")
        with open(os.path.join(self.dir, "app.py"), "w") as fh:
            fh.write("print(1)\n")
        os.makedirs(os.path.join(self.dir, "build"))
        with open(os.path.join(self.dir, "build", "out.js"), "w") as fh:
            fh.write("v1\n")
        git("add", "-A")
        git("commit", "-qm", "seed")

    def _touch(self, rel, content="x"):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        with open(p, "w") as fh:
            fh.write(content)

    def _committed(self):
        r = self.git(["git", "show", "--name-only", "--format=", "HEAD"],
                     cwd=self.dir, capture_output=True, text=True, env=self.env)
        return sorted(r.stdout.split())

    def test_artifacts_from_a_test_run_are_not_committed(self):
        self._touch("new_test.py", "def test_x():\n    assert True\n")
        self._touch("__pycache__/new_test.cpython-314.pyc")
        self._touch(".pytest_cache/v/cache/lastfailed", "{}")
        self._touch("node_modules/pkg/index.js")
        self.assertTrue(worktree.commit_all(self.dir, "add test"))
        self.assertEqual(self._committed(), ["new_test.py"])

    def test_tracked_artifact_modification_is_still_committed(self):

        self._touch("build/out.js", "v2\n")
        self.assertTrue(worktree.commit_all(self.dir, "rebuild"))
        self.assertEqual(self._committed(), ["build/out.js"])

    def test_ignored_scratch_dir_does_not_break_the_add(self):

        self._touch(".factory-tmp/scratch.txt", "junk")
        self._touch("real.py", "print(2)\n")
        self.assertTrue(worktree.commit_all(self.dir, "add real"))
        self.assertEqual(self._committed(), ["real.py"])

    def test_no_changes_returns_false(self):
        self.assertFalse(worktree.commit_all(self.dir, "nothing"))


class TestCommitIdentity(unittest.TestCase):
    """A worker on a fresh host has no git identity anywhere.

    CI caught this on the first PR: commit_all passed --author, but git still
    demands a *committer*, so every commit died with rc=128 "Committer identity
    unknown / empty ident name (for <runner@...>)". The factory cannot assume
    the machine it lands on has a configured user, so the identity travels with
    the process instead of living in someone's global config.
    """

    def test_commit_needs_no_git_identity_on_the_host(self):
        import subprocess
        from unittest import mock

        d = tempfile.mkdtemp()
        overrides = {"GIT_CONFIG_GLOBAL": os.devnull,
                     "GIT_CONFIG_SYSTEM": os.devnull,
                     "GIT_CONFIG_NOSYSTEM": "1"}
        with mock.patch.dict(os.environ, overrides, clear=False):
            for k in [k for k in os.environ
                      if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))]:
                del os.environ[k]

            def git(*a):
                r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
                assert r.returncode == 0, f"{a}: {r.stderr}"

            git("init", "-q", "-b", "main")
            with open(os.path.join(d, "new.py"), "w") as fh:
                fh.write("x = 1\n")

            self.assertTrue(worktree.commit_all(d, "identity-free commit"))
            who = subprocess.run(
                ["git", "log", "-1", "--format=%an|%ae|%cn|%ce"], cwd=d,
                capture_output=True, text=True).stdout.strip()
        name, email = worktree.AGENT_IDENTITY
        self.assertEqual(who, f"{name}|{email}|{name}|{email}")


class TestSync(unittest.TestCase):
    """The queue must only move when GitHub says so, and must record HOW."""

    def setUp(self):
        self.con = fresh_db(self)
        self.orig = syncmod.wt._gh_binary, syncmod.fetch_pr

    def tearDown(self):
        syncmod.wt._gh_binary, syncmod.fetch_pr = self.orig

    def _fake(self, payloads):
        syncmod.wt._gh_binary = lambda cfg_path=None: "gh"
        calls = []

        def fetch(gh, owner, repo, number):
            calls.append((owner, repo, number))
            p = payloads.get(number)
            if isinstance(p, Exception):
                raise p
            return p

        syncmod.fetch_pr = fetch
        return calls

    def test_url_parsing_and_rejects(self):
        self.assertEqual(syncmod.parse_pr_url(
            "https://github.com/acme/widget/pull/12"), ("acme", "widget", 12))
        self.assertEqual(syncmod.parse_pr_url(
            "https://github.com/acme/widget/pull/12/files"), ("acme", "widget", 12))
        for bad in (None, "", "not a url", "https://github.com/acme/widget"):
            with self.assertRaises(syncmod.SyncError):
                syncmod.parse_pr_url(bad)

    def test_classify(self):
        approved = {"state": "MERGED", "reviews": [{"state": "COMMENTED"},
                                                   {"state": "APPROVED"}]}
        bypassed = {"state": "MERGED", "reviews": [{"state": "COMMENTED"}]}
        self.assertEqual(syncmod.classify(approved), ("merged", "approved"))
        self.assertEqual(syncmod.classify(bypassed), ("merged", "admin-bypass"))
        self.assertEqual(syncmod.classify({"state": "CLOSED", "reviews": []}),
                         ("closed", "unreviewed"))
        self.assertEqual(syncmod.classify({"state": "OPEN", "reviews": []}), (None, None))

    def test_sync_records_merged_and_how(self):
        dbmod.set_pr(self.con, dbmod.add_ticket(self.con, "t"),
                     "https://github.com/acme/widget/pull/7")
        self.con.execute("UPDATE tickets SET status='pr_open' WHERE id=1")
        self._fake({7: {"state": "MERGED", "mergedAt": "2026-11-17T13:50:36Z",
                        "mergedBy": {"login": "boss"}, "reviews": []}})
        changes, errors = syncmod.sync({}, self.con)
        self.assertEqual(errors, [])
        row = self.con.execute("SELECT * FROM tickets WHERE id=1").fetchone()
        self.assertEqual(row["status"], "merged")

        self.assertEqual(row["review_state"], "admin-bypass")
        self.assertEqual(row["merged_by"], "boss")
        self.assertIsNotNone(row["merged_at"])
        self.assertEqual(changes[0][:3], (1, "pr_open", "merged"))

    def test_sync_leaves_open_and_unlinked_tickets_alone(self):
        dbmod.set_pr(self.con, dbmod.add_ticket(self.con, "open pr"),
                     "https://github.com/acme/widget/pull/8")
        dbmod.add_ticket(self.con, "no pr yet")
        self.con.execute("UPDATE tickets SET status='pr_open' WHERE id=1")
        self._fake({8: {"state": "OPEN", "reviews": []}})
        changes, errors = syncmod.sync({}, self.con)
        self.assertEqual((changes, errors), ([], []))
        self.assertEqual([r["status"] for r in self.con.execute(
            "SELECT status FROM tickets ORDER BY id")], ["pr_open", "queued"])

    def test_failed_lookup_never_marks_a_ticket_merged(self):
        dbmod.set_pr(self.con, dbmod.add_ticket(self.con, "t"),
                     "https://github.com/acme/widget/pull/9")
        self.con.execute("UPDATE tickets SET status='pr_open' WHERE id=1")
        self._fake({9: syncmod.SyncError("gh timed out")})
        changes, errors = syncmod.sync({}, self.con)
        self.assertEqual(changes, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.con.execute(
            "SELECT status FROM tickets WHERE id=1").fetchone()["status"], "pr_open")

    def test_missing_gh_raises_instead_of_guessing(self):
        dbmod.set_pr(self.con, dbmod.add_ticket(self.con, "t"),
                     "https://github.com/acme/widget/pull/1")
        syncmod.wt._gh_binary = lambda cfg_path=None: None
        with self.assertRaises(syncmod.SyncError):
            syncmod.sync({}, self.con)

    def test_legacy_db_gains_the_audit_columns(self):
        con = fresh_db(self)
        con.execute("DROP TABLE tickets")
        con.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT,"
                    " status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0,"
                    " max_retries INTEGER DEFAULT 3, budget_usd REAL DEFAULT 1,"
                    " spent_usd REAL DEFAULT 0, created_at REAL DEFAULT 0,"
                    " updated_at REAL DEFAULT 0)")
        dbmod.init(con)
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tickets)")}
        self.assertTrue({"pr_url", "review_state", "merged_by", "merged_at"} <= cols)


class TestCliSurface(unittest.TestCase):
    """The CLI must at least build.

    A literal `%` in any help string makes argparse raise while *constructing*
    the parser, which kills every subcommand - and a suite that only imports
    the module and calls functions directly stays green while the tool is
    completely unusable.
    """

    def _subcommands(self):
        import argparse
        from factory import cli
        for action in cli.build_parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices
        self.fail("no subcommands")

    def test_every_subcommand_is_wired_to_a_handler(self):
        choices = self._subcommands()
        self.assertIn("sync", choices)
        for name, sub in choices.items():
            fn = sub.get_default("fn")
            self.assertTrue(callable(fn), f"{name} has no handler")

    def test_sync_parses(self):
        from factory import cli
        ns = cli.build_parser().parse_args(["sync", "--gh", "/usr/bin/gh"])
        self.assertEqual(ns.gh, "/usr/bin/gh")
        self.assertTrue(callable(ns.fn))


class TestNoComments(unittest.TestCase):
    """The no-comments rule must hold for the whole repo, not by memory."""

    def test_repo_has_no_comments(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(root, "tools"))
        import no_comments_check
        hits = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in no_comments_check.SKIP_DIRS]
            for name in files:
                if name.endswith(".py"):
                    p = os.path.join(base, name)
                    for line, text in no_comments_check.check(p):
                        hits.append(f"{p}:{line}: {text[:60]}")
        self.assertEqual(hits, [], "comments found:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
