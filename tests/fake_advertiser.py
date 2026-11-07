"""Synthetic BLE advertising, for the address parity soak's tests.

Builds the two things the soak reads: a PCAP record of an ADV_NONCONN_IND as
`ubertooth-btle -q` writes one, and the text block libbtbb prints for the
same packet. Both are built from the spec encoding, with the CRC computed by
advcapture.crc24, so no capture of real traffic is used or stored.

The fake `ubertooth-btle` in tests/test_addresssoak.py writes these.
"""

from __future__ import annotations

import struct

from ubertooth_harness import advcapture

# LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR, the nanosecond PCAP magic libbtbb
# writes, and the fields of its 10-byte pseudo-header (RF channel first).
PCAP_HEADER = struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 262144,
                          advcapture.LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR)
ADV_NONCONN_IND = 0x2
# The harness payload the DK firmware sends: manufacturer data with company
# 0xffff, magic UTAD, format 1, the channel map byte, a counter, and the tag.
CHANNEL_MAP_37 = 0x01


def address_bytes(address: str) -> bytes:
    """`C3:11:22:33:44:55` as the six bytes a PDU carries (least first)."""
    return bytes(reversed([int(part, 16) for part in address.split(":")]))


def payload(counter: int, tag: bytes = b"\xc0\xff\xee",
            channel_map: int = CHANNEL_MAP_37) -> bytes:
    """One manufacturer-data AD structure, as the DK firmware builds it."""
    value = (struct.pack("<H", advcapture.COMPANY_ID) + advcapture.MAGIC
             + bytes([advcapture.PAYLOAD_FORMAT, channel_map])
             + struct.pack("<I", counter) + tag)
    return bytes([len(value) + 1, advcapture.AD_MANUFACTURER]) + value


def record(address: str, *, counter: int = 1, rf_channel: int = 0,
           tag: bytes = b"\xc0\xff\xee", valid_crc: bool = True,
           pad: int = 0) -> bytes:
    """One capture record of an ADV_NONCONN_IND from `address`.

    With ``valid_crc`` false the CRC bytes are wrong, which is the packet the
    text reader reports and the structural reader drops: libbtbb prints a
    decoded packet whether or not its CRC checks.
    """
    data = address_bytes(address) + payload(counter, tag)
    # TxAdd set (a random address), the PDU type, and the payload length.
    header = bytes([ADV_NONCONN_IND | 0x40, len(data)])
    crc = advcapture.crc24(header + data).to_bytes(3, "little")
    if not valid_crc:
        crc = bytes((crc[0] ^ 0xFF, crc[1], crc[2]))
    phdr = bytes([rf_channel, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    body = struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + header + data + crc
    return phdr + body + b"\x00" * pad


def pcap_record(data: bytes, seconds: int, nanoseconds: int) -> bytes:
    """A record and its PCAP header, timed on the capture's own clock."""
    return struct.pack("<IIII", seconds, nanoseconds, len(data), len(data)) + data


def text_block(address: str, *, rf_channel: int = 0, random: bool = True) -> str:
    """What libbtbb prints for that packet (bluetooth_le_packet.c).

    Only the lines the soak's text reader can meet: the per-packet line the
    tool prints, the advertising header libbtbb prints, and the `AdvA:` line
    itself, in the wording and the lower case `_dump_addr` uses.
    """
    kind = "random" if random else "public"
    return (
        f"systime=1 freq={2402 + rf_channel} addr=8e89bed6 delta_t=0.100 ms rssi=-40\n"
        "Advertising / AA 8e89bed6 (valid)/ 21 bytes\n"
        f"    Channel Index: {37 if rf_channel == 0 else 38}\n"
        "    Type:  ADV_NONCONN_IND\n"
        f"    AdvA:  {address.lower()} ({kind})\n"
        "\n"
    )
