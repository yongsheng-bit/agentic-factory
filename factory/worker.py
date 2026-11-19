"""The orchestrator loop.

Order of checks is the whole design:
  kill switch -> daily budget -> claim -> per-ticket budget -> work -> gates.
Budgets are checked BEFORE an agent starts, because money already spent cannot
be unspent. Gates run on the produced diff, never on the agent's claims about it.
"""

from __future__ import annotations

import os
import time

from . import agent as agentmod
from . import blast as blastmod
from . import db as dbmod
from . import gates, mutate, notify, refine as refinemod, review as reviewmod, worktree as wt

try:
    import msvcrt
except ImportError:
    msvcrt = None
try:
    import fcntl
except ImportError:
    fcntl = None

LOCK_PATH = os.environ.get(
    "FACTORY_LOCK_PATH", os.path.join(os.path.expanduser("~"),
                                      ".agentic-factory.lock"))


def _engine_lock(path=None):
    """Take the one-build-at-a-time lock, or return None if it is held.

    The line has a single inference engine, so two concurrent builds are not
    throughput - they halve each other's tokens-per-second until the wall-clock
    cap kills both. Overlapping scheduler ticks make that concurrency easy to
    reach, so the guard lives in the worker, below every entry point.
    """
    fh = open(path or LOCK_PATH, "a+")
    try:
        if msvcrt is not None:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        return None


def _release_engine_lock(fh):
    try:
        fh.seek(0)
        if msvcrt is not None:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    fh.close()


def process_once(cfg, con, dry=False):
    """Handle at most one ticket under the engine lock. Returns an outcome."""
    lock = _engine_lock()
    if lock is None:
        return "busy"
    try:
        return _process_once(cfg, con, dry=dry)
    finally:
        _release_engine_lock(lock)


def _escalate(con, ticket, cfg, why):
    dbmod.finish(con, ticket["id"], "escalated", detail=why)
    dbmod.add_escalation(con, ticket["id"], why)
    notify.post(cfg, "ESCALATION", f"ticket {ticket['id']}: {why}")
    return "escalated"


