"""scripts/test.py, which runs the test modules in parallel, and the fake
commands helper it made worth having (tests/executables.py).

What matters most here is that a failure is never lost: CI reads the runner's
exit status, so a failing, erroring or crashing module must reach the summary
and make it non-zero.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.executables import write_executable

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "test.py"


def _load():
    """scripts/test.py as a module (it is a script, not a package member)."""
    spec = importlib.util.spec_from_file_location("harness_suite_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load()


def _flatten(suite):
    """Every test case in a suite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _outcome(module, *, tests=1, failures=(), errors=(), skipped=(),
             unexpected=(), seconds=1.0, output=""):
    outcome = runner.Outcome(module, seconds)
    outcome.tests = tests
    outcome.failures = [list(item) for item in failures]
    outcome.errors = [list(item) for item in errors]
    outcome.skipped = [list(item) for item in skipped]
    outcome.unexpected_successes = list(unexpected)
    outcome.output = output
    return outcome


class ModuleSelectionTests(unittest.TestCase):

    def test_the_default_modules_are_the_ones_discovery_finds(self):
        # The runner globs; `python -m unittest discover -s tests` walks. A
        # module either command misses would be a test nobody runs, so the
        # two lists have to be the same one.
        suite = unittest.defaultTestLoader.discover(str(REPO / "tests"),
                                                    top_level_dir=str(REPO))
        discovered = set()
        for test in _flatten(suite):
            name = type(test).__module__
            self.assertTrue(name.startswith("tests."), f"{name}: {test}")
            discovered.add(name)
        self.assertIn("tests.test_suite_runner", discovered)
        self.assertEqual(sorted(discovered), runner._modules([]))

    def test_a_module_may_be_named_three_ways(self):
        for name in ("tests.test_build", "test_build", "tests/test_build.py"):
            with self.subTest(name):
                self.assertEqual(["tests.test_build"], runner._modules([name]))

    def test_an_unknown_module_is_an_error(self):
        with self.assertRaisesRegex(SystemExit, "no test module tests/test_nope.py"):
            runner._modules(["test_nope"])

    def test_the_longest_module_of_the_last_run_starts_first(self):
        modules = ["tests.test_build", "tests.test_gui", "tests.test_plan"]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "durations.json"
            cache.write_text(json.dumps({"tests.test_gui": 9.0,
                                         "tests.test_build": 30.0}))
            with mock.patch.object(runner, "CACHE", cache):
                ordered = runner._longest_first(modules)
        # The two with a time first, longest first; the unknown one after them.
        self.assertEqual(["tests.test_build", "tests.test_gui", "tests.test_plan"],
                         ordered)

    def test_a_missing_or_damaged_cache_still_gives_an_order(self):
        modules = ["tests.test_build", "tests.test_gui"]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "durations.json"
            for content in (None, "{{{", '"not a mapping"'):
                with self.subTest(content):
                    if content is None:
                        cache.unlink(missing_ok=True)
                    else:
                        cache.write_text(content)
                    with mock.patch.object(runner, "CACHE", cache):
                        self.assertEqual(sorted(modules),
                                         sorted(runner._longest_first(modules)))


