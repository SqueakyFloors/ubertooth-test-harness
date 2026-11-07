import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import refusal, refusal_cases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink, run_contract
from tests.test_tool_cases import failed_names

NAME = "Refused command lines"
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
USAGE = {
    "ubertooth-btle": "ubertooth-btle - passive Bluetooth Low Energy monitoring\n",
    "ubertooth-follow": "ubertooth-follow - active(bluez) CLK discovery and follow\n",
    "ubertooth-scan": "ubertooth-scan - active(Bluez) device scan and inquiry\n",
    "ubertooth-rx": "ubertooth-rx - passive Classic Bluetooth discovery/decode\n",
    "ubertooth-dump": "ubertooth-dump - output a continuous stream of received bits\n",
    "ubertooth-specan": "ubertooth-specan - output a continuous stream of signal strengths\n",
    "ubertooth-dfu": "ubertooth-dfu - Ubertooth firmware update tool\n",
}
FILE_OPTIONS = {
    "ubertooth-btle": "rqcd", "ubertooth-follow": "rqd", "ubertooth-rx": "rqd",
    "ubertooth-dump": "d", "ubertooth-specan": "d", "ubertooth-dfu": "su",
    "ubertooth-scan": "",
}
VALUE_OPTIONS = {
    "ubertooth-btle": "AxstUR", "ubertooth-follow": "lueUbw", "ubertooth-rx": "iluUecst",
    "ubertooth-dump": "U", "ubertooth-specan": "luU", "ubertooth-dfu": "dU",
    "ubertooth-scan": "Uteb",
}
# ubertooth-rx's own limit on the boards one capture reads (RX_MAX_BOARDS).
RX_MAX_BOARDS = 8
# ubertooth-btle's options that drive, name, or tune a board, in the order the
# fork asks about them beside -R, and the interference letters among them.
REPLAY_BOARD_OPTIONS = ("k", "t", "A", "U", "i", "I", "j", "J")
# The values that name the one stream standard output is. The lone "-" is the
# usual spelling; "./-" is a file of that name and is not here.
STDOUT_VALUES = ("-", "/dev/stdout", "/dev/fd/1", "/proc/self/fd/1")


def writes_to_stdout(value):
    return value in STDOUT_VALUES


