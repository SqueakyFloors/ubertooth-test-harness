"""Bench helper scripts: scripts/lab/hold and scripts/lab/hub-port.

Both are Python without a .py suffix, so they are loaded by path. No test
touches a real device: `hold` runs a trivial command against a temporary
registry and lease directory, and `hub-port`'s controller is replaced in the
loaded module, which is the only part that would open a hub.
"""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

from ubertooth_harness import leases


REPO = Path(__file__).resolve().parents[1]
LAB = REPO / "scripts" / "lab"
HOLD = LAB / "hold"
HUB_PORT = LAB / "hub-port"
RULES = LAB / "99-acroname.rules"

UT_SERIAL = "0123456789abcdef0123456789abcdef"
UT2_SERIAL = "fedcba9876543210fedcba9876543210"
BRIDGE = "a1b2c3d400000000a1b2c3d400000000"
JLINK = "000123456789"

REGISTRY = textwrap.dedent(
    f"""
    resources:
      Host_Linux:
        type: Host
        platform: linux
        roles: [controller, test target]
        defaults:
          Ubertooth: UT0
      UT0:
        type: Ubertooth
        attached to: Host_Linux
        identity:
          serial: {UT_SERIAL}
        power:
          controller: Hub0
          port: 3
      UT1:
        type: Ubertooth
        attached to: Host_Linux
        identity:
          serial: {UT2_SERIAL}
      DK0:
        type: nRF52840 DK
        attached to: Host_Linux
        roles: [advertiser]
        identity:
          jlink serial: "{JLINK}"
          BLE address: "C0:00:00:00:00:01"
      ESP0:
        type: ESP32 Classic peer
        attached to: Host_Linux
        roles: [acceptor, initiator]
        identity:
          bridge serial: {BRIDGE}
          BD_ADDR: "00:11:22:33:44:55"
      Hub0:
        type: USB power controller
        attached to: Host_Linux
        identity:
          serial: "0x1234ABCD"
        driver: acroname BrainStem USBHub3+
        capabilities:
          - per-port VBUS switching
          - per-port data switching
          - power readback
          - VBUS voltage measurement
          - VBUS current measurement
    """
).lstrip()


