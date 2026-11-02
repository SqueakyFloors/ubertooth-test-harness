"""A plan entry for hardware a registry has not got is left out, not fatal.

One plan is compiled against every bench's registry, so a step may name a
host this registry has not got, or a device no host here has. Run planning
records that as not applicable and leaves the step out;
``validate-plan --hardware`` refuses it, because there the registry is named
on purpose (#265).
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests.test_cli import make_source
from ubertooth_harness import cli
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError, TestInvocation
from ubertooth_harness.resources import (
    FileResourceRegistry,
    MissingResource,
    RegistryError,
)
from ubertooth_harness.rf import PlacementRequirement


# A Linux host with two Ubertooths and a DK registered as the advertiser, and
# a Mac with one Ubertooth and nothing else. Neither host has an ESP32
# Classic peer, which is what the Classic tests bind.
HARDWARE = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Linux0
      advertiser: DK_Linux0
  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "00000000000000000000000000000001"}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: "00000000000000000000000000000002"}
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity: {jlink serial: "000123456789", BLE address: "C0:00:00:00:00:02"}
"""

# A step every registry here can run, so a suite is never empty and the
# skipping is what the assertions are about. It binds no hardware, and it is
# not the test any step below names, so nothing it does hides a skip.
HOST_STEP = """    - Host CLI surface:
        hosts: each
"""

CLASSIC_STEP = """    - Capture known Classic device:
        hosts: [Host_Linux]
        Ubertooth: default
        acceptor: default
        initiator: default
        coverage: smoke
        duration: 25s
"""


def plan_text(*steps: str) -> str:
    return "plan: optional hardware\nsuites:\n  release:\n" + "".join(steps)


class OptionalHardwarePlanningTests(unittest.TestCase):

    def prepare(self, directory, steps, hardware=HARDWARE):
        root = Path(directory)
        (root / "hardware.yaml").write_text(hardware)
        (root / "plan.yaml").write_text(plan_text(*steps))
        return (
            load_plan(root / "plan.yaml"),
            FileResourceRegistry.load(root / "hardware.yaml"),
            root,
        )

    def compile(self, directory, steps, hardware=HARDWARE, strict=False):
        plan, registry, _root = self.prepare(directory, steps, hardware)
        return PlanCompiler(builtin_catalog(), registry).compile_suite(
            plan, "release", strict_hardware=strict
        )

    def test_a_host_this_registry_has_not_got_leaves_the_step_out(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, step))

            self.assertEqual(
                ("Host_Linux", "Host_Mac"),
                tuple(item.host for item in compiled.invocations),
            )
            (left_out,) = compiled.not_applicable
            self.assertEqual("Host_Lab", left_out.host)
            self.assertEqual("Ubertooth identity", left_out.test_name)
            self.assertEqual("Host_Lab is not a host in this registry", left_out.reason)
            self.assertEqual(("Host_Lab",), compiled.absent_hosts)
            # No run is ever made for Host_Lab, so the step is part of the
            # record of whichever host does run the suite. Both hosts here
            # run `Host CLI surface`, not this step.
            self.assertEqual((left_out,), compiled.for_host("Host_Mac"))
            self.assertEqual((left_out,), compiled.for_host("Host_Linux"))

    def test_a_host_without_the_device_a_role_binds_leaves_the_step_out(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, CLASSIC_STEP))

            self.assertEqual(
                ("Host CLI surface", "Host CLI surface"),
                tuple(item.test_name for item in compiled.invocations),
            )
            (left_out,) = compiled.not_applicable
            self.assertEqual("Host_Linux", left_out.host)
            self.assertEqual("Capture known Classic device", left_out.test_name)
            self.assertEqual(
                "host Host_Linux has no default ESP32 Classic peer for the "
                "acceptor role",
                left_out.reason,
            )

    def test_only_the_hosts_without_the_device_are_left_out(self):
        """The Linux host has the DK the advertiser role binds; the Mac does
        not, so the step runs on one host and is recorded on the other."""
        step = """    - Capture known BLE advertiser:
        hosts: each
        Ubertooth: default
        advertiser: default
        coverage: smoke
        duration: 5s
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (step,))

            (invocation,) = compiled.invocations
            self.assertEqual("Host_Linux", invocation.host)
            self.assertEqual("DK_Linux0", invocation.resources["advertiser"])
            (left_out,) = compiled.not_applicable
            self.assertEqual("Host_Mac", left_out.host)
            self.assertEqual(
                "host Host_Mac has no default nRF52840 DK for the advertiser role",
                left_out.reason,
            )
            self.assertEqual((left_out,), compiled.for_host("Host_Mac"))
            self.assertEqual((), compiled.for_host("Host_Linux"))

    def test_a_host_with_no_board_at_all_is_recorded_as_absent(self):
        """A capture test asks its Ubertooth for an ordinary link, so a host
        with no Ubertooth must still be reported as having none, not as
        having one placed wrongly."""
        step = """    - Classic Bluetooth receive:
        hosts: each
        Ubertooth: default
        coverage: smoke
        duration: 3s
