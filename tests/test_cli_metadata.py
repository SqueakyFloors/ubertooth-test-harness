import re
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from tests.test_sequencer import CollectSink, run_contract

VERSION = "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"


class SurfaceTools:
    """Host tools as built without BlueZ: scan and follow do not exist."""

    def __init__(self, missing=("ubertooth-scan", "ubertooth-follow"), broken=(),
                 unusable=()):
        self.missing = set(missing) - set(unusable)
        self.unusable = set(unusable)
        self.broken = set(broken)
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        tool = command[0]
        if tool in self.missing:
            # What SubprocessCommandExecutor returns when exec fails.
            return CommandResult(
                command, 127, "",
                f"unable to start command: [Errno 2] No such file or directory: '{tool}'\n",
                0.0, False)
        if tool in self.unusable:
            return CommandResult(
                command, 127, "",
                f"unable to start command: [Errno 13] Permission denied: '{tool}'\n",
                0.0, False)
        if command[1:] in (("-H",), ("--host-version",), ("-V",)):
            text = "garbage\n" if tool in self.broken else VERSION
            return CommandResult(command, 0, text, "", 0.01, False)
        if command[1:] == ("-h",):
            code = 0 if tool not in ("ubertooth-rx", "ubertooth-btle", "ubertooth-dfu",
                                     "ubertooth-scan", "ubertooth-follow") else 1
            return CommandResult(command, code, f"{tool} - usage\n", "", 0.01, False)
        raise AssertionError(f"unexpected command {command}")


def run_surface(executor):
    definition = builtin_catalog().get("Host CLI surface")
    arguments = definition.normalize_arguments({"hosts": ["Host_Linux"]})
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation("001-Host CLI surface", "Host CLI surface",
                                    "Host_Linux", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


def finished(events):
    return {
        e.details["case_id"].removeprefix("001-Host CLI surface-"): e
        for e in events if e.event_type == "check_finished" and e.details.get("phase") == "test"
    }


class HostCliSurfaceTests(unittest.TestCase):

    def test_version_cases_and_optional_bluez_help(self):
        cases = builtin_catalog().get("Host CLI surface").command_cases({}, {}, {})
        by_id = {case.case_id: case for case in cases}
        self.assertEqual(("ubertooth-util", "--host-version"),
                         by_id["ubertooth-util-host-version-long"].argv)
        for tool in ("ubertooth-rx", "ubertooth-tx", "ubertooth-afh"):
            self.assertEqual((tool, "-V"), by_id[f"{tool}-version"].argv)
            self.assertTrue(re.search(by_id[f"{tool}-version"].output_patterns[0], VERSION))
        for tool in ("ubertooth-scan", "ubertooth-follow"):
            self.assertTrue(by_id[f"{tool}-help"].optional_tool)

    def test_tools_not_built_are_skipped_not_failed(self):
        summary, events = run_surface(SurfaceTools())

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(2, summary.skipped_cases)
        results = finished(events)
        for tool in ("ubertooth-scan", "ubertooth-follow"):
            self.assertEqual("skipped", results[f"{tool}-help"].status)
            self.assertIn("not built on this host", results[f"{tool}-help"].details["reason"])

    def test_a_missing_required_tool_still_fails(self):
        summary, events = run_surface(SurfaceTools(missing=("ubertooth-rx",)))

        self.assertFalse(summary.passed)
        results = finished(events)
        self.assertEqual("failed", results["ubertooth-rx-help"].status)
        self.assertEqual("passed", results["ubertooth-scan-help"].status)

    def test_a_present_but_unusable_optional_tool_fails(self):
        summary, events = run_surface(SurfaceTools(unusable=("ubertooth-scan",)))

        self.assertFalse(summary.passed)
        results = finished(events)
        self.assertEqual("failed", results["ubertooth-scan-help"].status)
        self.assertEqual("skipped", results["ubertooth-follow-help"].status)

    def test_a_wrong_version_line_fails(self):
        summary, events = run_surface(SurfaceTools(broken=("ubertooth-tx",)))

        self.assertFalse(summary.passed)
        self.assertEqual("failed", finished(events)["ubertooth-tx-version"].status)


class UbertoothCountTests(unittest.TestCase):

    def count_case(self, identities):
        cases = builtin_catalog().get("Ubertooth count").command_cases({}, {}, identities)
        self.assertEqual(1, len(cases))
        return cases[0]

    def test_expects_the_selected_board_plus_every_guard(self):
        one = self.count_case({"Ubertooth": {"serial": "a"}})
        two = self.count_case({"Ubertooth": {"serial": "a"},
                               "guard Ubertooth 1": {"serial": "b"}})
        self.assertEqual(("ubertooth-util", "-N"), one.argv)
        self.assertRegex("1\n", one.output_patterns[0])
        self.assertNotRegex("2\n", one.output_patterns[0])
        self.assertRegex("2\n", two.output_patterns[0])
        self.assertNotRegex("12\n", two.output_patterns[0])

    def test_guards_every_other_board(self):
        self.assertTrue(builtin_catalog().get("Ubertooth count").guards_other_ubertooths)


if __name__ == "__main__":
    unittest.main()
