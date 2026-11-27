"""Scheduler entry: one watcher beat per wake-enabled config, logged.

Windows Task Scheduler launches this every minute (the 15-minute tick task
stays as the backstop). It is the event half of issue intake: a
``factory``-labelled issue filed from a phone becomes a running build
within a minute instead of a scheduler period. A missed or failed beat is
latency, never loss - the tick imports whatever the watcher missed.

Beats are stateless by design (change detection is the ticket database),
so this task needs no daemon, no pid file, and survives being killed
mid-beat. Only configs with ``wake.enabled`` are polled, so opting a
board out is one config key.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from tick_task import resolve_token

LOG = os.path.join(ROOT, "logs", "watch.log")
MAX_LOG = 1_000_000
CONFIGS = ["factory.json", "factory-petro.json", "factory-dashboard.json",
           "factory-acclone.json"]


def wake_enabled(cfg_path):
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            return bool((json.load(fh).get("wake") or {}).get("enabled"))
    except (OSError, ValueError):
        return False


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if os.path.exists(LOG) and os.path.getsize(LOG) > MAX_LOG:
        os.replace(LOG, LOG + ".1")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if not env.get("FACTORY_GH_TOKEN"):
        tok = resolve_token()
        if tok:
            env["FACTORY_GH_TOKEN"] = tok
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        for name in CONFIGS:
            cfg_path = os.path.join(ROOT, name)
            if not os.path.exists(cfg_path) or not wake_enabled(cfg_path):
                continue
            try:
                subprocess.run(
                    [sys.executable, "-m", "factory.cli",
                     "-c", cfg_path, "watch", "--once"],
                    cwd=ROOT, env=env, stdout=fh, stderr=fh, timeout=120)
            except subprocess.TimeoutExpired:
                fh.write("watch: beat timed out (%s)\n" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
