"""Tests for the event-driven wake path: decisions, spawn shape, beats."""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from factory import db as dbmod
from factory import wake as wakemod
from factory import worker as workermod

ENABLED = {"wake": {"enabled": True}}


def fresh_db(case):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = dbmod.connect(path)
    dbmod.init(con)
    case.addCleanup(con.close)
    return con


def lock_path(case):
    fd, path = tempfile.mkstemp(suffix=".lock")
    os.close(fd)

    def drop():
        try:
            os.unlink(path)
        except OSError:
            pass

    case.addCleanup(drop)
    return path


class TestShouldWake(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db(self)
        self.lock = lock_path(self)

    def test_refuses_when_disabled(self):
        dbmod.add_ticket(self.con, "t")
        ok, why = wakemod.should_wake({}, self.con, lock_path=self.lock)
        self.assertFalse(ok)
        self.assertEqual(why, "wake disabled")

    def test_refuses_when_paused(self):
        dbmod.add_ticket(self.con, "t")
        dbmod.set_paused(self.con, True)
        ok, why = wakemod.should_wake(ENABLED, self.con, lock_path=self.lock)
        self.assertFalse(ok)
        self.assertEqual(why, "queue paused")

    def test_refuses_when_queue_empty(self):
        ok, why = wakemod.should_wake(ENABLED, self.con, lock_path=self.lock)
        self.assertFalse(ok)
        self.assertEqual(why, "queue empty")

    def test_refuses_when_engine_busy(self):
        dbmod.add_ticket(self.con, "t")
        held = workermod._engine_lock(self.lock)
        self.assertIsNotNone(held)
        try:
            ok, why = wakemod.should_wake(ENABLED, self.con,
                                          lock_path=self.lock)
        finally:
            if held is not None:
                workermod._release_engine_lock(held)
                held.close()
        self.assertFalse(ok)
        self.assertIn("engine busy", why)

    def test_probe_release_leaves_the_lock_free(self):
        dbmod.add_ticket(self.con, "t")
        self.assertFalse(wakemod.lock_busy(self.lock))
        self.assertFalse(wakemod.lock_busy(self.lock))

    def test_wakes_on_queued_work_and_idle_engine(self):
        dbmod.add_ticket(self.con, "t")
        ok, why = wakemod.should_wake(ENABLED, self.con, lock_path=self.lock)
        self.assertTrue(ok)
        self.assertIn("idle", why)


class TestSpawnTick(unittest.TestCase):
    def test_builds_a_detached_tick_command(self):
        seen = {}

        def fake_popen(argv, **opts):
            seen["argv"] = argv
            seen["opts"] = opts

        log_dir = tempfile.mkdtemp()
        wakemod.spawn_tick("cfg.json", popen=fake_popen, log_dir=log_dir)
        self.assertEqual(seen["argv"],
                         [sys.executable, "-m", "factory.cli", "-c",
                          "cfg.json", "tick"])
        self.assertEqual(seen["opts"]["stdin"], subprocess.DEVNULL)
        self.assertTrue(os.path.exists(os.path.join(log_dir, "wake.log")))
        if os.name == "nt":
            flags = seen["opts"]["creationflags"]
            self.assertTrue(flags & subprocess.DETACHED_PROCESS)

    def test_wake_spawns_only_after_a_yes(self):
        con = fresh_db(self)
        lock = lock_path(self)
        with patch.object(wakemod, "spawn_tick") as sp:
            self.assertEqual(
                wakemod.wake({}, con, "cfg.json", lock_path=lock),
                "wake disabled")
            sp.assert_not_called()
            dbmod.add_ticket(con, "t")
            self.assertEqual(
                wakemod.wake(ENABLED, con, "cfg.json", lock_path=lock),
                "tick spawned")
            sp.assert_called_once()


class TestWatchOnce(unittest.TestCase):
    def setUp(self):
        self.con = fresh_db(self)
        self.lock = lock_path(self)

    def _beat(self, imported=None, raises=None):
        def importer(cfg, con, gh_path=None, label=None):
            if raises:
                raise raises
            return "o/r", imported or [], []
        with patch.object(wakemod, "wake") as wk:
            wk.return_value = "tick spawned"
            new, status = wakemod.watch_once(
                ENABLED, self.con, "cfg.json", lock_path=self.lock,
                importer=importer)
        return new, status, wk

    def test_quiet_beat_does_not_wake(self):
        new, status, wk = self._beat()
        self.assertEqual((new, status), ([], "quiet"))
        wk.assert_not_called()

    def test_new_tickets_wake(self):
        new, status, wk = self._beat(imported=[(7, 3)])
        self.assertEqual(new, [7])
        self.assertEqual(status, "tick spawned")
        wk.assert_called_once()

    def test_tracker_failure_is_a_quiet_beat_with_a_reason(self):
        new, status, _ = self._beat(raises=RuntimeError("gh down"))
        self.assertEqual(new, [])
        self.assertTrue(status.startswith("import failed"))

    def test_beat_passes_gh_and_label_through(self):
        got = {}

        def importer(cfg, con, gh_path=None, label=None):
            got["gh"] = gh_path
            got["label"] = label
            return "o/r", [], []
        wakemod.watch_once(ENABLED, self.con, "cfg.json", gh_path="gh.exe",
                           label="build", lock_path=self.lock,
                           importer=importer)
        self.assertEqual(got, {"gh": "gh.exe", "label": "build"})


class TestWatchLoop(unittest.TestCase):
    def test_once_shape_returns_the_beat(self):
        con = fresh_db(self)

        def beat(cfg, con, cfg_path, gh_path=None, label=None):
            return [9], "tick spawned"

        new, status = wakemod.watch({}, con, "cfg.json", once=True,
                                    beat=beat)
        self.assertEqual((new, status), ([9], "tick spawned"))


class TestImportRace(unittest.TestCase):
    def test_issue_number_is_unique_but_nulls_are_free(self):
        con = fresh_db(self)
        dbmod.add_ticket(con, "a", issue_number=42)
        with self.assertRaises(sqlite3.IntegrityError):
            dbmod.add_ticket(con, "b", issue_number=42)
        dbmod.add_ticket(con, "c")
        dbmod.add_ticket(con, "d")
        n = con.execute("SELECT COUNT(*) c FROM tickets").fetchone()["c"]
        self.assertEqual(n, 3)


if __name__ == "__main__":
    unittest.main()
