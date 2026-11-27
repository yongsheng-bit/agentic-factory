"""Event-driven wake: a ticket entering the queue starts a build now.

The 15-minute scheduler is a wake-up call, not a gate. This module is the
event half of intake: when a ticket actually lands (a local ``add`` or
``retry``, or the watcher noticing a newly-imported ``factory``-labelled
issue), it spawns a detached tick immediately - the same entry point,
engine lock, budget governor and alert discipline the scheduler uses.
The scheduler stays as the backstop for every event that was missed.

A wake is opportunistic by design. If the engine lock is held, a live
build is already running and its post-run cycle picks the queue up; if
the queue is paused, waking it would only re-pause and alert. A failed or
skipped wake is therefore latency, never loss - which is the whole reason
this module is allowed to be this simple.

Change detection is the database, not memory: ``import_issues`` is
idempotent by issue number, so a watcher beat that imports nothing new is
quiet by proof, and beats need no state - which is what lets the
scheduler run ``watch --once`` instead of keeping a daemon alive.

Latency this removes: worst-case ticket-to-claim drops from one scheduler
period (~15 min) to seconds for locally-added tickets, and to the watch
interval (default one minute) for issues filed from a phone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from . import db as dbmod
from . import issues as issuesmod
from . import worker as workermod


def lock_busy(path=None):
    """True when a live worker holds the engine lock (probe, never wait)."""
    fh = workermod._engine_lock(path)
    if fh is None:
        return True
    workermod._release_engine_lock(fh)
    fh.close()
    return False


def should_wake(cfg, con, lock_path=None):
    """(yes, reason) - every refusal names the condition that refused."""
    if not (cfg.get("wake") or {}).get("enabled"):
        return False, "wake disabled"
    if dbmod.is_paused(con):
        return False, "queue paused"
    if dbmod.count_by_status(con, "queued") == 0:
        return False, "queue empty"
    if lock_busy(lock_path):
        return False, "engine busy (a live build picks the queue up)"
    return True, "queued work and idle engine"


def spawn_tick(cfg_path, python=None, log_dir=None, popen=subprocess.Popen):
    """Fire-and-forget a full ``factory tick`` in a detached child.

    Detached so the CLI that woke returns at once and the build survives
    its parent exiting; the child re-reads config, re-takes the engine
    lock and re-checks the budget, so a wrong wake costs a no-op tick,
    never a double build. Output appends to logs/wake.log.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = log_dir or os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    argv = [python or sys.executable, "-m", "factory.cli", "-c",
            str(cfg_path), "tick"]
    log = open(os.path.join(log_dir, "wake.log"), "a", encoding="utf-8")
    opts = {"cwd": root, "stdin": subprocess.DEVNULL, "stdout": log,
            "stderr": subprocess.STDOUT}
    if os.name == "nt":
        opts["creationflags"] = (subprocess.DETACHED_PROCESS
                                 | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        opts["start_new_session"] = True
    try:
        popen(argv, **opts)
    finally:
        log.close()
    return True


def wake(cfg, con, cfg_path, lock_path=None, log_dir=None):
    """Wake decision plus spawn; returns a one-line status for the CLI."""
    ok, reason = should_wake(cfg, con, lock_path=lock_path)
    if not ok:
        return reason
    spawn_tick(cfg_path, log_dir=log_dir)
    return "tick spawned"


def watch_once(cfg, con, cfg_path, gh_path=None, label="factory",
               lock_path=None, log_dir=None, importer=None):
    """One stateless beat: import labelled issues, wake on new tickets.

    Returns (new_issue_numbers, wake_status). A tracker failure is a
    quiet beat with the error as its status - the watcher must outlive a
    transient gh outage, and the scheduler backstops whatever it misses.
    """
    importer = importer or issuesmod.import_issues
    try:
        _, imported, _ = importer(cfg, con, gh_path=gh_path, label=label)
    except Exception as exc:
        return [], f"import failed: {str(exc)[:200]}"
    if not imported:
        return [], "quiet"
    status = wake(cfg, con, cfg_path, lock_path=lock_path, log_dir=log_dir)
    return [n for n, _ in imported], status


def watch(cfg, con, cfg_path, gh_path=None, label="factory", interval=30,
          once=False, sleep=time.sleep, beat=None):
    """Poll for newly-filed issues and wake on each.

    ``once`` is the scheduler shape: one beat, one line, no state -
    safe to launch every minute from Task Scheduler.
    """
    beat = beat or watch_once
    while True:
        new, status = beat(cfg, con, cfg_path, gh_path=gh_path, label=label)
        if new or once or status != "quiet":
            line = f"watch: {status}"
            if new:
                line += f" - new tickets from issues {new}"
            print(line, flush=True)
        if once:
            return new, status
        sleep(interval)
