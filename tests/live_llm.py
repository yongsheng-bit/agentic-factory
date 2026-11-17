"""Live end-to-end test of the cheap tier against the real local endpoint.

Builds a throwaway git repo containing a genuine bug and a failing unittest,
queues one ticket describing it, runs the real worker (real worktree, real
gates, real commit), then checks out the agent's branch and runs the tests
against THAT commit.

The last step is the only one that counts. A ticket that reaches `pr_open` with
a diff that does not fix the bug is a failure, and every weaker test would let
it through.

Skips cleanly (exit 0) when the endpoint is down, so it can live in the repo.
Run:  python tests/live_llm.py            (one ticket)
      python tests/live_llm.py 5          (five tickets, for a hit rate)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import db as dbmod          # noqa: E402
from factory import worker               # noqa: E402

BASE = os.environ.get("LLM_BASE", "http://192.168.68.73:8080/v1")
PY = sys.executable
AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "local_llm.py")

BUGGY = '''"""Tank-dip receipt grade normalisation."""

GRADE_MAP = {
    "MAXIMUM": "REGULAR",
    "MAX PLUS": "PLUS",
    "DIESEL": "DIESEL",
    "UTRA94": "ULTRA94",
}


def normalize_grade(printed):
    """Map a grade as printed on a pump receipt to the ledger grade."""
    return GRADE_MAP.get(printed.strip().upper(), printed.strip().upper())


def parse_dip(line):
    """Parse 'D3,MAX PLUS,550721.47' -> (pump, grade, litres)."""
    pump, printed, litres = line.split(",")
    return pump.strip(), normalize_grade(printed), float(litres)
'''

TESTS = '''import unittest

from dips import normalize_grade, parse_dip


class TestGrades(unittest.TestCase):
    def test_regular(self):
        self.assertEqual(normalize_grade("MAXIMUM"), "REGULAR")

    def test_supreme(self):
        # Petro-Canada prints MAXIMUM SUPREME on the receipt; the ledger calls
        # it SUPREME. This is the mapping that is currently missing.
        self.assertEqual(normalize_grade("MAXIMUM SUPREME"), "SUPREME")

    def test_parse(self):
        self.assertEqual(parse_dip("D3,MAXIMUM SUPREME,861295.42"),
                         ("D3", "SUPREME", 861295.42))


if __name__ == "__main__":
    unittest.main()
'''

TICKET = (
    "Pump receipts print the grade as `MAXIMUM SUPREME`, but the ledger column is "
    "`SUPREME`. `normalize_grade()` in dips.py has no entry for it, so those rows keep "
    "the raw printed string and never line up with the ledger.\n\n"
    "test_dips.py already contains the failing expectations. Run "
    "`{py} -m unittest -q` to reproduce, and run it again to confirm the fix."
)


def endpoint_up():
    try:
        with urllib.request.urlopen(BASE + "/models", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True)


def one_run(root, repo, n):
    """Queue the bug ticket, run one worker pass, verify the branch's tests."""
    cfg = {
        "repo": repo,
        "db": os.path.join(root, f"f{n}.db"),
        "base_branch": "HEAD",
        "push": False,
        "daily_budget_usd": 50,
        "agent": {
            "strong": {"command": [PY, AGENT, "{prompt_file}"], "timeout_s": 1800},
            "cheap": {"command": [PY, AGENT, "{prompt_file}"], "timeout_s": 1800},
        },
    }
    con = dbmod.connect(cfg["db"])
    dbmod.init(con)
    tid = dbmod.add_ticket(con, title="MAXIMUM SUPREME receipts are not mapped to SUPREME",
                           body=TICKET.format(py=PY), ref=f"PETRO-{n}", budget_usd=5.0,
                           max_retries=1)

    t0 = time.time()
    worker.process_once(cfg, con)
    elapsed = time.time() - t0

    row = dbmod.get_ticket(con, tid)
    run = con.execute("SELECT detail FROM runs WHERE ticket_id=? ORDER BY id DESC LIMIT 1",
                      (tid,)).fetchone()
    detail = {}
    if run and run["detail"]:
        for line in run["detail"].splitlines():
            if line.strip().startswith("{"):
                try:
                    detail = json.loads(line)
                except json.JSONDecodeError:
                    pass
                break

    print(f"  status={row['status']}  blast={row['blast']}  steps={detail.get('steps')}  "
          f"tokens={detail.get('input_tokens')}/{detail.get('output_tokens')}  {elapsed:.0f}s")
    print(f"  agent said: {str(detail.get('result'))[:300]}")

    if row["status"] != "pr_open":
        return False, f"status={row['status']}"

    verify = os.path.join(root, f"verify{n}")
    r = git(repo, "worktree", "add", "--detach", verify, row["branch"])
    if r.returncode != 0:
        return False, "could not check out agent branch: " + r.stderr[:200]
    post = subprocess.run([PY, "-m", "unittest", "-q"], cwd=verify, capture_output=True,
                          text=True)
    diff = git(repo, "diff", "main", row["branch"]).stdout
    shutil.rmtree(verify, ignore_errors=True)
    if post.returncode != 0:
        print("  tests on agent branch:\n" + (post.stdout + post.stderr)[-700:])
        return False, "PR opened but tests still fail"
    print("  diff:")
    for line in diff.splitlines():
        if line.startswith(("+", "-", "@")) and not line.startswith(("+++", "---")):
            print("    " + line)
    return True, diff


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    if not endpoint_up():
        print(f"SKIP: no LLM endpoint at {BASE}")
        return 0

    root = tempfile.mkdtemp(prefix="live-agent-")
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    open(os.path.join(repo, "dips.py"), "w", encoding="utf-8").write(BUGGY)
    open(os.path.join(repo, "test_dips.py"), "w", encoding="utf-8").write(TESTS)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")

    # The bug must actually be failing before the agent touches it, otherwise
    # a green result proves nothing.
    pre = subprocess.run([PY, "-m", "unittest", "-q"], cwd=repo, capture_output=True, text=True)
    assert pre.returncode != 0, "test repo is not actually broken - the test is worthless"

    wins = 0
    for i in range(1, rounds + 1):
        print(f"[ticket {i}]")
        if i > 1:   # reset to the buggy baseline so every round faces the same bug
            git(repo, "checkout", "-q", "main")
            git(repo, "reset", "--hard", "-q", "main")
        ok, _why = one_run(root, repo, i)
        wins += ok

    shutil.rmtree(root, ignore_errors=True)
    print(f"\n{wins}/{rounds} tickets produced a PR whose branch passes the tests")
    return 0 if wins == rounds else 1


if __name__ == "__main__":
    sys.exit(main())
