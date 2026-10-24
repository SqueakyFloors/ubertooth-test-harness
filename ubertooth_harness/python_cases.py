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
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from ubertooth_harness import pytools
from ubertooth_harness.cases import CommandCase, guard_roles
from ubertooth_harness.stateful import StateCaseError

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
