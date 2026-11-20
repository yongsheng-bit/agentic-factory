"""End-to-end smoke test: real git, real worktrees, fake agent, no network.

Run:  python tests/smoke.py
Exits non-zero if any scenario misbehaves.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factory import db as dbmod
from factory import worker

FAKE = str(ROOT / "tests" / "fake_agent.py")
PY = sys.executable


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)


def make_repo():
    repo = tempfile.mkdtemp(prefix="smoke-repo-")
    sh(["git", "init", "-b", "main"], repo)
    sh(["git", "config", "user.email", "t@example.com"], repo)
    sh(["git", "config", "user.name", "Test"], repo)
    pathlib.Path(repo, "README.md").write_text("# demo\n", encoding="utf-8")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-m", "init"], repo)
    return repo


def cfg_for(repo, db, mode, **over):
    cfg = {
        "repo": repo,
        "db": db,
        "base_branch": "HEAD",
        "push": False,
        "daily_budget_usd": 50.0,
        "gates": {"require_tests": False},
        "agent": {
            "strong": {"command": [PY, FAKE, "{prompt_file}"], "timeout_s": 120,
                       "env": {"FAKE_MODE": mode}},
            "cheap": {"command": [PY, FAKE, "{prompt_file}"], "timeout_s": 120,
                      "env": {"FAKE_MODE": mode}},
        },
    }
    cfg.update(over)
    return cfg


def run_scenario(name, mode, expect_status, expect_blast=None, runs=1, title="do the thing"):
    repo = make_repo()
    db = os.path.join(tempfile.mkdtemp(prefix="smoke-db-"), "f.db")
    con = dbmod.connect(db)
    dbmod.init(con)
    cfg = cfg_for(repo, db, mode)
    tid = dbmod.add_ticket(con, title, "some body", ref=f"SMOKE-{mode}")
    outs = [worker.process_once(cfg, con) for _ in range(runs)]
    t = dbmod.get_ticket(con, tid)
    ok = t["status"] == expect_status and (expect_blast is None or t["blast"] == expect_blast)
    cost = dbmod.ticket_spend(con, tid)
    branches = sh(["git", "branch", "--list", "agent/*"], repo).stdout.strip()
    print(f"{'PASS' if ok else 'FAIL'}  {name:<22} status={t['status']:<12} "
          f"blast={str(t['blast']):<6} attempts={t['attempts']} "
          f"cost=${cost:.4f}  last={outs[-1][:40]!r}")
    if not ok:
        print(f"      expected status={expect_status} blast={expect_blast}")
    shutil.rmtree(repo, ignore_errors=True)
    return ok, t, cost, branches


def main():
    results = []

    ok, t, cost, branches = run_scenario("happy path", "ok", "pr_open", "low")
    results.append(ok)
    results.append(cost > 0)
    print(f"{'PASS' if cost > 0 else 'FAIL'}  cost parsed from agent output: ${cost:.4f}")
    results.append("agent/" in branches)
    print(f"{'PASS' if 'agent/' in branches else 'FAIL'}  branch kept for PR: "
          f"{branches.splitlines()[:1]}")


    repo = make_repo()
    db = os.path.join(tempfile.mkdtemp(prefix="smoke-db-"), "f.db")
    con = dbmod.connect(db)
    dbmod.init(con)
    cfg = cfg_for(repo, db, "ok")
    dbmod.add_ticket(con, "prompt leak check", ref="PL-1")
    worker.process_once(cfg, con)
    shown = sh(["git", "show", "--name-only", "--format=", "agent/pl-1-1"], repo).stdout
    committed = [l.strip() for l in shown.splitlines() if l.strip()]
    clean = committed == ["fix.py"]
    results.append(clean)
    print(f"{'PASS' if clean else 'FAIL'}  commit contains only the agent's files: {committed}")
    shutil.rmtree(repo, ignore_errors=True)

    ok, _, _, _ = run_scenario("migration -> human gate", "migration", "needs_review", "high")
    results.append(ok)

    ok, _, _, _ = run_scenario("secret -> blocked", "secret", "escalated")
    results.append(ok)

    ok, t, _, _ = run_scenario("crash -> escalate @3", "fail", "escalated", runs=3)
    results.append(ok and t["attempts"] == 3)
    print(f"{'PASS' if t['attempts'] == 3 else 'FAIL'}  retry cap honoured "
          f"(attempts={t['attempts']})")

    ok, _, _, _ = run_scenario("empty diff -> escalate", "empty", "escalated", runs=3)
    results.append(ok)


    repo = make_repo()
    db = os.path.join(tempfile.mkdtemp(prefix="smoke-db-"), "f.db")
    con = dbmod.connect(db)
    dbmod.init(con)
    cfg = cfg_for(repo, db, "ok")
    dbmod.add_ticket(con, "never starts")
    dbmod.set_paused(con, True)
    out = worker.process_once(cfg, con)
    results.append(out == "paused")
    print(f"{'PASS' if out == 'paused' else 'FAIL'}  kill switch blocks new work ({out})")
    dbmod.set_paused(con, False)
    results.append(worker.process_once(cfg, con) != "paused")
    print("PASS  resume lets work through")


    repo2 = make_repo()
    db2 = os.path.join(tempfile.mkdtemp(prefix="smoke-db-"), "f.db")
    con2 = dbmod.connect(db2)
    dbmod.init(con2)
    cfg2 = cfg_for(repo2, db2, "ok", daily_budget_usd=0.0)
    dbmod.add_ticket(con2, "over budget")
    out = worker.process_once(cfg2, con2)
    results.append(out == "budget" and dbmod.is_paused(con2))
    print(f"{'PASS' if out == 'budget' else 'FAIL'}  daily budget auto-pauses queue ({out})")


    repo3 = make_repo()
    db3 = os.path.join(tempfile.mkdtemp(prefix="smoke-db-"), "f.db")
    con3 = dbmod.connect(db3)
    dbmod.init(con3)
    cfg3 = cfg_for(repo3, db3, "ok")
    dbmod.add_ticket(con3, "first", ref="A-1")
    dbmod.add_ticket(con3, "second", ref="A-2")
    worker.process_once(cfg3, con3)
    worker.process_once(cfg3, con3)
    br = sh(["git", "branch", "--list", "agent/*"], repo3).stdout.split()
    results.append(len(br) == 2)
    print(f"{'PASS' if len(br) == 2 else 'FAIL'}  per-ticket branches isolated: {br}")


    results.append(not any(r is False for r in results))

    print("\n" + ("ALL SMOKE TESTS PASSED" if all(results) else "SMOKE TESTS FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
