import json
import tempfile
import unittest

from factory import agent


def runner_stdout(result_text, extra_line=None):
    lines = []
    if extra_line:
        lines.append(json.dumps(extra_line))
    lines.append(json.dumps({"status": "completed", "result": result_text}))
    return "\n".join(lines)


class ExtractDecisions(unittest.TestCase):
    def test_extracts_items_and_stops_at_next_heading(self):
        out = runner_stdout(
            "built the loader\n\n"
            "## Decisions\n"
            "- sqlite over sqlalchemy - stdlib only - extra deps it beat\n"
            "  * dict rows over tuples - readability\n\n"
            "## Notes\n- not a decision\n")
        self.assertEqual(agent.extract_decisions(out), [
            "sqlite over sqlalchemy - stdlib only - extra deps it beat",
            "dict rows over tuples - readability",
        ])

    def test_none_is_empty(self):
        out = runner_stdout("done\n\n## Decisions\nnone\n")
        self.assertEqual(agent.extract_decisions(out), [])

    def test_missing_section_is_empty(self):
        out = runner_stdout("done, no ledger here")
        self.assertEqual(agent.extract_decisions(out), [])

    def test_no_json_blob_is_empty(self):
        self.assertEqual(agent.extract_decisions("just prose"), [])

    def test_blob_without_text_is_empty(self):
        out = json.dumps({"status": "error", "error": "boom"})
        self.assertEqual(agent.extract_decisions(out), [])

    def test_openai_shape_response_key(self):
        out = json.dumps({"choices": [{"message": {"content":
                        "## Decisions\n- kept it flat - simpler - classes"}}]})
        self.assertEqual(agent.extract_decisions(out),
                         ["kept it flat - simpler - classes"])

    def test_last_blob_wins(self):
        out = runner_stdout("## Decisions\n- fresh - later line - wins",
                            extra_line={"status": "completed",
                                        "result": "## Decisions\n- stale - earlier - loses"})
        self.assertEqual(agent.extract_decisions(out),
                         ["fresh - later line - wins"])

    def test_blank_lines_between_items_are_skipped(self):
        out = runner_stdout("## Decisions\n\n- one - a - b\n\n- two - c - d\n")
        self.assertEqual(agent.extract_decisions(out),
                         ["one - a - b", "two - c - d"])


class PromptCarriesDecisionRule(unittest.TestCase):
    def test_prompt_asks_for_a_decisions_section(self):
        ticket = {"id": 7, "ref": None, "title": "t", "body": "b",
                  "max_retries": 2}
        path = agent.write_prompt(tempfile.mkdtemp(), ticket, 1,
                                  out_dir=tempfile.mkdtemp())
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("## Decisions", text)
        self.assertIn("alternative it beat", text)


class ClipSummary(unittest.TestCase):
    def test_short_text_untouched(self):
        from scripts import local_llm
        self.assertEqual(local_llm.clip_summary("short"), "short")

    def test_tail_survives_long_summary(self):
        from scripts import local_llm
        text = "x" * 5000 + "\n## Decisions\n- kept the tail - why - alt"
        clipped = local_llm.clip_summary(text)
        self.assertLessEqual(len(clipped), 2000 + len("\n[...]\n"))
        self.assertTrue(clipped.endswith("- kept the tail - why - alt"))


if __name__ == "__main__":
    unittest.main()
