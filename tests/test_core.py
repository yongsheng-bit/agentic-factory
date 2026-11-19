"""Tests for the parts that must not be wrong: claims, caps, gates."""

import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from factory import agent as agentmod
from factory import blast as blastmod
from factory import cli as climod
from factory import db as dbmod
from factory import gates
from factory import sync as syncmod
from factory import worker as workermod
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

    def test_log_failure_feeds_retry_prompt(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "x")
        dbmod.log_failure(con, tid, "tests failing:\nFAILED tests/test_x.py")
        self.assertIn("FAILED tests/test_x.py", dbmod.last_failure(con, tid))

    def test_claim_next_respects_seq(self):
        con = fresh_db(self)
        late = dbmod.add_ticket(con, "filed first, depends on other work")
        first = dbmod.add_ticket(con, "dependency, filed later")
        con.execute("UPDATE tickets SET seq=1 WHERE id=?", (late,))
        con.execute("UPDATE tickets SET seq=0 WHERE id=?", (first,))
        con.commit()
        row = dbmod.claim_next(con)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], first)

    def test_watchdog_reaps_stale_runs(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "orphan")
        run = dbmod.start_run(con, tid, 1)
        con.execute("UPDATE runs SET started_at = 1 WHERE id=?", (run,))
        self.assertEqual(dbmod.mark_stale_running_failed(con, older_than=60), 1)
        self.assertEqual(dbmod.get_ticket(con, tid)["status"], "escalated")

    def test_escalate_survives_its_own_db_call(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "doomed")
        ticket = dbmod.get_ticket(con, tid)
        self.assertEqual(
            workermod._escalate(con, ticket, {}, "retries exhausted"), "escalated")
        self.assertEqual(dbmod.get_ticket(con, tid)["status"], "escalated")
        rows = con.execute("SELECT why FROM escalations WHERE ticket_id=?",
                           (tid,)).fetchall()
        self.assertEqual([r["why"] for r in rows], ["retries exhausted"])

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


class TestRetryPush(unittest.TestCase):
    """A retry must be able to overwrite the branch attempt 1 pushed.

    Branch names carry no attempt number, so attempt 2 lands on a remote head
    it does not descend from and a plain push dies as non-fast-forward, taking
    the ticket with it. push_branch clears the agent-owned head first, and
    delete_remote_branch only ever clears `agent/*`: a bare origin in a temp
    dir is the only honest way to test both halves.
    """

    def setUp(self):
        import subprocess
        self.dir = tempfile.mkdtemp()
        self.origin = os.path.join(self.dir, "origin.git")
        self.repo = os.path.join(self.dir, "repo")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.env = env

        def git(*a):
            r = subprocess.run(["git", *a], capture_output=True, text=True,
                               env=env, cwd=self.repo)
            assert r.returncode == 0, f"{a}: {r.stderr}"
            return r
        self.g = git
        os.makedirs(self.repo)
        git("init", "-q", "--bare", self.origin)
        git("init", "-q", "-b", "main", self.repo)
        with open(os.path.join(self.repo, "app.py"), "w") as fh:
            fh.write("print(1)\n")
        git("add", "-A")
        git("commit", "-qm", "seed")
        git("remote", "add", "origin", self.origin)
        git("push", "-q", "origin", "main")

    def _commit_on(self, branch, name):
        self.g("checkout", "-q", "-B", branch)
        with open(os.path.join(self.repo, name), "w") as fh:
            fh.write(name + "\n")
        self.g("add", "-A")
        self.g("commit", "-qm", name)
        return self.g("rev-parse", "HEAD").stdout.strip()

    def _remote_sha(self, branch):
        out = self.g("ls-remote", "--heads", "origin", branch).stdout.split()
        return out[0] if out else ""

    def test_first_attempt_pushes_and_a_diverged_retry_is_rejected(self):
        self._commit_on("agent/t1-1", "one.py")
        worktree.push_branch(self.repo, "agent/t1-1", attempt=1)
        self.assertEqual(self._remote_sha("agent/t1-1"),
                         self.g("rev-parse", "HEAD").stdout.strip())
        self.g("checkout", "-q", "main")
        self._commit_on("agent/t1-1", "two.py")
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.push_branch(self.repo, "agent/t1-1", attempt=1)
        self.assertIn("non-fast-forward", str(cm.exception))
        self.assertNotEqual(self._remote_sha("agent/t1-1"),
                            self.g("rev-parse", "HEAD").stdout.strip())

    def test_retry_attempt_clears_the_stale_branch_and_pushes(self):
        self._commit_on("agent/t1-1", "one.py")
        worktree.push_branch(self.repo, "agent/t1-1", attempt=1)
        self.g("checkout", "-q", "main")
        new = self._commit_on("agent/t1-1", "two.py")
        worktree.push_branch(self.repo, "agent/t1-1", attempt=2)
        self.assertEqual(self._remote_sha("agent/t1-1"), new)

    def test_attempt_two_with_nothing_on_the_remote_still_pushes(self):
        new = self._commit_on("agent/t1-1", "one.py")
        worktree.push_branch(self.repo, "agent/t1-1", attempt=2)
        self.assertEqual(self._remote_sha("agent/t1-1"), new)

    def test_non_agent_branches_are_never_deleted(self):
        self._commit_on("main", "one.py")
        before = self._remote_sha("main")
        with self.assertRaises(worktree.WorktreeError) as cm:
            worktree.delete_remote_branch(self.repo, "main")
        self.assertIn("only agent-owned branches", str(cm.exception))
        self.assertEqual(self._remote_sha("main"), before)

    def test_deleting_an_absent_branch_is_silent(self):
        worktree.delete_remote_branch(self.repo, "agent/never-pushed-1")
        self.assertEqual(self._remote_sha("agent/never-pushed-1"), "")


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

        def fetch(gh, owner, repo, number, token=None):
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
        self.assertEqual(syncmod.classify({"state": "OPEN", "reviews": []}, "closed"),
                         ("pr_open", "unreviewed"))
        for other in ("merged", "queued", "pr_open", "needs_review", "escalated"):
            self.assertEqual(syncmod.classify({"state": "OPEN", "reviews": []}, other),
                             (None, None), other)

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

    def test_reopened_pr_reopens_a_closed_ticket(self):
        tid = dbmod.add_ticket(self.con, "reopened work")
        dbmod.set_pr(self.con, tid, "https://github.com/acme/widget/pull/11")
        self.con.execute("UPDATE tickets SET status='closed', review_state='approved'"
                         " WHERE id=?", (tid,))
        self._fake({11: {"state": "OPEN", "reviews": []}})
        changes, errors = syncmod.sync({}, self.con)
        self.assertEqual(errors, [])
        row = self.con.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["status"], "pr_open")
        self.assertEqual(row["review_state"], "unreviewed")
        self.assertEqual(changes[0][:4], (tid, "closed", "pr_open", "unreviewed"))

    def test_open_pr_never_resurrects_merged_or_touches_other_statuses(self):
        merged = dbmod.add_ticket(self.con, "already merged")
        dbmod.set_pr(self.con, merged, "https://github.com/acme/widget/pull/12")
        self.con.execute("UPDATE tickets SET status='merged', review_state='approved',"
                         " merged_by='boss' WHERE id=?", (merged,))
        queued = dbmod.add_ticket(self.con, "never ran")
        dbmod.set_pr(self.con, queued, "https://github.com/acme/widget/pull/13")
        review = dbmod.add_ticket(self.con, "waiting on a human")
        dbmod.set_pr(self.con, review, "https://github.com/acme/widget/pull/14")
        self.con.execute("UPDATE tickets SET status='needs_review' WHERE id=?", (review,))
        calls = self._fake({12: {"state": "OPEN", "reviews": []},
                            13: {"state": "OPEN", "reviews": []},
                            14: {"state": "OPEN", "reviews": []}})
        changes, errors = syncmod.sync({}, self.con)
        self.assertEqual((changes, errors), ([], []))
        self.assertEqual(
            [(r["id"], r["status"], r["review_state"]) for r in self.con.execute(
                "SELECT id, status, review_state FROM tickets ORDER BY id")],
            [(merged, "merged", "approved"), (queued, "queued", None),
             (review, "needs_review", None)])
        self.assertEqual(calls, [("acme", "widget", 14)])

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


