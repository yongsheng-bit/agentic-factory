"""Command-line control plane for the factory."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import agent as agentmod
from . import blast as blastmod
from . import db as dbmod
from . import gates, sync as syncmod, worker, worktree as wt


def _open(args):
    cfg = agentmod.load_config(args.config)
    con = dbmod.connect(cfg["db"])
    dbmod.init(con)
    return cfg, con


def cmd_init(args):
    cfg = agentmod.load_config(args.config)
    con = dbmod.connect(cfg["db"])
    dbmod.init(con)
    print(f"initialised {cfg['db']}")


def cmd_add(args):
    _, con = _open(args)
    tid = dbmod.add_ticket(con, args.title, args.body or "", source=args.source,
                           ref=args.ref, budget_usd=args.budget,
                           max_retries=args.retries, tier=args.tier)
    if args.blast:
        dbmod.set_blast(con, tid, args.blast, "set manually")
    route = f" (pinned to {args.tier} tier)" if args.tier else ""
    print(f"queued ticket {tid}{route}")


def cmd_list(args):
    _, con = _open(args)
    rows = dbmod.list_tickets(con, status=args.status)
    if not rows:
        print("(empty)")
        return
    print(f"{'id':>4} {'status':<12} {'att':>3} {'spent':>7} {'blast':<6} "
          f"{'tier':<7} {'ref':<10} title")
    for r in rows:
        print(f"{r['id']:>4} {r['status']:<12} {r['attempts']:>3} "
              f"${r['spent_usd']:>6.2f} {(r['blast'] or '-'):<6} "
              f"{(r['tier'] or 'auto'):<7} "
              f"{(r['ref'] or '-'):<10} {r['title'][:60]}")


def cmd_show(args):
    """One ticket, everything the queue knows about it, no SQL required."""
    _, con = _open(args)
    t = dbmod.get_ticket(con, args.id)
    if t is None:
        print(f"no such ticket {args.id}")
        sys.exit(1)
    run = con.execute(
        "SELECT tier, tier_reason FROM runs WHERE ticket_id=?"
        " ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
    tier = (run["tier"] if run else None) or "-"
    why = (run["tier_reason"] if run else None) or "-"
    print(f"ticket     : {t['id']} {t['title']}")
    print(f"status     : {t['status']}")
    print(f"attempts   : {t['attempts']}/{t['max_retries']}")
    print(f"spent      : ${float(t['spent_usd']):.2f} of ${float(t['budget_usd']):.2f}")
    print(f"blast      : {(t['blast'] or '-')} ({t['blast_reason'] or '-'})")
    print(f"tier       : {tier} ({why})")
    print(f"pr         : {t['pr_url'] or '-'}")
    print(f"last error : {dbmod.last_failure(con, t['id']) or '-'}")


def cmd_runs(args):
    """Run history with the routing decision, so a tier can be audited later."""
    _, con = _open(args)
    rows = dbmod.list_runs(con, ticket_id=args.ticket, limit=args.limit)
    if not rows:
        print("(no runs)")
        return
    print(f"{'id':>4} {'ticket':>6} {'att':>3} {'status':<9} {'tier':<7} "
          f"{'tier_reason':<24} {'cost':>6} started")
    for r in rows:
        started = time.strftime("%Y-%m-%d %H:%M:%S",
                                time.localtime(float(r["started_at"])))
        cost = f"${float(r['cost_usd']):.2f}"
        print(f"{r['id']:>4} {r['ticket_id']:>6} {r['attempt']:>3} "
              f"{r['status']:<9} {(r['tier'] or '-'):<7} "
              f"{(r['tier_reason'] or '-'):<24} {cost:>6} {started}")


def cmd_retry(args):
    """Requeue a finished ticket without touching its attempt count."""
    _, con = _open(args)
    ok, msg = dbmod.retry_ticket(con, args.id)
    print(msg)
    sys.exit(0 if ok else 1)


def cmd_status(args):
    cfg, con = _open(args)
    paused = dbmod.is_paused(con)
    counts = {r["status"]: r["c"] for r in con.execute(
        "SELECT status, COUNT(*) c FROM tickets GROUP BY status")}
    print(f"queue      : {'PAUSED (kill switch engaged)' if paused else 'running'}")
    print(f"today spend: ${dbmod.spend_today(con):.2f} of "
          f"${float(cfg.get('daily_budget_usd', 20)):.2f}")
    print(f"tickets    : {json.dumps(counts) if counts else '(none)'}")

    mix = dbmod.tier_mix(con)
    print(f"tiers      : {mix['cheap']} cheap / {mix['strong']} strong "
          f"({mix['escalated']} escalated after failure)")

    done = con.execute("SELECT COUNT(*) c FROM tickets WHERE status='merged'").fetchone()["c"]
    unreviewed = con.execute(
        "SELECT COUNT(*) c FROM tickets WHERE status='merged' "
        "AND (review_state IS NULL OR review_state <> 'approved')").fetchone()["c"]
    print(f"shipped    : {done} merged ({unreviewed} without an approving review)")


def cmd_pause(args):
    _, con = _open(args)
    dbmod.set_paused(con, True)
    print("PAUSED - no new tickets will start; running agents finish their step")


def cmd_resume(args):
    _, con = _open(args)
    dbmod.set_paused(con, False)
    print("resumed")


def cmd_once(args):
    cfg, con = _open(args)
    print(worker.process_once(cfg, con, dry=args.dry))


def cmd_worker(args):
    cfg, con = _open(args)
    worker.loop(cfg, con, dry=args.dry, idle_sleep=args.idle)


def cmd_watchdog(args):
    cfg, con = _open(args)
    n = worker.watchdog(cfg, con, max_age_s=args.older_than, kill=args.kill)
    print(f"reaped {n} stale run(s)")


def cmd_sync(args):
    cfg, con = _open(args)
    changes, errors = syncmod.sync(cfg, con, gh_path=args.gh)
    for tid, old, new, review, by, url in changes:
        how = ""
        if new == "merged":

            how = (f" by {by}" if by else "") + f" [{review}]"
        print(f"ticket {tid}: {old} -> {new}{how}  {url}")
    for tid, msg in errors:
        print(f"ticket {tid}: NOT updated - {msg}")
    if not changes and not errors:
        print("nothing to update")
    sys.exit(1 if errors and not changes else 0)


def cmd_gate(args):
    cfg, _ = _open(args)
    root = args.path or cfg["repo"]
    res = gates.check(root)
    print(json.dumps(res, indent=2)[:4000])
    sys.exit(0 if res["ok"] else 1)


def cmd_blast(args):
    cfg, _ = _open(args)
    target = args.path or cfg["repo"]
    try:
        wt.repo_root(target)
    except Exception as exc:
        print(f"not a git repo ({target}): {exc}")
        sys.exit(3)
    files = wt.changed_files(target)
    n, a, d = wt.diff_stat(args.path or cfg["repo"])
    level, reason = blastmod.classify(files, a, d,
                                      wt.diff_text(args.path or cfg["repo"]),
                                      cfg=cfg.get("blast"))
    print(f"{level}: {reason} ({n} files, +{a} -{d})")
    sys.exit(0 if level != "high" else 2)


def cmd_tier(args):
    """Pin a ticket's tier, or clear the pin with `auto`."""
    cfg, con = _open(args)
    t = dbmod.get_ticket(con, args.id)
    if t is None:
        print(f"no such ticket {args.id}")
        sys.exit(1)
    tier = None if args.tier == "auto" else args.tier
    dbmod.set_tier(con, t["id"], tier)
    t = dbmod.get_ticket(con, t["id"])
    decided, why = agentmod.tier_decision(cfg, t)
    now = f"pinned to {tier}" if tier else "router decides"
    print(f"ticket {t['id']}: {now}")
    print(f"would route: {decided} ({why})")


