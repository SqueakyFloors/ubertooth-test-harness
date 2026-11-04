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
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass

from ubertooth_harness import leases


REPO = Path(__file__).resolve().parents[1]
LAB = REPO / "scripts" / "lab"
HOLD = LAB / "hold"
HUB_PORT = LAB / "hub-port"
RULES = LAB / "99-acroname.rules"
PEER = LAB / "peer"

UT_SERIAL = "0123456789abcdef0123456789abcdef"
UT2_SERIAL = "fedcba9876543210fedcba9876543210"
# The board a debug programmer is wired to, and the programmer's own serial.
UT3_SERIAL = "99887766554433221100aabbccddeeff"
PROG_SERIAL = "A000AA00"
PROG_KEY = f"programmer-{PROG_SERIAL}"
BRIDGE = "a1b2c3d400000000a1b2c3d400000000"
BRIDGE2 = "d4c3b2a111111111d4c3b2a111111111"
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
      UT_Dbg:
        type: Ubertooth
        attached to: Host_Linux
        identity:
          serial: {UT3_SERIAL}
        power:
          controller: Hub0
          port: 2
      Prog0:
        type: Debug programmer
        attached to: Host_Linux
        programs: UT_Dbg
        identity:
          serial: "{PROG_SERIAL}"
        power:
          controller: Hub0
          port: 6
      DK0:
        type: nRF52840 DK
        attached to: Host_Linux
        roles: [advertiser]
        identity:
          jlink serial: "{JLINK}"
          BLE address: "C0:00:00:00:00:01"
        power:
          controller: Hub0
          port: 5
      ESP0:
        type: ESP32 Classic peer
        attached to: Host_Linux
        roles: [acceptor, initiator]
        identity:
          bridge serial: {BRIDGE}
          BD_ADDR: "00:11:22:33:44:55"
      ESP1:
        type: ESP32 Classic peer
        attached to: Host_Linux
        roles: [acceptor, initiator]
        identity:
          bridge serial: {BRIDGE2}
          BD_ADDR: "00:11:22:33:44:66"
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


sys.path.insert(0, str(LAB))  # the scripts import _bench from beside themselves
hold = load(HOLD)
hub_port = load(HUB_PORT)
peer = load(PEER)

import _bench  # noqa: E402  (beside the scripts, reachable from the line above)
import _checkout  # noqa: E402


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
            (PROG_SERIAL, "Prog0"),
            (PROG_SERIAL.lower(), "Prog0"),
            (PROG_SERIAL[:4], "Prog0"),
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
        # One rule, so the two scripts can never name a lock differently.
        self.assertIs(hold.lock_key, hub_port.device_board.__globals__["lock_key"])

    def test_an_ubertooth_is_resolved_by_serial_but_not_by_its_radio_address(self):
        """A BLE address or a BD_ADDR is not a device name."""
        from ubertooth_harness.resources import RegistryError

        registry = self.load_registry()
        for address in ("C0:00:00:00:00:01", "00:11:22:33:44:55"):
            with self.subTest(address=address), self.assertRaises(RegistryError):
                hold.resolve(registry, address)
        self.assertEqual("nrf52840dk-123456789", hold.lock_key(registry.get("DK0")))
        self.assertEqual(f"esp32-{BRIDGE}", hold.lock_key(registry.get("ESP0")))

    def test_the_peer_key_matches_the_harness_once_it_leases_the_type(self):
        """When the harness learns this type, its key must be the same one."""
        registry = self.load_registry()
        peer = registry.get("ESP0")
        if peer.type in leases.LEASED_TYPES:
            self.assertEqual(leases.resource_key(peer), hold.lock_key(peer))

    def test_a_programmer_is_held_by_its_own_key_and_its_bench_name(self):
        import _bench

        programmer = self.load_registry().get("Prog0")
        self.assertEqual(PROG_KEY, hold.lock_key(programmer))
        # As registered, not lowercased: the bench's own lock file is named
        # after the same string the lease key carries.
        self.assertEqual(PROG_SERIAL, _bench.plain_serial(programmer))
        second = _bench.bench_lock_board(programmer)
        # The bench's own file keeps the plain serial; the lease file beside
        # it is prefixed, so it cannot be an Ubertooth's (harness #290).
        self.assertEqual(PROG_SERIAL, second.lock_name)
        self.assertEqual(f"bench-{PROG_SERIAL}", second.key)

    def test_a_type_this_does_not_hold_is_refused(self):
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            hold.lock_key(self.load_registry().get("Hub0"))
        self.assertIn("USB power controller", str(caught.exception))


