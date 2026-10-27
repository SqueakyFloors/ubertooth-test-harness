"""Output patterns shared by catalog tests and transition cases."""

import re


FIRMWARE_PATTERN = r"Firmware version:\s+\S+"
# Since fork 730b4f5 (#523) `ubertooth-util -v` names the image ahead of the
# revision when the firmware carries an info block ("Firmware version:
# bluetooth_rxtx git-699960b (API:1.35)"); older firmware, such as the KGV,
# prints the revision alone ("Firmware version: 2020-12-R1 (API:1.07)").
# Only the application image the harness tests may be named; a board on
# another image (btbr, a bench image) does not match.
FIRMWARE_IMAGE = r"(?:bluetooth_rxtx\s+)?"
FIRMWARE_REVISION_RE = re.compile(
    rf"Firmware version:\s+{FIRMWARE_IMAGE}(?P<revision>\S+)\s+\(API:"
)
COMPILE_INFO_PATTERN = r"\bubertooth\s+\S+\s+\("
PART_ID_PATTERN = r"Part ID:\s+[0-9A-Fa-f]+"
BOARD_ID_PATTERN = r"Board ID Number:\s+\d+"


def serial_pattern(serial: str) -> str:
    return rf"Serial No:\s+{re.escape(serial.lower())}(?:\s|$)"
