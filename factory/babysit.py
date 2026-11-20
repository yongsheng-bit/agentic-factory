"""babysit-pr: every human review comment gets answered.

The loop used to end at PR-open. A comment on an open PR was a message into
a void: the ticket sat `pr_open` until the human merged or closed it, so
review feedback never flowed back into the worktree and the trust line's
"merged without review" number could only ever go down by accident.

This pass closes that seam. For every open PR it reads the conversation,
keeps the comments no factory marker claims (a human wrote them), and hands
them to the agent on the PR's own branch: fix what the comment asks, or
answer in the thread why it will not. The reply always carries the factory
marker so the next pass can tell its own voice from the human's.

Honest limits, stated in the reply itself: a babysit round re-runs the test
gate but not the mutation gate, and the reply is one summary quoting each
new comment - the cheap tier is not trusted to map fixes to comments one
for one.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from . import agent as agentmod
from . import db as dbmod
from . import gates as gatesmod
from . import notify
from . import sync as syncmod
from . import worktree as wt

AGENT_MARKER = "<!-- factory:agent -->"


class BabysitError(RuntimeError):
    pass


def is_human_comment(comment, after_id=0):
    """True for a comment this pass must answer.

    Factory comments are recognized by the marker, not by author: the agent
    and the operator share one GitHub account, so `user.login` cannot tell
    them apart. A marker is content the factory writes and the human does
    not, which is the only honest discriminator available.
    """
    try:
        cid = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        return False
    if cid <= int(after_id):
        return False
    if AGENT_MARKER in (comment.get("body") or ""):
        return False
    login = ((comment.get("user") or {}).get("login")) or ""
    if login.endswith("[bot]"):
        return False
    return True


def fetch_comments(gh, owner, repo, number, token=None):
    """Conversation comments of a PR, oldest first. Raises BabysitError."""
    proc = subprocess.run(
        [gh, "api", "--paginate",
         f"repos/{owner}/{repo}/issues/{number}/comments?per_page=100"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, env=wt._token_env(token))
    if proc.returncode != 0:
        raise BabysitError(
            f"gh api comments {owner}/{repo}#{number}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}")
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BabysitError(f"comments json for {owner}/{repo}#{number}: {exc}")
    if not isinstance(data, list):
        raise BabysitError(f"comments for {owner}/{repo}#{number}: not a list")
    return data


def write_feedback_prompt(workdir, ticket, comments, out_dir=None):
    """Prompt for one feedback round, written outside the worktree."""
    out_dir = out_dir or os.path.join(tempfile.gettempdir(),
                                      "agentic-factory-prompts")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ticket-{ticket['id']}-babysit-{int(time.time())}.md")
    rules = agentmod.repo_rules(workdir)
    thread = "\n\n".join(
        f"### comment {c.get('id')} by "
        f"{((c.get('user') or {}).get('login')) or 'unknown'}\n"
        f"{(c.get('body') or '').strip()}"
        for c in comments)
    body = (
        f"# Review feedback on {ticket['ref'] or ticket['id']}: "
        f"{ticket['title']}\n\n"
        f"The original ticket:\n\n{ticket['body']}\n\n"
        + (f"## Repo rules\n{rules}\n\n" if rules else "")
        + f"## Human review comments (newest round)\n\n{thread}\n\n"
        "## Rules\n"
        "- This is a review round on an OPEN pull request, not a new task. "
        "The branch already contains the change under review.\n"
        "- Address EVERY comment above: change the code, or decide the "
        "comment is wrong and say exactly why.\n"
        "- Work only inside the current directory.\n"
        "- Do not commit, push, or open pull requests; the orchestrator "
        "does that.\n"
        "- Keep the diff minimal and consistent with existing style.\n"
        "- End your final summary with a section titled '## Decisions': "
        "one line per comment, as `comment <id>: what you did - why`. "
        "Write `none` only if there was nothing to do.\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def reply_body(comments, decisions, changed, tests_note):
    """The one summary reply. Marker first: it is what keeps the next pass
    from answering the factory's own voice."""
    lines = [AGENT_MARKER, "## Review feedback addressed", ""]
    for c in comments:
        first = (c.get("body") or "").strip().splitlines()
        lines.append(f"> {first[0] if first else '(empty comment)'}")
    lines.append("")
    if decisions:
        lines += [f"- {d}" for d in decisions]
    elif changed:
        lines.append("- code changed; the agent left no decision ledger, "
                     "read the pushed commit.")
    else:
        lines.append("- no code change this round; the agent judged the "
                     "comments already satisfied or out of scope.")
    lines.append("")
    lines.append(f"{tests_note} Mutation gate is not re-run on babysit "
                 "rounds; it ran on the original attempt.")
    return "\n".join(lines) + "\n"