class TestShowCommand(unittest.TestCase):
    """`factory show <id>` is the only way to read one ticket back.

    Everything the escalation logic decides (tier, blast, spend, the failure the
    next attempt will be told about) lives in the database, so a ticket that
    cannot be inspected is a ticket that cannot be debugged without SQL.
    """

    def _env(self):
        """A real config file pointing at a real queue, so the CLI is
        exercised end to end instead of through a mocked _open()."""
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db}, fh)
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, *argv])
            return buf.getvalue()

        return con, cfg, run

    def test_seeded_ticket_shows_every_field(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "flaky", max_retries=4, budget_usd=3.0)
        dbmod.bump_attempt(con, tid)
        dbmod.set_blast(con, tid, "high", "touches auth")
        dbmod.set_pr(con, tid, "https://github.com/acme/widget/pull/12")
        first = dbmod.start_run(con, tid, 1, tier="cheap", tier_reason="default")
        dbmod.end_run(con, first, "fail", 0.11, "TypeError: nope")
        second = dbmod.start_run(con, tid, 2, tier="strong", tier_reason="escalated")
        dbmod.end_run(con, second, "ok", 0.31, "shipped")

        out = run("show", str(tid))
        self.assertIn("queued", out)
        self.assertIn("1/4", out)
        self.assertIn("$0.42", out)
        self.assertIn("high (touches auth)", out)
        self.assertIn("strong (escalated)", out)
        self.assertIn("https://github.com/acme/widget/pull/12", out)
        self.assertIn("TypeError: nope", out)

    def test_unknown_id_says_so_and_exits_one(self):
        import contextlib
        import io

        from factory import cli
        _, cfg, _ = self._env()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, "show", "4242"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(buf.getvalue().strip(), "no such ticket 4242")

    def test_ticket_without_a_run_shows_placeholders(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "never started")
        out = run("show", str(tid))
        self.assertIn("tier       : - (-)", out)
        self.assertIn("pr         : -", out)
        self.assertIn("last error : -", out)
        self.assertIn("blast      : - (-)", out)


