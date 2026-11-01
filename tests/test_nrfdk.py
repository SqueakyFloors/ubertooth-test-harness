import contextlib
import io
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from tests.fake_dk import FakeClock, FakeFirmware, FakePort, nrfutil_runner
from ubertooth_harness import nrfdk

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "nrf52840dk"


def hex_file(path: Path, payload: bytes) -> Path:
    """An Intel HEX file with ``payload`` as data records."""
    lines = []
    for offset in range(0, len(payload), 16):
        chunk = payload[offset:offset + 16]
        record = bytes([len(chunk), offset >> 8 & 0xFF, offset & 0xFF, 0]) + chunk
        lines.append(":" + (record + bytes([-sum(record) & 0xFF])).hex().upper())
    lines.append(":00000001FF")
    path.write_text("\n".join(lines) + "\n")
    return path


class Board:
    """A DK with fake firmware, its shell opener, and a fake nrfutil."""

    def __init__(self, firmware=None, flashed_version=nrfdk.FIRMWARE_VERSION, **runner):
        self.firmware = firmware or FakeFirmware()
        self.calls = []
        self.ports = []

        def on_program(_argv):
            self.firmware.version = flashed_version
            self.firmware.silent = False
            self.firmware.defaults()

        self.dk = nrfdk.Dk(
            1050000001, "/dev/fake-vcom0",
            nrfutil_runner(calls=self.calls, on_program=on_program, **runner),
            opener=self.open,
        )

    def open(self, _path):
        port = FakePort(self.firmware)
        self.ports.append(port)
        return nrfdk.Shell(port, clock=FakeClock())


class ShellTests(unittest.TestCase):
    def test_command_collects_data_lines_until_ok(self):
        firmware = FakeFirmware()
        shell = nrfdk.Shell(FakePort(firmware), clock=FakeClock())

        status = shell.status()

        self.assertEqual(nrfdk.FIRMWARE_VERSION, status["fw"])
        self.assertEqual(["status"], firmware.commands)

    def test_noise_before_the_command_is_dropped(self):
        firmware = FakeFirmware()
        port = FakePort(firmware, noise=b"*** Booting nRF Connect SDK ***\r\nOK\r\n")
        shell = nrfdk.Shell(port, clock=FakeClock())

        self.assertEqual(firmware.address, shell.status()["address"])

    def test_err_reply_raises_with_the_reason(self):
        firmware = FakeFirmware()
        firmware.refuse["adv chmap 40"] = "channel map must be 37, 38, 39 or all"
        shell = nrfdk.Shell(FakePort(firmware), clock=FakeClock())

        with self.assertRaisesRegex(nrfdk.NrfDkError, "channel map must be"):
            shell.command("adv chmap 40")

    def test_zephyr_shell_errors_raise_without_waiting(self):
        for reply in (b"bogus: command not found\r\n", b"chmap: wrong parameter count\r\n"):
            class Port(FakePort):
                def write(self, data, reply=reply):
                    self.pending += reply
                    return len(data)

            shell = nrfdk.Shell(Port(FakeFirmware()), clock=FakeClock())
            with self.assertRaisesRegex(nrfdk.NrfDkError, "refused by the shell"):
                shell.command("bogus")

    def test_no_answer_times_out(self):
        firmware = FakeFirmware()
        firmware.silent = True
        shell = nrfdk.Shell(FakePort(firmware), clock=FakeClock())

        with self.assertRaisesRegex(nrfdk.NrfDkError, "no answer to 'status' within 3s"):
            shell.status()

    def test_ansi_escapes_and_carriage_returns_are_stripped(self):
        class Port(FakePort):
            def write(self, data):
                self.pending += b"\x1b[1;32m\x1b[mOK\r\r\n"
                return len(data)

        shell = nrfdk.Shell(Port(FakeFirmware()), clock=FakeClock())
        self.assertEqual([], shell.command("adv start"))

    def test_with_an_alias_every_line_of_the_conversation_is_written(self):
        logged = []
        shell = nrfdk.Shell(FakePort(FakeFirmware()), clock=FakeClock(),
                            log=lambda direction, text: logged.append((direction, text)))

        shell.command("adv start")

        self.assertEqual([(">", "adv start"), ("<", "OK")], logged)

    def test_the_logged_lines_carry_the_alias_and_the_direction(self):
        stream = io.StringIO()
        shell = nrfdk.Shell(FakePort(FakeFirmware()), clock=FakeClock(),
                            log=nrfdk.shell_logger("DK_Linux1", stream))

        status = shell.status()

        lines = stream.getvalue().splitlines()
        self.assertEqual("[DK_Linux1 >] status", lines[0])
        self.assertTrue(lines[1].startswith('[DK_Linux1 <] {'))
        self.assertIn(status["fw"], lines[1])
        self.assertEqual("[DK_Linux1 <] OK", lines[-1])

    def test_without_an_alias_nothing_is_written(self):
        shell = nrfdk.Shell(FakePort(FakeFirmware()), clock=FakeClock())

        self.assertEqual([], shell.command("adv start"))

    def test_a_refused_command_is_written_before_it_raises(self):
        firmware = FakeFirmware()
        firmware.refuse["adv chmap 40"] = "channel map must be 37, 38, 39 or all"
        stream = io.StringIO()
        shell = nrfdk.Shell(FakePort(firmware), clock=FakeClock(),
                            log=nrfdk.shell_logger("DK_Linux1", stream))

        with self.assertRaises(nrfdk.NrfDkError):
            shell.command("adv chmap 40")

        self.assertEqual(
            ["[DK_Linux1 >] adv chmap 40",
             "[DK_Linux1 <] ERR channel map must be 37, 38, 39 or all"],
            stream.getvalue().splitlines(),
        )

    def test_status_must_have_every_field(self):
        with self.assertRaisesRegex(nrfdk.NrfDkError, "no valid 'counter'"):
            nrfdk.parse_status('{"role":"advertiser","fw":"x","bt":true,"address":"A",'
                               '"adv":false,"running":false,"type":"nonconn",'
                               '"chmap":"all","interval_ms":100,"payload":""}')
        with self.assertRaisesRegex(nrfdk.NrfDkError, "not JSON"):
            nrfdk.parse_status("{nope")


