"""A plan entry for hardware a registry has not got is left out, not fatal.

One plan is compiled against every bench's registry, so a step may name a
host this registry has not got, or a device no host here has. Run planning
records that as not applicable and leaves the step out;
``validate-plan --hardware`` refuses it, because there the registry is named
on purpose (#265).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.resources import (
    FileResourceRegistry,
    MissingResource,
    RegistryError,
)


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
# skipping is what the assertions are about.
HOST_STEP = """    - Ubertooth identity:
        hosts: each
        Ubertooth: default
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
            self.assertEqual(
                "this registry has no host 'Host_Lab'", left_out.reason
            )

    def test_a_host_without_the_device_a_role_binds_leaves_the_step_out(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = self.compile(directory, (HOST_STEP, CLASSIC_STEP))

            self.assertEqual(
                ("Ubertooth identity", "Ubertooth identity"),
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
