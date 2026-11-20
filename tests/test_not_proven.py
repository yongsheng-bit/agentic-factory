import unittest

from factory import worker as workermod


class NotProvenLines(unittest.TestCase):
    def test_all_gates_off_names_every_gap(self):
        lines = workermod.not_proven_lines(
            test_ran=False, mutation_ran=False,
            preview_enabled=False, review_ok=False)
        joined = "\n".join(lines)
        self.assertIn("test suite did NOT run", joined)
        self.assertIn("mutation gate disabled", joined)
        self.assertIn("GUI preview disabled", joined)
        self.assertIn("AI review unavailable", joined)

    def test_all_green_still_states_preview_uncertainty(self):
        lines = workermod.not_proven_lines(
            test_ran=True, mutation_ran=True,
            preview_enabled=True, review_ok=True)
        self.assertEqual(len(lines), 1)
        self.assertIn("after this PR opens", lines[0])

    def test_mutation_off_while_tests_pass(self):
        lines = workermod.not_proven_lines(
            test_ran=True, mutation_ran=False,
            preview_enabled=True, review_ok=True)
        joined = "\n".join(lines)
        self.assertNotIn("test suite", joined)
        self.assertIn("mutation gate disabled", joined)

    def test_section_is_never_empty(self):
        for t in (True, False):
            for m in (True, False):
                for p in (True, False):
                    for r in (True, False):
                        self.assertTrue(workermod.not_proven_lines(t, m, p, r))


if __name__ == "__main__":
    unittest.main()
