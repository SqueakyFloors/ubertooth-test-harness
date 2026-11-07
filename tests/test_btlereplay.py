"""The synthetic low-energy dump and the checks that read a replay of it.

The reference reader below is written from the record and PDU layout a second
time, so it takes its figures out of the fixture's bytes rather than from the
constants that wrote them; it then renders the report the fork prints
(cb_btle() in host/libubertooth/src/ubertooth_callback.c and lell_print() in
libbtbb's lib/src/bluetooth_le_packet.c). The checks are run over that, and
over fixtures with one byte changed, so each one is shown to bite.
"""

import io
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import btle_replay_cases, btlereplay
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.stateful import CAPTURE_DIR_KEY

NAME = "BLE replay"
BASE = 1_700_000_000
CONTEXT = {CAPTURE_DIR_KEY: "/run/captures/btle"}

ADVERTISING_TYPES = {0: "ADV_IND", 5: "CONNECT_REQ"}
LLID = {
    0: "Reserved",
    1: "LL Data PDU / empty or L2CAP continuation",
    2: "LL Data PDU / L2CAP start",
    3: "LL Control PDU",
}
OPCODES = {2: "LL_TERMINATE_IND", 12: "LL_VERSION_IND"}
SCA = {5: "31 ppm to 50 ppm"}
# What the fork's determine_signal_and_noise() reports for the fixed readings
# every fixture record carries.
RSSI = -91


def records(data):
    """Every whole record in a dump, as (host time, header fields, bytes)."""
    out = []
    for start in range(0, len(data) - btlereplay.RECORD + 1, btlereplay.RECORD):
        record = data[start:start + btlereplay.RECORD]
        host_time = struct.unpack(">I", record[:4])[0]
        pkt_type, _status, channel, _reserved, clk100ns = struct.unpack(
            "<BBBBI", record[4:12])
        out.append((host_time, pkt_type, channel, clk100ns, record[18:]))
    return out


def _address(payload, offset, random):
    text = ":".join(f"{byte:02x}" for byte in payload[offset:offset + 6][::-1])
    return f"{text} ({'random' if random else 'public'})"


def _advertising_data(payload):
    """lell_print()'s dump of the advertising data fields it can read."""
    lines = []
    position = 0
    while position < len(payload):
        length = payload[position]
        if length == 0 or position + 1 + length > len(payload):
            break
        field = payload[position + 1:position + 1 + length]
        lines.append(f"        Type {field[0]:02x} " + {
            0x01: "(Flags)", 0xFF: "(Manufacturer Specific Data)",
        }.get(field[0], "(other)"))
        if field[0] == 0x01:
            lines.append(f"           {field[1]:08b}")
        elif field[0] == 0xFF and len(field) >= 3:
            identifier = field[1] | (field[2] << 8)
            lines.append("           Company: " + COMPANIES.get(
                identifier, "not assigned"))
            lines.append("           Data:"
                         + "".join(f" {byte:02x}" for byte in field[3:]))
        position += 1 + length
    return lines


COMPANIES = dict((btlereplay.RECENT_COMPANY, btlereplay.ORIGINAL_COMPANY))


