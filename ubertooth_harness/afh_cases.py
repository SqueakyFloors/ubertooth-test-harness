"""Recover AFH map: ubertooth-afh against a piconet held to a known map.

The two ESP32 Classic peers make a Classic Bluetooth piconet whose Adaptive
Frequency Hopping map is chosen rather than discovered: the initiator leads
the piconet, and ``afh`` tells its controller which channels to keep
(firmware/esp32-classic-peer, "The channel map"). So the air carries a map
this test knows, and ``ubertooth-afh`` can be asked what it makes of it.

Three things make the comparison honest, and the cases do all three.

**The map in force is not the map asked for.** These controllers drop
channels 0, 24, and 78 of their own accord, so a board told to keep channels
0 to 39 leads a link hopping over 38 of them. The driver reads the live
link's map from the leader's controller and the comparison uses that;
``esp32peer.py`` refuses a link whose map is not a subset of the requested
one, because that is not the piconet this case set up.

**The map has to hold still.** A leader may classify channels again while
the link is up, and then no one map describes the capture. The driver reads
the link's map at the start, the middle, and the end of the capture, and a
reading that differs from the others fails the case with all three printed.

**Access-code errors have to be off.** ``ubertooth-afh`` marks a channel used
when it finds the piconet's access code on it, and with the tool's default of
two allowed bit errors the bench saw it mark the whole band used: ambient
traffic matches 64 bits within two often enough. With ``-e 0`` the same bench
found every kept channel inside a minute. So these cases run ``-e 0``, which
is the evidence rather than the default.

What is left is the channel edge, which is physics rather than software. A
receiver a few tens of centimeters from the piconet reads enough energy from
the channels beside the kept block to decode packets there, so a recovered
map is wider than the one in force, by about eight channels of skirt on this
bench. ``afhmap.py`` therefore judges two measures with documented
tolerances: every channel in force must be found, and the share of the
channels out of force that are marked used must stay under a ceiling. An
attenuated or more distant receiver sharpens the edge, which is why the plan
may place this test on one.

Each case is one ``esp32peer.py stream`` session: it holds both bridges,
sets the map, brings the link up, runs the tool for the capture window, and
saves the tool's output beside its own JSON report of the maps. A second step
reads that pair of files and judges the recovery. Keeping it a step of its own
is what puts the whole measurement in the run record as output with patterns
over it, and leaves the two files it read in the case's directory, so the
comparison can be run again by hand against exactly what the capture saw.
"""

from __future__ import annotations

from pathlib import Path
import sys

from ubertooth_harness import afhmap, esp32peer
from ubertooth_harness.classic_cases import (
    LINUX_ONLY,
    MIN_WRITES,
    PAIR_SETUP_S,
    STREAM_PAYLOAD,
    STREAM_PERIOD_MS,
    _pair,
    _pair_patterns,
    _reset_step,
    ACCEPTOR_ROLE,
    INITIATOR_ROLE,
)
from ubertooth_harness.receive_cases import EARLY_EXIT_S, EXIT_SLACK_S, seconds_arg
from ubertooth_harness.stateful import (
    ATTEMPT_KEY,
    CAPTURE_DIR_KEY,
    StateCase,
    Step,
    require,
)
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS,
    _cleanup,
    _read_originals,
    _serial,
    _stop,
)

TEST_NAME = "Recover AFH map"

# The two maps the cases put on the air, and what each is for.
#
# `half-band` keeps channels 0 to 39: a wide reduction whose edge sits in the
# middle of the band, where a recovered map that ignored the map altogether
# is obvious. `minimum` keeps channels 30 to 49, the twenty the spec allows
# at least (Core 6.0 Vol 4 Part E 7.3.46, and N_min in Vol 2 Part B 2.6.3)
# and the firmware's own floor, which is the edge case: below it a piconet
# falls back to hopping over everything.
HALF_BAND_MAP = afhmap.hex_from_channels(range(0, 40))
MINIMUM_MAP = afhmap.hex_from_channels(range(30, 50))

