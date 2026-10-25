import io
import random
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import capturefile, receive_cases, rxdump, rxreplay
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

REPLAY = "Raw receive replay"
RECORD = rxreplay.RECORD


# --- an independent reference decoder (libbtbb's decode steps) ---------------

def _reverse8(value):
    return int(f"{value:08b}"[::-1], 2)


def ref_uap_from_hec(data, hec):
    """libbtbb uap_from_hec(): run the HEC register backwards."""
    for i in range(9, -1, -1):
        if hec & 0x80:
            hec ^= 0x65
        hec = ((hec << 1) | (((hec >> 7) ^ (data >> i)) & 1)) & 0xFF
    return _reverse8(hec)


def ref_crcgen(bits, uap):
    """libbtbb crcgen()."""
    reg = (_reverse8(uap) << 8) & 0xFF00
    for bit in bits:
        reg = (reg >> 1) | (((reg & 1) ^ (bit & 1)) << 15)
        reg ^= (reg & 0x8000) >> 5
        reg ^= (reg & 0x8000) >> 12
    return reg


def unpack(record):
    data = record[4 + 14:]
    return [(byte >> (7 - j)) & 1 for byte in data for j in range(8)]


def bits_to_int(bits):
    return sum(bit << i for i, bit in enumerate(bits))


def decode_record(record, laps, max_errors=2):
    """(LAP, errors, channel, packet) for one record as cb_rx sees it, or None."""
    pkt_type, status, channel = record[4], record[5], record[6]
    if pkt_type != 0 or status & 0x20 or channel > 78:
        return None
    symbols = unpack(record) + [0] * 64
    for offset in range(400):
        word = bits_to_int(symbols[offset:offset + 64])
        for lap in laps:
            errors = bin(word ^ rxreplay.sync_word(lap)).count("1")
            if errors <= max_errors:
                clk100ns = struct.unpack("<I", record[8:12])[0]
                clock = (clk100ns + offset * 10 - 4000) // 3125
                return lap, errors, channel, _packet(symbols[offset:], clock)
    return None


def _packet(symbols, clock):
    """Header fields and DH1 CRC check, or None for an ID packet."""
    coded = symbols[68:122]
    if sum(coded[i] != coded[i + 1] or coded[i] != coded[i + 2]
           for i in range(0, 54, 3)) >= 5:
        return None
    white = rxreplay.whitening(clock, 18 + 56)
    header = [coded[3 * i] ^ white[i] for i in range(18)]
    data, hec = bits_to_int(header[:10]), bits_to_int(header[10:])
    uap = ref_uap_from_hec(data, hec)
    packet = {"uap": uap, "type": (data >> 3) & 0xF, "crc": False}
    if packet["type"] == rxreplay.DH1:
        payload = [b ^ w for b, w in zip(symbols[122:122 + 56], white[18:])]
        length = bits_to_int(payload[3:8]) + 3
        body = payload[:(length - 2) * 8]
        packet["crc"] = ref_crcgen(body, uap) == bits_to_int(payload[(length - 2) * 8:length * 8])
    return packet


# --- a fake ubertooth-rx -i following ubertooth-rx.c and cb_rx ------------------

