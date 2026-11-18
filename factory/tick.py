"""One unattended cycle: reap, sync, drain, alert.

Designed to be launched by a scheduler every few minutes with no human in the
loop, so it must be safe to overlap with a human using the factory: it
refuses to build while another worker holds a run, it never touches the kill
switch, and a GitHub read failure is reported instead of guessed around.

The return value is a plain dict so ``factory tick --json`` can be parsed by
whatever scheduled it. Alerts go to the configured webhook only when
something actually moved: a quiet cycle must stay silent, or the channel
becomes noise and the noise gets muted.

Ticket intake runs inside the cycle: open issues carrying the ``factory``
label join the queue here, so a ticket filed from a phone is built by the
next tick. A dead tracker call alerts but never stops the queue from
draining.
"""

from __future__ import annotations

from . import board as boardmod
from . import db as dbmod
from . import issues as issuesmod, notify, sync as syncmod, worker


def tick(cfg, con, gh_path=None, max_tickets=2, board=True):
    s = {"busy": False, "reaped": 0, "sync_changes": 0, "sync_errors": 0,
         "issues_imported": 0, "board_error": None, "outcomes": [],
         "alerts": [], "spend_today": 0.0, "idle": True}

    s["reaped"] = worker.watchdog(
        cfg, con, max_age_s=int(cfg.get("watchdog_stale_s", 3600)))

    if dbmod.running_pids(con):
        s["busy"] = True
        return s

    try:
        changes, errors = syncmod.sync(cfg, con, gh_path=gh_path)
        s["sync_changes"], s["sync_errors"] = len(changes), len(errors)
        for tid, old, new, review, by, url in changes:
            if new == "merged":
                s["alerts"].append(f"ticket {tid} merged [{review}] {url}")
            elif new == "closed":
                s["alerts"].append(f"ticket {tid} closed unmerged {url}")
    except syncmod.SyncError as exc:
        s["sync_errors"] = 1
        s["alerts"].append(f"sync failed: {exc}")

    try:
        _, imported, _skipped = issuesmod.import_issues(cfg, con, gh_path=gh_path)
        s["issues_imported"] = len(imported)
        for num, tid in imported:
            s["alerts"].append(f"issue #{num} -> ticket {tid}")
    except issuesmod.IssueError as exc:
        s["alerts"].append(f"issue import failed: {exc}")

    if board and cfg.get("project_id"):
        try:
            boardmod.sync_board(cfg, con)
        except Exception as exc:
            s["board_error"] = str(exc)[:200]

    if not dbmod.is_paused(con):
        for _ in range(max(0, int(max_tickets))):
            out = worker.process_once(cfg, con)
            s["outcomes"].append(out)
            if out == "idle":
                break
            s["idle"] = False
            if out.startswith("pr_open"):
                s["alerts"].append(f"PR ready: {out.split(' ', 1)[-1]}")

    s["spend_today"] = round(dbmod.spend_today(con), 4)
    if s["alerts"]:
        notify.post(cfg, "FACTORY", "\n".join(s["alerts"]))
    return s