class HoldArguments(Bench):
    def parse(self, argv):
        return hold.parse_args(argv)

    def refuse(self, argv):
        """The status and the message of a usage error."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            hold.parse_args(argv)
        return caught.exception.code, err.getvalue()

    def test_the_command_after_the_separator_is_untouched(self):
        args, command = self.parse(
            ["--hardware", str(self.registry), "UT0", "--", "echo", "--hardware", "x"]
        )
        self.assertEqual(["echo", "--hardware", "x"], command)
        self.assertEqual("UT0", args.devices)

    def test_no_separator_is_a_usage_error(self):
        status, message = self.refuse(["UT0", "echo", "hi"])
        self.assertEqual(2, status)
        self.assertIn("usage:", message)

    def test_no_command_after_the_separator_is_an_error(self):
        status, message = self.refuse(["--hardware", str(self.registry), "UT0", "--"])
        self.assertEqual(2, status)
        self.assertIn("no command after --", message)

    def test_help_only_counts_as_the_first_word(self):
        """`hold UT0 mytool -h` is the tool's help, and needs a separator."""
        status, message = self.refuse(["UT0", "mytool", "-h"])
        self.assertEqual(2, status)
        self.assertIn("usage:", message)

    def test_the_registry_may_come_from_the_environment(self):
        os.environ["UBERTOOTH_HARNESS_HARDWARE"] = str(self.registry)
        self.addCleanup(os.environ.pop, "UBERTOOTH_HARNESS_HARDWARE", None)
        args, _ = self.parse(["UT0", "--", "true"])
        self.assertEqual(self.registry, Path(args.hardware))

    def test_with_no_registry_it_says_how_to_give_one(self):
        os.environ.pop("UBERTOOTH_HARNESS_HARDWARE", None)
        status, message = self.refuse(["UT0", "--", "true"])
        self.assertEqual(2, status)
        self.assertIn("UBERTOOTH_HARNESS_HARDWARE", message)

    def test_a_negative_wait_is_refused(self):
        status, _ = self.refuse(
            ["--hardware", str(self.registry), "--wait-for-boards", "-1", "UT0", "--", "true"]
        )
        self.assertEqual(2, status)

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
        self.assertEqual(
            [sys.executable, "-P", "-m", "ubertooth_harness", "lease"], argv[:5]
        )
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
        import _bench

        board = self.load_registry().get("UT0")
        self.assertEqual("", _bench.plain_serial(board))  # no second name
        self.assertIsNone(_bench.bench_lock_board(board))

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

    def test_a_board_wired_to_a_programmer_holds_it_too(self):
        """Either name takes both: the pair is one piece of hardware here."""
        paths = (
            self.leases / f"{UT3_SERIAL}.lock",
            self.leases / f"{PROG_KEY}.lock",
            self.locks / f"board-{UT3_SERIAL}.lock",
            self.locks / f"board-{PROG_KEY}.lock",
            # The bench's older name for the programmer, as registered.
            self.locks / f"board-{PROG_SERIAL}.lock",
        )
        for device, expected_note in (
            ("UT_Dbg", "Prog0 is the programmer wired to UT_Dbg"),
            ("Prog0", "UT_Dbg is the device Prog0 programs"),
            (PROG_SERIAL, "UT_Dbg is the device Prog0 programs"),
        ):
            with self.subTest(device=device):
                status, _, err = self.run_hold(
                    device,
                    [sys.executable, "-c", self.busy_script(paths, "pair.txt")],
                )
                self.assertEqual(0, status, err)
                self.assertEqual(
                    "busy " * (len(paths) - 1) + "busy",
                    (self.root / "pair.txt").read_text(),
                )
                self.assertIn(expected_note, err)
                self.assertIn("so it is held too", err)

    def test_naming_both_members_of_a_pair_is_harmless(self):
        status, _, err = self.run_hold("UT_Dbg,Prog0", [sys.executable, "-c", "pass"])
        self.assertEqual(0, status, err)
        self.assertNotIn("is held too", err)

    def test_a_held_programmer_refuses_a_hold_of_the_board_it_programs(self):
        holder = leases.acquire(
            [leases.Board("Prog0", PROG_KEY)],
            leases.holder_info(run_id=None, owner="bob", purpose="programming it"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        marker = self.root / "pair-ran"
        status, _, err = self.run_hold(
            "UT_Dbg", [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            extra=("--wait-for-boards", "0"),
        )
        self.assertEqual(2, status)
        self.assertFalse(marker.exists())
        self.assertIn("bob", err)
        # Why a command that named one device was refused another's holder.
        self.assertIn("Prog0 is the programmer wired to UT_Dbg", err)

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

    def test_a_negative_wait_is_refused(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            hub_port.parse_args(
                ["--hardware", str(self.registry), "--wait", "-1", "Hub0", "off", "1"]
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
                ["--hardware", str(self.registry), "--lease-dir", str(self.leases),
                 "--wait", "0", *argv],
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

    def test_a_signal_during_a_cycle_is_turned_into_a_cancellation(self):
        """A dropped SSH session must not leave a port off."""
        with self.assertRaises(KeyboardInterrupt):
            hub_port._cancel(15, None)

    def test_a_signal_right_after_the_off_write_still_leaves_the_port_on(self):
        """The restore guard is armed before the write, not after it."""
        real = self.controller.set_power_enabled

        def off_then_interrupt(port, enabled):
            real(port, enabled)
            if not enabled:
                raise KeyboardInterrupt  # delivered as the write returns

        self.controller.connect()
        self.controller.set_power_enabled = off_then_interrupt
        with self.assertRaises(KeyboardInterrupt), redirect_stderr(io.StringIO()):
            hub_port.switch(self.controller, 4, "cycle", 5.0, sleep=lambda _s: None)
        self.assertTrue(self.controller.powered[4], "the port was left off")

    def test_a_restore_reconnects_a_driver_that_closed_its_link(self):
        """The StarTech driver closes its link on any write error."""
        from ubertooth_harness.power import PowerControllerError

        attempts = []
        real = self.controller.set_power_enabled

        def closes_on_failure(port, enabled):
            attempts.append(enabled)
            if enabled and attempts.count(True) == 1:
                self.controller.connected = False  # as close() leaves it
                raise PowerControllerError("write refused")
            real(port, enabled)  # raises AssertionError if still disconnected

        self.controller.connect()
        self.controller.set_power_enabled = closes_on_failure
        err = io.StringIO()
        with self.assertRaises(PowerControllerError), redirect_stderr(err):
            hub_port.switch(self.controller, 4, "cycle", 0.0, sleep=lambda _s: None)
        self.assertIn("switched back on", err.getvalue())
        self.assertTrue(self.controller.powered[4], "the port was left off")

    def test_an_interrupted_cycle_still_leaves_the_port_on(self):
        """Ctrl-C in the wait must not leave a device without power."""
        def interrupt(_seconds):
            raise KeyboardInterrupt

        self.controller.connect()
        with self.assertRaises(KeyboardInterrupt), redirect_stderr(io.StringIO()):
            hub_port.switch(self.controller, 4, "cycle", 5.0, sleep=interrupt)
        self.assertTrue(self.controller.powered[4], "the port was left off")
        self.assertEqual(
            [("power", 4, False), ("power", 4, True)], self.controller.calls
        )

    def test_a_cycle_whose_power_on_fails_is_retried_and_reported(self):
        """The port ends on, and the write's own error is what comes out."""
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
        err = io.StringIO()
        with self.assertRaises(PowerControllerError) as caught, redirect_stderr(err):
            hub_port.switch(self.controller, 4, "cycle", 0.0, sleep=lambda _s: None)
        self.assertIn("write refused", str(caught.exception))
        self.assertIn("switched back on", err.getvalue())
        self.assertTrue(self.controller.powered[4], "the port was left off")

    def test_a_cycle_that_cannot_power_on_at_all_says_the_port_is_off(self):
        from ubertooth_harness.power import PowerControllerError

        def never_on(port, enabled):
            if enabled:
                raise PowerControllerError("write refused")
            self.controller.powered[port] = False

        self.controller.connect()
        self.controller.set_power_enabled = never_on
        err = io.StringIO()
        with self.assertRaises(PowerControllerError), redirect_stderr(err):
            hub_port.switch(self.controller, 4, "cycle", 0.0, sleep=lambda _s: None)
        self.assertIn("is OFF", err.getvalue())
        self.assertFalse(self.controller.powered[4])

    def test_it_refuses_a_port_whose_device_someone_holds(self):
        """UT0 is mapped to port 3 in the test registry.

        The device is held for the switch, not merely checked, so there is no
        window in which a run can take it and lose its power.
        """
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
        self.assertIn("--wait SECONDS", err)
        # Not two different wait options in one message: the harness's own
        # advice names an option this command does not have.
        self.assertNotIn("--wait-for-boards", err)

    def test_the_device_is_held_while_its_port_is_switched(self):
        seen = {}

        def switch(controller, port, action, seconds, sleep=None):
            seen["states"] = leases.board_states(
                [leases.Board("UT0", UT_SERIAL)], directory=self.leases
            )
            controller.set_power_enabled(port, False)

        original = hub_port.switch
        hub_port.switch = switch
        self.addCleanup(setattr, hub_port, "switch", original)
        status, _, err = self.run_hub_port(["Hub0", "off", "3"])
        self.assertEqual(0, status, err)
        self.assertTrue(seen["states"][0]["held"])

    def test_a_peer_on_a_port_is_held_too(self):
        """A type the harness does not lease must not read as free here."""
        registry = self.load_registry()
        peer = registry.get("ESP0")
        self.assertEqual(f"esp32-{BRIDGE}", hub_port.device_board(peer).key)

    def test_a_held_bench_lock_name_also_prevents_switching(self):
        """A hold of only the older name must be met here too.

        That hold takes the bench's own `board-<plain serial>.lock`, which is
        where a bench script written against the serial meets it, and the
        prefixed lease file beside it.
        """
        holder = leases.acquire(
            [leases.Board(
                "DK0 (bench lock)", leases.bench_lock_key(JLINK), external_key=JLINK
            )],
            leases.holder_info(run_id=None, owner="bob", purpose="a DK session"),
            directory=self.leases,
            external_template=self.template,
        )
        self.addCleanup(holder.release)
        # DK0 is mapped to port 5 in the registry below.
        status, _, err = self.run_hub_port(["Hub0", "cycle", "5"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("bob", err)

    def test_a_programmer_is_held_while_its_board_s_port_is_switched(self):
        """UT_Dbg is on port 2, with Prog0 wired to it.

        Cutting the board's power while it is being programmed leaves it
        needing hands on it, so the programmer is held for the switch.
        """
        seen = {}

        def switch(controller, port, action, seconds, sleep=None):
            seen["states"] = leases.board_states(
                [leases.Board("Prog0", PROG_KEY)], directory=self.leases
            )
            controller.set_power_enabled(port, False)

        original = hub_port.switch
        hub_port.switch = switch
        self.addCleanup(setattr, hub_port, "switch", original)
        status, _, err = self.run_hub_port(["Hub0", "off", "2"])
        self.assertEqual(0, status, err)
        self.assertTrue(seen["states"][0]["held"])
        self.assertIn("Prog0 is the programmer wired to UT_Dbg", err)

    def test_it_refuses_a_port_whose_device_s_programmer_someone_holds(self):
        holder = leases.acquire(
            [leases.Board("Prog0", PROG_KEY)],
            leases.holder_info(run_id=None, owner="bob", purpose="programming it"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        status, _, err = self.run_hub_port(["Hub0", "cycle", "2"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("bob", err)

    def test_it_refuses_the_programmer_s_own_port_while_its_board_is_held(self):
        """Prog0 is on port 6: the pair holds both ways."""
        holder = leases.acquire(
            [leases.Board("UT_Dbg", UT3_SERIAL)],
            leases.holder_info(run_id=None, owner="bob", purpose="a capture"),
            directory=self.leases,
        )
        self.addCleanup(holder.release)
        status, _, err = self.run_hub_port(["Hub0", "cycle", "6"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("bob", err)

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
        self.assertIn("UT0", err)

    def test_force_names_every_device_it_leaves_unheld(self):
        """A pair member is unheld too, so the warning has to name it."""
        status, _, err = self.run_hub_port(["Hub0", "off", "2", "--force"])
        self.assertEqual(0, status, err)
        self.assertEqual([("power", 2, False)], self.controller.calls)
        self.assertIn("UT_Dbg", err)
        self.assertIn("Prog0", err)
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
        status, _, err = self.run_hub_port(["Hub0", "off", "1"])
        self.assertEqual(2, status)
        self.assertEqual([], self.controller.calls)
        self.assertIn("a run", err)
        self.assertIn("--wait SECONDS", err)
        # The host lock's message has its own wording; neither form may leave
        # an option this command does not accept in the advice.
        self.assertNotIn("--wait-for-boards", err)


class PeerArguments(Bench):
    """scripts/lab/peer: it translates names, and delegates everything else."""

    def test_the_bridge_path_comes_from_the_serial_or_the_registry(self):
        registry = self.load_registry()
        self.assertIn(BRIDGE, peer.peer_device(registry.get("ESP0")))
        self.assertTrue(peer.peer_device(registry.get("ESP0")).startswith("/dev/serial/by-id/"))
        # The fixture is dedented, so the indentation is taken from the line
        # itself rather than assumed.
        named = re.sub(
            rf"( *)bridge serial: {BRIDGE}",
            lambda m: f"{m.group(0)}\n{m.group(1)}serial device: /dev/serial/by-id/chosen",
            REGISTRY, count=1,
        )
        self.assertIn("serial device", named)
        self.registry.write_text(named, encoding="utf-8")
        self.assertEqual(
            "/dev/serial/by-id/chosen", peer.peer_device(self.load_registry().get("ESP0"))
        )

    def test_a_resource_that_is_not_a_peer_is_refused(self):
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            peer.require_peer(self.load_registry().get("UT0"))
        self.assertIn("not an ESP32 Classic peer", str(caught.exception))

    def test_one_peer_command_passes_the_device_address_and_alias(self):
        args, command = peer.parse_args(
            ["status", "ESP0", "--hardware", str(self.registry)]
        )
        argv = peer.esp32peer_argv(
            args, {"peer": self.load_registry().get("ESP0")}, command
        )
        self.assertEqual("status", argv[2])
        pairs = dict(zip(argv, argv[1:]))
        self.assertIn(BRIDGE, pairs["--device"])
        self.assertEqual("00:11:22:33:44:55", pairs["--address"])
        self.assertEqual("ESP0", pairs["--alias"])
        self.assertNotIn("--", argv[3:])

    def test_stream_passes_both_roles_and_the_command_after_a_separator(self):
        args, command = peer.parse_args([
            "stream", "ESP0", "ESP1", "--hardware", str(self.registry),
            "--period", "10", "--payload", "300", "--min-writes", "50",
            "--", "ubertooth-rx", "-z", "-t", "30",
        ])
        registry = self.load_registry()
        argv = peer.esp32peer_argv(
            args, {"acceptor": registry.get("ESP0"), "initiator": registry.get("ESP1")},
            command,
        )
        self.assertEqual("stream", argv[2])
        pairs = dict(zip(argv, argv[1:]))
        # The first positional is the acceptor and the second the initiator.
        # That mapping decides which LAP is on the air, so a swap must fail.
        self.assertEqual("00:11:22:33:44:55", pairs["--acceptor-address"])
        self.assertEqual("00:11:22:33:44:66", pairs["--initiator-address"])
        self.assertEqual("ESP0", pairs["--acceptor-alias"])
        self.assertEqual("ESP1", pairs["--initiator-alias"])
        self.assertIn(BRIDGE, pairs["--acceptor"])
        self.assertIn(BRIDGE2, pairs["--initiator"])
        # Each value with its own option, so swapping two values fails.
        self.assertEqual("10", pairs["--period"])
        self.assertEqual("300", pairs["--payload"])
        self.assertEqual("50", pairs["--min-writes"])
        self.assertEqual(["--", "ubertooth-rx", "-z", "-t", "30"], argv[-5:])

    def test_audio_passes_both_roles_the_packet_type_and_the_command(self):
        args, command = peer.parse_args([
            "audio", "ESP0", "ESP1", "--hardware", str(self.registry),
            "--type", "ev3", "--min-frames", "1000",
            "--min-seconds", "25", "--", "ubertooth-rx", "-l", "e9c5ba",
        ])
        registry = self.load_registry()
        argv = peer.esp32peer_argv(
            args, {"acceptor": registry.get("ESP0"), "initiator": registry.get("ESP1")},
            command,
        )
        self.assertEqual("audio", argv[2])
        pairs = dict(zip(argv, argv[1:]))
        self.assertEqual("00:11:22:33:44:55", pairs["--acceptor-address"])
        self.assertEqual("00:11:22:33:44:66", pairs["--initiator-address"])
        self.assertEqual("ev3", pairs["--type"])
        self.assertEqual("1000", pairs["--min-frames"])
        self.assertEqual("25.0", pairs["--min-seconds"])
        # A stream option has no meaning for an audio link, so it is not sent.
        for absent in ("--period", "--payload", "--min-writes"):
            self.assertNotIn(absent, argv)
        self.assertEqual(["--", "ubertooth-rx", "-l", "e9c5ba"], argv[-4:])

    def test_an_audio_option_not_given_is_left_to_the_delegate(self):
        args, command = peer.parse_args(
            ["audio", "ESP0", "ESP1", "--hardware", str(self.registry), "--", "true"]
        )
        registry = self.load_registry()
        argv = peer.esp32peer_argv(
            args, {"acceptor": registry.get("ESP0"), "initiator": registry.get("ESP1")},
            command,
        )
        for absent in ("--type", "--min-frames", "--min-seconds",
                       "--timeout"):
            self.assertNotIn(absent, argv)

    def test_the_script_refuses_a_packet_type_it_has_no_name_for(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args([
                "audio", "ESP0", "ESP1", "--hardware", str(self.registry),
                "--type", "ev4", "--", "true",
            ])
        self.assertEqual(2, caught.exception.code)

    def test_the_script_refuses_a_negative_frame_floor(self):
        # Refused before anything is held, so no board is taken and reset for
        # a command line that cannot check anything.
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args([
                "audio", "ESP0", "ESP1", "--hardware", str(self.registry),
                "--min-frames", "-1", "--", "true",
            ])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("--min-frames cannot be negative", err.getvalue())

    def test_audio_holds_the_pair_and_the_capturing_board_together(self):
        args, _command = peer.parse_args([
            "audio", "ESP0", "ESP1", "--hardware", str(self.registry),
            "--with", "UT0", "--", "true",
        ])
        argv = peer.hold_argv(args, ["ESP0", "ESP1", "UT0"], ["inner"])
        self.assertIn("ESP0,ESP1,UT0", argv)

    def test_audio_without_a_command_is_a_usage_error(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args(["audio", "ESP0", "ESP1", "--hardware", str(self.registry)])
        self.assertEqual(2, caught.exception.code)
        # No separator at all, so the usage line is what names both sessions.
        self.assertIn("peer stream|audio <acceptor> <initiator>", err.getvalue())

    def test_audio_with_a_separator_and_nothing_after_it_names_the_command(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args([
                "audio", "ESP0", "ESP1", "--hardware", str(self.registry), "--",
            ])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("audio needs the command to run after --", err.getvalue())

    def test_one_board_cannot_be_both_peers_of_an_audio_link(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = peer.main(
                ["audio", "ESP0", "ESP0", "--hardware", str(self.registry),
                 "--", "true"]
            )
        self.assertEqual(2, status)
        self.assertIn("cannot be both peers", err.getvalue())

    def test_an_option_not_given_is_left_to_the_delegate(self):
        args, command = peer.parse_args(
            ["stream", "ESP0", "ESP1", "--hardware", str(self.registry), "--", "true"]
        )
        registry = self.load_registry()
        argv = peer.esp32peer_argv(
            args, {"acceptor": registry.get("ESP0"), "initiator": registry.get("ESP1")},
            command,
        )
        for absent in ("--period", "--payload", "--min-writes", "--min-seconds", "--timeout"):
            self.assertNotIn(absent, argv)

    def test_the_hold_it_wraps_itself_in_names_every_device(self):
        args, command = peer.parse_args([
            "stream", "ESP0", "ESP1", "--hardware", str(self.registry),
            "--with", "UT0", "--external-board-lock", self.template,
            "--lease-dir", str(self.leases), "--owner", "alice", "--", "true",
        ])
        argv = peer.hold_argv(args, ["ESP0", "ESP1", "UT0"], ["inner"])
        self.assertEqual(str(HOLD), argv[1])
        # Not forwarded unless asked for, so hold owns the default.
        self.assertNotIn("--wait-for-boards", argv)
        self.assertIn("ESP0,ESP1,UT0", argv)
        self.assertIn(self.template, argv)
        self.assertIn("alice", argv)
        self.assertEqual(["--", "inner"], argv[-2:])

    def test_one_board_cannot_be_both_peers(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = peer.main(
                ["stream", "ESP0", "ESP0", "--hardware", str(self.registry),
                 "--", "true"]
            )
        self.assertEqual(2, status)
        self.assertIn("cannot be both peers", err.getvalue())

    def test_stream_without_a_command_is_a_usage_error(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args(["stream", "ESP0", "ESP1", "--hardware", str(self.registry)])
        self.assertEqual(2, caught.exception.code)

    def test_a_single_peer_command_takes_no_command(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args(
                ["status", "ESP0", "--hardware", str(self.registry), "--", "true"]
            )
        self.assertEqual(2, caught.exception.code)
        self.assertIn("takes no command", err.getvalue())

    def run_peer(self, argv):
        """main() with the delegate replaced: what it would run, and its status."""
        seen = []

        def call(command, **kwargs):
            seen.append(list(command))
            return 7  # a status that is neither success nor a usage error

        with unittest.mock.patch.object(peer.subprocess, "call", call):
            status = peer.main(argv)
        return status, (seen[0] if seen else [])

    def test_no_hold_runs_the_delegate_directly_and_passes_its_status_on(self):
        """Inside an outer hold, holding again would wait for its own lock."""
        status, argv = self.run_peer([
            "status", "ESP0", "--hardware", str(self.registry), "--no-hold",
        ])
        self.assertEqual(7, status)
        self.assertEqual(str(peer.ESP32PEER), argv[1])
        self.assertNotIn(str(HOLD), argv)

    def test_with_cannot_go_with_no_hold(self):
        """Resolving --with and then not holding it would drop a lock silently."""
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            peer.parse_args([
                "stream", "ESP0", "ESP1", "--hardware", str(self.registry),
                "--with", "UT0", "--no-hold", "--", "true",
            ])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("--no-hold", err.getvalue())

    def test_a_signal_killed_delegate_reads_the_same_either_way(self):
        """128+N, as the hold path gives, not subprocess's raw negative."""
        with unittest.mock.patch.object(peer.subprocess, "call", lambda *a, **k: -15):
            status = peer.main(
                ["status", "ESP0", "--hardware", str(self.registry), "--no-hold"]
            )
        self.assertEqual(143, status)

    def test_without_no_hold_it_runs_the_delegate_inside_hold(self):
        status, argv = self.run_peer([
            "status", "ESP0", "--hardware", str(self.registry),
        ])
        self.assertEqual(7, status)
        self.assertEqual(str(HOLD), argv[1])
        self.assertIn(str(peer.ESP32PEER), argv)

    def test_it_delegates_to_the_harness_module_not_a_copy(self):
        from ubertooth_harness import esp32peer as module

        self.assertEqual(Path(module.__file__).resolve(), peer.ESP32PEER)


class CheckoutEnvironment(Bench):
    """The harness a helper uses is its own checkout's (harness #312).

    A wrapper that named another checkout's interpreter, and ran from inside
    that checkout, is how one `hold` came to read the registry with one
    version of the harness and lease with a much older one. Each test here
    closes one of the ways that happened.
    """

    def decoy_checkout(self) -> Path:
        """A directory holding a harness that must never be imported.

        Importing it raises, so a test fails with the decoy's own words
        rather than with whatever an old harness would have done.
        """
        decoy = self.root / "decoy"
        (decoy / "ubertooth_harness").mkdir(parents=True)
        (decoy / "ubertooth_harness" / "__init__.py").write_text(
            "raise SystemExit('decoy harness imported')\n", encoding="utf-8"
        )
        return decoy

    def test_the_delegate_ignores_a_harness_in_the_working_directory(self):
        """The regression: `-m` puts the working directory first (#312).

        A bench wrapper that ran `hold` from inside another checkout handed
        its locks to that checkout's harness, whatever PYTHONPATH said. Run
        from a directory holding a harness of its own, the hold must still
        be this checkout's, start to finish.
        """
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("UBERTOOTH_HARNESS_HARDWARE", None)
        result = subprocess.run(
            [
                sys.executable, str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", sys.executable, "-c", "print('held')",
            ],
            capture_output=True, text=True, env=environment,
            cwd=str(self.decoy_checkout()), timeout=120,
        )
        self.assertNotIn("decoy harness imported", result.stdout + result.stderr)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("held", result.stdout)

    def outside_interpreter(self) -> Path:
        """An interpreter no checkout bootstrapped, as a bench wrapper gives.

        The environment this suite runs in was built from it, so it exists
        wherever the suite does. Skipped rather than assumed, and checked
        rather than assumed to be unbootstrapped: a helper succeeding under
        it is only evidence of a re-run while that holds.
        """
        outside = Path(sys.base_prefix) / "bin" / "python3"
        if not outside.is_file():
            self.skipTest(f"no interpreter outside the environment at {outside}")
        asked = subprocess.run(
            [str(outside), "-c",
             "import pathlib, sys;"
             f"print((pathlib.Path(sys.prefix) / {_checkout.VENV_MARKER!r}).is_file())"],
            capture_output=True, text=True, timeout=60,
        )
        if asked.stdout.strip() != "False":
            self.skipTest(f"{outside} reports itself a harness environment")
        return outside

    def plain_environment(self) -> dict:
        """What a shell gives: no PYTHONPATH, no harness variables."""
        environment = dict(os.environ)
        for name in ("PYTHONPATH", "UBERTOOTH_HARNESS_HARDWARE"):
            environment.pop(name, None)
        return environment

    def test_another_interpreter_starting_a_helper_re_runs_it_under_this_one(self):
        """Any interpreter may start a helper; only one may finish it.

        An interpreter nobody bootstrapped is turned down, so a hold that
        completes under one is proof that the re-run happened. Remove the
        re-run and leave the child's `-P`, and this fails.
        """
        outside = self.outside_interpreter()
        result = subprocess.run(
            [
                str(outside), str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", "sh", "-c", "echo held",
            ],
            capture_output=True, text=True, env=self.plain_environment(),
            cwd=str(self.decoy_checkout()), timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("held", result.stdout)

    def test_every_helper_re_runs_itself_not_only_hold(self):
        """`peer` and `hub-port` carry the same locks and the same risk."""
        outside = self.outside_interpreter()
        for path in (HOLD, HUB_PORT, PEER):
            with self.subTest(script=path.name):
                result = subprocess.run(
                    [str(outside), str(path), "--help"],
                    capture_output=True, text=True,
                    env=self.plain_environment(),
                    cwd=str(self.root), timeout=120,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_the_re_run_leaves_nothing_in_the_commands_environment(self):
        """The command under `hold` may be a helper with its own to make,
        so it must not inherit anything about this one's."""
        outside = self.outside_interpreter()
        result = subprocess.run(
            [
                str(outside), str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", "sh", "-c",
                "echo \"harness vars: [$(env | grep -c UBERTOOTH_HARNESS_LAB)]\"",
            ],
            capture_output=True, text=True, env=self.plain_environment(),
            cwd=str(self.root), timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("harness vars: [0]", result.stdout)

    def test_a_venv_nobody_bootstrapped_is_not_this_checkouts_environment(self):
        """`.venv` is a path, not a promise: `python -m venv` makes one too,
        and running a device helper in one is what bootstrap.py avoids."""
        plain = self.root / "plain-venv"
        (plain / "bin").mkdir(parents=True)
        with unittest.mock.patch.object(_checkout, "VENV", plain):
            with unittest.mock.patch.object(sys, "prefix", str(plain)):
                self.assertFalse(_checkout.in_harness_environment())
                self.assertFalse(_checkout.in_checkout_environment())
        marked = self.root / "marked-venv"
        (marked / "bin").mkdir(parents=True)
        (marked / _checkout.VENV_MARKER).write_text("", encoding="utf-8")
        with unittest.mock.patch.object(_checkout, "VENV", marked):
            with unittest.mock.patch.object(sys, "prefix", str(marked)):
                self.assertTrue(_checkout.in_checkout_environment())

    def test_a_checkout_path_that_cannot_go_on_an_import_path_is_refused(self):
        """It would arrive as two entries, neither of them this checkout."""
        colon = self.root / f"check{os.pathsep}out"
        with unittest.mock.patch.object(_checkout, "CHECKOUT", colon):
            self.assertFalse(_checkout.importable_checkout_path())
            with self.assertRaises(SystemExit) as caught:
                _checkout.run_in_checkout_environment(str(HOLD), "hold")
        self.assertIn(str(colon), str(caught.exception))
        self.assertIn("import path", str(caught.exception))
        self.assertTrue(_checkout.importable_checkout_path())

    def test_unbuffered_output_survives_the_re_run(self):
        """A hold runs as long as its command; output through a pipe should
        not start buffering because the script re-ran itself."""
        with unittest.mock.patch.object(sys, "argv", ["hold", "UT0", "--", "true"]):
            with unittest.mock.patch.object(_checkout, "unbuffered", lambda: True):
                self.assertEqual(
                    ["-P", "-u", str(HOLD)], _checkout.reexec_argv(str(HOLD))[1:4]
                )
            with unittest.mock.patch.object(_checkout, "unbuffered", lambda: False):
                self.assertEqual(
                    ["-P", str(HOLD)], _checkout.reexec_argv(str(HOLD))[1:3]
                )

    def test_the_u_flag_is_read_from_what_it_leaves_behind(self):
        """`sys.flags` has nothing for `-u`; a write-through stdout does.

        Asked of real interpreters, with the output piped, because that is
        the case the carrying is for and the one an attribute check could
        get wrong.
        """
        for flag, expected in ((["-u"], "True"), ([], "False")):
            with self.subTest(flag=flag or "none"):
                probe = subprocess.run(
                    [sys.executable, *flag, "-c",
                     f"import sys; sys.path.insert(0, {str(LAB)!r});"
                     " import _checkout; print(_checkout.unbuffered())"],
                    capture_output=True, text=True, timeout=60,
                )
                self.assertEqual(expected, probe.stdout.strip(), probe.stderr)

    def test_an_unexecutable_environment_is_refused_not_a_traceback(self):
        """Executable and not an executable image: os.execv raises."""
        broken = self.root / "broken" / "bin" / "python"
        broken.parent.mkdir(parents=True)
        broken.write_text("not an executable image\n", encoding="utf-8")
        broken.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", broken):
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=False
            ):
                with unittest.mock.patch.object(
                    _checkout, "in_harness_environment", return_value=False
                ):
                    # Past the probe, which would otherwise answer for this
                    # file before the exec was reached, so what this
                    # exercises is the exec's own failure.
                    with unittest.mock.patch.object(
                        _checkout, "venv_python_would_arrive", return_value=True
                    ):
                        with unittest.mock.patch.object(
                            _checkout.os, "execv",
                            side_effect=OSError("exec format"),
                        ):
                            with self.assertRaises(SystemExit) as caught:
                                _checkout.run_in_checkout_environment(
                                    str(HOLD), "hold"
                                )
        self.assertIn("did not confirm", str(caught.exception))
        # The attempt did not happen, so nothing is left behind to stop a
        # process that goes on to make one.
        self.assertNotIn(_checkout.ATTEMPT_ENV, os.environ)

    def test_the_re_run_names_this_checkouts_interpreter_and_the_script(self):
        given = ["UT0,DK0", "--", "ubertooth-util", "-U", "x", "-v"]
        with unittest.mock.patch.object(sys, "argv", ["hold", *given]):
            argv = _checkout.reexec_argv(str(HOLD))
        self.assertEqual(str(_checkout.VENV_PYTHON), argv[0])
        # The script's own path, and nothing of the caller's before it.
        self.assertEqual(["-P", str(HOLD)], argv[1:3])
        # Every argument through untouched, separator and all: a lost `--`
        # would turn the command into devices to hold.
        self.assertEqual(given, argv[3:])

    def test_the_interpreter_is_asked_before_it_is_run(self):
        """The same predicate, put to the candidate: a `True` means the
        re-run returns at `in_checkout_environment` and does nothing more,
        which is what makes one attempt the only attempt."""
        self.assertTrue(_checkout.venv_python_would_arrive())
        saying_no = self.root / "says-no" / "python"
        saying_no.parent.mkdir(parents=True)
        saying_no.write_text("#!/bin/sh\necho False\n", encoding="utf-8")
        saying_no.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", saying_no):
            self.assertFalse(_checkout.venv_python_would_arrive())

    def test_an_interpreter_that_cannot_answer_is_not_run(self):
        """A `#!` wrapper passes every test a file can pass and reports the
        interpreter behind it, so a guard that looked at the answer
        afterwards would re-run for ever. This asks first."""
        wrapper = self.root / "wrapper" / "python"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(
            f"#!{sys.executable}\n# a wrapper, not an interpreter\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", wrapper):
            self.assertFalse(_checkout.venv_python_would_arrive())
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=False
            ):
                with unittest.mock.patch.object(
                    _checkout, "in_harness_environment", return_value=False
                ):
                    with unittest.mock.patch.object(
                        _checkout.os, "execv"
                    ) as execv:
                        with self.assertRaises(SystemExit) as caught:
                            _checkout.run_in_checkout_environment(str(HOLD), "hold")
        execv.assert_not_called()
        self.assertIn("bootstrap.py", str(caught.exception))

    def test_a_banner_from_someone_elses_sitecustomize_is_not_an_answer(self):
        """An interpreter may print at startup for reasons of its own. A
        good environment must not be turned down over a banner, and a
        banner must not be able to pass for the answer."""
        noisy = self.root / "noisy" / "python"
        noisy.parent.mkdir(parents=True)
        noisy.write_text(
            "#!/bin/sh\necho 'loading site customizations'\necho True\n",
            encoding="utf-8",
        )
        noisy.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", noisy):
            self.assertTrue(_checkout.venv_python_would_arrive())
        lying = self.root / "lying" / "python"
        lying.parent.mkdir(parents=True)
        lying.write_text(
            "#!/bin/sh\necho 'True is a fine word'\necho False\n",
            encoding="utf-8",
        )
        lying.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", lying):
            self.assertFalse(_checkout.venv_python_would_arrive())

    def test_an_interpreter_that_answers_one_way_and_runs_another_still_ends(self):
        """Not a threat -- the same write access reaches this module -- but a
        helper on a bench has to end whatever it is given, so the attempts
        are bounded as well as decided."""
        self.assertFalse(_checkout.attempted_once({}))
        self.assertFalse(
            _checkout.attempted_once({_checkout.ATTEMPT_ENV: "/elsewhere"})
        )
        self.assertTrue(
            _checkout.attempted_once(
                {_checkout.ATTEMPT_ENV: str(_checkout.CHECKOUT)}
            )
        )
        with unittest.mock.patch.dict(
            _checkout.os.environ, {_checkout.ATTEMPT_ENV: str(_checkout.CHECKOUT)}
        ):
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=False
            ):
                with unittest.mock.patch.object(
                    _checkout, "in_harness_environment", return_value=False
                ):
                    with unittest.mock.patch.object(
                        _checkout, "venv_python_would_arrive", return_value=True
                    ) as asked:
                        with unittest.mock.patch.object(
                            _checkout.os, "execv"
                        ) as execv:
                            with self.assertRaises(SystemExit):
                                _checkout.run_in_checkout_environment(
                                    str(HOLD), "hold"
                                )
        execv.assert_not_called()
        asked.assert_not_called()

    def test_the_attempt_note_does_not_reach_the_command(self):
        outside = self.outside_interpreter()
        result = subprocess.run(
            [
                str(outside), str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", "sh", "-c",
                "echo \"attempt vars: [$(env | grep -c UBERTOOTH_HARNESS_LAB)]\"",
            ],
            capture_output=True, text=True, env=self.plain_environment(),
            cwd=str(self.root), timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("attempt vars: [0]", result.stdout)

    def test_an_environment_that_would_not_arrive_is_told_apart_from_none(self):
        """"Run bootstrap.py" is wrong advice for a checkout whose
        environment is fine and whose interpreter cannot be reached."""
        advice = _checkout.unreachable_environment_advice("hold")
        self.assertIn(str(_checkout.VENV_PYTHON), advice)
        self.assertIn("PYTHONEXECUTABLE", advice)
        self.assertNotIn("has no environment yet", advice)
        # bootstrap.py refuses to touch a `.venv` it did not build, even
        # with --recreate, and that is the likeliest cause of all, so the
        # remedy it does accept comes first.
        self.assertIn("move it aside", advice)
        self.assertLess(advice.index("move it aside"), advice.index("--recreate"))
        outside = self.outside_interpreter()
        environment = self.plain_environment()
        environment["PYTHONEXECUTABLE"] = "/usr/bin/not-this-interpreter"
        result = subprocess.run(
            [str(outside), str(HOLD), "--help"],
            capture_output=True, text=True, env=environment,
            cwd=str(self.root), timeout=120,
        )
        if result.returncode != 0:
            self.assertIn("did not confirm", result.stderr)
            self.assertNotIn("has no environment yet", result.stderr)

    def test_an_interpreter_there_and_not_executable_is_not_called_absent(self):
        """A malformed environment is not a missing one, and the advice for
        a missing one would send the operator the wrong way."""
        unrunnable = self.root / "unrunnable" / "python"
        unrunnable.parent.mkdir(parents=True)
        unrunnable.write_text("", encoding="utf-8")
        unrunnable.chmod(0o644)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", unrunnable):
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=False
            ):
                with unittest.mock.patch.object(
                    _checkout, "in_harness_environment", return_value=False
                ):
                    with self.assertRaises(SystemExit) as caught:
                        _checkout.run_in_checkout_environment(str(HOLD), "hold")
        self.assertIn("did not confirm", str(caught.exception))

    def test_output_that_is_not_text_is_an_answer_not_a_traceback(self):
        """A file in place of an interpreter may write anything at all."""
        binary = self.root / "binary" / "python"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/bin/sh\nprintf '\\377\\376 not text\\n'\n", encoding="utf-8"
        )
        binary.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", binary):
            self.assertFalse(_checkout.venv_python_would_arrive())

    def test_an_interpreter_that_never_answers_is_not_waited_on(self):
        """A bench gets an answer or a refusal, not a helper that hangs."""
        slow = self.root / "slow" / "python"
        slow.parent.mkdir(parents=True)
        slow.write_text("#!/bin/sh\nsleep 600\n", encoding="utf-8")
        slow.chmod(0o755)
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", slow):
            with unittest.mock.patch.object(_checkout, "PROBE_TIMEOUT_S", 2):
                self.assertFalse(_checkout.venv_python_would_arrive())

    def test_a_forged_interpreter_name_ends_in_a_refusal_not_a_loop(self):
        """On a macOS framework build `PYTHONEXECUTABLE` decides what an
        interpreter calls itself, and app-bundle launchers set it, so this
        reaches a bench by accident as well as on purpose. Either way it
        has to end."""
        outside = self.outside_interpreter()
        environment = self.plain_environment()
        environment["PYTHONEXECUTABLE"] = "/usr/bin/not-this-interpreter"
        result = subprocess.run(
            [str(outside), str(HOLD), "--help"],
            capture_output=True, text=True, env=environment,
            cwd=str(self.root), timeout=120,
        )
        # Whichever way it goes, it goes: the usage text if the interpreter
        # still arrives, a refusal naming it if it cannot.
        self.assertIn(
            "usage:" if result.returncode == 0 else "did not confirm",
            result.stdout + result.stderr,
        )

    def test_what_a_caller_can_arrange_is_a_refusal_at_most(self):
        """Setting the attempt note is how a caller would try to keep a
        helper out of its checkout's environment. What it buys is the
        refusal, not a hold in whatever environment the caller preferred."""
        outside = self.outside_interpreter()
        environment = self.plain_environment()
        environment[_checkout.ATTEMPT_ENV] = str(REPO)
        result = subprocess.run(
            [
                str(outside), str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", "sh", "-c", "echo held",
            ],
            capture_output=True, text=True, env=environment,
            cwd=str(self.root), timeout=120,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("did not confirm", result.stderr)
        self.assertNotIn("held", result.stdout)

    def test_a_pythonpath_a_caller_sets_does_not_answer_for_the_delegate(self):
        """The checkout is named ahead of it, and the working directory is
        off the delegate's path entirely."""
        outside = self.outside_interpreter()
        environment = self.plain_environment()
        environment["PYTHONPATH"] = str(self.decoy_checkout())
        result = subprocess.run(
            [
                str(outside), str(HOLD),
                "--hardware", str(self.registry),
                "--lease-dir", str(self.leases),
                "--wait-for-boards", "0",
                "UT0", "--", "sh", "-c", "echo held",
            ],
            capture_output=True, text=True, env=environment,
            cwd=str(self.root), timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("held", result.stdout)
        self.assertNotIn("decoy harness imported", result.stdout + result.stderr)

    def test_it_does_not_re_run_when_it_is_already_this_environment(self):
        with unittest.mock.patch.object(_checkout.os, "execv") as execv:
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=True
            ):
                _checkout.run_in_checkout_environment(str(HOLD), "hold")
        execv.assert_not_called()

    def test_with_no_environment_here_another_bootstrapped_one_will_do(self):
        """The package still comes from beside the script, so the two halves
        of a hold cannot be different versions of the harness."""
        missing = self.root / "nothing" / "bin" / "python"
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", missing):
            with unittest.mock.patch.object(_checkout.os, "execv") as execv:
                with unittest.mock.patch.object(
                    _checkout, "in_checkout_environment", return_value=False
                ):
                    with unittest.mock.patch.object(
                        _checkout, "in_harness_environment", return_value=True
                    ):
                        _checkout.run_in_checkout_environment(str(HOLD), "hold")
        execv.assert_not_called()

    def test_an_interpreter_nobody_bootstrapped_is_turned_down(self):
        """Whatever versions of the dependencies happen to be installed is
        not an environment to drive devices from."""
        missing = self.root / "nothing" / "bin" / "python"
        with unittest.mock.patch.object(_checkout, "VENV_PYTHON", missing):
            with unittest.mock.patch.object(
                _checkout, "in_checkout_environment", return_value=False
            ):
                with unittest.mock.patch.object(
                    _checkout, "in_harness_environment", return_value=False
                ):
                    with self.assertRaises(SystemExit) as caught:
                        _checkout.run_in_checkout_environment(str(HOLD), "hold")
        self.assertIn(str(REPO), str(caught.exception))
        self.assertIn("bootstrap.py", str(caught.exception))

    def test_the_environment_marker_is_the_one_bootstrap_writes(self):
        """Named in two places because this module runs before the harness
        is importable; they must not drift."""
        from ubertooth_harness import environment

        self.assertEqual(environment.MARKER, _checkout.VENV_MARKER)

    def test_this_checkouts_environment_is_recognized_as_one(self):
        self.assertTrue(_checkout.in_harness_environment())
        self.assertTrue(_checkout.in_checkout_environment())

    def test_the_advice_names_the_checkout_to_bootstrap(self):
        """A bench runs helpers by path, so "run bootstrap.py" is not enough."""
        advice = _checkout.bootstrap_advice("hold")
        self.assertIn(str(REPO), advice)
        self.assertIn("bootstrap.py", advice)
        self.assertTrue(advice.startswith("hold: "))

    def test_the_advice_carries_the_import_error_it_came_from(self):
        """An import that is broken rather than missing must not read as
        a checkout nobody bootstrapped."""
        advice = _checkout.bootstrap_advice("hold", ImportError("no module named yaml"))
        self.assertIn("no module named yaml", advice)

    def test_a_refused_registry_names_the_harness_that_refused_it(self):
        """#312's error read as a mistake in a file that was perfectly good.

        What it was missing is which harness turned it down, since an older
        one turns down a registry a newer one wrote.
        """
        self.registry.write_text(
            REGISTRY.replace("Ubertooth: UT0", "Ubertooth: UT_Absent"),
            encoding="utf-8",
        )
        from ubertooth_harness.resources import RegistryError

        with self.assertRaises(RegistryError) as caught:
            _bench.load_registry(self.registry)
        self.assertIn(str(_checkout.CHECKOUT), str(caught.exception))
        self.assertIn("read by the harness in", str(caught.exception))

    def test_a_registry_that_loads_is_returned_unchanged(self):
        registry = _bench.load_registry(self.registry)
        self.assertEqual("UT0", registry.get("Host_Linux").defaults["Ubertooth"])

    def test_a_name_error_is_not_blamed_on_the_harness(self):
        """A device nobody registered is the caller's mistake, not a version."""
        from ubertooth_harness.resources import RegistryError

        registry = self.load_registry()
        with self.assertRaises(RegistryError) as caught:
            hold.resolve(registry, "UT_Absent")
        self.assertNotIn("read by the harness in", str(caught.exception))


class LabFiles(unittest.TestCase):
    def test_both_scripts_are_executable_and_compile(self):
        for path in (HOLD, HUB_PORT, PEER):  # _bench.py is imported, not run
            with self.subTest(script=path.name):
                self.assertTrue(os.access(path, os.X_OK), f"{path.name} is not executable")
                # compile() rather than py_compile, which would write bytecode
                # into the checkout.
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_the_documented_command_works_from_any_directory(self):
        """How the README says to run them: the interpreter and the path, no more.

        No PYTHONPATH and a working directory that is not the checkout, which
        is what a user has.
        """
        for path in (HOLD, HUB_PORT, PEER):
            with self.subTest(script=path.name):
                environment = dict(os.environ)
                environment.pop("PYTHONPATH", None)
                environment.pop("UBERTOOTH_HARNESS_HARDWARE", None)
                with tempfile.TemporaryDirectory() as elsewhere:
                    result = subprocess.run(
                        [sys.executable, str(path), "--help"],
                        capture_output=True, text=True, env=environment,
                        cwd=elsewhere, timeout=60,
                    )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("usage:", result.stdout)
                self.assertNotIn("harness environment", result.stderr)

    def test_it_holds_a_device_run_as_a_user_runs_it(self):
        """End to end as a shell invokes it: no PYTHONPATH, another directory.

        The in-process tests cannot catch this, because the delegate is a
        child process with an environment of its own.
        """
        with tempfile.TemporaryDirectory() as elsewhere:
            root = Path(elsewhere)
            registry = root / "hardware.yaml"
            registry.write_text(REGISTRY, encoding="utf-8")
            leases_dir = root / "leases"
            leases_dir.mkdir()
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment.pop("UBERTOOTH_HARNESS_HARDWARE", None)
            result = subprocess.run(
                [
                    sys.executable, str(HOLD),
                    "--hardware", str(registry),
                    "--lease-dir", str(leases_dir),
                    "--wait-for-boards", "0",
                    "UT0", "--", sys.executable, "-c", "print('held')",
                ],
                capture_output=True, text=True, env=environment,
                cwd=elsewhere, timeout=120,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("held", result.stdout)

    def test_the_delegate_can_import_the_harness(self):
        environment = hold.child_environment({"PATH": "/usr/bin"})
        self.assertIn(str(REPO), environment["PYTHONPATH"])
        kept = hold.child_environment({"PYTHONPATH": "/somewhere"})
        self.assertEqual(f"{REPO}{os.pathsep}/somewhere", kept["PYTHONPATH"])

    def test_a_usage_error_exits_like_argparse_does(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(HOLD), "UT0", "no-separator"],
            capture_output=True, text=True, env=environment, timeout=60,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("usage:", result.stderr)

    def test_the_udev_rule_names_the_acroname_vendor_and_a_group(self):
        text = RULES.read_text(encoding="utf-8")
        rule = [line for line in text.splitlines() if not line.startswith("#")]
        self.assertEqual(1, len([line for line in rule if line.strip()]))
        self.assertIn('ATTR{idVendor}=="24ff"', text)
        self.assertRegex(text, r'GROUP="[a-z]+"')
        self.assertRegex(text, r'MODE="0[0-7]{3}"')

    def test_the_udev_rule_matches_only_the_hub_not_what_is_behind_it(self):
        """ATTRS would match the attribute on any ancestor, so every device
        plugged into the hub would get the group's read and write."""
        rule = "\n".join(
            line for line in RULES.read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        )
        self.assertNotIn("ATTRS{", rule)

    def test_every_helper_is_a_program_a_bench_can_exec(self):
        """A wrapper on a bench names a path, nothing else (#312).

        No interpreter, no working directory, no environment: anything a
        wrapper has to supply is something it can supply wrongly, and one
        wrapper naming another checkout's interpreter is what took every
        device command on a host down.
        """
        for path in (HOLD, HUB_PORT, PEER):
            with self.subTest(script=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("#!/usr/bin/env python3\n"))
                self.assertTrue(os.access(path, os.X_OK))
                self.assertIn("run_in_checkout_environment", text)

    def test_no_script_names_a_host_serial_or_home_path(self):
        """These are public: a bench's own identities stay in its registry."""
        banned = re.compile(
            r"/home/"                      # a lab account's home directory
            r"|[0-9]{1,3}(\.[0-9]{1,3}){3}"  # an address
            r"|\b[0-9a-f]{32}\b"             # a device serial
            r"|\b0x[0-9a-f]{8}\b",           # a hub's BrainStem serial
            re.IGNORECASE,
        )
        for path in (HOLD, HUB_PORT, PEER, RULES, LAB / "_bench.py",
                     LAB / "_checkout.py", LAB / "README.md"):
            with self.subTest(file=path.name):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    self.assertIsNone(
                        banned.search(line), f"{path.name}:{number}: {line.strip()}"
                    )


if __name__ == "__main__":
    unittest.main()