class ReplayRx:
    """ubertooth-rx -i on the fork: by default fork main 6fee30c; ``fixed=False``
    models 3b22c95, with #202 (replay time printed), #203 (a read error or a
    cut last record exits 0 silently) and #236 (-u without -l checked only
    after a device is opened, and ignored with -i). ``bugs``
    switches on a misbehavior per case: decoys, no-uap, wrong-uap, ignores-l,
    ignores-e, link-type, no-capture, dump-mangled, survey-no-uap,
    partial-record, empty-fails, missing-exit-0, u-fails, wrong-u-confirms.
    """

    def __init__(self, *, fixed=True, bugs=()):
        self.fixed = fixed
        self.bugs = set(bugs)
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-H"):
            return self._result(command, 0, "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n")
        if command[0] == sys.executable and command[1].endswith(".py"):
            module = {"rxreplay.py": rxreplay, "capturefile.py": capturefile,
                      "rxdump.py": rxdump}[Path(command[1]).name]
            out = io.StringIO()
            with redirect_stdout(out):
                code = module.main(list(command[2:]))
            return self._result(command, code, out.getvalue())
        if command[0] == sys.executable:
            subprocess.run(command, check=True)
            return self._result(command, 0, "")
        assert command[0] == "ubertooth-rx", command
        assert "-U" not in command
        return self._rx(command)

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    @staticmethod
    def _value(options, flag):
        return options[options.index(flag) + 1] if flag in options else None

    def _rx(self, command):
        options = list(command[1:])
        if "-u" in options and "-l" not in options:
            if self.fixed:
                return self._result(command, 1, "", "error: -u <UAP> requires -l <LAP>\n")
            if "-i" not in options:
                return self._result(command, 1, "", "ubertooth_connect: no device found\n")
        path = Path(self._value(options, "-i"))
        if not path.exists():
            code = 0 if "missing-exit-0" in self.bugs else 1
            return self._result(command, code, f"Could not open file {path}\nusage\n")
        if path.is_dir():
            return self._result(command, 1 if self.fixed else 0, "",
                                f"error: reading {path} failed: Is a directory\n"
                                if self.fixed else "")
        data = path.read_bytes()
        if not data and "empty-fails" in self.bugs:
            return self._result(command, 1, "")
        lap = self._value(options, "-l")
        uap = self._value(options, "-u")
        max_errors = 2 if "ignores-e" in self.bugs else int(self._value(options, "-e") or 2)
        laps = [rxreplay.LAP_A, rxreplay.LAP_B, rxreplay.LAP_DECOY]
        if lap is not None and "ignores-l" not in self.bugs:
            laps = [int(lap, 16)]
        records = [data[i:i + RECORD] for i in range(0, len(data), RECORD)]
        partial = ""
        if records and len(records[-1]) < RECORD:
            if "partial-record" not in self.bugs:
                if self.fixed:
                    partial = (f"error: {path} ends in a partial record: "
                               f"{len(records[-1])} of {RECORD} bytes, ignored\n")
                records.pop()
            else:
                records[-1] = records[-1] + bytes(RECORD - len(records[-1]))
        lines, found, dumped, uap_seen = [], [], [], {}
        for index, record in enumerate(records):
            if "decoys" in self.bugs:
                record = record[:4] + bytes([0, 0, record[6] % 79]) + record[7:]
            result = decode_record(record, laps, max_errors)
            if result is None:
                continue
            got_lap, errors, channel, packet = result
            systime = struct.unpack(">I", record[:4])[0] if self.fixed else int(time.time()) + 7
            lines.append(f"systime={systime} ch={channel:2d} LAP={got_lap:06x} err={errors} "
                         "clkn=1 clk_offset=1000 s=-55 n=-120 snr=65")
            found.append(record)
            dumped.append(record)
            if packet and packet["crc"] and got_lap not in uap_seen:
                uap_seen[got_lap] = packet["uap"]
                if "-z" in options or (lap is not None and uap is None):
                    shown = packet["uap"] ^ (1 if "wrong-uap" in self.bugs else 0)
                    if "no-uap" not in self.bugs:
                        lines.append(f"Correct CRC! UAP = 0x{shown:x} found after "
                                     f"{len(found)} total packets.")
                        lines.append("CLK6 = 0x0 found after 1 total packets.")
                elif uap is not None:
                    if int(uap, 16) == packet["uap"] or "wrong-u-confirms" in self.bugs:
                        if "u-fails" in self.bugs:
                            lines.append("failed to confirm UAP")
                        else:
                            lines.append("CLK6 = 0x0 found after 2 total packets.")
                            lines.append("26523 initial CLK1-27 candidates")
        if "-z" in options:
            lines.append("Survey Results")
            for survey_lap in dict.fromkeys(int(line.split("LAP=")[1][:6], 16)
                                            for line in lines if "LAP=" in line):
                text = f"{survey_lap:06X}"
                text = ":".join(text[i:i + 2] for i in (0, 2, 4))
                known = uap_seen.get(survey_lap)
                if known is not None and "survey-no-uap" not in self.bugs:
                    lines.append(f"??:??:{known:02X}:{text}")
                else:
                    lines.append(f"??:??:??:{text}")
        self._write_outputs(options, found, dumped)
        return self._result(command, 1 if partial else 0,
                            "".join(line + "\n" for line in lines), partial)

    def _write_outputs(self, options, found, dumped):
        link = 1 if "link-type" in self.bugs else 255
        count = 0 if "no-capture" in self.bugs else len(found)
        if "-q" in options:
            data = struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 400, link)
            for _ in range(count):
                data += struct.pack("<IIII", 1, 2, 6, 6) + bytes(6)
            Path(self._value(options, "-q")).write_bytes(data)
        if "-r" in options:
            shb = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1)
            shb += struct.pack("<I", 28)
            idb = struct.pack("<IIHHI", 1, 20, link, 0, 0) + struct.pack("<I", 20)
            epb = struct.pack("<IIIIIII", 6, 32, 0, 0, 0, 0, 0) + struct.pack("<I", 32)
            Path(self._value(options, "-r")).write_bytes(shb + idb + epb * count)
        if "-d" in options:
            data = b"".join(dumped)
            if "dump-mangled" in self.bugs and data:
                data = data[:20] + bytes([data[20] ^ 1]) + data[21:]
            Path(self._value(options, "-d")).write_bytes(data)


