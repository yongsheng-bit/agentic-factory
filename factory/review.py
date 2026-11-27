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
    '{{"verdict": "approve" or "concerns", "score": <integer 1-5>, '
    '"notes": "max 3 short sentences"}}\n'
    "score is merge-readiness: 5 = ship it as-is, 4 = good with one small "
    "fix, 3 = real issues worth a round, 1-2 = wrong on the ticket. "
    "State-of-the-art reviewers usually land a first PR at 4.\n"
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
    score = None
    raw_score = blob.get("score")
    if raw_score is not None:
        try:
            score = max(1, min(5, int(raw_score)))
        except (TypeError, ValueError):
            score = None
    return verdict, str(blob.get("notes", ""))[:600], score


def review_diff(cfg, ticket, diff_text):
    """Returns (verdict, notes, score) or (None, reason, None). Never raises.

    score is the merge-readiness number 1-5 or None when the model did not
    supply one; the judge loop treats a missing score as "not a 5". The
    engine comes from the review section, which may name a DIFFERENT model
    than the one that wrote the code - that independence is the point.
    """
    rc, base, model = llmclient.engine_from_cfg(cfg, "review")
    if not rc.get("enabled"):
        return None, "review disabled", None
    if not base or not model:
        return None, "review not configured", None
    prompt = REVIEW_TEMPLATE.format(
        title=ticket["title"], body=(ticket["body"] or "")[:2000],
        diff=(diff_text or "")[:12000])
    try:
        content = llmclient.chat(base, model, prompt, max_tokens=900,
                                 api_key=rc.get("api_key"))
    except Exception as exc:
        return None, "review failed: " + str(exc)[:120], None
    parsed = _parse_verdict(content)
    if parsed is None:
        return None, "review unparseable", None
    return parsed
