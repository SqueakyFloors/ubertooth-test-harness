"""run-suite power isolation: registry-driven controllers of several types."""

import io
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cli import main
from ubertooth_harness.enumeration import (
    DEFAULT_RETURN_DEADLINE_S,
    DEFAULT_SETTLE_S,
    UNIDENTIFIED_PREFIX,
    SysfsUbertoothEnumerator,
)
from ubertooth_harness.events import RunEvent, TerminalEventSink
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.isolation import IsolationRuntime, IsolationSetupError
from ubertooth_harness.manifest import InvocationIdentity
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.plan import load_plan
from ubertooth_harness.power import (
    ACRONAME_DRIVER,
    SIMULATED_DRIVER,
    PortState,
    PowerControllerError,
    SimulatedPowerController,
)
from ubertooth_harness.report import load_events, render_run
from ubertooth_harness.resources import STARTECH_DRIVER, FileResourceRegistry
from ubertooth_harness.sequencer import NamedTestSequencer

from tests.test_sequencer import CollectSink, run_contract

SERIALS = {
    "UT_Linux0": "aaaa0000aaaa0000aaaa0000aaaa0000",
    "UT_Linux1": "bbbb1111bbbb1111bbbb1111bbbb1111",
    "UT_Linux2": "cccc2222cccc2222cccc2222cccc2222",
}

# Three boards behind two different kinds of USB power switch.
HARDWARE = f"""
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {{Ubertooth: UT_Linux0}}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIALS["UT_Linux0"]}}}
    power: {{controller: Hub_StarTech, port: 1}}
  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIALS["UT_Linux1"]}}}
    power: {{controller: Hub_StarTech, port: 3}}
  UT_Linux2:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {SERIALS["UT_Linux2"]}}}
    power: {{controller: Hub_Acroname, port: 0}}
  Hub_StarTech:
    type: USB power controller
    attached to: Host_Linux
    identity: {{serial device: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_X-if00-port0}}
    driver: startech managed USB hub
    capabilities: [per-port VBUS switching]
  Hub_Acroname:
    type: USB power controller
    attached to: Host_Linux
    identity: {{serial: "0x12345678"}}
    driver: acroname BrainStem USBHub3+
    capabilities: [per-port VBUS switching, power readback]
"""

PLAN = """
plan: exclusive
suites:
  exclusive:
    - Exclusive Ubertooth topology:
        hosts: [Host_Linux]
        platforms: [linux]
        Ubertooth: UT_Linux0
        exclusive Ubertooth: true
        capture duration: 1s
"""


class Lab:
    """Simulated controllers (one per registry resource) plus USB enumeration."""

    def __init__(self, registry):
        self.registry = registry
        self.controllers = {}
        self.created = []  # (alias, driver) for every controller built
        self.extra = set()  # serials always enumerated (unpowerable boards)
        self.never_return = frozenset()  # serials that stay gone once powered off
        self.gone = set()
        self.connects = []  # controller aliases connected, in order

    def factory(self, resource, *, allow_real_changes):
        assert allow_real_changes
        self.created.append((resource.alias, resource.driver))
        ports = {
            board.power["port"]
            for board in self.registry.resources.values()
            if board.power and board.power.get("controller") == resource.alias
        }
        controller = SimulatedPowerController(
            0x1,
            {port: PortState(port, True, True, 5_000_000, 0) for port in ports},
        )
        real_connect = controller.connect

        def connect():
            self.connects.append(resource.alias)
            real_connect()

        controller.connect = connect
        self.controllers[resource.alias] = controller
        return controller

    def serials(self):
        seen = set(self.extra)
        for alias, serial in SERIALS.items():
            power = self.registry.get(alias).power
            controller = self.controllers.get(power["controller"])
            if controller is None or controller.physical_power(power["port"]):
                seen.add(serial)
            elif serial in self.never_return:
                self.gone.add(serial)
        return frozenset(seen - self.gone)


def multiple_devices(seen):
    """libubertooth's no-selector output with more than one board (stderr)."""
    return "multiple Ubertooth devices found! Use '-U' to specify device number\n" + "".join(
        f"  Device {i}: Serial No: {serial}\n" for i, serial in enumerate(seen)
    )


class LabExecutor:
    """Answers the exclusive-topology commands like real hardware would."""

    def __init__(self, lab, on_command=None):
        self.lab = lab
        self.commands = []
        self.on_command = on_command

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        argv = tuple(argv)
        self.commands.append(argv)
        if self.on_command:
            self.on_command(argv)
        seen = sorted(self.lab.serials())
        stdout, stderr, code = "", "", 0
        if argv == ("ubertooth-util", "-V"):
            stdout = "Host tools version: libubertooth 1.1 (git-test)\n"
        elif len(seen) != 1 and argv[0] in ("ubertooth-util", "bash"):
            # ubertooth_start() opens no device, so the tool fails.
            stderr, code = multiple_devices(seen), 1
        elif argv == ("ubertooth-util", "-s"):
            stdout = f"Serial No: {seen[0]}\n"
        elif argv == ("ubertooth-util", "-v"):
            stdout = "Firmware version: git-test (API:1.07)\n"
        elif argv[:2] == ("bash", "-c") and "ubertooth-btle -f" in argv[2]:
            code = 124
        else:
            raise AssertionError(f"unexpected command {argv}")
        if on_output:
            if stdout:
                on_output("stdout", stdout)
            if stderr:
                on_output("stderr", stderr)
        cancelled = cancellation is not None and cancellation.is_cancelled()
        return CommandResult(argv, code, stdout, stderr, 0.01, False, cancelled)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def compile_plan(directory, registry, text):
    path = directory / "plan.yaml"
    path.write_text(text)
    catalog = builtin_catalog()
    return PlanCompiler(catalog, registry).compile(load_plan(path, catalog), "exclusive")


class RunIsolationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        (directory / "hardware.yaml").write_text(HARDWARE)
        self.registry = FileResourceRegistry.load(directory / "hardware.yaml")
        self.invocations = compile_plan(directory, self.registry, PLAN)
        (self.invocation,) = self.invocations
        self.lab = Lab(self.registry)
        self.clock = Clock()

    def runtime(self, allow_changes=True):
        return IsolationRuntime(
            self.registry,
            "Host_Linux",
            allow_changes=allow_changes,
            enumerator=self.lab,
            controller_factory=self.lab.factory,
            verification_options={"clock": self.clock, "sleep": self.clock.sleep},
        )

    def run_suite(self, isolation, executor=None, cancellation=None):
        directory = Path(self.tmp.name) / "run"
        directory.mkdir(exist_ok=True)
        invocations = tuple(self.invocations)
        manifest, checks = run_contract(directory, invocations)
        sink = CollectSink()
        summary = NamedTestSequencer(
            executor or LabExecutor(self.lab), event_sink=sink, isolation=isolation
        ).run(
            invocations, directory / "out", run_id="named-run",
            manifest=manifest, preflight_checks=checks, cancellation=cancellation,
        )
        return summary, sink.events

    def all_ports_on(self):
        return (self.lab.controllers["Hub_StarTech"].physical_power(1)
                and self.lab.controllers["Hub_StarTech"].physical_power(3)
                and self.lab.controllers["Hub_Acroname"].physical_power(0))

    def registry_with(self, old, new):
        path = Path(self.tmp.name) / "variant.yaml"
        path.write_text(HARDWARE.replace(old, new))
        return FileResourceRegistry.load(path)

    def test_plan_isolates_boards_behind_both_switch_types(self):
        actions = {(a.resource_alias, a.controller_alias, a.port)
                   for a in self.invocation.power_isolation}
        self.assertEqual(
            {("UT_Linux1", "Hub_StarTech", 3), ("UT_Linux2", "Hub_Acroname", 0)},
            actions,
        )

    def test_runtime_builds_controllers_from_registered_drivers(self):
        runtime = IsolationRuntime(
            self.registry, "Host_Linux", allow_changes=True, enumerator=self.lab
        )
        self.assertEqual(
            "StarTechManagedHubController",
            type(runtime.controller("Hub_StarTech")).__name__,
        )
        self.assertEqual(
            "AcronameUSBHub3pController",
            type(runtime.controller("Hub_Acroname")).__name__,
        )
        self.assertIs(runtime.controller("Hub_StarTech"), runtime.controller("Hub_StarTech"))

    def test_exclusive_run_isolates_restores_and_passes(self):
        summary, events = self.run_suite(self.runtime())

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(
            {("Hub_Acroname", ACRONAME_DRIVER), ("Hub_StarTech", STARTECH_DRIVER)},
            set(self.lab.created),
        )
        types = [e.event_type for e in events if e.event_type.startswith("isolation_")]
        self.assertEqual(
            ["isolation_started", "isolation_applied", "isolation_confirmed",
             "isolation_restored", "isolation_returned", "isolation_finished"],
            types,
        )
        started = next(e for e in events if e.event_type == "isolation_started")
        self.assertEqual(
            {"Hub_Acroname": ACRONAME_DRIVER, "Hub_StarTech": STARTECH_DRIVER},
            started.details["controllers"],
        )
        confirmed = next(e for e in events if e.event_type == "isolation_confirmed")
        self.assertEqual([SERIALS["UT_Linux0"]], confirmed.details["enumerated"])
        # Every port is powered again afterwards.
        self.assertTrue(self.lab.controllers["Hub_StarTech"].physical_power(3))
        self.assertTrue(self.lab.controllers["Hub_Acroname"].physical_power(0))
        # Isolation events are durable in run.jsonl.
        lines = (Path(self.tmp.name) / "run" / "out" / "run.jsonl").read_text()
        self.assertIn("isolation_confirmed", lines)

    def test_without_isolation_runtime_the_test_fails_closed(self):
        executor = LabExecutor(self.lab)
        summary, _ = self.run_suite(None, executor)

        self.assertFalse(summary.passed)
        self.assertIn("--allow-power-changes", summary.failure_reason)
        self.assertNotIn(("ubertooth-util", "-s"), executor.commands)

    def test_unarmed_runtime_refuses_before_switching(self):
        runtime = self.runtime(allow_changes=False)
        with self.assertRaisesRegex(IsolationSetupError, "--allow-power-changes"):
            runtime.check_ready(self.invocation.power_isolation)
        self.assertEqual([], self.lab.created)

    def test_unregistered_extra_board_fails_before_switching(self):
        self.lab.extra = {"dddd3333dddd3333dddd3333dddd3333"}
        executor = LabExecutor(self.lab)

        summary, events = self.run_suite(self.runtime(), executor)

        self.assertFalse(summary.passed)
        self.assertIn("unregistered Ubertooth attached: dddd3333", summary.failure_reason)
        self.assertNotIn(("ubertooth-util", "-s"), executor.commands)
        self.assertNotIn("isolation_started", [e.event_type for e in events])
        self.assertTrue(self.all_ports_on())

    def test_board_left_off_by_a_killed_run_is_refused(self):
        runtime = self.runtime()
        hub = runtime.controller("Hub_StarTech")
        hub.connect()
        hub.set_power_enabled(3, False)
        hub.close()

        with self.assertRaisesRegex(IsolationSetupError, "UT_Linux1 is already powered off"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

    def test_missing_selected_board_is_refused_at_readiness(self):
        runtime = self.runtime()
        hub = runtime.controller("Hub_StarTech")
        hub.connect()
        hub.set_power_enabled(1, False)
        hub.close()

        with self.assertRaisesRegex(IsolationSetupError, "UT_Linux0 .* is not enumerated"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

    def test_cancel_during_baseline_does_not_switch(self):
        token = CancellationToken()
        lab = self.lab
        calls = {"n": 0}

        class CancellingLab:
            # The first scans are readiness; cancel during the baseline wait.
            def serials(self):
                calls["n"] += 1
                if calls["n"] == 2:
                    token.cancel("cancel during baseline")
                return lab.serials()

        runtime = IsolationRuntime(
            self.registry, "Host_Linux", allow_changes=True,
            enumerator=CancellingLab(), controller_factory=lab.factory,
            verification_options={"clock": self.clock, "sleep": self.clock.sleep},
        )
        summary, events = self.run_suite(runtime, cancellation=token)

        self.assertEqual("canceled", summary.status)
        self.assertTrue(self.all_ports_on())
        types = [e.event_type for e in events if e.event_type.startswith("isolation_")]
        self.assertNotIn("isolation_applied", types)
        finished = [e for e in events if e.event_type == "isolation_finished"]
        self.assertEqual("canceled", finished[-1].status)

    def test_partial_power_off_failure_restores_the_switched_board(self):
        runtime = self.runtime()
        original = self.lab.factory

        def failing_factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_StarTech":  # switched after Hub_Acroname
                def set_power(port, enabled):
                    raise PowerControllerError("write rejected")
                controller.set_power_enabled = set_power
            return controller

        runtime._controller_factory = failing_factory
        summary, _ = self.run_suite(runtime)

        self.assertFalse(summary.passed)
        self.assertIn("write rejected", summary.failure_reason)
        self.assertTrue(self.lab.controllers["Hub_Acroname"].physical_power(0))

    def test_close_error_after_restore_is_not_a_restore_failure(self):
        runtime = self.runtime()
        original = self.lab.factory

        def factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_Acroname":
                def close():
                    raise RuntimeError("disconnect failed")
                controller.close = close
            return controller

        runtime._controller_factory = factory
        summary, events = self.run_suite(runtime)

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertIn("isolation_returned", [e.event_type for e in events])

    def test_applied_event_records_original_port_states(self):
        _, events = self.run_suite(self.runtime())
        applied = next(e for e in events if e.event_type == "isolation_applied")
        self.assertEqual(
            [{"controller": "Hub_Acroname", "port": 0, "power_enabled": True},
             {"controller": "Hub_StarTech", "port": 3, "power_enabled": True}],
            applied.details["original"],
        )

    def test_restore_failure_fails_the_run(self):
        runtime = self.runtime()
        original = self.lab.factory

        def failing_factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_Acroname":
                real = controller.set_power_enabled

                def set_power(port, enabled):
                    if enabled:
                        raise PowerControllerError("hub stopped answering")
                    real(port, enabled)

                controller.set_power_enabled = set_power
            return controller

        runtime._controller_factory = failing_factory
        summary, _ = self.run_suite(runtime)

        self.assertFalse(summary.passed)
        self.assertIn("restoration failed: Hub_Acroname port 0", summary.failure_reason)
        # The other controller's port is still restored.
        self.assertTrue(self.lab.controllers["Hub_StarTech"].physical_power(3))

    def test_unsupported_platform_fails_closed(self):
        path = Path(self.tmp.name) / "mac.yaml"
        path.write_text(HARDWARE.replace("platform: linux", "platform: macos"))
        mac_registry = FileResourceRegistry.load(path)
        runtime = IsolationRuntime(
            mac_registry, "Host_Linux", allow_changes=True,
            controller_factory=self.lab.factory,
        )
        with self.assertRaisesRegex(IsolationSetupError, "no USB enumerator"):
            runtime.check_ready(self.invocation.power_isolation)


    def test_every_controller_restore_failure_is_reported(self):
        runtime = self.runtime()
        original = self.lab.factory

        def failing_factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            real = controller.set_power_enabled

            def set_power(port, enabled):
                if enabled:
                    raise PowerControllerError("hub stopped answering")
                real(port, enabled)

            controller.set_power_enabled = set_power
            return controller

        runtime._controller_factory = failing_factory
        summary, _ = self.run_suite(runtime)

        self.assertIn("Hub_Acroname port 0", summary.failure_reason)
        self.assertIn("Hub_StarTech port 3", summary.failure_reason)

    def test_unexpected_error_while_isolated_restores_and_records(self):
        class Exploding(LabExecutor):
            def run(self, argv, **kwargs):
                if tuple(argv) == ("ubertooth-util", "-v"):
                    raise OSError("disk full")
                return super().run(argv, **kwargs)

        summary, events = self.run_suite(self.runtime(), Exploding(self.lab))

        self.assertFalse(summary.passed)
        self.assertIn("unexpected OSError: disk full", summary.failure_reason)
        self.assertTrue(self.all_ports_on())
        types = [e.event_type for e in events]
        self.assertIn("isolation_restored", types)
        self.assertEqual("run_finished", types[-1])
        finished = [e for e in events if e.event_type == "isolation_finished"]
        self.assertEqual("failed", finished[-1].status)
        self.assertTrue((Path(self.tmp.name) / "run" / "out" / "report.md").exists())

    def test_interrupt_still_records_restoration(self):
        class Interrupting(LabExecutor):
            def run(self, argv, **kwargs):
                if tuple(argv) == ("ubertooth-util", "-v"):
                    raise KeyboardInterrupt
                return super().run(argv, **kwargs)

        sink = CollectSink()
        directory = Path(self.tmp.name) / "run"
        directory.mkdir()
        manifest, checks = run_contract(directory, tuple(self.invocations))
        with self.assertRaises(KeyboardInterrupt):
            NamedTestSequencer(
                Interrupting(self.lab), event_sink=sink, isolation=self.runtime()
            ).run(tuple(self.invocations), directory / "out", run_id="named-run",
                  manifest=manifest, preflight_checks=checks)
        types = [e.event_type for e in sink.events if e.event_type.startswith("isolation_")]
        self.assertEqual(["isolation_restored", "isolation_finished"], types[-2:])
        self.assertTrue(self.all_ports_on())

    def test_cancel_before_isolation_does_not_switch(self):
        token = CancellationToken()
        executor = LabExecutor(self.lab)
        directory = Path(self.tmp.name) / "run"
        directory.mkdir()
        manifest, checks = run_contract(directory, tuple(self.invocations))
        real_emit = CollectSink.emit

        class CancelOnTestStart(CollectSink):
            # Cancel lands after test_started, just before isolation.
            def emit(self, event):
                real_emit(self, event)
                if event.event_type == "test_started":
                    token.cancel("late cancel")

        sequencer = NamedTestSequencer(executor, event_sink=CancelOnTestStart(),
                                       isolation=self.runtime())
        summary = sequencer.run(tuple(self.invocations), directory / "out",
                                run_id="named-run", manifest=manifest,
                                preflight_checks=checks, cancellation=token)

        self.assertEqual("canceled", summary.status)
        self.assertEqual([], self.lab.connects)  # no controller touched

    def test_btle_duration_never_uses_exponent_notation(self):
        from ubertooth_harness.catalog import _seconds_argument

        self.assertEqual("1", _seconds_argument(1.0))
        self.assertEqual("1234.567", _seconds_argument(1234.567))
        self.assertEqual("1000000", _seconds_argument(1e6))

    def test_each_ubertooth_isolates_every_other_board_in_turn(self):
        self.invocations = compile_plan(
            Path(self.tmp.name), self.registry,
            PLAN.replace("Ubertooth: UT_Linux0", "Ubertooth: each"),
        )
        self.assertEqual(3, len(self.invocations))
        executor = LabExecutor(self.lab)

        summary, events = self.run_suite(self.runtime(), executor)

        self.assertTrue(summary.passed, summary.failure_reason)
        confirmed = [e.details["enumerated"] for e in events
                     if e.event_type == "isolation_confirmed"]
        self.assertEqual([[SERIALS[a]] for a in sorted(SERIALS)], confirmed)
        returned = [e for e in events if e.event_type == "isolation_returned"]
        self.assertEqual(3, len(returned))
        # One controller per resource for the whole run, reconnected per use.
        self.assertEqual(2, len(self.lab.created))
        self.assertGreater(len(self.lab.connects), 2)
        self.assertTrue(self.all_ports_on())

    def test_cancellation_while_isolated_restores_every_port(self):
        token = CancellationToken()

        def cancel_on_serial_query(argv):
            if argv == ("ubertooth-util", "-s"):
                self.assertFalse(self.all_ports_on())
                token.cancel("test cancel")

        executor = LabExecutor(self.lab, on_command=cancel_on_serial_query)
        summary, events = self.run_suite(self.runtime(), executor, token)

        self.assertFalse(summary.passed)
        self.assertEqual("canceled", summary.status)
        self.assertTrue(self.all_ports_on())
        self.assertNotIn(("ubertooth-util", "-v"), executor.commands)
        self.assertEqual("isolation_finished", [
            e.event_type for e in events if e.event_type.startswith("isolation_")
        ][-1])

    def test_board_that_does_not_return_fails_a_passing_test(self):
        self.lab.never_return = frozenset({SERIALS["UT_Linux1"]})

        summary, events = self.run_suite(self.runtime())

        self.assertFalse(summary.passed)
        self.assertIn("power isolation", summary.failure_reason)
        self.assertIn(SERIALS["UT_Linux1"], summary.failure_reason)
        self.assertTrue(self.all_ports_on())
        finished = [e for e in events if e.event_type == "isolation_finished"]
        self.assertEqual("failed", finished[-1].status)
        self.assertNotIn("isolation_returned", [e.event_type for e in events])

    def test_topology_cases_fail_with_more_than_one_board(self):
        executor = LabExecutor(self.lab)  # no controllers: every board enumerated
        cases = builtin_catalog().get("Exclusive Ubertooth topology").command_cases(
            self.invocation.arguments,
            self.invocation.resources,
            self.invocation.resource_identities,
        )
        for case in cases:
            result = executor.run(case.argv, timeout_s=case.timeout_s)
            self.assertIsNotNone(case.result_failure(result), case.case_id)

    def test_btle_case_keeps_timeout_in_process_group_with_kill_after(self):
        btle = builtin_catalog().get("Exclusive Ubertooth topology").command_cases(
            self.invocation.arguments,
            self.invocation.resources,
            self.invocation.resource_identities,
        )[-1]
        self.assertIn("timeout --foreground -k 2 -s INT 1 ubertooth-btle -f", btle.argv[2])
        self.assertEqual(11, btle.timeout_s)

    def test_readiness_probes_controllers_and_usb_without_switching(self):
        runtime = self.runtime()
        runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

        self.assertEqual(["Hub_Acroname", "Hub_StarTech"], self.lab.connects)
        self.assertTrue(self.all_ports_on())

    def test_readiness_rereads_ports_on_every_check(self):
        runtime = self.runtime()
        runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")
        hub = runtime.controller("Hub_StarTech")
        hub.connect()
        hub.set_power_enabled(3, False)  # someone else, between invocations
        hub.close()

        with self.assertRaisesRegex(IsolationSetupError, "UT_Linux1 is already powered off"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

    def test_uppercase_registry_serials_match_lowercase_enumeration(self):
        registry_text = HARDWARE.replace(SERIALS["UT_Linux1"], SERIALS["UT_Linux1"].upper())
        path = Path(self.tmp.name) / "upper.yaml"
        path.write_text(registry_text.replace(SERIALS["UT_Linux0"], SERIALS["UT_Linux0"].upper()))
        registry = FileResourceRegistry.load(path)
        lab = Lab(registry)
        runtime = IsolationRuntime(registry, "Host_Linux", allow_changes=True,
                                   enumerator=lab, controller_factory=lab.factory)

        runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")
        self.assertEqual(SERIALS["UT_Linux0"], runtime._serial("UT_Linux0"))

    def test_driver_error_at_readiness_fails_closed(self):
        original = self.lab.factory

        def factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_Acroname":
                def snapshot(port):
                    raise AttributeError("BrainStem API mismatch")
                controller.snapshot = snapshot
            return controller

        runtime = self.runtime()
        runtime._controller_factory = factory
        with self.assertRaisesRegex(IsolationSetupError, "Hub_Acroname: BrainStem API"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

    def test_restore_failure_keeps_the_error_it_replaced(self):
        runtime = self.runtime()
        original = self.lab.factory

        def failing_factory(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_Acroname":
                real = controller.set_power_enabled

                def set_power(port, enabled):
                    if enabled:
                        raise PowerControllerError("hub stopped answering")
                    real(port, enabled)
                controller.set_power_enabled = set_power
            return controller

        class Exploding(LabExecutor):
            def run(self, argv, **kwargs):
                if tuple(argv) == ("ubertooth-util", "-v"):
                    raise OSError("disk full")
                return super().run(argv, **kwargs)

        runtime._controller_factory = failing_factory
        summary, _ = self.run_suite(runtime, Exploding(self.lab))

        self.assertIn("restoration failed: Hub_Acroname port 0", summary.failure_reason)
        self.assertIn("while handling OSError: disk full", summary.failure_reason)

    def test_capture_duration_is_capped(self):
        from ubertooth_harness.catalog import CatalogError

        definition = builtin_catalog().get("Exclusive Ubertooth topology")
        arguments = dict(self.invocation.arguments)
        arguments["capture duration"] = "11m"
        with self.assertRaisesRegex(CatalogError, "at most 10m"):
            definition.command_cases(arguments, self.invocation.resources,
                                     self.invocation.resource_identities)

    def test_readiness_reports_controller_connect_failure(self):
        original = self.lab.factory

        def unreachable(resource, *, allow_real_changes):
            controller = original(resource, allow_real_changes=allow_real_changes)
            if resource.alias == "Hub_StarTech":
                def connect():
                    raise PowerControllerError("pyserial is not installed")
                controller.connect = connect
            return controller

        runtime = self.runtime()
        runtime._controller_factory = unreachable
        with self.assertRaisesRegex(IsolationSetupError, "Hub_StarTech: pyserial"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")

    def test_readiness_rejects_placeholder_serial(self):
        registry = self.registry_with(
            SERIALS["UT_Linux2"], "REPLACE_WITH_UBERTOOTH_SERIAL"
        )
        runtime = IsolationRuntime(
            registry, "Host_Linux", allow_changes=True, enumerator=self.lab,
            controller_factory=self.lab.factory,
        )
        with self.assertRaisesRegex(IsolationSetupError, "UT_Linux2 has no registered"):
            runtime.check_ready(self.invocation.power_isolation, "UT_Linux0")
        self.assertEqual([], self.lab.connects)

    def test_simulated_controller_is_refused_with_real_enumeration(self):
        registry = self.registry_with(
            "driver: acroname BrainStem USBHub3+", f"driver: {SIMULATED_DRIVER}"
        )
        runtime = IsolationRuntime(registry, "Host_Linux", allow_changes=True)
        with (patch("ubertooth_harness.isolation.default_enumerator",
                    return_value=SysfsUbertoothEnumerator("/nonexistent")),
              self.assertRaisesRegex(IsolationSetupError, "simulated")):
            runtime.controller("Hub_Acroname")

    def test_registered_linux_host_must_be_this_machine(self):
        from ubertooth_harness.isolation import default_enumerator

        with self.assertRaisesRegex(IsolationSetupError, "this machine is 'darwin'"):
            default_enumerator("linux", "darwin")
        self.assertIsInstance(
            default_enumerator("linux", "linux"), SysfsUbertoothEnumerator
        )

    def test_terminal_renders_every_isolation_step(self):
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        _, events = self.run_suite(self.runtime())
        for event in events:
            if event.event_type.startswith("isolation_"):
                sink.emit(event)
        text = stream.getvalue()
        for phrase in ("UT_Linux1 off via Hub_StarTech port 3", "other boards powered off",
                       "confirmed; enumerated", "original port states restored",
                       "isolated boards enumerated again", "[power isolation] passed"):
            self.assertIn(phrase, text)


class SingleBoardUsb:
    """USB enumeration on a host with one registered board (no switching)."""

    def __init__(self, serials):
        self.present = set(serials)
        self.scans = 0

    def serials(self):
        self.scans += 1
        return frozenset(self.present)


def single_board_hardware(text, keep):
    """The registry text with every Ubertooth except `keep` unregistered."""
    data = yaml.safe_load(text)
    resources = data["resources"]
    for alias in [a for a, r in resources.items()
                  if r["type"] == "Ubertooth" and a != keep]:
        del resources[alias]
    return yaml.safe_dump(data, sort_keys=False)


class SingleBoardExclusiveTests(unittest.TestCase):
    """`exclusive Ubertooth: true` with no other registered board (#27)."""

    PLAN_PATH = Path(__file__).resolve().parent.parent / "testplans" / "exclusive.yaml"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        full = yaml.safe_load(HARDWARE)["resources"]
        self.selected = full["Host_Linux"]["defaults"]["Ubertooth"]
        (self.root / "hardware.yaml").write_text(
            single_board_hardware(HARDWARE, self.selected)
        )
        self.registry = FileResourceRegistry.load(self.root / "hardware.yaml")
        self.serial = self.registry.get(self.selected).identity["serial"]
        # The registered boards this registry no longer knows about.
        self.unregistered = sorted(
            r["identity"]["serial"] for a, r in full.items()
            if r["type"] == "Ubertooth" and a != self.selected
        )
        catalog = builtin_catalog()
        self.invocations = PlanCompiler(catalog, self.registry).compile(
            load_plan(self.PLAN_PATH, catalog), "exclusive"
        )
        self.usb = SingleBoardUsb({self.serial})
        self.clock = Clock()
        self.created = []

    def factory(self, resource, *, allow_real_changes):
        self.created.append(resource.alias)
        raise AssertionError("a read-only check must not build a controller")

    def runtime(self, enumerator=None, allow_changes=False):
        return IsolationRuntime(
            self.registry, "Host_Linux", allow_changes=allow_changes,
            enumerator=enumerator or self.usb, controller_factory=self.factory,
            verification_options={"clock": self.clock, "sleep": self.clock.sleep},
        )

    def run_suite(self, isolation, executor=None, cancellation=None):
        directory = self.root / "run"
        directory.mkdir(exist_ok=True)
        invocations = tuple(self.invocations)
        manifest, checks = run_contract(directory, invocations)
        sink = CollectSink()
        executor = executor or LabExecutor(self.usb)
        summary = NamedTestSequencer(executor, event_sink=sink, isolation=isolation).run(
            invocations, directory / "out", run_id="named-run",
            manifest=manifest, preflight_checks=checks, cancellation=cancellation,
        )
        return summary, sink.events, executor

    def case_commands(self, events):
        return [e.details.get("argv") for e in events
                if e.event_type == "command_started" and e.details.get("phase") == "test"]

    def isolation_events(self, events):
        return [e for e in events if e.event_type.startswith("isolation_")]

    def test_plan_compiles_exclusive_with_no_power_actions(self):
        self.assertEqual(
            [self.selected], [i.resources["Ubertooth"] for i in self.invocations]
        )
        for invocation in self.invocations:
            self.assertTrue(invocation.exclusive_ubertooth)
            self.assertEqual((), invocation.power_isolation)

    def test_single_board_passes_read_only_without_power_opt_in(self):
        summary, events, executor = self.run_suite(self.runtime())

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual([], self.created)
        isolation = self.isolation_events(events)
        self.assertEqual(
            ["isolation_started", "isolation_confirmed", "isolation_finished"],
            [e.event_type for e in isolation],
        )
        started, confirmed, finished = isolation
        self.assertEqual([], started.details["actions"])
        self.assertEqual({}, started.details["controllers"])
        self.assertTrue(started.details["read_only"])
        self.assertEqual([self.serial], confirmed.details["enumerated"])
        self.assertEqual("passed", finished.status)
        # The check precedes every test command.
        first_command = next(
            i for i, e in enumerate(events) if e.event_type == "command_started"
            and e.details.get("phase") == "test"
        )
        self.assertLess(events.index(confirmed), first_command)
        self.assertIn(["ubertooth-util", "-s"], self.case_commands(events))

    def test_extra_identified_board_fails_before_cases_run(self):
        extra = self.unregistered[0]
        self.usb.present.add(extra)

        summary, events, executor = self.run_suite(self.runtime())

        self.assertFalse(summary.passed)
        self.assertIn("power isolation", summary.failure_reason)
        self.assertIn(extra, summary.failure_reason)
        self.assertEqual([], self.case_commands(events))
        self.assertEqual([], self.created)
        isolation = self.isolation_events(events)
        self.assertEqual(
            ["isolation_started", "isolation_finished"], [e.event_type for e in isolation]
        )
        self.assertEqual("failed", isolation[-1].status)
        self.assertIn(extra, isolation[-1].details["reason"])
        self.assertFalse(any(e.event_type == "check_started" and e.details.get("phase") == "test"
                             for e in events))

    def test_unidentified_board_fails_before_cases_run(self):
        dfu = f"{UNIDENTIFIED_PREFIX}1-1.4:6003"
        self.usb.present.add(dfu)

        summary, events, executor = self.run_suite(self.runtime())

        self.assertFalse(summary.passed)
        self.assertIn(dfu, summary.failure_reason)
        self.assertIn("DFU bootloader", summary.failure_reason)
        self.assertEqual([], self.case_commands(events))
        self.assertEqual("failed", self.isolation_events(events)[-1].status)

    def test_missing_selected_board_fails_before_cases_run(self):
        self.usb.present.clear()

        summary, events, executor = self.run_suite(self.runtime())

        self.assertFalse(summary.passed)
        self.assertIn(f"not enumerated: {self.serial}", summary.failure_reason)
        self.assertEqual([], self.case_commands(events))

    def test_extra_board_that_appears_during_the_pre_case_wait_is_caught(self):
        # The first scan of the pre-case wait is clean, then a board shows up
        # and stays: the wait fails at the deadline and gates the cases.
        usb = self.usb
        extra = self.unregistered[0]

        class LateBoard:
            def serials(self):
                if usb.scans >= 1:
                    usb.present.add(extra)
                return usb.serials()

        summary, events, _ = self.run_suite(self.runtime(LateBoard()))

        self.assertFalse(summary.passed)
        self.assertIn(extra, summary.failure_reason)
        self.assertEqual([], self.case_commands(events))

    def test_settle_waits_out_a_board_briefly_re_enumerating(self):
        # Through the full sequencer path: the board is gone when the check
        # starts, then steadily present; the settled wait absorbs the blink.
        usb = self.usb
        serial = self.serial
        settle = DEFAULT_SETTLE_S
        clock = self.clock

        class Blinking:
            def serials(self):
                seen = usb.serials()
                return seen if clock.now >= settle / 2 else seen - {serial}

        summary, events, _ = self.run_suite(self.runtime(Blinking()))

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertGreaterEqual(clock.now, settle / 2 + settle)
        confirmed = [e for e in events if e.event_type == "isolation_confirmed"]
        self.assertEqual([[serial]], [e.details["enumerated"] for e in confirmed])
        self.assertIn(["ubertooth-util", "-s"], self.case_commands(events))

    def test_selected_board_missing_past_settle_but_back_before_deadline_passes(self):
        # Missing for twice the settle time (well inside the return deadline):
        # the wait holds out for the expected set instead of judging the
        # first stable one.
        usb = self.usb
        serial = self.serial
        back_at = 2 * DEFAULT_SETTLE_S
        self.assertLess(back_at + DEFAULT_SETTLE_S, DEFAULT_RETURN_DEADLINE_S)
        clock = self.clock

        class LateReturn:
            def serials(self):
                seen = usb.serials()
                return seen if clock.now >= back_at else seen - {serial}

        summary, events, _ = self.run_suite(self.runtime(LateReturn()))

        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertGreaterEqual(clock.now, back_at + DEFAULT_SETTLE_S)
        confirmed = [e for e in events if e.event_type == "isolation_confirmed"]
        self.assertEqual([[serial]], [e.details["enumerated"] for e in confirmed])
        self.assertIn(["ubertooth-util", "-s"], self.case_commands(events))

    def test_cancel_during_the_wait_is_seen_promptly_and_recorded(self):
        # The enumeration never settles; a cancel arriving mid-wait must stop
        # it at the next poll, not at the return deadline.
        usb = self.usb
        extra = self.unregistered[0]
        token = CancellationToken()
        cancel_at = 3

        class Flapping:
            def serials(self):
                seen = usb.serials()
                if usb.scans == cancel_at:
                    token.cancel("received SIGINT")
                return seen | {extra} if usb.scans % 2 else seen

        summary, events, _ = self.run_suite(self.runtime(Flapping()), cancellation=token)

        self.assertFalse(summary.passed)
        self.assertEqual(cancel_at, usb.scans)
        self.assertLess(self.clock.now, DEFAULT_RETURN_DEADLINE_S)
        finished = self.isolation_events(events)[-1]
        self.assertEqual("isolation_finished", finished.event_type)
        self.assertEqual("canceled", finished.status)
        self.assertEqual("canceled", summary.status)
        tests = [e for e in events if e.event_type == "test_finished"]
        self.assertTrue(tests)
        self.assertEqual({"canceled"}, {e.status for e in tests})
        self.assertEqual([], self.case_commands(events))
        self.assertEqual([], self.created)

    def test_redacted_report_masks_an_unregistered_board(self):
        extra = self.unregistered[0]
        self.usb.present.add(extra)

        summary, _, _ = self.run_suite(self.runtime())
        self.assertIn(extra, summary.failure_reason)

        events = load_events(self.root / "run" / "out" / "run.jsonl")
        plain = render_run(events)
        self.assertIn(f"unexpected Ubertooth enumerated: {extra}", plain)
        redacted = render_run(events, redact_serials=True)
        for serial in (extra, self.serial):
            self.assertNotIn(serial, redacted)
            self.assertNotIn(serial.upper(), redacted)
        self.assertIn("unexpected Ubertooth enumerated: <serial>", redacted)
        self.assertIn("[I] finished failed:", redacted)

    def test_manifest_records_the_read_only_check(self):
        for invocation in self.invocations:
            identity = InvocationIdentity.from_invocation(invocation).to_dict()
            self.assertTrue(identity["exclusive_ubertooth"])
            self.assertEqual([], identity["power_isolation"])

    def test_readiness_is_read_only_and_needs_no_opt_in(self):
        runtime = self.runtime(allow_changes=False)
        runtime.check_ready((), self.selected)
        self.assertEqual([], self.created)

        self.usb.present.add(self.unregistered[0])
        with self.assertRaisesRegex(IsolationSetupError, "not the only Ubertooth") as caught:
            runtime.check_ready((), self.selected)
        self.assertIn("unplug the other Ubertooth, or register it", str(caught.exception))
        self.assertNotIn("DFU", str(caught.exception))

    def test_unidentified_refusal_hints_at_dfu(self):
        runtime = self.runtime(allow_changes=False)
        self.usb.present.add(f"{UNIDENTIFIED_PREFIX}1-1.4:6003")
        with self.assertRaisesRegex(IsolationSetupError, "DFU bootloader"):
            runtime.check_ready((), self.selected)

    def test_unsupported_platform_fails_closed(self):
        path = self.root / "mac.yaml"
        path.write_text(
            (self.root / "hardware.yaml").read_text().replace(
                "platform: linux", "platform: macos")
        )
        runtime = IsolationRuntime(
            FileResourceRegistry.load(path), "Host_Linux", allow_changes=False,
            controller_factory=self.factory,
        )
        with self.assertRaisesRegex(IsolationSetupError, "no USB enumerator"):
            runtime.check_ready((), self.selected)

    def test_without_runtime_the_test_fails_closed(self):
        summary, events, _ = self.run_suite(None)

        self.assertFalse(summary.passed)
        self.assertIn("enumeration check", summary.failure_reason)
        self.assertEqual([], self.case_commands(events))

    def test_terminal_and_report_render_the_check(self):
        stream = io.StringIO()
        sink = TerminalEventSink(stream)
        _, events, _ = self.run_suite(self.runtime())
        for event in self.isolation_events(events):
            sink.emit(event)
        text = stream.getvalue()
        self.assertIn(f"checking {self.selected} is the only Ubertooth (read-only)", text)
        self.assertIn(f"confirmed; enumerated: {self.serial}", text)
        self.assertIn("[power isolation] passed", text)
        report = (self.root / "run" / "out" / "report.md").read_text()
        self.assertIn("[I] started running: read-only enumeration check", report)
        self.assertIn("[I] confirmed passed: selected board is the only Ubertooth", report)

    def test_validate_plan_shows_the_read_only_check(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main([
                "validate-plan", str(self.PLAN_PATH),
                "--hardware", str(self.root / "hardware.yaml"),
            ])
        self.assertEqual(0, result)
        self.assertIn("read-only enumeration check", stdout.getvalue())
        self.assertNotIn("power:", stdout.getvalue())


class RunSuiteCliIsolationTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / "source"
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "tracked.txt").write_text("source\n")
        subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
            check=True,
        )
        # run-suite only runs on the registered host's own platform.
        local = "macos" if sys.platform == "darwin" else "linux"
        (self.root / "hardware.yaml").write_text(
            HARDWARE.replace("platform: linux", f"platform: {local}")
        )
        (self.root / "plan.yaml").write_text(
            PLAN.replace("platforms: [linux]", f"platforms: [{local}]")
        )
        self.output = self.root / "runs"
        self.argv = [
            "run-suite", "--plan", str(self.root / "plan.yaml"), "--suite", "exclusive",
            "--hardware", str(self.root / "hardware.yaml"), "--host", "Host_Linux",
            "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
            "--run-id", "iso-run", "--output", str(self.output),
        ]

    def test_without_flag_fails_before_creating_run_directory(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            result = main(self.argv)

        self.assertEqual(2, result)
        self.assertIn("--allow-power-changes", stderr.getvalue())
        self.assertFalse((self.output / "iso-run").exists())

    def test_board_left_off_is_refused_before_creating_run_directory(self):
        built = []

        class Runtime(IsolationRuntime):
            def __init__(self, registry, host, *, allow_changes):
                lab = Lab(registry)
                super().__init__(registry, host, allow_changes=allow_changes,
                                 enumerator=lab, controller_factory=lab.factory)
                hub = self.controller("Hub_StarTech")
                hub.connect()
                hub.set_power_enabled(3, False)  # left off by a killed run
                hub.close()
                built.append(self)

        stderr = io.StringIO()
        with (patch("ubertooth_harness.cli.IsolationRuntime", Runtime),
              redirect_stderr(stderr), redirect_stdout(io.StringIO())):
            result = main(self.argv + ["--allow-power-changes"])

        self.assertEqual(2, result)
        self.assertIn("UT_Linux1 is already powered off", stderr.getvalue())
        self.assertFalse((self.output / "iso-run").exists())

    def test_single_board_extra_board_refused_without_power_opt_in(self):
        (self.root / "hardware.yaml").write_text(single_board_hardware(
            (self.root / "hardware.yaml").read_text(), "UT_Linux0"
        ))
        extra = SERIALS["UT_Linux1"]
        built = []

        class Runtime(IsolationRuntime):
            def __init__(self, registry, host, *, allow_changes):
                usb = SingleBoardUsb({registry.get("UT_Linux0").identity["serial"], extra})
                super().__init__(registry, host, allow_changes=allow_changes,
                                 enumerator=usb)
                built.append(self)

        stderr = io.StringIO()
        with (patch("ubertooth_harness.cli.IsolationRuntime", Runtime),
              redirect_stderr(stderr), redirect_stdout(io.StringIO())):
            result = main(self.argv)  # no --allow-power-changes

        self.assertEqual(2, result)
        self.assertIn("not the only Ubertooth", stderr.getvalue())
        self.assertIn(extra, stderr.getvalue())
        self.assertNotIn("--allow-power-changes", stderr.getvalue())
        (runtime,) = built
        self.assertFalse(runtime.allow_changes)
        self.assertEqual({}, runtime._controllers)
        self.assertFalse((self.output / "iso-run").exists())

    def test_single_board_runs_without_power_opt_in(self):
        selected = yaml.safe_load(HARDWARE)["resources"]["Host_Linux"]["defaults"]["Ubertooth"]
        (self.root / "hardware.yaml").write_text(single_board_hardware(
            (self.root / "hardware.yaml").read_text(), selected
        ))
        serial = FileResourceRegistry.load(self.root / "hardware.yaml").get(
            selected).identity["serial"]
        usb = SingleBoardUsb({serial})
        clock = Clock()
        built = []

        def no_controller(resource, *, allow_real_changes):
            raise AssertionError("a read-only check must not build a controller")

        class Runtime(IsolationRuntime):
            def __init__(self, registry, host, *, allow_changes):
                super().__init__(
                    registry, host, allow_changes=allow_changes, enumerator=usb,
                    controller_factory=no_controller,
                    verification_options={"clock": clock, "sleep": clock.sleep},
                )
                built.append(self)

        source = Path(self.argv[self.argv.index("--ubertooth-source") + 1])
        revision = "git-" + subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        class SourceBuiltLab(LabExecutor):
            # Host and firmware report the checked-out source revision; with
            # one board attached, `-U <serial>` answers like the bare tool.
            def run(self, argv, **kwargs):
                argv = tuple(argv)
                selected = argv[1:2] == ("-U",) and argv[2] == serial
                if selected:
                    argv = argv[:1] + argv[3:]
                # The compile identity of the board the check named. Bare -V
                # is the host version check, which the base fake answers.
                if selected and argv == ("ubertooth-util", "-V"):
                    self.commands.append(argv)
                    return CommandResult(
                        argv, 0, f"ubertooth {revision} (test@host)\n", "", 0.01, False, False
                    )
                result = super().run(argv, **kwargs)
                return replace(result, stdout=result.stdout.replace("git-test", revision))

        executor = SourceBuiltLab(usb)
        stderr = io.StringIO()
        with (patch("ubertooth_harness.cli.IsolationRuntime", Runtime),
              patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
              redirect_stderr(stderr), redirect_stdout(io.StringIO())):
            result = main(self.argv)  # no --allow-power-changes

        self.assertEqual(0, result, stderr.getvalue())
        (runtime,) = built
        self.assertFalse(runtime.allow_changes)
        self.assertIn(("ubertooth-util", "-s"), executor.commands)
        events = load_events(self.output / "iso-run" / "run.jsonl")
        types = [e["event_type"] for e in events]
        self.assertLess(types.index("isolation_confirmed"), len(types) - 1)
        finished = [e for e in events if e["event_type"] == "isolation_finished"]
        self.assertEqual(["passed"], [e["status"] for e in finished])
        manifest = next(e for e in events if e["event_type"] == "manifest")
        (identity,) = manifest["details"]["manifest"]["invocations"]
        self.assertTrue(identity["exclusive_ubertooth"])

    def test_controllers_are_closed_when_the_run_ends(self):
        built = []
        handled = set()

        class Runtime(IsolationRuntime):
            def __init__(self, registry, host, *, allow_changes):
                super().__init__(registry, host, allow_changes=allow_changes,
                                 enumerator=Lab(registry))
                self.closed = False
                built.append(self)

            def check_ready(self, actions, selected_alias=None):
                self.armed = self.allow_changes

            def close(self):
                self.closed = True

        class Boom:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs):
                # A dropped SSH session (SIGHUP) must cancel, not kill, the run.
                handled.update(
                    signum for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
                    if signal.getsignal(signum) not in (signal.SIG_DFL, signal.SIG_IGN,
                                                        signal.default_int_handler)
                )
                raise RuntimeError("sequencer exploded")

        with (patch("ubertooth_harness.cli.IsolationRuntime", Runtime),
              patch("ubertooth_harness.cli.NamedTestSequencer", Boom),
              redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(RuntimeError, "exploded"):
                main(self.argv + ["--allow-power-changes"])

        (runtime,) = built
        self.assertTrue(runtime.armed)
        self.assertTrue(runtime.closed)
        self.assertEqual({signal.SIGINT, signal.SIGTERM, signal.SIGHUP}, handled)


if __name__ == "__main__":
    unittest.main()
