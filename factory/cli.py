"""Command-line control plane for the factory."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import agent as agentmod
from . import blast as blastmod
from . import db as dbmod
from . import gates, worker, worktree as wt


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
                           max_retries=args.retries)
    if args.blast:
        dbmod.set_blast(con, tid, args.blast, "set manually")
    print(f"queued ticket {tid}")


def cmd_list(args):
    _, con = _open(args)
    rows = dbmod.list_tickets(con, status=args.status)
    if not rows:
        print("(empty)")
        return
    print(f"{'id':>4} {'status':<12} {'att':>3} {'spent':>7} {'blast':<6} "
          f"{'ref':<10} title")
    for r in rows:
        print(f"{r['id']:>4} {r['status']:<12} {r['attempts']:>3} "
              f"${r['spent_usd']:>6.2f} {(r['blast'] or '-'):<6} "
              f"{(r['ref'] or '-'):<10} {r['title'][:60]}")


def cmd_status(args):
    cfg, con = _open(args)
    paused = dbmod.is_paused(con)
    counts = {r["status"]: r["c"] for r in con.execute(
        "SELECT status, COUNT(*) c FROM tickets GROUP BY status")}
    print(f"queue      : {'PAUSED (kill switch engaged)' if paused else 'running'}")
    print(f"today spend: ${dbmod.spend_today(con):.2f} of "
          f"${float(cfg.get('daily_budget_usd', 20)):.2f}")
    print(f"tickets    : {json.dumps(counts) if counts else '(none)'}")


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
        wt.repo_root(target)          # fail loudly: "low" from a non-repo is a lie
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


def main(argv=None):
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
    s.set_defaults(fn=cmd_add)

    s = a.add_parser("list"); s.add_argument("--status"); s.set_defaults(fn=cmd_list)
    a.add_parser("status").set_defaults(fn=cmd_status)
    a.add_parser("pause").set_defaults(fn=cmd_pause)
    a.add_parser("resume").set_defaults(fn=cmd_resume)

    s = a.add_parser("once"); s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_once)

    s = a.add_parser("worker"); s.add_argument("--dry", action="store_true")
    s.add_argument("--idle", type=int, default=15); s.set_defaults(fn=cmd_worker)

    s = a.add_parser("watchdog"); s.add_argument("--older-than", type=int, default=3600)
    s.add_argument("--kill", action="store_true"); s.set_defaults(fn=cmd_watchdog)

    s = a.add_parser("gate"); s.add_argument("--path"); s.set_defaults(fn=cmd_gate)
    s = a.add_parser("blast"); s.add_argument("--path"); s.set_defaults(fn=cmd_blast)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
