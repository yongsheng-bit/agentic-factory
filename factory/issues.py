"""GitHub issues as a ticket source.

The workflow this exists for: spot a bug from your phone, file it as an issue
with the ``factory`` label, and the next scheduled tick builds it. The label
is the opt-in: unlabelled issues are invisible to the factory, so the tracker
stays a tracker until you say otherwise. A ``tier:strong`` label routes the
ticket to the strong tier.

Idempotency is by issue number: an issue already in the queue is skipped,
never duplicated, even after its ticket ships. Re-running the import must
never resurrect a merged ticket as new work.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess

from . import db as dbmod, worktree as wt

_SLUG = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


class IssueError(RuntimeError):
    pass


def repo_slug(cfg):
    """owner/repo from the checkout's origin URL, ssh or https form."""
    url = wt._git(["remote", "get-url", "origin"], cwd=(cfg or {}).get("repo")).strip()
    m = _SLUG.search(url)
    if not m:
        raise IssueError(f"cannot read owner/repo from origin url: {url!r}")
    return f"{m.group(1)}/{m.group(2)}"


def parse_issues(payload):
    """gh issue list --json output -> rows, with tier taken from labels."""
    try:
        items = json.loads(payload)
    except ValueError as exc:
        raise IssueError(f"gh returned unparseable json: {exc}")
    out = []
    for it in items:
        labels = [str(l.get("name", "")) for l in it.get("labels") or []]
        tier = ("strong" if "tier:strong" in labels
                else "cheap" if "tier:cheap" in labels else None)
        out.append({"number": int(it["number"]),
                    "title": str(it.get("title") or ""),
                    "body": str(it.get("body") or ""),
                    "tier": tier})
    return out


def import_issues(cfg, con, gh_path=None, label="factory"):
    """Queue open labelled issues. Returns (slug, imported, skipped)."""
    slug = repo_slug(cfg)
    gh = wt._gh_binary(gh_path or (cfg or {}).get("gh_path"))
    if not gh:
        raise IssueError("gh CLI not found; set gh_path in factory.json")
    p = subprocess.run(
        [gh, "issue", "list", "--repo", slug, "--state", "open", "--label", label,
         "--json", "number,title,body,labels", "--limit", "50"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=wt._token_env((cfg or {}).get("gh_token")))
    if p.returncode != 0:
        raise IssueError(f"gh issue list {slug}: {(p.stderr or p.stdout).strip()[:300]}")
    imported, skipped = [], []
    for it in parse_issues(p.stdout):
        have = con.execute(
            "SELECT id, status FROM tickets WHERE issue_number=?",
            (it["number"],)).fetchone()
        if have:
            skipped.append((it["number"], int(have["id"])))
            continue
        try:
            tid = dbmod.add_ticket(con, it["title"], it["body"],
                                   source="github-issue",
                                   ref=f"#{it['number']}", tier=it["tier"],
                                   issue_number=it["number"])
        except sqlite3.IntegrityError:
            have = con.execute(
                "SELECT id FROM tickets WHERE issue_number=?",
                (it["number"],)).fetchone()
            skipped.append((it["number"], int(have["id"])))
            continue
        imported.append((it["number"], tid))
    con.commit()
    return slug, imported, skipped
