"""BLE extcap in tshark: real Wireshark decodes what the plugin sends.

The other extcap cases (``python_cases.py`` with ``extcapcapture.py``) play
Wireshark's part themselves and stay the main coverage: they control exactly
which arguments are sent, when the capture is stopped, and they check the PCAP
stream with the harness's own parser. This test adds the one thing they cannot
show, that a real Wireshark agrees (harness issue #148): tshark installs the
plugin itself, calls it the way that Wireshark version does, and its own
dissector reads the records (link type 256 and libbtbb's LE pseudo-header).
A known advertiser makes the answer exact: an nRF52840 DK advertises from its
registered address, so the row Wireshark must print is known before the
capture starts.

Each case sets the DK advertising on one channel, runs tshark through
``tsharkcapture.py`` for the plan's duration, and stops both. The cases are
skipped where tshark is not installed, which only running it can tell.
"""

from __future__ import annotations

from pathlib import Path
import sys

from ubertooth_harness import nrfdk, pytools, tsharkcapture
from ubertooth_harness.advertiser_cases import (
    CHANNELS, DK_TIMEOUT_S, _advertiser, _dk, _dk_idle, _ensure_case,
)
from ubertooth_harness.receive_cases import seconds_arg
from ubertooth_harness.stateful import StateCase, Step
from ubertooth_harness.transitions import (
    MODE_RESET_SETTINGS, _cleanup, _read_originals, _serial, _stop,
)

TEST_NAME = "BLE extcap in tshark"
_TSHARK_CAPTURE = str(Path(tsharkcapture.__file__).resolve())
# The channel the DK advertises on, and tshark captures on, in every case:
# which channel reaches the board is already covered by BLE extcap capture.
CHANNEL = CHANNELS[0]
ADVERTISING_INTERVAL_MS = 100
PAYLOAD_TAG = "c0ffee"
# Fetching the plugin, starting tshark, and stopping it, beyond the capture.
CAPTURE_SLACK_S = 60.0
# The capture filter Wireshark would pass on; the plugin cannot apply it.
CAPTURE_FILTER = "btle.length > 20"
# How far into the capture the SIGINT case stops tshark.
INTERRUPT_SHARE = 0.5


def _tshark_case(serial, dk, seconds, *, case_id, name, expected_state,
                 capture_filter=None, interrupt=False):
    argv = [
        sys.executable, _TSHARK_CAPTURE, "capture",
        "--interface", f"ubertooth{serial}",
        "--seconds", str(seconds),
        "--address", dk["address"],
        "--min-packets", "1",
    ]
    if capture_filter is not None:
        argv += ["--filter", capture_filter]
    if interrupt:
        argv += ["--interrupt-after", f"{seconds * INTERRUPT_SHARE:g}"]
    steps = (
        _stop(serial, "establish-idle", "Establish idle before reading"),
        *_read_originals(serial, MODE_RESET_SETTINGS),
        Step("advertise",
             f"Advertiser on channel {CHANNEL}, {ADVERTISING_INTERVAL_MS} ms, nonconn",
             _dk(dk, "advertise", "--chmap", str(CHANNEL),
                 "--interval", str(ADVERTISING_INTERVAL_MS), "--payload", PAYLOAD_TAG),
             timeout_s=DK_TIMEOUT_S, mutates=True,
             output_patterns=(r"(?m)^advertising: on type nonconn "
                              rf"chmap {CHANNEL} ",)),
        # SKIP_EXIT: no tshark on this host (or no extcap script in the run).
        Step("tshark", name, tuple(argv), timeout_s=seconds + CAPTURE_SLACK_S,
             mutates=True, skip_returncode=pytools.SKIP_EXIT,
             output_patterns=(r"(?m)^tshark checks passed$",
                              r"(?m)^tshark version: \d+\.\d+")),
        _stop(serial),
        Step("advertiser-stop", "Stop the advertiser", _dk(dk, "stop"),
             timeout_s=DK_TIMEOUT_S, output_patterns=(r"(?m)^advertising: off ",)),
    )
    return StateCase(
        case_id, name, steps, (_dk_idle(dk),) + _cleanup(serial, MODE_RESET_SETTINGS),
        expected_state=expected_state
        + "; advertiser idle; channel, squelch, and LEDs restored",
    )


def extcap_tshark_cases(arguments, resources, resource_identities):
    """tshark captures through btle-extcap from the selected board; the other
    registered boards are guards."""
    serial = _serial(resource_identities, TEST_NAME).lower()
    dk = _advertiser(resources, resource_identities)
    seconds = seconds_arg(arguments)
    decoded = (f"Wireshark decodes the advertiser's {nrfdk.FIRMWARE_VERSION} packets "
               f"from {dk['address']}")
    cases = [
        _ensure_case(dk),
        _tshark_case(
            serial, dk, seconds, case_id="tshark-fields",
            name=f"tshark -i ubertooth{serial} decodes the advertiser's address",
            expected_state=decoded + ", tshark exit 0"),
    ]
    if arguments["coverage"] != "comprehensive":
        return tuple(cases)
    cases += [
        # Wireshark sends a capture filter as --extcap-capture-filter; the
        # plugin cannot apply one, says so, and captures anyway. tshark
        # relays what the plugin wrote, so the warning is visible to a user.
        _tshark_case(
            serial, dk, seconds, case_id="tshark-capture-filter",
            name="tshark -f (a capture filter the plugin cannot apply) still captures",
            expected_state=decoded + ", tshark exit 0, with the plugin's warning that "
                           "no capture filter is applied",
            capture_filter=CAPTURE_FILTER),
        # The other way a user ends a capture: Ctrl-C. tshark must stop its
        # capture and exit 0 with what it decoded, not die.
        _tshark_case(
            serial, dk, seconds, case_id="tshark-interrupt",
            name="tshark stopped with SIGINT partway exits 0 with what it decoded",
            expected_state=decoded + " before the SIGINT, tshark exit 0",
            interrupt=True),
    ]
    return tuple(cases)
