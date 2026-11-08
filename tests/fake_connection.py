"""Synthetic BLE records for the intermittent follow soak's tests.

Builds the three things that soak reads out of a capture: a connectable
advertisement from the peripheral, the CONNECT_IND the central sends it, and
data packets on the connection that CONNECT_IND handed out. Every one is
built from the spec encoding, with the CRCs computed by advcapture.crc24, so
no capture of real traffic is used or stored.

tests/fake_advertiser.py does the same for `BLE address parity soak`; what is
here is what a *followed connection* adds. The PCAP header and the record
framing are shared with it, since both soaks read the same file format.
"""

from __future__ import annotations

import struct

from ubertooth_harness import advcapture, conncapture
from tests.fake_advertiser import PCAP_HEADER, address_bytes, pcap_record

__all__ = ["PCAP_HEADER", "address_bytes", "pcap_record", "adv_ind",
           "connect_ind", "data_packet", "text_block", "ALL_DATA_CHANNELS",
           "DEFAULT_INTERVAL_UNITS", "DEFAULT_TIMEOUT_UNITS", "DEFAULT_HOP"]

ADV_IND = 0x0
# Every data channel, as a CONNECT_IND's five channel-map bytes carry it.
ALL_DATA_CHANNELS = conncapture.ALL_DATA_CHANNELS
# The 30 ms connection interval the plans ask for, in 1.25 ms units, and the
# supervision timeout and hop increment the role firmware uses.
DEFAULT_INTERVAL_UNITS = 24
DEFAULT_TIMEOUT_UNITS = conncapture.DEFAULT_TIMEOUT_UNITS
DEFAULT_HOP = 7
# An LL data PDU header's first byte: LLID 2 (a start or complete L2CAP
# message), with no more data and no sequence bits set. Only the length byte
# matters to the reader, but a header that could not exist would make the
# fixture a worse stand-in than it needs to be.
DATA_HEADER_FLAGS = 0x02


def _phdr(rf_channel: int) -> bytes:
    """The 10-byte LE pseudo-header libbtbb writes, RF channel first."""
    return bytes([rf_channel, 0, 0, 0, 0, 0, 0, 0, 0, 0])


def adv_ind(address: str, *, rf_channel: int = 0, name: bytes = b"UTDK",
            pad: int = 0) -> bytes:
    """One capture record of a connectable ADV_IND from ``address``.

    Connectable, not ADV_NONCONN_IND: this fixture is a peripheral waiting to
    be connected, which is what makes the central's CONNECT_IND possible and
    what the soak's advertising phase measures.
    """
    flags = bytes([2, 0x01, 0x06])
    local_name = bytes([len(name) + 1, 0x09]) + name
    data = address_bytes(address) + flags + local_name
    # TxAdd set (a random address), the PDU type, and the payload length.
    header = bytes([ADV_IND | 0x40, len(data)])
    crc = advcapture.crc24(header + data).to_bytes(3, "little")
    body = (struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + header + data
            + crc)
    return _phdr(rf_channel) + body + b"\x00" * pad


def connect_ind(central: str, peripheral: str, *, access_address: int,
                crc_init: int, interval_units: int = DEFAULT_INTERVAL_UNITS,
                latency: int = 0, timeout_units: int = DEFAULT_TIMEOUT_UNITS,
                channel_map: int = ALL_DATA_CHANNELS, hop: int = DEFAULT_HOP,
                sleep_clock_accuracy: int = 0, rf_channel: int = 0,
                valid_crc: bool = True) -> bytes:
    """One capture record of the CONNECT_IND ``central`` sends ``peripheral``.

    The 34-byte payload is InitA, AdvA, and the 22 bytes of LLData the
    receiver takes the access address and CRC init from
    (conncapture.ConnectInd).
    """
    ll_data = (struct.pack("<I", access_address)
               + crc_init.to_bytes(3, "little")
               + bytes([2])                      # WinSize
               + struct.pack("<HHHH", 1, interval_units, latency, timeout_units)
               + channel_map.to_bytes(5, "little")
               + bytes([(hop & 0x1F) | (sleep_clock_accuracy << 5)]))
    pdu = address_bytes(central) + address_bytes(peripheral) + ll_data
    # TxAdd and RxAdd both set: two static random addresses.
    header = bytes([conncapture.CONNECT_IND | 0xC0, len(pdu)])
    crc = advcapture.crc24(header + pdu).to_bytes(3, "little")
    if not valid_crc:
        crc = bytes((crc[0] ^ 0xFF, crc[1], crc[2]))
    body = (struct.pack("<I", advcapture.ADV_ACCESS_ADDRESS) + header + pdu
            + crc)
    return _phdr(rf_channel) + body


def data_packet(access_address: int, crc_init: int, *, length: int = 244,
                rf_channel: int = 10, valid_crc: bool = True,
                counter: int = 0) -> bytes:
    """One capture record of a data packet on that connection.

    ``length`` is the link-layer payload, so anything above
    conncapture.DEFAULT_DATA_LENGTH is a packet only Data Length Extension
    allows. The payload is filler with the counter in it, never captured
    traffic.
    """
    payload = (struct.pack("<I", counter) + b"UTDK" * 64)[:length]
    payload = payload + bytes(length - len(payload))
    header = bytes([DATA_HEADER_FLAGS, length])
    crc = advcapture.crc24(header + payload, crc_init).to_bytes(3, "little")
    if not valid_crc:
        crc = bytes((crc[0] ^ 0xFF, crc[1], crc[2]))
    body = struct.pack("<I", access_address) + header + payload + crc
    return _phdr(rf_channel) + body


def text_block(address: str, *, rf_channel: int = 0) -> str:
    """What libbtbb prints for a connectable advertisement.

    Only the lines the soak's text reader can meet, in the wording and the
    lower case `_dump_addr` uses (lib/src/bluetooth_le_packet.c). libbtbb
    prints no `AdvA:` for a data packet, which is why the connection half of
    this soak is read from the records alone.
    """
    return (
        f"systime=1 freq={2402 + rf_channel} addr=8e89bed6 delta_t=0.100 ms "
        "rssi=-40\n"
        "Advertising / AA 8e89bed6 (valid)/ 21 bytes\n"
        f"    Channel Index: {37 if rf_channel == 0 else 38}\n"
        "    Type:  ADV_IND\n"
        f"    AdvA:  {address.lower()} (random)\n"
        "\n"
    )