class FakeTools:
    """The seven tools' argument handling as at fork f2e06fa (#264, #286): file
    options are named in the option loop and opened after every check, dfu
    -s writes nothing when -d is refused, and "ignoring extra capture file"
    goes to stderr; ubertooth-btle as at fork e4f1dc5 (#377): -h exits 0 and
    the note reads "warning: ignoring extra capture file"; every -h exits 0
    (fork 12211ce, #406), dfu says "error: -s requires a .bin", and an option
    given a value twice is refused in the option loop (opt_check_once()).
    ``bugs`` switches on a misbehavior: open-early (a file
    option creates its file in the loop), extra-stdout, temp-left (dfu -u
    leaves its temporary file), sign-first (fork #286, before f2e06fa: dfu -s
    writes its .dfu before the -d image check), help-first (-h prints the
    usage before the stray-word check), no-double-dash (a word after "--"
    is read as an option), repeat-allowed (an option given a value twice is
    taken, as it was before opt_check_once()), modulation-last-wins
    (ubertooth-dump takes -c with -l and uses the last of the two, as it did
    before fork d3429234).
    ubertooth-btle's replay is as at fork 04dbe53c (#1129): -R needs the mode
    its dump was recorded with, every option that drives a board is refused
    beside it, -A is refused with -p before either, a destination that is the
    dump -R is replaying is refused by file rather than by name, and one
    capture at a time may have standard output. ubertooth-dfu -u refuses a
    value naming standard output as the value is read (#333).
    Replay and read-back bug switches: replay-mode-guessed (-R is taken with
    no major mode), replay-board-options-dropped (an option that drives a
    board is accepted beside -R and dropped), dump-over-input-allowed (a
    destination that is the replayed dump is opened, emptying it),
    two-captures-on-stdout (-q or -c and -d both take the stream),
    destinations-may-collide (two destinations naming one file are both
    opened), replay-refusal-order (the board-option refusal is asked before
    the -A-with-p one), missing-input-at-the-board (a -R input that is not
    there is left to the board lookup),
    upload-stdout-allowed (dfu -u takes a name of standard output as a file).
    before fork d3429234). ubertooth-rx's board and channel lists are the
    tool's own (fork ubertooth#1063), with four more switches for them:
    board-twice-allowed (one board named twice is taken as two, so the next
    refusal on the list speaks instead), channel-count-ignored (a channel
    count that lines up with nothing is taken), version-several-boards (-V
    answers for the first board of a list) and replay-several-boards (-i is
    given a list), dump-several-boards (-d is given one), and
    list-checks-skipped (rx_split_list takes an empty entry and a list longer
    than the tool reads). Three more add to a refusal that is otherwise right,
    which a check for the message alone would accept: second-error-line,
    note-after-refusal and warning-after-refusal.
    ``missing`` tools are not installed; ``switch`` False is a library from
    before b6d8ede."""

    def __init__(self, *, bugs=(), missing=(), switch=True):
        self.bugs = set(bugs)
        self.missing = set(missing)
        self.switch = switch
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            return self._result(command, 0, "Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)\n")
        if command[0] == sys.executable:
            assert Path(command[1]).name == "refusal.py", command
            if command[2] == "probe":
                if command[3] in self.missing:
                    answer = f"no: {command[3]} is not installed in this run (not on PATH)"
                elif not self.switch:
                    answer = "no: /p/lib/libubertooth.so.1 predates UBERTOOTH_TEST_NO_DEVICES"
                else:
                    answer = "yes (/p/lib/libubertooth.so.1)"
                return self._result(command, 0, f"No-device switch: {answer}\n")
            out = io.StringIO()
            with redirect_stdout(out):
                code = refusal.main(list(command[2:]))
            return self._result(command, code, out.getvalue())
        assert command[:2] == refusal_cases.NO_DEVICES, command
        return self._tool(command, command[2], list(command[3:]))

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _tool(self, command, tool, words):
        assert tool not in self.missing, tool
        out, err = [], []
        files, operands, flags = {}, [], []
        with_value = set()
        index = 0
        while index < len(words):
            word = words[index]
            if word == "--" and "no-double-dash" not in self.bugs:
                operands.extend(words[index + 1:])
                break
            if word == "--":
                index += 1
                continue
            if not word.startswith("-") or word == "-":
                operands.append(word)
                index += 1
                continue
            option = word[1]
            if option in FILE_OPTIONS[tool] or option in VALUE_OPTIONS[tool]:
                if index + 1 >= len(words):
                    err.append(f"error: option '-{option}' requires a value\n"
                               if tool == "ubertooth-dump"
                               else f"{tool}: option requires an argument -- {option}\n")
                    return self._result(command, 1, USAGE[tool], "".join(err))
                value = words[index + 1]
                index += 2
                # opt_check_once() runs before the switch, so a repeat is
                # refused before the value is read and before -h.
                if option in with_value and "repeat-allowed" not in self.bugs:
                    return self._result(
                        command, 1, "".join(out), "".join(err)
                        + f"error: option '-{option}' given more than once\n")
                with_value.add(option)
                refusal_text = self._value(tool, option, value)
                if refusal_text:
                    return self._result(command, 1, "".join(out),
                                        "".join(err) + refusal_text)
                if option in FILE_OPTIONS[tool]:
                    # btle -q and -c share one PCAP handle, so the first of
                    # the two wins and the second file is only reported.
                    key = "q" if option in "qc" and tool == "ubertooth-btle" else option
                    if "open-early" in self.bugs and value != "-":
                        self._create(tool, option, value)
                    if key in files:
                        (out if "extra-stdout" in self.bugs else err).append(
                            f"warning: ignoring extra capture file: {value}\n")
                    else:
                        files[key] = (option, value)
                else:
                    flags.append((option, value))
                continue
            if (tool == "ubertooth-dump" and option in "cl"
                    and "modulation-last-wins" not in self.bugs):
                # One capture, one demodulator (fork d3429234): refused here
                # in the option loop, naming both options as the user wrote
                # them, so the -d file is never opened and -h never reached.
                # The same letter twice says the same thing twice, so only a
                # different modulation clashes.
                earlier = next((o for o, _v in flags if o in "cl" and o != option),
                               None)
                if earlier is not None:
                    return self._result(
                        command, 1, "".join(out), "".join(err)
                        + f"error: -{option} cannot be combined with -{earlier}; "
                        "a capture uses one modulation\n")
            flags.append((option, None))
            index += 1
        if ("h", None) in flags and "help-first" in self.bugs:
            return self._result(command, 1, "".join(out) + USAGE[tool], "".join(err))
        if operands:
            return self._result(command, 1, "".join(out), "".join(err)
                                + f"error: unexpected argument '{operands[0]}'\n")
        # ubertooth-rx reads its board list, and then the channels for it,
        # after the operand check and before -h and -V: how many channels are
        # allowed depends on -U, which may be written after -c.
        n_boards = 1
        if tool == "ubertooth-rx":
            refused, n_boards = self._rx_lists(flags)
            if refused:
                return self._result(command, 1, "".join(out),
                                    "".join(err) + refused + self._chatter())
            if n_boards > 1 and ("V", None) in flags:
                if "version-several-boards" in self.bugs:
                    # A fork without the guard answers for the first board of
                    # the list, which is not what the command line asked for.
                    return self._result(
                        command, 0, "".join(out) + "Host tools version: libubertooth"
                        " 1.1 (git-test), libbtbb 1.0\n", "".join(err))
                return self._result(
                    command, 1, "".join(out), "".join(err)
                    + "error: -V reports one board; name one with -U, or none to"
                      " report every board attached\n" + self._chatter())
        if ("h", None) in flags:
            return self._result(command, 0,
                                "".join(out) + USAGE[tool], "".join(err))
        refused = self._after_loop(tool, flags, files)
        if refused:
            return self._result(command, 1, "".join(out), "".join(err) + refused)
        # The replay and the dump each read or write one board's records, so
        # neither can be given a list; both are refused after the checks
        # above and before any file is opened.
        if tool == "ubertooth-rx" and n_boards > 1:
            if dict(flags).get("i") is not None \
                    and "replay-several-boards" not in self.bugs:
                return self._result(
                    command, 1, "".join(out), "".join(err)
                    + "error: -i names one saved capture, so it cannot be given"
                      " several boards to read\n" + self._chatter())
            if "d" in files and "dump-several-boards" not in self.bugs:
                return self._result(
                    command, 1, "".join(out), "".join(err)
                    + "error: -d writes one dump for -i to replay as one board,"
                      " so it cannot be given several boards\n" + self._chatter())
        if tool == "ubertooth-btle":
            refused = self._replay(flags, files)
            if refused:
                return self._result(command, 1, "".join(out),
                                    "".join(err) + refused)
        if tool == "ubertooth-dfu":
            return self._dfu(command, files, dict(flags), out, err)
        # Every check on the command line has passed, so the command asks
        # for a board. ubertooth-specan opens its -d file only once it has
        # one (fork 47bae592), so a file it could not have opened is never
        # created and never mentioned here.
        if tool == "ubertooth-specan":
            return self._result(command, 1, "".join(out), "".join(err) + NOTE
                                + "error: no Ubertooth devices found\n")
        for option, value in files.values():
            if value == "-":
                out.append("\x00pcap header\x00")
            elif not Path(value).parent.is_dir():
                return self._result(command, 1, "".join(out), "".join(err)
                                    + f"error: {value}: No such file or directory\n")
            else:
                self._create(tool, option, value)
        return self._result(command, 1, "".join(out),
                            "".join(err) + NOTE + "error: no Ubertooth devices found\n")

    @staticmethod
    def _create(tool, option, value):
        if tool == "ubertooth-dfu" and option == "s":
            if Path(value).exists():
                Path(value[:-4] + ".dfu").write_bytes(b"signed")
        elif tool == "ubertooth-dfu":
            Path(value + ".Ab12Cd").write_bytes(b"")
        else:
            Path(value).write_bytes(b"\x0a\x0d\x0d\x0a")

    def _replay(self, flags, files):
        """ubertooth-btle's replay checks, in the order the fork asks them.

        The destination comparisons come first because they are asked of
        every run, replay or not; then the replay's own three.
        """
        options = [option for option, _value in flags]
        pcap = files.get("q")
        dump = files.get("d")
        if pcap is not None and dump is not None:
            if (writes_to_stdout(pcap[1]) and writes_to_stdout(dump[1])
                    and "two-captures-on-stdout" not in self.bugs):
                # One stream, one capture: interleaved there, neither file
                # would be readable and the second writer said nothing.
                return (f"error: -{pcap[0]} and -d cannot both write to "
                        "standard output\n")
        named = [files[key] for key in ("r", "q", "d") if key in files]
        if "destinations-may-collide" not in self.bugs:
            for first, second in ((a, b) for index, a in enumerate(named)
                                  for b in named[index + 1:]):
                if (writes_to_stdout(first[1])
                        or not self._same_file(first[1], second[1])):
                    continue
                # Both options are named; where the two values differ, so is
                # what each was given, since one destination reached through
                # another is not obvious from either on its own.
                options = f"-{first[0]} and -{second[0]}"
                if first[1] == second[1]:
                    return (f"error: {options} cannot both write to "
                            f"{first[1]}\n")
                return (f"error: {options} cannot both write to one file "
                        f"({first[1]} and {second[1]})\n")
        source = dict(flags).get("R")
        if source is None:
            return ""
        if (not any(option in options for option in "fnp")
                and "replay-mode-guessed" not in self.bugs):
            return ("error: -R needs -f, -n or -p: a dump does not record "
                    "which mode wrote it, and the modes stamp their packets "
                    "with different clocks\n")
        def board_option():
            if "replay-board-options-dropped" in self.bugs:
                return ""
            for option in REPLAY_BOARD_OPTIONS:
                if option in options:
                    return (f"error: -{option} cannot be combined with -R; a "
                            "replay reads a saved dump rather than driving a "
                            "board\n")
            return ""

        if "replay-refusal-order" in self.bugs:
            # What the order is there for: with the board-option refusal
            # asked first, "-p -R <dump> -A 38" is reported as a replay
            # having no board to tune rather than as -A not going with -p.
            refused = board_option()
            if refused:
                return refused
        if "A" in options and "p" in options:
            # Decided before the replay's own refusals, so the message is
            # about the mode rather than about there being no board to tune.
            return ("error: -A does not go with -p: promiscuous mode listens "
                    "on the channel the board is holding\n")
        refused = board_option()
        if refused:
            return refused
        # The replay's input is opened for reading here, before the
        # destination comparison below, which takes the open input to say
        # which file it holds. So an input that is not there is reported
        # first -- and before any board, since a replay opens none.
        if (not Path(source).exists()
                and "missing-input-at-the-board" not in self.bugs):
            return f"error: {source}: No such file or directory\n"
        if "dump-over-input-allowed" in self.bugs:
            for option, value in files.values():
                if self._same_file(value, source):
                    # What the refusal prevents: the destination is opened
                    # for writing, so the dump is empty before a record of it
                    # has been read.
                    Path(value).write_bytes(b"")
            return ""
        for option, value in files.values():
            if self._same_file(value, source):
                return (f"error: -{option} would write over the dump -R is "
                        f"replaying ({value})\n")
        return ""

    @staticmethod
    def _same_file(first, second):
        """Whether two names are one file, as the fork compares them.

        Device and inode, which is what makes a hard link the same file and
        not merely the same path; a destination that is not there yet is
        compared by path instead, as the fork compares a link's target.
        """
        if writes_to_stdout(first) or writes_to_stdout(second):
            return False
        one, other = Path(first), Path(second)
        try:
            first_stat, other_stat = one.stat(), other.stat()
        except OSError:
            return one.resolve() == other.resolve()
        return ((first_stat.st_dev, first_stat.st_ino)
                == (other_stat.st_dev, other_stat.st_ino))

    def _value(self, tool, option, value):
        bad = {
            ("ubertooth-btle", "A"): ("40", "error: advertising channel index must be 37-39 (got 40)\n"),
            ("ubertooth-rx", "c"): ("79", "error: channel must be 0-78 (got 79)\n"),
            ("ubertooth-follow", "e"): ("9", "error: maximum access code errors must be 0-5 (got 9)\n"),
            ("ubertooth-specan", "l"): ("9999", "error: lower frequency must be 2402-2480 MHz (got 9999)\n"),
        }
        if (tool, option) in bad and value == bad[(tool, option)][0]:
            return bad[(tool, option)][1]
        if tool == "ubertooth-dfu" and option == "s" and not value.endswith(".bin"):
            return "error: -s requires a .bin\n"
        if (tool == "ubertooth-dfu" and option == "u" and writes_to_stdout(value)
                and "upload-stdout-allowed" not in self.bugs):
            # The read-back is renamed over the name given and a retry
            # rewinds it, neither of which a pipe can do, so the value is
            # refused as it is read: before -h and before any board.
            return "error: -u cannot write the read-back to stdout; give a file name\n"
        if tool == "ubertooth-btle" and option == "r" and value == "-":
            return "error: PCAPNG capture to stdout is not supported; use -q - or -c -\n"
        return ""

    @staticmethod
    def _split_list(text, what, bugs=frozenset()):
        """ubertooth-rx's rx_split_list(): the entries, or the one refusal.

        The empty-entry check comes before the limit, per entry, so a list
        that is both too long and ends empty reports the empty entry.
        """
        words = []
        for part in text.split(","):
            if part == "" and "list-checks-skipped" not in bugs:
                return None, f"error: the {what} list has an empty entry\n"
            if len(words) == RX_MAX_BOARDS and "list-checks-skipped" not in bugs:
                return None, (f"error: at most {RX_MAX_BOARDS} {what}s can be "
                              "given\n")
            words.append(part)
        return words, ""

    def _chatter(self):
        """What a refusal must not say after its own line.

        The real tool prints one error: line and stops. These are the three
        ways a regression could add to it and still look refused: a second
        reason, a note, and a warning about a file.
        """
        if "second-error-line" in self.bugs:
            return "error: no Ubertooth devices found\n"
        if "note-after-refusal" in self.bugs:
            return "note: give one -c channel per board\n"
        if "warning-after-refusal" in self.bugs:
            return "warning: the capture file was not written\n"
        return ""

    def _rx_lists(self, flags):
        """The board list, then the channels for it: (refusal, n_boards)."""
        values = dict(flags)
        boards = None
        if values.get("U") is not None:
            boards, refused = self._split_list(values["U"], "board", self.bugs)
            if refused:
                return refused, 1
            if "board-twice-allowed" not in self.bugs:
                # The same board twice would be opened twice, and the second
                # open would report whatever libusb says about a board
                # already claimed, which says nothing about the command line.
                for index, selector in enumerate(boards):
                    if selector in boards[:index]:
                        return f"error: board {selector} is named twice\n", 1
        n_boards = len(boards) if boards else 1
        if values.get("c") is not None:
            channels, refused = self._split_list(values["c"], "channel", self.bugs)
            if refused:
                return refused, n_boards
            if (len(channels) not in (1, n_boards)
                    and "channel-count-ignored" not in self.bugs):
                return ("error: -c names {} channel{} for {} board{}; give one "
                        "each or one for all\n".format(
                            len(channels), "" if len(channels) == 1 else "s",
                            n_boards, "" if n_boards == 1 else "s")), n_boards
        return "", n_boards

    @staticmethod
    def _after_loop(tool, flags, files):
        options = [option for option, _value in flags]
        if tool == "ubertooth-btle" and sum(o in options for o in "fnp") > 1:
            return "error: choose one of -f, -n or -p, pick one pal\n"
        if tool == "ubertooth-btle" and ("a", None) in flags and len(
                [o for o in options if o == "a"]) > 1:
            return "error: -a cannot both read and set the access address\n"
        if tool == "ubertooth-rx" and "z" in options and (
                "l" in options or "u" in options):
            return ("error: survey mode (-z) discovers addresses, so it cannot "
                    "be given -l or -u\n")
        if tool == "ubertooth-rx" and "u" in options and "l" not in options:
            return "error: -u <UAP> requires -l <LAP>\n"
        if tool == "ubertooth-specan":
            values = dict(flags)
            if int(values.get("l", 2402)) > int(values.get("u", 2480)):
                return "error: lower frequency must not exceed upper frequency\n"
        return ""

    def _dfu(self, command, files, flags, out, err):
        image = flags.get("d")
        temp = None
        # A value naming standard output never gets this far unless the
        # upload-stdout-allowed switch is on, and there is no file beside a
        # stream to put a temporary one next to.
        if "u" in files and not writes_to_stdout(files["u"][1]):
            temp = Path(files["u"][1] + ".Ab12Cd")
            temp.write_bytes(b"")
        if "s" in files:
            source = Path(files["s"][1])
            if not source.exists():
                self._drop(temp)
                return self._result(command, 1, "", f"{source}: No such file or directory\n")
            if image is None or "sign-first" in self.bugs:
                Path(str(source)[:-4] + ".dfu").write_bytes(source.read_bytes() + b"suffix")
        if image is not None:
            self._drop(temp)
            # The seeded bad.dfu is a line of text: its last bytes stand in
            # for the suffix, so the length it reports is what landed there.
            length = Path(image).read_bytes()[-5]
            return self._result(command, 1, "Checking firmware signature\n",
                                f"error: {image}: Unknown DFU suffix length: "
                                f"{length}; firmware will not be updated\n")
        self._drop(temp)
        return self._result(command, 1, "",
                            NOTE + "error: no Ubertooth devices found\n")

    def _drop(self, temp):
        if temp is not None and "temp-left" not in self.bugs:
            temp.unlink()


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments({"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run(executor, coverage="comprehensive"):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Mac", arguments, {}, {})
        manifest, checks = run_contract(root, invocation)
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root, run_id="named-run", manifest=manifest,
            preflight_checks=checks,
        )
        return summary, sink.events




