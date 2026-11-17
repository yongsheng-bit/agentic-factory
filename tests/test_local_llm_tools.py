"""Sandbox tests for the cheap-tier agent's tools.

These call the tool functions directly, with no LLM in the loop, because the
things being tested must hold regardless of what the model decides to ask for.
A model that hallucinates a `git push` must not be able to produce one.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("local_llm",
                                              os.path.join(ROOT, "scripts", "local_llm.py"))
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)


def w(path, text):
    """Write a file and close it, so no ResourceWarning pollutes the output."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


ENV_PROBE = """
import os
print("secret_leaked=", "FACTORY_TEST_SECRET" in os.environ, sep="")
print("has_path=", "PATH" in os.environ, sep="")
temp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
print("temp_in_tree=", os.path.realpath(temp).startswith(os.path.realpath(os.getcwd())), sep="")
"""


class ToolSandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="llm-tool-")
        llm.CTX = llm.Ctx(self.tmp)
        w(os.path.join(self.tmp, "app.py"), "x = 1\n")
        os.makedirs(os.path.join(self.tmp, ".git"), exist_ok=True)
        w(os.path.join(self.tmp, ".git", "config"), "[core]\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- path containment -------------------------------------------------

    def test_read_outside_worktree_is_refused(self):
        self.assertIn("escapes", llm.t_read({"path": "../../../Windows/win.ini"}))

    def test_write_outside_worktree_is_refused(self):
        out = os.path.join(self.tmp, "..", "escaped.py")
        self.assertIn("escapes", llm.t_write({"path": out, "content": "boom"}))
        self.assertFalse(os.path.exists(os.path.abspath(out)))

    def test_absolute_path_outside_is_refused(self):
        self.assertIn("escapes", llm.t_read({"path": sys.executable}))

    def test_git_internals_are_off_limits(self):
        self.assertIn("internals", llm.t_write({"path": ".git/config", "content": "x"}))
        self.assertIn("internals", llm.t_read({"path": ".git/config"}))

    def test_symlink_escape_is_refused(self):
        link = os.path.join(self.tmp, "sneaky")
        try:
            os.symlink(os.path.dirname(self.tmp), link, target_is_directory=True)
        except OSError:      # symlinks need privileges on Windows
            self.skipTest("cannot create symlinks here")
        self.assertIn("escapes", llm.t_read({"path": "sneaky"}))

    def test_normal_read_write_work(self):
        self.assertIn("x = 1", llm.t_read({"path": "app.py"}))
        self.assertTrue(llm.t_write({"path": "new/util.py",
                                     "content": "y = 2\n"}).startswith("wrote"))
        self.assertIn("y = 2", llm.t_read({"path": "new/util.py"}))

    # ---- edit_file precision ---------------------------------------------

    def test_edit_requires_unique_match(self):
        w(os.path.join(self.tmp, "dup.py"), "a\na\n")
        self.assertIn("matches 2 times",
                      llm.t_edit({"path": "dup.py", "old_text": "a", "new_text": "b"}))

    def test_edit_reports_missing_text_clearly(self):
        self.assertIn("not found",
                      llm.t_edit({"path": "app.py", "old_text": "x = 99", "new_text": "z"}))

    def test_edit_applies_on_exact_match(self):
        self.assertTrue(llm.t_edit({"path": "app.py", "old_text": "x = 1",
                                    "new_text": "x = 2"}).startswith("edited"))
        self.assertIn("x = 2", llm.t_read({"path": "app.py"}))

    # ---- command denylist ---------------------------------------------------

    def test_dangerous_commands_refused(self):
        for cmd in ["git commit -m x", "git push origin main", "curl http://evil.com",
                    "rm -rf /", "sudo rm -rf /", "ssh root@10.0.0.1", "wget http://x/y",
                    "taskkill /F /IM python.exe", "shutdown /s"]:
            self.assertIn("refused", llm.t_run({"command": cmd}), f"allowed: {cmd}")

    def test_benign_commands_allowed(self):
        # No shell, so cmd builtins (echo, dir, set) are gone; a coding agent
        # verifies with interpreters and test runners, which still work.
        out = llm.t_run({"command": "python -c \"print('hello')\""})
        self.assertTrue(out.startswith("exit 0"), out)
        self.assertIn("hello", out)

    def test_run_command_runs_in_the_worktree(self):
        llm.t_write({"path": "where.py",
                     "content": "import os\nprint(os.getcwd())\n"})
        out = llm.t_run({"command": "python where.py"})
        self.assertIn(os.path.realpath(self.tmp).lower().replace("\\", "/"),
                      out.lower().replace("\\", "/"))

    # ---- command runner: no shell, scrubbed environment -------------------

    def test_redirection_refused_and_nothing_written(self):
        out = llm.t_run({"command": "python -c pass > ../escaped.txt"})
        self.assertIn("no shell", out)
        self.assertFalse(os.path.exists(
            os.path.abspath(os.path.join(self.tmp, "..", "escaped.txt"))))

    def test_pipes_and_chains_refused(self):
        for cmd in ["python -c pass | more", "python -c pass && echo done",
                    "python -c pass; echo done"]:
            self.assertIn("no shell", llm.t_run({"command": cmd}), cmd)

    def test_operators_inside_quoted_arguments_are_allowed(self):
        # The runner has no shell, so a ';' inside an argument is data. Rejecting
        # it would make `python -c "import os; print(1)"` unusable.
        out = llm.t_run({"command": 'python -c "import sys; print(sys.version_info[0])"'})
        self.assertTrue(out.startswith("exit 0"), out)

    def test_child_environment_is_scrubbed(self):
        os.environ["FACTORY_TEST_SECRET"] = "hunter2-do-not-leak"
        try:
            llm.t_write({"path": "showenv.py", "content": ENV_PROBE})
            out = llm.t_run({"command": "python showenv.py"})
        finally:
            del os.environ["FACTORY_TEST_SECRET"]
        self.assertIn("secret_leaked=False", out.replace(" ", ""))
        self.assertIn("has_path=True", out.replace(" ", ""))

    def test_scratch_dir_stays_inside_the_worktree(self):
        llm.t_write({"path": "showenv.py", "content": ENV_PROBE})
        out = llm.t_run({"command": "python showenv.py"})
        self.assertIn("temp_in_tree=True", out.replace(" ", ""))

    def test_arguments_naming_outside_paths_are_refused(self):
        for cmd in ["python ..\\evil.py", "python C:\\Windows\\win.ini",
                    "python /etc/passwd", "python -m pytest ../shared_tests"]:
            self.assertIn("outside the worktree", llm.t_run({"command": cmd}).replace(
                "contains '..'", "outside the worktree"), cmd)

    def test_relative_paths_inside_the_worktree_are_fine(self):
        llm.t_write({"path": "pkg\\t.py", "content": "print('ok')\n"})
        self.assertIn("ok", llm.t_run({"command": "python pkg/t.py"}))

    # ---- discovery tools ----------------------------------------------------

    def test_search_finds_literal_text(self):
        self.assertIn("app.py:1", llm.t_search({"text": "x = 1"}))

    def test_search_reports_no_match_without_lying(self):
        self.assertIn("no matches", llm.t_search({"text": "def nonexistent_zzz"}))

    def test_list_skips_noise_directories(self):
        os.makedirs(os.path.join(self.tmp, "node_modules", "pkg"), exist_ok=True)
        w(os.path.join(self.tmp, "node_modules", "pkg", "index.js"), "//x")
        self.assertNotIn("node_modules", llm.t_list({"pattern": "**/*.js"}))

    def test_list_default_pattern_includes_root_files(self):
        # fnmatch has no real `**`: `**/*` used to match nothing at the root,
        # so list_files returned an empty string for a normal small repo.
        listing = llm.t_list({})
        self.assertIn("app.py", listing)

    def test_list_pattern_matches_root_and_nested(self):
        os.makedirs(os.path.join(self.tmp, "pkg"), exist_ok=True)
        w(os.path.join(self.tmp, "pkg", "deep.py"), "pass\n")
        listing = llm.t_list({"pattern": "**/*.py"})
        self.assertIn("app.py", listing)
        self.assertIn(os.path.join("pkg", "deep.py").replace("\\", "/"), listing)

    # ---- output shape consumed by factory/agent.py --------------------------

    def test_tool_output_is_capped(self):
        w(os.path.join(self.tmp, "big.py"), "z" * 500000)
        self.assertLessEqual(len(llm.t_read({"path": "big.py"})), llm.MAX_TOOL_CHARS + 5)

    def test_tools_return_errors_instead_of_raising(self):
        # A raised exception escapes to the orchestrator and kills the run;
        # an error string at least lets the model recover.
        for call in (lambda: llm.t_read({"path": "/etc/passwd"}),
                     lambda: llm.t_write({"path": "../x", "content": "a"}),
                     lambda: llm.t_edit({"path": "../x", "old_text": "a", "new_text": "b"})):
            self.assertIn("error", call())

    def test_json_summary_shape(self):
        # The orchestrator parses one JSON line with these keys for cost.
        payload = {"status": "completed", "result": "did it", "steps": 3,
                   "changed_files": ["app.py"], "input_tokens": 100, "output_tokens": 20}
        buf = io.StringIO()
        with redirect_stdout(buf):
            print(json.dumps(payload))
        parsed = json.loads(buf.getvalue().strip())
        self.assertEqual(parsed["input_tokens"], 100)
        self.assertEqual(parsed["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
