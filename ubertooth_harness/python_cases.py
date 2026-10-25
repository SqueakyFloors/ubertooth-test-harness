"""Cases for the fork's Python tools: ubertooth-specan-ui and btle-extcap.

Contracts come from the fork sources: host/python/specan_ui/
ubertooth-specan-ui.in (argparse after the numpy and qtpy imports) and
host/python/extcap/btle-extcap.py at 71e1a8a (getopt; interfaces are
ubertooth<serial>, as --extcap-interfaces lists them from
"ubertooth-util -U all -s", and every operation but --extcap-interfaces needs
one; a usage error is "error: ..." plus the usage line on stderr, exit 1).
The commands run through pytools.py, which finds the run's tool and
interpreter. The ubertooth-specan-ui cases need the Python tools build
(hostneeds.py), so a run without ``--build --with-python`` leaves them out as
not applicable; pytools.py still skips one at run time when the interpreter
cannot import numpy, qtpy and a Qt binding, which only running it can tell.

Comprehensive cases also check behavior the fork gets wrong at 71e1a8a, so
they fail there:

- SqueakyFloors/ubertooth#334: btle-extcap --capture exits 0 when the
  ubertooth-btle it runs fails (no board with the serial, --channel=40).

BLE extcap capture runs --capture through extcapcapture.py, which reads the
FIFO as Wireshark would and stops the script with SIGTERM.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from ubertooth_harness import extcapcapture, pytools
from ubertooth_harness.cases import CommandCase, guard_roles
from ubertooth_harness.hostneeds import NO_NEEDS, Needs
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
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
        # ubertooth-specan-ui exists only in the Python tools build; a run
        # without one leaves the case out as not applicable.
        needs=Needs(python_tools=True) if command == "specan-ui" else NO_NEEDS,
        skip_returncode=pytools.SKIP_EXIT,
    )


def _line(text: str) -> str:
    return rf"(?m)^{re.escape(text)}$"


# The interface refusal: the message, exit 1, no listing and no crash (an
# uncaught exception also exits 1).
_INTERFACE_REFUSAL = (
    r"(?m)^error: --extcap-interface must be ubertooth<serial>, as "
    r"--extcap-interfaces lists it$",
    r"\A(?![\s\S]*Traceback)(?![\s\S]*(?m:^(?:dlt|arg|value) \{))",
)
_USAGE = r"(?m)^Usage: \S*btle-extcap\S* --extcap-interfaces \| "

# --extcap-dlts and --extcap-config open no board, so any serial will do.
_INTERFACE = "--extcap-interface=ubertooth" + "0123456789abcdef" * 2
_DLT_LINE = _line(f"dlt {{number={EXTCAP_DLT}}}"
                  "{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}{display=Bluetooth LE}")


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
            "extcap-help", "btle-extcap -h names its real options", "extcap", ("-h",),
            (_USAGE, *(re.escape(option) for option in (
                "--extcap-interface=ubertooth<serial>", "--extcap-dlts",
                "--extcap-config", "--capture --fifo=PATH", "--channel=37|38|39",
            ))),
        ),
        _case(
            "extcap-dlts", f"btle-extcap --extcap-dlts lists DLT {EXTCAP_DLT}",
            "extcap", (_INTERFACE, "--extcap-dlts"), (_DLT_LINE,),
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
        # An older ubertooth<index> still works (ubertooth-btle -U reads an
        # all-digit value as an index).
        _case(
            "extcap-dlts-index-interface", "btle-extcap --extcap-dlts with ubertooth0",
            "extcap", ("--extcap-interface=ubertooth0", "--extcap-dlts"), (_DLT_LINE,),
        ),
        # "every operation down here depends on having an interface": exit 1.
        _case(
            "extcap-dlts-no-interface", "btle-extcap --extcap-dlts without an interface",
            "extcap", ("--extcap-dlts",), _INTERFACE_REFUSAL, returncodes=(1,),
        ),
        _case(
            "extcap-config-bad-interface",
            "btle-extcap --extcap-config with a non-Ubertooth interface",
            "extcap", ("--extcap-interface=wlan0", "--extcap-config"), _INTERFACE_REFUSAL,
            returncodes=(1,),
        ),
        # Refused before ubertooth-btle runs, so no board is opened.
        _case(
            "extcap-capture-no-fifo", "btle-extcap --capture without --fifo",
            "extcap", (_INTERFACE, "--capture"),
            (r"(?m)^error: --capture needs --fifo=PATH$", r"\A(?![\s\S]*Traceback)"),
            returncodes=(1,),
        ),
        _case(
            "extcap-unknown-option", "btle-extcap rejects an unknown option",
            "extcap", ("--extcap-bogus",),
            (r"(?m)^error: option --extcap-bogus not recognized$", _USAGE),
            returncodes=(1,),
        ),
    ]
    return tuple(cases)


def extcap_interface_cases(arguments, _resources, resource_identities):
    """--extcap-interfaces lists every attached board (via ubertooth-util
    -U all -s) by serial: the selected one plus every other registered board,
    bound as guards."""
    roles = ("Ubertooth",) + guard_roles(resource_identities)
    serials = []
    for role in roles:
        serial = resource_identities.get(role, {}).get("serial")
        if not serial:
            raise StateCaseError(f"BLE extcap interfaces: {role} has no registered serial")
        serials.append(serial.lower())
    count = len(serials)
    # Listing order is the USB index order, which the registry does not give.
    listing = r"(?i)\A" + "".join(
        rf"interface \{{value=ubertooth(?P<s{index}>[0-9a-f]+)\}}"
        rf"\{{display=Ubertooth One (?P=s{index})\}}\n"
        for index in range(count)
    ) + r"(?!interface)"
    return (
        _case(
            "extcap-interfaces", f"btle-extcap --extcap-interfaces lists {count} board(s)",
            "extcap", ("--extcap-interfaces",), (listing,),
        ),
        _case(
            "extcap-interfaces-serials",
            "btle-extcap --extcap-interfaces shows each board's serial",
            "extcap", ("--extcap-interfaces",),
            tuple(
                rf"(?mi)^interface \{{value=ubertooth{serial}\}}"
                rf"\{{display=Ubertooth One {serial}\}}$"
                for serial in serials
            ),
        ),
    )


# ---------------------------------------------------------------------------
# btle-extcap --capture (BLE extcap capture)
#
# capture() runs "ubertooth-btle -f -U <serial> -q <fifo> -A <channel>" with
# the serial from --extcap-interface=ubertooth<serial> and --channel (default
# 37). extcapcapture.py reads the FIFO as Wireshark would, stops the script
# with SIGTERM, and checks the PCAP stream.

_EXTCAP_CAPTURE = str(Path(extcapcapture.__file__).resolve())
_FIFO = "extcap.fifo"
# A serial no board has. It has hex letters, because ubertooth-btle -U reads
# an all-digit value as an index.
_NO_BOARD_SERIAL = "0badc0de" * 4
# Fetching the script and stopping it may take this long beyond the capture.
_CAPTURE_SLACK_S = 45.0


def _fifo(context):
    return str(Path(require(context, CAPTURE_DIR_KEY)) / _FIFO)


def _capture_case(serial, seconds, case_id, name, expected_state, *, channel=None,
                  interface_serial=None, refused=False):
    """The --extcap-interface is ubertooth<interface_serial>, by default the
    selected board's serial."""
    def argv(context):
        command = [sys.executable, _EXTCAP_CAPTURE, "--seconds", str(seconds),
                   "--fifo", _fifo(context)]
        if refused:
            command.append("--refused")
        else:
            command += ["--board", serial, "--channel", str(channel or 37)]
        command += ["--", "--capture",
                    f"--extcap-interface=ubertooth{interface_serial or serial}",
                    f"--fifo={_fifo(context)}"]
        if channel is not None:
            command.append(f"--channel={channel}")
        return command

    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
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
    """btle-extcap --capture on the selected board, chosen by its serial; the
    other registered boards are guards."""
    serial = _serial(resource_identities, "BLE extcap capture").lower()
    registered = {serial} | {
        str(resource_identities.get(role, {}).get("serial", "")).lower()
        for role in guard_roles(resource_identities)
    }
    if _NO_BOARD_SERIAL in registered:
        raise StateCaseError(f"BLE extcap capture: serial {_NO_BOARD_SERIAL} is registered")
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
    # Strict: btle-extcap exits 0 when ubertooth-btle fails, at fork 71e1a8a
    # (SqueakyFloors/ubertooth#334).
    cases += [
        _capture_case(serial, seconds, "extcap-capture-no-board",
                      "btle-extcap --capture with a serial no board has",
                      "the script ends with a non-zero exit, no packet records",
                      interface_serial=_NO_BOARD_SERIAL, refused=True),
        _capture_case(serial, seconds, "extcap-capture-bad-channel",
                      "btle-extcap --capture --channel=40",
                      "the script ends with a non-zero exit, no packet records",
                      channel=40, refused=True),
    ]
    return tuple(cases)
