"""Unit tests for the GUI preview module."""

import csv
import os
import tempfile
import unittest

from factory import preview


DIFF_GUI = (
    "diff --git a/dashboard/render.py b/dashboard/render.py\n"
    "--- a/dashboard/render.py\n"
    "+++ b/dashboard/render.py\n"
    "@@ -1 +1 @@\n-x\n+y\n")

DIFF_OTHER = (
    "diff --git a/factory/worker.py b/factory/worker.py\n"
    "--- a/factory/worker.py\n"
    "+++ b/factory/worker.py\n"
    "@@ -1 +1 @@\n-x\n+y\n")


class TestShouldPreview(unittest.TestCase):
    def test_gui_diff_triggers(self):
        self.assertTrue(preview.should_preview(DIFF_GUI))

    def test_backend_diff_does_not_trigger(self):
        self.assertFalse(preview.should_preview(DIFF_OTHER))

    def test_empty_diff_does_not_trigger(self):
        self.assertFalse(preview.should_preview(""))
        self.assertFalse(preview.should_preview(None))

    def test_custom_paths_are_honored(self):
        self.assertTrue(preview.should_preview(DIFF_OTHER, ("factory/",)))
        self.assertFalse(preview.should_preview(DIFF_GUI, ("web/",)))


class TestFixtureCsv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = preview.write_fixture_csv(self.tmp.name, days=14)

    def test_schema_matches_the_product(self):
        with open(self.path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 14 * len(preview.FIXTURE_GRADES))
        self.assertEqual(sorted(rows[0]),
                         ["date", "dispenser", "dollars", "grade", "liters"])
        for row in rows:
            self.assertEqual(row["dollars"], "")
            self.assertGreater(float(row["liters"]), 0.0)
            self.assertIn(row["dispenser"], preview.FIXTURE_DISPENSERS)

    def test_fixture_is_deterministic(self):
        again = preview.write_fixture_csv(self.tmp.name, days=14)
        with open(self.path, "rb") as fh:
            first = fh.read()
        with open(again, "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)


class TestCommentBody(unittest.TestCase):
    def test_body_embeds_every_image(self):
        body = preview.comment_body(["/tmp/index.png", "/tmp/summary.png"])
        self.assertIn("![index](/tmp/index.png)", body)
        self.assertIn("![summary](/tmp/summary.png)", body)
        self.assertIn("GUI preview", body)


class TestRunPreviewGuards(unittest.TestCase):
    def test_disabled_config_returns_false(self):
        self.assertFalse(preview.run_preview("/nowhere", DIFF_GUI, {},
                                             "https://example/pr"))

    def test_missing_pr_url_returns_false(self):
        cfg = {"preview": {"enabled": True}}
        self.assertFalse(preview.run_preview("/nowhere", DIFF_GUI, cfg, ""))

    def test_non_gui_diff_returns_false(self):
        cfg = {"preview": {"enabled": True}}
        self.assertFalse(preview.run_preview("/nowhere", DIFF_OTHER, cfg,
                                             "https://example/pr"))

    def test_render_failure_is_swallowed(self):
        cfg = {"preview": {"enabled": True, "python": "no-such-python-xyz"}}
        self.assertFalse(preview.run_preview(
            os.getcwd(), DIFF_GUI, cfg, "https://example/pr"))


if __name__ == "__main__":
    unittest.main()