def load(path: Path):
    """Load a script with no .py suffix as a module of its own."""
    spec = importlib.util.spec_from_loader(
        "lab_" + path.name.replace("-", "_"),
        importlib.machinery.SourceFileLoader("lab_" + path.name.replace("-", "_"), str(path)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hold = load(HOLD)
hub_port = load(HUB_PORT)


@dataclass
class FakeState:
    port: int
    power_enabled: bool
    data_enabled: bool
    vbus_microvolts: int | None
    vbus_microamps: int | None


class FakeController:
    """Records what was switched; never opens a hub."""

    def __init__(self, ports=range(8)):
        self.powered = {port: True for port in ports}
        self.calls: list[tuple[str, int, bool]] = []
        self.connected = False
        self.closed = False
        self.firmware_version = "0x28000000"

    def connect(self):
        self.connected = True

    def set_power_enabled(self, port, enabled):
        if not self.connected:
            raise AssertionError("switched before connect")
        self.powered[port] = bool(enabled)
        self.calls.append(("power", port, bool(enabled)))

    def snapshot(self, port):
        on = self.powered[port]
        return FakeState(port, on, True, 5_110_000 if on else 0, 81_900 if on else 0)

    def close(self):
        self.closed = True


class Bench(unittest.TestCase):
    """A temporary registry, lease directory, and external lock directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.registry = self.root / "hardware.yaml"
        self.registry.write_text(REGISTRY, encoding="utf-8")
        self.leases = self.root / "leases"
        self.leases.mkdir()
        self.locks = self.root / "locks"
        self.locks.mkdir()
        self.template = str(self.locks / "board-{serial}.lock")

    def load_registry(self):
        from ubertooth_harness.resources import FileResourceRegistry

        return FileResourceRegistry.load(self.registry)


class HoldResolution(Bench):
    def test_alias_serial_prefix_and_jlink_short_form(self):
        registry = self.load_registry()
        for name, alias in (
            ("UT0", "UT0"),
            (UT_SERIAL, "UT0"),
            (UT_SERIAL.upper(), "UT0"),
            (UT_SERIAL[:6], "UT0"),
            ("fedc", "UT1"),
            (JLINK, "DK0"),
            ("123456789", "DK0"),  # nrfutil drops the leading zeros
            (BRIDGE, "ESP0"),
            (BRIDGE[:8], "ESP0"),
        ):
            with self.subTest(name=name):
                self.assertEqual(alias, hold.resolve(registry, name).alias)

    def test_an_ambiguous_prefix_is_refused_with_both_aliases(self):
        sibling = UT_SERIAL[:6] + "f" * (len(UT_SERIAL) - 6)
        self.registry.write_text(
            REGISTRY.replace(f"serial: {UT2_SERIAL}", f"serial: {sibling}"),
            encoding="utf-8",
        )
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            hold.resolve(self.load_registry(), UT_SERIAL[:6])
        message = str(caught.exception)
        self.assertIn("UT0", message)
        self.assertIn("UT1", message)

    def test_a_name_too_short_to_be_a_prefix_is_refused(self):
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            hold.resolve(self.load_registry(), "01")
        self.assertIn("too short", str(caught.exception))

    def test_an_unknown_name_is_refused(self):
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError):
            hold.resolve(self.load_registry(), "deadbeefdeadbeef")

    def test_a_serial_device_path_is_not_a_name(self):
        """A controller's /dev path must not match a prefix of itself."""
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError):
            hold.resolve(self.load_registry(), "/dev/serial")


class HoldLockKeys(Bench):
    def test_each_type_gets_the_key_the_harness_uses(self):
        registry = self.load_registry()
        self.assertEqual(UT_SERIAL, hold.lock_key(registry.get("UT0")))
        self.assertEqual("nrf52840dk-123456789", hold.lock_key(registry.get("DK0")))
        self.assertEqual(f"esp32-{BRIDGE}", hold.lock_key(registry.get("ESP0")))

    def test_the_peer_key_matches_the_harness_once_it_leases_the_type(self):
        """When the harness learns this type, its key must be the same one."""
        registry = self.load_registry()
        peer = registry.get("ESP0")
        if peer.type in leases.LEASED_TYPES:
            self.assertEqual(leases.resource_key(peer), hold.lock_key(peer))

    def test_a_type_this_does_not_hold_is_refused(self):
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            hold.lock_key(self.load_registry().get("Hub0"))
        self.assertIn("USB power controller", str(caught.exception))


class HoldArguments(Bench):
    def parse(self, argv):
        return hold.parse_args(argv)

    def test_the_command_after_the_separator_is_untouched(self):
        args, command = self.parse(
            ["--hardware", str(self.registry), "UT0", "--", "echo", "--hardware", "x"]
        )
        self.assertEqual(["echo", "--hardware", "x"], command)
        self.assertEqual("UT0", args.devices)

    def test_no_separator_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            self.parse(["UT0", "echo", "hi"])

    def test_no_command_after_the_separator_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.parse(["--hardware", str(self.registry), "UT0", "--"])

    def test_the_registry_may_come_from_the_environment(self):
        os.environ["UBERTOOTH_HARNESS_HARDWARE"] = str(self.registry)
        self.addCleanup(os.environ.pop, "UBERTOOTH_HARNESS_HARDWARE", None)
        args, _ = self.parse(["UT0", "--", "true"])
        self.assertEqual(self.registry, Path(args.hardware))

    def test_with_no_registry_it_says_how_to_give_one(self):
        os.environ.pop("UBERTOOTH_HARNESS_HARDWARE", None)
        with self.assertRaises(SystemExit) as caught:
            self.parse(["UT0", "--", "true"])
        self.assertIn("UBERTOOTH_HARNESS_HARDWARE", str(caught.exception))

    def test_a_negative_wait_is_refused(self):
        with self.assertRaises(SystemExit):
            self.parse(
                ["--hardware", str(self.registry), "--wait-for-boards", "-1", "UT0", "--", "true"]
            )

    def test_the_delegated_lease_command_carries_every_option(self):
        args, command = self.parse([
            "--hardware", str(self.registry),
            "--external-board-lock", self.template,
            "--lease-dir", str(self.leases),
            "--owner", "alice",
            "--wait-for-boards", "2",
            "--host-lock",
            "UT0,DK0", "--", "true",
        ])
        argv = hold.harness_lease_argv(args, ["UT0", "DK0"], command)
        self.assertEqual([sys.executable, "-m", "ubertooth_harness", "lease"], argv[:4])
        self.assertIn("--host-lock", argv)
        self.assertIn(self.template, argv)
        self.assertIn(str(self.leases), argv)
        self.assertIn("alice", argv)
        self.assertEqual(["UT0", "DK0", "--", "true"], argv[-4:])


class HoldRuns(Bench):
    """End to end, with a trivial command in place of a device command."""

    def run_hold(self, devices, command, extra=()):
        """Run it with the real file descriptors redirected to files.

        A delegated `ubertooth-harness lease` writes to the inherited
        descriptor, not to this process's sys.stderr, so capturing it needs
        the descriptor itself.
        """
        argv = [
            "--hardware", str(self.registry),
            "--lease-dir", str(self.leases),
            "--external-board-lock", self.template,
            *extra, devices, "--", *command,
        ]
        out_path, err_path = self.root / "out.txt", self.root / "err.txt"
        with open(out_path, "w") as out, open(err_path, "w") as err:
            saved = os.dup(1), os.dup(2)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(out.fileno(), 1)
                os.dup2(err.fileno(), 2)
                status = hold.main(argv)
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
        return (
            status,
            out_path.read_text(encoding="utf-8"),
            err_path.read_text(encoding="utf-8"),
        )

    def test_it_runs_the_command_and_passes_its_exit_status_on(self):
        status, _, _ = self.run_hold("UT0", [sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(7, status)

    def test_a_leasable_device_is_leased_while_the_command_runs(self):
        marker = self.root / "held"
        script = (
            "import fcntl, pathlib, sys;"
            f"p = pathlib.Path({str(self.leases / (UT_SERIAL + '.lock'))!r});"
            "fd = open(p, 'a+');"
            "busy = False\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except OSError:\n"
            "    busy = True\n"
            f"pathlib.Path({str(marker)!r}).write_text('busy' if busy else 'free')\n"
        )
        status, _, _ = self.run_hold("UT0", [sys.executable, "-c", script])
        self.assertEqual(0, status)
        self.assertEqual("busy", marker.read_text())

    def test_both_lock_names_are_taken_for_a_device_whose_key_is_not_its_serial(self):
        """A bench script using the older plain-serial name must still be excluded."""
        for device, paths in (
            ("ESP0", (f"board-esp32-{BRIDGE}.lock", f"board-{BRIDGE}.lock")),
            ("DK0", (f"board-nrf52840dk-{int(JLINK)}.lock", f"board-{JLINK}.lock")),
        ):
            with self.subTest(device=device):
                checks = "\n".join(
                    f"out.append(busy({str(self.locks / name)!r}))" for name in paths
                )
                script = (
                    "import fcntl, pathlib\n"
                    "def busy(path):\n"
                    "    fd = open(path, 'a+')\n"
                    "    try:\n"
                    "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "    except OSError:\n"
                    "        return 'busy'\n"
                    "    return 'free'\n"
                    "out = []\n"
                    f"{checks}\n"
                    f"pathlib.Path({str(self.root / (device + '.txt'))!r})"
                    ".write_text(' '.join(out))\n"
                )
                status, _, err = self.run_hold(device, [sys.executable, "-c", script])
                self.assertEqual(0, status, err)
                self.assertEqual(
                    "busy busy", (self.root / f"{device}.txt").read_text()
                )

    def test_an_ubertooth_needs_only_one_lock_name(self):
        registry = self.load_registry()
        board = registry.get("UT0")
        self.assertEqual("", hold.plain_serial(board))  # no second name to take

    def test_a_peer_is_held_here_and_its_external_lock_is_taken(self):
        external = self.locks / f"board-esp32-{BRIDGE}.lock"
        script = (
            "import fcntl, pathlib\n"
            f"fd = open({str(external)!r}, 'a+')\n"
            "busy = False\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except OSError:\n"
            "    busy = True\n"
            f"pathlib.Path({str(self.root / 'peer')!r}).write_text('busy' if busy else 'free')\n"
        )
        status, _, _ = self.run_hold("ESP0", [sys.executable, "-c", script])
        self.assertEqual(0, status)
        self.assertEqual("busy", (self.root / "peer").read_text())

    def test_a_device_held_by_someone_else_fails_and_names_them(self):
        holder = leases.acquire(
            [leases.Board("UT0", UT_SERIAL)],
            leases.holder_info(run_id=None, owner="bob", purpose="another command"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        status, _, err = self.run_hold(
            "UT0", [sys.executable, "-c", "pass"], extra=("--wait-for-boards", "0")
        )
        # The delegated `ubertooth-harness lease` reports it and exits 2, which
        # is the status this passes on, the same as its own failures.
        self.assertEqual(2, status)
        self.assertIn("bob", err)

    def busy_script(self, paths, out_name):
        """A command that records which of these lock files it cannot take."""
        checks = "\n".join(f"out.append(busy({str(p)!r}))" for p in paths)
        return (
            "import fcntl, pathlib\n"
            "def busy(path):\n"
            "    fd = open(path, 'a+')\n"
            "    try:\n"
            "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    except OSError:\n"
            "        return 'busy'\n"
            "    return 'free'\n"
            "out = []\n"
            f"{checks}\n"
            f"pathlib.Path({str(self.root / out_name)!r}).write_text(' '.join(out))\n"
        )

    def test_a_mixed_hold_holds_every_device_in_one_attempt(self):
        paths = (
            self.leases / f"{UT_SERIAL}.lock",
            self.leases / f"esp32-{BRIDGE}.lock",
            self.locks / f"board-{UT_SERIAL}.lock",
            self.locks / f"board-esp32-{BRIDGE}.lock",
        )
        status, _, err = self.run_hold(
            "UT0,ESP0",
            [sys.executable, "-c", self.busy_script(paths, "mixed.txt")],
        )
        self.assertEqual(0, status, err)
        self.assertEqual("busy busy busy busy", (self.root / "mixed.txt").read_text())

    def test_the_host_lock_is_taken_when_asked_for_on_a_peer_only_hold(self):
        host = self.leases / "host-power.lock"
        status, _, err = self.run_hold(
            "ESP0",
            [sys.executable, "-c", self.busy_script((host,), "host.txt")],
            extra=("--host-lock",),
        )
        self.assertEqual(0, status, err)
        self.assertEqual("busy", (self.root / "host.txt").read_text())

    def test_a_firmware_write_asks_for_the_host_lock_by_itself(self):
        """The harness's own rule, so `hold` and `lease` agree on which lines."""
        host = self.leases / "host-power.lock"
        script = self.busy_script((host,), "dfu.txt")
        status, _, err = self.run_hold(
            "ESP0", [sys.executable, "-c", script, "ubertooth-dfu"]
        )
        self.assertEqual(0, status, err)
        self.assertEqual("busy", (self.root / "dfu.txt").read_text())
        self.assertIn("ubertooth-dfu", err)

    def test_a_hold_without_a_firmware_write_leaves_the_host_lock_alone(self):
        host = self.leases / "host-power.lock"
        status, _, err = self.run_hold(
            "ESP0", [sys.executable, "-c", self.busy_script((host,), "nohost.txt")]
        )
        self.assertEqual(0, status, err)
        self.assertEqual("free", (self.root / "nohost.txt").read_text())

    def test_a_serial_no_lock_can_be_named_after_is_reported_not_raised(self):
        self.registry.write_text(
            REGISTRY.replace(f"serial: {UT_SERIAL}", "serial: nothex"), encoding="utf-8"
        )
        status, _, err = self.run_hold("UT0", [sys.executable, "-c", "pass"])
        self.assertEqual(2, status)
        self.assertIn("hold: error:", err)
        self.assertNotIn("Traceback", err)

    def test_an_unknown_device_fails_before_the_command_runs(self):
        marker = self.root / "ran"
        status, _, err = self.run_hold(
            "NoSuchBoard", [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
        )
        self.assertEqual(2, status)
        self.assertFalse(marker.exists())
        self.assertIn("hold: error:", err)

    def test_one_bad_device_in_a_list_holds_nothing(self):
        marker = self.root / "ran2"
        status, _, _ = self.run_hold(
            "UT0,NoSuchBoard", [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
        )
        self.assertEqual(2, status)
        self.assertFalse(marker.exists())


class HubPortArguments(Bench):
    def test_a_port_the_controller_does_not_have_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = hub_port.main(
                ["--hardware", str(self.registry), "Hub0", "off", "9"]
            )
        self.assertEqual(2, status)
        self.assertIn("no port 9", err.getvalue())

    def test_an_unknown_action_is_a_usage_error(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            hub_port.parse_args(["--hardware", str(self.registry), "Hub0", "reboot", "1"])

    def test_an_out_of_range_cycle_time_is_refused(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            hub_port.parse_args(["--hardware", str(self.registry), "Hub0", "cycle", "1", "600"])

    def test_seconds_is_refused_for_an_action_that_does_not_wait(self):
        for action in ("on", "off"):
            with self.subTest(action=action):
                with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                    hub_port.parse_args(
                        ["--hardware", str(self.registry), "Hub0", action, "1", "5"]
                    )

    def test_cycle_gets_the_default_wait(self):
        args = hub_port.parse_args(["--hardware", str(self.registry), "Hub0", "cycle", "1"])
        self.assertEqual(hub_port.DEFAULT_CYCLE_S, args.seconds)

    def test_an_unknown_controller_fails(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = hub_port.main(["--hardware", str(self.registry), "Nope", "off", "1"])
        self.assertEqual(2, status)
        self.assertIn("hub-port: error:", err.getvalue())


class HubPortSwitches(Bench):
    def setUp(self):
        super().setUp()
        self.controller = FakeController()
        self.original = hub_port.controller_from_resource
        hub_port.controller_from_resource = lambda resource, **kw: self.controller
        self.addCleanup(setattr, hub_port, "controller_from_resource", self.original)
        self.slept: list[float] = []

    def run_hub_port(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = hub_port.main(
                ["--hardware", str(self.registry), "--lease-dir", str(self.leases), *argv],
                sleep=self.slept.append,
            )
        return status, out.getvalue(), err.getvalue()

    def test_off_switches_only_that_port_and_reports_it(self):
        status, out, _ = self.run_hub_port(["Hub0", "off", "3"])
        self.assertEqual(0, status)
        self.assertEqual([("power", 3, False)], self.controller.calls)
        self.assertIn("port 3: power off", out)
        self.assertTrue(self.controller.closed)

    def test_cycle_goes_off_then_on_and_waits_in_between(self):
        status, out, _ = self.run_hub_port(["Hub0", "cycle", "5", "3"])
        self.assertEqual(0, status)
        self.assertEqual([("power", 5, False), ("power", 5, True)], self.controller.calls)
        self.assertIn(3.0, self.slept)
        self.assertIn("port 5: power on", out)

    def test_it_holds_the_host_lock_while_it_switches(self):
        seen = {}

        def switch(controller, port, action, seconds, sleep=None):
            seen["host_lock"] = leases.host_lock_state(directory=self.leases)
            controller.set_power_enabled(port, False)

        original = hub_port.switch
        hub_port.switch = switch
        self.addCleanup(setattr, hub_port, "switch", original)
        status, _, _ = self.run_hub_port(["Hub0", "off", "1"])
        self.assertEqual(0, status)
        self.assertTrue(seen["host_lock"].get("held"))

    def test_a_cycle_whose_power_on_fails_tries_again_to_leave_the_port_on(self):
        from ubertooth_harness.power import PowerControllerError

        attempts = []
        real = self.controller.set_power_enabled

        def flaky(port, enabled):
            attempts.append(enabled)
            if enabled and attempts.count(True) == 1:
                raise PowerControllerError("write refused")
            real(port, enabled)

        self.controller.connect()
        self.controller.set_power_enabled = flaky
        with self.assertRaises(PowerControllerError) as caught:
            hub_port.switch(self.controller, 4, "cycle", 0.0, sleep=lambda _s: None)
        self.assertIn("took two attempts", str(caught.exception))
        self.assertTrue(self.controller.powered[4], "the port was left off")

    def test_a_cycle_that_cannot_power_on_at_all_says_the_port_is_off(self):
        from ubertooth_harness.power import PowerControllerError

        def never_on(port, enabled):
            if enabled:
                raise PowerControllerError("write refused")
            self.controller.powered[port] = False

        self.controller.connect()
        self.controller.set_power_enabled = never_on
        with self.assertRaises(PowerControllerError) as caught:
            hub_port.switch(self.controller, 4, "cycle", 0.0, sleep=lambda _s: None)
        message = str(caught.exception)
        self.assertIn("is OFF", message)
        self.assertIn("second attempt failed", message)

    def test_it_refuses_a_port_whose_device_someone_holds(self):
        """UT0 is mapped to port 3 in the test registry."""
        holder = leases.acquire(
            [leases.Board("UT0", UT_SERIAL)],
            leases.holder_info(run_id=None, owner="bob", purpose="a capture"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        status, _, err = self.run_hub_port(["Hub0", "cycle", "3"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("UT0", err)
        self.assertIn("bob", err)
        self.assertIn("--force", err)

    def test_force_switches_a_held_port_and_says_so(self):
        holder = leases.acquire(
            [leases.Board("UT0", UT_SERIAL)],
            leases.holder_info(run_id=None, owner="bob", purpose="a capture"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        status, _, err = self.run_hub_port(["Hub0", "off", "3", "--force"])
        self.assertEqual(0, status)
        self.assertEqual([("power", 3, False)], self.controller.calls)
        self.assertIn("--force", err)

    def test_an_empty_port_is_switched_with_no_holder_check(self):
        status, _, err = self.run_hub_port(["Hub0", "off", "7"])
        self.assertEqual(0, status, err)
        self.assertEqual([("power", 7, False)], self.controller.calls)

    def test_it_waits_for_a_held_host_lock_rather_than_switching(self):
        holder = leases.acquire(
            [],
            leases.holder_info(run_id=None, owner="a run", purpose="flashing"),
            directory=self.leases,
            host_lock=True,
        )
        self.addCleanup(holder.release)
        original = hub_port.HOST_LOCK_WAIT_S
        hub_port.HOST_LOCK_WAIT_S = 0.0
        self.addCleanup(setattr, hub_port, "HOST_LOCK_WAIT_S", original)
        status, _, err = self.run_hub_port(["Hub0", "off", "1"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("a run", err)


class LabFiles(unittest.TestCase):
    def test_both_scripts_are_executable_and_compile(self):
        for path in (HOLD, HUB_PORT):
            with self.subTest(script=path.name):
                self.assertTrue(os.access(path, os.X_OK), f"{path.name} is not executable")
                # compile() rather than py_compile, which would write bytecode
                # into the checkout.
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_help_works_without_a_registry(self):
        for path in (HOLD, HUB_PORT):
            with self.subTest(script=path.name):
                environment = dict(os.environ, PYTHONPATH=str(REPO))
                environment.pop("UBERTOOTH_HARNESS_HARDWARE", None)
                result = subprocess.run(
                    [sys.executable, str(path), "--help"],
                    capture_output=True, text=True, env=environment, timeout=60,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_the_udev_rule_names_the_acroname_vendor_and_a_group(self):
        text = RULES.read_text(encoding="utf-8")
        self.assertIn('ATTRS{idVendor}=="24ff"', text)
        self.assertRegex(text, r'GROUP="[a-z]+"')
        self.assertRegex(text, r'MODE="0[0-7]{3}"')

    def test_no_script_names_a_host_serial_or_home_path(self):
        """These are public: a bench's own identities stay in its registry."""
        banned = re.compile(
            r"/home/"                      # a lab account's home directory
            r"|[0-9]{1,3}(\.[0-9]{1,3}){3}"  # an address
            r"|\b[0-9a-f]{32}\b"             # a device serial
            r"|\b0x[0-9a-f]{8}\b",           # a hub's BrainStem serial
            re.IGNORECASE,
        )
        for path in (HOLD, HUB_PORT, RULES, LAB / "README.md"):
            with self.subTest(file=path.name):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    self.assertIsNone(
                        banned.search(line), f"{path.name}:{number}: {line.strip()}"
                    )


if __name__ == "__main__":
    unittest.main()
