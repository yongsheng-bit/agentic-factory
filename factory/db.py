"""Queue, run history and spend ledger. Stdlib sqlite3 only.

Design notes
------------
* Claims are atomic: a single UPDATE with a status guard, so two workers can
  never pick up the same ticket.
* Spend is recorded per run and summed for the daily cap. The cap is enforced
  by the worker BEFORE starting an agent, never after.
* `control` is a one-row table used by the kill switch. The worker re-reads it
  between every step, so pausing takes effect within seconds, not minutes.
"""

from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL DEFAULT 'manual',
    ref          TEXT,
    title        TEXT    NOT NULL,
    body         TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'queued',  -- queued|running|done|escalated|paused|rejected
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_retries  INTEGER NOT NULL DEFAULT 3,
    budget_usd   REAL    NOT NULL DEFAULT 2.00,
    spent_usd    REAL    NOT NULL DEFAULT 0.0,
    blast        TEXT,
    blast_reason TEXT,
    tier         TEXT,
    branch       TEXT,
    pr_url       TEXT,
    review_state TEXT,   -- approved | admin-bypass | unreviewed (set by sync)
    merged_by    TEXT,
    merged_at    REAL,
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    attempt   INTEGER NOT NULL,
    status    TEXT    NOT NULL,          -- ok|fail|blocked|timeout|skipped
    cost_usd  REAL    NOT NULL DEFAULT 0.0,
    detail    TEXT    NOT NULL DEFAULT '',
    tier         TEXT,
    tier_reason  TEXT,
    pid       INTEGER,
    started_at REAL   NOT NULL,
    ended_at   REAL
);

CREATE TABLE IF NOT EXISTS control (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    paused INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS escalations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    why        TEXT    NOT NULL,
    created_at REAL    NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)

    have = {r["name"] for r in con.execute("PRAGMA table_info(tickets)")}
    for col, coltype in (("pr_url", "TEXT"),
                         ("review_state", "TEXT"),
                         ("merged_by", "TEXT"),
                         ("merged_at", "REAL"),
                         ("tier", "TEXT"),
                         ("issue_number", "INTEGER"),
                         ("issue_node", "TEXT"),
                         ("project_item", "TEXT"),
                         ("issue_state", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE tickets ADD COLUMN {col} {coltype}")

    have_runs = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
    for col, coltype in (("tier", "TEXT"), ("tier_reason", "TEXT")):
        if col not in have_runs:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {coltype}")
    con.execute("INSERT OR IGNORE INTO control (id, paused) VALUES (1, 0)")



def add_ticket(con, title, body="", source="manual", ref=None,
               budget_usd=2.00, max_retries=3, tier=None, issue_number=None):
    now = time.time()
    cur = con.execute(
        "INSERT INTO tickets (source, ref, title, body, budget_usd, max_retries,"
        " tier, issue_number, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source, ref, title, body, float(budget_usd), int(max_retries), tier,
         issue_number, now, now),
    )
    return int(cur.lastrowid)


def get_ticket(con, ticket_id):
    return con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()


def list_tickets(con, status=None, limit=50):
    if status:
        rows = con.execute(
            "SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT ?",
                           (limit,)).fetchall()
    return rows


def list_runs(con, ticket_id=None, limit=20):
    """Recent run history, newest first. `ticket_id` narrows it to one ticket."""
    if ticket_id is not None:
        return con.execute(
            "SELECT * FROM runs WHERE ticket_id=? ORDER BY id DESC LIMIT ?",
            (ticket_id, limit)).fetchall()
    return con.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def claim_next(con, worker_id="w0"):
    """Atomically move the oldest queued ticket to running. Returns row or None."""
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT * FROM tickets WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            con.execute("COMMIT")
            return None
        con.execute(
            "UPDATE tickets SET status='running', updated_at=? WHERE id=? AND status='queued'",
            (time.time(), row["id"]))
        if con.execute("SELECT changes()").fetchone()[0] != 1:
            con.execute("COMMIT")
            return None
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return get_ticket(con, row["id"])


def set_blast(con, ticket_id, level, reason, branch=None):
    con.execute(
        "UPDATE tickets SET blast=?, blast_reason=?, branch=COALESCE(?, branch),"
        " updated_at=? WHERE id=?",
        (level, reason, branch, time.time(), ticket_id))


def set_tier(con, ticket_id, tier):
    """Pin the model tier, or pass None to hand the decision back to the router."""
    con.execute("UPDATE tickets SET tier=?, updated_at=? WHERE id=?",
                (tier, time.time(), ticket_id))


def set_pr(con, ticket_id, url):
    """Record the PR link. The dashboard's `pr_open` status is worthless
    without something a human can click."""
    con.execute("UPDATE tickets SET pr_url=?, updated_at=? WHERE id=?",
                (url, time.time(), ticket_id))


def finish(con, ticket_id, status, detail=""):
    con.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                (status, time.time(), ticket_id))
    return detail