"""
        hardware = HARDWARE + """  Host_Spare:
    type: Host
    platform: linux
    roles: [test target]
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (step,), hardware)

            self.assertEqual(
                ("Host_Linux", "Host_Mac"),
                tuple(item.host for item in compiled.invocations),
            )
            (left_out,) = compiled.not_applicable
            self.assertEqual("Host_Spare", left_out.host)
            self.assertEqual(
                "host Host_Spare has no default Ubertooth for the Ubertooth role",
                left_out.reason,
            )

    def test_a_host_whose_devices_all_declare_another_role_is_absent(self):
        """The plan named no alias, so a bench whose only DK is a peripheral
        has no advertiser rather than a plan to correct."""
        step = """    - Capture known BLE advertiser:
        hosts: [Host_Linux]
        Ubertooth: default
        advertiser: default
        coverage: smoke
        duration: 5s
"""
        hardware = HARDWARE.replace(
            "      advertiser: DK_Linux0", "      nRF52840 DK: DK_Linux0"
        ).replace("    roles: [advertiser]", "    roles: [peripheral]")
        with tempfile.TemporaryDirectory() as directory:
            # The only step in the suite, so the skip leaves nothing to run
            # and the reason is reported rather than lost.
            with self.assertRaisesRegex(
                PlanningError,
                "host Host_Linux has no nRF52840 DK for the advertiser role "
                r"\(DK_Linux0 declares peripheral\)",
            ):
                self.compile(directory, (step,), hardware)
            registry = FileResourceRegistry.load(
                Path(directory) / "hardware.yaml"
            )
            with self.assertRaisesRegex(
                MissingResource,
                "host Host_Linux has no nRF52840 DK for the advertiser role",
            ):
                registry.resolve_hardware(
                    "Host_Linux", "nRF52840 DK", "default", "advertiser"
                )

    def test_an_explicit_device_alias_this_registry_has_not_got_is_absent(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Linux]
        Ubertooth: UT_Lab7
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, step))

            (left_out,) = compiled.not_applicable
            self.assertEqual(
                "this registry has no Ubertooth 'UT_Lab7'", left_out.reason
            )

    def test_a_host_blocked_by_its_platform_does_not_make_absence_fatal(self):
        """The Mac cannot run a Linux-only step and the Linux host has no
        peers: the step belongs to another bench either way."""
        step = """    - Capture known Classic device:
        hosts: each
        platforms: [linux]
        Ubertooth: default
        acceptor: default
        initiator: default
        coverage: smoke
        duration: 25s
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, step))

            self.assertEqual(
                ("Host CLI surface", "Host CLI surface"),
                tuple(item.test_name for item in compiled.invocations),
            )
            self.assertEqual(
                [
                    ("Host_Mac", "runs only on Linux, and Host_Mac is macOS"),
                    (
                        "Host_Linux",
                        "host Host_Linux has no default ESP32 Classic peer for "
                        "the acceptor role",
                    ),
                ],
                [(item.host, item.reason) for item in compiled.not_applicable],
            )

    def test_a_host_that_is_here_runs_the_step_an_absent_one_was_named_for(self):
        """With one host absent and one here, the step runs, and the host
        that runs it is not told it was left out of it."""
        step = """    - Ubertooth identity:
        hosts: [Host_Lab, Host_Mac]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (step,))

            (invocation,) = compiled.invocations
            self.assertEqual("Host_Mac", invocation.host)
            # The host that runs the test is not also told the test was left
            # out, which would mark its own row not applicable in the GUI.
            self.assertEqual((), compiled.for_host("Host_Mac"))
            (item,) = compiled.for_host("Host_Linux")
            self.assertEqual(
                "Ubertooth identity: not applicable on Host_Lab: Host_Lab is "
                "not a host in this registry",
                item.describe(),
            )

    def test_a_platform_blocked_host_beside_an_absent_one_leaves_the_step_out(self):
        """The host that would have run it is not here, so the Mac's platform
        is beside the point and the step belongs to another bench."""
        step = """    - Ubertooth identity:
        hosts: [Host_Lab, Host_Mac]
        platforms: [linux]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, step))

            self.assertEqual(
                ("Host_Linux", "Host_Mac"),
                tuple(item.host for item in compiled.invocations),
            )
            self.assertEqual(
                [
                    ("Host_Lab", "Host_Lab is not a host in this registry"),
                    ("Host_Mac", "runs only on Linux, and Host_Mac is macOS"),
                ],
                [(item.host, item.reason) for item in compiled.not_applicable],
            )

    def test_another_device_takes_a_role_the_hosts_default_cannot(self):
        """The plan said `default`, and the bench does have an advertiser: it
        takes the role, as it would for a placement the default cannot
        satisfy."""
        step = """    - Capture known BLE advertiser:
        hosts: [Host_Linux]
        Ubertooth: default
        advertiser: default
        coverage: smoke
        duration: 5s
