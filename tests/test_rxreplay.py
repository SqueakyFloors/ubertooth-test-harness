import io
import os
import random
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import (
    capturefile, receive_cases, rxdump, rxreplay, stdoutdest,
)
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
    survey-heading-on-stdout (the heading among the addresses on standard
    output, as it was before the fork split the two streams),
    partial-record, empty-fails, missing-exit-0, u-fails, wrong-u-confirms,
    dump-open-exit-0, dump-open-usage, dump-open-stray (leaves a file beside
    the fixture), dump-open-touches (rewrites the fixture),
    sweep-refused (-s refused, as from 5ac22a8 until 7b46ec4), no-sweep-help
    (-h does not list -s). ``fixed=False`` prints the -s deprecation notice
    of the builds before 5ac22a8.
    """

    def __init__(self, *, fixed=True, bugs=()):
        self.fixed = fixed
        self.bugs = set(bugs)
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n")
        if (command[0] == sys.executable and command[1].endswith("stdoutdest.py")
                and command[2] == "run"):
            return self._stdout_destination(command)
        if command[0] == sys.executable and command[1].endswith(".py"):
            module = {"rxreplay.py": rxreplay, "capturefile.py": capturefile,
                      "rxdump.py": rxdump, "stdoutdest.py": stdoutdest}[
                          Path(command[1]).name]
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

    # --- where a capture goes (opt_parse.c) ---------------------------------
    #
    # stdoutdest.py puts the descriptors where a case needs them and then runs
    # the tool, so the fake answers it here rather than letting it spawn one:
    # it reads the same options, decides where descriptor 1 and the extra
    # descriptors point, and hands that to _rx().

    def _stdout_destination(self, command):
        words = list(command[3:])
        stdout_name, append, fds = None, False, {}
        while words and words[0].startswith("--") and words[0] != "--":
            option = words.pop(0)
            if option == "--append":
                append = True
            elif option == "--stdout":
                stdout_name = words.pop(0)
            elif option == "--fd":
                number, _separator, target = words.pop(0).partition("=")
                fds[int(number)] = target
            else:  # pragma: no cover - the cases pass no other option
                raise AssertionError(option)
        if words and words[0] == "--":
            words.pop(0)
        if stdout_name is not None:
            mode = "ab" if append else "xb"
            with open(stdout_name, mode):
                pass
        for target in fds.values():
            if target != "stdout":
                Path(target).write_bytes(b"")
        result = self._rx(tuple(words), stdout_name=stdout_name,
                          fds={number: (stdout_name if target == "stdout" else target)
                               for number, target in fds.items()})
        notes = []
        if stdout_name is not None:
            notes.append(f"stdout -> {Path(stdout_name).name}")
        notes += [f"fd {number} -> {Path(target).name}" for number, target in fds.items()]
        summary = (f"Stdout destination: ubertooth-rx exit {result.returncode}; "
                   + ("; ".join(notes) or "the harness's own streams") + "\n")
        # A capture that took descriptor 1 has the tool's own output on it;
        # anything the tool still printed comes back on this command's stdout,
        # as it does when the tool inherits the harness's stream.
        return self._result(command, result.returncode,
                            summary + result.stdout, result.stderr)

    # The values that name descriptor 1 (opt_parse.c, stdout_paths plus "-").
    _STDOUT_VALUES = ("-", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1")

    def _is_stdout_value(self, value, fds):
        if value is None:
            return False
        if value in self._STDOUT_VALUES:
            return "stdout-alias-is-a-file" not in self.bugs or value == "-"
        if "other-fd-is-stdout" in self.bugs and value.startswith("/dev/fd/"):
            return True
        return False

    def _reaches_stdout(self, value, fds):
        """Whether a destination reaches descriptor 1, a link to it included.

        A writer writes through a symbolic link, so a link whose chain ends
        at one of the names for that descriptor reaches the stream as surely
        as the name itself (opt_destination_is_stdout).
        """
        if self._is_stdout_value(value, fds):
            return True
        if value == "-":
            return True
        path, hops = value, 0
        while os.path.islink(path) and hops < 16:
            target = os.readlink(path)
            path = target if os.path.isabs(target) else os.path.join(
                os.path.dirname(path), target)
            hops += 1
        return hops > 0 and path in self._STDOUT_VALUES[1:]

    def _destination_file(self, value, stdout_name, fds):
        """(device, inode) a writer given ``value`` would write to, or None.

        None is a destination that does not exist yet, which is the ordinary
        case for a capture; the comparisons treat a matched pair of them as
        one file by their text, as opt_parse.c does.
        """
        if value is None or value == "":
            return None
        if self._is_stdout_value(value, fds):
            value = stdout_name
            if value is None:
                return None
        elif value.startswith("/dev/fd/"):
            number = value.removeprefix("/dev/fd/")
            if number.isdigit() and int(number) in fds:
                value = fds[int(number)]
        try:
            info = os.stat(value)
        except OSError:
            return None
        return (info.st_dev, info.st_ino)

    def _same_destination(self, first, second, stdout_name, fds):
        first_file = self._destination_file(first, stdout_name, fds)
        second_file = self._destination_file(second, stdout_name, fds)
        if first_file is not None and second_file is not None:
            return first_file == second_file
        if first_file is None and second_file is None:
            # Neither exists yet, so only the text can answer. os.path
            # reduces the redundant "/." elements that same_path_text() does.
            return os.path.normpath(first) == os.path.normpath(second)
        return False

    @staticmethod
    def _keeps_nothing(value):
        return value == "/dev/null"

    def _destination_problems(self, destinations, stdout_name, fds, input_path):
        """The refusal and warning lines opt_parse.c would print, in order."""
        named = [(option, value) for option, value in destinations if value]
        if "no-destination-check" not in self.bugs:
            for index, (option, value) in enumerate(named):
                for other_option, other in named[index + 1:]:
                    both_stdout = (self._is_stdout_value(value, fds)
                                   and self._is_stdout_value(other, fds))
                    if both_stdout:
                        continue
                    if not self._same_destination(value, other, stdout_name, fds):
                        continue
                    if (self._keeps_nothing(value)
                            and "null-twice-refused" not in self.bugs):
                        continue
                    first = ("standard output" if value == "-" else value)
                    last = ("standard output" if other == "-" else other)
                    if value == other:
                        return (f"error: {option} and {other_option} cannot both "
                                f"write to {first}\n", "", 1)
                    return (f"error: {option} and {other_option} cannot both write "
                            f"to one file ({first} and {last})\n", "", 1)
        if input_path is None:
            return ("", "", 0)
        input_file = self._destination_file(str(input_path), stdout_name, fds)
        if "no-input-check" not in self.bugs:
            for option, value in named:
                if "input-check-ignores-links" in self.bugs:
                    same = os.path.normpath(value) == os.path.normpath(str(input_path))
                else:
                    same = (self._destination_file(value, stdout_name, fds)
                            == input_file)
                if not same:
                    continue
                named_as = "standard output" if value == "-" else value
                if "refusal-truncates-input" in self.bugs:
                    Path(input_path).write_bytes(b"")
                return (f"error: {option} would write over the dump -i is "
                        f"replaying ({named_as})\n", "", 1)
        if ("no-stdout-warning" not in self.bugs and stdout_name is not None
                and not any(self._is_stdout_value(value, fds)
                            for _option, value in named)
                and self._destination_file("-", stdout_name, fds) == input_file):
            return ("", "warning: the decode is going to the dump -i is replaying, "
                    "so the capture is lost\n", 0)
        return ("", "", 0)

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    @staticmethod
    def _value(options, flag):
        return options[options.index(flag) + 1] if flag in options else None

    def _rx(self, command, *, stdout_name=None, fds=None):
        fds = dict(fds or {})
        options = list(command[1:])
        if options == ["-h"]:
            sweep = ("" if "no-sweep-help" in self.bugs else
                     "\t-s sweep all channels - the default; a -c channel wins over it\n")
            return self._result(command, 0, "ubertooth-rx - passive Classic Bluetooth "
                                "discovery/decode\nConfiguration:\n" + sweep)
        notice = ""
        if "-s" in options:
            if "sweep-refused" in self.bugs:
                return self._result(command, 1, "", "error: unknown option '-s'\n")
            if not self.fixed:
                notice = "sweep mode is now the default and the -s argument is deprecated\n"
            options.remove("-s")
        if "-u" in options and "-l" not in options:
            if self.fixed:
                return self._result(command, 1, "", "error: -u <UAP> requires -l <LAP>\n")
            if "-i" not in options:
                return self._result(command, 1, "", "ubertooth_connect: no device found\n")
        path = Path(self._value(options, "-i"))
        if not path.exists():
            code = 0 if "missing-exit-0" in self.bugs else 1
            return self._result(command, code, "", f"error: {path}: No such file or directory\n")
        if path.is_dir():
            return self._result(command, 1 if self.fixed else 0, "",
                                f"error: reading {path} failed: Is a directory\n"
                                if self.fixed else "")
        # Where each capture would go, in the order ubertooth-rx.c lists them.
        destinations = [(option, self._value(options, option))
                        for option in ("-r", "-q", "-d")]
        # PcapNG seeks back over what it has written, which no stream allows,
        # so -r refuses every destination that reaches standard output while
        # the command line is read (opt_check_pcapng_destination).
        pcapng = self._value(options, "-r")
        if ("pcapng-stdout-allowed" not in self.bugs and pcapng is not None
                and self._reaches_stdout(pcapng, fds)):
            return self._result(command, 1, "",
                                "error: PCAPNG capture to stdout is not "
                                "supported; use -q -\n")
        refusal, warning, status = self._destination_problems(
            destinations, stdout_name, fds, path)
        if refusal:
            return self._result(command, status, "", refusal)
        dump = self._value(options, "-d")
        if dump is not None and not Path(dump).parent.is_dir():
            # The -d file is opened before any record is read; 5ac22a8^
            # used perror() (no "error: " prefix).
            code = 0 if "dump-open-exit-0" in self.bugs else 1
            prefix = "error: " if self.fixed else ""
            usage = ("ubertooth-rx - passive Classic Bluetooth discovery/decode\n"
                     if "dump-open-usage" in self.bugs else "")
            if "dump-open-stray" in self.bugs:
                (path.parent / "x.bin.tmp").write_bytes(b"")
            if "dump-open-touches" in self.bugs:
                path.write_bytes(path.read_bytes()[:-1])
            return self._result(command, code, "",
                                f"{prefix}{dump}: No such file or directory\n{usage}")
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
        # libbtbb's notes go to stderr since libbtbb bf878df2 (piconet_note);
        # the packet and survey lines are the tool's own, on stdout.
        lines, notes, found, dumped, uap_seen = [], [], [], [], {}
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
                        notes.append(f"Correct CRC! UAP = 0x{shown:x} found after "
                                     f"{len(found)} total packets.")
                        notes.append("CLK6 = 0x0 found after 1 total packets.")
                elif uap is not None:
                    if int(uap, 16) == packet["uap"] or "wrong-u-confirms" in self.bugs:
                        if "u-fails" in self.bugs:
                            notes.append("failed to confirm UAP")
                        else:
                            notes.append("CLK6 = 0x0 found after 2 total packets.")
                            notes.append("26523 initial CLK1-27 candidates")
        survey_heading = ""
        if "-z" in options:
            # The heading is standard error and the addresses standard output
            # (ubertooth-rx.c), so a script reading stdout gets the list
            # alone. survey-heading-on-stdout puts it back among them.
            if "survey-heading-on-stdout" in self.bugs:
                lines.append("Survey Results")
            else:
                survey_heading = "Survey Results\n"
            for survey_lap in dict.fromkeys(int(line.split("LAP=")[1][:6], 16)
                                            for line in lines if "LAP=" in line):
                text = f"{survey_lap:06X}"
                text = ":".join(text[i:i + 2] for i in (0, 2, 4))
                known = uap_seen.get(survey_lap)
                if known is not None and "survey-no-uap" not in self.bugs:
                    lines.append(f"??:??:{known:02X}:{text}")
                else:
                    lines.append(f"??:??:??:{text}")
        report = "".join(line + "\n" for line in lines)
        # A capture that was given descriptor 1 owns it: the tool's own
        # standard output goes to the null device for the rest of the run
        # (take_stdout), so the report is printed nowhere at all.
        on_stdout = any(self._is_stdout_value(value, fds)
                        for _option, value in destinations)
        spoiled = report if on_stdout and "stdout-report" in self.bugs else ""
        self._write_outputs(options, found, dumped, stdout_name, fds,
                            prefix=spoiled.encode())
        if on_stdout:
            report = ""
        elif stdout_name is not None:
            # Not a capture's stream, so the report goes where the caller
            # pointed standard output -- which is how a decode ends up in the
            # dump it is replaying.
            with open(stdout_name, "ab") as handle:
                handle.write(report.encode())
            report = ""
        return self._result(command, 1 if partial else 0, report,
                            notice + partial + survey_heading + warning
                            + "".join(note + "\n" for note in notes))

    def _destination_path(self, value, stdout_name, fds):
        """The file a writer given ``value`` opens, as this fake sees it."""
        if self._is_stdout_value(value, fds):
            return stdout_name
        if value.startswith("/dev/fd/"):
            number = value.removeprefix("/dev/fd/")
            if number.isdigit() and int(number) in fds:
                return fds[int(number)]
        return value

    def _write_outputs(self, options, found, dumped, stdout_name=None, fds=None,
                       prefix=b""):
        fds = dict(fds or {})
        link = 1 if "link-type" in self.bugs else 255
        count = 0 if "no-capture" in self.bugs else len(found)

        def write(option, data):
            target = self._destination_path(
                self._value(options, option), stdout_name, fds)
            # Nothing under /dev is written: the null device keeps nothing,
            # and a descriptor path this fake did not resolve to one of its
            # own files is a destination it declines to reach, rather than
            # the test process's own stream.
            if target is None or target.startswith(("/dev/", "/proc/")):
                return
            Path(target).write_bytes(prefix + data)

        if "-q" in options:
            data = struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 400, link)
            for _ in range(count):
                data += struct.pack("<IIII", 1, 2, 6, 6) + bytes(6)
            write("-q", data)
        if "-r" in options:
            shb = struct.pack("<IIIHHq", 0x0A0D0D0A, 28, 0x1A2B3C4D, 1, 0, -1)
            shb += struct.pack("<I", 28)
            idb = struct.pack("<IIHHI", 1, 20, link, 0, 0) + struct.pack("<I", 20)
            epb = struct.pack("<IIIIIII", 6, 32, 0, 0, 0, 0, 0) + struct.pack("<I", 32)
            write("-r", shb + idb + epb * count)
        if "-d" in options:
            data = b"".join(dumped)
            if "dump-mangled" in self.bugs and data:
                data = data[:20] + bytes([data[20] ^ 1]) + data[21:]
            write("-d", data)


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

    def test_only_fixture(self):
        path, _out = self.make()
        base = 1_700_000_000
        self.assertEqual("", rxreplay.only_fixture(self.dir, path.name, base, "full"))
        self.assertIn("changed", rxreplay.only_fixture(self.dir, path.name, base + 1, "full"))
        (self.dir / "stray").write_bytes(b"")
        self.assertIn("unexpected file(s) left behind: stray",
                      rxreplay.only_fixture(self.dir, path.name, base, "full"))
        (self.dir / "stray").unlink()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, rxreplay.main(["only-fixture", str(self.dir), path.name,
                                               "--base-time", str(base)]))
        self.assertIn(f"Replay directory: only {path.name}, unchanged", out.getvalue())
        path.unlink()
        self.assertIn("is gone", rxreplay.only_fixture(self.dir, path.name, base, "full"))
        self.assertIn("not a directory",
                      rxreplay.only_fixture(self.dir / "no", path.name, base, "full"))

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

    # The fixture's records claim no background estimate, so a current
    # ubertooth-rx prints the word instead of a figure (fork 532adc19).
    NONE_LINE = ("systime={t} ch={ch:2d} LAP={lap:06x} err={err} clkn=1 "
                 "clk_offset=2 s=-1 n=none snr=none")

    def test_packets_check_takes_an_absent_noise_figure(self):
        want = rxreplay.expected_packets()
        output = "".join(
            self.NONE_LINE.format(t=5, lap=lap, err=err, ch=ch) + "\n"
            for lap, err, ch in want
        )
        self.assertEqual("", rxreplay.packets_check(want)(output))

    def test_a_line_with_one_figure_and_one_word_is_not_a_packet(self):
        # The ratio comes from the noise, so one of each is a malformed line.
        want = rxreplay.expected_packets()
        for noise, snr in (("none", "1"), ("-2", "none")):
            line = self.NONE_LINE.replace("n=none", f"n={noise}").replace(
                "snr=none", f"snr={snr}")
            output = "".join(
                line.format(t=5, lap=lap, err=err, ch=ch) + "\n"
                for lap, err, ch in want
            )
            self.assertIn("0 packet lines", rxreplay.packets_check(want)(output))

    def test_uap_and_survey_checks(self):
        want = rxreplay.expected_packets(lap=rxreplay.LAP_A)
        check = rxreplay.uap_check(want)
        note = "Correct CRC! UAP = 0x6b found after 4 total packets.\n"
        # The note belongs on stderr, which this check does not see.
        self.assertEqual("", check(self.lines(want)))
        self.assertIn("note about UAP 6b is on stdout", check(self.lines(want) + note))
        self.assertRegex(note, rxreplay.uap_note(0x6B))
        self.assertRegex(note, rxreplay.ANY_UAP_NOTE)
        self.assertNotRegex(note, rxreplay.uap_note(0x6C))
        # The heading is on stderr, which this check does not see: every
        # line here that is not a packet line is an address the survey
        # listed, so a heading among them is a listed "address" and a
        # failure.
        survey = rxreplay.survey_check({"??:??:6B:5A:3C:96"})
        self.assertEqual("", survey(self.lines(want) + "??:??:6B:5A:3C:96\n"))
        self.assertIn("survey listed nothing", survey(""))
        self.assertIn("survey listed", survey("??:??:??:5A:3C:96\n"))
        self.assertIn("heading is on stdout",
                      survey("Survey Results\n??:??:6B:5A:3C:96\n"))

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


# The capture-destination cases that read back a PCAP, so anything that
# changes what reaches a capture file fails them along with the older
# PCAP cases (#333).
_STDOUT_ALIAS_CASES = {
    "-i -q /dev/stdout and /dev/fd/1 are that stream too",
    "-i -q /proc/self/fd/1 is that stream on Linux",
}
_STDOUT_CAPTURE_CASES = _STDOUT_ALIAS_CASES | {
    "-i -q - writes the capture to stdout",
    "-i -q /dev/fd/3 is the caller's file, not stdout",
}


class ReplayCaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(REPLAY)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = replay_cases("smoke")
        self.assertEqual(["replay-decode-lap", "replay-lap-uap", "replay-pcapng",
                          "replay-pcap", "replay-dump-unopenable",
                          "replay-sweep-explicit", "replay-stdout-dash",
                          "replay-stdout-paths", "replay-stdout-other-fd",
                          "replay-destinations-collide",
                          "replay-destination-is-input"],
                         [c.case_id for c in smoke])
        _a, full = replay_cases("comprehensive")
        self.assertEqual(30, len(full))
        # One of them is Linux's: /proc names descriptor 1 there and nothing
        # at all elsewhere, so a macOS run has 29.
        linux_only = [case.case_id for case in full
                      if case.needs.platforms == ("linux",)]
        self.assertEqual(["replay-stdout-proc-path"], linux_only)
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

    def test_old_fork_fails_only_the_fixed_cases(self):
        # Smoke follows fork main: before 5ac22a8 the -d open failure has no
        # "error: " prefix and -s printed a deprecation notice, so only those
        # smoke cases fail there.
        summary, events = run_replay(ReplayRx(fixed=False), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(["-i -d to a file it cannot open", "-i -s decodes as without -s"],
                         failed_names(events))
        summary, events = run_replay(ReplayRx(fixed=False))
        self.assertFalse(summary.passed)
        self.assertEqual(["-i -d to a file it cannot open",
                          "-i -s decodes as without -s",
                          "-i -u without -l is refused",
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
                       "-i packet lines carry the recorded systime",
                       "-i -s decodes as without -s"} | _STDOUT_CAPTURE_CASES | {
                           "-i: one file under two names is refused, "
                           "/dev/null twice is not"},
            "no-uap": {"-i -l <LAP> derives the UAP"},
            "wrong-uap": {"-i -l <LAP> derives the UAP"},
            "ignores-l": {"-i -l <LAP> derives the UAP", "-i -l 0f1e2d: only that LAP",
                          "-i -l -u with the right UAP",
                          "-i -l -u with a wrong UAP confirms nothing",
                          "-i -l 5a3c96 -q: only that LAP captured"},
            "ignores-e": {"-i -e 0 drops the packet with an error"},
            "link-type": {"-i -r replays into PCAPNG", "-i -q replays into PCAP",
                          "-i -l 5a3c96 -q: only that LAP captured"}
                         | _STDOUT_CAPTURE_CASES,
            "no-capture": {"-i -r replays into PCAPNG", "-i -q replays into PCAP",
                           "-i -l 5a3c96 -q: only that LAP captured"}
                          | _STDOUT_CAPTURE_CASES,
            "dump-mangled": {"-i -d writes the packet records back"},
            "survey-no-uap": {"-i -z lists both piconets"},
            "survey-heading-on-stdout": {"-i -z lists both piconets"},
            "partial-record": {"-i of a file whose last record is cut"},
            "empty-fails": {"-i of an empty file"},
            "missing-exit-0": {"-i of a missing file"},
            "dump-open-exit-0": {"-i -d to a file it cannot open"},
            "dump-open-usage": {"-i -d to a file it cannot open"},
            "dump-open-stray": {"-i -d to a file it cannot open"},
            "dump-open-touches": {"-i -d to a file it cannot open"},
            "sweep-refused": {"-i -s decodes as without -s"},
            "no-sweep-help": {"-i -s decodes as without -s"},
            "u-fails": {"-i -l -u with the right UAP"},
            "wrong-u-confirms": {"-i -l -u with a wrong UAP confirms nothing"},
            # Where a capture goes, and the pairs that are one file (#333).
            "stdout-report": _STDOUT_CAPTURE_CASES
                             - {"-i -q /dev/fd/3 is the caller's file, not stdout"},
            "stdout-alias-is-a-file": _STDOUT_ALIAS_CASES,
            "other-fd-is-stdout":
                {"-i -q /dev/fd/3 is the caller's file, not stdout",
                 "-i: a link, a hard link, and a descriptor beside the file it "
                 "holds"},
            "no-destination-check":
                {"-i -q and -r on one file is refused",
                 "-i: one file under two names is refused, /dev/null twice is not",
                 "-i: a link, a hard link, and a descriptor beside the file it "
                 "holds"},
            "null-twice-refused":
                {"-i: one file under two names is refused, /dev/null twice is not"},
            "pcapng-stdout-allowed":
                {"-i -r refuses every destination that reaches stdout"},
            "no-input-check":
                {"-i -d naming the dump being replayed is refused",
                 "-i: a symbolic and a hard link to the dump are refused",
                 "-i with stdout on the dump: -q refused, the decode warned about"},
            "input-check-ignores-links":
                {"-i: a symbolic and a hard link to the dump are refused",
                 "-i with stdout on the dump: -q refused, the decode warned about"},
            "no-stdout-warning":
                {"-i with stdout on the dump: -q refused, the decode warned about"},
            "refusal-truncates-input":
                {"-i -d naming the dump being replayed is refused",
                 "-i: a symbolic and a hard link to the dump are refused",
                 "-i with stdout on the dump: -q refused, the decode warned about"},
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
