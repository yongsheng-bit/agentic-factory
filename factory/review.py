"""AI reviewer: one LLM pass over the diff before the PR opens.

The human merge stays the only binding gate. The reviewer's job is to make
the human's read cheaper: a two-line second opinion on architecture
regression, duplication, secrets and scope, rendered into the PR body. A
reviewer failure never blocks a PR - the merge decision must not depend on
the same engine that wrote the code being available and correct twice in a
row.
"""

import json
import re

from . import llmclient

REVIEW_TEMPLATE = (
    "You are a senior engineer reviewing one pull request from an automated"
    " code factory. Judge only what is shown.\n\n"
    "## Ticket\n{title}\n\n{body}\n\n"
    "## Diff\n```diff\n{diff}\n```\n\n"
    "Reply with ONLY this JSON object:\n"
    '{{"verdict": "approve" or "concerns", "notes": "max 3 short sentences"}}\n'
    "Use verdict=concerns only for: architecture regression, duplicated"
    " logic, hardcoded secrets or machine paths, changes beyond the ticket,"
    " or tests that do not test the ticket. Style preferences are not"
    " concerns.\n"
)


def _parse_verdict(text):
    blob = llmclient.extract_json(text)
    if not blob:
        return None
    verdict = str(blob.get("verdict", "")).lower()
    if verdict not in ("approve", "concerns"):
        return None
    return verdict, str(blob.get("notes", ""))[:600]


def review_diff(cfg, ticket, diff_text):
    """Returns (verdict, notes) or (None, reason). Never raises."""
    rc, base, model = llmclient.engine_from_cfg(cfg, "review")
    if not rc.get("enabled"):
        return None, "review disabled"
    if not base or not model:
        return None, "review not configured"
    prompt = REVIEW_TEMPLATE.format(
        title=ticket["title"], body=(ticket["body"] or "")[:2000],
        diff=(diff_text or "")[:12000])
    try:
        content = llmclient.chat(base, model, prompt, max_tokens=900)
    except Exception as exc:
        return None, "review failed: " + str(exc)[:120]
    parsed = _parse_verdict(content)
    if parsed is None:
        return None, "review unparseable"
    return parsed