def _process_once(cfg, con, dry=False):
    """Handle at most one ticket. Returns a one-word outcome."""
    if dbmod.is_paused(con):
        return "paused"

    daily = float(cfg.get("daily_budget_usd", 20.0))
    spent_today = dbmod.spend_today(con)
    if spent_today >= daily:
        dbmod.set_paused(con, True)
        notify.post(cfg, "DAILY BUDGET EXHAUSTED",
                    f"${spent_today:.2f} >= ${daily:.2f}. Queue paused; run "
                    f"`python -m factory.cli resume` to continue.")
        return "budget"

    ticket = dbmod.claim_next(con)
    if ticket is None:
        return "idle"

    if dbmod.ticket_spend(con, ticket["id"]) >= float(ticket["budget_usd"]):
        dbmod.finish(con, ticket["id"], "rejected")
        notify.post(cfg, "BUDGET",
                    f"ticket {ticket['id']} budget ${ticket['budget_usd']:.2f} exhausted")
        return "rejected"

    repo = cfg["repo"]
    tier, tier_reason = agentmod.tier_decision(cfg, ticket)
    spec = cfg["agent"][tier]
    attempt = int(ticket["attempts"]) + 1

    if attempt == 1 and (cfg.get("refine") or {}).get("enabled"):
        problems = refinemod.check(cfg, con, ticket)
        if problems:
            why = "; ".join(problems)
            dbmod.finish(con, ticket["id"], "needs_info")
            dbmod.add_escalation(con, ticket["id"], "refine: " + why)
            notify.post(cfg, "NEEDS INFO", f"ticket {ticket['id']}: {why}")
            return "needs_info: " + why[:120]

    path = branch = None
    keep_branch = False
    try:
        path, branch = wt.create(repo, ticket["id"], ticket["ref"],
                                 base=cfg.get("base_branch", "HEAD"))
    except Exception as exc:
        return _escalate(con, ticket, cfg, f"worktree create failed: {exc}")

    try:
        prev = dbmod.last_failure(con, ticket["id"])
        prompt_path = agentmod.write_prompt(path, ticket, attempt, extra=prev)

        if dry:
            dbmod.finish(con, ticket["id"], "queued")
            dbmod.bump_attempt(con, ticket["id"])
            return f"dry-run (tier={tier}, worktree={path})"

        run_id = dbmod.start_run(con, ticket["id"], attempt,
                                 tier=tier, tier_reason=tier_reason)
        res = agentmod.run(cfg, spec, ticket["title"], path, prompt_path=prompt_path)
        dbmod.end_run(con, run_id, res["status"], res.get("cost_usd", 0.0),
                      (res.get("stderr") or res.get("stdout") or "")[:2000])
        print(f"[ticket {ticket['id']}] tier={tier} ({tier_reason}) attempt={attempt} "
              f"status={res['status']} cost=${res.get('cost_usd', 0.0):.4f} "
              f"{res['seconds']:.0f}s", flush=True)

        if res["status"] == "error":
            dbmod.finish(con, ticket["id"], "queued")
            return "deferred (engine unreachable)"

        if res["status"] != "ok":
            dbmod.bump_attempt(con, ticket["id"])
            if dbmod.attempt_of(con, ticket["id"]) >= int(ticket["max_retries"]):
                return _escalate(con, ticket, cfg,
                                 f"{res['status']} after {ticket['max_retries']} retries: "
                                 f"{(res.get('stderr') or '')[:300]}")
            dbmod.finish(con, ticket["id"], "queued")
            return f"retry {dbmod.attempt_of(con, ticket['id'])}/{ticket['max_retries']}"


        base_sha = wt.head_sha(path)
        if not wt.commit_all(path, f"{ticket['ref'] or 'ticket ' + str(ticket['id'])}: {ticket['title']}"):
            dbmod.bump_attempt(con, ticket["id"])
            if dbmod.attempt_of(con, ticket["id"]) >= int(ticket["max_retries"]):
                return _escalate(con, ticket, cfg, "agent produced no changes")
            dbmod.finish(con, ticket["id"], "queued")
            return "retry (empty diff)"
        keep_branch = True


        diff_text = wt.diff_text(path, base=base_sha)

        junk = None
        if "Binary files" in diff_text:
            junk = "diff contains binary files"
        elif ("test" in (ticket["title"] + " " + (ticket["body"] or "")).lower()
              and "tests/" not in diff_text):
            junk = "ticket asks for tests but diff touches no test files"
        if junk:
            dbmod.bump_attempt(con, ticket["id"])
            if dbmod.attempt_of(con, ticket["id"]) >= int(ticket["max_retries"]):
                return _escalate(con, ticket, cfg, junk)
            dbmod.finish(con, ticket["id"], "queued")
            return f"retry ({junk})"

        gate = gates.check(path, diff=diff_text)
        if not gate["ok"]:
            f = gate["secrets"][0]

            keep_branch = False
            return _escalate(con, ticket, cfg,
                             f"SECRET FOUND {f.get('file')}:{f.get('line')} "
                             f"({f.get('reason')}) - PR blocked")

        test_cmd = (cfg.get("gates") or {}).get("test_command")
        if test_cmd:
            ok, out = gates.run_tests(
                path, test_cmd,
                env=agentmod.minimal_env(
                    env_extra=(cfg.get("agent") or {}).get("env")))
            if not ok:
                why = "tests failing:\n" + out[-1500:]
                dbmod.bump_attempt(con, ticket["id"])
                if dbmod.attempt_of(con, ticket["id"]) >= int(ticket["max_retries"]):
                    return _escalate(con, ticket, cfg, why)
                dbmod.log_failure(con, ticket["id"], why)
                dbmod.finish(con, ticket["id"], "queued")
                return "retry (tests failing)"

        mut_report = ""
        mut_cfg = (cfg.get("gates") or {}).get("mutation") or {}
        if test_cmd and mut_cfg.get("enabled"):
            mut_ok, mut_report = mutate.run_mutations(
                path, diff_text, test_cmd,
                env=agentmod.minimal_env(
                    env_extra=(cfg.get("agent") or {}).get("env")),
                max_mutants=int(mut_cfg.get("max_mutants", 12)),
                min_score=float(mut_cfg.get("min_score", 0.5)))
            if not mut_ok:
                dbmod.bump_attempt(con, ticket["id"])
                if dbmod.attempt_of(con, ticket["id"]) >= int(ticket["max_retries"]):
                    return _escalate(con, ticket, cfg, mut_report)
                dbmod.log_failure(con, ticket["id"], mut_report)
                dbmod.finish(con, ticket["id"], "queued")
                return "retry (tests do not constrain the code)"


        nfiles, added, deleted = wt.diff_stat(path, base=base_sha)
        if nfiles == 0 or not diff_text.strip():

            return _escalate(con, ticket, cfg,
                             "cannot classify diff (git returned no changes after commit)")
        level, reason = blastmod.classify(
            wt.changed_files(path, base=base_sha),
            added, deleted, diff_text,
            cfg=cfg.get("blast"))
        dbmod.set_blast(con, ticket["id"], level, reason, branch=branch)

        body = (f"{ticket['body']}\n\n---\n"
                f"blast radius: **{level}** ({reason})\n"
                f"files: {nfiles}  +{added} -{deleted}\n"
                f"tier: {tier}  attempt: {attempt}  "
                f"cost: ${dbmod.ticket_spend(con, ticket['id']):.4f}\n"
                f"gate: {'; '.join(gate['tools'])} clean\n")
        if mut_report:
            body += "\n" + mut_report + "\n"
        if ticket["issue_number"]:
            body += f"\nCloses #{ticket['issue_number']}\n"

        verdict, notes = reviewmod.review_diff(cfg, ticket, diff_text)
        if verdict:
            body += f"\nAI review: **{verdict}** — {notes}\n"
        else:
            body += f"\nAI review: unavailable ({notes})\n"
        print(f"ai review: {verdict or notes}", flush=True)

        pr_url = None
        if cfg.get("push", True):
            try:
                wt.push_branch(path, branch, cfg.get("remote", "origin"),
                               token=cfg.get("gh_token"), attempt=attempt)
                pr_url = wt.open_pr(path, branch, f"{ticket['ref'] or ''} {ticket['title']}".strip(),
                                    body, base=cfg.get("pr_base", "main"),
                                    gh_path=cfg.get("gh_path"),
                                    token=cfg.get("gh_token"))
            except Exception as exc:

                return _escalate(con, ticket, cfg, f"push/PR failed: {exc}")
            dbmod.set_pr(con, ticket["id"], pr_url)

        if level == "high":
            dbmod.finish(con, ticket["id"], "needs_review")
            notify.post(cfg, f"HUMAN GATE: {ticket['ref'] or ticket['id']}",
                        f"high blast radius ({reason}). Review {pr_url or branch}")
            return "needs_review"

        dbmod.finish(con, ticket["id"], "pr_open")
        return f"pr_open {pr_url or '(push disabled - branch ' + branch + ' is local only)'}"

    except Exception as exc:
        return _escalate(con, ticket, cfg, f"worker error: {exc}")
    finally:
        if path and not cfg.get("keep_worktrees", False):
            try:

                wt.remove(repo, path, branch=None if keep_branch else branch)
            except Exception as exc:

                print(f"WARNING: could not clean worktree {path}: {exc}")