class ToolTests(unittest.TestCase):
    def test_pinned_nrfutil_versions_pass(self):
        nrfdk.check_nrfutil(nrfutil_runner())

    def test_other_nrfutil_device_version_is_refused(self):
        with self.assertRaisesRegex(nrfdk.NrfDkError, "nrfutil-device is 2.0.0, the harness "
                                    "is validated with 2.23.0"):
            nrfdk.check_nrfutil(nrfutil_runner(device="2.0.0"))

    def test_missing_nrfutil_fails_closed(self):
        def run(argv, timeout):
            raise nrfdk.NrfDkError("nrfutil not found on PATH")

        with self.assertRaisesRegex(nrfdk.NrfDkError, "not found"):
            nrfdk.check_nrfutil(run)

    def test_find_probe_matches_the_padded_serial(self):
        device = nrfdk.find_probe(1050000001, nrfutil_runner())
        self.assertEqual("/dev/fake-001050000001-0", nrfdk.probe_vcom0(device))
        with self.assertRaisesRegex(nrfdk.NrfDkError, "no J-Link probe with serial 42"):
            nrfdk.find_probe(42, nrfutil_runner())

    def test_linux_port_is_the_by_id_path(self):
        self.assertEqual("/dev/serial/by-id/usb-SEGGER_J-Link_001050000001-if00",
                         nrfdk.default_port(1050000001, "linux"))
        self.assertIsNone(nrfdk.default_port(1050000001, "darwin"))

    def test_jlink_serial_and_address_checks(self):
        self.assertEqual(1050000001, nrfdk.jlink_serial("001050000001"))
        for bad in ("", "12ab", "123"):
            with self.assertRaises(nrfdk.NrfDkError):
                nrfdk.jlink_serial(bad)
        self.assertEqual("C3:11:22:33:44:55", nrfdk.normalize_address("c3:11:22:33:44:55"))
        self.assertTrue(nrfdk.is_static_random("C3:11:22:33:44:55"))
        self.assertFalse(nrfdk.is_static_random("43:11:22:33:44:55"))
        with self.assertRaises(nrfdk.NrfDkError):
            nrfdk.normalize_address("C3-11-22-33-44-55")

    def test_image_must_contain_the_firmware_version(self):
        with tempfile.TemporaryDirectory() as directory:
            good = hex_file(Path(directory) / "good.hex",
                            b"\x00" * 20 + nrfdk.FIRMWARE_VERSION.encode() + b"\x00")
            nrfdk.check_image(good)
            old = hex_file(Path(directory) / "old.hex", b"utdk-0.0.1\x00" * 3)
            with self.assertRaisesRegex(nrfdk.NrfDkError, "does not contain firmware version"):
                nrfdk.check_image(old)
            broken = Path(directory) / "broken.hex"
            broken.write_text(":10000000FF\n")
            with self.assertRaisesRegex(nrfdk.NrfDkError, "bad Intel HEX record"):
                nrfdk.check_image(broken)

    def test_check_image_is_a_command_of_its_own_and_opens_no_board(self):
        """`nrfdk.py check-image` is what scripts/ci.sh firmware-dk runs on a
        fresh build, so it must need no J-Link and no serial port."""
        def no_tools(argv, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError(f"check-image ran {argv}")

        with tempfile.TemporaryDirectory() as directory:
            good = hex_file(Path(directory) / "zephyr.hex",
                            nrfdk.FIRMWARE_VERSION.encode())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = nrfdk.main(["check-image", str(good)], run=no_tools)
            self.assertEqual(0, code, err.getvalue())
            self.assertIn(f"firmware version: {nrfdk.FIRMWARE_VERSION}", out.getvalue())
            self.assertIn(f"image: {good}", out.getvalue())

            old = hex_file(Path(directory) / "old.hex", b"utdk-0.0.1")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err):
                code = nrfdk.main(["check-image", str(old)], run=no_tools)
            self.assertEqual(1, code)
            self.assertIn("does not contain firmware version", err.getvalue())

    def test_firmware_and_driver_versions_agree(self):
        header = (FIRMWARE / "src" / "roles.h").read_text()
        match = re.search(r'#define UTDK_FW_VERSION "([^"]+)"', header)
        self.assertEqual(nrfdk.FIRMWARE_VERSION, match[1])
        for name, value in (("UTDK_TAG_MAX", nrfdk.TAG_MAX),
                            ("UTDK_FORMAT", nrfdk.PAYLOAD_FORMAT),
                            ("UTDK_COMPANY_ID", nrfdk.COMPANY_ID)):
            found = re.search(rf"#define {name} (\S+)", header)[1]
            self.assertEqual(value, int(found, 0), name)
        self.assertIn(f'#define UTDK_MAGIC "{nrfdk.MAGIC.decode()}"', header)

    def test_firmware_readme_names_the_pinned_versions(self):
        readme = (FIRMWARE / "README.md").read_text()
        version = (FIRMWARE / "NCS_VERSION").read_text().strip()
        self.assertIn(f"nRF Connect SDK {version}", readme)
        self.assertIn(f"nrfutil {nrfdk.NRFUTIL_VERSION}", readme)
        self.assertIn(f"`nrfutil device` {nrfdk.NRFUTIL_DEVICE_VERSION}", readme)