def replay_cases(coverage):
    definition = builtin_catalog().get(REPLAY)
    arguments = definition.normalize_arguments({"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run_replay(executor, coverage="comprehensive"):
    arguments, _cases = replay_cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{REPLAY}", REPLAY, "Host_Mac", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events


# --- tests ----------------------------------------------------------------------

class EncodingTests(unittest.TestCase):

    def test_sync_words_match_libbtbb(self):
        # btbb_gen_syncword(): LAP 0 is libbtbb's DEFAULT_CODEWORD; the GIAC.
        self.assertEqual(0xB0000002C7820E7E, rxreplay.sync_word(0))
        self.assertEqual(0x4E7A2CCE331A3AE2, rxreplay.sync_word(0x9E8B33))
        for lap in (rxreplay.LAP_A, rxreplay.LAP_B, rxreplay.LAP_DECOY):
            # The LAP is bits 34-57, the Barker code bits 58-63.
            self.assertEqual(lap, (rxreplay.sync_word(lap) >> 34) & 0xFFFFFF)

    def test_sync_words_are_far_apart(self):
        laps = (rxreplay.LAP_A, rxreplay.LAP_B, rxreplay.LAP_DECOY)
        for a in laps:
            for b in laps:
                if a != b:
                    distance = bin(rxreplay.sync_word(a) ^ rxreplay.sync_word(b)).count("1")
                    self.assertGreater(distance, 8)

    def test_hec_is_what_libbtbb_reverses(self):
        rng = random.Random(1)
        for _ in range(500):
            data, uap = rng.getrandbits(10), rng.getrandbits(8)
            self.assertEqual(uap, ref_uap_from_hec(data, rxreplay.hec(data, uap)))

    def test_whitening_matches_libbtbb_tables(self):
        # libbtbb WHITENING_DATA from INDICES[CLK1-6], first 24 bits.
        vectors = {0: "100100110100111101110000", 1: "100100010000001001001101",
                   0x2A: "110011000001101101011101", 0x3F: "111000111011000101001011"}
        for clk6, bits in vectors.items():
            got = "".join(map(str, rxreplay.whitening(clk6 << 1, 24)))
            self.assertEqual(bits, got, clk6)
        sequence = rxreplay.whitening(0, 254)
        self.assertEqual(sequence[:127], sequence[127:])  # period 127
        self.assertEqual(rxreplay.whitening(0x2468, 24), rxreplay.whitening(0x2468 | 1, 24))

    def test_crc_is_libbtbbs(self):
        rng = random.Random(2)
        for _ in range(200):
            bits = [rng.getrandbits(1) for _ in range(rng.randint(8, 240))]
            uap = rng.getrandbits(8)
            self.assertEqual(ref_crcgen(bits, uap), rxreplay.crc16(bits, uap))


class FixtureTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def make(self, variant="full", base=1_700_000_000):
        path = self.dir / f"{variant}.bin"
        result = subprocess.run(
            [sys.executable, rxreplay.__file__, "fixture", str(path), "--variant", variant,
             "--base-time", str(base)],
            capture_output=True, text=True, cwd=self.dir, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return path, result.stdout

    def test_generator_cli_writes_the_variants(self):
        path, out = self.make()
        self.assertEqual(len(rxreplay.RECORDS) * RECORD, path.stat().st_size)
        self.assertIn("full, 15 whole records, 1020 bytes, first systime=1700000000", out)
        self.assertEqual(rxreplay.fixture_bytes(1_700_000_000), path.read_bytes())
        path, out = self.make("truncated")
        self.assertEqual(1010, path.stat().st_size)
        self.assertIn("14 whole records", out)
        path, _out = self.make("empty")
        self.assertEqual(0, path.stat().st_size)

    def test_default_base_time_is_an_hour_ago(self):
        path = self.dir / "now.bin"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, rxreplay.main(["fixture", str(path)]))
        first = struct.unpack(">I", path.read_bytes()[:4])[0]
        self.assertAlmostEqual(time.time() - rxreplay.FIXTURE_AGE_S, first, delta=60)

    def test_reference_decoder_reads_the_expected_packets(self):
        path, _out = self.make()
        data = path.read_bytes()
        laps = [rxreplay.LAP_A, rxreplay.LAP_B, rxreplay.LAP_DECOY]
        decoded, crc_ok = [], []
        for index in range(len(data) // RECORD):
            record = data[index * RECORD:(index + 1) * RECORD]
            self.assertEqual(1_700_000_000 + index, struct.unpack(">I", record[:4])[0])
            result = decode_record(record, laps)
            if result is None:
                continue
            lap, errors, channel, packet = result
            decoded.append((lap, errors, channel))
            if lap == rxreplay.LAP_A:
                self.assertEqual(rxreplay.UAP_A, packet["uap"])
                self.assertIn(packet["type"], (rxreplay.POLL, rxreplay.DH1))
                crc_ok.append(packet["crc"])
            else:
                self.assertIsNone(packet)  # LAP B sends ID packets only
        self.assertEqual(rxreplay.expected_packets(), decoded)
        self.assertEqual(1, crc_ok.count(True))  # the one DH1
        self.assertEqual(rxreplay.packet_record_indices(),
                         [i for i, r in enumerate(rxreplay.RECORDS) if not r.decoy])

    def test_decoys_hold_a_valid_access_code_cb_rx_must_skip(self):
        records = rxreplay.RECORDS
        data = rxreplay.fixture_bytes(1)
        decoys = [i for i, r in enumerate(records) if r.decoy and r.lap is not None]
        self.assertEqual(3, len(decoys))
        for index in decoys:
            record = bytearray(data[index * RECORD:(index + 1) * RECORD])
            self.assertIsNone(decode_record(bytes(record), [rxreplay.LAP_DECOY]))
            record[4:7] = bytes([0, 0, 12])  # as a plain BR packet it decodes
            self.assertEqual(rxreplay.LAP_DECOY,
                             decode_record(bytes(record), [rxreplay.LAP_DECOY])[0])

    def test_expected_packets_per_variant_and_filter(self):
        full = rxreplay.expected_packets()
        self.assertEqual(11, len(full))
        self.assertEqual(10, len(rxreplay.expected_packets("truncated")))
        self.assertEqual([], rxreplay.expected_packets("empty"))
        self.assertEqual(8, len(rxreplay.expected_packets(lap=rxreplay.LAP_A)))
        self.assertEqual([(rxreplay.LAP_B, 0, 9)] * 2 + [(rxreplay.LAP_B, 1, 9)],
                         rxreplay.expected_packets(lap=rxreplay.LAP_B))
        self.assertEqual(10, len(rxreplay.expected_packets(max_errors=0)))

    def test_dump_check(self):
        path, _out = self.make()
        source = path.read_bytes()
        good = b"".join(source[i * RECORD:(i + 1) * RECORD]
                        for i in rxreplay.packet_record_indices())
        dump = self.dir / "d"
        dump.write_bytes(good)
        self.assertEqual("", rxreplay.dump_check(dump, path))
        dump.write_bytes(good + b"x")
        self.assertIn("trailing bytes", rxreplay.dump_check(dump, path))
        dump.write_bytes(good[:-RECORD])
        self.assertIn("10 records, expected 11", rxreplay.dump_check(dump, path))
        dump.write_bytes(good[:100] + b"\xff" + good[101:])
        self.assertIn("record 1 differs", rxreplay.dump_check(dump, path))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(1, rxreplay.main(["dump-check", str(self.dir / "no"), str(path)]))
        self.assertIn("does not exist", out.getvalue())

    def test_files_lists_names_and_sizes(self):
        (self.dir / "a.bin").write_bytes(b"123")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, rxreplay.main(["files", str(self.dir)]))
        self.assertIn("a.bin: 3\n", out.getvalue())
        self.assertIn("Replay files: 1 in ", out.getvalue())


class CheckTests(unittest.TestCase):

    LINE = "systime={t} ch={ch:2d} LAP={lap:06x} err={err} clkn=1 clk_offset=2 s=-1 n=-2 snr=1"

    def lines(self, packets, t=5):
        return "".join(self.LINE.format(t=t, lap=lap, err=err, ch=ch) + "\n"
                       for lap, err, ch in packets)

    def test_packets_check(self):
        want = rxreplay.expected_packets()
        check = rxreplay.packets_check(want)
        self.assertEqual("", check(self.lines(want) + "noise\n"))
        self.assertIn("10 packet lines, expected 11", check(self.lines(want[:-1])))
        decoy = want + [(rxreplay.LAP_DECOY, 0, 12)]
        self.assertIn("decoy", check(self.lines(decoy)))
        swapped = [want[1], want[0]] + want[2:]
        self.assertIn("packet line 1", check(self.lines(swapped)))

    def test_uap_and_survey_checks(self):
        want = rxreplay.expected_packets(lap=rxreplay.LAP_A)
        check = rxreplay.uap_check(want, 0x6B)
        good = self.lines(want) + "Correct CRC! UAP = 0x6b found after 4 total packets.\n"
        self.assertEqual("", check(good))
        self.assertIn("reported UAP none", check(self.lines(want)))
        self.assertIn("reported UAP 6b, 6c", check(
            good + "UAP = 0x6c found after 9 total packets.\n"))
        self.assertIn("reported UAP 6b", rxreplay.uap_check(want, None)(good))
        survey = rxreplay.survey_check({"??:??:6B:5A:3C:96"})
        self.assertEqual("", survey("x\nSurvey Results\n??:??:6B:5A:3C:96\n"))
        self.assertIn("no 'Survey Results'", survey(""))
        self.assertIn("survey listed", survey("Survey Results\n??:??:??:5A:3C:96\n"))

    def test_systime_check(self):
        want = rxreplay.expected_packets()
        out = self.lines(want[:2], t=7)
        self.assertEqual("", rxreplay.systime_check(out, [7, 7]))
        self.assertIn("report systime 7, the records hold 3",
                      rxreplay.systime_check(out, [3, 4]))


class CaptureCountTests(unittest.TestCase):

    def test_counts_pcap_and_pcapng_packets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rx = ReplayRx()
            fixture = root / "f.bin"
            fixture.write_bytes(rxreplay.fixture_bytes(1))
            rx._rx(("ubertooth-rx", "-i", str(fixture), "-q", str(root / "a.pcap"),
                    "-r", str(root / "a.pcapng")))
            self.assertEqual(11, capturefile.count_packets(root / "a.pcap"))
            self.assertEqual(11, capturefile.count_packets(root / "a.pcapng"))
            out = io.StringIO()
            with redirect_stdout(out):
                capturefile.main(["--packets", str(root / "a.pcap")])
            self.assertEqual("Capture format: pcap, link type: 255, packets: 11\n",
                             out.getvalue())
            cut = root / "cut.pcap"
            cut.write_bytes((root / "a.pcap").read_bytes()[:-3])
            with self.assertRaises(capturefile.CaptureFileError):
                capturefile.count_packets(cut)
            cut.write_bytes((root / "a.pcapng").read_bytes()[:-3])
            with self.assertRaises(capturefile.CaptureFileError):
                capturefile.count_packets(cut)
            empty = root / "empty.pcap"
            empty.write_bytes((root / "a.pcap").read_bytes()[:24])
            self.assertEqual(0, capturefile.count_packets(empty))


class ReplayCaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(REPLAY)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = replay_cases("smoke")
        self.assertEqual(["replay-decode-lap", "replay-lap-uap", "replay-pcapng",
                          "replay-pcap"], [c.case_id for c in smoke])
        _a, full = replay_cases("comprehensive")
        self.assertEqual(17, len(full))
        context = {"capture dir": "/run/captures/x"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                self.assertNotIn("-U", argv)  # never a board, not even by default
                self.assertFalse(step.mutates or step.continuous)
                self.assertIn(argv[0], ("ubertooth-rx", sys.executable))
                for path in (a for a in argv if a.startswith("/run")):
                    self.assertTrue(path == "/run/captures/x"
                                    or path.startswith("/run/captures/x/"), path)

    def test_fixture_times_are_an_hour_before_the_cases(self):
        cases = receive_cases.replay_cases({"coverage": "smoke"}, {}, {}, now=1_800_000_000)
        argv = cases[0].steps[0].resolve_argv({"capture dir": "/c"})
        self.assertEqual(str(1_800_000_000 - 3600), argv[argv.index("--base-time") + 1])

    def test_old_fork_passes_smoke_and_fails_only_the_fixed_cases(self):
        summary, _events = run_replay(ReplayRx(fixed=False), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, events = run_replay(ReplayRx(fixed=False))
        self.assertFalse(summary.passed)
        self.assertEqual(["-i -u without -l is refused",
                          "-i of a directory reports the read error",
                          "-i of a file whose last record is cut",
                          "-i packet lines carry the recorded systime"],
                         failed_names(events))

    def test_fork_main_passes_everything(self):
        summary, _events = run_replay(ReplayRx())
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_each_case_can_fail(self):
        expectations = {
            "decoys": {"-i decodes every LAP in the fixture", "-i -r replays into PCAPNG",
                       "-i -q replays into PCAP", "-i -e 0 drops the packet with an error",
                       "-i -d writes the packet records back",
                       "-i of a file whose last record is cut", "-i -z lists both piconets",
                       "-i packet lines carry the recorded systime"},
            "no-uap": {"-i -l <LAP> derives the UAP"},
            "wrong-uap": {"-i -l <LAP> derives the UAP"},
            "ignores-l": {"-i -l <LAP> derives the UAP", "-i -l 0f1e2d: only that LAP",
                          "-i -l -u with the right UAP",
                          "-i -l -u with a wrong UAP confirms nothing",
                          "-i -l 5a3c96 -q: only that LAP captured"},
            "ignores-e": {"-i -e 0 drops the packet with an error"},
            "link-type": {"-i -r replays into PCAPNG", "-i -q replays into PCAP",
                          "-i -l 5a3c96 -q: only that LAP captured"},
            "no-capture": {"-i -r replays into PCAPNG", "-i -q replays into PCAP",
                           "-i -l 5a3c96 -q: only that LAP captured"},
            "dump-mangled": {"-i -d writes the packet records back"},
            "survey-no-uap": {"-i -z lists both piconets"},
            "partial-record": {"-i of a file whose last record is cut"},
            "empty-fails": {"-i of an empty file"},
            "missing-exit-0": {"-i of a missing file"},
            "u-fails": {"-i -l -u with the right UAP"},
            "wrong-u-confirms": {"-i -l -u with a wrong UAP confirms nothing"},
        }
        for bug, names in expectations.items():
            with self.subTest(bug):
                _summary, events = run_replay(ReplayRx(bugs={bug}))
                self.assertEqual(sorted(names), failed_names(events))

    def test_plans_place_it_without_a_board(self):
        from ubertooth_harness.plan import load_plan
        catalog = builtin_catalog()
        root = Path(__file__).resolve().parents[1] / "testplans"
        for plan_name, suite, coverage in (("host", "host", "smoke"),
                                           ("fast", "fast", "smoke"),
                                           ("release", "release", "comprehensive")):
            plan = load_plan(root / f"{plan_name}.yaml", catalog)
            items = [item for item in plan.suites[suite] if item.test_name == REPLAY]
            self.assertEqual(1, len(items), plan_name)
            self.assertEqual(coverage, items[0].arguments["coverage"])
        host = load_plan(root / "host.yaml", catalog)
        for item in host.suites["host"]:
            self.assertEqual({}, dict(catalog.get(item.test_name).hardware_roles or {}))


if __name__ == "__main__":
    unittest.main()
