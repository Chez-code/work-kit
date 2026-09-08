"""Kit unit tests for the hook helpers. Stdlib only.

Run from the kit checkout:  python3 -m unittest discover tests
Kit-only: bootstrap.sh and sync.sh never install this file into a target repo.

The dotnet samples below are the documented MSBuild / VSTest formats. On the
work machine, replace them with a real capture:
    dotnet build -v q 2>&1 | grep -m1 error      (a compile error)
    dotnet test -v q 2>&1 | grep -m1 Failed      (a failing test)
and paste the lines into DOTNET_BUILD_SAMPLE / DOTNET_TEST_SAMPLE.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "kit" / ".claude" / "hooks"
sys.dont_write_bytecode = True
sys.path.insert(0, str(HOOKS))

import bash_guard  # noqa: E402
import gate_log  # noqa: E402

# Real captures, 2026-09-07: ruff 0.16.x `check --output-format concise`, pytest `-q -x` (uv-managed venv).
PYTEST_SAMPLE = """\
pytest:
F
================================== FAILURES ===================================
__________________________________ test_x _____________________________________

    def test_x():
>       assert False
E       assert False

tests/test_x.py:2: AssertionError
=========================== short test summary info ===========================
FAILED tests/test_x.py::test_x - assert False
!!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.01s
"""
PYTEST_VERBOSE_SAMPLE = "tests/test_y.py::test_two FAILED                                   [100%]\n"
PYTEST_COLLECT_SAMPLE = """\
ERROR tests/test_broken.py - ImportError: cannot import name 'x'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.05s
"""
RUFF_CONCISE_SAMPLE = """\
ruff:
s.py:1:1: I001 [*] Import block is un-sorted or un-formatted
s.py:1:8: F401 [*] `os` imported but unused
Found 2 errors.
[*] 2 fixable with the `--fix` option.
"""
RUFF_FULL_SAMPLE = """\
F401 [*] `os` imported but unused
 --> s.py:1:8
  |
1 | import os
  |        ^^
  |
