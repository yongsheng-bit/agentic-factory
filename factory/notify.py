"""Escalation. Prints, and posts to a webhook if one is configured.

Escalation is the human's only entry point into the factory, so it must never
fail silently. If the webhook is unreachable we still print and still record
the escalation in the database.
"""

from __future__ import annotations

import json
import os
import urllib.request


def notify(title, text, webhook=None, dry=False):
    line = f"\n=== {title} ===\n{text}\n"
    print(line, flush=True)
    url = webhook or os.environ.get("FACTORY_WEBHOOK")
    if not url or dry:
        return False
    payload = {"text": f"{title}\n{text}"}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as exc:  # never let notification failure kill a run
        print(f"[notify] webhook failed: {exc}", flush=True)
        return False
