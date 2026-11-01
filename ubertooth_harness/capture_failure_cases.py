"""Case builder for Failed capture files: no Ubertooth needed.

A capture that fails must not leave behind something a user would later read
as a capture. The fork states where each tool stands on that:

- A command line that is refused names its files and opens none
  (``host/doc/ubertooth-rx.md``). The Refused command lines cases check that,
  so nothing here repeats them.
- A run refused before the board is usable creates no capture file at all:
  ``ubertooth-rx`` and ``ubertooth-follow`` do every step that can fail -- the
  decoder, the piconet, the firmware check, the board itself -- before they
  create their files, because a refused run used to leave an empty capture
  that a user then had to tell apart from a capture that caught nothing
  (fork #996, #1000).
- A run that fails after creating them takes them back, by name and while the
  writer still holds them, so the header bytes the writer had buffered go
  nowhere (``opt_discard_capture_file()``, fork #1009). Only a regular file at
  that name: a capture on standard output is not a file, and ``/dev/null``, a
  pipe, or a symbolic link is a path the user arranged and is left where it
  is. A removal that fails says so, as ``warning: cannot remove <file>: ...``.
- From the first packet on the file stays, however the run ends, because by
  then it holds evidence (fork #1009).
- A capture file that cannot be created is reported as
  ``error: <file>: <reason>`` carrying that create call's own reason and no
  other, and the run exits 1 rather than passing a library's return value out
  as its status (fork #565, #1020; libbtbb #117, #119).

Every case runs host-only, two ways in:

- ``env UBERTOOTH_TEST_NO_DEVICES=1 <tool> <capture options>`` reaches the
  board lookup, finds nothing, and stops there (libubertooth, fork b6d8ede):
  the no-device half of the setup failures.
- ``ubertooth-rx -i <fixture>`` replays a synthetic file
  ([`rxreplay.py`](rxreplay.py)) with no board and no USB at all, and so is
  the one path on which a capture file is really created, written to, and then
  judged, in CI and on any host. The ``partial`` fixture is one record cut
  short, which a replay reads no whole record from, so the run fails with its
  files created and nothing in them yet; the ``truncated`` fixture holds whole
  records and then a cut-short one, so the same failure arrives after real
  packets and the files must stay.

Each case states its directory before the command and again afterwards
(``dfuimage.py state``, the same complete statement the DFU image checks use),
so a temporary file, a truncated seed, or a file removed that should have
stayed fails the case. Whatever a case does leave stays in the run directory
under the name of the attempt that wrote it, so a failure keeps its evidence
rather than presenting it as a capture that worked.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ubertooth_harness import capturefile, dfuimage, refusal, rxreplay
from ubertooth_harness.hostneeds import tool_needs
from ubertooth_harness.outputfiles import text_lines
from ubertooth_harness.refusal_cases import NO_BOARD_NOTE, probe_step, tool_step
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    nothing_on_stdout,
    require,
)

_DIRECTORY = str(Path(dfuimage.__file__).resolve())
_REFUSAL = str(Path(refusal.__file__).resolve())
_REPLAY = str(Path(rxreplay.__file__).resolve())
_CAPTURE = str(Path(capturefile.__file__).resolve())
_HELPER_TIMEOUT_S = 15.0

# What a board lookup that found nothing prints. The status is 1 and not a
# libusb return value, which is what it used to be (fork #565: seen as 255,
# 247, and 249).
NO_BOARD = r"(?m)^error: no Ubertooth devices found$"
# A run that takes a file back and cannot says so. No case here expects it,
# and one that appeared would mean a path the run should have left alone.
CANNOT_REMOVE = r"warning: cannot remove "

# The fixture each case replays, by variant, and the destinations the cases
# name. A seeded destination must come back from a refused run unchanged, and
# never as half a capture.
FIXTURES = {"full": "replay.bin", "truncated": "tail-cut.bin",
            "partial": "no-record.bin", "skipped": "skipped-record.bin"}
SEEDED = "kept.pcap"
PCAPNG, PCAP, DUMP, PPI = "new.pcapng", "new.pcap", "new.dump", "new-ppi.pcap"
# The three writers, as the words a tool step takes.
ALL_WRITERS = ("-r", ("file", PCAPNG), "-q", ("file", PCAP), "-d", ("file", DUMP))


def _create_failure(name: str, reason: str) -> str:
    """The one line for a capture file a writer would not create."""
    return rf"(?m)^error: \S*{re.escape(name)}: {reason}$"


def _partial_record(variant: str) -> str:
    """What a replay says about the bytes after its last whole record."""
    return (rf"(?m)^error: \S+ ends in a partial record: "
            rf"{rxreplay.PARTIAL_RECORD_BYTES[variant]} of {rxreplay.RECORD} "
            rf"bytes, ignored$")


def _path(name):
    return lambda context: str(Path(require(context, CAPTURE_DIR_KEY)) / name)


def _dir(context):
    return require(context, CAPTURE_DIR_KEY)


def _run(step_id, name, *argv, **extra) -> Step:
    """A plain command; ("file", name) words are paths in the case directory."""
    return Step(step_id, name, lambda context: tuple(
        _path(word[1])(context) if isinstance(word, tuple) else word for word in argv),
        timeout_s=5.0, **extra)


def _seed(names) -> Step:
    return Step(
        "seed", "Seed " + ", ".join(names) + " with known contents",
        lambda context: (sys.executable, _REFUSAL, "seed", _dir(context), *names),
        output_patterns=(r"Seeded: \d+ file",),
    )


def _fixture(variant, base_time) -> Step:
    name = FIXTURES[variant]
    return Step(
        "fixture", f"Write the synthetic {variant} replay fixture as {name}",
        lambda context: (sys.executable, _REPLAY, "fixture", _path(name)(context),
                         "--variant", variant, "--base-time", str(base_time)),
        output_patterns=(rf"Replay fixture: {variant}, ",),
    )


_ENTRIES = r"Directory entries: (?P<value>\{[^\n]*\})"


def _state_before() -> Step:
    return Step(
        "state-before", "Record the case directory (names, sizes, modes, hashes)",
        lambda context: (sys.executable, _DIRECTORY, "state", _dir(context)),
        timeout_s=_HELPER_TIMEOUT_S, output_patterns=(_ENTRIES,),
        observe=Observation("directory before", _ENTRIES),
    )


def _changes(stdout) -> str:
    for line in text_lines(stdout):
        if line.startswith("Directory changed: "):
            return line
    return ""


def _state_after(allow=(), name="") -> Step:
    """The case directory as _state_before recorded it, apart from ``allow``."""
    label = name or (
        "The case directory is unchanged apart from " + ", ".join(allow) if allow
        else "The case directory is unchanged: no capture file created, nothing "
             "truncated, nothing left behind")
    return Step(
        "state-after", label,
        lambda context: (sys.executable, _DIRECTORY, "state", _dir(context),
                         "--before", require(context, "directory before"),
                         *(word for entry in allow for word in ("--allow", entry))),
        timeout_s=_HELPER_TIMEOUT_S, expected_returncodes=(0, 1),
        output_patterns=(r"Directory (unchanged|changed): ",), output_check=_changes,
    )


def _valid_capture(step_id, file_name, fmt, packets) -> Step:
    """``file_name`` is a capture a reader can open, holding ``packets``."""
    return Step(
        step_id, f"{file_name} is a readable {fmt} holding {packets} packet(s)",
        lambda context: (sys.executable, _CAPTURE, "--packets", _path(file_name)(context)),
        timeout_s=_HELPER_TIMEOUT_S,
        output_patterns=(rf"Capture format: {fmt}, link type: "
                         rf"{capturefile.LINKTYPE_BLUETOOTH_BREDR_BB}, "
                         rf"packets: {packets}$",),
    )


def _symlink(name, target) -> Step:
    """``name`` is still the symbolic link the case set up."""
    return Step(
        "symlink-kept", f"{name} is still a symbolic link to {target}",
        lambda context: (sys.executable, _DIRECTORY, "symlink", _path(name)(context),
                         "--to", target),
        timeout_s=_HELPER_TIMEOUT_S, output_patterns=(r"Symbolic link: ",))


def _dump_is_the_replay(variant) -> Step:
    """The -d dump holds the fixture's own packet records, byte for byte.

    Counting records would pass a dump of the right number of other records,
    so this compares each one with the record it came from (rxreplay.py
    ``dump-check``), which for a variant whose records the decoder skips
    means the dump must be empty.
    """
    records = len(rxreplay.packet_record_indices(variant=variant))
    return Step(
        "verify-dump",
        f"{DUMP} is the {records} record(s) the replay decoded, byte for byte",
        lambda context: (sys.executable, _REPLAY, "dump-check",
                         _path(DUMP)(context), _path(FIXTURES[variant])(context),
                         "--variant", variant),
        timeout_s=_HELPER_TIMEOUT_S,
        output_patterns=(rf"Replay dump: {records} records match the fixture",),
    )


def _not_root() -> Step:
    """Skips the case when a read-only directory would not stop this process."""
    return Step(
        "root", "Does this run as root", (sys.executable, _DIRECTORY, "not-root"),
        timeout_s=_HELPER_TIMEOUT_S, output_patterns=(r"Root: ",),
        observe=Observation("root", r"Root: (?P<value>[^\n]+)",
                            reject=lambda value: value if value.startswith("yes") else ""),
    )


def _case(case_id, name, tool, steps, expected_state, *, cleanup=()) -> StateCase:
    return StateCase(
        case_id, name, (probe_step(tool), *steps),
        # No case here opens a board, so there is nothing on a device to
        # restore; a case that changed a directory's mode puts it back. The
        # last step records what the case left, so the run holds the evidence.
        (*cleanup,
         Step("list-files", "List the files the case left behind",
              lambda context: (sys.executable, _DIRECTORY, "state", _dir(context)),
              timeout_s=_HELPER_TIMEOUT_S, output_patterns=(_ENTRIES,))),
        expected_state=expected_state, capture_dir=True, needs=tool_needs(tool))


# --- a setup failure: the board lookup finds nothing --------------------------


def _no_device_case(tool, destinations, *, mode=(), suffix="") -> StateCase:
    """``tool`` with capture options and no board: it stops at the lookup.

    One of the destinations is the seeded file, so the case states not only
    that no file was created but that a destination the user already had is
    not truncated on the way to the lookup. It is never given to ``-r``,
    which refuses a name that exists (ubertooth-rx.md).
    """
    words = mode + tuple(
        word for option, name in destinations for word in (option, ("file", name)))
    new = tuple(name for _option, name in destinations if name != SEEDED)
    shown = " ".join((*mode, *(f"{option} <dir>/{name}"
                               for option, name in destinations)))
    return _case(
        f"{tool.removeprefix('ubertooth-')}-no-device{suffix}",
        f"{tool} {shown} with no board", tool,
        (
            _seed((SEEDED,)),
            _state_before(),
            tool_step("attempt", tool, words, patterns=(NO_BOARD_NOTE, NO_BOARD),
                      # The files are opened only once the board is, so a run
                      # that never opened one says nothing about them.
                      forbidden=tuple(re.escape(name) for name in new),
                      check=nothing_on_stdout),
            _state_after(),
        ),
        "exit 1 at the board lookup; " + ", ".join(new) + " not created; "
        f"the seeded {SEEDED} not truncated; nothing on stdout",
    )


def _stdout_capture_case() -> StateCase:
    """A capture bound for standard output, and a lookup that fails: the
    status is 1 and the failure is on standard error, so a reader of the
    capture gets nothing rather than an error line in it."""
    return _case(
        "rx-no-device-capture-on-stdout",
        "ubertooth-rx -q - with no board keeps its failure off the capture",
        "ubertooth-rx",
        (
            _seed((SEEDED,)),
            _state_before(),
            tool_step("attempt", "ubertooth-rx", ("-q", "-"),
                      patterns=(NO_BOARD_NOTE, NO_BOARD), forbidden=(),
                      check=nothing_on_stdout),
            _state_after(),
        ),
        "exit 1 with the note and the error on stderr; nothing at all on "
        "stdout, where the capture would have been; nothing created",
    )


# --- a replay: the capture files are created, then the run fails --------------


def _replay_step(source, words, patterns, *, forbidden=()) -> Step:
    return tool_step(
        "replay", "ubertooth-rx", ("-i", ("file", source)) + tuple(words),
        patterns=patterns,
        # A replay opens no board, so it must never reach the lookup, and no
        # case here expects a path it could not take back.
        forbidden=(NO_BOARD_NOTE, CANNOT_REMOVE, *forbidden))


def _replay_case(case_id, name, source, words, patterns, expected, *, steps=(),
                 allow=(), after=(), cleanup=(), state_name="") -> StateCase:
    return _case(
        case_id, name, "ubertooth-rx",
        (*steps, _state_before(),
         _replay_step(source, words, patterns),
         _state_after(allow=allow, name=state_name), *after),
        expected, cleanup=cleanup)


def capture_failure_cases(arguments, _resources, _resource_identities, *, now=None):
    """Host-only cases: no case opens a board, and none needs one."""
    base = rxreplay.fixture_base_time(now)
    kept = len(rxreplay.expected_packets("truncated"))
    cases = [
        # ubertooth-specan's -d with no board is Refused command lines
        # (specan-d-unopenable), so it is not repeated here.
        _no_device_case("ubertooth-rx",
                        (("-r", PCAPNG), ("-q", SEEDED), ("-d", DUMP))),
        _no_device_case("ubertooth-btle", (("-r", PCAPNG), ("-q", SEEDED)),
                        mode=("-n",)),
        _no_device_case("ubertooth-dump", (("-d", SEEDED),)),
        # No whole record read: every file created goes back.
        _replay_case(
            "replay-no-record-discards-files",
            "ubertooth-rx -i <no whole record> -r -q -d: the files created go back",
            FIXTURES["partial"], ALL_WRITERS, (_partial_record("partial"),),
            f"exit 1 naming the partial record; {PCAPNG}, {PCAP} and {DUMP} taken "
            f"back; {SEEDED} unchanged; no removal warning",
            steps=(_fixture("partial", base), _seed((SEEDED,)))),
        # The same failure after real packets: the captures hold evidence, so
        # they stay, and each one is a file a reader can open.
        _replay_case(
            "replay-partial-tail-keeps-files",
            "ubertooth-rx -i <whole records then a cut-short one>: the captures stay",
            FIXTURES["truncated"], ALL_WRITERS, (_partial_record("truncated"),),
            "exit 1 naming the partial record; all three files kept, each one "
            f"readable and holding the {kept} packets decoded before the failure",
            steps=(_fixture("truncated", base),), allow=(PCAPNG, PCAP, DUMP),
            state_name="The case directory gained only the three capture files",
            after=(_valid_capture("verify-pcapng", PCAPNG, "pcapng", kept),
                   _valid_capture("verify-pcap", PCAP, "pcap", kept),
                   _dump_is_the_replay("truncated"))),
        # A writer that cannot create its file: the reason is that create
        # call's, and nothing is left where the file would have been. The -d
        # writer in a missing directory is Raw receive replay's
        # replay-dump-unopenable; these are libbtbb's two writers.
        _replay_case(
            "replay-missing-directory",
            "ubertooth-rx -i <fixture> -r -q into a directory that is not there",
            FIXTURES["full"], ("-r", ("file", "gone/x.pcapng")),
            (_create_failure("gone/x.pcapng", "No such file or directory"),),
            "exit 1 with the create failure naming the file and its reason; "
            "nothing created, and no directory made",
            steps=(_fixture("full", base),)),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        # ubertooth-btle's -c is its own create call (libbtbb's PPI PCAP
        # writer), and -q and -c share one handle, so it gets a case of its
        # own rather than another option on the -q one.
        _no_device_case("ubertooth-btle", (("-c", PPI),), mode=("-n",),
                        suffix="-ppi"),
        _stdout_capture_case(),
        # The line the files are kept from is the first whole record, not the
        # first packet: this input holds one record the decoder skips, and
        # then one cut short, so a run that kept its files only once a packet
        # had been decoded would take these away.
        _replay_case(
            "replay-skipped-record-keeps-files",
            "ubertooth-rx -i <one record the decoder skips, then a cut-short one>",
            FIXTURES["skipped"], ALL_WRITERS, (_partial_record("skipped"),),
            "exit 1 naming the partial record; all three files kept, the two "
            "captures readable and holding no packet, and the dump empty, "
            "because the record the decoder skipped is written to none of them",
            steps=(_fixture("skipped", base),), allow=(PCAPNG, PCAP, DUMP),
            state_name="The case directory gained only the three capture files",
            after=(_valid_capture("verify-pcapng", PCAPNG, "pcapng", 0),
                   _valid_capture("verify-pcap", PCAP, "pcap", 0),
                   _dump_is_the_replay("skipped"))),
        _replay_case(
            "replay-missing-directory-pcap",
            "ubertooth-rx -i <fixture> -q into a directory that is not there",
            FIXTURES["full"], ("-q", ("file", "gone/x.pcap")),
            (_create_failure("gone/x.pcap", "No such file or directory"),),
            "exit 1 with the create failure naming the file and its reason; "
            "nothing created, and no directory made",
            steps=(_fixture("full", base),)),
        # A read that fails before any record: the files go back for a
        # different reason from the partial record above.
        _replay_case(
            "replay-read-error-discards-files",
            "ubertooth-rx -i <a directory> -r -q -d: the files created go back",
            "input.d", ALL_WRITERS,
            (r"(?m)^error: reading \S*input\.d failed: ",),
            f"exit 1 with the read error; {PCAPNG}, {PCAP} and {DUMP} taken back; "
            f"the input directory and {SEEDED} unchanged",
            steps=(_run("mkdir", "mkdir <dir>/input.d", "mkdir", ("file", "input.d")),
                   _seed((SEEDED,)))),
        # A destination the writer cannot make a file of: the refusal names
        # it, and what was in the way is not removed to make room.
        _replay_case(
            "replay-destination-is-a-directory",
            "ubertooth-rx -i <fixture> -q <a directory>",
            FIXTURES["full"], ("-q", ("file", "in-the-way")),
            (_create_failure("in-the-way", "Is a directory"),),
            "exit 1; the directory in the way is still there and still empty",
            steps=(_fixture("full", base),
                   _run("mkdir", "mkdir <dir>/in-the-way", "mkdir",
                        ("file", "in-the-way")))),
        _replay_case(
            "replay-read-only-directory",
            "ubertooth-rx -i <fixture> -q into a read-only directory",
            FIXTURES["full"], ("-q", ("file", "ro/x.pcap")),
            (_create_failure("ro/x.pcap", "Permission denied"),),
            "exit 1 naming ro/x.pcap and the permission; nothing created "
            "(skipped as root)",
            steps=(_fixture("full", base), _not_root(),
                   _run("mkdir", "mkdir <dir>/ro", "mkdir", ("file", "ro")),
                   _run("chmod-ro", "chmod 555 <dir>/ro", "chmod", "555",
                        ("file", "ro"))),
            cleanup=(_run("cleanup-chmod", "chmod 755 <dir>/ro", "chmod", "755",
                          ("file", "ro"), expected_returncodes=(0, 1)),)),
        # Paths the user arranged, which no run of ours takes away: the
        # capture goes through them, and they are still there afterwards.
        _replay_case(
            "replay-symlink-destination-kept",
            "ubertooth-rx -i <no whole record> -q <a symbolic link>",
            FIXTURES["partial"], ("-q", ("file", "link.pcap")),
            (_partial_record("partial"),),
            "exit 1; link.pcap is still there and still names target.pcap, "
            "which holds what the writer wrote through it; no removal warning",
            steps=(_fixture("partial", base), _seed(("target.pcap",)),
                   _run("symlink", "ln -s target.pcap <dir>/link.pcap", "ln", "-s",
                        "target.pcap", ("file", "link.pcap"))),
            allow=("link.pcap", "target.pcap"),
            state_name="Nothing but link.pcap and the target it names changed",
            after=(_symlink("link.pcap", "target.pcap"),
                   _valid_capture("verify-target", "target.pcap", "pcap", 0))),
        _replay_case(
            "replay-devnull-destination-kept",
            "ubertooth-rx -i <no whole record> -q /dev/null",
            FIXTURES["partial"], ("-q", "/dev/null"),
            (_partial_record("partial"),),
            "exit 1; /dev/null is no file to take back, so nothing is removed "
            "and no warning is printed (skipped as root)",
            # As root a wrong removal would succeed in silence, and take
            # /dev/null with it, so the case does not run there.
            steps=(_fixture("partial", base), _not_root())),
    ]
    return tuple(cases)