class TestRunsCommand(unittest.TestCase):
    """`factory runs` is how a routing decision gets audited after the fact.

    Every run already stores the tier it used and why; without a reader for
    those columns the only way to ask "why did this ticket get the strong
    model" is raw SQL against the queue file.
    """

    def _env(self):
        """A real config file pointing at a real queue, so the CLI is
        exercised end to end instead of through a mocked _open()."""
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db}, fh)
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, *argv])
            return buf.getvalue()

        return con, cfg, run

    def _seed(self, con):
        cheap = dbmod.add_ticket(con, "typo in README")
        strong = dbmod.add_ticket(con, "rotate auth tokens")
        one = dbmod.start_run(con, cheap, 1, tier="cheap", tier_reason="blast low")
        dbmod.end_run(con, one, "ok", 0.04, "shipped")
        two = dbmod.start_run(con, strong, 1, tier="strong",
                              tier_reason="blast high: touches auth")
        dbmod.end_run(con, two, "fail", 0.31, "boom")
        return cheap, strong

    def test_seeded_runs_are_listed_newest_first(self):
        con, _, run = self._env()
        self._seed(con)
        out = run("runs")
        self.assertIn("tier_reason", out)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 3)
        self.assertTrue(re.match(r"^\s*\d+\s+\d+\s+1\s+fail\s+strong\s+blast high: "
                                 r"touches auth\s+\$0\.31\s+\d{4}-\d\d-\d\d "
                                 r"\d\d:\d\d:\d\d$", lines[1]), lines[1])
        self.assertTrue(re.match(r"^\s*\d+\s+\d+\s+1\s+ok\s+cheap\s+blast low\s+"
                                 r"\$0\.04\s+\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$",
                                 lines[2]), lines[2])
        self.assertGreater(int(lines[1].split()[0]), int(lines[2].split()[0]))

    def test_ticket_filter(self):
        con, _, run = self._env()
        cheap, strong = self._seed(con)
        out = run("runs", "--ticket", str(strong))
        self.assertIn("blast high: touches auth", out)
        self.assertNotIn("blast low", out)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1].split()[1], str(strong))
        other = run("runs", "--ticket", str(cheap))
        self.assertIn("blast low", other)
        self.assertNotIn("touches auth", other)
        self.assertEqual([ln.split()[1] for ln in other.splitlines()
                          if ln.strip()][1], str(cheap))

    def test_limit_caps_the_output(self):
        con, _, run = self._env()
        self._seed(con)
        out = run("runs", "--limit", "1")
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 2)
        self.assertIn("blast high: touches auth", out)

    def test_empty_queue_says_no_runs(self):
        _, _, run = self._env()
        self.assertEqual(run("runs").strip(), "(no runs)")
        self.assertEqual(run("runs", "--ticket", "99").strip(), "(no runs)")

    def test_run_without_a_tier_shows_placeholders(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "unrouted")
        run_id = dbmod.start_run(con, tid, 1)
        dbmod.end_run(con, run_id, "ok", 0.0, "fine")
        out = run("runs")
        row = [ln for ln in out.splitlines() if ln.strip()][-1]
        self.assertTrue(re.match(r"^\s*\d+\s+\d+\s+1\s+ok\s+-\s+-\s+\$0\.00\s+"
                                 r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", row), row)


class TestRetryCommand(unittest.TestCase):
    """`factory retry <id>` replaces hand-written UPDATE statements.

    Hand-editing the status is how two workers once ended up on the same
    worktree, so the guard has to live in the same write that does the move,
    and the attempt count has to survive: the tier router escalates on it, and
    a retry that forgets it sends a known-hard ticket back to the cheap model.
    """

    ALLOWED = ("failed", "escalated", "closed")
    REFUSED = ("queued", "running", "pr_open", "needs_review", "merged")

    def _env(self):
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db}, fh)
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        def run(*argv):
            buf = io.StringIO()
            code = 0
            try:
                with contextlib.redirect_stdout(buf):
                    cli.main(["-c", cfg, *argv])
            except SystemExit as exc:
                code = exc.code
            return int(code or 0), buf.getvalue()

        return con, run

    def _ticket(self, con, status, attempts=2):
        tid = dbmod.add_ticket(con, "hard one", max_retries=3)
        for _ in range(attempts):
            dbmod.bump_attempt(con, tid)
        con.execute("UPDATE tickets SET status=? WHERE id=?", (status, tid))
        return tid

    def test_finished_statuses_requeue_and_keep_attempts(self):
        for status in self.ALLOWED:
            con, run = self._env()
            tid = self._ticket(con, status)
            code, out = run("retry", str(tid))
            self.assertEqual(code, 0, f"{status}: {out}")
            row = dbmod.get_ticket(con, tid)
            self.assertEqual(row["status"], "queued", status)
            self.assertEqual(row["attempts"], 2, f"{status} lost its attempt count")
            self.assertIn("requeued", out)

    def test_retry_is_what_makes_the_next_run_use_the_strong_tier(self):
        con, run = self._env()
        tid = self._ticket(con, "failed", attempts=1)
        run("retry", str(tid))
        ticket = dbmod.get_ticket(con, tid)
        self.assertEqual(agentmod.tier_decision({"agent": {"cheap": {}, "strong": {}}},
                                                ticket)[0], "strong")

    def test_in_flight_and_shipped_statuses_are_refused(self):
        for status in self.REFUSED:
            con, run = self._env()
            tid = self._ticket(con, status)
            before = dbmod.get_ticket(con, tid)
            code, out = run("retry", str(tid))
            self.assertEqual(code, 1, f"{status} should have been refused")
            after = dbmod.get_ticket(con, tid)
            self.assertEqual(after["status"], status)
            self.assertEqual(after["attempts"], before["attempts"])
            self.assertEqual(after["updated_at"], before["updated_at"])
            self.assertIn("cannot retry", out)
            self.assertIn(status, out)

    def test_unknown_id_is_refused_with_exit_one(self):
        _, run = self._env()
        code, out = run("retry", "4242")
        self.assertEqual(code, 1)
        self.assertIn("no such ticket 4242", out)

    def test_retry_only_moves_a_ticket_that_is_still_finished(self):
        con = fresh_db(self)
        tid = self._ticket(con, "escalated")
        con.execute("UPDATE tickets SET status='running' WHERE id=?", (tid,))
        ok, msg = dbmod.retry_ticket(con, tid)
        self.assertFalse(ok)
        self.assertEqual(dbmod.get_ticket(con, tid)["status"], "running")
        self.assertIn("running", msg)

    def test_requeued_ticket_can_be_claimed_again(self):
        con = fresh_db(self)
        tid = self._ticket(con, "failed")
        self.assertIsNone(dbmod.claim_next(con))
        self.assertTrue(dbmod.retry_ticket(con, tid)[0])
        self.assertEqual(dbmod.claim_next(con)["id"], tid)
        self.assertIsNone(dbmod.claim_next(con))