def log_failure(con, ticket_id, detail, tier_reason="gate"):
    """Record a gate failure as a fail run row so the retry prompt sees it.

    last_failure() reads run details; a failure that never becomes a row is
    invisible to the next attempt - the agent would repeat the mistake blind.
    """
    r = con.execute("SELECT attempts FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    con.execute(
        "INSERT INTO runs (ticket_id, attempt, status, cost_usd, detail, tier,"
        " tier_reason, started_at, ended_at)"
        " VALUES (?, ?, 'fail', 0, ?, 'gate', ?, strftime('%s','now'), strftime('%s','now'))",
        (ticket_id, r["attempts"] if r else 0, detail[:4000], tier_reason))


def add_escalation(con, ticket_id, why):
    """Persist why a ticket left the queue for a human.

    An escalation that only lives in a log line cannot be audited later:
    the queue must answer 'what did it give up on, and why' from the DB.
    """
    con.execute("INSERT INTO escalations (ticket_id, why, created_at) VALUES (?,?,?)",
                (ticket_id, why, time.time()))


RETRYABLE = ("failed", "escalated", "closed")
RETRY_REFUSED = ("queued", "running", "pr_open", "needs_review", "merged")


def retry_ticket(con, ticket_id):
    """Requeue a finished ticket. Returns (ok, message).

    The guard lives in the UPDATE, not in a read-then-write, so a ticket that a
    worker claimed between the check and the write cannot be yanked out from
    under it - that is how two workers end up on one worktree. Attempts are
    deliberately left alone: the tier router escalates on the attempt count, so
    resetting it would send a known-hard ticket back to the cheap model.
    """
    t = get_ticket(con, ticket_id)
    if t is None:
        return False, f"no such ticket {ticket_id}"
    status = t["status"]
    if status not in RETRYABLE:
        if status in RETRY_REFUSED:
            why = f"{status} is still in flight or already shipped"
        else:
            why = f"{status} is not retryable"
        return False, (f"cannot retry ticket {ticket_id}: {why} "
                       f"(retryable: {', '.join(RETRYABLE)})")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE tickets SET status='queued', updated_at=?"
                    " WHERE id=? AND status=?", (time.time(), ticket_id, status))
        moved = int(con.execute("SELECT changes()").fetchone()[0])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    if moved != 1:
        now = get_ticket(con, ticket_id)
        return False, (f"cannot retry ticket {ticket_id}: status changed to "
                       f"{now['status'] if now else 'unknown'} under us")
    return True, (f"requeued ticket {ticket_id} (was {status}, "
                  f"attempt {t['attempts']} kept)")


