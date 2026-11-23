"""Pre-flight ticket refinement: catch spec bugs before they burn attempts.

The most expensive tickets in this factory failed on their own text, not
their code: an unsolvable spec (T1) and a ticket naming a module its
dependency would create (T3). Both are checkable before any attempt runs -
mechanically for symbols and duplicates, with one cheap LLM pass for
contradictions. A ticket that fails refinement goes to needs_info without
consuming an attempt: fixing the text is cheaper than watching three
attempts rediscover the same hole.
"""

import difflib
import json
import os
import re
import subprocess

from . import llmclient

SYMBOL_RE = re.compile(r"`([A-Za-z_]\w*(?:\.\w+)+)(?:\([^)]*\))?`")
FILE_RE = re.compile(r"`([\w./-]+\.(?:py|md|json|csv|xlsx|yml|yaml))`")

SOLVABILITY_TEMPLATE = (
    "You are checking whether a work ticket can be completed as written."
    " Do not solve it; only judge the spec.\n\n"
    "## Ticket\n{title}\n\n{body}\n\n"
    "## Repository files\n{files}\n\n"
    "A ticket is broken if its acceptance criteria contradict each other,"
    " depend on files or data not present and not mentioned as inputs, or"
    " have no defined passing condition.\n"
    "Reply with ONLY this JSON object:\n"
    '{{"ok": true or false, "problems": ["one line each"]}}\n'
)


def symbols(body):
    return sorted(set(SYMBOL_RE.findall(body or ""))
                  | set(FILE_RE.findall(body or "")))


def _mentioned_elsewhere(sym, sibling_texts):
    attr = sym.split(".")[-1]
    if attr.endswith(".py"):
        attr = attr[:-3]
    return any(attr in text for text in sibling_texts)


STDLIB_ROOTS = {"os", "sys", "re", "json", "csv", "io", "time", "math",
                "datetime", "pathlib", "collections", "difflib", "urllib",
                "subprocess", "shutil", "tempfile", "unittest", "typing"}


def _tree_files(repo_dir):
    try:
        r = subprocess.run(
            ["git", "-C", repo_dir, "ls-tree", "-r", "--name-only",
             "origin/main"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return set(r.stdout.split())


def _tree_show(repo_dir, rel):
    try:
        r = subprocess.run(
            ["git", "-C", repo_dir, "show", "origin/main:" + rel],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _file_exists(repo_dir, sym, tree):
    if tree is not None:
        return sym in tree
    return os.path.exists(os.path.join(repo_dir, sym.replace("/", os.sep)))


def symbol_problems(repo_dir, body, sibling_texts):
    """Backticked dotted names and files in the ticket must exist in the
    repo, or be named by a sibling ticket that creates them (T3).

    Only module paths of three or more parts are checked, and stdlib roots
    are exempt: two-part names are attribute calls on local or third-party
    objects, and `page.get_text()` (PyMuPDF) was flagged as a missing repo
    module on ticket 8. The T3 class this gate exists for always spells
    the full module path (`petro.dips.resolve_blocks`).
    """
    problems = []
    tree = _tree_files(repo_dir)
    for sym in symbols(body):
        if sym.endswith(".py") or "/" in sym:
            if not _file_exists(repo_dir, sym, tree):
                if not _mentioned_elsewhere(sym, sibling_texts):
                    problems.append(f"file `{sym}` does not exist in the repo")
            continue
        parts = sym.split(".")
        if len(parts) < 3 or parts[0] in STDLIB_ROOTS:
            continue
        if tree is not None:
            stem = "/".join(parts[:-1])
            rel = stem + ".py" if stem + ".py" in tree else (
                stem + "/__init__.py" if stem + "/__init__.py" in tree
                else None)
            src = _tree_show(repo_dir, rel) if rel is not None else None
        else:
            local_stem = os.path.join(repo_dir, *parts[:-1])
            rel = local_stem + ".py" if os.path.exists(local_stem + ".py") \
                else (os.path.join(local_stem, "__init__.py")
                      if os.path.exists(
                          os.path.join(local_stem, "__init__.py"))
                      else None)
            src = None
            if rel is not None:
                with open(rel, encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
        if src is None:
            if not _mentioned_elsewhere(sym, sibling_texts):
                problems.append(
                    f"module for `{sym}` does not exist in the repo")
            continue
        if parts[-1] not in src and not _mentioned_elsewhere(sym,
                                                             sibling_texts):
            problems.append(
                f"`{sym}` is not defined in the repo and no queued ticket"
                " names it")
    return problems


def duplicate_problems(tickets, ticket):
    problems = []
    for other in tickets:
        if other["id"] == ticket["id"]:
            continue
        if other["status"] not in ("queued", "pr_open"):
            continue
        ratio = difflib.SequenceMatcher(
            None, ticket["title"].lower(), other["title"].lower()).ratio()
        if ratio > 0.8:
            problems.append(
                f"overlaps ticket {other['id']} ({ratio:.0%} title match):"
                f" {other['title']}")
    return problems


def _file_list(repo_dir, cap=300):
    names = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules",
                                ".venv", "build", "dist")]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), repo_dir)
            names.append(rel.replace(os.sep, "/"))
            if len(names) >= cap:
                return names
    return names


def llm_problems(cfg, ticket, repo_dir):
    rc, base, model = llmclient.engine_from_cfg(cfg, "refine")
    if not base or not model:
        return []
    prompt = SOLVABILITY_TEMPLATE.format(
        title=ticket["title"], body=(ticket["body"] or "")[:3000],
        files="\n".join(_file_list(repo_dir))[:6000])
    try:
        content = llmclient.chat(base, model, prompt, max_tokens=600)
    except Exception:
        return []
    blob = llmclient.extract_json(content)
    if not blob or blob.get("ok"):
        return []
    return ["spec check: " + str(p) for p in
            list(blob.get("problems") or [])[:3]]


def check(cfg, con, ticket):
    """Blocking problems with this ticket's text; empty list means build it.
    Mechanical checks first - they are free, deterministic and caught every
    real escalation so far; the LLM pass only adds contradiction detection."""
    repo_dir = cfg["repo"]
    siblings = [t["title"] + " " + (t["body"] or "") for t in con.execute(
        "SELECT id, title, body FROM tickets"
        " WHERE status IN ('queued', 'pr_open')").fetchall()]
    problems = symbol_problems(repo_dir, ticket["body"], siblings)
    problems += duplicate_problems(
        con.execute("SELECT id, title, status FROM tickets").fetchall(),
        ticket)
    if problems:
        return problems
    if (cfg.get("refine") or {}).get("llm", True):
        return llm_problems(cfg, ticket, repo_dir)
    return []
