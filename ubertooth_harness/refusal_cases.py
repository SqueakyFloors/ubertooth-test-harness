"""Case builder for Refused command lines: no Ubertooth needed.

Fork main 6fee30c (SqueakyFloors/ubertooth#193, fixed in #264) documents in
host/doc/ubertooth-{btle,dfu,dump,follow,rx,specan}.md that a refused command
line creates and truncates nothing: every file option only names its file
while the command line is read, and the file is opened once every check has
passed (ubertooth-dfu adds: and leaves no temporary file). The same change
moved "ignoring extra capture file" to standard error, so it cannot land in a
capture written to stdout (ubertooth-btle -q -). Since fork e4f1dc5 (fork
#377) ubertooth-btle words that note "warning: ignoring extra capture file:"
(ubertooth-btle.md, DIAGNOSTICS). -h exits 0 on every tool since fork
12211ce (fork #406; btle since e4f1dc5) and still creates and truncates
nothing.

The one-value-each rule (opt_check_once() in opt_parse.c, called from every
tool's option loop before its switch): no option may be given a value twice.
"-r a.pcapng -r b.pcapng" names two files for one capture, so the command
line is refused with "error: option '-r' given more than once" and exit 1,
rather than one of the two being dropped. It is refused before the value is
looked at and before -h, so a repeat beats both a bad value and the usage
text. Only ubertooth-btle's -q and -c still share one capture handle, and
they are different letters: naming a file with each is the one note left.

Each case seeds files with known contents in its own capture directory,
refuses one command naming a new file and one naming a seeded file, then
checks that the new file does not exist, the seeded one is unchanged, and
nothing else appeared. Every tool runs as ``env UBERTOOTH_TEST_NO_DEVICES=1
<tool> ...`` (libubertooth, fork b6d8ede), so a command that got past its
argument checks finds no board instead of opening one; a refusal must come
before that, so the switch's "no board is selected" note is forbidden in the
refusals. Forks before b6d8ede ignore the switch: the first step reads the
built libubertooth's bytes (refusal.py probe, never running a tool) and the
case skips when it lacks the variable's name. That is a runtime skip because
only the built library can tell. The ubertooth-follow and ubertooth-scan
cases declare that they need BlueZ (hostneeds.py), so on a host without it
(every macOS host) they are left out before the run as not applicable.

The BlueZ-only ubertooth-scan and ubertooth-follow must also refuse a stray
word (the one argument rule): smoke checks a stray word on each, and
comprehensive adds -h with a stray word, a bare -, and a word after --.

Comprehensive coverage adds the checks that run after the option loop, the
pairs of options a tool cannot take together (ubertooth-dump -c with -l: one
capture uses one demodulator, fork d3429234, fork #650), a
repeated -d alongside -h, and ubertooth-dfu with an input that cannot be
used, including
-s <file.bin> -d <bad image>: since fork f2e06fa (#286) the files that are
only read are checked first, so the refused image neither creates nor
replaces <file>.dfu (ubertooth-dfu.md).

ubertooth-btle's replay (fork 04dbe53c, fork #1129) decides four things of
its own, each before a board is asked for and before any destination is
opened for writing, so they belong here (ubertooth-btle.md, REPLAYING A
DUMP). The first three are decided while the command line is read; the
fourth comes after the replay's input has been opened for reading, because
identifying the file it holds takes the open input, so a -R input that is
not there is reported before it:

  * -R needs the major mode its dump was recorded with, because a record does
    not say which mode wrote it and -f/-n and -p stamp their packets from
    different clocks, so the times a replay reconstructs would be wrong past
    the first wrap with nothing in the file to say so;
  * every option that drives, names, or tunes a board (-k, -t, -A, -U, and
    the interference letters -i, -I, -j, -J) is refused beside -R rather than
    accepted and dropped without a word;
  * -A with -p is refused for not going with promiscuous mode at all, which
    is decided before the replay's own refusals, so the message is about the
    mode rather than about there being no board to tune;
  * a -d that would write over the dump -R is replaying is refused with the
    dump left as it was, since a destination is opened for writing and would
    empty the file before a record of it was read. It is the file that is
    compared and not the name. One capture may have standard output, so -q or
    -c and -d both asking for it is refused as well, as is either pair naming
    one file.

The link, descriptor and redirected-standard-output forms of that last
comparison are covered on ubertooth-rx in Raw receive replay: both tools hand
their names to the same opt_parse.c helpers, so what is checked here is
ubertooth-btle's own call and its own option letters.

ubertooth-dfu -u writes its read-back through a temporary file renamed over
the name given, and rewinds to block zero to retry, neither of which a pipe
can do, so a value naming standard output is refused as the value is read --
before -h, and before any board (ubertooth-dfu.md, READING FLASH BACK). The
lone "-" and the names of descriptor 1 are refused; only the lone "-" is the
stream, so a path whose last component is "-" names a file of that name, and
that command line is accepted and goes on to look for a board.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ubertooth_harness import refusal
from ubertooth_harness.hostneeds import tool_needs
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.stateful import (
    StateCaseError,
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
# ubertooth-btle's wording since fork e4f1dc5 (fork #377); it is the only
# note of its kind left, because -q and -c share one capture handle.
_EXTRA = "warning: ignoring extra capture file: "
_TIMEOUT_S = 10.0


def _repeated(option: str) -> str:
    """What a tool says when an option is given a value twice."""
    return rf"(?m)^error: option '{re.escape(option)}' given more than once$"


@dataclass(frozen=True)
class Tool:
    name: str
    usage: str                    # first usage line, as a pattern
    help_returncode: int          # -h exit status
    value: tuple[str, ...] = ()   # a later option with a bad value
    value_pattern: str = ""       # what the tool says about it
    files: tuple[tuple[str, str], ...] = ()  # (option, extension)


TOOLS = (
    # -h exits 0 on every tool: btle since fork e4f1dc5 (fork #377), follow
    # and dfu since 12211ce (fork #406); each was 1.
    Tool("ubertooth-btle", r"(?m)^ubertooth-btle - passive Bluetooth Low Energy", 0,
         ("-A", "40"), r"error: advertising channel index must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-c", "pcap"))),
    Tool("ubertooth-follow", r"(?m)^ubertooth-follow - active", 0,
         ("-e", "9"), r"error: maximum access code errors must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-d", "dump"))),
    Tool("ubertooth-rx", r"(?m)^ubertooth-rx - passive Classic Bluetooth", 0,
         ("-c", "79"), r"error: channel must be ",
         (("-r", "pcapng"), ("-q", "pcap"), ("-d", "dump"))),
    # ubertooth-dump has no option value to check; -U without its value is
    # the tool's own refusal (fork aafca8f).
    Tool("ubertooth-dump", r"(?m)^ubertooth-dump - output a continuous stream", 0,
         ("-U",), r"error: option '-U' requires a value",
         (("-d", "dump"),)),
    Tool("ubertooth-specan", r"(?m)^ubertooth-specan - output a continuous stream", 0,
         ("-l", "9999"), r"error: lower frequency must be ",
         (("-d", "specan"),)),
    # -s names the .bin to sign and writes the .dfu beside it; -u writes its
    # name through a temporary file. A second -s that is not a .bin is refused
    # ("error: ..." since fork 12211ce, it was "Error: ...").
    Tool("ubertooth-dfu", r"(?m)^ubertooth-dfu - Ubertooth firmware update tool", 0,
         ("-s", "other.txt"), r"error: -s requires a \.bin",
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


def _link(*, symlinks=(), hardlinks=()) -> Step:
    """Give a seeded file more names, for the cases about one file under two."""
    names = [f"{name} -> {target}" for name, target in tuple(symlinks) + tuple(hardlinks)]
    arguments = [word for name, target in symlinks
                 for word in ("--symlink", f"{name}={target}")]
    arguments += [word for name, target in hardlinks
                  for word in ("--hardlink", f"{name}={target}")]
    return Step(
        "link", "Link " + ", ".join(names),
        lambda context: (sys.executable, _HELPER, "link", _dir(context), *arguments),
        output_patterns=(r"Linked: \d+ symbolic, \d+ hard, ",),
    )


def _check(absent, kept, step_id="check", *, symlinks=(), hardlinks=()) -> Step:
    what = []
    if absent:
        what.append(", ".join(absent) + " absent")
    if kept:
        what.append(", ".join(kept) + " unchanged")
    links = tuple(symlinks) + tuple(hardlinks)
    if links:
        what.append(", ".join(f"{name} still linked to {target}"
                              for name, target in links))
    arguments = ["--absent", *absent, "--kept", *kept]
    arguments += [word for name, target in symlinks
                  for word in ("--symlink", f"{name}={target}")]
    arguments += [word for name, target in hardlinks
                  for word in ("--hardlink", f"{name}={target}")]
    return Step(
        step_id, "Check " + "; ".join(what) + "; nothing else in the directory"
        if what else "Check that nothing is in the directory",
        lambda context: (sys.executable, _HELPER, "check", _dir(context),
                         *arguments),
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
    steps = tuple(steps)
    # Every case starts with probe_step(<tool>); a tool built only with BlueZ
    # (ubertooth-follow) makes the case not applicable where it is not built.
    # Every case starts with probe_step(tool), which names the tool the case
    # needs; refuse to build one that does not, so its needs cannot be lost.
    if not steps or steps[0].step_id != "probe":
        raise StateCaseError(f"{case_id}: the first step must be the probe step")
    tool = steps[0].argv[-1]
    return StateCase(
        case_id, name, steps,
        # Nothing on a device to restore; list what the case left (names and
        # sizes only) so the record shows the directory's final state.
        (Step("list-files", "List the files in the case directory",
              lambda context: (sys.executable, _HELPER, "files", _dir(context)),
              output_patterns=(r"Refusal files: \d+ in ",)),),
        expected_state=expected_state,
        capture_dir=True,
        needs=tool_needs(tool),
    )


def _no_extra_on_stdout(stdout) -> str:
    for line in text_lines(stdout):
        if "ignoring extra capture file" in line.lower():
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
    if slug == "value" and extra[0] == option:
        # The file option and the later option are the same letter, so this
        # is a repeat: it is refused before the value is looked at, which
        # the value's own refusal never gets to say.
        label = f"a second {' '.join(extra)}"
        patterns = (_repeated(option),)
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


def _repeat_and_extra_cases():
    """A repeated capture option is refused, and the one note left for two
    files through one handle goes to stderr."""
    btle = _case(
        "btle-q-stdout-extra", "btle -n -q - -c <file>: the extra-file note is on stderr",
        (
            probe_step("ubertooth-btle"),
            # -q and -c share one PCAP handle, so the first of the two wins
            # and the second file is only reported. The command is accepted,
            # so it reaches the board lookup and finds none with the switch
            # set: the note must not have gone to stdout, where the capture
            # -q - writes would be. -n gives the command its work: since fork
            # 482264d (#287) a command line with no mode is refused.
            tool_step("accepted", "ubertooth-btle",
                       ("-n", "-q", "-", "-c", ("file", "extra.pcap")),
                       patterns=(_EXTRA + r"\S*extra\.pcap", NO_BOARD_NOTE),
                       forbidden=(), check=_no_extra_on_stdout),
            _check(("extra.pcap",), ()),
        ),
        "exit 1 (no board); the note on stderr only; extra.pcap not created",
    )
    cases = [btle]
    for tool, option, extension in (("ubertooth-btle", "-q", "pcap"),
                                    ("ubertooth-rx", "-r", "pcapng"),
                                    ("ubertooth-follow", "-q", "pcap")):
        first, second = f"first.{extension}", f"second.{extension}"
        short = _short(tool)
        cases.append(_case(
            f"{short}-{option[1:]}-repeated-stray",
            f"{short} {option} twice and a stray word: the repeat is refused",
            (
                probe_step(tool),
                # The repeat is caught in the option loop, so it is reported
                # instead of the stray word and instead of any note about an
                # extra file.
                tool_step("refuse", tool, (option, ("file", first), option,
                                            ("file", second), STRAY),
                           patterns=(_repeated(option),),
                           forbidden=(NO_BOARD_NOTE, _STRAY_PATTERN, _EXTRA)),
                _check((first, second), ()),
            ),
            f"refused (exit 1) with error: option '{option}' given more than once, "
            "before the stray word and before any board is asked for; neither "
            "file created",
        ))
    return cases


# ubertooth-btle's replay input, seeded like any other file here, so every
# case can require it byte for byte afterwards: a command line refused for
# naming it as a destination must not have opened it for writing. A seeded
# file is a line of text rather than records, which the replay cases that
# reach a decode report as a partial record; none of these reaches one.
_DUMP = "dump.bin"
_NEEDS_MODE = (
    r"(?m)^error: -R needs -f, -n or -p: a dump does not record which mode "
    r"wrote it, and the modes stamp their packets with different clocks$"
)
# The channel refusal that is decided before the replay's own, so that
# "-p -R <dump> -A 38" is about -p and -A rather than about a replay having no
# board to tune.
_CHANNEL_NOT_WITH_PROMISC = (
    r"(?m)^error: -A does not go with -p: promiscuous mode listens on the "
    r"channel the board is holding$"
)


def _board_option_with_replay(option: str) -> str:
    """What btle says about an option that drives a board, beside -R."""
    return (rf"(?m)^error: -{re.escape(option)} cannot be combined with -R; a "
            r"replay reads a saved dump rather than driving a board$")


def _dump_over_input(shown: str) -> str:
    """What btle says when -d would write over the dump -R is replaying.

    The name is reported as the user wrote it, which here is a path in the
    case directory, so ``shown`` is the tail of it as a pattern.
    """
    return (r"(?m)^error: -d would write over the dump -R is replaying "
            rf"\(\S*{shown}\)$")


def _two_captures_on_stdout(option: str) -> str:
    """-q or -c and -d both asking for the one stream standard output is."""
    return (rf"(?m)^error: -{option} and -d cannot both write to standard "
            r"output$")


def _replay_case(case_id, name, words, patterns, expected, *,
                 absent=(), forbidden=(NO_BOARD_NOTE,),
                 seeded=(_DUMP,), kept=(_DUMP,)) -> StateCase:
    """One refused ubertooth-btle replay command line, over a seeded dump."""
    steps = [probe_step("ubertooth-btle")]
    if seeded:
        steps.append(_seed(seeded))
    steps += [
        tool_step("refuse", "ubertooth-btle", words, patterns=patterns,
                  forbidden=forbidden),
        _check(absent, kept),
    ]
    return _case(case_id, name, steps, expected)


def _replay_cases(coverage: str):
    """btle -R and -d: the decisions made before a board or a file is opened."""
    cases = [
        # A replay with no major mode: refused before the dump is opened.
        _replay_case(
            "btle-replay-no-mode", "btle -R <dump> with no -f, -n or -p",
            ("-R", ("file", _DUMP)), (_NEEDS_MODE,),
            "refused (exit 1) before any board is asked for; the dump "
            "unchanged; nothing created"),
        # The dump -R is replaying, named as the destination of -d: opening it
        # for writing would empty it before a record was read.
        _replay_case(
            "btle-replay-dump-over-input", "btle -f -R <dump> -d <the same dump>",
            ("-f", "-R", ("file", _DUMP), "-d", ("file", _DUMP)),
            (_dump_over_input(re.escape(_DUMP)),),
            "refused (exit 1) naming the dump, before it is opened for "
            "writing; the dump unchanged byte for byte; nothing created"),
        # Standard output carries one stream, so one capture can have it.
        _replay_case(
            "btle-q-d-stdout", "btle -n -q - -d -: one stream, one capture",
            ("-n", "-q", "-", "-d", "-"), (_two_captures_on_stdout("q"),),
            "refused (exit 1) before any board is asked for; nothing created",
            seeded=(), kept=()),
    ]
    if coverage != "comprehensive":
        return tuple(cases)
    # Every option that drives, names, or tunes a board, beside -R. -f is the
    # mode throughout: -A is refused with -p for its own reason (below), and
    # the rest go with any of the three.
    cases.append(_case(
        "btle-replay-board-options",
        "btle -f -R <dump> with each option that drives a board",
        (
            probe_step("ubertooth-btle"),
            _seed((_DUMP,)),
            *(tool_step(
                f"refuse-{slug}", "ubertooth-btle",
                ("-f", "-R", ("file", _DUMP)) + words,
                patterns=(_board_option_with_replay(option),))
              for slug, option, words in (
                  ("crc", "k", ("-k",)),
                  ("target", "t", ("-t", "11:22:33:44:55:66")),
                  ("channel", "A", ("-A", "38")),
                  ("device", "U", ("-U", "1234")),
                  # The four interference letters are two options and their
                  # aliases; each must name itself as the user wrote it.
                  ("interfere-once", "i", ("-i",)),
                  ("interfere-always", "I", ("-I",)),
                  ("interfere-once-alias", "j", ("-j",)),
                  ("interfere-always-alias", "J", ("-J",)),
              )),
            _check((), (_DUMP,)),
        ),
        "each refused (exit 1) naming the option, before any board is asked "
        f"for; {_DUMP} unchanged; nothing created"))
    # -A with -p is about the mode, and is decided first.
    cases.append(_replay_case(
        "btle-replay-promisc-channel", "btle -p -R <dump> -A 38: -A is about -p",
        ("-p", "-R", ("file", _DUMP), "-A", "38"),
        (_CHANNEL_NOT_WITH_PROMISC,),
        "refused (exit 1) for -A not going with -p, not for the replay; the "
        "dump unchanged; nothing created",
        forbidden=(NO_BOARD_NOTE, _board_option_with_replay("A"))))
    # It is the file that is compared and not the name, so a second name for
    # the dump is the dump: a symbolic link, which the destination would be
    # opened through, and a hard link, which is the file itself under another
    # name. The redirected-standard-output and descriptor forms of the
    # comparison are covered on ubertooth-rx in Raw receive replay, which
    # reaches the same opt_parse.c helpers.
    cases.append(_case(
        "btle-replay-dump-over-input-links",
        "btle -f -R <dump> -d <another name for it>",
        (
            probe_step("ubertooth-btle"),
            _seed((_DUMP,)),
            _link(symlinks=(("link.bin", _DUMP),), hardlinks=(("hard.bin", _DUMP),)),
            *(tool_step(f"refuse-{slug}", "ubertooth-btle",
                        ("-f", "-R", ("file", source), "-d", ("file", destination)),
                        patterns=(_dump_over_input(re.escape(destination)),))
              for slug, source, destination in (
                  ("symlink", _DUMP, "link.bin"),
                  ("hardlink", _DUMP, "hard.bin"),
                  # And the other way round: the replay reads the link and
                  # the destination names the file it points at.
                  ("through-the-link", "link.bin", _DUMP),
              )),
            _check((), (_DUMP,), symlinks=(("link.bin", _DUMP),),
                   hardlinks=(("hard.bin", _DUMP),)),
        ),
        "each refused (exit 1) naming the destination as it was written: it "
        f"is the file that is compared; {_DUMP} unchanged byte for byte; "
        "nothing created"))
    # A destination pair naming one file, with the letter the PCAP option was
    # given.
    cases.append(_replay_case(
        "btle-q-d-same-file", "btle -f -R <dump> -q <file> -d <the same file>",
        ("-f", "-R", ("file", _DUMP), "-q", ("file", "cap.pcap"),
         "-d", ("file", "cap.pcap")),
        (r"(?m)^error: -q and -d cannot both write to \S*cap\.pcap$",),
        "refused (exit 1) naming both options; cap.pcap not created; the dump "
        "unchanged", absent=("cap.pcap",)))
    cases.append(_replay_case(
        "btle-c-d-stdout", "btle -n -c - -d -: the letter -c was given",
        ("-n", "-c", "-", "-d", "-"), (_two_captures_on_stdout("c"),),
        "refused (exit 1) before any board is asked for; nothing created",
        seeded=(), kept=()))
    # A replay's input is opened before the board, so a dump that is not
    # there is reported instead of the board lookup, and creates nothing.
    cases.append(_replay_case(
        "btle-replay-missing-input", "btle -f -R <a dump that is not there>",
        ("-f", "-R", ("file", "missing.bin")),
        (r"(?m)^error: \S*missing\.bin: No such file or directory$",),
        "exit 1 at the input, before any board is asked for; missing.bin not "
        "created; nothing else in the directory",
        absent=("missing.bin",), seeded=(), kept=()))
    return tuple(cases)


# ubertooth-dfu -u renames a temporary file over the name it was given and
# rewinds to block zero to retry a block, so standard output is refused as
# the value is read: before -h, and before any board.
_UPLOAD_TO_STDOUT = (
    r"(?m)^error: -u cannot write the read-back to stdout; give a file name$"
)
# The spellings of descriptor 1 that every host here has. Linux also answers
# /proc/self/fd/1; Raw receive replay covers that one, where the case can be
# left out on a host without /proc.
_STDOUT_SPELLINGS = (("dash", "-"), ("dev-stdout", "/dev/stdout"),
                     ("dev-fd-1", "/dev/fd/1"))


def _upload_stdout_cases(coverage: str):
    """dfu -u: standard output is not a file the read-back can be written to."""
    cases = [_case(
        "dfu-u-stdout", "dfu -u: the lone - and the names of descriptor 1",
        (
            probe_step("ubertooth-dfu"),
            *(tool_step(f"refuse-{slug}", "ubertooth-dfu", ("-u", spelling),
                        patterns=(_UPLOAD_TO_STDOUT,))
              for slug, spelling in _STDOUT_SPELLINGS),
            _check((), ()),
        ),
        "each refused (exit 1) as the value is read, before any board is "
        "asked for; nothing created")]
    if coverage != "comprehensive":
        return tuple(cases)
    # The value is refused in the option loop, which -h never reaches.
    dfu = next(item for item in TOOLS if item.name == "ubertooth-dfu")
    cases.append(_case(
        "dfu-u-stdout-help", "dfu -u /dev/stdout with -h: no usage text", (
            probe_step("ubertooth-dfu"),
            tool_step("refuse", "ubertooth-dfu", ("-u", "/dev/stdout", "-h"),
                      patterns=(_UPLOAD_TO_STDOUT,),
                      forbidden=(NO_BOARD_NOTE, dfu.usage)),
            _check((), ()),
        ), "refused (exit 1) with no usage text, before any board is asked "
           "for; nothing created"))
    # Only the lone "-" is the stream: a path ending in a "-" names a file of
    # that name, so the command line is accepted and the run goes on to look
    # for a board. The temporary file comes only once a read-back has
    # arrived, so a run stopped at the lookup leaves nothing.
    cases.append(_case(
        "dfu-u-dash-is-a-file", "dfu -u <dir>/-: a file whose name is -", (
            probe_step("ubertooth-dfu"),
            tool_step("accepted", "ubertooth-dfu", ("-u", ("file", "-")),
                      patterns=(NO_BOARD_NOTE,
                                r"(?m)^error: no Ubertooth devices found$"),
                      forbidden=(_UPLOAD_TO_STDOUT,)),
            _check((), ()),
        ), "accepted and stopped at the board lookup (exit 1); no file and no "
           "temporary file created"))
    return tuple(cases)


# A seeded bad.dfu is a line of text, not an image, so what the image check
# finds wrong with it depends on the seed: the one refusal line is what
# matters here, and the DFU image checks test covers the reasons one by one.
_BAD_IMAGE = r"(?m)^error: \S*bad\.dfu: [^\n]+; firmware will not be updated$"


def _comprehensive_cases():
    cases = []

    def simple(case_id, name, tool, words, patterns, absent, expected, *,
               seeded=(), kept=(), forbidden=(NO_BOARD_NOTE,)):
        steps = [probe_step(tool)]
        if seeded:
            steps.append(_seed(seeded))
        steps += [tool_step("refuse", tool, words, patterns=patterns,
                            forbidden=forbidden),
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
           (r"(?m)^error: survey mode \(-z\) discovers addresses, so it cannot be "
            r"given -l or -u$",), ("new.dump",),
           "refused (exit 1); new.dump not created")
    simple("rx-q-uap-without-lap", "rx -q with -u and no -l", "ubertooth-rx",
           ("-q", ("file", "new.pcap"), "-u", "12"),
           (r"error: -u <UAP> requires -l <LAP>",), ("new.pcap",),
           "refused (exit 1); new.pcap not created")
    simple("specan-d-range", "specan -d with -l above -u", "ubertooth-specan",
           ("-d", ("file", "new.specan"), "-l", "2480", "-u", "2402"),
           (r"error: lower frequency must not exceed upper frequency",), ("new.specan",),
           "refused (exit 1); new.specan not created")
    # One capture, one demodulator: -c and -l in one ubertooth-dump command
    # line used to mean whichever came last, with the other dropped without a
    # word, and are refused since fork d3429234 (fork #650). The refusal
    # names both options as the user wrote them, so each order is its own
    # line, and it comes from the option loop, before the -d file is opened.
    for first, second in (("-l", "-c"), ("-c", "-l")):
        simple(f"dump-d-modulation-{second[1:]}-after-{first[1:]}",
               f"dump -d with {first} {second}", "ubertooth-dump",
               ("-d", ("file", "new.dump"), first, second),
               (rf"(?m)^error: {second} cannot be combined with {first}; a capture "
                r"uses one modulation$",), ("new.dump",),
               f"refused (exit 1) naming {second} and {first}; new.dump not created")
    # Since fork 47bae592 a -d file is opened once a board is open and its
    # firmware accepted, so that a run refused for either reason leaves no
    # empty file behind (ubertooth-specan.md, DIAGNOSTICS). With no board the
    # command stops at the lookup, and the file it could not have opened is
    # neither created nor mentioned.
    simple("specan-d-unopenable", "specan -d in a missing directory, with no board",
           "ubertooth-specan", ("-d", ("file", "missing/new.specan")),
           (NO_BOARD_NOTE, r"(?m)^error: no Ubertooth devices found$"),
           ("missing",), "exit 1 at the board lookup; nothing created, and nothing "
           "said about the -d file, which is opened only once a board is",
           forbidden=(r"missing/new\.specan",))
    # A repeated -d is refused in the option loop, which -h never reaches:
    # the error line describes the problem, so no usage text follows it.
    for tool in ("ubertooth-follow", "ubertooth-rx", "ubertooth-dump", "ubertooth-specan"):
        spec = next(item for item in TOOLS if item.name == tool)
        short = _short(tool)
        cases.append(_case(
            f"{short}-d-repeated-help", f"{short} -d twice with -h", (
                probe_step(tool),
                tool_step("refuse", tool, ("-d", ("file", "first.dump"), "-d",
                                            ("file", "second.dump"), "-h"),
                           patterns=(_repeated("-d"),),
                           forbidden=(NO_BOARD_NOTE, spec.usage)),
                _check(("first.dump", "second.dump"), ()),
            ), "refused (exit 1) with error: option '-d' given more than once, "
               "no usage text; neither file created"))
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
           (_BAD_IMAGE,), (),
           "exit 1 with no device opened; kept.bin unchanged; no temporary file",
           seeded=("kept.bin", "bad.dfu"), kept=("kept.bin", "bad.dfu"))
    # The image check refuses the command before -s writes anything (fork
    # #286, fixed in f2e06fa): new.dfu is not created, kept.dfu not replaced.
    cases.append(_case(
        "dfu-s-bad-image", "dfu -s <file.bin> -d <bad image> writes no .dfu", (
            probe_step("ubertooth-dfu"),
            _seed(("new.bin", "kept.bin", "kept.dfu", "bad.dfu")),
            tool_step("refuse-new", "ubertooth-dfu",
                       ("-s", ("file", "new.bin"), "-d", ("file", "bad.dfu")),
                       patterns=(_BAD_IMAGE,)),
            tool_step("refuse-kept", "ubertooth-dfu",
                       ("-s", ("file", "kept.bin"), "-d", ("file", "bad.dfu")),
                       patterns=(_BAD_IMAGE,)),
            _check(("new.dfu",), ("new.bin", "kept.bin", "kept.dfu", "bad.dfu")),
        ), "exit 1 with no device opened; new.dfu not created; kept.dfu unchanged"))
    return cases


# The one argument rule on the BlueZ-only tools (fork 0b63b01;
# opt_check_no_operands() in opt_parse.c, called right after the option loop
# of ubertooth-scan.c and ubertooth-follow.c, before -h, any HCI device, or
# any board): a leftover word, a bare "-", or a word after "--" is refused
# with "error: unexpected argument '<word>'" and exit 1, even alongside -h
# (ubertooth-scan.md and ubertooth-follow.md, ARGUMENTS). Argument
# validation, which checks the rule on every other tool, needs a board and
# so never runs in CI; these refusals need none, so they are here and run on
# every Linux build in CI. Not applicable where the tools are not built
# (macOS: no BlueZ).
_BLUEZ_USAGE = {
    "ubertooth-scan": r"(?m)^ubertooth-scan - active",
    "ubertooth-follow": r"(?m)^ubertooth-follow - active",
}
# (slug, label, words, offending word, smoke)
_STRAY_WAYS = (
    ("stray", f"a stray word '{STRAY}'", (STRAY,), STRAY, True),
    ("help-stray", f"-h and a stray word '{STRAY}'", ("-h", STRAY), STRAY, False),
    ("bare-dash", "a bare -", ("-",), "-", False),
    ("after-double-dash", "-x after --", ("--", "-x"), "-x", False),
)


def _bluez_stray_case(tool: str, way) -> StateCase:
    slug, label, words, word, _smoke = way
    short = _short(tool)
    return _case(
        f"{short}-{slug}", f"{short}: {label} is refused",
        (
            probe_step(tool),
            tool_step("refuse", tool, words,
                       patterns=(rf"(?m)^error: unexpected argument '{re.escape(word)}'$",),
                       forbidden=(NO_BOARD_NOTE, _BLUEZ_USAGE[tool])),
            _check((), ()),
        ),
        f"refused (exit 1) with error: unexpected argument '{word}', no usage, "
        "before any board is asked for; nothing created",
    )


def bluez_stray_cases(coverage: str):
    return tuple(
        _bluez_stray_case(tool, way)
        for tool in _BLUEZ_USAGE
        for way in _STRAY_WAYS
        if coverage == "comprehensive" or way[4]
    )


def refusal_cases(arguments, _resources, _resource_identities):
    """Host-only cases: every tool runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    cases = []
    for tool in TOOLS:
        for option, extension in tool.files:
            for way in _ways(tool):
                cases.append(_refusal_case(tool, option, extension, way))
    cases += _repeat_and_extra_cases()
    cases += _replay_cases(arguments["coverage"])
    cases += _upload_stdout_cases(arguments["coverage"])
    cases += bluez_stray_cases(arguments["coverage"])
    if arguments["coverage"] == "comprehensive":
        cases += _comprehensive_cases()
    return tuple(cases)
