"""Case builder for Refused command lines: no Ubertooth needed.

Fork main 6fee30c (SqueakyFloors/ubertooth#193, fixed in #264) documents in
host/doc/ubertooth-{btle,dfu,dump,follow,rx,specan}.md that a refused command
line creates and truncates nothing: every file option only names its file
while the command line is read, and the file is opened once every check has
passed (ubertooth-dfu adds: and leaves no temporary file). The same change
moved "Ignoring extra capture file" to standard error, so it cannot land in a
capture written to stdout (ubertooth-btle -q -).

Each case seeds files with known contents in its own capture directory,
refuses one command naming a new file and one naming a seeded file, then
checks that the new file does not exist, the seeded one is unchanged, and
nothing else appeared. Every tool runs as ``env UBERTOOTH_TEST_NO_DEVICES=1
<tool> ...`` (libubertooth, fork b6d8ede), so a command that got past its
argument checks finds no board instead of opening one; a refusal must come
before that, so the switch's "no board is selected" note is forbidden in the
refusals. Forks before b6d8ede ignore the switch: the first step reads the
built libubertooth's bytes (refusal.py probe, never running a tool) and the
case skips when it lacks the variable's name, or when the tool is not built
(ubertooth-follow needs BlueZ, so macOS has none).

Comprehensive coverage adds the checks that run after the option loop, a
repeated -d, and ubertooth-dfu with an input that cannot be used. One of those
is strict and fails at 6fee30c:

- SqueakyFloors/ubertooth#286: ubertooth-dfu -s <file.bin> -d <bad image>
  writes (or overwrites) <file>.dfu before the image check refuses the
  command.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from ubertooth_harness import refusal
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    require,
)

_HELPER = str(Path(refusal.__file__).resolve())
NO_DEVICES = ("env", "UBERTOOTH_TEST_NO_DEVICES=1")
# Printed once a tool asks libubertooth for a board while the switch is set:
# the command line was accepted. A refusal must come before it.
NO_BOARD_NOTE = r"UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected"
STRAY = "stray"
_STRAY_PATTERN = rf"error: unexpected argument '{STRAY}'"
_EXTRA = "Ignoring extra capture file: "
_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class Tool:
    name: str
    usage: str                    # first usage line, as a pattern
    help_returncode: int          # -h exit status
    value: tuple[str, ...] = ()   # a later option with a bad value
    value_pattern: str = ""       # what the tool says about it
    files: tuple[tuple[str, str], ...] = ()  # (option, extension)


TOOLS = (
    Tool("ubertooth-btle", r"(?m)^ubertooth-btle - passive Bluetooth Low Energy", 1,
         ("-A", "40"), r"error: advertising channel index must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-c", "pcap"))),
    Tool("ubertooth-follow", r"(?m)^ubertooth-follow - active", 1,
         ("-e", "9"), r"error: maximum access code errors must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-d", "dump"))),
    Tool("ubertooth-rx", r"(?m)^ubertooth-rx - passive Classic Bluetooth", 1,
         ("-c", "79"), r"error: channel must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-d", "dump"))),
    # ubertooth-dump has no option value to check; -U without its value is
    # getopt's refusal.
    Tool("ubertooth-dump", r"(?m)^ubertooth-dump - output a continuous stream", 1,
         ("-U",), r"option requires an argument",
         (("-d", "dump"),)),
    Tool("ubertooth-specan", r"(?m)^ubertooth-specan - output a continuous stream", 0,
         ("-l", "9999"), r"error: lower frequency must be ",
         (("-d", "specan"),)),
    # -s names the .bin to sign and writes the .dfu beside it; -u writes its
    # name through a temporary file. A second -s that is not a .bin is refused.
    Tool("ubertooth-dfu", r"(?m)^ubertooth-dfu - Ubertooth firmware update tool", 1,
         ("-s", "other.txt"), r"Error: -s requires a \.bin",
         (("-s", "bin"), ("-u", "bin"))),
)


def _short(tool: str) -> str:
    return tool.removeprefix("ubertooth-")


def _path(name):
    def path(context):
        return str(Path(require(context, CAPTURE_DIR_KEY)) / name)
    return path


def _dir(context):
    return require(context, CAPTURE_DIR_KEY)


def probe_step(tool: str) -> Step:
    return Step(
        "probe", f"Check that the built libubertooth knows UBERTOOTH_TEST_NO_DEVICES "
        f"and {tool} is installed (reads the library, runs no tool)",
        (sys.executable, _HELPER, "probe", tool),
        observe=Observation(
            "no-device switch", r"No-device switch: (?P<value>[^\n]+)",
            reject=lambda value: "" if value.startswith("yes") else value,
        ),
    )


def _seed(names) -> Step:
    return Step(
        "seed", "Seed " + ", ".join(names) + " with known contents",
        lambda context: (sys.executable, _HELPER, "seed", _dir(context), *names),
        output_patterns=(r"Seeded: \d+ file",),
    )


def _check(absent, kept, step_id="check") -> Step:
    what = []
    if absent:
        what.append(", ".join(absent) + " absent")
    if kept:
        what.append(", ".join(kept) + " unchanged")
    return Step(
        step_id, "Check " + "; ".join(what) + "; nothing else in the directory",
        lambda context: (sys.executable, _HELPER, "check", _dir(context),
                         "--absent", *absent, "--kept", *kept),
        output_patterns=(r"Refusal check: ",),
    )


def tool_step(step_id, tool, words, *, returncodes=(1,), patterns=(),
               forbidden=(NO_BOARD_NOTE,), check=None) -> Step:
    """One tool command. ``words`` are strings, or ("file", name) for a path
    in the capture directory."""
    def shown(word):
        return f"<dir>/{word[1]}" if isinstance(word, tuple) else word

    def argv(context):
        return NO_DEVICES + (tool,) + tuple(
            _path(word[1])(context) if isinstance(word, tuple) else word
            for word in words)

    return Step(
        step_id, " ".join(("env UBERTOOTH_TEST_NO_DEVICES=1", tool)
                          + tuple(shown(word) for word in words)),
        argv, timeout_s=_TIMEOUT_S, expected_returncodes=tuple(returncodes),
        output_patterns=tuple(patterns), forbidden_patterns=tuple(forbidden),
        output_check=check,
    )


def _case(case_id, name, steps, expected_state) -> StateCase:
    return StateCase(
        case_id, name, tuple(steps),
        # Nothing on a device to restore; list what the case left (names and
        # sizes only) so the record shows the directory's final state.
        (Step("list-files", "List the files in the case directory",
              lambda context: (sys.executable, _HELPER, "files", _dir(context)),
              output_patterns=(r"Refusal files: \d+ in ",)),),
        expected_state=expected_state,
        capture_dir=True,
    )


def _no_extra_on_stdout(stdout: str) -> str:
    if _EXTRA in stdout:
        return f"'{_EXTRA.strip()}' went to stdout, where a capture can be"
    return ""


def _ways(tool: Tool):
    """(slug, label, extra words, exit statuses, patterns) per refusal."""
    ways = [
        ("stray", f"a stray word '{STRAY}'", (STRAY,), (1,), (_STRAY_PATTERN,)),
        ("help", "-h", ("-h",), (tool.help_returncode,), (tool.usage,)),
    ]
    if tool.value:
        ways.append(("value", "a bad later " + " ".join(tool.value), tool.value, (1,),
                     (tool.value_pattern,)))
    return ways


def _file_names(tool: Tool, option: str, extension: str):
    """(seeded, new word, kept word, absent names, kept names)."""
    if tool.name == "ubertooth-dfu" and option == "-s":
        # The .bin is the input; the .dfu beside it is the output.
        return (("new.bin", "kept.bin", "kept.dfu"), "new.bin", "kept.bin",
                ("new.dfu",), ("new.bin", "kept.bin", "kept.dfu"))
    new, kept = f"new.{extension}", f"kept.{extension}"
    return (kept,), new, kept, (new,), (kept,)


def _refusal_case(tool: Tool, option: str, extension: str, way) -> StateCase:
    slug, label, extra, returncodes, patterns = way
    seeded, new, kept, absent, unchanged = _file_names(tool, option, extension)
    short = _short(tool.name)
    return _case(
        f"{short}-{option[1:]}-{slug}", f"{short} {option}: {label} creates and "
        "truncates nothing",
        (
            probe_step(tool.name),
            _seed(seeded),
            tool_step("refuse-new", tool.name, (option, ("file", new)) + extra,
                       returncodes=returncodes, patterns=patterns),
            tool_step("refuse-kept", tool.name, (option, ("file", kept)) + extra,
                       returncodes=returncodes, patterns=patterns),
            _check(absent, unchanged),
        ),
        f"refused (exit {returncodes[0]}) before any board is asked for; "
        + ", ".join(absent) + " not created; " + ", ".join(unchanged)
        + " unchanged; no other file",
    )


def _extra_capture_cases():
    """'Ignoring extra capture file' goes to stderr, and neither file of a
    refused command appears."""
    btle = _case(
        "btle-q-stdout-extra", "btle -n -q - -q <file>: the extra-file note is on stderr",
        (
            probe_step("ubertooth-btle"),
            # Accepted, so the PCAP header goes to stdout and the board lookup
            # finds none: the note must not be in that capture. -n gives the
            # command its work: since fork 482264d (#287) a command line with
            # no mode is refused.
            tool_step("accepted", "ubertooth-btle",
                       ("-n", "-q", "-", "-q", ("file", "extra.pcap")),
                       patterns=(_EXTRA + r"\S*extra\.pcap", NO_BOARD_NOTE),
                       forbidden=(), check=_no_extra_on_stdout),
            _check(("extra.pcap",), ()),
        ),
        "exit 1 (no board); the note on stderr only; extra.pcap not created",
    )
    cases = [btle]
    for tool, option, extension in (("ubertooth-rx", "-r", "pcapng"),
                                    ("ubertooth-follow", "-q", "pcap")):
        first, second = f"first.{extension}", f"second.{extension}"
        short = _short(tool)
        cases.append(_case(
            f"{short}-{option[1:]}-repeated-stray",
            f"{short} {option} twice and a stray word: the note is on stderr",
            (
                probe_step(tool),
                tool_step("refuse", tool, (option, ("file", first), option,
                                            ("file", second), STRAY),
                           patterns=(_EXTRA + rf"\S*{second}", _STRAY_PATTERN),
                           check=_no_extra_on_stdout),
                _check((first, second), ()),
            ),
            "refused (exit 1); the note on stderr only; neither file created",
        ))
    return cases


def _comprehensive_cases():
    cases = []

    def simple(case_id, name, tool, words, patterns, absent, expected, *,
               seeded=(), kept=()):
        steps = [probe_step(tool)]
        if seeded:
            steps.append(_seed(seeded))
        steps += [tool_step("refuse", tool, words, patterns=patterns),
                  _check(absent, kept)]
        cases.append(_case(case_id, name, steps, expected))

    # Checks that run after the option loop, before any file is opened.
    simple("btle-r-mode-conflict", "btle -r with -f -n", "ubertooth-btle",
           ("-r", ("file", "new.pcapng"), "-f", "-n"),
           (r"error: choose one of -f, -n or -p",), ("new.pcapng",),
           "refused (exit 1); new.pcapng not created")
    simple("btle-q-aa-read-and-set", "btle -q with -a and -a<address>", "ubertooth-btle",
           ("-q", ("file", "new.pcap"), "-a", "-a12345678"),
           (r"error: -a cannot both read and set the access address",), ("new.pcap",),
           "refused (exit 1); new.pcap not created")
    simple("btle-c-pcapng-stdout", "btle -c with -r -", "ubertooth-btle",
           ("-c", ("file", "new.pcap"), "-r", "-"),
           (r"PCAPNG capture to stdout is not supported",), ("new.pcap",),
           "refused (exit 1); new.pcap not created")
    simple("rx-d-survey-address", "rx -d with -z -l", "ubertooth-rx",
           ("-d", ("file", "new.dump"), "-z", "-l", "123456"),
           (r"No address should be specified for survey mode",), ("new.dump",),
           "refused (exit 1); new.dump not created")
    simple("rx-q-uap-without-lap", "rx -q with -u and no -l", "ubertooth-rx",
           ("-q", ("file", "new.pcap"), "-u", "12"),
           (r"error: -u <UAP> requires -l <LAP>",), ("new.pcap",),
           "refused (exit 1); new.pcap not created")
    simple("specan-d-range", "specan -d with -l above -u", "ubertooth-specan",
           ("-d", ("file", "new.specan"), "-l", "2480", "-u", "2402"),
           (r"error: lower frequency must not exceed upper frequency",), ("new.specan",),
           "refused (exit 1); new.specan not created")
    # A repeated -d keeps the last name; neither is opened when refused.
    for tool in ("ubertooth-follow", "ubertooth-rx", "ubertooth-dump", "ubertooth-specan"):
        spec = next(item for item in TOOLS if item.name == tool)
        short = _short(tool)
        returncodes = (spec.help_returncode,)
        cases.append(_case(
            f"{short}-d-repeated-help", f"{short} -d twice with -h", (
                probe_step(tool),
                tool_step("refuse", tool, ("-d", ("file", "first.dump"), "-d",
                                            ("file", "second.dump"), "-h"),
                           returncodes=returncodes, patterns=(spec.usage,)),
                _check(("first.dump", "second.dump"), ()),
            ), f"usage (exit {returncodes[0]}); neither file created"))
    # ubertooth-dfu with an input it cannot use: -u replaces its output only
    # once a whole read-back has arrived, and leaves no temporary file
    # whichever way it ends on its own (ubertooth-dfu.md, READING FLASH BACK).
    simple("dfu-u-missing-sign-input", "dfu -u <kept> -s <missing .bin>", "ubertooth-dfu",
           ("-u", ("file", "kept.bin"), "-s", ("file", "missing.bin")),
           (r"missing\.bin: No such file",), ("missing.dfu",),
           "exit 1; kept.bin unchanged; no temporary file",
           seeded=("kept.bin",), kept=("kept.bin",))
    simple("dfu-u-bad-image", "dfu -u <kept> -d <bad image>", "ubertooth-dfu",
           ("-u", ("file", "kept.bin"), "-d", ("file", "bad.dfu")),
           (r"Signature check failed",), (),
           "exit 1 with no device opened; kept.bin unchanged; no temporary file",
           seeded=("kept.bin", "bad.dfu"), kept=("kept.bin", "bad.dfu"))
    # Strict: fails at 6fee30c (SqueakyFloors/ubertooth#286). The image check
    # refuses the command, but -s has already written the .dfu.
    cases.append(_case(
        "dfu-s-bad-image", "dfu -s <file.bin> -d <bad image> writes no .dfu", (
            probe_step("ubertooth-dfu"),
            _seed(("new.bin", "kept.bin", "kept.dfu", "bad.dfu")),
            tool_step("refuse-new", "ubertooth-dfu",
                       ("-s", ("file", "new.bin"), "-d", ("file", "bad.dfu")),
                       patterns=(r"Signature check failed",)),
            tool_step("refuse-kept", "ubertooth-dfu",
                       ("-s", ("file", "kept.bin"), "-d", ("file", "bad.dfu")),
                       patterns=(r"Signature check failed",)),
            _check(("new.dfu",), ("new.bin", "kept.bin", "kept.dfu", "bad.dfu")),
        ), "exit 1 with no device opened; new.dfu not created; kept.dfu unchanged"))
    return cases


def refusal_cases(arguments, _resources, _resource_identities):
    """Host-only cases: every tool runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    cases = []
    for tool in TOOLS:
        for option, extension in tool.files:
            for way in _ways(tool):
                cases.append(_refusal_case(tool, option, extension, way))
    cases += _extra_capture_cases()
    if arguments["coverage"] == "comprehensive":
        cases += _comprehensive_cases()
    return tuple(cases)
