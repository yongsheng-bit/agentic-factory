"""GUI preview: render the app, screenshot it, post the pictures to the PR.

DOM assertions prove the page says the right things; they cannot tell a
readable chart from an empty one. When a ticket touches GUI files the
worker renders the app from synthetic fixture data inside the attempt
worktree, screenshots each page with real Chromium, and posts the images
as a PR comment so the human reviewer sees what the change looks like
before merging.

The preview is strictly best-effort: it runs after the PR is already
open, and every failure path returns False so a missing Chromium or a
broken gh call costs the reviewer pictures, never the ticket. Fixture
data is synthetic and generated here; real station data never enters
this pipeline.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys

FIXTURE_GRADES = ("REGULAR", "PLUS", "SUPREME", "DIESEL", "ULTRA94")
FIXTURE_DISPENSERS = ("D1", "D2", "D3", "D4", "D5", "D6")
DEFAULT_GUI_PATHS = ("dashboard/",)
DEFAULT_PAGES = ("index.html", "summary.html")


def should_preview(diff_text, paths=DEFAULT_GUI_PATHS):
    """Return True when the diff touches any configured GUI path."""
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git a/"):
            touched = line.split(" b/", 1)[-1].strip()
            for pattern in paths:
                if touched.startswith(pattern):
                    return True
    return False


def write_fixture_csv(out_dir, days=21):
    """Write a deterministic synthetic receipts CSV and return its path.

    The shape matches the real product schema (date, dispenser, grade,
    liters, dollars with dollars empty) so the preview exercises the same
    loader path as production data. Values follow a fixed weekly rhythm
    so screenshots are stable across runs.
    """
    import datetime
    start = datetime.date(2026, 1, 5)
    rows = []
    for day in range(days):
        date = (start + datetime.timedelta(days=day)).isoformat()
        for index, grade in enumerate(FIXTURE_GRADES):
            liters = 1000.0 * (index + 1) * (1 + (day % 7) / 2.0)
            rows.append({"date": date,
                         "dispenser": FIXTURE_DISPENSERS[index],
                         "grade": grade,
                         "liters": "%.2f" % liters,
                         "dollars": ""})
    path = os.path.join(out_dir, "fixture.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["date", "dispenser", "grade",
                                                "liters", "dollars"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def render_pages(worktree, cfg, out_dir):
    """Render the app's pages from fixture data; return their paths.

    Runs the customer CLI from the attempt worktree with the preview
    python (the venv that has the project's test extras). Raises on any
    nonzero exit; the caller treats that as "no pictures".
    """
    preview = cfg.get("preview") or {}
    python = preview.get("python") or sys.executable
    os.makedirs(out_dir, exist_ok=True)
    csv_path = write_fixture_csv(out_dir, int(preview.get("fixture_days", 21)))
    pages = preview.get("pages") or DEFAULT_PAGES
    args = [python, "-m", preview.get("module", "dashboard"),
            "--csv", csv_path, "--out", os.path.join(out_dir, pages[0])]
    if len(pages) > 1:
        args += ["--summary", os.path.join(out_dir, pages[1])]
    env = dict(os.environ)
    env["PYTHONPATH"] = worktree
    proc = subprocess.run(args, cwd=worktree, env=env, timeout=120,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("preview render failed (%d): %s"
                           % (proc.returncode,
                              ((proc.stderr or "") + (proc.stdout or ""))[:300]))
    return [os.path.join(out_dir, page) for page in pages]


SCREENSHOT_SCRIPT = """
import os
import sys
from playwright.sync_api import sync_playwright

out_dir = sys.argv[1]
made = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    for html_path in sys.argv[2:]:
        page.goto("file:///" + os.path.abspath(html_path).replace("\\\\", "/"))
        page.wait_for_load_state("networkidle")
        png = os.path.join(
            out_dir,
            os.path.splitext(os.path.basename(html_path))[0] + ".png")
        page.screenshot(path=png, full_page=True)
        made.append(png)
    browser.close()
print("\\n".join(made))
"""


def screenshot_pages(html_paths, out_dir, python=None):
    """Full-page Chromium screenshots via the preview python subprocess.

    The factory is stdlib-only, so playwright is never imported here:
    the screenshots run inside the customer venv (which has playwright
    and its Chromium) and only the resulting PNG paths cross back.
    """
    proc = subprocess.run([python or sys.executable, "-c", SCREENSHOT_SCRIPT,
                           out_dir] + list(html_paths),
                          timeout=180, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("screenshot failed: %s"
                           % ((proc.stderr or "")[-300:]))
    return [line.strip() for line in (proc.stdout or "").splitlines()
            if line.strip().endswith(".png")]


def comment_body(png_paths):
    """Markdown PR comment embedding each screenshot.

    The factory marker is what tells babysit this is the factory's own
    voice and not review feedback; a preview comment without it would be
    answered by an agent round forever.
    """
    lines = ["<!-- factory:agent -->",
             "**GUI preview** (synthetic fixture data, rendered from this "
             "branch in headless Chromium):", ""]
    for png in png_paths:
        name = os.path.splitext(os.path.basename(png))[0]
        lines.append("![%s](%s)" % (name, png))
        lines.append("")
    return "\n".join(lines)


def post_comment(pr_url, body, png_paths, worktree, gh_path=None, token=None):
    """Attach the screenshots to the PR as one comment. Returns True on ok."""
    from factory import worktree as wt
    body_file = os.path.join(os.path.dirname(png_paths[0]), "preview.md")
    with open(body_file, "w", encoding="utf-8") as fh:
        fh.write(body)
    args = [wt._gh_binary(gh_path), "pr", "comment", pr_url,
            "--body-file", body_file]
    for png in png_paths:
        args += ["--attach", png]
    proc = subprocess.run(args, cwd=worktree, timeout=300,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=wt._token_env(token))
    return proc.returncode == 0


def run_preview(worktree, diff_text, cfg, pr_url, out_dir=None):
    """Best-effort GUI preview for one PR. Never raises; returns True on post.

    Does nothing (returns False) when previewing is disabled, the diff
    touches no GUI files, or any step fails - the PR is already open and
    the reviewer simply gets no pictures.
    """
    preview = cfg.get("preview") or {}
    if not preview.get("enabled") or not pr_url:
        return False
    if not should_preview(diff_text, tuple(preview.get("paths")
                                           or DEFAULT_GUI_PATHS)):
        return False
    try:
        if out_dir is None:
            import tempfile
            out_dir = tempfile.mkdtemp(prefix="factory-preview-")
        pngs = screenshot_pages(render_pages(worktree, cfg, out_dir), out_dir,
                                python=preview.get("python"))
        if not pngs:
            return False
        ok = post_comment(pr_url, comment_body(pngs), pngs, worktree,
                          gh_path=cfg.get("gh_path"),
                          token=cfg.get("gh_token"))
        print("gui preview: %s (%d images)" % ("posted" if ok else "FAILED",
                                               len(pngs)), flush=True)
        return ok
    except Exception as exc:
        print("gui preview skipped: %s" % (exc,), flush=True)
        return False
