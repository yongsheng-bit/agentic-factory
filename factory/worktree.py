"""Git worktree isolation: one checkout per agent instance.

This is the detail that stops N concurrent agents from destroying each other's
work. A shared checkout means agent A's `git checkout` moves agent B's files
mid-edit. Worktrees give each ticket its own working directory while sharing
one object store, so it costs disk, not clones.
"""

from __future__ import annotations

import os
import re
import subprocess


class WorktreeError(RuntimeError):
    pass


def _git(args, cwd=None):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
    return r.stdout


def repo_root(path):
    return _git(["rev-parse", "--show-toplevel"], cwd=path).strip()


def ensure_worktrees_dir(root):
    d = os.path.join(root, ".factory-worktrees")
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(root, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(gi), exist_ok=True)
    want = "/.factory-worktrees/\n"
    have = ""
    if os.path.exists(gi):
        have = open(gi, "r", encoding="utf-8").read()
    if want not in have:
        with open(gi, "a", encoding="utf-8") as fh:
            fh.write(want)
    return d


def _safe_branch(ref, ticket_id):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(ref or f"t{ticket_id}")).strip("-").lower()
    return f"agent/{slug or 'ticket'}-{ticket_id}"[:100]


def create(root, ticket_id, ref=None, base="HEAD"):
    """Create an isolated worktree + branch for one ticket. Returns (path, branch)."""
    root = repo_root(root)
    wt_root = ensure_worktrees_dir(root)
    branch = _safe_branch(ref, ticket_id)
    path = os.path.join(wt_root, os.path.basename(branch.replace("/", "_")))

    if os.path.exists(path):
        remove(root, path, branch=branch)
    else:
        # A crashed worker can leave the branch without a worktree; `finally`
        # never ran, so clean it here rather than escalating on retry.
        try:
            _git(["rev-parse", "--verify", "--quiet", branch], cwd=root)
            _git(["branch", "-D", branch], cwd=root)
        except WorktreeError:
            pass

    _git(["worktree", "add", "-b", branch, path, base], cwd=root)
    return path, branch


def remove(root, path, branch=None, force=True):
    root = repo_root(root)
    args = ["worktree", "remove", path]
    if force:
        args.insert(2, "--force")
    try:
        _git(args, cwd=root)
    except WorktreeError:
        if os.path.exists(path):
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        try:
            _git(["worktree", "prune"], cwd=root)
        except WorktreeError:
            pass
    if branch:
        try:
            _git(["branch", "-D", branch], cwd=root)
        except WorktreeError:
            pass


def head_sha(path):
    """Current HEAD sha. Capture this BEFORE committing so the diff you
    classify later is the agent's change and not an empty one."""
    return _git(["rev-parse", "HEAD"], cwd=path).strip()


def changed_files(path, base="HEAD"):
    """Paths changed in the worktree: staged, unstaged and untracked."""
    out = []
    for args in (["diff", "--name-only", base], ["diff", "--name-only", "--cached", base],
                 ["ls-files", "--others", "--exclude-standard"]):
        try:
            out += [l.strip() for l in _git(args, cwd=path).splitlines() if l.strip()]
        except WorktreeError:
            pass
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def diff_text(path, base="HEAD", max_bytes=400_000):
    try:
        staged = _git(["diff", "--cached", base], cwd=path)
        unstaged = _git(["diff", base], cwd=path)
        return (staged + "\n" + unstaged)[:max_bytes]
    except WorktreeError:
        return ""


def diff_stat(path, base="HEAD"):
    """(files, added, deleted) from git diff --shortstat plus untracked files."""
    files = changed_files(path, base)
    added = deleted = 0
    try:
        num = _git(["diff", "--numstat", base], cwd=path)
        for line in num.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                a, d = parts[0], parts[1]
                added += int(a) if a.isdigit() else 0
                deleted += int(d) if d.isdigit() else 0
    except WorktreeError:
        pass
    return len(files), added, deleted


# Build artifacts an agent's own test run leaves behind. The factory must not
# depend on the target repo's .gitignore: with a bare `git add -A`, a committed
# .pyc turns "agent produced no changes" into "PR containing only .pyc files",
# which the blast-radius gate reads as a real change. Caught by
# tests/live_adversarial.py.
ARTIFACT_EXCLUDES = [
    ":(exclude,glob)**/__pycache__/", ":(exclude,glob)__pycache__/",
    ":(exclude,glob)**/*.py[cod]",
    ":(exclude,glob)**/.pytest_cache/", ":(exclude,glob).pytest_cache/",
    ":(exclude,glob)**/.factory-tmp/", ":(exclude,glob).factory-tmp/",
    ":(exclude,glob)**/.mypy_cache/", ":(exclude,glob)**/.ruff_cache/",
    ":(exclude,glob)**/node_modules/",
    ":(exclude,glob)**/dist/", ":(exclude,glob)**/build/",
]


def commit_all(path, message):
    _git(["add", "-A", "--", "."] + ARTIFACT_EXCLUDES, cwd=path)
    try:
        _git(["diff", "--cached", "--quiet"], cwd=path)
        return False  # nothing staged
    except WorktreeError:
        pass
    _git(["commit", "-m", message, "--author", "agentic-factory <bot@localhost>"], cwd=path)
    return True


def push_branch(path, branch, remote="origin"):
    _git(["push", "-u", remote, branch], cwd=path)


def open_pr(path, branch, title, body, base="main"):
    """Open a PR with gh. Returns the PR url, or None if gh is not installed.

    Raises when gh IS installed but the call failed. Returning None there was a
    lie one level up: the worker marked the ticket pr_open and printed the
    branch name, so a failed PR looked exactly like a successful one.
    """
    import shutil as _sh
    if not _sh.which("gh"):
        return None
    r = subprocess.run(["gh", "pr", "create", "--head", branch, "--base", base,
                        "--title", title, "--body", body],
                       cwd=path, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"gh pr create failed ({r.returncode}): {detail[:400]}")
    return r.stdout.strip()