class TestTierCommand(unittest.TestCase):
    """`factory tier <id> <cheap|strong|auto>` edits the routing pin later.

    `factory add --tier` is the only other way to set tickets.tier, so without
    this command a wrong pin at creation time is permanent without SQL. The
    command echoes the decision the router would now make, because the pin is
    worth nothing if the operator cannot see what it changed.
    """

    def _env(self):
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db,
                       "agent": {"cheap": {}, "strong": {}}}, fh)
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, *argv])
            return buf.getvalue()

        return con, cfg, run

    def test_pin_overrides_the_router(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "dangerous")
        out = run("tier", str(tid), "strong")
        self.assertIn("pinned to strong", out)
        self.assertIn("would route: strong (explicit override)", out)
        self.assertEqual(dbmod.get_ticket(con, tid)["tier"], "strong")

    def test_auto_clears_the_pin_back_to_null(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "mis-pinned", tier="cheap")
        run("tier", str(tid), "auto")
        self.assertIsNone(dbmod.get_ticket(con, tid)["tier"])
        out = run("tier", str(tid), "auto")
        self.assertIn("router decides", out)
        self.assertIn("would route: cheap (default)", out)

    def test_cleared_pin_shows_the_escalation_the_router_now_makes(self):
        con, _, run = self._env()
        tid = dbmod.add_ticket(con, "flaky", tier="cheap")
        dbmod.bump_attempt(con, tid)
        out = run("tier", str(tid), "auto")
        self.assertIn("would route: strong (escalated after 1 failed attempt(s))", out)

    def test_unknown_id_says_so_and_exits_one(self):
        import contextlib
        import io

        from factory import cli
        _, cfg, _ = self._env()
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, "tier", "4242", "strong"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(buf.getvalue().strip(), "no such ticket 4242")

    def test_set_tier_round_trips_through_the_database(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "edited")
        dbmod.set_tier(con, tid, "strong")
        self.assertEqual(dbmod.get_ticket(con, tid)["tier"], "strong")
        dbmod.set_tier(con, tid, None)
        self.assertIsNone(dbmod.get_ticket(con, tid)["tier"])


class TestStatusTiers(unittest.TestCase):
    """`factory status` must show which tier actually did the work.

    Escalation is the routing rule that decides cost and quality, so a daily
    view that hides it makes the strong tier look like a mystery. The counts
    come from the runs table, tier_reason LIKE 'escalated%' being the same
    marker the router writes when it promotes a retry.
    """

    def _env(self):
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db}, fh)
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["-c", cfg, *argv])
            return buf.getvalue()

        return con, run

    def _seed(self, con, runs):
        for i, (tier, reason) in enumerate(runs):
            tid = dbmod.add_ticket(con, f"ticket {i}")
            rid = dbmod.start_run(con, tid, 1, tier=tier, tier_reason=reason)
            dbmod.end_run(con, rid, "ok", 0.01, "fine")

    def test_counts_cheap_strong_and_escalations(self):
        con, run = self._env()
        self._seed(con, [("cheap", "default")] * 8
                   + [("strong", "explicit override")]
                   + [("strong", "escalated after 1 failed attempt(s)")] * 2)
        out = run("status")
        self.assertIn("tiers      : 8 cheap / 3 strong "
                      "(2 escalated after failure)", out)

    def test_no_runs_prints_zeros(self):
        con, run = self._env()
        out = run("status")
        self.assertIn("tiers      : 0 cheap / 0 strong "
                      "(0 escalated after failure)", out)

    def test_runs_without_a_tier_are_not_escalations(self):
        con, run = self._env()
        tid = dbmod.add_ticket(con, "legacy row")
        dbmod.end_run(con, dbmod.start_run(con, tid, 1), "ok", 0.01, "no tier")
        self.assertEqual(dbmod.tier_mix(con),
                         {"cheap": 0, "strong": 0, "escalated": 0})
        self.assertIn("tiers      : 0 cheap / 0 strong "
                      "(0 escalated after failure)", run("status"))


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


