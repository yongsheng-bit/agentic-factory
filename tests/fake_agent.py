#!/usr/bin/env python3
"""Stand-in agent for smoke-testing the pipeline without spending tokens.

Reads the prompt file (so we prove the placeholder wiring works), writes a
diff, prints a cost JSON in the shape factory/agent.py parses. Behaviour is
selected with FAKE_MODE: ok | fail | secret | migration | empty
"""

import json
import os
import pathlib
import sys

mode = os.environ.get("FAKE_MODE", "ok")

prompt = ""
if len(sys.argv) > 1:
    p = pathlib.Path(sys.argv[1])
    if p.exists():
        prompt = p.read_text(encoding="utf-8")

if mode == "empty":
    print(json.dumps({"total_cost_usd": 0.01, "note": "wrote nothing",
                      "saw_prompt_bytes": len(prompt)}))
    sys.exit(0)

if mode == "secret":
    pathlib.Path("leak.py").write_text('AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n', encoding="utf-8")
elif mode == "migration":
    pathlib.Path("migrations").mkdir(exist_ok=True)
    pathlib.Path("migrations/0001_add_col.py").write_text("# schema change\n", encoding="utf-8")
else:
    pathlib.Path("fix.py").write_text("def fix():\n    return 42\n", encoding="utf-8")
    # An agent that runs the tests leaves build artifacts behind. They must not
    # ride into the PR just because the target repo happens to lack a
    # .gitignore entry for them.
    pathlib.Path("__pycache__").mkdir(exist_ok=True)
    pathlib.Path("__pycache__/fix.cpython-314.pyc").write_bytes(b"\x00\x0b\x0d\x0a")
    pathlib.Path(".pytest_cache").mkdir(exist_ok=True)
    pathlib.Path(".pytest_cache/lastfailed").write_text("{}", encoding="utf-8")

if mode == "fail":
    print("Traceback: simulated agent crash", file=sys.stderr)
    sys.exit(1)

print(json.dumps({"type": "result", "total_cost_usd": 0.0123,
                  "saw_prompt_bytes": len(prompt)}))