class HelperTests(unittest.TestCase):

    def _prefix(self, root, library_bytes, name="libubertooth.so.1.1"):
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        tool = root / "bin" / "ubertooth-rx"
        tool.write_text("#!/bin/sh\nexit 99\n")
        tool.chmod(0o755)
        if library_bytes is not None:
            (root / "lib" / name).write_bytes(library_bytes)
        return str(root / "bin")

    def test_probe_reads_the_library_beside_the_tool(self):
        for name in ("libubertooth.so.1.1", "libubertooth.1.1.dylib"):
            with tempfile.TemporaryDirectory() as directory:
                path = self._prefix(Path(directory), b"\0getenv\0UBERTOOTH_TEST_NO_DEVICES\0",
                                    name)
                self.assertTrue(refusal.probe("ubertooth-rx", path).startswith("yes ("))

    def test_probe_rejects_an_old_library_a_missing_one_and_a_missing_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), b"\0getenv\0libusb\0")
            self.assertIn("predates UBERTOOTH_TEST_NO_DEVICES",
                          refusal.probe("ubertooth-rx", path))
            self.assertIn("is not installed", refusal.probe("ubertooth-follow", path))
        with tempfile.TemporaryDirectory() as directory:
            path = self._prefix(Path(directory), None)
            self.assertIn("no libubertooth beside", refusal.probe("ubertooth-rx", path))

    def test_link_gives_a_seeded_file_another_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                refusal.main(["seed", directory, "dump.bin"])
                refusal.main(["link", directory, "--symlink", "link.bin=dump.bin",
                              "--hardlink", "hard.bin=dump.bin"])
            self.assertTrue((root / "link.bin").is_symlink())
            # Relative, so what a tool follows is the file beside it.
            self.assertEqual("dump.bin", os.readlink(root / "link.bin"))
            self.assertEqual((root / "dump.bin").stat().st_ino,
                             (root / "hard.bin").stat().st_ino)
            symlinks = [("link.bin", "dump.bin")]
            hardlinks = [("hard.bin", "dump.bin")]
            self.assertEqual("", refusal.check(
                root, ["new.bin"], ["dump.bin"], symlinks, hardlinks))
            # A name nothing was said about is a file a refused command line
            # left behind.
            self.assertIn("link.bin", refusal.check(root, [], ["dump.bin"]))
            # A destination opened through the symbolic link empties the file
            # at its end, which the target's own contents report.
            (root / "dump.bin").write_bytes(b"")
            self.assertIn("dump.bin changed", refusal.check(
                root, [], ["dump.bin"], symlinks, hardlinks))

    def test_check_finds_a_link_replaced_by_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                refusal.main(["seed", directory, "dump.bin"])
                refusal.main(["link", directory, "--symlink", "link.bin=dump.bin",
                              "--hardlink", "hard.bin=dump.bin"])
            for name in ("link.bin", "hard.bin"):
                os.unlink(root / name)
                (root / name).write_bytes(b"")
            self.assertIn("link.bin is no longer a symbolic link", refusal.check(
                root, [], ["dump.bin"], [("link.bin", "dump.bin")], []))
            self.assertIn("hard.bin is no longer the same file", refusal.check(
                root, [], ["dump.bin"], [], [("hard.bin", "dump.bin")]))

    def test_check_finds_a_hard_link_replaced_by_a_symbolic_one(self):
        """A hard link is the file itself, so a symbolic link to the same
        target is not one: it reads as the file but is a name a writer would
        replace instead of writing through."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                refusal.main(["seed", directory, "dump.bin"])
            (root / "hard.bin").symlink_to("dump.bin")
            self.assertIn("hard.bin is a symbolic link", refusal.check(
                root, [], ["dump.bin"], [], [("hard.bin", "dump.bin")]))
            # A name that is not there at all is reported, not raised.
            os.unlink(root / "hard.bin")
            self.assertIn("cannot be read", refusal.check(
                root, [], ["dump.bin"], [], [("hard.bin", "dump.bin")]))

    def test_check_finds_created_changed_missing_and_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(io.StringIO()):
                refusal.main(["seed", directory, "kept.pcap"])
            self.assertEqual("", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap.XYZ").write_bytes(b"")
            self.assertIn("new.pcap.XYZ", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap.XYZ").unlink()
            (root / "new.pcap").write_bytes(b"")
            self.assertIn("new.pcap exists", refusal.check(root, ["new.pcap"], ["kept.pcap"]))
            (root / "new.pcap").unlink()
            (root / "kept.pcap").write_bytes(b"")
            self.assertIn("kept.pcap changed", refusal.check(root, [], ["kept.pcap"]))
            (root / "kept.pcap").unlink()
            self.assertIn("kept.pcap is gone", refusal.check(root, [], ["kept.pcap"]))


class CaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = cases("smoke")
        # 3 ways for each of 13 file options (dump has its -U), plus the
        # extra-capture-file case, three repeated-option cases, three btle
        # replay cases and the dfu -u stdout case, a stray word on scan and
        # follow, and five ubertooth-rx board-list refusals.
        self.assertEqual(13 * 3 + 1 + 3 + 4 + 2 + 5, len(smoke))
        _a, full = cases("comprehensive")
        # The comprehensive cases: 16 of them here (the checks after the
        # option loop, both orders of dump's two modulation options, the
        # four repeated -d cases, and the dfu inputs), 8 more for the btle
        # replay and the dfu read-back, 6 more stray-word cases on scan and
        # follow, and 5 more board-list refusals.
        self.assertEqual(len(smoke) + 16 + 8 + 6 + 5, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        context = {"capture dir": "/run/captures/x"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            self.assertEqual("probe", case.steps[0].step_id)
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] != sys.executable:
                    # Every tool runs with the switch, one command per step.
                    self.assertEqual(("env", "UBERTOOTH_TEST_NO_DEVICES=1"), argv[:2])
                    self.assertIn(argv[2], USAGE)
                    self.assertTrue(step.name.startswith("env UBERTOOTH_TEST_NO_DEVICES=1 "))
                for path in (a for a in argv if a.startswith("/run")):
                    self.assertTrue(path == "/run/captures/x"
                                    or path.startswith("/run/captures/x/"), path)

    def test_fork_tools_pass_everything(self):
        summary, _events = run(FakeTools(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        summary, _events = run(FakeTools())
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_dfu_signing_before_the_image_check_fails(self):
        summary, events = run(FakeTools(bugs=("sign-first",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["dfu -s <file.bin> -d <bad image> writes no .dfu"],
                         failed_names(events))

    def test_files_opened_in_the_option_loop_fail(self):
        summary, events = run(FakeTools(bugs=("open-early",)), coverage="smoke")
        self.assertFalse(summary.passed)
        failed = failed_names(events)
        # Every file option in every refusal, plus the four cases that name two
        # capture files: the first one is opened in the loop before the repeat
        # or the note. Plus the replay case whose -d names the dump, which the
        # loop then empties, and the four board-list cases that name a capture
        # file. The scan and follow stray cases, and the -V case, name none,
        # and a destination spelled "-" is not a file to create.
        self.assertEqual(13 * 3 + 4 + 1 + 4, len(failed))
        self.assertFalse([name for name in failed if "'stray' is refused" in name])

    def test_extra_capture_note_on_stdout_fails(self):
        summary, events = run(FakeTools(bugs=("extra-stdout",)), coverage="smoke")
        self.assertFalse(summary.passed)
        self.assertEqual(["btle -n -q - -c <file>: the extra-file note is on stderr"],
                         failed_names(events))

    def test_an_option_given_a_value_twice_and_taken_fails(self):
        """Before opt_check_once() the extra value was dropped with a note, or
        kept. Every case that repeats an option has to notice."""
        summary, events = run(FakeTools(bugs=("repeat-allowed",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "btle -q twice and a stray word: the repeat is refused",
            "rx -r twice and a stray word: the repeat is refused",
            "follow -q twice and a stray word: the repeat is refused",
            "dfu -s: a second -s other.txt creates and truncates nothing",
            "follow -d twice with -h", "rx -d twice with -h",
            "dump -d twice with -h", "specan -d twice with -h",
        ]), failed_names(events))

    def test_a_dump_that_takes_both_modulations_fails(self):
        """Before fork d3429234 -c with -l meant the last of the two and the
        other was dropped without a word, so the dump ran and wrote its
        file."""
        summary, events = run(FakeTools(bugs=("modulation-last-wins",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["dump -d with -c -l", "dump -d with -l -c"],
                         failed_names(events))

    def test_dfu_temporary_file_left_behind_fails(self):
        summary, events = run(FakeTools(bugs=("temp-left",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "dfu -u <kept> -d <bad image>", "dfu -u <kept> -s <missing .bin>",
            # A run stopped at the board lookup has read nothing back, so the
            # temporary file it left is a file it should not have created.
            "dfu -u <dir>/-: a file whose name is -",
        ]), failed_names(events))

    def test_a_replay_that_guesses_its_mode_fails(self):
        """-R with no -f, -n or -p: a dump does not record which mode wrote
        it, and the modes stamp their packets from different clocks, so the
        times a replay reconstructs would be wrong with nothing to say so."""
        summary, events = run(FakeTools(bugs=("replay-mode-guessed",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["btle -R <dump> with no -f, -n or -p"],
                         failed_names(events))

    def test_board_options_accepted_beside_a_replay_fail(self):
        """A replay opens no board, so an option that drives, names, or tunes
        one asks for something it cannot do: refused, not dropped in silence."""
        summary, events = run(FakeTools(bugs=("replay-board-options-dropped",)))
        self.assertFalse(summary.passed)
        self.assertEqual(
            ["btle -f -R <dump> with each option that drives a board"],
            failed_names(events))

    def test_a_destination_that_empties_the_replayed_dump_fails(self):
        """A destination is opened for writing, so one that is the dump -R is
        replaying empties it before a record of it has been read."""
        summary, events = run(FakeTools(bugs=("dump-over-input-allowed",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "btle -f -R <dump> -d <the same dump>",
            "btle -f -R <dump> -d <another name for it>",
        ]), failed_names(events))

    def test_two_captures_taking_standard_output_fail(self):
        """Standard output carries one stream: a PCAP and a dump interleaved
        there would be neither, and the second writer said nothing."""
        summary, events = run(FakeTools(bugs=("two-captures-on-stdout",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "btle -n -q - -d -: one stream, one capture",
            "btle -n -c - -d -: the letter -c was given",
        ]), failed_names(events))

    def test_two_destinations_naming_one_file_fail(self):
        """Each writer opens its own destination, which truncates what another
        put there, so a run given one name twice would keep one format and
        lose the rest."""
        summary, events = run(FakeTools(bugs=("destinations-may-collide",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["btle -f -R <dump> -q <file> -d <the same file>"],
                         failed_names(events))

    def test_the_channel_refusal_asked_after_the_replay_one_fails(self):
        """-A does not go with -p at all, so that is what the user needs to be
        told; asking the replay's own refusals first reports a board it has no
        business tuning instead."""
        summary, events = run(FakeTools(bugs=("replay-refusal-order",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["btle -p -R <dump> -A 38: -A is about -p"],
                         failed_names(events))

    def test_a_missing_replay_input_left_to_the_board_lookup_fails(self):
        """A replay opens no board, so a dump that is not there has to be
        reported as itself rather than as a board that could not be found."""
        summary, events = run(FakeTools(bugs=("missing-input-at-the-board",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["btle -f -R <a dump that is not there>"],
                         failed_names(events))

    def test_a_read_back_written_to_standard_output_fails(self):
        """dfu -u renames a temporary file over the name it was given and
        rewinds to retry a block, neither of which a pipe can do."""
        summary, events = run(FakeTools(bugs=("upload-stdout-allowed",)))
        self.assertFalse(summary.passed)
        self.assertEqual(sorted([
            "dfu -u: the lone - and the names of descriptor 1",
            "dfu -u /dev/stdout with -h: no usage text",
        ]), failed_names(events))

    def test_missing_tool_and_old_library_skip(self):
        tools = FakeTools(missing=("ubertooth-follow",))
        summary, _events = run(tools)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertFalse(any(c[2:3] == ("ubertooth-follow",) for c in tools.commands
                             if c[:2] == refusal_cases.NO_DEVICES))
        old = FakeTools(switch=False)
        summary, _events = run(old)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertFalse(any(c[:2] == refusal_cases.NO_DEVICES for c in old.commands))


class MultiBoardRefusalTests(unittest.TestCase):
    """ubertooth-rx's board and channel lists (fork ubertooth#1063).

    What several boards share -- a corroborated UAP, and the channels the
    piconet was heard on across them -- depends on which board hears what, so
    nothing here asserts it. These are the command lines the list cannot be
    given, each refused before a board is opened or a file created.
    """

    SMOKE = ("rx-boards-named-twice", "rx-boards-empty-entry", "rx-channel-count",
             "rx-dump-several-boards", "rx-version-several-boards")
    COMPREHENSIVE = ("rx-channel-empty-entry", "rx-boards-over-long",
                     "rx-channels-over-long", "rx-replay-several-boards",
                     "rx-boards-named-twice-before-channels")

    def _by_id(self, coverage):
        _arguments, built = cases(coverage)
        return {case.case_id: case for case in built}

    def test_every_case_is_rx_with_no_board_and_an_empty_stdout(self):
        smoke = self._by_id("smoke")
        self.assertLessEqual(set(self.SMOKE), set(smoke))
        self.assertFalse(set(self.COMPREHENSIVE) & set(smoke))
        full = self._by_id("comprehensive")
        self.assertLessEqual(set(self.SMOKE) | set(self.COMPREHENSIVE), set(full))
        for case_id in self.SMOKE + self.COMPREHENSIVE:
            case = full[case_id]
            steps = [s for s in case.steps if s.step_id.startswith("refuse")]
            with self.subTest(case_id):
                # No BlueZ and no board role. A case that names a capture file
                # refuses twice, once naming a new file and once a seeded one,
                # so "creates nothing" and "truncates nothing" are both said.
                self.assertEqual((), case.needs.bluez_tools)
                names = [s.step_id for s in steps]
                self.assertIn(names, (["refuse"], ["refuse-new", "refuse-kept"]))
                for refuse in steps:
                    argv = refuse.resolve_argv({"capture dir": "/x"})
                    self.assertEqual(refusal_cases.NO_DEVICES + ("ubertooth-rx",),
                                     argv[:3])
                    self.assertEqual((1,), refuse.expected_returncodes)
                    # The refusal must be one error: line on standard error,
                    # with standard output left empty and no board ever asked
                    # for.
                    self.assertIs(refuse.output_check,
                                  refusal_cases.nothing_on_stdout)
                    self.assertIn(refusal_cases.NO_BOARD_NOTE,
                                  refuse.forbidden_patterns)
                    # Exactly one error: line and no note or warning: a
                    # refusal that went on to say more would pass a check for
                    # its own message alone.
                    for pattern in refusal_cases._ONE_ERROR_ONLY:
                        self.assertIn(pattern, refuse.forbidden_patterns)
                    # One expected message, so a case cannot quietly accept two.
                    self.assertEqual(1, len(refuse.output_patterns))
                if names == ["refuse-new", "refuse-kept"]:
                    # The second command must name the seeded file, or the
                    # pair says nothing more than the first alone.
                    new, existing = (s.resolve_argv({"capture dir": "/x"})[-1]
                                     for s in steps)
                    self.assertTrue(new.endswith("/x/new." + new.rsplit(".", 1)[1]))
                    self.assertTrue(existing.startswith("/x/kept."))
                    self.assertEqual(new.rsplit(".", 1)[1],
                                     existing.rsplit(".", 1)[1])

    def test_the_selectors_are_not_registry_serials(self):
        """No board is opened, so the selectors are invented; each must still
        begin with a letter, since libubertooth reads an all-digit selector as
        a USB index rather than as a serial. That covers the over-long list's
        own nine entries too: a digit-only one there would be an index."""
        selectors = list(refusal_cases._SEL) + refusal_cases._OVER_LONG.split(",")
        self.assertEqual(3 + 9, len(selectors))
        for selector in selectors:
            with self.subTest(selector):
                self.assertRegex(selector, r"^[a-z][0-9a-z]+$")
                self.assertFalse(selector.isdigit())

    def test_a_split_list_that_takes_what_the_tool_refuses_fails(self):
        """An empty entry and a list longer than the eight boards a capture
        reads are rx_split_list's own refusals. A fork that dropped them would
        carry on with the list, so the four cases that cover them must fail:
        the message they require never comes."""
        summary, events = run(FakeTools(bugs=("list-checks-skipped",)))
        self.assertFalse(summary.passed)
        full = self._by_id("comprehensive")
        failed = failed_names(events)
        for case_id in ("rx-boards-empty-entry", "rx-channel-empty-entry",
                        "rx-boards-over-long", "rx-channels-over-long"):
            with self.subTest(case_id):
                self.assertIn(full[case_id].name, failed)

    def test_fork_tools_pass_the_board_list_refusals(self):
        for coverage in ("smoke", "comprehensive"):
            with self.subTest(coverage):
                summary, _events = run(FakeTools(), coverage=coverage)
                self.assertTrue(summary.passed, summary.failure_reason)

    def test_one_board_named_twice_and_taken_as_two_fails(self):
        """Taken as two, the command line reaches the next refusal the list
        has: the -d case would be told it cannot have several boards, and the
        channel case that its counts do not line up. Each case forbids the
        other message, so both notice."""
        summary, events = run(FakeTools(bugs=("board-twice-allowed",)))
        self.assertFalse(summary.passed)
        failed = failed_names(events)
        for case_id in ("rx-boards-named-twice",
                        "rx-boards-named-twice-before-channels"):
            case = self._by_id("comprehensive")[case_id]
            self.assertIn(case.name, failed)

    def test_a_channel_count_that_lines_up_with_nothing_fails(self):
        summary, events = run(FakeTools(bugs=("channel-count-ignored",)))
        self.assertFalse(summary.passed)
        self.assertIn(self._by_id("comprehensive")["rx-channel-count"].name,
                      failed_names(events))

    def test_a_version_answer_for_the_first_board_of_a_list_fails(self):
        """-V reports one board, so a list beside it cannot be answered as
        asked: an answer for the first board of the list is not a refusal, and
        it lands on standard output, which the case requires to stay empty."""
        summary, events = run(FakeTools(bugs=("version-several-boards",)))
        self.assertFalse(summary.passed)
        self.assertEqual(
            [self._by_id("comprehensive")["rx-version-several-boards"].name],
            failed_names(events))

    def test_a_replay_given_several_boards_fails(self):
        summary, events = run(FakeTools(bugs=("replay-several-boards",)))
        self.assertFalse(summary.passed)
        self.assertEqual(
            [self._by_id("comprehensive")["rx-replay-several-boards"].name],
            failed_names(events))

    def test_a_dump_given_several_boards_fails(self):
        summary, events = run(FakeTools(bugs=("dump-several-boards",)))
        self.assertFalse(summary.passed)
        self.assertEqual(
            [self._by_id("comprehensive")["rx-dump-several-boards"].name],
            failed_names(events))

    def test_a_refusal_that_goes_on_to_say_more_fails(self):
        """A refusal is its own line and nothing else. A tool that printed the
        right message and then a second reason, a note, or a warning would
        pass a check for the message alone, and would leave a reader unable to
        tell which of the two sentences to act on; every case must notice."""
        expected = sorted(self._by_id("comprehensive")[case_id].name
                          for case_id in self.SMOKE + self.COMPREHENSIVE)
        for bug in ("second-error-line", "note-after-refusal",
                    "warning-after-refusal"):
            with self.subTest(bug):
                summary, events = run(FakeTools(bugs=(bug,)))
                self.assertFalse(summary.passed)
                self.assertEqual(expected, failed_names(events))


class BluezStrayWordTests(unittest.TestCase):
    """The one argument rule on ubertooth-scan and ubertooth-follow (fork
    0b63b01, opt_check_no_operands(); ubertooth-scan.md and
    ubertooth-follow.md, ARGUMENTS)."""

    def test_cases_need_bluez_and_name_the_offending_word(self):
        _a, smoke = cases("smoke")
        smoke_ids = {c.case_id for c in smoke}
        self.assertLessEqual({"scan-stray", "follow-stray"}, smoke_ids)
        self.assertFalse({"scan-help-stray", "follow-bare-dash"} & smoke_ids)
        _a, full = cases("comprehensive")
        ids = [c.case_id for c in full]
        for tool in ("scan", "follow"):
            for slug in ("stray", "help-stray", "bare-dash", "after-double-dash"):
                case = full[ids.index(f"{tool}-{slug}")]
                with self.subTest(case.case_id):
                    self.assertEqual((f"ubertooth-{tool}",), case.needs.bluez_tools)
                    refuse = case.steps[[s.step_id for s in case.steps].index("refuse")]
                    self.assertEqual(refusal_cases.NO_DEVICES + (f"ubertooth-{tool}",),
                                     refuse.resolve_argv({"capture dir": "/x"})[:3])
                    self.assertEqual((1,), refuse.expected_returncodes)
        case = full[ids.index("scan-after-double-dash")]
        refuse = case.steps[[s.step_id for s in case.steps].index("refuse")]
        self.assertEqual(("--", "-x"), refuse.resolve_argv({"capture dir": "/x"})[3:])
        self.assertIn("'\\-x'", refuse.output_patterns[0])

    def test_not_applicable_without_bluez(self):
        from ubertooth_harness.hostneeds import HostCapabilities, unmet
        _a, full = cases("comprehensive")
        mac = HostCapabilities("Host_Mac", "macos", bluez_tools=frozenset())
        left_out = [c.case_id for c in full if unmet(c.needs, mac)]
        for tool in ("scan", "follow"):
            for slug in ("stray", "help-stray", "bare-dash", "after-double-dash"):
                self.assertIn(f"{tool}-{slug}", left_out)

    def test_help_before_the_stray_check_fails(self):
        summary, events = run(FakeTools(bugs=("help-first",)))
        self.assertFalse(summary.passed)
        self.assertIn("scan: -h and a stray word 'stray' is refused", failed_names(events))
        self.assertIn("follow: -h and a stray word 'stray' is refused", failed_names(events))

    def test_a_word_after_double_dash_read_as_an_option_fails(self):
        summary, events = run(FakeTools(bugs=("no-double-dash",)))
        self.assertFalse(summary.passed)
        self.assertEqual(["follow: -x after -- is refused", "scan: -x after -- is refused"],
                         failed_names(events))


if __name__ == "__main__":
    unittest.main()