class TestTokenPlumbing(unittest.TestCase):
    def _auth(self, token, url=None, env=None):
        from factory import worktree as wt
        return wt._auth_env(dict(env or {}), token, url)

    def test_token_travels_as_one_shot_config_not_argv(self):
        import base64
        env = self._auth("github_pat_test", "https://github.com/o/r.git")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        want = base64.b64encode(b"x-access-token:github_pat_test").decode()
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "AUTHORIZATION: basic " + want)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_host_comes_from_the_remote(self):
        env = self._auth("t", "https://git.example.net:8443/o/r.git")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://git.example.net:8443/.extraheader")

    def test_no_token_changes_nothing(self):
        self.assertEqual(self._auth(None, "https://github.com/o/r.git"), {})

    def test_existing_one_shot_config_is_not_clobbered(self):
        env = self._auth("t", "https://github.com/o/r.git",
                         {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b",
                          "GIT_CONFIG_VALUE_0": "c"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/.extraheader")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "a.b")

    def test_gh_gets_the_token_by_env(self):
        from factory import worktree as wt
        env = wt._token_env("github_pat_test")
        self.assertEqual(env["GH_TOKEN"], "github_pat_test")
        self.assertEqual(env["GIT_COMMITTER_NAME"], wt.AGENT_IDENTITY[0])


class TestAgentEnv(unittest.TestCase):
    BASE = {
        "PATH": "/usr/bin", "Path": "/usr/bin", "HOME": "/home/op",
        "SystemRoot": "C:\\Windows", "USERPROFILE": "C:\\Users\\op",
        "GH_TOKEN": "ghp_owner", "GITHUB_TOKEN": "gho_owner",
        "FACTORY_WEBHOOK": "https://hooks.example/leak",
        "AWS_SECRET_ACCESS_KEY": "aws", "NPM_TOKEN": "npm",
        "OPENAI_API_KEY": "sk-openai", "LLM_API_KEY": "sk-llm",
        "DATABASE_URL": "postgres://user:pw@db/app",
        "SSH_AUTH_SOCK": "/tmp/socket", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic ***",
    }

    def test_credentials_never_reach_the_agent(self):
        env = agentmod.minimal_env(base=self.BASE)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "FACTORY_WEBHOOK",
                     "AWS_SECRET_ACCESS_KEY", "NPM_TOKEN", "OPENAI_API_KEY",
                     "LLM_API_KEY", "DATABASE_URL", "SSH_AUTH_SOCK",
                     "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_0", "GIT_CONFIG_COUNT"):
            self.assertNotIn(name, env, f"{name} was handed to the agent")

    def test_tools_still_work(self):
        env = agentmod.minimal_env(base=self.BASE)
        for name in ("PATH", "HOME", "USERPROFILE", "SystemRoot"):
            self.assertIn(name, env, f"{name} missing - the agent cannot run tools")

    def test_windows_spelling_matched_but_original_preserved(self):
        env = agentmod.minimal_env(base=self.BASE)
        self.assertEqual(env.get("Path"), "/usr/bin")
        self.assertEqual(env.get("SystemRoot"), "C:\\Windows")

    def test_factory_still_configures_the_run(self):
        env = agentmod.minimal_env(base=self.BASE,
                                   spec_env={"LLM_BASE": "http://llm/v1"},
                                   env_extra={"TICKET_ID": "7"})
        self.assertEqual(env["LLM_BASE"], "http://llm/v1")
        self.assertEqual(env["TICKET_ID"], "7")

    def test_allowlist_can_be_widened_by_config(self):
        env = agentmod.minimal_env(base=self.BASE, allow_extra=["FACTORY_WEBHOOK"])
        self.assertEqual(env["FACTORY_WEBHOOK"], "https://hooks.example/leak")

    def test_path_placeholder_expands_to_base_path(self):
        env = agentmod.minimal_env(base=self.BASE,
                                   env_extra={"PATH": "/opt/tools/bin:{PATH}"})
        self.assertEqual(env["PATH"], "/opt/tools/bin:/usr/bin")

    def test_override_replaces_every_spelling(self):
        env = agentmod.minimal_env(base=self.BASE,
                                   env_extra={"PATH": "/opt/tools/bin:{PATH}"})
        self.assertEqual(sorted(k for k in env if k.upper() == "PATH"), ["PATH"],
                         "a stale Path/PATH twin lets the loader ignore the override")
        self.assertEqual(env["PATH"], "/opt/tools/bin:/usr/bin")

    def test_config_env_reaches_the_agent_run(self):
        spec = {"command": [sys.executable, "-c",
                            "import os; print(os.environ.get('PETRO_ENV_MARK', ''))"],
                "timeout_s": 60}
        cfg = {"agent": {"env": {"PETRO_ENV_MARK": "venv-pinned"}}}
        res = agentmod.run(cfg, spec, "t", os.getcwd(),
                           prompt_path=os.devnull, timeout_s=60)
        self.assertEqual(res["stdout"].strip(), "venv-pinned")

    def test_config_env_can_prepend_path_for_the_run(self):
        spec = {"command": [sys.executable, "-c",
                            "import os; print(os.environ['PATH'].startswith('MARKER'))"],
                "timeout_s": 60}
        cfg = {"agent": {"env": {"PATH": "MARKER:{PATH}"}}}
        res = agentmod.run(cfg, spec, "t", os.getcwd(),
                           prompt_path=os.devnull, timeout_s=60)
        self.assertEqual(res["stdout"].strip(), "True")

    def test_run_does_not_inherit_the_real_environment(self):
        marker = "FACTORY_TEST_CANARY_" + uuid.uuid4().hex
        spec = {"command": [sys.executable, "-c",
                            "import os; print('\\n'.join(sorted(os.environ)))"],
                "timeout_s": 60}
        os.environ[marker] = "value-the-agent-must-never-see"
        try:
            res = agentmod.run({}, spec, "prompt", os.getcwd())
        finally:
            os.environ.pop(marker, None)
        self.assertEqual(res["status"], "ok", res["stderr"][:300])
        seen = {line.strip().upper() for line in res["stdout"].splitlines()}
        self.assertNotIn(marker.upper(), seen)


class TestTierRouting(unittest.TestCase):
    CFG = {"agent": {"cheap": {}, "strong": {}}}

    def decision(self, ticket, cfg=None):
        return agentmod.tier_decision(cfg or self.CFG, ticket)

    def test_first_attempt_with_no_blast_is_cheap(self):
        self.assertEqual(self.decision({"blast": None, "attempts": 0}),
                         ("cheap", "default"))

    def test_retry_escalates_to_the_strong_tier(self):
        tier, reason = self.decision({"blast": None, "attempts": 1})
        self.assertEqual(tier, "strong")
        self.assertIn("escalated", reason)

    def test_explicit_tier_beats_everything(self):
        for ticket in ({"tier": "cheap", "blast": "high", "attempts": 5},
                       {"tier": "strong", "blast": "low", "attempts": 0}):
            tier, reason = self.decision(ticket)
            self.assertEqual(tier, ticket["tier"])
            self.assertEqual(reason, "explicit override")

    def test_unknown_tier_value_is_ignored(self):
        self.assertEqual(self.decision({"tier": "gpt-99", "attempts": 0})[0], "cheap")

    def test_blast_carried_from_a_previous_diff_routes_strong(self):
        self.assertEqual(self.decision({"blast": "medium", "attempts": 1}),
                         ("strong", "blast=medium"))

    def test_escalation_threshold_is_configurable(self):
        cfg = {"agent": {"cheap": {}, "strong": {}, "escalate_after_attempts": 2}}
        self.assertEqual(self.decision({"attempts": 1}, cfg)[0], "cheap")
        self.assertEqual(self.decision({"attempts": 2}, cfg)[0], "strong")

    def test_single_tier_config_still_routes_strong(self):
        self.assertEqual(self.decision({"attempts": 0}, {"agent": {"strong": {}}})[0],
                         "strong")

    def test_decision_reads_a_database_row(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "route me")
        ticket = dbmod.claim_next(con)
        self.assertEqual(agentmod.tier_decision(self.CFG, ticket)[0], "cheap")
        dbmod.bump_attempt(con, tid)
        ticket = dbmod.get_ticket(con, tid)
        self.assertEqual(agentmod.tier_decision(self.CFG, ticket)[0], "strong")

    def test_pinned_tier_survives_the_queue(self):
        con = fresh_db(self)
        dbmod.add_ticket(con, "dangerous", tier="strong")
        ticket = dbmod.claim_next(con)
        if ticket is None:
            self.fail("nothing claimed")
        self.assertEqual(ticket["tier"], "strong")

    def test_run_records_which_tier_ran_and_why(self):
        con = fresh_db(self)
        tid = dbmod.add_ticket(con, "auditable")
        run_id = dbmod.start_run(con, tid, 1, tier="strong", tier_reason="blast=high")
        row = con.execute("SELECT tier, tier_reason FROM runs WHERE id=?",
                          (run_id,)).fetchone()
        self.assertEqual((row["tier"], row["tier_reason"]), ("strong", "blast=high"))

    def test_old_databases_gain_the_columns(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        con = dbmod.connect(path)
        self.addCleanup(con.close)
        con.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT,"
                    " status TEXT, attempts INTEGER, max_retries INTEGER,"
                    " budget_usd REAL, spent_usd REAL, created_at REAL, updated_at REAL)")
        con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, ticket_id INTEGER,"
                    " attempt INTEGER, status TEXT, cost_usd REAL, detail TEXT,"
                    " pid INTEGER, started_at REAL, ended_at REAL)")
        dbmod.init(con)
        for table, col in (("tickets", "tier"), ("runs", "tier"), ("runs", "tier_reason"),
                           ("tickets", "issue_number"), ("tickets", "issue_node"),
                           ("tickets", "project_item"), ("tickets", "issue_state")):
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            self.assertIn(col, cols, f"{table}.{col} missing after migration")


