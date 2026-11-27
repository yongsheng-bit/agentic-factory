"""Unslop pass: strip mechanical slop after the tests, before the PR opens.

Agent-authored diffs routinely carry slop the test suite cannot see: dead
code the agent left behind, unused imports, duplicated literals, a private
re-implementation of a helper the repo already has. A reviewer reads every
line of it; this pass asks the engine for a list of exact-match deletions
or replacements, applies them mechanically, and keeps them only if the test
suite still passes. The suite is the contract - an unslop edit that changes
behavior cannot survive it.

The safety rules are the feature: edits never touch tests/ (tests are the
weapon in this pipeline, never the target, and a mutation-gate scar says
why); every find string must match its file exactly once or the edit is
skipped; a red suite after applying reverts every edit; and every failure
path returns the original diff. The pass can only ever make the PR smaller
or leave it byte-identical.

Runs between the test gate and the mutation gate so the mutation gate
judges the code that actually ships.
"""

from __future__ import annotations

import os

from . import gates
from . import llmclient
from . import worktree as wt

UNSLOP_TEMPLATE = (
    "You are the unslop pass of an automated code factory. The diff below "
    "already passes the test suite. Find MECHANICAL slop only:\n"
    "- dead code the author left behind (unreachable branches, commented-"
    "out leftovers, unused imports or variables the diff introduced)\n"
    "- duplicated literals or a re-implementation of a helper that "
    "already exists in the shown diff's own file\n"
    "- redundant indirection the diff added for no reason\n\n"
    "Rules:\n"
    "- NEVER touch files under tests/ or any test_*.py file.\n"
    "- NEVER change behavior; this is cleanup, not a fix.\n"
    "- Every find string must be copied EXACTLY from the file and must "
    "appear exactly once in it.\n"
    "- Style preferences are not slop.\n\n"
    "## Ticket\n{title}\n\n"
    "## Diff\n```diff\n{diff}\n```\n\n"
    "Reply with ONLY this JSON object:\n"
    '{{"edits": [{{"file": "path/in/repo", "find": "exact text", '
    '"replace": "replacement or empty string"}}, ...], '
    '"notes": "max 2 short sentences"}}\n'
    "Use an empty edits list when the diff is already clean.\n"
)


def parse_edits(text, max_edits=8):
    """The edit list from a model reply, or []. Never raises."""
    blob = llmclient.extract_json(text)
    if not isinstance(blob, dict):
        return []
    raw = blob.get("edits")
    if not isinstance(raw, list):
        return []
    edits = []
    for item in raw[:int(max_edits)]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("file") or "").strip().replace("\\", "/")
        find = item.get("find")
        repl = item.get("replace")
        if not path or not isinstance(find, str) or not find:
            continue
        if not isinstance(repl, str):
            continue
        edits.append({"file": path, "find": find, "replace": repl})
    return edits


def _reject_reason(worktree, edit):
    """Why this edit must be skipped, or None when it may apply."""
    rel = edit["file"]
    if rel.startswith("/") or ".." in rel.split("/"):
        return "path escapes the worktree"
    norm = os.path.normpath(os.path.join(worktree, rel))
    if not norm.startswith(os.path.abspath(worktree) + os.sep):
        return "path escapes the worktree"
    if rel.startswith("tests/") or os.path.basename(rel).startswith("test_"):
        return "edits to tests are never allowed"
    if not os.path.isfile(norm):
        return "file not found"
    try:
        with open(norm, encoding="utf-8") as fh:
            content = fh.read()
    except (OSError, UnicodeDecodeError):
        return "file unreadable"
    if content.count(edit["find"]) != 1:
        return "find text does not match exactly once"
    return None


def apply_edits(worktree, edits):
    """Apply the safe subset of edits to the worktree. Returns applied."""
    applied = []
    for edit in edits:
        reason = _reject_reason(worktree, edit)
        if reason:
            continue
        norm = os.path.normpath(os.path.join(worktree, edit["file"]))
        with open(norm, encoding="utf-8") as fh:
            content = fh.read()
        with open(norm, "w", encoding="utf-8", newline="") as fh:
            fh.write(content.replace(edit["find"], edit["replace"]))
        applied.append(edit)
    return applied


def clean(cfg, ticket, worktree, diff_text, test_cmd, env=None, base="HEAD"):
    """Run the unslop pass on a committed, test-green worktree.

    Returns (diff_text, applied_edits): the diff as it should ship and
    the edits that survived. On ANY failure - no engine, unparseable
    reply, red suite after applying - the worktree is restored to the
    agent's commit and the original diff comes back untouched. base is
    the pre-agent SHA the returned diff must be measured against, since
    a successful pass adds a commit on top.
    """
    ucfg = cfg.get("unslop") or {}
    if not ucfg.get("enabled") or not test_cmd or not diff_text.strip():
        return diff_text, []
    scfg, engine_base, model = llmclient.engine_from_cfg(cfg, "unslop")
    if not engine_base or not model:
        return diff_text, []
    prompt = UNSLOP_TEMPLATE.format(
        title=ticket["title"],
        diff=(diff_text or "")[:int(ucfg.get("diff_cap", 12000))])
    try:
        content = llmclient.chat(engine_base, model, prompt, max_tokens=1200,
                                 api_key=scfg.get("api_key"))
    except Exception:
        return diff_text, []
    edits = parse_edits(content, ucfg.get("max_edits", 8))
    if not edits:
        return diff_text, []
    applied = apply_edits(worktree, edits)
    if not applied:
        return diff_text, []
    ok, _out = gates.run_tests(worktree, test_cmd, env=env, cfg=cfg)
    if not ok:
        wt.discard(worktree)
        return diff_text, []
    message = "unslop: mechanical cleanup pass (%d edits)" % len(applied)
    if not wt.commit_all(worktree, message):
        wt.discard(worktree)
        return diff_text, []
    return wt.diff_text(worktree, base=base), applied
