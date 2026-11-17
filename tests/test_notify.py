"""Tests for escalation delivery.

Escalation is the human's only entry point into the factory, so the two
properties that matter are: it always prints, and it never raises. A dead
webhook must not be able to kill a worker run.
"""

import io
import os
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from factory import notify as notifymod


class TestNotify(unittest.TestCase):
    def test_no_webhook_configured_still_prints(self):
        """No argument and no FACTORY_WEBHOOK: print, return False, no request."""
        urlopen = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch("urllib.request.urlopen", urlopen), \
                redirect_stdout(buf):
            result = notifymod.notify("STUCK", "task 7 needs a human")
        out = buf.getvalue()
        self.assertFalse(result)
        urlopen.assert_not_called()
        self.assertIn("STUCK", out)
        self.assertIn("task 7 needs a human", out)

    def test_dry_run_performs_no_network_request(self):
        urlopen = MagicMock()
        buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch("urllib.request.urlopen", urlopen), \
                redirect_stdout(buf):
            result = notifymod.notify("STUCK", "text",
                                      webhook="https://example.invalid/hook",
                                      dry=True)
        out = buf.getvalue()
        self.assertFalse(result)
        urlopen.assert_not_called()
        self.assertIn("STUCK", out)
        self.assertIn("text", out)

    def test_unreachable_webhook_does_not_raise(self):
        """A dead webhook returns False; the run continues."""
        urlopen = MagicMock(side_effect=urllib.error.URLError("connection refused"))
        buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
                patch("urllib.request.urlopen", urlopen), \
                redirect_stdout(buf):
            result = notifymod.notify("STUCK", "text",
                                      webhook="https://example.invalid/hook")
        out = buf.getvalue()
        self.assertFalse(result)
        urlopen.assert_called_once()
        self.assertIn("STUCK", out)
        self.assertIn("webhook failed", out)


if __name__ == "__main__":
    unittest.main()
