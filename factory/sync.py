"""Pull GitHub's verdict back into the local queue.

Why this exists
---------------
The pipeline used to stop at `pr_open`, so the queue filled with tickets that
were merged, closed or abandoned months ago and never counted a single
completed job. Worse, "merged" said nothing about *how*: with branch
protection on, an admin can still merge a green PR with no review at all, and
a ledger that records both outcomes as `merged` cannot tell a reviewed change
from one that walked through the human gate.

So sync records the mechanism, not just the outcome: `review_state` is
`approved` when a real APPROVED review exists, and `admin-bypass` when
GitHub merged without one.

Nothing here guesses. A ticket only moves when `gh` reports the PR's state;
a failed read leaves the row alone and is reported.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import worktree as wt

PR_FIELDS = ("number,state,mergedAt,mergedBy,mergeCommit,reviews,mergeStateStatus")
_URL = re.compile(r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/"
                  r"(?P<number>\d+)(?:[/?#].*)?$")


class SyncError(RuntimeError):
    pass


def parse_pr_url(url):
    """(owner, repo, number) from a PR URL, or raise.

    Parsing the URL rather than storing a repo config key means a ticket
    always points at the thing the sync checks - no chance of asking GitHub
    about the wrong repository and trusting the answer.
    """
    m = _URL.match((url or "").strip())
    if not m:
        raise SyncError(f"not a pull request url: {url!r}")
    return m.group("owner"), m.group("repo"), int(m.group("number"))


def _epoch(iso):
    """ISO8601 from GitHub ('2026-11-17T13:50:36Z') -> epoch seconds."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def classify(pr):
    """(new_status, review_state) from a PR payload. None status = no change."""
    state = (pr.get("state") or "").upper()
    if state == "MERGED":
        approved = any((r.get("state") or "").upper() == "APPROVED"
                       for r in pr.get("reviews") or [])
        return "merged", ("approved" if approved else "admin-bypass")
    if state == "CLOSED":
        return "closed", "unreviewed"
    return None, None


def fetch_pr(gh, owner, repo, number):
    p = subprocess.run(
        [gh, "pr", "view", str(number), "--repo", f"{owner}/{repo}",
         "--json", PR_FIELDS],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SyncError(f"gh pr view {owner}/{repo}#{number}: "
                        f"{(p.stderr or p.stdout).strip()[:300]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise SyncError(f"gh returned unparseable json for {owner}/{repo}#{number}: {exc}")


def sync(cfg, con, gh_path=None):
    """Update every ticket that has a PR. Returns (changes, errors)."""
    rows = con.execute(
        "SELECT id, ref, title, status, pr_url FROM tickets "
        "WHERE pr_url IS NOT NULL AND pr_url <> '' "
        "  AND status IN ('pr_open','needs_review','escalated')").fetchall()
    gh = wt._gh_binary(gh_path or (cfg or {}).get("gh_path"))
    if not gh:
        raise SyncError(
            "gh CLI not found, so PR state cannot be read. Set gh_path in "
            "factory.json. Refusing to guess at merge state.")

    changes, errors = [], []
    for r in rows:
        try:
            owner, repo, number = parse_pr_url(r["pr_url"])
            pr = fetch_pr(gh, owner, repo, number)
        except SyncError as exc:
            # A ticket that could not be checked keeps its old status. Marking
            # something merged because a lookup failed is exactly the kind of
            # lie this module exists to prevent.
            errors.append((r["id"], str(exc)))
            continue
        status, review_state = classify(pr)
        if not status:
            continue
        merged_by = ((pr.get("mergedBy") or {}).get("login"))
        con.execute(
            "UPDATE tickets SET status=?, review_state=?, merged_by=?, merged_at=?,"
            " updated_at=? WHERE id=?",
            (status, review_state, merged_by, _epoch(pr.get("mergedAt")),
             time.time(), r["id"]))
        changes.append((r["id"], r["status"], status, review_state, merged_by,
                        r["pr_url"]))
    return changes, errors
