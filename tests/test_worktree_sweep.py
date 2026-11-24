"""Tests for worktree.sweep: stale registrations, merged branches, corpses."""

import os
import shutil
import subprocess
import tempfile
import unittest

from factory import worktree


def _git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    return r.stdout


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        _git(["init", "-b", "main"], self.root)
        _git(["config", "user.email", "t@example.com"], self.root)
        _git(["config", "user.name", "Test"], self.root)
        with open(os.path.join(self.root, "f.txt"), "w") as fh:
            fh.write("base\n")
        _git(["add", "f.txt"], self.root)
        _git(["commit", "-m", "base"], self.root)
        _git(["remote", "add", "origin", self.root], self.root)
        _git(["fetch", "origin"], self.root)

    def _branch(self, name, from_ref="HEAD"):
        _git(["branch", name, from_ref], self.root)

    def test_merged_branch_deleted_unmerged_survives(self):
        self._branch("agent/1-1")
        _git(["push", "origin", "main"], self.root)
        self._branch("agent/2-2")
        _git(["checkout", "agent/2-2"], self.root)
        with open(os.path.join(self.root, "g.txt"), "w") as fh:
            fh.write("work\n")
        _git(["add", "g.txt"], self.root)
        _git(["commit", "-m", "wip"], self.root)
        _git(["checkout", "main"], self.root)
        rep = worktree.sweep(self.root)
        self.assertIn("agent/1-1", rep["branches"])
        self.assertNotIn("agent/2-2", rep["branches"])

    def test_unmerged_branch_survives(self):
        _git(["checkout", "-b", "agent/9-9"], self.root)
        with open(os.path.join(self.root, "g.txt"), "w") as fh:
            fh.write("work\n")
        _git(["add", "g.txt"], self.root)
        _git(["commit", "-m", "wip"], self.root)
        _git(["checkout", "main"], self.root)
        rep = worktree.sweep(self.root)
        self.assertNotIn("agent/9-9", rep["branches"])
        _git(["rev-parse", "--verify", "agent/9-9"], self.root)

    def test_checked_out_branch_survives(self):
        path, branch = worktree.create(self.root, 5, ref="issue-5")
        rep = worktree.sweep(self.root)
        self.assertNotIn(branch, rep["branches"])
        self.assertTrue(os.path.isdir(path))

    def test_dry_does_not_list_checked_out_merged_branch(self):
        path, branch = worktree.create(self.root, 8, ref="issue-8")
        rep = worktree.sweep(self.root, dry=True)
        self.assertNotIn(branch, rep["branches"])

    def test_corpse_dir_removed_registered_kept(self):
        path, _ = worktree.create(self.root, 6, ref="issue-6")
        corpse = os.path.join(self.root, ".factory-worktrees", "rescue_9-9")
        os.makedirs(os.path.join(corpse, "sub"))
        with open(os.path.join(corpse, "sub", "x.txt"), "w") as fh:
            fh.write("x\n")
        rep = worktree.sweep(self.root)
        self.assertIn("rescue_9-9", rep["dirs"])
        self.assertFalse(os.path.exists(corpse))
        self.assertTrue(os.path.isdir(path))

    def test_stale_registration_pruned_then_branch_reclaimable(self):
        path, branch = worktree.create(self.root, 7, ref="issue-7")
        shutil.rmtree(path)
        rep = worktree.sweep(self.root)
        self.assertGreaterEqual(rep["pruned"], 1)
        self.assertIn(branch, rep["branches"])

    def test_dry_changes_nothing(self):
        self._branch("agent/3-3")
        corpse = os.path.join(self.root, ".factory-worktrees", "rescue_x")
        os.makedirs(corpse)
        rep = worktree.sweep(self.root, dry=True)
        self.assertIn("agent/3-3", rep["branches"])
        self.assertIn("rescue_x", rep["dirs"])
        _git(["rev-parse", "--verify", "agent/3-3"], self.root)
        self.assertTrue(os.path.exists(corpse))


if __name__ == "__main__":
    unittest.main()
