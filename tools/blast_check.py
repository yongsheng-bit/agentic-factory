"""CI-side blast-radius check. Reads changed paths from FILES and numstat.txt.

Exit codes: 0 = low/medium, 2 = high. CI runs this in its own non-required job
and turns a 2 into a warning annotation plus a step summary, rather than
failing a required check. A deliberately-failing required check makes "a human
must read this" indistinguishable from "the build is broken", and the only way
through it is an override - which trains the reviewer to override. Whether a
human actually has to approve is branch protection and CODEOWNERS' job; this
job's job is to say how carefully to read.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.blast import classify


def main():
    files = [f for f in os.environ.get("FILES", "").split() if f]
    if not files and os.path.exists("changed.txt"):
        files = [l.strip() for l in open("changed.txt", encoding="utf-8") if l.strip()]

    added = deleted = 0
    if os.path.exists("numstat.txt"):
        for line in open("numstat.txt", encoding="utf-8"):
            p = line.split("\t")
            if len(p) >= 2:
                added += int(p[0]) if p[0].isdigit() else 0
                deleted += int(p[1]) if p[1].isdigit() else 0

    level, why = classify(files, added, deleted)
    print(f"blast radius: {level} ({why}) [{len(files)} files, +{added} -{deleted}]")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Blast radius: **{level}**\n\n{why}\n")
    return 0 if level != "high" else 2


if __name__ == "__main__":
    sys.exit(main())