class TestBoard(unittest.TestCase):
    def _env(self):
        import tempfile
        from factory import board, db as dbmod
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        con = dbmod.connect(path)
        self.addCleanup(con.close)
        dbmod.init(con)
        cfg = {"repo": "o/r", "project_id": "PVT_x",
               "gh_token": "t"}
        return board, con, cfg

    def test_every_queue_status_has_a_column(self):
        from factory import board
        for status in ("queued", "running", "pr_open", "needs_review",
                       "escalated", "merged", "closed"):
            self.assertIn(status, board.COLUMN_FOR)

    def test_dry_run_touches_nothing(self):
        board, con, cfg = self._env()
        dbmod.add_ticket(con, "t")
        calls = []
        with patch.object(board, "_request", lambda *a, **k: calls.append(a)):
            moves = board.sync_board(cfg, con, dry=True)
        self.assertEqual(moves, [(1, "queued", "Open")])
        self.assertEqual(calls, [])

    def test_missing_project_id_is_a_clear_error(self):
        board, con, cfg = self._env()
        cfg.pop("project_id")
        with self.assertRaises(board.BoardError):
            board.sync_board(cfg, con)

    def test_issue_created_once_and_remembered(self):
        board, con, cfg = self._env()
        dbmod.add_ticket(con, "t")
        ticket = con.execute("SELECT * FROM tickets").fetchone()
        calls = []

        def fake(cfg_, url, method, body=None, graphql=False):
            calls.append((url, method))
            return {"number": 42, "node_id": "I_ABC42"}
        with patch.object(board, "_request", fake):
            self.assertEqual(board.ensure_issue(cfg, con, ticket), 42)
        row = con.execute("SELECT issue_number, issue_node, issue_state"
                          " FROM tickets").fetchone()
        self.assertEqual((row["issue_number"], row["issue_node"],
                          row["issue_state"]), (42, "I_ABC42", "open"))
        fresh = con.execute("SELECT * FROM tickets").fetchone()
        self.assertEqual(board.ensure_issue(cfg, con, fresh), 42)
        self.assertEqual(len(calls), 1)

    def test_close_issue_is_idempotent(self):
        board, con, cfg = self._env()
        dbmod.add_ticket(con, "t")
        con.execute("UPDATE tickets SET issue_number=42, issue_state='open'")
        ticket = con.execute("SELECT * FROM tickets").fetchone()
        calls = []
        with patch.object(board, "_request",
                          lambda c, u, m, b=None, graphql=False: calls.append((u, m)) or {}):
            board.close_issue(cfg, con, ticket)
            board.close_issue(cfg, con, con.execute("SELECT * FROM tickets").fetchone())
        self.assertEqual(calls, [("/repos/o/r/issues/42", "PATCH")])


