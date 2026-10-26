"""What a case needs from its host, decided before the run (hostneeds.py)."""

import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ubertooth_harness.cases import CommandCase
from ubertooth_harness.catalog import (
    HOSTS,
    ArgumentRule,
    TestCatalog,
    TestDefinition,
    _platforms,
    builtin_catalog,
)
from ubertooth_harness.cli import main
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.hostneeds import (
    BLUEZ_TOOLS,
    HostCapabilities,
    Needs,
    NotApplicable,
    planned_capabilities,
    platform_capabilities,
    run_capabilities,
    split_cases,
    tool_needs,
    unmet,
)
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError, TestInvocation
from ubertooth_harness.report import render_run
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.sequencer import NamedTestSequencer
from tests.test_catalog_resources import HARDWARE
from tests.test_cli import make_source
from tests.test_sequencer import CollectSink, run_contract

MAC = HostCapabilities("Host_Mac", "macos", frozenset(), False, False, basis="build")
LINUX = HostCapabilities("Host_Linux", "linux", frozenset(BLUEZ_TOOLS), True, True,
                         basis="build")


class NeedsTests(unittest.TestCase):

    def test_declarations_are_checked(self):
        self.assertFalse(Needs())
        self.assertTrue(Needs(python_tools=True))
        with self.assertRaisesRegex(ValueError, "unknown platform"):
            Needs(platforms=("windows",))
        with self.assertRaisesRegex(ValueError, "not a BlueZ-only tool"):
            Needs(bluez_tools=("ubertooth-util",))
        self.assertEqual(Needs(bluez_tools=("ubertooth-follow",)), tool_needs("ubertooth-follow"))
        self.assertFalse(tool_needs("ubertooth-util"))
        self.assertEqual("ubertooth-scan (BlueZ), firmware build",
                         Needs(bluez_tools=("ubertooth-scan",), firmware_build=True).describe())

    def test_each_need_against_a_host(self):
        follow = Needs(bluez_tools=("ubertooth-follow",))
        self.assertIn("built only with BlueZ (libbluetooth), which macOS does not have",
                      unmet(follow, MAC))
        self.assertEqual("", unmet(follow, LINUX))
        partial = HostCapabilities("Host_Linux", "linux", frozenset({"ubertooth-scan"}),
                                   basis="installed tools")
        self.assertIn("ubertooth-follow is not in this run's tools (installed tools)",
                      unmet(follow, partial))
        self.assertIn("runs only on Linux, and Host_Mac is macOS",
                      unmet(Needs(platforms=("linux",)), MAC))
        self.assertIn("Python tools build", unmet(Needs(python_tools=True), MAC))
        self.assertIn("firmware build", unmet(Needs(firmware_build=True), MAC))
        self.assertEqual("", unmet(Needs(python_tools=True, firmware_build=True), LINUX))
        self.assertEqual("", unmet(follow, None))

    def test_not_known_yet_counts_as_met_except_the_platform(self):
        # Plan validation: only the platform is known.
        linux = platform_capabilities("Host_Linux", "linux")
        self.assertEqual("", unmet(Needs(bluez_tools=BLUEZ_TOOLS, python_tools=True,
                                         firmware_build=True), linux))
        mac = platform_capabilities("Host_Mac", "macos")
        self.assertNotEqual("", unmet(Needs(bluez_tools=("ubertooth-scan",)), mac))
        self.assertEqual("", unmet(Needs(python_tools=True), mac))

    def test_planned_capabilities_from_the_run_options(self):
        mac = planned_capabilities("Host_Mac", "macos", build=True, with_python=True,
                                   with_firmware=False)
        self.assertEqual((frozenset(), True, False), (mac.bluez_tools, mac.python_tools,
                                                      mac.firmware_build))
        # A Linux build configures with REQUIRE_ALL=ON: every BlueZ tool or no build.
        linux = planned_capabilities("Host_Linux", "linux", build=True, with_python=False,
                                     with_firmware=True)
        self.assertEqual(frozenset(BLUEZ_TOOLS), linux.bluez_tools)
        self.assertTrue(linux.firmware_build)
        artifact = planned_capabilities("Host_Mac", "macos", build=False, with_python=False,
                                        with_firmware=False, artifact_labels=("test_build",))
        self.assertTrue(artifact.firmware_build)
        self.assertEqual("installed tools", artifact.basis)

    def test_installed_linux_tools_come_from_path(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "ubertooth-scan"
            tool.write_text("#!/bin/sh\n")
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
            linux = planned_capabilities("Host_Linux", "linux", build=False,
                                         with_python=False, with_firmware=False,
                                         path=directory)
            self.assertEqual(frozenset({"ubertooth-scan"}), linux.bluez_tools)
            # macOS never has them, whatever is on PATH.
            mac = run_capabilities("Host_Mac", "macos", None, (), path=directory)
            self.assertEqual(frozenset(), mac.bluez_tools)

    def test_run_capabilities_from_the_manifest(self):
        build = {"python": "/usr/bin/python3"}
        names = ("built ubertooth-util", "built ubertooth-follow", "test build")
        linux = run_capabilities("Host_Linux", "linux", build, names)
        # A Linux build has both BlueZ tools whatever the manifest lists:
        # build.py fails a build without them, so their cases are required.
        self.assertEqual((frozenset(BLUEZ_TOOLS), True, True, "build"),
                         (linux.bluez_tools, linux.python_tools, linux.firmware_build,
                          linux.basis))
        self.assertEqual([], split_cases(
            builtin_catalog().get("Host CLI surface").command_cases({}, {}, {}),
            linux)[1])
        mac = run_capabilities("Host_Mac", "macos", {"python": None}, ("built ubertooth-util",))
        self.assertEqual((frozenset(), False, False),
                         (mac.bluez_tools, mac.python_tools, mac.firmware_build))
        self.assertEqual(
            {"host": "Host_Mac", "platform": "macos", "basis": "build", "bluez_tools": [],
             "python_tools": False, "firmware_build": False}, mac.to_dict())

    def test_split_cases_keeps_order(self):
        cases = builtin_catalog().get("Host CLI surface").command_cases({}, {}, {})
        runnable, left_out = split_cases(cases, MAC)
        self.assertEqual(len(cases) - 2, len(runnable))
        self.assertEqual(["ubertooth-scan-help", "ubertooth-follow-help"],
                         [case.case_id for case, _reason in left_out])
        self.assertEqual((tuple(cases), []), split_cases(cases, LINUX))


def _bluez_only_catalog():
    """Real catalog plus a test whose every case runs ubertooth-follow."""
    def builder(_arguments, _resources, _identities):
        return (CommandCase("follow-help", "follow help", ("ubertooth-follow", "-h"),
                            ("usage",), expected_returncodes=(1,),
                            needs=tool_needs("ubertooth-follow")),)
    bluez = TestDefinition(
        "BlueZ only", "every case needs ubertooth-follow",
        {"hosts": HOSTS, "platforms": ArgumentRule(_platforms, default=None)},
        command_builder=builder,
    )
    linux = TestDefinition(
        "Linux only", "a test only Linux runs",
        {"hosts": HOSTS}, command_builder=lambda *_: (
            CommandCase("util-help", "util help", ("ubertooth-util", "-h"), ("usage",)),),
        needs=Needs(platforms=("linux",)),
    )
    builtin = builtin_catalog()
    return TestCatalog([builtin.get(name) for name in builtin.names] + [bluez, linux])


class PlanningTests(unittest.TestCase):

    def compile(self, plan_text, suite="fast"):
        catalog = _bluez_only_catalog()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(HARDWARE)
            (root / "plan.yaml").write_text(plan_text)
            plan = load_plan(root / "plan.yaml", catalog)
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            return PlanCompiler(catalog, registry).compile_suite(plan, suite)

    def test_a_platform_the_plan_names_leaves_other_hosts_out(self):
        compiled = self.compile(
            "plan: p\nsuites:\n  fast:\n    - BlueZ only:\n        hosts: each\n"
            "        platforms: [linux]\n")
        self.assertEqual(["Host_Linux"], [i.host for i in compiled.invocations])
        (item,) = compiled.for_host("Host_Mac")
        self.assertEqual(("BlueZ only", "", ""), (item.test_name, item.invocation_id,
                                                  item.case_id))
        self.assertIn("runs only on Linux, and Host_Mac is macOS", item.reason)
        self.assertEqual((), compiled.for_host("Host_Linux"))

    def test_a_test_declaring_a_platform_is_left_out_elsewhere(self):
        compiled = self.compile(
            "plan: p\nsuites:\n  fast:\n    - Linux only:\n        hosts: each\n")
        self.assertEqual(["Host_Linux"], [i.host for i in compiled.invocations])
        self.assertEqual(["Host_Mac"], [item.host for item in compiled.not_applicable])

    def test_a_test_no_named_host_can_run_is_a_plan_error(self):
        with self.assertRaisesRegex(PlanningError, "Linux only: no selected host can run it: "
                                                   "Host_Mac: runs only on Linux"):
            self.compile("plan: p\nsuites:\n  fast:\n    - Linux only:\n"
                         "        hosts: [Host_Mac]\n")
        # Every case needs BlueZ and the only host is macOS.
        with self.assertRaisesRegex(PlanningError, "BlueZ only: no case can run on any "
                                                   "selected host: Host_Mac: ubertooth-follow"):
            self.compile("plan: p\nsuites:\n  fast:\n    - BlueZ only:\n"
                         "        hosts: [Host_Mac]\n")

    def test_a_test_with_some_case_for_the_host_compiles(self):
        compiled = self.compile(
            "plan: p\nsuites:\n  fast:\n    - BlueZ only:\n        hosts: each\n"
            "    - Host CLI surface:\n        hosts: [Host_Mac]\n")
        # BlueZ only still compiles for the Mac (Linux can run it); the
        # sequencer leaves its case out there.
        self.assertEqual([("BlueZ only", "Host_Linux"), ("BlueZ only", "Host_Mac"),
                          ("Host CLI surface", "Host_Mac")],
                         [(i.test_name, i.host) for i in compiled.invocations])
        self.assertEqual((), compiled.not_applicable)

    def test_the_host_plan_compiles_on_both_platforms(self):
        plan_path = Path(__file__).resolve().parents[1] / "testplans" / "host-only.yaml"
        if not plan_path.exists():
            self.skipTest("no testplans/host-only.yaml")
        catalog = builtin_catalog()
        plan = load_plan(plan_path, catalog)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "hardware.yaml").write_text(HARDWARE)
            registry = FileResourceRegistry.load(Path(directory) / "hardware.yaml")
            for suite in plan.suites:
                compiled = PlanCompiler(catalog, registry).compile_suite(plan, suite)
                self.assertTrue(compiled.invocations)