class ChildTests(unittest.TestCase):
    """What one module's child process reports back."""

    def test_a_passing_module_reports_its_tests(self):
        outcome = runner._start("tests.test_build", 1)
        self.assertEqual(0, outcome.problems)
        self.assertGreater(outcome.tests, 0)
        self.assertEqual(len(outcome.timings), outcome.tests)
        self.assertEqual("ok", outcome.status())

    def test_a_module_that_cannot_be_imported_is_an_error(self):
        outcome = runner._start("tests.test_build.NoSuchClass", 1)
        self.assertEqual("FAIL", outcome.status())
        self.assertTrue(outcome.errors)

    def test_a_module_that_hangs_is_stopped_and_reported(self):
        outcome = runner._start("tests.test_build", 1, timeout=0.001)
        self.assertEqual("FAIL", outcome.status())
        self.assertIn("did not finish within 0.001s", outcome.errors[0][1])

    def child(self, returncode, output="", errors="", report=None):
        """A stand-in child process, in place of a real one."""
        class Child:
            pid = os.getpid()  # never signaled: it ends on its own here

            def __init__(self, argv):
                self.argv = argv
                self.returncode = returncode

            def communicate(self, timeout=None):
                return output, errors

        def popen(argv, **_kwargs):
            if report is not None:
                path = argv[argv.index("--worker-result") + 1]
                Path(path).write_text(json.dumps(report))
            return Child(argv)

        return mock.patch.object(runner.subprocess, "Popen", popen)

    def test_stopping_a_module_takes_the_commands_it_started(self):
        # A hung module may have left a command of its own running, holding a
        # lock or a port; the whole process group goes.
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys, time\n"
             "started = subprocess.Popen([sys.executable, '-c', "
             "'import time; time.sleep(120)'])\n"
             "print(started.pid, flush=True)\n"
             "time.sleep(120)\n"],
            stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(child.kill)
        grandchild = int(child.stdout.readline())
        runner._stop([child])
        child.stdout.close()
        self.assertIsNotNone(child.poll())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"the command the module started ({grandchild}) is still running")

    def test_a_child_that_dies_without_a_result_is_an_error(self):
        with self.child(-11, errors="zsh: segmentation fault\n"):
            outcome = runner._start("tests.test_build", 1)
        self.assertEqual("FAIL", outcome.status())
        (_module, text), = outcome.errors
        self.assertIn("exited -11 without a result", text)
        self.assertIn("segmentation fault", text)

    def test_a_child_that_exits_oddly_with_passing_tests_is_an_error(self):
        # A module that passes its tests and then exits 3 (an atexit hook, a
        # stray sys.exit) must not pass quietly.
        report = {"module": "tests.test_build", "tests": 4,
                  "failures": [], "errors": [], "skipped": [],
                  "expected_failures": 0, "unexpected_successes": [],
                  "timings": []}
        with self.child(3, report=report):
            outcome = runner._start("tests.test_build", 1)
        self.assertEqual("FAIL", outcome.status())
        self.assertEqual(4, outcome.tests)
        self.assertIn("exited 3", outcome.errors[0][1])


class ReportTests(unittest.TestCase):

    def report(self, outcomes, durations=0, verbose=False):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            passed = runner._report(outcomes, 1.25, 4, durations, verbose)
        return passed, err.getvalue()

    def test_a_clean_run_passes_and_counts_its_tests(self):
        passed, text = self.report([_outcome("tests.a", tests=3),
                                    _outcome("tests.b", tests=4,
                                             skipped=[("b.test_x", "no board")])])
        self.assertTrue(passed)
        self.assertIn("Ran 7 tests in 1.250s (2 modules, 4 at a time)", text)
        self.assertIn("OK (skipped=1)", text)

    def test_a_failure_is_printed_with_its_module_output_and_fails_the_run(self):
        outcome = _outcome("tests.a", tests=2,
                           failures=[("test_x (tests.a.A.test_x)", "AssertionError: 1 != 2")],
                           output="a warning from the module\n")
        passed, text = self.report([outcome, _outcome("tests.b", tests=1)])
        self.assertFalse(passed)
        self.assertIn("FAIL: test_x (tests.a.A.test_x)", text)
        self.assertIn("AssertionError: 1 != 2", text)
        self.assertIn("a warning from the module", text)
        self.assertIn("FAILED (failures=1)", text)

    def test_an_error_or_an_unexpected_success_fails_the_run(self):
        for outcome in (_outcome("tests.a", errors=[("tests.a", "ImportError")]),
                        _outcome("tests.a", unexpected=["test_y (tests.a.A.test_y)"])):
            with self.subTest(outcome.status()):
                passed, text = self.report([outcome])
                self.assertFalse(passed)
                self.assertIn("FAILED", text)

    def test_a_passing_module_keeps_its_output_to_itself_unless_verbose(self):
        passed, quiet = self.report([_outcome("tests.a", output="chatter\n")])
        self.assertTrue(passed)
        self.assertNotIn("chatter", quiet)
        _passed, loud = self.report([_outcome("tests.a", output="chatter\n")],
                                    verbose=True)
        self.assertIn("chatter", loud)

    def test_durations_name_the_slowest_tests_and_modules(self):
        slow = _outcome("tests.slow", tests=2, seconds=9.5)
        slow.timings = [["tests.slow.A.test_long", 7.5], ["tests.slow.A.test_short", 0.5]]
        fast = _outcome("tests.fast", tests=1, seconds=0.25)
        fast.timings = [["tests.fast.A.test_x", 0.2]]
        _passed, text = self.report([slow, fast], durations=2)
        self.assertIn("7.500s    tests.slow.A.test_long", text)
        self.assertIn("9.500s    tests.slow", text)
        order = text.index("tests.slow.A.test_long"), text.index("tests.slow.A.test_short")
        self.assertLess(*order)


