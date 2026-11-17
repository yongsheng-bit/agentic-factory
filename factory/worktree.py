"""Git worktree isolation: one checkout per agent instance.

This is the detail that stops N concurrent agents from destroying each other's
work. A shared checkout means agent A's `git checkout` moves agent B's files
mid-edit. Worktrees give each ticket its own working directory while sharing
one object store, so it costs disk, not clones.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import time


class WorktreeError(RuntimeError):
    pass


def _git(args, cwd=None, env=None):
    """Run git, retrying children that were killed from OUTSIDE.

    On Windows a spawned console app can be terminated by something that is
    not git: antivirus scanning a freshly created worktree, a console Ctrl+C
    reaching the process, a transient `.git/index.lock`. Those surface as
    NTSTATUS termination codes (0xC000013A STATUS_CONTROL_C_EXIT, 0xC0000142
    DLL_INIT_FAILED, ...) with empty stderr, or as git's 128 lock error.
    Observed twice on this machine: `git add -A` returned 0xC000013A with no
    output, while the identical call from a terminal and from a detached
    probe returned 0. Treating that as a ticket failure is wrong; treating it
    as unretryable is fragile.
    """
    killed = {0xC000013A, 0xC0000142, 0xC0000005, 0xC0000022}
    for attempt in range(4):
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600, env=env)
        rc = r.returncode
        transient = ((rc is not None and (rc & 0xFFFFFFFF) in killed)
                     or (rc == 128 and "index.lock" in (r.stderr or "")))
        if not (transient and attempt < 3):
            break
        time.sleep(0.5 * (attempt + 1))
    if r.returncode != 0:

        detail = (r.stderr.strip() or r.stdout.strip() or "(no output)")
        raise WorktreeError(f"git {' '.join(args)}: rc={r.returncode} "
                            f"(0x{r.returncode & 0xFFFFFFFF:08X}) {detail[:400]}"
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


ARTIFACT_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 ".factory-tmp", "node_modules", "dist", "build"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _is_artifact(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in ARTIFACT_DIRS for p in parts[:-1]):
        return True
    return rel.endswith(ARTIFACT_SUFFIXES)


AGENT_IDENTITY = ("agentic-factory", "bot@localhost")


def _identity_env():
    """Env for `git commit` that carries a committer identity.

    `--author` alone is not enough: git still refuses to commit without a
    *committer*, and a worker on a fresh host - a CI runner, a new rental box -
    has no global user.name/user.email. CI caught this as a red e2e job: rc=128
    "Committer identity unknown / empty ident name (for <runner@...>)". The
    factory must not depend on whose machine it runs on, so the identity is
    supplied per-process and never written to anyone's global config.
    """
    name, email = AGENT_IDENTITY
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        env.setdefault(f"GIT_{role}_NAME", name)
        env.setdefault(f"GIT_{role}_EMAIL", email)
    return env


def _auth_env(env, token, remote_url=None):
    """Attach a token as an HTTP header via git's one-shot config env vars.

    Deliberately not a URL with an embedded password: that persists in
    .git/config, which every linked worktree shares, and shows up in
    `git remote -v`. One-shot config lives only in this child's environment,
    and a header keeps the secret out of argv, where a process list could read
    it. GIT_TERMINAL_PROMPT=0 makes a bad token fail instead of hanging on an
    invisible credential prompt.
    """
    if not token:
        return env
    host = "github.com"
    if remote_url and "://" in remote_url:
        host = remote_url.split("://", 1)[1].split("/", 1)[0].split("@")[-1]
    secret = base64.b64encode(("x-access-token:" + token).encode("utf-8")).decode("ascii")
    idx = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    env["GIT_CONFIG_KEY_%d" % idx] = "http.https://%s/.extraheader" % host
    env["GIT_CONFIG_VALUE_%d" % idx] = "AUTHORIZATION: basic " + secret
    env["GIT_CONFIG_COUNT"] = str(idx + 1)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _token_env(token):
    """Env for the gh CLI: gh prefers GH_TOKEN over the keyring login."""
    env = _identity_env()
    if token:
        env["GH_TOKEN"] = token
    return env


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
        return False
    except WorktreeError:
        pass
    name, email = AGENT_IDENTITY
    _git(["commit", "-m", message, "--author", f"{name} <{email}>"],
         cwd=path, env=_identity_env())
    return True


def delete_remote_branch(path, branch, remote="origin", token=None):
    """Remove one agent branch from the remote, ignoring "it was not there".

    Only ever called for `agent/*` names, which the factory owns outright. The
    shape is delete-then-push rather than `--force` so a mistake cannot rewrite
    a branch somebody else pushed to.
    """
    url = ""
    if token:
        url = _git(["remote", "get-url", remote], cwd=path)
    env = _auth_env(_identity_env(), token, url)
    if not str(branch or "").startswith("agent/"):
        raise WorktreeError(f"refusing to delete {remote}/{branch}: only "
                            "agent-owned branches may be removed")
    try:
        _git(["push", remote, "--delete", branch], cwd=path, env=env)
    except WorktreeError:
        pass


def push_branch(path, branch, remote="origin", token=None, attempt=1):
    """Push an attempt's branch.

    Branch names carry no attempt number, so a retry reuses the name. If an
    earlier attempt pushed but never opened a pull request - or the PR was
    closed - the remote head is a commit this attempt does not descend from and
    a plain push is rejected as non-fast-forward, killing the ticket. Clearing
    the stale agent-owned head first makes the retry pushable.
    """
    url = ""
    if token:
        url = _git(["remote", "get-url", remote], cwd=path)
    env = _auth_env(_identity_env(), token, url)
    if int(attempt or 1) > 1:
        delete_remote_branch(path, branch, remote=remote, token=token)
    _git(["push", "-u", remote, branch], cwd=path, env=env)


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


def open_pr(path, branch, title, body, base="main", gh_path=None, token=None):
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
                       encoding="utf-8", errors="replace", env=_token_env(token))
    if r.returncode != 0:
        detail = ((r.stderr or "") + (r.stdout or "")).strip()
        raise RuntimeError(f"gh pr create failed ({r.returncode}): {detail[:400]}")
    url = (r.stdout or "").strip().splitlines()[-1].strip() if (r.stdout or "").strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"gh pr create returned no url: {(r.stdout or '')[:200]!r}")
    return url