class FollowHelp:
    """ubertooth-util -H for preflight, ubertooth-util -h for the test."""

    def __init__(self):
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command[0] == "ubertooth-follow":
            raise AssertionError("a not-applicable case ran")
        text = "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
        if command == ("ubertooth-util", "-h"):
            text = "usage\n"
        return CommandResult(command, 0, text, "", 0.01, False)


class SequencerTests(unittest.TestCase):

    def run_suite(self, invocations, capabilities, not_applicable=()):
        catalog = _bluez_only_catalog()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, checks = run_contract(root, invocations)
            sink = CollectSink()
            executor = FollowHelp()
            summary = NamedTestSequencer(executor, catalog=catalog, event_sink=sink).run(
                invocations, root, run_id="named-run", manifest=manifest,
                preflight_checks=checks, capabilities=capabilities,
                not_applicable=not_applicable,
            )
            records = [json.loads(line) for line in (root / "run.jsonl").read_text().splitlines()]
            report = (root / "report.md").read_text()
        return summary, sink.events, records, report, executor

    def test_a_test_with_no_case_for_the_host_is_not_applicable_not_failed(self):
        invocations = (
            TestInvocation("001-BlueZ only", "BlueZ only", "Host_Mac", {"hosts": "each"}, {}, {}),
            TestInvocation("002-Linux only", "Linux only", "Host_Mac", {"hosts": "each"}, {}, {}),
        )
        planner_item = NotApplicable("Host_Mac", "Host version", "runs only on Linux, and "
                                     "Host_Mac is macOS")
        summary, events, records, report, executor = self.run_suite(
            invocations, MAC, (planner_item,))

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual((1, 2, 1, 0), (summary.completed_tests, summary.not_applicable_tests,
                                        summary.not_applicable_cases, summary.skipped_cases))
        self.assertFalse([c for c in executor.commands if c[0] == "ubertooth-follow"])
        kinds = [e.event_type for e in events]
        # Recorded after the manifest, before preflight or any test.
        self.assertLess(kinds.index("host_capabilities"), kinds.index("preflight_started"))
        left_out = [e for e in events if e.event_type == "not_applicable"]
        self.assertEqual(["Host version", "BlueZ only"], [e.details["test_name"] for e in left_out])
        self.assertEqual("001-BlueZ only-follow-help", left_out[1].details["case_id"])
        finished = {e.details["test_name"]: e for e in events if e.event_type == "test_finished"}
        self.assertEqual("not_applicable", finished["BlueZ only"].status)
        self.assertIn("not applicable on Host_Mac: ubertooth-follow is built only with BlueZ",
                      finished["BlueZ only"].details["reason"])
        self.assertEqual("passed", finished["Linux only"].status)
        # The same records reach run.jsonl.
        self.assertEqual(2, sum(1 for r in records if r["event_type"] == "not_applicable"))
        capabilities = next(r for r in records if r["event_type"] == "host_capabilities")
        self.assertEqual("macos", capabilities["details"]["capabilities"]["platform"])
        self.assertEqual(2, capabilities["details"]["not_applicable"])

        # report.md: the result line, the table column, a section and the transcript.
        self.assertIn("1 cases and 2 tests not applicable on this host", report)
        self.assertIn("| Host can run | macos (build); BlueZ tools: none; Python tools build: "
                      "no; firmware build: no |", report)
        self.assertIn("| Not applicable cases |", report)
        self.assertIn("| 001-BlueZ only | Host_Mac | not_applicable | 0 | 0 | 1 |", report)
        section = report.split("## Not applicable", 1)[1].split("## Transcript", 1)[0]
        self.assertIn("| Host version | (whole test) | Host_Mac | runs only on Linux", section)
        self.assertIn("| 001-BlueZ only | follow help | Host_Mac | ubertooth-follow is built "
                      "only with BlueZ", section)
        self.assertIn("[M] not applicable on Host_Mac: BlueZ only / follow help: ", report)

    def test_the_registry_platform_and_manifest_decide_by_default(self):
        invocation = TestInvocation("001-BlueZ only", "BlueZ only", "Host_Mac",
                                    {"hosts": "each"}, {}, {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hw.yaml").write_text(HARDWARE)
            registry = FileResourceRegistry.load(root / "hw.yaml")
            manifest, checks = run_contract(root, invocation)
            summary = NamedTestSequencer(
                FollowHelp(), catalog=_bluez_only_catalog(), event_sink=CollectSink(),
                registry=registry,
            ).run((invocation,), root, run_id="named-run", manifest=manifest,
                  preflight_checks=checks)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual((1, 1), (summary.not_applicable_tests, summary.not_applicable_cases))

    def test_a_report_without_not_applicable_items_has_no_section(self):
        invocation = TestInvocation("001-Linux only", "Linux only", "Host_Linux",
                                    {"hosts": "each"}, {}, {})
        _summary, events, _records, report, _executor = self.run_suite((invocation,), LINUX)
        self.assertNotIn("## Not applicable", report)
        self.assertNotIn("not applicable on this host", render_run(
            [e.to_dict() for e in events], transcript=False))


class DryRunTests(unittest.TestCase):

    def dry_run(self, *extra):
        platform = "macos" if sys.platform == "darwin" else "linux"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = make_source(root)
            plan = root / "plan.yaml"
            plan.write_text(
                "plan: p\nsuites:\n  fast:\n    - Host CLI surface:\n        hosts: each\n"
                "    - DFU image checks:\n        hosts: each\n        coverage: smoke\n")
            hardware = root / "hardware.yaml"
            hardware.write_text(
                f"resources:\n  Host_Local:\n    type: Host\n    platform: {platform}\n"
                "    roles: [test target]\n")
            image = root / "fw.dfu"
            image.write_bytes(b"fw")
            output = root / "runs"
            out = io.StringIO()
            # No installed BlueZ tools on a Linux host either (git stays on PATH).
            with (patch("ubertooth_harness.hostneeds.shutil.which", return_value=None),
                  redirect_stdout(out), redirect_stderr(io.StringIO())):
                code = main([
                    "run-suite", "--dry-run", "--plan", str(plan), "--suite", "fast",
                    "--hardware", str(hardware), "--host", "Host_Local",
                    "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                    "--output", str(output), "--lease-dir", str(root / "leases"),
                    *[value.replace("IMAGE", str(image)) for value in extra],
                ])
            self.assertFalse(output.exists(), "a dry run writes no run directory")
            self.assertFalse((root / "leases").exists() and any((root / "leases").iterdir()))
        return code, out.getvalue()

    def test_dry_run_lists_what_is_not_applicable_and_runs_nothing(self):
        code, text = self.dry_run()
        self.assertEqual(0, code)
        self.assertIn("nothing is built or run", text)
        self.assertIn("host: Host_Local (", text)
        for tool in BLUEZ_TOOLS:
            self.assertIn(f"    not applicable on Host_Local: {tool} help (BlueZ builds): {tool}",
                          text)
        self.assertIn("not applicable on Host_Local: -s of the test build's binary gives the "
                      "build's .dfu: needs a firmware build", text)
        self.assertIn("    run: ubertooth-util help", text)
        self.assertRegex(text, r"total: 2 test\(s\), \d+ case\(s\) to run, 3 case\(s\) and 0 "
                               r"test\(s\) not applicable on Host_Local")

    def test_a_test_build_artifact_makes_the_sign_case_applicable(self):
        code, text = self.dry_run("--artifact", "test build=IMAGE")
        self.assertEqual(0, code)
        self.assertIn("    run: -s of the test build's binary gives the build's .dfu", text)
        self.assertIn("2 case(s) and 0 test(s) not applicable", text)


if __name__ == "__main__":
    unittest.main()
