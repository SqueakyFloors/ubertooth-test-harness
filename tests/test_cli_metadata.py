import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.hostneeds import (
    ALWAYS_BUILT_TOOLS, BLUEZ_TOOLS, HostCapabilities,
)
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from tests.test_sequencer import CollectSink, run_contract

VERSION = "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"


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
        if command[1:] == ("-V",):
            text = "garbage\n" if tool in self.broken else VERSION
            return CommandResult(command, 0, text, "", 0.01, False)
        if command[1:] == ("-h",):
            code = 0
            return CommandResult(command, code, f"{tool} - usage\n", "", 0.01, False)
        raise AssertionError(f"unexpected command {command}")


LINUX_ALL = HostCapabilities("Host_Linux", "linux",
                             frozenset({"ubertooth-scan", "ubertooth-follow"}), basis="build")
MACOS = HostCapabilities("Host_Linux", "macos", frozenset(), basis="build")


def run_surface(executor, capabilities=LINUX_ALL):
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
            preflight_checks=checks, capabilities=capabilities,
        )
        return summary, sink.events


def finished(events):
    return {
        e.details["case_id"].removeprefix("001-Host CLI surface-"): e
        for e in events if e.event_type == "check_finished" and e.details.get("phase") == "test"
    }


class HostCliSurfaceTests(unittest.TestCase):

    def test_every_host_tool_is_asked_its_usage_and_its_version(self):
        # Every one of the fork's host tools answers -V through the one
        # libubertooth function and ends the command; four of the thirteen
        # used to be asked (#347). The tools come from the build's own list,
        # so a tool the fork adds gets both cases without a second list to
        # edit: that is the drift this asserts, rather than a copy of the
        # names.
        cases = builtin_catalog().get("Host CLI surface").command_cases({}, {}, {})
        by_id = {case.case_id: case for case in cases}
        tools = ALWAYS_BUILT_TOOLS + BLUEZ_TOOLS

        self.assertEqual(
            [f"{tool}-help" for tool in tools] + [f"{tool}-version" for tool in tools],
            [case.case_id for case in cases],
        )
        for tool in tools:
            with self.subTest(tool):
                self.assertEqual((tool, "-h"), by_id[f"{tool}-help"].argv)
                version = by_id[f"{tool}-version"]
                self.assertEqual((tool, "-V"), version.argv)
                self.assertTrue(re.search(version.output_patterns[0], VERSION))
                # A board that cannot be read makes -V exit 1 after printing
                # the host line, and may not even be a board this run uses.
                self.assertEqual((0, 1), version.expected_returncodes)
        # Both cases of a BlueZ-only tool carry the need, so a host without
        # BlueZ leaves out its help and its version together.
        for tool in BLUEZ_TOOLS:
            for case_id in (f"{tool}-help", f"{tool}-version"):
                self.assertEqual((tool,), by_id[case_id].needs.bluez_tools)
        for tool in ALWAYS_BUILT_TOOLS:
            for case_id in (f"{tool}-help", f"{tool}-version"):
                self.assertFalse(by_id[case_id].needs)

    def test_a_board_that_cannot_be_read_does_not_fail_the_version_cases(self):
        class BusyBoard(SurfaceTools):
            def run(self, argv, **kwargs):
                result = super().run(argv, **kwargs)
                if tuple(argv)[1:] == ("-V",):
                    return replace(
                        result, returncode=1,
                        stdout=result.stdout + "Firmware version: unavailable, "
                               "in use by another program\n",
                    )
                return result

        summary, _events = run_surface(BusyBoard(), MACOS)

        self.assertTrue(summary.passed, summary.failure_reason)

    def test_bluez_tools_are_not_applicable_on_macos_not_skipped(self):
        executor = SurfaceTools()
        summary, events = run_surface(executor, MACOS)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(0, summary.skipped_cases)
        # Help and version for each of the two tools macOS has not got.
        self.assertEqual(4, summary.not_applicable_cases)
        results = finished(events)
        left_out = [e for e in events if e.event_type == "not_applicable"]
        self.assertEqual(["ubertooth-scan -h (BlueZ builds)",
                          "ubertooth-follow -h (BlueZ builds)",
                          "ubertooth-scan -V (BlueZ builds)",
                          "ubertooth-follow -V (BlueZ builds)"],
                         [e.details["case_name"] for e in left_out])
        for event in left_out:
            self.assertEqual("not_applicable", event.status)
            self.assertIn("BlueZ (libbluetooth), which macOS does not have",
                          event.details["reason"])
        for tool in ("ubertooth-scan", "ubertooth-follow"):
            # Never started: no check, no command.
            self.assertNotIn(f"{tool}-help", results)
            self.assertNotIn(f"{tool}-version", results)
            self.assertFalse([c for c in executor.commands if c[0] == tool])
        test = next(e for e in events if e.event_type == "test_finished")
        self.assertEqual(("passed", 4, 0), (test.status, test.details["not_applicable_cases"],
                                            test.details["skipped_cases"]))

    def test_a_linux_run_without_one_bluez_tool_leaves_out_only_its_cases(self):
        capabilities = HostCapabilities("Host_Linux", "linux", frozenset({"ubertooth-scan"}),
                                        basis="installed tools")
        summary, events = run_surface(SurfaceTools(missing=("ubertooth-follow",)), capabilities)

        self.assertTrue(summary.passed, summary.failure_reason)
        # Its help and its version; the tool that is there keeps both.
        self.assertEqual(2, summary.not_applicable_cases)
        results = finished(events)
        self.assertEqual("passed", results["ubertooth-scan-help"].status)
        self.assertEqual("passed", results["ubertooth-scan-version"].status)
        left_out = [e for e in events if e.event_type == "not_applicable"]
        self.assertEqual(["ubertooth-follow-help", "ubertooth-follow-version"],
                         [e.details["case_id"].rsplit("surface-", 1)[-1]
                          for e in left_out])
        for event in left_out:
            self.assertIn("ubertooth-follow is not in this run's tools (installed tools)",
                          event.details["reason"])

    def test_a_declared_bluez_tool_that_is_missing_fails(self):
        # The capabilities say the run has it; a missing file is a failure.
        summary, events = run_surface(SurfaceTools())

        self.assertFalse(summary.passed)
        self.assertEqual("failed", finished(events)["ubertooth-follow-help"].status)

    def test_a_missing_required_tool_still_fails(self):
        summary, events = run_surface(SurfaceTools(missing=("ubertooth-rx",)))

        self.assertFalse(summary.passed)
        results = finished(events)
        self.assertEqual("failed", results["ubertooth-rx-help"].status)
        self.assertEqual("passed", results["ubertooth-scan-help"].status)

    def test_a_present_but_unusable_bluez_tool_fails(self):
        capabilities = HostCapabilities("Host_Linux", "linux", frozenset({"ubertooth-scan"}),
                                        basis="installed tools")
        summary, events = run_surface(
            SurfaceTools(missing=("ubertooth-follow",), unusable=("ubertooth-scan",)),
            capabilities)

        self.assertFalse(summary.passed)
        results = finished(events)
        self.assertEqual("failed", results["ubertooth-scan-help"].status)
        self.assertNotIn("ubertooth-follow-help", results)

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
