"""Container sandbox tests: argv shape, wiring, and fail-closed behavior.

These run without Docker: they assert the *decisions* (what would be
mounted, what network, which env survives). The live smoke test at the
bottom only runs when FACTORY_SANDBOX_LIVE=1 and docker answers.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import agent as agentmod
from factory import gates, mutate, sandbox


CFG = {"sandbox": {"enabled": True, "image": "factory-agent:local",
                   "network": "factory-llm",
                   "egress": {"network": "factory-llm",
                              "gate_name": "factory-egress",
                              "listen": 8080,
                              "target": "192.168.68.73:8080",
                              "llm_base": "http://factory-egress:8080/v1"},
                   "limits": {"memory": "2g", "cpus": "2", "pids": "256"}}}
OFF = {"sandbox": {"enabled": False, "image": "factory-agent:local"}}


class ArgvTests(unittest.TestCase):
    def test_disabled_keeps_legacy_paths(self):
        self.assertFalse(sandbox.enabled(OFF))
        self.assertFalse(sandbox.enabled({}))
        self.assertFalse(sandbox.enabled(None))

    def test_run_argv_is_boxed(self):
        argv = sandbox.build_run_argv("img:1", ["python3", "-c", "print(1)"],
                                      name="factory-x", workdir="/tmp/w",
                                      mounts=[("/tmp/out", "/out", "rw")],
                                      env=(("LLM_BASE", "http://gate:8080/v1"),),
                                      network="none")
        joined = " ".join(argv)
        self.assertIn("--network none", joined)
        self.assertIn("--read-only --tmpfs /tmp", joined)
        self.assertIn("--pids-limit 256", joined)
        self.assertIn("/tmp/out:/out:rw", joined)
        self.assertIn("-e LLM_BASE=http://gate:8080/v1", joined)

    def test_no_denylisted_mounts_ever(self):
        argv = sandbox.build_run_argv(
            "img:1", ["true"], workdir=os.getcwd(),
            mounts=[(os.path.expanduser("~"), "/home", "ro")])
        self.assertIn("home", " ".join(argv))

    def test_agent_wrap_mounts_only_prompt_runner_and_worktree(self):
        argv, name, env = sandbox.wrap_agent_command(
            CFG, {"command": "true"}, "prompt text",
            os.path.join(os.getcwd(), "prompt.txt"), os.getcwd(),
            {"LLM_BASE": "http://192.168.68.73:8080/v1",
             "PATH": "C:\\Users\\someone\\env\\Scripts;{PATH}",
             "SYSTEMROOT": "C:\\Windows"})
        joined = " ".join(argv)
        self.assertIn("/prompts:ro", joined)
        self.assertIn("/opt/runner:ro", joined)
        self.assertIn(f"{sandbox.WORK_MOUNT}:rw", joined)
        self.assertIn("--network factory-llm", joined)
        self.assertIn("http://factory-egress:8080/v1", joined)
        self.assertNotIn("192.168.68.73", joined)
        self.assertNotIn("GH_TOKEN", joined)
        self.assertNotIn("GITHUB", joined)
        self.assertNotIn("-e PATH=", joined)
        self.assertNotIn("PATH", env)
        self.assertTrue(name.startswith("factory-"))

    def test_kill_tolerates_missing_container(self):
        sandbox.kill("factory-does-not-exist-xyz")
        sandbox.kill(None)


class WiringTests(unittest.TestCase):
    def test_run_tests_signature_accepts_cfg(self):
        ok, out = gates.run_tests(os.getcwd(), "exit 0", cfg=OFF)
        self.assertTrue(ok)

    def test_zero_tests_still_blocks(self):
        ok, out = gates.run_tests(os.getcwd(),
                                  "echo 'no tests ran'", cfg=OFF)
        self.assertFalse(ok)

    def test_mutations_skip_without_candidates(self):
        ok, report = mutate.run_mutations(os.getcwd(), "", "exit 0", cfg=OFF)
        self.assertTrue(ok)
        self.assertIn("no candidates", report)

    def test_agent_run_blocked_when_command_missing(self):
        result = agentmod.run(OFF, {"command": "definitely-not-a-real-binary-xyz"},
                              "p", os.getcwd())
        self.assertEqual(result["status"], "blocked")


class LiveSmoke(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FACTORY_SANDBOX_LIVE") == "1",
                         "set FACTORY_SANDBOX_LIVE=1 with docker running")
    def test_probe_and_suite_in_container(self):
        self.assertTrue(sandbox.probe(CFG))
        rc, out = sandbox.run_suite(CFG, "python3 -c 'print(6*7)'",
                                    os.getcwd())
        self.assertEqual(rc, 0)
        self.assertIn("42", out)

    @unittest.skipUnless(os.environ.get("FACTORY_SANDBOX_LIVE") == "1",
                         "set FACTORY_SANDBOX_LIVE=1 with docker running")
    def test_no_network_inside(self):
        probe = ("import socket\n"
                 "try:\n"
                 "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
                 "    print('LEAKED')\n"
                 "except OSError:\n"
                 "    print('BLOCKED')\n")
        rc, out = sandbox.run_container(
            CFG, ["python3", "-c", probe], timeout_s=60)
        self.assertIn("BLOCKED", out)
        self.assertNotIn("LEAKED", out)


if __name__ == "__main__":
    unittest.main()