class ExecutableFixtureTests(unittest.TestCase):
    """tests/executables.py, which the fakes on PATH are written with."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def test_the_same_fake_twice_is_one_file_and_runs(self):
        first = write_executable(self.directory / "a" / "tool", "#!/bin/sh\nexit 7\n")
        second = write_executable(self.directory / "b" / "tool", "#!/bin/sh\nexit 7\n")
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)
        self.assertEqual(7, subprocess.run([str(second)], timeout=30).returncode)

    def test_writing_over_a_fake_leaves_the_other_copies_alone(self):
        path = write_executable(self.directory / "tool", "#!/bin/sh\nexit 7\n")
        write_executable(path, "#!/bin/sh\nexit 8\n")
        again = write_executable(self.directory / "other" / "tool",
                                 "#!/bin/sh\nexit 7\n")
        self.assertEqual(8, subprocess.run([str(path)], timeout=30).returncode)
        self.assertEqual(7, subprocess.run([str(again)], timeout=30).returncode)

    def test_a_link_a_test_changed_is_written_again_and_left_alone(self):
        # chmod and a write go to the one file behind every link, so the next
        # caller gets a fresh one, and what the test did to its own copy
        # stays done.
        path = write_executable(self.directory / "tool", "#!/bin/sh\nexit 7\n")
        path.chmod(0o644)
        again = write_executable(self.directory / "other" / "tool",
                                 "#!/bin/sh\nexit 7\n")
        self.assertEqual(0o755, again.stat().st_mode & 0o777)
        self.assertEqual(7, subprocess.run([str(again)], timeout=30).returncode)
        self.assertNotEqual(path.stat().st_ino, again.stat().st_ino)
        self.assertEqual(0o644, path.stat().st_mode & 0o777)

    def test_content_and_mode_are_what_was_asked_for(self):
        path = write_executable(self.directory / "tool", "#!/bin/sh\nexit 0\n",
                                mode=0o700)
        self.assertEqual("#!/bin/sh\nexit 0\n", path.read_text())
        self.assertEqual(0o700, path.stat().st_mode & 0o777)


class EndToEndTests(unittest.TestCase):

    def run_runner(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), *args], cwd=REPO,
                              capture_output=True, text=True, timeout=300)

    def test_a_module_run_through_the_runner_passes(self):
        result = self.run_runner("-j", "2", "--durations", "3", "test_build",
                                 "test_plan")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("running 2 test modules, 2 at a time", result.stderr)
        self.assertIn("Slowest test durations", result.stderr)
        self.assertRegex(result.stderr, r"(?m)^OK$")

    def test_a_child_that_cannot_load_its_module_exits_1(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_runner("--worker", "tests.test_build.NoSuchClass",
                                     "--worker-result",
                                     str(Path(directory) / "result.json"))
            self.assertEqual(1, result.returncode, result.stderr)
            report = json.loads((Path(directory) / "result.json").read_text())
        self.assertEqual(1, len(report["errors"]), report)

    def test_an_unknown_module_exits_1_without_running_anything(self):
        result = self.run_runner("test_nope")
        self.assertEqual(1, result.returncode)
        self.assertIn("no test module tests/test_nope.py", result.stderr)
        self.assertNotIn("running", result.stderr)

    def test_jobs_must_be_a_real_count(self):
        result = self.run_runner("-j", "0", "test_build")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("1 test modules, 1 at a time", result.stderr)
        result = self.run_runner("-j", "-2", "test_build")
        self.assertEqual(1, result.returncode)
        self.assertIn("--jobs must be 1 or more", result.stderr)


if __name__ == "__main__":
    unittest.main()
