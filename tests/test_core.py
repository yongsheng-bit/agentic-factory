"""Tests for the parts that must not be wrong: claims, caps, gates."""

import json
import os
import sqlite3
import tempfile
import unittest

from factory import agent as agentmod
from factory import blast as blastmod
from factory import db as dbmod
from factory import gates
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
        # 3 files, tiny diff, no triggers -> low is correct; but a single
        # migration file must never be low.
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
        # A prompt inside the checkout gets committed by `git add -A`.
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
        self._write("node_modules/pkg/x.js", "var k='AKIAIOSFODNN7EXAMPLE'\n")
        self.assertTrue(gates.check(self.dir)["ok"])


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
            fh.write(".factory-tmp/\n")   # the sandbox scratch dir; see the regression test
        with open(os.path.join(self.dir, "app.py"), "w") as fh:
            fh.write("print(1)\n")
        os.makedirs(os.path.join(self.dir, "build"))
        with open(os.path.join(self.dir, "build", "out.js"), "w") as fh:
            fh.write("v1\n")             # a repo that TRACKS build/ on purpose
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
        # Unstaging artifacts must never stage the DELETION of a file the repo
        # tracks deliberately - only newly added files are dropped.
        self._touch("build/out.js", "v2\n")
        self.assertTrue(worktree.commit_all(self.dir, "rebuild"))
        self.assertEqual(self._committed(), ["build/out.js"])

    def test_ignored_scratch_dir_does_not_break_the_add(self):
        # Regression: :(exclude) pathspecs naming a gitignored directory make
        # `git add` exit 1, which escalated every ticket. The sandbox writes
        # its scratch into .factory-tmp, so this fires on every real run.
        self._touch(".factory-tmp/scratch.txt", "junk")
        self._touch("real.py", "print(2)\n")
        self.assertTrue(worktree.commit_all(self.dir, "add real"))
        self.assertEqual(self._committed(), ["real.py"])

    def test_no_changes_returns_false(self):
        self.assertFalse(worktree.commit_all(self.dir, "nothing"))


if __name__ == "__main__":
    unittest.main()