# Access-code errors the cases allow. Zero: see the module docstring.
AC_ERRORS = 0

# The tolerances, with the bench measurement behind each. A plan does not set
# them; they belong to what the test means, and docs/tests.md carries the
# numbers.
#
# At least 90% of the channels in force must be found. Measured on the bench
# over 120 s windows at about 30 cm, with -e 0: the 40-channel map's link
# used 38 channels and the tool found 37, and the 20-channel map's used 20
# and it found 19, each missing a different single channel; the detection
# phase found all 38. Finding a channel means the sweep and the hop sequence
# met on it, so the last one or two are a matter of luck in a window this
# long: the per-minute series of the same runs went 31 of 38 after a minute
# to 37 after two. 90% leaves room for one more than was measured (3 of 38,
# 2 of 20) and still fails a recovery that stopped working.
MIN_FOUND_SHARE = 0.90
# Up to 70% of the channels out of force may be marked used by the -r cases.
# The same two runs marked 20 of 41 (49%) and 13 of 59 (22%), and an earlier
# three-minute run 21 of 41 (51%). The ceiling is what separates a recovery
# from a tool marking everything used, not a measure of the skirt.
MAX_STRAY_SHARE = 0.70
# The detection phase's map is not held to that, and 1.0 is how the checker
# is told to report the share without judging it. Two things make its map
# wider than -r's, both by design: it fills a single channel gap between two
# channels it has seen, and it never removes a channel once seen, so its map
# is an upper bound that grows with the capture. The bench's own -t 60 map
# marked 29 of 41 channels out of force used, 71%, which is already past the
# -r ceiling, and a longer window marks more. So the -r cases are where
# selectivity is measured, and this case measures that the detection phase
# finds the channels in use at all.
DETECTION_MAX_STRAY_SHARE = 1.0
# Channels that must be out of force for the stray share to be judged at
# all. A map that keeps nearly the whole band leaves too few for a share to
# mean anything; both maps here leave 39 or more.
MIN_DROPPED_CHANNELS = afhmap.DEFAULT_MIN_DROPPED_CHANNELS

# The capture is stopped by the driver, because ubertooth-afh keeps watching
# for map changes once it has a map and stops only when it is signalled. The
# detection-phase case gets its -t window plus this, so the map is printed
# before the interrupt arrives.
DETECT_GRACE_S = 5.0

_ESP32PEER = str(Path(esp32peer.__file__).resolve())
_AFHMAP = str(Path(afhmap.__file__).resolve())
# The tool's two forms of output, which afhmap.py reads and these patterns
# only have to find one line of.
MAP_LINE = r"(?m)^AFH map: 0x[0-9a-f]{20}$"
R_LINE = r"(?m)^\d+ [01]{80}$"
CHECK_TIMEOUT_S = 60.0
MAPS_FILE = "afh-maps.txt"
REPORT_FILE = "afh-peer-report.json"


def _attempt_path(name: str):
    """The file of the attempt that is running.

    Each attempt keeps its own pair of files (``afh-maps-1.txt`` and
    ``afh-peer-report-1.json``, then ``-2``), as the BLE connection cases
    keep a capture per attempt, so the run directory holds what every try
    measured rather than only the last.
    """
    stem, _, extension = name.rpartition(".")

    def path(context):
        attempt = context.get(ATTEMPT_KEY, "1")
        return str(Path(require(context, CAPTURE_DIR_KEY))
                   / f"{stem}-{attempt}.{extension}")

    return path


