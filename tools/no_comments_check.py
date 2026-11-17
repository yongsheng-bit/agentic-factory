"""Fail the build if any Python file contains a comment.

Comments are banned in this codebase; docstrings carry the explanation. A
tokenizer is used rather than grep because '#' inside a string - a markdown
heading in a prompt, a regex, a generated file's contents - is data.
"""

import io
import os
import sys
import tokenize

SKIP_DIRS = {".git", ".factory-worktrees", "__pycache__", ".pytest_cache",
             ".venv", "node_modules", ".mypy_cache", ".ruff_cache", "build",
             "dist"}


def check(path):
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    try:
        return [(tok.start[0], tok.string)
                for tok in tokenize.generate_tokens(io.StringIO(text).readline)
                if tok.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return [(0, f"unparseable file, cannot audit: {exc}")]


def main(root="."):
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                for line, text in check(path):
                    hits.append((path, line, text))
    if hits:
        print(f"::error::{len(hits)} comment(s) found. This codebase uses "
              f"docstrings, not comments.")
        for path, line, text in hits[:50]:
            print(f"  {path}:{line}: {text[:90]}")
        if len(hits) > 50:
            print(f"  ... {len(hits) - 50} more")
        return 1
    print("no comments in any Python file")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
