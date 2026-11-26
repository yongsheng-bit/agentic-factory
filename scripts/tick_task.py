"""Scheduler entry: one `factory tick`, logged.

Windows Task Scheduler launches this every 15 minutes. It exists because a
scheduled process has no console and no memory: everything worth knowing
about a tick must land in the log, and the log must never grow without
bound. Exit code is the tick's own; the scheduler sees failures without
needing a second alerting path.
"""

import os
import subprocess
import sys
import time

if os.name == "nt":
    _orig_popen = subprocess.Popen

    class _NoWindowPopen(_orig_popen):
        """Task Scheduler runs this entry under pythonw: every console child
        (python, gh, git) would otherwise flash its own window, so the
        no-window flag is the process-wide default."""

        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags", 0)
            kwargs["creationflags"] = flags | 0x08000000
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoWindowPopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "tick.log")
MAX_LOG = 1_000_000

CONFIGS = ["factory.json", "factory-petro.json", "factory-dashboard.json", "factory-acclone.json"]


def resolve_token():
    """The gh CLI login is the one credential; a pasted PAT expires and dies.

    `gh auth token` returns the keyring's oauth token, which works for both
    the API and git-over-https, and refreshes with the login. Returns None
    when gh is not logged in: the tick then fails loudly per-ticket instead
    of pushing with no identity.
    """
    import json
    gh = None
    try:
        with open(os.path.join(ROOT, "factory.json"), encoding="utf-8") as fh:
            gh = json.load(fh).get("gh_path")
    except OSError:
        pass
    for cand in (gh, "gh"):
        if not cand:
            continue
        try:
            p = subprocess.run([cand, "auth", "token"], capture_output=True,
                               text=True, timeout=30)
        except OSError:
            continue
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return None


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
        fh.flush()
        rc = 0
        for name in CONFIGS:
            cfg_path = os.path.join(ROOT, name)
            if not os.path.exists(cfg_path):
                continue
            fh.write("--- %s ---\n" % name)
            fh.flush()
            one = subprocess.run(
                [sys.executable, "-m", "factory.cli",
                 "-c", cfg_path, "tick", "--max", "2"],
                cwd=ROOT, env=env, stdout=fh, stderr=fh, timeout=3000).returncode
            rc = rc or one
        fh.write("exit=%d\n" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