def attempt_of(con, ticket_id):
    r = con.execute("SELECT attempts FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return int(r["attempts"]) if r else 0


def bump_attempt(con, ticket_id):
    con.execute("UPDATE tickets SET attempts=attempts+1, updated_at=? WHERE id=?",
                (time.time(), ticket_id))
    return attempt_of(con, ticket_id)



def start_run(con, ticket_id, attempt, pid=None, tier=None, tier_reason=None):
    cur = con.execute(
        "INSERT INTO runs (ticket_id, attempt, status, tier, tier_reason, started_at, pid)"
        " VALUES (?,?,?,?,?,?,?)",
        (ticket_id, attempt, "running", tier, tier_reason, time.time(), pid))
    return int(cur.lastrowid)


def end_run(con, run_id, status, cost_usd=0.0, detail=""):
    con.execute(
        "UPDATE runs SET status=?, cost_usd=?, detail=?, ended_at=? WHERE id=?",
        (status, float(cost_usd), detail[:4000], time.time(), run_id))
    run = con.execute("SELECT ticket_id FROM runs WHERE id=?", (run_id,)).fetchone()
    if run:
        con.execute("UPDATE tickets SET spent_usd = spent_usd + ?, updated_at=? WHERE id=?",
                    (float(cost_usd), time.time(), run["ticket_id"]))


def last_failure(con, ticket_id):
    """Detail from the most recent non-ok run, for the retry prompt."""
    r = con.execute(
        "SELECT detail FROM runs WHERE ticket_id=? AND status IN ('fail','timeout','blocked')"
        " ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    return (r["detail"] if r else "")[:1500]


def ticket_spend(con, ticket_id):
    r = con.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE ticket_id=?",
                    (ticket_id,)).fetchone()
    return float(r["s"])


def spend_today(con, now=None):
    """Spend since *local* midnight, the boundary the daily cap resets on.

    A UTC boundary (`time.time() % 86400`) rolls over at 19:00 Eastern, so an
    afternoon of runaway runs would quietly get a fresh budget nine hours
    early. The offset is taken from `now` itself, so the answer never depends
    on the process timezone changing mid-day. `now` is injectable for tests.
    """
    if now is None:
        now = time.time()
    lt = time.localtime(now)
    offset = getattr(lt, "gmtoff", None)
    if offset is None:
        offset = -time.timezone if not lt.tm_isdst else -time.altzone
    midnight = (int((now + offset) // 86400) * 86400) - offset
    r = con.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE started_at>=?",
                    (midnight,)).fetchone()
    return float(r["s"])



def tier_mix(con):
    """How many runs each model tier actually did, plus how many of them were
    escalations. `tier_reason LIKE 'escalated%'` is the same string the router
    writes when a retry is promoted, so the daily view can show escalation
    without anyone remembering to ask for it."""
    r = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN tier='cheap' THEN 1 ELSE 0 END),0) cheap,"
        " COALESCE(SUM(CASE WHEN tier='strong' THEN 1 ELSE 0 END),0) strong,"
        " COALESCE(SUM(CASE WHEN tier_reason LIKE 'escalated%' THEN 1 ELSE 0 END),0)"
        " escalated FROM runs").fetchone()
    return {"cheap": int(r["cheap"]), "strong": int(r["strong"]),
            "escalated": int(r["escalated"])}


def set_paused(con, flag: bool):
    con.execute("UPDATE control SET paused=? WHERE id=1", (1 if flag else 0,))


def is_paused(con) -> bool:
    r = con.execute("SELECT paused FROM control WHERE id=1").fetchone()
    return bool(r and r["paused"])


def running_pids(con):
    rows = con.execute(
        "SELECT r.id, r.pid, t.ref FROM runs r JOIN tickets t ON t.id=r.ticket_id"
        " WHERE r.status='running' AND r.pid IS NOT NULL").fetchall()
    return [(int(r["pid"]), r["ref"]) for r in rows]


def mark_stale_running_failed(con, older_than=3600):
    """Reap runs whose worker died without closing them out."""
    cutoff = time.time() - older_than
    rows = con.execute(
        "SELECT id, ticket_id FROM runs WHERE status='running' AND started_at < ?",
        (cutoff,)).fetchall()
    for r in rows:
        end_run(con, int(r["id"]), "timeout", 0.0, "reaped by watchdog")
        con.execute("UPDATE tickets SET status='escalated', updated_at=? WHERE id=?",
                    (time.time(), int(r["ticket_id"])))
    return len(rows)