def render(data):
    """The report a replay of ``data`` prints, read out of the bytes."""
    lines = []
    previous = None
    for host_time, pkt_type, channel, clk100ns, body in records(data):
        if pkt_type == btlereplay.LE_PROMISC:
            state, value = body[0], body[1:]
            u32 = struct.unpack("<I", value[:4])[0]
            u16 = struct.unpack("<H", value[:2])[0]
            lines += ["--------------------", "LE Promisc - " + {
                0: f"Access Address: {u32:08x}",
                1: f"CRC Init: {u32 & 0xFFFFFF:06x}",
                2: f"Hop interval: {u16 * 1.25:g} ms",
                3: f"Hop increment: {value[0]}",
                4: f"Algorithm 2 event counter: {u16}",
            }[state], ""]
            continue
        access_address = struct.unpack("<I", body[:4])[0]
        length = body[5]
        payload = body[6:6 + length]
        delta = 0.0 if previous is None else (clk100ns - previous) / 10000.0
        previous = clk100ns
        lines.append(f"systime={host_time} freq={2402 + channel} "
                     f"addr={access_address:08x} delta_t={delta:.03f} ms "
                     f"rssi={RSSI}")
        lines.append("".join(f"{byte:02x} " for byte in body[4:6 + length + 3]))
        if channel + 2402 == 2402:        # an advertising channel index
            adv_type = body[4] & 0xF
            valid = ("valid" if access_address == btlereplay.ADVERTISING_AA
                     else "invalid")
            lines += [
                f"Advertising / AA {access_address:08x} ({valid})/ "
                f"{length:2d} bytes",
                f"    Channel Index: {btlereplay.ADVERTISING_INDEX}",
                f"    Type:  {ADVERTISING_TYPES[adv_type]}",
            ]
            if adv_type == 0:             # ADV_IND
                lines.append("    AdvA:  " + _address(payload, 0,
                                                      body[4] & 0x40))
                lines.append("    AdvData:"
                             + "".join(f" {b:02x}" for b in payload[6:]))
                lines += _advertising_data(payload[6:])
            else:                         # CONNECT_REQ
                hop = payload[33]
                lines += [
                    "    InitA: " + _address(payload, 0, body[4] & 0x40),
                    "    AdvA:  " + _address(payload, 6, body[4] & 0x80),
                    f"    AA:    {struct.unpack('<I', payload[12:16])[0]:08x}",
                    f"    CRCInit: {int.from_bytes(payload[16:19], 'little'):06x}",
                    f"    WinSize: {payload[19]:02x} ({payload[19]})",
                ]
                for name, offset in (("WinOffset", 20), ("Interval", 22),
                                     ("Latency", 24), ("Timeout", 26)):
                    value = struct.unpack("<H", payload[offset:offset + 2])[0]
                    lines.append(f"    {name}: {value:04x} ({value})")
                lines.append("    ChM:" + "".join(f" {b:02x}"
                                                  for b in payload[28:33]))
                lines.append(f"    Hop: {hop & 0x1F}")
                lines.append(f"    SCA: {hop >> 5}, {SCA[hop >> 5]}")
        else:
            lines += [
                f"Data / AA {access_address:08x} (valid) / {length:2d} bytes",
                f"    Channel Index: {btlereplay.DATA_INDEX}",
                f"    LLID: {body[4] & 3} / {LLID[body[4] & 3]}",
                f"    NESN: {(body[4] >> 2) & 1}  SN: {(body[4] >> 3) & 1}  MD: 0",
            ]
            if body[4] & 3 == 3:
                lines.append(f"    Opcode: {payload[0]} / {OPCODES[payload[0]]}")
        lines.append("")
    return "\n".join(lines) + "\n"


class FixtureTests(unittest.TestCase):

    def test_the_connection_dump_holds_the_records_it_promises(self):
        data = btlereplay.fixture_bytes(BASE, "connection")
        self.assertEqual(8 * btlereplay.RECORD, len(data))
        kinds = [(host_time, pkt_type, channel) for host_time, pkt_type,
                 channel, _clk, _body in records(data)]
        self.assertEqual(
            [(BASE + index, btlereplay.LE_PROMISC, btlereplay.DATA_CHANNEL)
             for index in range(5)]
            + [(BASE + 5, btlereplay.LE_PACKET, btlereplay.ADVERTISING_CHANNEL),
               (BASE + 6, btlereplay.LE_PACKET, btlereplay.DATA_CHANNEL),
               (BASE + 7, btlereplay.LE_PACKET, btlereplay.DATA_CHANNEL)],
            kinds)
        # Each record carries its own device clock, a tenth of a second on
        # from the last, so each packet line reports a delta of its own.
        clocks = [clk for _t, _p, _c, clk, _b in records(data)]
        self.assertEqual([btlereplay.CLK100NS_STEP * (index + 1)
                          for index in range(8)], clocks)

    def test_the_advertising_dump_is_one_record(self):
        data = btlereplay.fixture_bytes(BASE, "advertising")
        self.assertEqual(btlereplay.RECORD, len(data))
        self.assertEqual(btlereplay.LE_PACKET, data[4])
        self.assertEqual(btlereplay.ADVERTISING_CHANNEL, data[6])

    def test_the_connection_access_address_breaks_no_spec_rule(self):
        # libbtbb counts offenses against a data channel access address
        # (Core spec Vol 6, Part B, 2.1.2) and the -x filter drops a packet
        # over the limit, so the fixture's own address has to be a legal one.
        aa = btlereplay.CONNECTION_AA
        bits = [(aa >> index) & 1 for index in range(32)]
        runs, longest = 1, 1
        for index in range(1, 32):
            runs = runs + 1 if bits[index] == bits[index - 1] else 1
            longest = max(longest, runs)
        transitions = sum(bits[i] != bits[i + 1] for i in range(31))
        octets = [(aa >> (8 * index)) & 0xFF for index in range(4)]
        self.assertLessEqual(longest, 6)
        self.assertLessEqual(transitions, 24)
        self.assertGreaterEqual(sum(bits[i] != bits[i + 1]
                                    for i in range(26, 31)), 2)
        self.assertNotEqual(1, len(set(octets)))
        self.assertNotEqual(btlereplay.ADVERTISING_AA, aa)
        self.assertGreater(bin(aa ^ btlereplay.ADVERTISING_AA).count("1"), 1)

    def test_an_unassigned_identifier_is_out_of_the_assigned_range(self):
        # Chosen far above every identifier the SIG has assigned, and not
        # the 0xffff it keeps for internal use, so a later registration
        # cannot turn this case's answer into a name.
        identifier = btlereplay.UNASSIGNED_COMPANY[0]
        self.assertGreater(identifier, 0x2000)
        self.assertNotEqual(0xFFFF, identifier)
        self.assertEqual("not assigned", btlereplay.UNASSIGNED_COMPANY[1])

    def test_default_base_time_is_an_hour_ago(self):
        self.assertEqual(500, btlereplay.fixture_base_time(
            500 + btlereplay.FIXTURE_AGE_S))
        self.assertAlmostEqual(time.time() - btlereplay.FIXTURE_AGE_S,
                               btlereplay.fixture_base_time(), delta=60)

    def test_an_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError):
            btlereplay.fixture_bytes(BASE, "no-such-variant")
        with self.assertRaises(ValueError):
            btlereplay.expected_packets(BASE, "no-such-variant")

    def test_a_record_cannot_hold_more_than_the_record_holds(self):
        with self.assertRaises(ValueError):
            btlereplay.record(BASE, 1, btlereplay.LE_PACKET,
                              bytes(btlereplay.DATA_LEN + 1), 0)


