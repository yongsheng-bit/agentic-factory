"""Tests for the prompt audit: findings on bloat, silence on clean."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from factory import audit
from factory import cli as climod


def make_repo(files):
    d = tempfile.mkdtemp()
    for name, text in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


def kinds(findings):
    return sorted(f["kind"] for f in findings)


class TestAuditInstructions(unittest.TestCase):
    def test_clean_instructions_produce_nothing(self):
        d = make_repo({"AGENTS.md": "# Rules\n\n- Run `python -m unittest` "
                                    "before every PR (CI enforces).\n"})
        self.assertEqual(audit.audit_instructions(d), [])

    def test_no_instruction_files_is_not_an_error(self):
        self.assertEqual(audit.audit_instructions(tempfile.mkdtemp()), [])

    def test_emphasis_bloat_is_flagged(self):
        text = "\n".join(f"- IMPORTANT: never do thing {i}!!" for i in range(5))
        d = make_repo({"AGENTS.md": text})
        self.assertIn("emphasis-bloat", kinds(audit.audit_instructions(d)))

    def test_unenforced_rule_flagged_enforced_rule_not(self):
        d = make_repo({"AGENTS.md": "- Never push directly to main.\n"
                                    "- Never merge before the unittest "
                                    "gate is green.\n"})
        findings = [f for f in audit.audit_instructions(d)
                    if f["kind"] == "unenforced-rule"]
        self.assertEqual(len(findings), 1)
        self.assertIn("Never push", findings[0]["detail"])

    def test_duplicate_rule_across_files_is_flagged(self):
        d = make_repo({"AGENTS.md": "- Always run the test gate.\n",
                       "CLAUDE.md": "- Always run the test gate.\n"})
        self.assertIn("duplicate-rule", kinds(audit.audit_instructions(d)))

    def test_over_budget_file_is_flagged(self):
        d = make_repo({"AGENTS.md": "\n".join(f"line {i}" for i in range(90))})
        self.assertIn("over-budget", kinds(audit.audit_instructions(d)))


class TestAuditRepo(unittest.TestCase):
    def test_secret_in_tree_is_flagged(self):
        d = make_repo({"config.py": "TOKEN = \"ghp_" + "a" * 36 + "\"\n"})
        self.assertIn("secret", kinds(audit.audit_repo(d)))

    def test_ignore_file_skips_triaged_fixtures(self):
        d = make_repo({"config.py": "TOKEN = \"ghp_" + "a" * 36 + "\"\n",
                       ".factoryauditignore": "config.py\n"})
        self.assertEqual(audit.audit_repo(d), [])

    def test_clean_tree_is_silent(self):
        d = make_repo({"app.py": "print('hi')\n",
                       "AGENTS.md": "- Run the unittest gate first.\n"})
        self.assertEqual(audit.audit_repo(d), [])


class TestAuditCli(unittest.TestCase):
    def _cfg(self, repo):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"repo": repo.replace("\\", "/"),
                       "db": os.path.join(tempfile.mkdtemp(), "a.db"),
                       "base_branch": "main"}, fh)
        return path

    def _run(self, repo):
        buf = StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            climod.main(["-c", self._cfg(repo), "audit", "--path", repo])
        return cm.exception.code, buf.getvalue()

    def test_findings_exit_two(self):
        d = make_repo({"AGENTS.md": "\n".join(
            f"- IMPORTANT: never do thing {i}!!" for i in range(5))})
        code, out = self._run(d)
        self.assertEqual(code, 2)
        self.assertIn("emphasis-bloat", out)

    def test_clean_repo_exits_zero(self):
        d = make_repo({"AGENTS.md": "- Run the unittest gate first.\n"})
        code, out = self._run(d)
        self.assertEqual(code, 0)
        self.assertIn("clean", out)

    def test_json_output_parses(self):
        d = make_repo({"AGENTS.md": "- Never push directly to main.\n"})
        import tempfile
        fd, cfg = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"repo": d.replace("\\", "/"),
                       "db": os.path.join(tempfile.mkdtemp(), "a.db"),
                       "base_branch": "main"}, fh)
        buf = StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(buf):
            climod.main(["-c", cfg, "audit", "--path", d, "--json"])
        self.assertEqual(cm.exception.code, 2)
        findings = json.loads(buf.getvalue())
        self.assertEqual(kinds(findings), ["unenforced-rule"])