def loop(cfg, con, dry=False, idle_sleep=15):
    print("factory worker running (Ctrl-C to stop)", flush=True)
    while True:
        try:
            out = process_once(cfg, con, dry=dry)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[worker] unexpected: {exc}", flush=True)
            out = "error"
        if out == "idle":
            time.sleep(idle_sleep)
        elif out == "paused":
            time.sleep(max(idle_sleep, 5))


def watchdog(cfg, con, max_age_s=3600, kill=False):
    """The kill switch that actually reaches running agents.

    The queue-level pause only stops NEW work. This reaps runs whose worker
    died or hung, and (with kill=True) kills their process trees.
    """
    n = dbmod.mark_stale_running_failed(con, older_than=max_age_s)
    if n:
        notify.post(cfg, "WATCHDOG", f"reaped {n} stale run(s); tickets escalated")
    if kill:
        for pid, ref in dbmod.running_pids(con):
            if not _alive(pid):
                continue
            print(f"[watchdog] killing agent pid {pid} ({ref})", flush=True)
            agentmod._kill_tree(_Proc(pid))
    return n


def _alive(pid):
    import os
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


class _Proc:
    """Minimal shim so agent._kill_tree can tree-kill a pid we did not spawn."""
    def __init__(self, pid):
        self.pid = int(pid)