def post_reply(pr_url, body, gh_path=None, token=None):
    """Post the summary comment. Raises BabysitError on failure."""
    gh = wt._gh_binary(gh_path)
    if not gh:
        raise BabysitError("gh CLI not found; cannot reply to review comments")
    fd, path = tempfile.mkstemp(suffix=".md", prefix="babysit-reply-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        proc = subprocess.run([gh, "pr", "comment", pr_url, "--body-file", path],
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace",
                              env=wt._token_env(token))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise BabysitError(f"gh pr comment failed ({proc.returncode}): "
                           f"{(proc.stderr or proc.stdout).strip()[:300]}")


def run_round(cfg, con, ticket, comments):
    """Agent round on the PR's own branch.

    Returns (outcome, detail, decisions, changed). outcome is 'replied'
    when the worktree ended with a pushed (or purely explanatory) answer,
    'deferred' when the engine was unreachable, and 'failed' for anything
    the human must know about.
    """
    repo = cfg["repo"]
    branch = ticket["branch"] or wt._safe_branch(ticket["ref"], ticket["id"])
    tier, tier_reason = agentmod.tier_decision(cfg, ticket)
    spec = cfg["agent"][tier]
    attempt = int(ticket["attempts"]) + 1

    path = None
    try:
        path, branch = wt.checkout(repo, branch)
    except wt.WorktreeError as exc:
        return "failed", f"could not check out {branch}: {exc}", [], False

    try:
        prompt_path = write_feedback_prompt(path, ticket, comments)
        run_id = dbmod.start_run(con, ticket["id"], attempt, tier=tier,
                                 tier_reason="review feedback")
        res = agentmod.run(cfg, spec, ticket["title"], path,
                           prompt_path=prompt_path)
        dbmod.end_run(con, run_id, res["status"], res.get("cost_usd", 0.0),
                      (res.get("stderr") or res.get("stdout") or "")[:2000])
        print(f"[babysit {ticket['id']}] tier={tier} status={res['status']} "
              f"cost=${res.get('cost_usd', 0.0):.4f}", flush=True)

        if res["status"] == "error":
            return "deferred", "engine unreachable", [], False
        if res["status"] != "ok":
            return "failed", (f"agent {res['status']}: "
                              f"{(res.get('stderr') or '')[:300]}"), [], False

        decisions = agentmod.extract_decisions(res.get("stdout", ""))
        changed = wt.commit_all(path, f"babysit {ticket['ref'] or ticket['id']}: "
                                      f"address review feedback")
        if changed:
            test_cmd = (cfg.get("gates") or {}).get("test_command")
            if test_cmd:
                ok, out = gatesmod.run_tests(
                    path, test_cmd,
                    env=agentmod.minimal_env(
                        env_extra=(cfg.get("agent") or {}).get("env")))
                if not ok:
                    return "failed", ("tests failing after feedback round, "
                                      "nothing pushed:\n" + out[-1200:]), [], True
            if cfg.get("push", True):
                try:
                    wt.push_branch(path, branch, cfg.get("remote", "origin"),
                                   token=cfg.get("gh_token"))
                except wt.WorktreeError as exc:
                    return "failed", f"push failed: {exc}", [], True
        return "replied", "", decisions, changed
    finally:
        if path and not cfg.get("keep_worktrees", False):
            try:
                wt.remove(repo, path)
            except Exception as exc:
                print(f"WARNING: could not clean babysit worktree {path}: {exc}")


