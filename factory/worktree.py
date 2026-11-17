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
        # rc and stdout belong in the message: a real run died on
        # "git add -A: " with nothing after the colon, because git sometimes
        # fails with the explanation on stdout (or nowhere) and an empty
        # reason is undebuggable after the worktree is already gone.
        detail = (r.stderr.strip() or r.stdout.strip() or "(no output)")
        raise WorktreeError(f"git {' '.join(args)}: rc={r.returncode} {detail[:400]}"
                            f" [cwd={cwd}]")
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


def _rmtree(path):
    """Delete a directory tree on Windows, where a leftover file can be
    read-only or briefly locked. Returns True if the path is gone."""
    import shutil
    import stat
    import time

    def _ro(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for _ in range(4):
        if not os.path.lexists(path):
            return True
        shutil.rmtree(path, onerror=_ro)
        time.sleep(0.25)
    return not os.path.lexists(path)


def remove(root, path, branch=None, force=True):
    root = repo_root(root)
    args = ["worktree", "remove", path]
    if force:
        args.insert(2, "--force")
    try:
        _git(args, cwd=root)
    except WorktreeError:
        pass
    # `git worktree remove` unregisters the worktree but refuses to delete a
    # directory that still holds untracked files - and the agent's own scratch
    # (.factory-tmp) lives inside it. Left behind, that stub makes the next
    # `worktree add` die with "already exists", so the retry escalates too.
    if not _rmtree(path):
        try:
            _git(["worktree", "prune"], cwd=root)
        except WorktreeError:
            pass
        raise WorktreeError(
            f"could not delete stale worktree dir {path} - a file inside is "
            "locked (open editor, AV scan, or a worker still running). Remove "
            "it by hand before retrying this ticket.")
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
ARTIFACT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 ".factory-tmp", "node_modules", "dist", "build"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _is_artifact(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in ARTIFACT_DIRS for p in parts[:-1]):
        return True
    return rel.endswith(ARTIFACT_SUFFIXES)


def commit_all(path, message):
    """Stage everything the agent legitimately changed; return False if it changed nothing.

    Exclusion is done by unstaging AFTER `git add -A`, not with :(exclude)
    pathspecs. Git refuses exclude pathspecs that name a directory the repo's
    .gitignore already ignores ("The following paths are ignored by one of your
    .gitignore files: .factory-tmp") and exits 1 - which is what the first real
    run hit, because the sandbox writes its scratch into .factory-tmp.

    Only files with status A (newly added) are unstaged. A repo that tracks
    build/out.js on purpose must get its modification committed, not deleted
    from the index.
    """
    _git(["add", "-A"], cwd=path)
    try:
        added = _git(["diff", "--cached", "--name-only", "--diff-filter=A"], cwd=path)
    except WorktreeError:
        added = ""
    junk = [f for f in added.splitlines() if f.strip() and _is_artifact(f.strip())]
    if junk:
        _git(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--"] + junk, cwd=path)
    try:
        _git(["diff", "--cached", "--quiet"], cwd=path)
        return False  # nothing staged
    except WorktreeError:
        pass
    _git(["commit", "-m", message, "--author", "agentic-factory <bot@localhost>"], cwd=path)
    return True


def push_branch(path, branch, remote="origin"):
    _git(["push", "-u", remote, branch], cwd=path)


def _gh_binary(cfg_path=None):
    """Locate the gh CLI. A config path wins, then PATH, then the per-user
    Windows install location (which is NOT on PATH for a service-style run)."""
    import shutil as _sh
    for cand in (cfg_path,
                 os.environ.get("LOCALAPPDATA") and
                 os.path.join(os.environ["LOCALAPPDATA"], "Programs", "gh", "bin",
                              "gh.exe"),
                 ):
        if cand and os.path.exists(cand):
            return cand
    return _sh.which("gh")


def open_pr(path, branch, title, body, base="main", gh_path=None):
    """Open a PR with gh and return its url.

    Raises whenever no PR was opened. Returning None used to be the polite
    option and it was a lie one level up: the worker marked the ticket
    pr_open and printed the branch name, so "gh is not installed" looked
    identical to "the PR is waiting for review" - a commit on a remote with
    no reviewer and no CI.
    """
    gh = _gh_binary(gh_path)
    if not gh:
        raise RuntimeError(
            "gh CLI not found, so no PR could be opened. Install gh or set "
            '"gh_path" in factory.json. The branch was pushed; open the PR '
            "by hand and fix the config before the next run.")
    r = subprocess.run([gh, "pr", "create", "--head", branch, "--base", base,
                        "--title", title, "--body", body],
                       cwd=path, capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        detail = ((r.stderr or "") + (r.stdout or "")).strip()
        raise RuntimeError(f"gh pr create failed ({r.returncode}): {detail[:400]}")
    url = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"gh pr create returned no url: {(r.stdout or '')[:200]!r}")
    return url