class OperationTests(unittest.TestCase):
    def run_quiet(self, function, *args, **kwargs):
        out = io.StringIO()
        result = function(*args, out=out, **kwargs)
        return result, out.getvalue()

    def test_ensure_with_the_right_firmware_does_not_flash(self):
        board = Board()
        board.firmware.adv = True

        status, out = self.run_quiet(nrfdk.ensure, board.dk, None, "c3:11:22:33:44:55")

        self.assertFalse(status["adv"])
        self.assertIn(f"firmware: {nrfdk.FIRMWARE_VERSION}\n", out)
        self.assertIn("advertising: off type nonconn chmap all", out)
        self.assertNotIn("flashed", out)
        self.assertFalse(any(call[:3] == ["nrfutil", "device", "program"]
                             for call in board.calls))

    def test_ensure_flashes_other_firmware_then_verifies(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"))
        with tempfile.TemporaryDirectory() as directory:
            image = hex_file(Path(directory) / "utdk.hex", nrfdk.FIRMWARE_VERSION.encode())

            status, out = self.run_quiet(nrfdk.ensure, board.dk, image, None)

        self.assertEqual(nrfdk.FIRMWARE_VERSION, status["fw"])
        self.assertIn("flashed: utdk.hex (it runs utdk-0.9.0 before)", out)
        program = [call for call in board.calls if call[:3] == ["nrfutil", "device", "program"]]
        self.assertEqual(1, len(program))
        self.assertEqual(["--serial-number", "1050000001", "--firmware", str(image)],
                         program[0][3:7])
        self.assertIn("verify=VERIFY_READ", program[0][-1])

    def test_ensure_flashes_a_board_that_does_not_answer(self):
        firmware = FakeFirmware()
        firmware.silent = True
        board = Board(firmware)
        with tempfile.TemporaryDirectory() as directory:
            image = hex_file(Path(directory) / "utdk.hex", nrfdk.FIRMWARE_VERSION.encode())
            _status, out = self.run_quiet(nrfdk.ensure, board.dk, image, None)
        self.assertIn("flashed: utdk.hex (the DK did not answer status", out)

    def test_ensure_without_an_image_fails_closed(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"))
        with self.assertRaisesRegex(
                nrfdk.NrfDkError,
                rf"does not run {re.escape(nrfdk.FIRMWARE_VERSION)} \(it runs "
                r"utdk-0\.9\.0\); pass --artifact"):
            self.run_quiet(nrfdk.ensure, board.dk, None, None)

    def test_ensure_refuses_an_image_without_the_version(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"))
        with tempfile.TemporaryDirectory() as directory:
            image = hex_file(Path(directory) / "old.hex", b"utdk-0.9.0")
            with self.assertRaisesRegex(nrfdk.NrfDkError, "does not contain"):
                self.run_quiet(nrfdk.ensure, board.dk, image, None)
        self.assertEqual([], board.calls)

    def test_ensure_fails_when_the_flash_does_not_take(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"), flashed_version="utdk-0.9.0")
        with tempfile.TemporaryDirectory() as directory:
            image = hex_file(Path(directory) / "utdk.hex", nrfdk.FIRMWARE_VERSION.encode())
            with self.assertRaisesRegex(nrfdk.NrfDkError, "after flashing, J-Link 1050000001 "
                                        "reports utdk-0.9.0"):
                self.run_quiet(nrfdk.ensure, board.dk, image, None)

    def test_ensure_fails_on_a_failed_program(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"), program_returncode=1)
        with tempfile.TemporaryDirectory() as directory:
            image = hex_file(Path(directory) / "utdk.hex", nrfdk.FIRMWARE_VERSION.encode())
            with self.assertRaisesRegex(nrfdk.NrfDkError, "program exited 1: Error: failed"):
                self.run_quiet(nrfdk.ensure, board.dk, image, None)

    def test_ensure_checks_the_registered_address(self):
        board = Board()
        with self.assertRaisesRegex(nrfdk.NrfDkError, "advertises from C3:11:22:33:44:55, the "
                                    "registry says C3:00:00:00:00:01"):
            self.run_quiet(nrfdk.ensure, board.dk, None, "C3:00:00:00:00:01")

    def test_advertise_sets_everything_then_checks_status(self):
        board = Board()

        status, out = self.run_quiet(nrfdk.advertise, board.dk, "38", 250, "C0FFEE")

        self.assertEqual(["adv stop", "adv type nonconn", "adv chmap 38", "adv interval 250",
                          "adv payload c0ffee", "adv start", "status"],
                         board.firmware.commands)
        self.assertTrue(status["adv"])
        self.assertIn("advertising: on type nonconn chmap 38 interval 250 ms payload c0ffee",
                      out)
        self.assertIn("counter: 9\n", out)
        self.assertTrue(all(port.closed for port in board.ports))

    def test_advertise_fails_when_status_disagrees(self):
        board = Board()
        original = board.firmware.handle

        def ignore_chmap(line):
            return ["OK"] if line.startswith("adv chmap") else original(line)

        board.firmware.handle = ignore_chmap
        with self.assertRaisesRegex(nrfdk.NrfDkError, "chmap 'all' \\(asked '39'\\)"):
            self.run_quiet(nrfdk.advertise, board.dk, "39", 100, "none")

    def test_advertise_refuses_bad_settings_before_any_command(self):
        board = Board()
        for args, reason in (((("40", 100, "aa")), "channel map"),
                             ((("37", 19, "aa")), "interval must be 20-10240"),
                             ((("37", 100, "abc")), "payload must be")):
            with self.assertRaisesRegex(nrfdk.NrfDkError, reason):
                self.run_quiet(nrfdk.advertise, board.dk, *args)
        self.assertEqual([], board.firmware.commands)

    def test_stop_and_idle(self):
        board = Board()
        self.run_quiet(nrfdk.advertise, board.dk, "37", 100, "aa")
        status, out = self.run_quiet(nrfdk.stop, board.dk)
        self.assertFalse(status["adv"])
        self.assertEqual("37", status["chmap"])
        status, _out = self.run_quiet(nrfdk.stop, board.dk, defaults=True)
        self.assertEqual("all", status["chmap"])
        self.assertEqual("adv defaults", board.firmware.commands[-2])


class CommandLineTests(unittest.TestCase):
    """main() against a fake firmware on a pseudo-terminal, as on a VCOM port."""

    def setUp(self):
        try:
            import serial  # noqa: F401
        except ImportError:
            self.skipTest("pyserial is not installed")
        self.firmware = FakeFirmware()
        # The two ends of one pty: the fake firmware answers on firmware_fd,
        # and device_fd is the end whose path nrfdk.py opens.
        self.firmware_fd, device_fd = os.openpty()
        self.path = os.ttyname(device_fd)
        self.device_fd = device_fd
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(timeout=2)
        os.close(self.firmware_fd)
        os.close(self.device_fd)

    def serve(self):
        import select
        line = b""
        while not self.stop.is_set():
            ready, _, _ = select.select([self.firmware_fd], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.firmware_fd, 256)
            except OSError:
                return
            line += data
            while b"\r" in line:
                raw, line = line.split(b"\r", 1)
                for answer in self.firmware.handle(raw.decode().strip("\n")):
                    os.write(self.firmware_fd, answer.encode() + b"\r\n")

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nrfdk.main(list(argv), run=nrfutil_runner(serials=("001050000001",)))
        return code, out.getvalue(), err.getvalue()

    def test_advertise_then_stop(self):
        code, out, err = self.main("advertise", "--jlink", "1050000001", "--port", self.path,
                                   "--chmap", "39", "--interval", "100", "--payload", "c0ffee")
        self.assertEqual((0, ""), (code, err))
        self.assertRegex(out, r"(?m)^advertising: on type nonconn chmap 39 ")
        self.assertRegex(out, r"(?m)^counter: 9$")
        code, out, err = self.main("stop", "--jlink", "1050000001", "--port", self.path)
        self.assertEqual(0, code, err)
        self.assertRegex(out, r"(?m)^counter: 11$")

    def test_with_an_alias_the_whole_conversation_goes_to_stderr(self):
        code, out, err = self.main("stop", "--jlink", "1050000001", "--port", self.path,
                                   "--alias", "DK_Linux1")

        self.assertEqual(0, code, err)
        self.assertIn("[DK_Linux1 >] adv stop\n", err)
        self.assertIn("[DK_Linux1 <] OK\n", err)
        self.assertIn("[DK_Linux1 >] status\n", err)
        # The results the cases read still go to stdout.
        self.assertRegex(out, r"(?m)^advertising: off ")

    def test_identify_names_the_probe(self):
        code, out, err = self.main("identify", "--jlink", "001050000001", "--port", self.path)
        self.assertEqual(0, code, err)
        self.assertIn("J-Link serial: 1050000001\nboard: PCA10056\n", out)

    def test_errors_are_one_line_and_exit_1(self):
        code, out, err = self.main("identify", "--jlink", "1050000002", "--port", self.path)
        self.assertEqual(1, code)
        self.assertEqual("error: no J-Link probe with serial 1050000002 is attached "
                         "(nrfutil device list)\n", err)
        code, _out, err = self.main("status", "--jlink", "1050000001",
                                    "--port", "/dev/no-such-port")
        self.assertEqual((1, "error: /dev/no-such-port does not exist: is J-Link 1050000001 "
                          "attached?\n"), (code, err))


class RoleTests(unittest.TestCase):
    """Switching roles, and the central and peripheral operations."""

    def run_quiet(self, function, *args, **kwargs):
        out = io.StringIO()
        result = function(*args, out=out, **kwargs)
        return result, out.getvalue()

    def test_role_switches_and_reports_the_new_role(self):
        board = Board()
        board.firmware.adv = True

        status, out = self.run_quiet(nrfdk.role, board.dk, "central")

        self.assertEqual("central", status["role"])
        self.assertIn("role: central\n", out)
        # Switching leaves the previous role idle.
        self.assertFalse(status["adv"])

    def test_an_unknown_role_is_refused_before_the_board_is_opened(self):
        board = Board()
        with self.assertRaisesRegex(nrfdk.NrfDkError, "role must be advertiser"):
            nrfdk.role(board.dk, "sniffer")
        self.assertEqual([], board.firmware.commands)

    def test_idle_releases_a_board_in_any_role(self):
        board = Board()
        self.run_quiet(nrfdk.peripheral, board.dk)
        self.assertTrue(board.firmware.conn_on)

        status, out = self.run_quiet(nrfdk.idle, board.dk)

        self.assertEqual("advertiser", status["role"])
        self.assertFalse(status["conn_on"] or status["connected"] or status["adv"])
        self.assertIn("role: advertiser\n", out)
        self.assertIn("role advertiser", board.firmware.commands)

    def test_peripheral_advertises_connectable(self):
        board = Board()

        status, out = self.run_quiet(nrfdk.peripheral, board.dk)

        self.assertEqual(("peripheral", True), (status["role"], status["conn_on"]))
        self.assertEqual(["role peripheral", "conn chmap 37", "conn dle on",
                          "conn start"],
                         [c for c in board.firmware.commands if c != "status"])
        self.assertIn("connection: down peer none", out)

    def test_central_connects_and_reports_the_negotiated_link(self):
        board = Board()

        status, out = self.run_quiet(
            nrfdk.central, board.dk, "c1:92:ec:e7:71:bc", 30, True, 244, 100, 4000, 60)

        self.assertEqual(("central", True), (status["role"], status["connected"]))
        self.assertEqual("C1:92:EC:E7:71:BC", status["peer"])
        self.assertEqual(48, status["interval_units"])  # the update's 60 ms
        self.assertEqual(nrfdk.DATA_LENGTH_MAX, status["tx_octets"])
        self.assertIn("conn peer C1:92:EC:E7:71:BC", board.firmware.commands)
        self.assertIn("conn cycle 4000", board.firmware.commands)
        self.assertIn("conn autoupdate 60", board.firmware.commands)
        self.assertIn(f"data length: tx {nrfdk.DATA_LENGTH_MAX} rx "
                      f"{nrfdk.DATA_LENGTH_MAX} octets mtu 247 phy 1M\n", out)

    def test_central_waits_for_the_data_length_to_settle(self):
        board = Board()
        board.firmware.slow_data_length = 3

        status, _out = self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)

        self.assertEqual(nrfdk.DATA_LENGTH_MAX, status["tx_octets"])

    def test_central_fails_with_what_the_connection_is_not(self):
        board = Board()
        board.firmware.slow_data_length = 10**6
        # Long enough for the shell to answer, short enough to give up at once.
        nrfdk.CONNECT_TIMEOUT_S, saved = 0.5, nrfdk.CONNECT_TIMEOUT_S
        try:
            with self.assertRaisesRegex(nrfdk.NrfDkError, r"tx 27 octets, asked 251"):
                self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)
        finally:
            nrfdk.CONNECT_TIMEOUT_S = saved

    def test_an_out_of_range_interval_is_refused_before_the_board_is_opened(self):
        board = Board()
        for interval in (0, 4001):
            with self.assertRaisesRegex(nrfdk.NrfDkError, "connection interval must be"):
                nrfdk.central(board.dk, "C1:92:EC:E7:71:BC", interval)
        self.assertEqual([], board.firmware.commands)

    def test_update_waits_for_the_new_interval(self):
        board = Board()
        self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)

        status, out = self.run_quiet(nrfdk.update, board.dk, 100)

        self.assertEqual(80, status["interval_units"])
        self.assertIn("conn update 100", board.firmware.commands)
        self.assertIn("interval 80 units (100 ms)", out)
        self.assertIn("update: asked 100 ms (80 units)\n", out)

    def test_datamap_narrows_the_live_connection(self):
        board = Board()
        self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)

        status, out = self.run_quiet(nrfdk.datamap, board.dk, "0,12,36")

        self.assertEqual("conn datamap 0,12,36", board.firmware.commands[-2])
        # Channels 0, 12 and 36: bit 0 of byte 0, bit 4 of byte 1, bit 4 of
        # byte 4, in the order a CONNECT_IND carries the map.
        self.assertEqual("0110000010", status["data_map"])
        self.assertEqual(3, status["data_map_used"])
        self.assertIn("channel map: 0110000010 (3 of 37 channels, set 1 time(s))",
                      out)

    def test_datamap_takes_one_channel_and_all(self):
        board = Board()
        self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)

        # Channel 17 is bit 1 of byte 2.
        status, _out = self.run_quiet(nrfdk.datamap, board.dk, "17")
        self.assertEqual((1, "0000020000"),
                         (status["data_map_used"], status["data_map"]))

        status, _out = self.run_quiet(nrfdk.datamap, board.dk, "all")
        self.assertEqual((37, nrfdk.DATA_MAP_ALL),
                         (status["data_map_used"], status["data_map"]))

    def test_a_map_the_air_has_no_room_for_is_refused_before_the_board(self):
        board = Board()
        for channels in ("", "37", "0,", "0,,1", "all,1", "-1", "0x3", "00"):
            with self.subTest(channels):
                with self.assertRaises(nrfdk.NrfDkError):
                    nrfdk.datamap(board.dk, channels)
        self.assertEqual([], board.firmware.commands)

    def test_a_map_change_without_a_connection_is_refused_by_the_board(self):
        board = Board()
        self.run_quiet(nrfdk.role, board.dk, "central")

        with self.assertRaisesRegex(nrfdk.NrfDkError, "no connection to update"):
            self.run_quiet(nrfdk.datamap, board.dk, "0,1,2")

    def test_only_the_central_changes_the_map(self):
        board = Board()
        self.run_quiet(nrfdk.peripheral, board.dk)

        with self.assertRaisesRegex(nrfdk.NrfDkError, "only the central"):
            self.run_quiet(nrfdk.datamap, board.dk, "0,1,2")

    def test_firmware_that_does_not_report_the_map_is_named(self):
        board = Board()
        self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)
        status = board.firmware.status
        board.firmware.status = lambda: {
            key: value for key, value in status().items()
            if key not in ("data_map", "data_map_used", "data_map_sets")
        }

        with self.assertRaisesRegex(nrfdk.NrfDkError,
                                    "does not report the connection's channel map"):
            self.run_quiet(nrfdk.datamap, board.dk, "0,1,2")

    def test_leaving_a_role_forgets_the_peer(self):
        board = Board()
        self.run_quiet(nrfdk.central, board.dk, "C1:92:EC:E7:71:BC", 30)

        status, _out = self.run_quiet(nrfdk.idle, board.dk)

        # A later `conn start` must not open a link to the last test's peer.
        self.assertEqual("", status["peer"])

    def test_firmware_without_the_connection_fields_is_named(self):
        board = Board(FakeFirmware(version="utdk-0.9.0"))
        # Old firmware answers only the advertiser's fields.
        board.firmware.status = lambda: {
            "role": "advertiser", "fw": "utdk-0.9.0", "bt": True,
            "address": board.firmware.address, "address_type": "random static",
            "adv": False, "running": False, "type": "nonconn", "chmap": "all",
            "interval_ms": 100, "payload": "", "counter": 0,
        }
        with self.assertRaisesRegex(nrfdk.NrfDkError,
                                    "no connection fields.*utdk-0.9.0"):
            self.run_quiet(nrfdk.idle, board.dk)


if __name__ == "__main__":
    unittest.main()