help: Remove unused import: `os`
"""
# Documented formats — replace with a real capture on the work machine.
DOTNET_BUILD_SAMPLE = (
    "dotnet build:\n"
    "/home/u/App/src/Orders/Orders.cs(12,5): error CS0103: The name 'total' does not exist "
    "in the current context [/home/u/App/src/Orders/Orders.csproj]\n"
    "    0 Warning(s)\n    1 Error(s)\n\nTime Elapsed 00:00:03.21\n"
)
DOTNET_TEST_SAMPLE = (
    "dotnet test:\n"
    "  Failed Orders.Tests.TotalTest [12 ms]\n"
    "  Error Message:\n   Assert.Equal() Failure\n"
    "Failed!  - Failed:     1, Passed:    41, Skipped:     0, Total:    42, Duration: 1 s\n"
)
NETSDK_SAMPLE = "error NETSDK1045: The current .NET SDK does not support targeting .NET 9.0. [x.csproj]\n"
TIMEOUT_SAMPLE = "timed out after 540s (GATE_TIMEOUT budget): dotnet build App.sln\n"


class FailureKeys(unittest.TestCase):
    def test_pytest_short_summary(self):
        self.assertEqual(gate_log.failure_keys(PYTEST_SAMPLE), ["tests/test_x.py::test_x"])

    def test_pytest_verbose(self):
        self.assertEqual(gate_log.failure_keys(PYTEST_VERBOSE_SAMPLE), ["tests/test_y.py::test_two"])

    def test_pytest_collection_error(self):
        self.assertEqual(gate_log.failure_keys(PYTEST_COLLECT_SAMPLE), ["error:tests/test_broken.py"])

    def test_ruff_concise_all_findings(self):
        self.assertEqual(gate_log.failure_keys(RUFF_CONCISE_SAMPLE), ["ruff:I001:s.py", "ruff:F401:s.py"])

    def test_ruff_full_format_fallback(self):
        self.assertEqual(gate_log.failure_keys(RUFF_FULL_SAMPLE), ["ruff:F401:s.py"])

    def test_dotnet_build(self):
        self.assertEqual(
            gate_log.failure_keys(DOTNET_BUILD_SAMPLE),
            ["CS0103: The name 'total' does not exist in the current context"],
        )

    def test_dotnet_test(self):
        self.assertEqual(gate_log.failure_keys(DOTNET_TEST_SAMPLE), ["Orders.Tests.TotalTest"])

    def test_netsdk(self):
        keys = gate_log.failure_keys(NETSDK_SAMPLE)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("NETSDK1045: "))

    def test_timeout(self):
        self.assertEqual(gate_log.failure_keys(TIMEOUT_SAMPLE), ["timeout"])

    def test_all_keys_not_first(self):
        two = DOTNET_BUILD_SAMPLE + "x.cs(1,1): error CS0246: The type 'Foo' could not be found [p.csproj]\n"
        keys = gate_log.failure_keys(two)
        self.assertEqual(len(keys), 2)
        self.assertTrue(keys[1].startswith("CS0246"))

    def test_dedup(self):
        keys = gate_log.failure_keys(DOTNET_BUILD_SAMPLE + DOTNET_BUILD_SAMPLE)
        self.assertEqual(len(keys), 1)

    def test_fallback_is_stable_across_timings(self):
        a = gate_log.failure_keys("something odd\n1 failed, 3 passed in 0.12s\n")
        b = gate_log.failure_keys("something odd\n2 failed, 3 passed in 4.90s\n")
        self.assertEqual(a, b)
        self.assertTrue(a[0].startswith("raw:"))

    def test_fallback_prefers_error_line(self):
        text = "Build FAILED: missing thing\nTime Elapsed 00:00:03.21\n"
        self.assertEqual(gate_log.failure_keys(text), ["raw:Build FAILED: missing thing"])

    def test_fallback_last_line_when_no_error_word(self):
        self.assertEqual(gate_log.failure_keys("alpha\nbeta 42\n"), ["raw:beta"])

    def test_key_length_capped(self):
        long = "x.cs(1,1): error CS0001: " + "y" * 300 + " [p.csproj]\n"
        self.assertLessEqual(len(gate_log.failure_keys(long)[0]), gate_log.KEY_MAX)


class State(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_clear(self):
        self.assertEqual(gate_log.load_state(self.root)["strike"], 0)
        gate_log.save_state(self.root, {"keys": ["a"], "strike": 2, "times": ["t1", "t2"]})
        st = gate_log.load_state(self.root)
        self.assertEqual((st["keys"], st["strike"], st["times"]), (["a"], 2, ["t1", "t2"]))
        self.assertFalse(list(self.root.glob(".claude/.gate-state.*.tmp")), "temp file left behind")
        gate_log.clear_state(self.root)
        self.assertEqual(gate_log.load_state(self.root)["strike"], 0)
        gate_log.clear_state(self.root)  # idempotent

    def test_corrupt_reads_empty(self):
        (self.root / ".claude").mkdir()
        (self.root / gate_log.STATE_REL).write_text("{not json")
        self.assertEqual(gate_log.load_state(self.root), {"keys": [], "strike": 0, "times": []})
        (self.root / gate_log.STATE_REL).write_text(json.dumps({"keys": "nope", "strike": "3"}))
        self.assertEqual(gate_log.load_state(self.root)["strike"], 0)

    def test_log_append_and_read(self):
        gate_log.append_event(self.root, kind="prompt", phase="end", session="abc", agent=None)
        events = gate_log.read_events(self.root)
        self.assertEqual(len(events), 1)
        self.assertNotIn("agent", events[0])
        self.assertRegex(events[0]["ts"], r"[+-]\d\d:\d\d$")

    def test_repo_root_prefers_env(self):
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        try:
            os.environ["CLAUDE_PROJECT_DIR"] = str(self.root)
            self.assertEqual(gate_log.repo_root({"cwd": "/elsewhere"}), self.root)
            del os.environ["CLAUDE_PROJECT_DIR"]
            self.assertEqual(gate_log.repo_root({"cwd": "/elsewhere"}), Path("/elsewhere"))
        finally:
            if old is not None:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_edit_delta(self):
        self.assertEqual(gate_log.edit_delta({"old_string": "ab", "new_string": "abcd"}), 2)
        self.assertEqual(gate_log.edit_delta({"content": "12345"}), 5)
        self.assertEqual(gate_log.edit_delta({"edits": [{"old_string": "a", "new_string": "abc"}, {"old_string": "xyz", "new_string": ""}]}), -1)
        self.assertIsNone(gate_log.edit_delta({"file_path": "x"}))


class GuardMatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "orders.py").write_text("x = 1\n")
        (self.root / "src" / "Orders.cs").write_text("class A {}\n")
        self.guard = bash_guard.Guard(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def labels(self, command):
        return [(label, os.path.relpath(p, self.root) if p else None) for label, p in self.guard.find(command)]

    def test_sed_in_place_on_repo_file(self):
        self.assertEqual(self.labels("sed -i 's/a/b/' src/orders.py"), [("in-place edit", "src/orders.py")])

    def test_sed_combined_flags_and_expression_flag(self):
        self.assertEqual(self.labels("sed -ri -e 's/a/b/' src/orders.py"), [("in-place edit", "src/orders.py")])

    def test_sed_without_in_place_is_fine(self):
        self.assertEqual(self.labels("sed -n '1,5p' src/orders.py"), [])

    def test_sed_on_tmp_ignored(self):
        self.assertEqual(self.labels("sed -i 's/a/b/' /tmp/x.py"), [])

    def test_cd_then_relative_target(self):
        self.assertEqual(self.labels("cd src && sed -i 's/a/b/' orders.py"), [("in-place edit", "src/orders.py")])

    def test_missing_file_still_matches_path_inside_repo(self):
        # existence is the guard main()'s concern (Python lane only), not the matcher's
        self.assertEqual(self.labels("sed -i 's/a/b/' src/nope.py"), [("in-place edit", "src/nope.py")])

    def test_redirect_forms(self):
        self.assertEqual(self.labels("cat > src/notes.md <<EOF"), [("redirect into source", "src/notes.md")])
        self.assertEqual(self.labels("echo hi >> src/orders.py"), [("redirect into source", "src/orders.py")])
        self.assertEqual(self.labels("echo hi >src/orders.py"), [("redirect into source", "src/orders.py")])

    def test_redirect_to_dev_null_and_tmp_ignored(self):
        self.assertEqual(self.labels("dotnet build > /dev/null 2>&1"), [])
        self.assertEqual(self.labels("dotnet build > /tmp/out.txt"), [])

    def test_tee_truncate_mv_cp_rm(self):
        self.assertEqual(self.labels("echo x | tee -a src/orders.py"), [("tee into source", "src/orders.py")])
        self.assertEqual(self.labels("truncate -s 0 src/orders.py"), [("truncate source", "src/orders.py")])
        self.assertEqual(self.labels("mv a.cs src/Orders.cs"), [("file move/copy", "src/Orders.cs")])
        self.assertEqual(self.labels("cp -r x src/Orders.cs"), [("file move/copy", "src/Orders.cs")])
        self.assertEqual(self.labels("rm -rf src/Orders.cs"), [("delete source", "src/Orders.cs")])

    def test_perl_in_place(self):
        self.assertEqual(self.labels("perl -pi -e 's/a/b/' src/orders.py"), [("in-place edit", "src/orders.py")])

    def test_script_write(self):
        self.assertEqual(self.labels("""python3 -c "open('src/x.py','w').write('1')" """), [("script write", None)])

    def test_harmless_commands(self):
        for cmd in ("ls -la", "git status", "git commit -m 'x > y'", "dotnet build", "uv run pytest -q", "grep -rn foo src/"):
            self.assertEqual(self.labels(cmd), [], cmd)

    def test_outside_repo_ignored(self):
        self.assertEqual(self.labels("sed -i 's/a/b/' ../other/x.py"), [])

    def test_operators_attached_to_words(self):
        self.assertEqual(
            self.labels("rm src/Orders.cs; sed -i 's/a/b/' src/orders.py"),
            [("delete source", "src/Orders.cs"), ("in-place edit", "src/orders.py")],
        )
        self.assertEqual(self.labels("true&&sed -i 's/a/b/' src/orders.py"), [("in-place edit", "src/orders.py")])
        self.assertEqual(self.labels("dotnet build 2>&1 | tail -n 5"), [])

    def test_ampersand_redirect_forms(self):
        # review probes: `&>` and `2>&1` must not be split as list operators
        self.assertEqual(self.labels("echo x &> src/orders.py"), [("redirect into source", "src/orders.py")])
        self.assertEqual(self.labels("curl 'http://h/?a=1&b=2' > src/orders.py"), [("redirect into source", "src/orders.py")])
        self.assertEqual(self.labels("dotnet build > src/Orders.cs 2>&1"), [("redirect into source", "src/Orders.cs")])
        self.assertEqual(self.labels("sed -i 's/a/b/' src/orders.py &"), [("in-place edit", "src/orders.py")])

    def test_bare_truncation(self):
        self.assertEqual(self.labels("> src/orders.py"), [("redirect into source", "src/orders.py")])

    def test_dedup_two_targets(self):
        self.assertEqual(
            self.labels("sed -i 's/a/b/' src/orders.py src/Orders.cs"),
            [("in-place edit", "src/orders.py"), ("in-place edit", "src/Orders.cs")],
        )


if __name__ == "__main__":
    unittest.main()