class TestParseTickets(unittest.TestCase):
    """factory.cli.parse_tickets: rows plus the first bad line, if any."""

    def test_valid_lines_become_rows(self):
        rows, bad = climod.parse_tickets(
            '{"title": "first"}\n{"title": "second", "body": "more"}\n')
        self.assertIsNone(bad)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["title"] for r in rows], ["first", "second"])
        self.assertEqual(rows[0]["body"], "")

    def test_blank_lines_are_skipped(self):
        rows, bad = climod.parse_tickets(
            '\n   \n{"title": "kept"}\n\n\t\n{"title": "also kept"}\n\n')
        self.assertIsNone(bad)
        self.assertEqual([r["title"] for r in rows], ["kept", "also kept"])

    def test_line_without_title_names_the_line(self):
        rows, bad = climod.parse_tickets('{"title": "ok"}\n{"body": "no title"}\n')
        self.assertEqual(bad, "line 2: missing title")
        self.assertEqual([r["title"] for r in rows], ["ok"])

    def test_blank_title_is_missing_title(self):
        self.assertEqual(climod.parse_tickets('{"title": "   "}')[1],
                         "line 1: missing title")

    def test_only_the_first_bad_line_is_named(self):
        bad = climod.parse_tickets('{"nope": 1}\n{"also": 2}\n')[1]
        self.assertEqual(bad, "line 1: missing title")

    def test_bad_json_and_non_objects_are_reported(self):
        self.assertEqual(climod.parse_tickets("{not json")[1], "line 1: invalid JSON")
        self.assertEqual(climod.parse_tickets("[1, 2]")[1], "line 1: not a JSON object")

    def test_optional_keys_pass_through(self):
        rows, bad = climod.parse_tickets(
            '{"title": "t", "body": "b", "tier": "strong", '
            '"budget_usd": 5.0, "retries": 1}')
        self.assertIsNone(bad)
        self.assertEqual(rows[0], {"title": "t", "body": "b", "tier": "strong",
                                   "budget_usd": 5.0, "retries": 1})

    def test_defaults_when_optional_keys_are_absent(self):
        row = climod.parse_tickets('{"title": "t"}')[0][0]
        self.assertEqual(row["body"], "")
        self.assertIsNone(row["tier"])
        self.assertEqual(row["budget_usd"], 2.0)
        self.assertEqual(row["retries"], 3)

    def test_unknown_tier_is_rejected(self):
        self.assertEqual(climod.parse_tickets('{"title": "t", "tier": "huge"}')[1],
                         "line 1: tier must be cheap or strong")

    def test_keys_the_queue_does_not_read_are_ignored(self):
        row = climod.parse_tickets('{"title": "t", "blast": "high"}')[0][0]
        self.assertNotIn("blast", row)

    def test_empty_text_yields_no_rows_and_no_error(self):
        self.assertEqual(climod.parse_tickets(""), ([], None))
class TestAddFromFile(unittest.TestCase):
    """`factory add --from-file FILE` is how a backlog enters the queue in bulk.

    Only the real command line proves the wiring: the parser has to accept a
    missing title when --from-file is present, the reader has to turn each JSONL
    line into a row, and the queue has to end up holding both tickets as queued.
    """

    def test_two_line_jsonl_imports_two_queued_tickets(self):
        import contextlib
        import io
        import json

        from factory import cli
        fd, db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, cfg = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        fd, src = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"repo": ".", "db": db}, fh)
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"title": "first import", "body": "one",
                                 "tier": "cheap"}) + "\n")
            fh.write(json.dumps({"title": "second import", "body": "two",
                                 "tier": "strong"}) + "\n")
        con = dbmod.connect(db)
        dbmod.init(con)
        self.addCleanup(con.close)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["-c", cfg, "add", "--from-file", src])
        printed = buf.getvalue().split()

        rows = dbmod.list_tickets(con, status="queued")
        self.assertEqual([r["title"] for r in rows],
                         ["second import", "first import"])
        self.assertEqual({r["status"] for r in rows}, {"queued"})
        self.assertEqual({r["tier"] for r in rows}, {"cheap", "strong"})
        self.assertEqual(printed, [str(r["id"]) for r in reversed(rows)])


class TestSpendToday(unittest.TestCase):
    """The daily cap resets at *local* midnight, so the boundary must follow the
    machine's timezone, not a UTC day. `now` is injectable, so the tests hand it
    in directly instead of faking the clock."""

    def _midnight(self):
        """Local midnight, computed from the wall clock fields so it is correct
        whatever timezone the suite runs in."""
        import time
        t = time.localtime()
        seconds_today = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
        return time.time() - seconds_today

    def _run_at(self, con, started_at, cost):
        con.execute(
            "INSERT INTO runs (ticket_id, attempt, status, cost_usd, detail,"
            " started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
            (1, 1, "ok", cost, "", started_at, started_at + 1))

    def test_sums_only_runs_since_local_midnight(self):
        """A run started 100s before local midnight is yesterday's spend; the
        two after it are today's, and only those two count toward the cap."""
        import time
        con = fresh_db(self)
        midnight = self._midnight()
        self._run_at(con, midnight - 100, 5.00)
        self._run_at(con, midnight + 10, 0.75)
        self._run_at(con, midnight + 20, 0.25)
        self.assertAlmostEqual(dbmod.spend_today(con, now=time.time()), 1.0)

    def test_zero_when_no_runs_today(self):
        import time
        con = fresh_db(self)
        self._run_at(con, self._midnight() - 100, 5.00)
        self.assertAlmostEqual(dbmod.spend_today(con, now=time.time()), 0.0)