"""
        hardware = HARDWARE.replace(
            "      advertiser: DK_Linux0", "      nRF52840 DK: DK_Linux0"
        ).replace("    roles: [advertiser]", "    roles: [peripheral]") + """  DK_Linux1:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity: {jlink serial: "000123456790", BLE address: "C0:00:00:00:00:03"}
"""
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (step,), hardware)

            (invocation,) = compiled.invocations
            self.assertEqual("DK_Linux1", invocation.resources["advertiser"])

    def test_a_role_no_device_can_take_is_absence_not_a_placement_failure(self):
        """With an `rf:` requirement on the role as well, a host whose only
        DK is placed as asked but declares another role has not got the
        hardware; the reason says so rather than blaming the placement."""
        step = """    - Capture known BLE advertiser:
        hosts: [Host_Linux]
        Ubertooth: default
        advertiser: default
        coverage: smoke
        duration: 5s
        rf:
          advertiser: {min_attenuation_db: 10}
"""
        hardware = HARDWARE.replace(
            "      advertiser: DK_Linux0", "      nRF52840 DK: DK_Linux0"
        ).replace(
            "    roles: [advertiser]",
            "    roles: [peripheral]\n    rf: {attenuation_db: 20}",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "host Host_Linux has no nRF52840 DK for the advertiser role "
                r"\(DK_Linux0 declares peripheral\)",
            ):
                self.compile(directory, (step,), hardware)
            registry = FileResourceRegistry.load(
                Path(directory) / "hardware.yaml"
            )
            # Absence, not the placement refusal it would have been.
            with self.assertRaises(MissingResource):
                registry.resolve_hardware(
                    "Host_Linux", "nRF52840 DK", "default", "advertiser",
                    PlacementRequirement(min_attenuation_db=10),
                )

    def test_a_suite_with_nothing_this_registry_can_run_is_still_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "suite 'release' produced no runnable invocations; "
                "Capture known Classic device: not applicable on Host_Linux: "
                "host Host_Linux has no default ESP32 Classic peer",
            ):
                self.compile(directory, (CLASSIC_STEP,))

    def test_a_misspelled_host_alias_fails_planning(self):
        step = """    - Ubertooth identity:
        hosts: [host_linux]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "this registry has no host 'host_linux'; it has 'Host_Linux', "
                "so check the spelling in the plan",
            ):
                self.compile(directory, (HOST_STEP, step))

    def test_a_misspelled_device_alias_fails_planning(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Linux]
        Ubertooth: ut_linux0
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "this registry has no Ubertooth 'ut_linux0'; it has 'UT_Linux0'",
            ):
                self.compile(directory, (HOST_STEP, step))

    def test_an_alias_of_the_wrong_kind_fails_planning(self):
        """A name the registry does have, used as something it is not, is a
        mistake in the plan wherever it is compiled."""
        steps = (
            HOST_STEP,
            """    - Ubertooth identity:
        hosts: [UT_Linux0]
        Ubertooth: default
""",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError, "resource UT_Linux0 is not a Host"
            ):
                self.compile(directory, steps)

    def test_a_host_that_is_here_and_cannot_run_the_step_still_fails(self):
        """Unchanged: a platform the step does not list is not absent
        hardware, so a step no host it names can run is a plan error."""
        step = """    - Ubertooth identity:
        hosts: [Host_Mac]
        platforms: [linux]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                PlanningError,
                "Ubertooth identity: no selected host can run it: Host_Mac: "
                "runs only on Linux",
            ):
                self.compile(directory, (HOST_STEP, step))

    def test_absence_is_not_hidden_when_the_registry_is_named_on_purpose(self):
        for name, steps in (
            ("absent host", (HOST_STEP, """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