class CheckTests(unittest.TestCase):
    """The checks over the report the reference reader renders."""

    def setUp(self):
        self.connection = btlereplay.fixture_bytes(BASE, "connection")
        self.advertising = btlereplay.fixture_bytes(BASE, "advertising")

    def test_the_connection_replay_passes_every_check(self):
        report = render(self.connection)
        check = btlereplay.all_checks(
            btlereplay.packets_check(btlereplay.expected_packets(BASE)),
            btlereplay.lines_check(btlereplay.connection_lines()))
        self.assertEqual("", check(report))

    def test_the_advertising_replay_passes_every_check(self):
        report = render(self.advertising)
        check = btlereplay.all_checks(
            btlereplay.packets_check(
                btlereplay.expected_packets(BASE, "advertising")),
            btlereplay.lines_check(btlereplay.company_lines()))
        self.assertEqual("", check(report))

    def _mutate(self, data, offset, value):
        changed = bytearray(data)
        changed[offset] = value
        return bytes(changed)

    def test_a_changed_connection_parameter_fails_the_line_check(self):
        # The CONNECT_IND's hop increment is the last payload byte of the
        # sixth record: a replay that reported another one would describe a
        # connection the capture never carried.
        payload_at = 5 * btlereplay.RECORD + 18 + 6
        hop = 0x0A | (btlereplay.SLEEP_CLOCK_ACCURACY << 5)
        report = render(self._mutate(self.connection, payload_at + 33, hop))
        problem = btlereplay.lines_check(btlereplay.connection_lines())(report)
        self.assertIn(f"Hop: {btlereplay.HOP_INCREMENT}", problem)

    def test_a_dropped_parameter_record_fails_the_line_check(self):
        # The parameter records carry no packet, so only the line check sees
        # them: a replay that printed the packets and skipped them would
        # leave a reader with no note of which connection they belong to.
        without = self.connection[btlereplay.RECORD:]
        problem = btlereplay.lines_check(btlereplay.connection_lines())(
            render(without))
        self.assertIn("Access Address", problem)

    def test_an_out_of_order_line_fails_the_line_check(self):
        lines = render(self.connection).splitlines()
        swapped = lines[:]
        first = swapped.index("LE Promisc - CRC Init: 9b4f27")
        second = swapped.index("LE Promisc - Hop increment: 7")
        swapped[first], swapped[second] = swapped[second], swapped[first]
        problem = btlereplay.lines_check(btlereplay.connection_lines())(
            "\n".join(swapped) + "\n")
        self.assertIn("out of order", problem)

    def test_the_wrong_company_name_fails_the_line_check(self):
        report = render(self.advertising).replace(
            btlereplay.RECENT_COMPANY[1], "Someone Else")
        problem = btlereplay.lines_check(btlereplay.company_lines())(report)
        self.assertIn(btlereplay.RECENT_COMPANY[1], problem)

    def test_a_named_unassigned_identifier_fails_the_line_check(self):
        # The wording for an identifier the SIG has not registered is part
        # of what the case pins: a library that invented a name for it, or
        # reworded this line, fails here.
        report = render(self.advertising).replace("Company: not assigned",
                                                 "Company: Example Ltd")
        problem = btlereplay.lines_check(btlereplay.company_lines())(report)
        self.assertIn("not assigned", problem)

    def test_the_packet_check_reads_the_recorded_times_and_addresses(self):
        want = btlereplay.expected_packets(BASE)
        check = btlereplay.packets_check(want)
        report = render(self.connection)
        self.assertEqual("", check(report))
        # A replay reporting its own clock instead of the recorded one.
        replayed = report.replace(f"systime={BASE + 5}",
                                  f"systime={BASE + 100000}")
        self.assertIn("expected systime", check(replayed))
        # One packet line fewer, which is the shape a filter would give.
        self.assertIn("2 packet lines, expected 3",
                      check(report.replace(f"systime={BASE + 7}", "systime=x")))
        # A line with no level at all is not a packet line: the records
        # carry a reading far below the radio's ceiling, so `none` there
        # would be a reading the board never took.
        self.assertIn("packet lines", check(report.replace(f"rssi={RSSI}",
                                                           "rssi=none")))

    def test_all_checks_reports_the_first_problem(self):
        check = btlereplay.all_checks(lambda _lines: "", lambda _lines: "first",
                                      lambda _lines: "second")
        self.assertEqual("first", check("anything\n"))
        self.assertEqual("", btlereplay.all_checks()("anything\n"))

    def test_the_checks_read_lines_or_a_string(self):
        report = render(self.advertising)
        check = btlereplay.packets_check(
            btlereplay.expected_packets(BASE, "advertising"))
        self.assertEqual("", check(report))
        self.assertEqual("", check(report.splitlines()))


class HelperCommandTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def run_helper(self, *arguments):
        out = io.StringIO()
        with redirect_stdout(out):
            code = btlereplay.main(list(arguments))
        return code, out.getvalue()

    def test_the_generator_writes_each_variant(self):
        for variant in btlereplay.VARIANTS:
            path = self.dir / f"{variant}.bin"
            code, out = self.run_helper("fixture", str(path), "--variant",
                                        variant, "--base-time", str(BASE))
            self.assertEqual(0, code, out)
            self.assertIn(f"BLE replay fixture: {variant}, ", out)
            self.assertEqual(btlereplay.fixture_bytes(BASE, variant),
                             path.read_bytes())

    def test_the_generator_defaults_to_an_hour_ago(self):
        path = self.dir / "now.bin"
        self.assertEqual(0, self.run_helper("fixture", str(path))[0])
        first = struct.unpack(">I", path.read_bytes()[:4])[0]
        self.assertAlmostEqual(time.time() - btlereplay.FIXTURE_AGE_S, first,
                               delta=60)

    def test_dump_check(self):
        fixture = self.dir / "connection.bin"
        fixture.write_bytes(btlereplay.fixture_bytes(BASE, "connection"))
        dump = self.dir / "again.bin"
        dump.write_bytes(fixture.read_bytes())
        code, out = self.run_helper("dump-check", str(dump), str(fixture))
        self.assertEqual(0, code, out)
        self.assertIn("8 records match the dump replayed", out)
        # A dump that dropped the parameter records: the whole point of -d
        # on a promiscuous capture.
        dump.write_bytes(fixture.read_bytes()[5 * btlereplay.RECORD:])
        self.assertIn("3 records, expected 8",
                      self.run_helper("dump-check", str(dump), str(fixture))[1])
        dump.write_bytes(fixture.read_bytes() + b"x")
        self.assertIn("1 trailing bytes",
                      self.run_helper("dump-check", str(dump), str(fixture))[1])
        changed = bytearray(fixture.read_bytes())
        changed[btlereplay.RECORD + 20] ^= 0xFF
        dump.write_bytes(bytes(changed))
        self.assertIn("record 1 differs",
                      self.run_helper("dump-check", str(dump), str(fixture))[1])
        code, out = self.run_helper("dump-check", str(self.dir / "no"),
                                    str(fixture))
        self.assertEqual(1, code)
        self.assertIn("does not exist", out)

    def test_files_lists_names_and_sizes(self):
        (self.dir / "a.bin").write_bytes(b"123")
        code, out = self.run_helper("files", str(self.dir))
        self.assertEqual(0, code)
        self.assertIn("a.bin: 3\n", out)
        self.assertIn("BLE replay files: 1 in ", out)

    def test_the_helper_runs_as_a_script_by_path(self):
        # The steps run it as "python <path> ...", from the run's own
        # directory, so it may import nothing of the package.
        path = self.dir / "connection.bin"
        result = subprocess.run(
            [sys.executable, btlereplay.__file__, "fixture", str(path),
             "--base-time", str(BASE)],
            capture_output=True, text=True, timeout=60, cwd=self.dir,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(btlereplay.fixture_bytes(BASE, "connection"),
                         path.read_bytes())


class CaseTests(unittest.TestCase):

    def cases(self, coverage):
        definition = builtin_catalog().get(NAME)
        arguments = definition.normalize_arguments(
            {"hosts": ["Host_Mac"], "coverage": coverage})
        return definition.command_cases(arguments, {}, {})

    def test_the_catalog_builds_the_cases_for_each_profile(self):
        self.assertEqual(
            ["btle-replay-connection", "btle-replay-companies"],
            [case.case_id for case in self.cases("smoke")])
        self.assertEqual(
            ["btle-replay-connection", "btle-replay-companies",
             "btle-replay-dump", "btle-replay-needs-a-mode"],
            [case.case_id for case in self.cases("comprehensive")])

    def test_the_test_binds_no_hardware(self):
        definition = builtin_catalog().get(NAME)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        # Its own two arguments, beside the three every test takes.
        self.assertEqual({"hosts", "coverage"},
                         set(definition.arguments)
                         - {"platforms", "max_watchdog_resets", "rf",
                            "weak link", "required hardware"})

    def test_every_command_replays_a_file_and_opens_no_board(self):
        helper = Path(btlereplay.__file__).resolve()
        for case in self.cases("comprehensive"):
            for step in case.steps + case.cleanup:
                argv = list(step.argv(CONTEXT) if callable(step.argv)
                            else step.argv)
                with self.subTest(case=case.case_id, step=step.step_id):
                    if argv[0] == sys.executable:
                        self.assertEqual(str(helper), argv[1])
                        continue
                    self.assertEqual("ubertooth-btle", argv[0])
                    if step.step_id == "help":
                        self.assertEqual(["ubertooth-btle", "-h"], argv)
                        continue
                    self.assertIn("-R", argv)
                    # No board is selected, and nothing names one.
                    self.assertNotIn("-U", argv)
                    # Every file the command names is in the case's own
                    # capture directory.
                    for word in argv[1:]:
                        if word.startswith("/"):
                            self.assertTrue(
                                word.startswith(CONTEXT[CAPTURE_DIR_KEY] + "/")
                                or word == str(helper), word)

    def test_each_case_lists_the_files_it_wrote(self):
        for case in self.cases("comprehensive"):
            with self.subTest(case=case.case_id):
                self.assertEqual(["list-files"],
                                 [step.step_id for step in case.cleanup])

    def test_the_first_case_requires_the_help_text_to_list_the_options(self):
        # A build too old to have -R or -d fails here, saying which option is
        # missing, rather than on a refusal the checks cannot parse.
        (first, *_rest) = self.cases("smoke")
        step = first.steps[0]
        self.assertEqual("help", step.step_id)
        self.assertEqual(("ubertooth-btle", "-h"), step.argv)
        self.assertEqual(2, len(step.output_patterns))

    def test_the_builder_takes_the_base_time_it_is_given(self):
        definition = builtin_catalog().get(NAME)
        arguments = definition.normalize_arguments(
            {"hosts": ["Host_Mac"], "coverage": "smoke"})
        cases = btle_replay_cases.btle_replay_cases(
            arguments, {}, {}, now=BASE + btlereplay.FIXTURE_AGE_S)
        argv = list(cases[0].steps[1].argv(CONTEXT))
        self.assertIn(str(BASE), argv)