class TestVersionCommand(unittest.TestCase):
    """`factory version` names the code that is actually installed.

    A factory that runs unattended needs one command that touches nothing - no
    config, no database, no network - so an operator can tell which build a
    checkout is running.
    """

    def test_version_prints_the_package_version_and_exits_zero(self):
        import contextlib
        import io

        import factory
        from factory import cli
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(["version"])
        except SystemExit as exc:
            self.assertEqual(exc.code, 0)
        printed = buf.getvalue().strip()
        self.assertTrue(printed)
        self.assertEqual(printed, factory.__version__)


class TestBoardIssueNode(unittest.TestCase):
    """Tickets imported by `factory import-issues` carry an issue_number but no
    issue_node, so ensure_item() has to fetch the node id before the project
    mutation: contentId=NULL is a GraphQL type error."""

    def _env(self):
        from factory import board, db as dbmod
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        con = dbmod.connect(path)
        self.addCleanup(con.close)
        dbmod.init(con)
        cfg = {"repo": "o/r", "project_id": "PVT_x", "gh_token": "t"}
        return board, con, cfg

    def _fake_http(self, calls):
        """Stand-in for the whole HTTP layer: REST issue lookup + GraphQL."""
        def fake(cfg_, url, method, body=None, graphql=False):
            calls.append((url, method, body))
            if graphql:
                return {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}}
            return {"node_id": "I_NODE7"}
        return fake

    def test_missing_issue_node_is_looked_up_before_the_mutation(self):
        board, con, cfg = self._env()
        dbmod.add_ticket(con, "t", source="github-issue", ref="#7", issue_number=7)
        ticket = con.execute("SELECT * FROM tickets").fetchone()
        self.assertIsNone(ticket["issue_node"])
        calls = []
        with patch.object(board, "_request", self._fake_http(calls)):
            self.assertEqual(board.ensure_item(cfg, con, ticket, "PVT_x"), "PVTI_1")
        self.assertIn(("/repos/o/r/issues/7", "GET", None), calls)
        mutation = [c for c in calls if c[1] == "POST" and c[2] is not None][-1]
        self.assertEqual(mutation[2]["variables"],
                         {"projectId": "PVT_x", "contentId": "I_NODE7"})
        self.assertLess(calls.index(("/repos/o/r/issues/7", "GET", None)),
                        calls.index(mutation))
        row = con.execute("SELECT issue_number, issue_node, project_item"
                          " FROM tickets").fetchone()
        self.assertEqual((row["issue_number"], row["issue_node"],
                          row["project_item"]), (7, "I_NODE7", "PVTI_1"))

    def test_known_issue_node_is_not_looked_up_again(self):
        board, con, cfg = self._env()
        dbmod.add_ticket(con, "t", source="github-issue", ref="#7", issue_number=7)
        con.execute("UPDATE tickets SET issue_node='I_ABC42'")
        ticket = con.execute("SELECT * FROM tickets").fetchone()
        calls = []
        with patch.object(board, "_request", self._fake_http(calls)):
            self.assertEqual(board.ensure_item(cfg, con, ticket, "PVT_x"), "PVTI_1")
        gets = [c for c in calls if c[1] == "GET"]
        self.assertLessEqual(len(gets), 1)
        self.assertEqual(gets, [], "node id already known, nothing to look up")
        self.assertEqual(con.execute("SELECT issue_node FROM tickets").fetchone()[0],
                         "I_ABC42")


class TestRunTestsGate(unittest.TestCase):
    def test_pass_fail_and_timeout(self):
        import tempfile
        d = tempfile.mkdtemp()
        ok, out = gates.run_tests(d, f'"{sys.executable}" -c "print(1)"')
        self.assertTrue(ok)
        self.assertIn("1", out)
        ok, out = gates.run_tests(d, f'"{sys.executable}" -c "import sys; sys.exit(3)"')
        self.assertFalse(ok)
        ok, out = gates.run_tests(
            d, f'"{sys.executable}" -c "import time; time.sleep(30)"', timeout_s=1)
        self.assertFalse(ok)
        self.assertIn("timed out", out)


class TestChatBody(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "localllm", os.path.join(os.path.dirname(__file__), "..",
                                     "scripts", "local_llm.py"))
        cls.llm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.llm)

    def test_step_cap_and_thinking_default_off_in_body(self):
        body = self.llm.chat_body([{"role": "user", "content": "x"}])
        self.assertEqual(body["max_tokens"], self.llm.STEP_MAX_TOKENS)
        self.assertNotIn("chat_template_kwargs", body)

    def test_disable_thinking_adds_template_kwargs(self):
        with patch.object(self.llm, "DISABLE_THINKING", True):
            body = self.llm.chat_body([{"role": "user", "content": "x"}])
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_wander_nudge_fires_at_limit_then_every_four(self):
        self.assertIsNone(self.llm.wander_nudge(7, limit=8))
        self.assertIn("STOP EXPLORING", self.llm.wander_nudge(8, limit=8))
        self.assertIsNone(self.llm.wander_nudge(9, limit=8))
        self.assertIn("STOP EXPLORING", self.llm.wander_nudge(12, limit=8))


class TestEngineLock(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".lock")
        os.close(fd)

    def tearDown(self):
        os.unlink(self.path)

    def test_lock_is_exclusive_and_releasable(self):
        held = workermod._engine_lock(self.path)
        self.assertIsNotNone(held)
        self.assertIsNone(workermod._engine_lock(self.path))
        workermod._release_engine_lock(held)
        again = workermod._engine_lock(self.path)
        self.assertIsNotNone(again)
        workermod._release_engine_lock(again)

    def test_process_once_returns_busy_when_locked(self):
        con = fresh_db(self)
        held = workermod._engine_lock(self.path)
        try:
            with patch.object(workermod, "LOCK_PATH", self.path):
                self.assertEqual(workermod.process_once({}, con), "busy")
        finally:
            workermod._release_engine_lock(held)


if __name__ == "__main__":
    unittest.main()
