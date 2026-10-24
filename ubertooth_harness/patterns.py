"""Output patterns shared by catalog tests and transition cases."""

import re


FIRMWARE_PATTERN = r"Firmware version:\s+\S+"
COMPILE_INFO_PATTERN = r"\bubertooth\s+\S+\s+\("
PART_ID_PATTERN = r"Part ID:\s+[0-9A-Fa-f]+"
BOARD_ID_PATTERN = r"Board ID Number:\s+\d+"


def serial_pattern(serial: str) -> str:
    return rf"Serial No:\s+{re.escape(serial.lower())}(?:\s|$)"