def _afh_argv(acceptor, initiator, serial, lap, uap, seconds, wanted, options,
              interrupt_after):
    """esp32peer.py stream --map ... -- ubertooth-afh -e 0 ...

    The driver holds both peers for the whole session and runs the tool
    itself, for the reason classic_cases.py gives: opening a bridge resets
    the board, so a case cannot set the pair up in one step and capture in
    the next.
    """
    def argv(context):
        return (
            sys.executable, _ESP32PEER, "stream",
            "--acceptor", acceptor["device"],
            "--acceptor-address", acceptor["address"],
            "--acceptor-alias", acceptor["alias"],
            "--initiator", initiator["device"],
            "--initiator-address", initiator["address"],
            "--initiator-alias", initiator["alias"],
            "--period", str(STREAM_PERIOD_MS),
            "--payload", str(STREAM_PAYLOAD),
            "--min-writes", str(MIN_WRITES),
            "--min-seconds", str(seconds - EARLY_EXIT_S),
            "--timeout", str(interrupt_after + EXIT_SLACK_S),
            "--map", wanted,
            "--map-report", _attempt_path(REPORT_FILE)(context),
            "--command-output", _attempt_path(MAPS_FILE)(context),
            "--interrupt-after", str(interrupt_after),
            "--", "ubertooth-afh", "-U", serial, "-l", lap, "-u", uap,
            "-e", str(AC_ERRORS),
        ) + tuple(options)
    return argv


def _map_patterns(wanted: str) -> tuple[str, ...]:
    """What the driver must report for the capture to mean anything."""
    used = len(afhmap.channels_from_hex(wanted, "the requested map"))
    return (
        rf"(?m)^channel map requested: {wanted} used {used}$",
        # Every sample is a map of its own line, and the summary says whether
        # the three agreed; the comparison refuses a map that changed.
        r"(?m)^channel map in force at the start \(0\.0 s\): [0-9a-f]{20} used \d+$",
        r"(?m)^channel map in force at the middle \([0-9.]+ s\): [0-9a-f]{20} "
        r"used \d+$",
        r"(?m)^channel map in force at the end \([0-9.]+ s\): [0-9a-f]{20} "
        r"used \d+$",
        r"(?m)^channel map in force: stable$",
    )


def _found_pattern() -> str:
    """The found line, as strictly as the tolerance allows.

    The checker's exit status already carries the verdict; this is the line a
    reader (and `ubertooth-harness validate`) sees. With every in-force
    channel demanded, the two counts have to be the same number, which the
    pattern can say; with a share below one they need not be.
    """
    if MIN_FOUND_SHARE >= 1.0:
        return r"(?m)^in-force channels found: (\d+) of \1 \(100%\)$"
    return r"(?m)^in-force channels found: \d+ of \d+ \(\d+%\)$"


def _judge_step(wanted: str, max_stray_share: float) -> Step:
    """Compare the recovered map with the one the link was hopping over."""
    return Step(
        "judge-map",
        f"The recovered map covers the channels in force for {wanted}",
        lambda context: (
            sys.executable, _AFHMAP,
            "--peer-report", _attempt_path(REPORT_FILE)(context),
            "--maps", _attempt_path(MAPS_FILE)(context),
            "--min-found-share", str(MIN_FOUND_SHARE),
            "--max-stray-share", str(max_stray_share),
            # Named rather than left to the checker's default, so the run
            # record says which floor decided whether the share was judged.
            "--min-dropped-channels", str(MIN_DROPPED_CHANNELS),
        ),
        timeout_s=CHECK_TIMEOUT_S,
        output_patterns=(
            rf"(?m)^requested map: {wanted} used \d+ ",
            r"(?m)^in-force map: [0-9a-f]{20} used \d+ ",
            r"(?m)^recovered map: [0-9a-f]{20} used \d+ ",
            _found_pattern(),
            r"(?m)^stray channels marked used: \d+ of \d+ out of force ",
        ) + ((r"(?m)^stray share: not judged, this test allows every channel ",)
             if max_stray_share >= 1.0 else ()) + (
            r"(?m)^afh recovery: ok$",
        ),
    )


