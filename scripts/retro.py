"""Factory retro: one markdown digest of what the factory did and who it waits on.

Two questions, answered from the board DBs alone (no network, no gh):
what happened in the window, and what is waiting on a human right now.
Cost comes from the runs table, failure classes from run details, the
human queue from ticket status plus open escalations.

Usage:
    python scripts/retro.py --db dashboard.db [--db petro.db ...] [--days 7]
"""

import argparse
import json
import sqlite3
import sys
import time

HUMAN_PENDING = ("pr_open", "escalated", "needs_info")
FAILURE_STATUSES = ("fail", "escalated")


def classify(detail):
    """Collapse a run detail to a stable failure class. Raw details carry
    tracebacks, token counts, and JSON payloads that differ per run; the
    retro wants the shape, not the incident."""
    text = (detail or "").strip()
    if not text:
        return "(no detail)"
    first = text.splitlines()[0].strip()
    if first.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if payload and "status" in payload:
            return "agent stopped: %s" % payload["status"]
    if text.startswith("tests do not constrain"):
        return "tests do not constrain the new code - mutation gate"
    if text.startswith("tests failing"):
        return "tests failing (see run detail)"
    return first[:100]


def cutoff(days):
    """Runs store started_at as epoch seconds; the window is computed here
    so the comparison never mixes numeric and text affinity."""
    return time.time() - days * 86400


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def status_counts(con):
    rows = con.execute(
        "select status, count(*) as n from tickets group by status"
        " order by status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def spend_by_tier(con, days):
    rows = con.execute(
        "select tier, round(sum(cost_usd), 4) as usd, count(*) as runs"
        " from runs where started_at >= ?"
        " group by tier order by tier", (cutoff(days),)).fetchall()
    return [(r["tier"], r["usd"] or 0.0, r["runs"]) for r in rows]


def failure_classes(con, days):
    marks = ",".join("?" * len(FAILURE_STATUSES))
    rows = con.execute(
        "select detail, ticket_id from runs r where r.status in (%s)"
        " and r.started_at >= ?" % marks,
        FAILURE_STATUSES + (cutoff(days),)).fetchall()
    classes = {}
    for row in rows:
        entry = classes.setdefault(classify(row["detail"]), [0, set()])
        entry[0] += 1
        entry[1].add(row["ticket_id"])
    ranked = [(key, n, len(tickets))
              for key, (n, tickets) in classes.items()]
    return sorted(ranked, key=lambda c: (-c[1], c[0]))


def human_queue(con):
    marks = ",".join("?" * len(HUMAN_PENDING))
    tickets = con.execute(
        "select id, title, status, pr_url from tickets"
        " where status in (%s) order by id" % marks,
        HUMAN_PENDING).fetchall()
    queue = [dict(t) for t in tickets]
    for item in queue:
        esc = con.execute(
            "select why from escalations where ticket_id = ?"
            " order by id desc limit 1", (item["id"],)).fetchone()
        item["why"] = classify(esc["why"]) if esc else ""
    return queue


def render_markdown(name, con, days):
    lines = ["## %s (last %d days)" % (name, days), ""]
    counts = status_counts(con)
    total = sum(counts.values())
    lines.append("- tickets: %d total — %s" % (
        total, ", ".join("%s %d" % (s, n) for s, n in counts.items())
        or "none"))
    spend = spend_by_tier(con, days)
    if spend:
        lines.append("- spend: %s" % ", ".join(
            "%s $%.2f (%d runs)" % (t, u, n) for t, u, n in spend))
    else:
        lines.append("- spend: nothing spent in the window")
    failures = failure_classes(con, days)
    if failures:
        lines.append("- failure classes:")
        for detail, n, tickets in failures:
            lines.append("  - %s — %d run(s) across %d ticket(s)"
                         % (detail[:120], n, tickets))
    else:
        lines.append("- no failed runs in the window")
    queue = human_queue(con)
    if queue:
        lines.append("- waiting on a human:")
        for item in queue:
            tail = item["pr_url"] or item["why"]
            lines.append("  - #%s [%s] %s%s" % (
                item["id"], item["status"], item["title"],
                " — " + tail if tail else ""))
    else:
        lines.append("- nothing waiting on a human")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Factory retro digest from one or more board DBs")
    parser.add_argument("--db", action="append", required=True,
                        help="board DB to include; repeat for each customer")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)
    print("# Factory retro — %s" % _today())
    print("")
    for db_path in args.db:
        con = connect(db_path)
        print(render_markdown(db_path, con, args.days))
    return 0


def _today():
    import datetime
    return datetime.date.today().isoformat()


if __name__ == "__main__":
    sys.exit(main())