def build_parser():
    """Construct the whole CLI, subcommands included.

    Separate from main() so a test can build it. Building is not free: argparse
    %-formats every help string, so a literal `%` (a Windows env var, a
    percentage) raises at construction and takes the entire CLI down - every
    command, including the ones whose help text was fine.
    """
    p = argparse.ArgumentParser(prog="factory", description="Agentic software factory")
    p.add_argument("-c", "--config", default=os.environ.get("FACTORY_CONFIG", "factory.json"))
    a = p.add_subparsers(dest="cmd", required=True)

    a.add_parser("init").set_defaults(fn=cmd_init)

    s = a.add_parser("add"); s.add_argument("title")
    s.add_argument("--body", default=""); s.add_argument("--ref")
    s.add_argument("--source", default="manual")
    s.add_argument("--budget", type=float, default=2.0)
    s.add_argument("--retries", type=int, default=3)
    s.add_argument("--blast", choices=["low", "medium", "high"])
    s.add_argument("--tier", choices=["cheap", "strong"],
                   help="pin the model tier instead of letting the router decide")
    s.set_defaults(fn=cmd_add)

    s = a.add_parser("list"); s.add_argument("--status"); s.set_defaults(fn=cmd_list)
    s = a.add_parser("show"); s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)
    s = a.add_parser("runs"); s.add_argument("--ticket", type=int,
                                             help="only runs for this ticket id")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_runs)

    s = a.add_parser("retry"); s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_retry)

    s = a.add_parser("tier"); s.add_argument("id", type=int)
    s.add_argument("tier", choices=["cheap", "strong", "auto"],
                   help="pin the model tier, or auto to let the router decide")
    s.set_defaults(fn=cmd_tier)
    a.add_parser("status").set_defaults(fn=cmd_status)
    a.add_parser("pause").set_defaults(fn=cmd_pause)
    a.add_parser("resume").set_defaults(fn=cmd_resume)

    s = a.add_parser("once"); s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_once)

    s = a.add_parser("worker"); s.add_argument("--dry", action="store_true")
    s.add_argument("--idle", type=int, default=15); s.set_defaults(fn=cmd_worker)

    s = a.add_parser("watchdog"); s.add_argument("--older-than", type=int, default=3600)
    s.add_argument("--kill", action="store_true"); s.set_defaults(fn=cmd_watchdog)

    s = a.add_parser("sync")
    s.add_argument("--gh", help="path to the gh CLI; default is config gh_path, "
                                "then PATH, then the per-user install")
    s.set_defaults(fn=cmd_sync)

    s = a.add_parser("gate"); s.add_argument("--path"); s.set_defaults(fn=cmd_gate)
    s = a.add_parser("blast"); s.add_argument("--path"); s.set_defaults(fn=cmd_blast)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