def _map_case(serial, acceptor, initiator, seconds, *, case_id, name, wanted,
              options, tool_line, expected_state, tool_pattern,
              interrupt_after, max_stray_share=MAX_STRAY_SHARE):
    """One capture on one map, then the comparison, with one retry.

    The capture and the comparison are one retried group: whether a channel
    was heard depends on where the sweep and the hop sequence met, the peers
    keep making the traffic either way, and a second window costs only its
    own time. Anything before the capture failing, or the cleanup failing,
    ends the case as it always did.
    """
    lap, uap = initiator["lap"], initiator["uap"]
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("stream-and-recover",
             f"The pair holds the piconet to {wanted} while {tool_line}",
             _afh_argv(acceptor, initiator, serial, lap, uap, seconds, wanted,
                       options, interrupt_after),
             timeout_s=PAIR_SETUP_S + interrupt_after + EXIT_SLACK_S,
             mutates=True,
             output_patterns=_pair_patterns(acceptor, initiator)
             + _map_patterns(wanted)
             + (tool_pattern, r"(?m)^command interrupted after [0-9.]+ s$")),
        _judge_step(wanted, max_stray_share),
        _stop(serial),
    )
    return StateCase(
        case_id, name, steps,
        (_reset_step("reset-initiator", initiator, INITIATOR_ROLE),
         _reset_step("reset-acceptor", acceptor, ACCEPTOR_ROLE))
        + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=(expected_state + "; both peers reset to their boot state; "
                        "channel, squelch, and LEDs restored"),
        capture_dir=True,
        needs=LINUX_ONLY,
        retry_steps=("stream-and-recover", "judge-map"),
        attempts=2,
    )


def afh_map_cases(arguments, resources, resource_identities):
    """Recover AFH map: a reduced map on the air, and the map recovered from it."""
    serial = _serial(resource_identities, TEST_NAME)
    acceptor, initiator = _pair(resource_identities, TEST_NAME, resources)
    seconds = seconds_arg(arguments)

    def case(**options):
        return _map_case(serial, acceptor, initiator, seconds, **options)

    recovered = (f"the recovered map holds every channel the link was hopping "
                 f"over, and at most {MAX_STRAY_SHARE:.0%} of the channels out "
                 f"of force")
    def every_second(**options):
        return case(options=("-r",), tool_pattern=R_LINE,
                    interrupt_after=float(seconds),
                    tool_line=f"ubertooth-afh -e {AC_ERRORS} -r runs for "
                              f"{seconds} s",
                    expected_state=f"one map line a second for about {seconds} s; "
                                   + recovered,
                    **options)

    cases = [
        every_second(case_id="half-band",
                     name=f"ubertooth-afh -e {AC_ERRORS} -r recovers the "
                          "40-channel map the piconet is held to",
                     wanted=HALF_BAND_MAP),
        every_second(case_id="minimum-channels",
                     name=f"ubertooth-afh -e {AC_ERRORS} -r recovers the "
                          "20-channel minimum map",
                     wanted=MINIMUM_MAP),
    ]
    if arguments["coverage"] == "comprehensive":
        # The other path through the tool: -t detects the map for that long
        # and prints it, then watches for changes until it is signalled. It
        # also fills a single channel gap between two it has seen, which -r
        # does not, so the two are different measurements of one piconet.
        cases.append(case(
            case_id="half-band-detection",
            name=f"ubertooth-afh -e {AC_ERRORS} -t {seconds} prints the "
                 "40-channel map it detected",
            wanted=HALF_BAND_MAP, options=("-t", str(seconds)),
            tool_pattern=MAP_LINE,
            # The detection phase ends at -t, and the map is printed then, so
            # the interrupt waits past it; the tool watches for changes until
            # it arrives.
            interrupt_after=seconds + DETECT_GRACE_S,
            tool_line=f"ubertooth-afh -e {AC_ERRORS} -t {seconds} detects the map",
            max_stray_share=DETECTION_MAX_STRAY_SHARE,
            expected_state=f"an AFH map line after about {seconds} s; every "
                           "channel the link was hopping over in the map it "
                           "printed, with the stray channels reported and not "
                           "judged"))
    return tuple(cases)