""")),
            ("absent role", (HOST_STEP, CLASSIC_STEP)),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(PlanningError):
                    self.compile(directory, steps, strict=True)

    def test_the_registry_tells_absence_from_a_misspelling(self):
        with tempfile.TemporaryDirectory() as directory:
            _plan, registry, _root = self.prepare(directory, (HOST_STEP,))

            with self.assertRaises(MissingResource):
                registry.resolve_hosts(["Host_Lab"])
            with self.assertRaises(MissingResource):
                registry.resolve_hardware("Host_Mac", "nRF52840 DK", "default")
            # A misspelling and a name used as the wrong kind of thing are
            # plan mistakes, so neither is reported as absence.
            for selector in (["host_linux"], ["UT_Linux0"]):
                with self.subTest(selector=selector):
                    with self.assertRaises(RegistryError) as caught:
                        registry.resolve_hosts(selector)
                    self.assertNotIsInstance(caught.exception, MissingResource)


class RunRecordTests(unittest.TestCase):
    """What a real run records for a step left out for an absent host."""

    def test_the_run_log_and_report_carry_the_step_and_name_its_host(self):
        from tests.test_sequencer import CollectSink, FakeExecutor, run_contract
        from ubertooth_harness.sequencer import NamedTestSequencer

        step = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(HARDWARE)
            (root / "plan.yaml").write_text(plan_text(HOST_STEP, step))
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            compiled = PlanCompiler(builtin_catalog(), registry).compile_suite(
                load_plan(root / "plan.yaml"), "release"
            )
            left_out = compiled.for_host("Host_Mac")
            invocation = TestInvocation(
                "001-Host version", "Host version", "Host_Mac", {"hosts": "each"}, {},
            )
            manifest, checks = run_contract(root, invocation)

            summary = NamedTestSequencer(
                FakeExecutor(), event_sink=CollectSink()
            ).run(
                (invocation,), root, run_id="named-run", manifest=manifest,
                preflight_checks=checks, not_applicable=left_out,
            )

            self.assertTrue(summary.passed, summary.failure_reason)
            self.assertEqual(1, summary.not_applicable_tests)
            recorded = [
                json.loads(line)["details"]
                for line in (root / "run.jsonl").read_text().splitlines()
                if json.loads(line)["event_type"] == "not_applicable"
            ]
            self.assertEqual(
                [("Host_Lab", "Ubertooth identity",
                  "Host_Lab is not a host in this registry")],
                [(item["host"], item["test_name"], item["reason"])
                 for item in recorded],
            )
            report = (root / "report.md").read_text()
            self.assertIn("## Not applicable", report)
            self.assertIn(
                "| Ubertooth identity | (whole test) | Host_Lab | Host_Lab is "
                "not a host in this registry |",
                report,
            )


class DryRunTests(unittest.TestCase):
    """What run-suite would do: the step is left out, and it says so."""

    def dry_run(self, *steps, hardware=HARDWARE):
        platform = "macos" if sys.platform == "darwin" else "linux"
        host = "Host_Mac" if platform == "macos" else "Host_Linux"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = make_source(root)
            (root / "plan.yaml").write_text(plan_text(*steps))
            (root / "hardware.yaml").write_text(hardware)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli.main([
                    "run-suite", "--dry-run", "--plan", str(root / "plan.yaml"),
                    "--suite", "release", "--hardware", str(root / "hardware.yaml"),
                    "--host", host, "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--output", str(root / "runs"),
                    "--lease-dir", str(root / "leases"),
                ])
            return code, out.getvalue(), host

    def test_a_step_for_a_host_this_registry_has_not_got_is_reported(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        code, text, host = self.dry_run(HOST_STEP, step)

        self.assertEqual(0, code)
        self.assertIn(
            "Ubertooth identity: not applicable on Host_Lab: Host_Lab is not "
            "a host in this registry",
            text,
        )
        # The total does not claim the skip was this host's doing; each
        # line names the host it belongs to.
        self.assertIn("case(s) and 1 test(s) not applicable", text)

    def test_a_step_whose_hardware_this_host_has_not_got_is_reported(self):
        code, text, host = self.dry_run(HOST_STEP, CLASSIC_STEP)

        self.assertEqual(0, code)
        if host == "Host_Linux":
            self.assertIn(
                "Capture known Classic device: not applicable on Host_Linux: "
                "host Host_Linux has no default ESP32 Classic peer",
                text,
            )
            self.assertIn("1 test(s) not applicable", text)
        else:
            # The step names the Linux host only, so the Mac's run never had
            # it: the Linux run is where it is recorded.
            self.assertNotIn("Capture known Classic device", text)


class ValidatePlanTests(unittest.TestCase):
    """`validate-plan --hardware` is the strict one: it is given the bench."""

    def validate(self, steps, hardware=HARDWARE, with_registry=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware)
            (root / "plan.yaml").write_text(plan_text(*steps))
            argv = ["validate-plan", str(root / "plan.yaml"), "--suite", "release"]
            if with_registry:
                argv += ["--hardware", str(root / "hardware.yaml")]
            with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                code = cli.main(argv)
            printed = "".join(call.args[0] for call in out.write.call_args_list)
            errors = "".join(call.args[0] for call in err.write.call_args_list)
            return code, printed, errors

    def test_a_registry_without_the_role_fails_validation(self):
        code, _printed, errors = self.validate((HOST_STEP, CLASSIC_STEP))

        self.assertEqual(2, code)
        self.assertIn(
            "error: host Host_Linux has no default ESP32 Classic peer for the "
            "acceptor role",
            errors,
        )

    def test_a_registry_without_the_host_fails_validation(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Lab]
        Ubertooth: default
"""
        code, _printed, errors = self.validate((HOST_STEP, step))

        self.assertEqual(2, code)
        self.assertIn("error: this registry has no host 'Host_Lab'", errors)

    def test_every_absent_host_is_named_in_one_refusal(self):
        step = """    - Ubertooth identity:
        hosts: [Host_Lab, Host_Linux, Host_Bench]
        Ubertooth: default
"""
        code, _printed, errors = self.validate((step,))

        self.assertEqual(2, code)
        self.assertIn(
            "error: this registry has no host 'Host_Lab', 'Host_Bench'", errors
        )

    def test_without_a_registry_the_plan_alone_is_validated(self):
        code, printed, _errors = self.validate(
            (HOST_STEP, CLASSIC_STEP), with_registry=False
        )

        self.assertEqual(0, code)
        self.assertIn("Capture known Classic device", printed)

    def test_a_registry_that_has_the_hardware_passes(self):
        step = """    - Capture known BLE advertiser:
        hosts: [Host_Linux]
        Ubertooth: default
        advertiser: default
        coverage: smoke
        duration: 5s
"""
        code, printed, errors = self.validate((step,))

        self.assertEqual(0, code, errors)
        self.assertIn("compiled release: 1 invocation(s)", printed)


if __name__ == "__main__":
    unittest.main()
