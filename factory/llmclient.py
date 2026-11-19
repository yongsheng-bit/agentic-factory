"""One-shot chat against the engine, shared by the reviewer and the refiner.

Both need a single stateless completion - no tools, no worktree, no retry
loop - so they share one stdlib-only client instead of spawning the full
agent runner. enable_thinking is pinned off: these are classification tasks,
and thinking cost 20x tokens on exactly this engine for the same answers.
"""

import json
import re
import urllib.request


def chat(base_url, model, prompt, max_tokens=900, temperature=0.2,
         timeout_s=180, api_key=None,
):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


def extract_json(text):
    """First brace-balanced JSON object in free text, or None.

    A greedy first-brace to last-brace match breaks when the model emits
    stray braces before the object, which happened live on the first real
    review; try every balanced candidate, then fall back to greedy.
    """
    text = text or ""
    candidates = re.findall(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, re.S)
    candidates += re.findall(r"\{.*\}", text, re.S)
    for cand in candidates:
        try:
            blob = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(blob, dict):
            return blob
    return None


def engine_from_cfg(cfg, section):
    """(section_config, base_url, model) for a feature, falling back to the
    strong agent tier's engine."""
    rc = cfg.get(section) or {}
    env = ((cfg.get("agent") or {}).get("strong") or {}).get("env") or {}
    return rc, rc.get("base_url") or env.get("LLM_BASE"), \
        rc.get("model") or env.get("LLM_MODEL")