def address(cfg, con, gh_path=None, limit=1):
    """One babysit pass over open PRs. Returns [(ticket_id, outcome), ...].

    The engine lock is the same one the worker holds: a feedback round runs
    a model, and this deployment serves one engine at a time.
    """
    from . import worker as workermod

    if not (cfg.get("babysit") or {}).get("enabled"):
        return []
    if dbmod.is_paused(con):
        return []
    lock = workermod._engine_lock()
    if lock is None:
        return []
    try:
        return _address(cfg, con, gh_path, limit)
    finally:
        workermod._release_engine_lock(lock)


def _address(cfg, con, gh_path, limit):
    gh = wt._gh_binary(gh_path or (cfg or {}).get("gh_path"))
    if not gh:
        return []
    rows = con.execute(
        "SELECT * FROM tickets WHERE status = 'pr_open' "
        "AND pr_url IS NOT NULL AND pr_url <> '' ORDER BY id").fetchall()

    results, rounds = [], 0
    for ticket in rows:
        try:
            owner, repo, number = syncmod.parse_pr_url(ticket["pr_url"])
            comments = fetch_comments(gh, owner, repo, number,
                                      (cfg or {}).get("gh_token"))
        except (syncmod.SyncError, BabysitError) as exc:
            results.append((ticket["id"], f"fetch failed: {exc}"))
            continue
        after = int(ticket["feedback_after"] or 0)
        new = [c for c in comments if is_human_comment(c, after)]
        if not new:
            continue
        if rounds >= int(limit):
            results.append((ticket["id"], "waiting (one round per pass)"))
            continue
        rounds += 1
        cursor = max(int(c.get("id") or 0) for c in new)

        if dbmod.ticket_spend(con, ticket["id"]) >= float(ticket["budget_usd"]):
            why = (f"babysit: budget exhausted, {len(new)} review comment(s) "
                   f"on {ticket['pr_url']} unanswered")
            dbmod.add_escalation(con, ticket["id"], why)
            notify.post(cfg, "BABYSIT", f"ticket {ticket['id']}: {why}")
            con.execute("UPDATE tickets SET feedback_after=?, updated_at=? "
                        "WHERE id=?", (cursor, time.time(), ticket["id"]))
            results.append((ticket["id"], "escalated (budget)"))
            continue

        outcome, detail, decisions, changed = run_round(cfg, con, ticket, new)
        if outcome == "deferred":
            results.append((ticket["id"], "deferred (engine unreachable)"))
            continue
        if outcome == "failed":
            con.execute("UPDATE tickets SET feedback_after=?, updated_at=? "
                        "WHERE id=?", (cursor, time.time(), ticket["id"]))
            dbmod.add_escalation(con, ticket["id"], "babysit: " + detail)
            notify.post(cfg, "BABYSIT",
                        f"ticket {ticket['id']}: {detail[:300]}")
            results.append((ticket["id"], "escalated"))
            continue

        tests_note = ("Tests were re-run and pass." if changed else
                      "No code changed, so no gates re-ran.")
        try:
            post_reply(ticket["pr_url"],
                       reply_body(new, decisions, changed, tests_note),
                       gh_path=gh_path or (cfg or {}).get("gh_path"),
                       token=(cfg or {}).get("gh_token"))
        except BabysitError as exc:
            results.append((ticket["id"], f"reply failed: {exc}"))
            continue
        con.execute("UPDATE tickets SET feedback_after=?, updated_at=? "
                    "WHERE id=?", (cursor, time.time(), ticket["id"]))
        results.append((ticket["id"], "replied"))
    return results
