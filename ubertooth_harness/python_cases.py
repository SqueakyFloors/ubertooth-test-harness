"""Cases for the fork's Python tools: ubertooth-specan-ui and btle-extcap.

Contracts come from the fork sources: host/python/specan_ui/
ubertooth-specan-ui.in (argparse after the numpy and qtpy imports) and
host/python/extcap/btle-extcap.py (getopt; every operation but
--extcap-interfaces needs --extcap-interface=ubertooth<N>). The commands run
through pytools.py, which finds the run's tool and interpreter and skips a
case that cannot run on this host.

Comprehensive cases also check behavior the fork gets wrong at 6fee30c, so
they fail there:

- SqueakyFloors/ubertooth#268: btle-extcap -h names options that do not
  exist (--list-interfaces, --list-dlts, --config).
- SqueakyFloors/ubertooth#269: with two or more boards, btle-extcap
  --extcap-interfaces shows the listing index ("0:") instead of the serial.
- SqueakyFloors/ubertooth#334: btle-extcap --capture exits 0 when the
  ubertooth-btle it runs fails (no board at the index, --channel=40).

BLE extcap capture runs --capture through extcapcapture.py, which reads the
FIFO as Wireshark would and stops the script with SIGTERM.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from ubertooth_harness import extcapcapture, pytools
from ubertooth_harness.cases import CommandCase, guard_roles
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    StateCaseError,
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

_PYTOOLS = str(Path(pytools.__file__).resolve())
# pytools.py runs the probe and the tool with its own 20 s limits.
_TIMEOUT_S = 50.0
EXTCAP_DLT = 256


def _case(case_id, name, command, args, patterns, returncodes=(0,)):
    return CommandCase(
        case_id, name, (sys.executable, _PYTOOLS, command, *args),
        tuple(patterns), _TIMEOUT_S, expected_returncodes=tuple(returncodes),
        skip_returncode=pytools.SKIP_EXIT,
    )


def _line(text: str) -> str:
    return rf"(?m)^{re.escape(text)}$"


# The script's silent exit 1 for a missing or non-Ubertooth interface, not a
# crash (an uncaught exception also exits 1) and no listing.
_SILENT_REFUSAL = (r"\A(?![\s\S]*Traceback)(?![\s\S]*(?m:^(?:dlt|arg|value) \{))",)


_INTERFACE = "--extcap-interface=ubertooth0"


def python_tool_cases(arguments, _resources, _resource_identities):
    """Host-only cases: no Ubertooth is opened."""
    cases = [
        _case(
            "specan-ui-help", "ubertooth-specan-ui -h without a display",
            "specan-ui", ("-h",),
            (r"(?m)^usage: ubertooth-specan-ui ", r"-U index\|serial",
             r"-l LOWER_FREQ", r"-u UPPER_FREQ", r"--wifi \[channel\(s\)\]",
             r"--padding PADDING"),
        ),
        _case(
            "extcap-help", "btle-extcap -h", "extcap", ("-h",),
            (r"(?m)^Usage: ",),
        ),
        _case(
            "extcap-dlts", f"btle-extcap --extcap-dlts lists DLT {EXTCAP_DLT}",
            "extcap", (_INTERFACE, "--extcap-dlts"),
            (_line(f"dlt {{number={EXTCAP_DLT}}}"
                   "{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}{display=Bluetooth LE}"),),
        ),
        _case(
            "extcap-config", "btle-extcap --extcap-config: advertising channel 37-39",
            "extcap", (_INTERFACE, "--extcap-config"),
            (
                _line("arg {number=0}{call=--channel}"
                      "{display=Advertising Channel}{type=selector}"),
                _line("value {arg=0}{value=37}{display=37}{default=true}"),
                _line("value {arg=0}{value=38}{display=38}{default=false}"),
                _line("value {arg=0}{value=39}{display=39}{default=false}"),
            ),
        ),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        # Strict: fails at 6fee30c (SqueakyFloors/ubertooth#268).
        _case(
            "extcap-help-options", "btle-extcap -h names its real options",
            "extcap", ("-h",),
            tuple(re.escape(option) for option in (
                "--extcap-interfaces", "--extcap-dlts", "--extcap-config",
                "--capture",
            )),
        ),
        # "every operation down here depends on having an interface": exit 1.
        _case(
            "extcap-dlts-no-interface", "btle-extcap --extcap-dlts without an interface",
            "extcap", ("--extcap-dlts",), _SILENT_REFUSAL, returncodes=(1,),
        ),
        _case(
            "extcap-config-bad-interface",
            "btle-extcap --extcap-config with a non-Ubertooth interface",
            "extcap", ("--extcap-interface=wlan0", "--extcap-config"), _SILENT_REFUSAL,
            returncodes=(1,),
        ),
        _case(
            "extcap-unknown-option", "btle-extcap rejects an unknown option",
            "extcap", ("--extcap-bogus",),
            (r"option --extcap-bogus not recognized", r"(?m)^Usage: "),
            returncodes=(2,),
        ),
    ]
    return tuple(cases)


def extcap_interface_cases(arguments, _resources, resource_identities):
    """--extcap-interfaces lists every attached board (via ubertooth-util -s):
    the selected one plus every other registered board, bound as guards."""
    roles = ("Ubertooth",) + guard_roles(resource_identities)
    serials = []
    for role in roles:
        serial = resource_identities.get(role, {}).get("serial")
        if not serial:
            raise StateCaseError(f"BLE extcap interfaces: {role} has no registered serial")
        serials.append(serial.lower())
    count = len(serials)
    listing = r"\A" + "".join(
        rf"interface \{{value=ubertooth{index}\}}\{{display=Ubertooth One [^}}\n]+\}}\n"
        for index in range(count)
    ) + r"(?!interface)"
    cases = [
        _case(
            "extcap-interfaces", f"btle-extcap --extcap-interfaces lists {count} board(s)",
            "extcap", ("--extcap-interfaces",), (listing,),
        ),
    ]
    if arguments["coverage"] == "comprehensive":
        # Strict: fails with two or more boards at 6fee30c
        # (SqueakyFloors/ubertooth#269).
        cases.append(
            _case(
                "extcap-interfaces-serials",
                "btle-extcap --extcap-interfaces shows each board's serial",
                "extcap", ("--extcap-interfaces",),
                tuple(
                    rf"(?m)^interface \{{value=ubertooth\d+\}}"
                    rf"\{{display=Ubertooth One {serial}\}}$"
                    for serial in serials
                ),
            )
        )
    return tuple(cases)


# ---------------------------------------------------------------------------
# btle-extcap --capture (BLE extcap capture)
#
# capture() runs "ubertooth-btle -f -U<N> -q <fifo> -A <channel>" with N from
# --extcap-interface=ubertooth<N> (an index into the ubertooth-util -s list)
# and --channel (default 37). extcapcapture.py reads the FIFO as Wireshark
# would, stops the script with SIGTERM, and checks the PCAP stream.

_EXTCAP_CAPTURE = str(Path(extcapcapture.__file__).resolve())
_INDEX_KEY = "extcap index"
_FIFO = "extcap.fifo"
# An index no attached board has: boards that are attached but not registered
# also take indexes, so the registered count cannot be used.
_NO_BOARD_INDEX = 99
# Fetching the script and stopping it may take this long beyond the capture.
_CAPTURE_SLACK_S = 45.0


def _index_step(serial):
    """The selected board's index in the ubertooth-util -s list: "N: serial"
    with several boards, "Serial No: serial" (index 0) with one."""
    return Step(
        "board-index", "Read the board's index (ubertooth-util -s)",
        ("ubertooth-util", "-s"),
        observe=Observation(
            _INDEX_KEY,
            rf"(?mi)^(?:(?P<index>\d+): |Serial No:\s+){re.escape(serial)}\s*$",
            convert=lambda match: match["index"] or "0",
        ),
    )


def _fifo(context):
    return str(Path(require(context, CAPTURE_DIR_KEY)) / _FIFO)


def _capture_case(serial, seconds, case_id, name, expected_state, *, channel=None,
                  interface=None, refused=False):
    """interface(context) -> the --extcap-interface value; by default the
    selected board's ubertooth<N>."""
    def argv(context):
        index = require(context, _INDEX_KEY)
        command = [sys.executable, _EXTCAP_CAPTURE, "--seconds", str(seconds),
                   "--fifo", _fifo(context)]
        if refused:
            command.append("--refused")
        else:
            command += ["--board", index, "--channel", str(channel or 37)]
        value = interface(context) if interface else f"ubertooth{index}"
        command += ["--", "--capture", f"--extcap-interface={value}",
                    f"--fifo={_fifo(context)}"]
        if channel is not None:
            command.append(f"--channel={channel}")
        return command

    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        _index_step(serial),
        Step("capture", name, argv, timeout_s=seconds + _CAPTURE_SLACK_S,
             mutates=True, output_patterns=(r"(?m)^capture checks passed$",)),
        _stop(serial),
    )
    return StateCase(
        case_id, name, steps, _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=expected_state + "; channel, squelch, and LEDs restored",
        capture_dir=True,
    )


def extcap_capture_cases(arguments, _resources, resource_identities):
    """btle-extcap --capture on the selected board; the other registered
    boards are guards (ubertooth-util -s reads every one)."""
    serial = _serial(resource_identities, "BLE extcap capture")
    seconds = seconds_arg(arguments)
    stream = "a DLT 256 PCAP stream on the FIFO until SIGTERM, no process left"
    cases = [
        _capture_case(serial, seconds, "extcap-capture",
                      "btle-extcap --capture (default channel 37)",
                      stream + ", advertising channel 37"),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        _capture_case(serial, seconds, f"extcap-capture-{channel}",
                      f"btle-extcap --capture --channel={channel}",
                      stream + f", advertising channel {channel}", channel=channel)
        for channel in (37, 38, 39)
    ]
    # Strict: btle-extcap exits 0 when ubertooth-btle fails, at fork 89d9197
    # (SqueakyFloors/ubertooth#334).
    cases += [
        _capture_case(serial, seconds, "extcap-capture-no-board",
                      "btle-extcap --capture with no board at the index",
                      "the script ends with a non-zero exit, no packet records",
                      interface=lambda _context: f"ubertooth{_NO_BOARD_INDEX}",
                      refused=True),
        _capture_case(serial, seconds, "extcap-capture-bad-channel",
                      "btle-extcap --capture --channel=40",
                      "the script ends with a non-zero exit, no packet records",
                      channel=40, refused=True),
    ]
    return tuple(cases)
