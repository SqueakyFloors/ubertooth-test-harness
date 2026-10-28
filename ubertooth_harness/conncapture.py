"""Check a followed connection in an ``ubertooth-btle -f`` capture file.

Run as a script by the Capture known BLE connection cases::

    python conncapture.py FILE --central ADDR --peripheral ADDR
        --interval-units N --latency N --timeout-units N
        [--min-data-packets N --min-data-length N --long-packets N]
        [--connection-info-option]

FILE is PCAP or PCAPNG as ``ubertooth-btle -q`` and ``-r`` write it; the
records are read by advcapture.py.

The checks:

- at least one CONNECT_IND between the two addresses (the pair remakes the
  link on a schedule, so a capture may hold several), each with the
  connection interval, peripheral latency and supervision timeout the
  central was told to ask for, a hop increment of 5-16, and all 37 data
  channels in its channel map (every value a capture must show for the link
  the harness set up, so a wrong one means the DKs or the decode are wrong);
- the access address and CRC init each CONNECT_IND hands out, and at least
  ``--min-data-packets`` data-channel packets carrying one of them, so the
  capture really followed a connection;
- at least ``--long-packets`` of those data packets at least
  ``--min-data-length`` bytes long, which is what Data Length Extension is
  for: without it the link layer would cap the payload at 27 bytes;
- with ``--connection-info-option``, that the connection parameters libbtbb
  records as a PCAPNG interface option are still copied 6 bytes early
  (SqueakyFloors/libbtbb#21). That is the opposite of a wish: the check
  pins a known bug so it fails when the bug is fixed.

Only the two DKs' own addresses and counts are printed, never another
device's address or data. Each failure is a ``FAIL:`` line and exit status
1; exit status 2 is a usage error.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import struct
import sys

if __package__ in (None, ""):
    # Run as a script: make the package importable for advcapture's reader.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness.advcapture import (  # noqa: E402
    ADV_ACCESS_ADDRESS,
    LE_PHDR_LEN,
    CaptureError,
    LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR,
    _PCAPNG_IDB,
    _PCAPNG_SHB,
    crc24,
    format_address,
    read_records,
)

CONNECT_IND = 0x5
# CONNECT_IND payload: InitA (6), AdvA (6), then LLData (22).
CONNECT_IND_LEN = 34
# The supervision timeout the role firmware asks for, in 10 ms units
# (firmware/nrf52840dk/src/connection.c TIMEOUT_UNITS).
DEFAULT_TIMEOUT_UNITS = 400
# libbtbb records the connection parameters as an interface option of the
# PCAPNG it writes: this code, then 8 bytes of timestamp and 34 of PDU
# (lib/src/pcapng-bt.c, PCAPNG_LE_LL_CONNECTION_INFO).
CONNECTION_INFO_OPTION = 0xD380
CONNECTION_INFO_NS_LEN = 8
_PCAPNG_IDB_HEADER = 8 + 2 + 2 + 4  # block type, length, link type, reserved, snaplen
# Every data channel in the channel map: 37 bits set.
ALL_DATA_CHANNELS = (1 << 37) - 1
# Bluetooth Core: the hop increment is 5 to 16.
HOP_MIN = 5
HOP_MAX = 16
# Without Data Length Extension the link layer payload stops at 27 bytes.
DEFAULT_DATA_LENGTH = 27


class ConnectIndError(ValueError):
    """The CONNECT_IND in the capture cannot be read."""


@dataclass(frozen=True)
class ConnectInd:
    """The fields of a CONNECT_IND, as the capture carries them."""

    initiator: str
    advertiser: str
    access_address: int
    crc_init: int
    window_size: int
    window_offset: int
    interval: int   # 1.25 ms units
    latency: int
    timeout: int    # 10 ms units
    channel_map: int
    hop: int
    sleep_clock_accuracy: int

    @classmethod
    def parse(cls, pdu: bytes) -> "ConnectInd":
        if len(pdu) != CONNECT_IND_LEN:
            raise ConnectIndError(f"CONNECT_IND is {len(pdu)} bytes, not {CONNECT_IND_LEN}")
        data = pdu[12:]
        access_address, = struct.unpack("<I", data[0:4])
        crc_init = int.from_bytes(data[4:7], "little")
        window_size = data[7]
        window_offset, interval, latency, timeout = struct.unpack("<HHHH", data[8:16])
        channel_map = int.from_bytes(data[16:21], "little")
        hop_sca = data[21]
        return cls(
            initiator=format_address(pdu[0:6]),
            advertiser=format_address(pdu[6:12]),
            access_address=access_address,
            crc_init=crc_init,
            window_size=window_size,
            window_offset=window_offset,
            interval=interval,
            latency=latency,
            timeout=timeout,
            channel_map=channel_map,
            hop=hop_sca & 0x1F,
            sleep_clock_accuracy=hop_sca >> 5,
        )


def connect_ind_bodies(records) -> list[bytes]:
    """Each CONNECT_IND record from its access address on (header and LLData)."""
    bodies = []
    for record in records:
        if len(record) < LE_PHDR_LEN + 4 + 2 + CONNECT_IND_LEN:
            continue
        body = record[LE_PHDR_LEN:]
        if struct.unpack("<I", body[:4])[0] != ADV_ACCESS_ADDRESS:
            continue
        header = body[4:6]
        if header[0] & 0x0F != CONNECT_IND or header[1] & 0x3F != CONNECT_IND_LEN:
            continue
        bodies.append(bytes(body[:6 + CONNECT_IND_LEN]))
    return bodies


def connect_inds(records) -> list[ConnectInd]:
    """Every CONNECT_IND in the records, in capture order."""
    found = []
    for record in records:
        if len(record) < LE_PHDR_LEN + 4 + 2 + CONNECT_IND_LEN:
            continue
        body = record[LE_PHDR_LEN:]
        if struct.unpack("<I", body[:4])[0] != ADV_ACCESS_ADDRESS:
            continue
        header = body[4:6]
        if header[0] & 0x0F != CONNECT_IND:
            continue
        length = header[1] & 0x3F
        if length != CONNECT_IND_LEN or len(body) < 6 + length:
            continue
        try:
            found.append(ConnectInd.parse(body[6:6 + length]))
        except ConnectIndError:
            continue
    return found


def data_packets(records, access_address: int, crc_init: int) -> list[tuple[int, bool]]:
    """(payload length, CRC is right) of each packet on this access address.

    The CRC is computed here with the connection's own CRC init, so a
    corrupted length byte cannot pass for a long packet.
    """
    found = []
    for record in records:
        if len(record) < LE_PHDR_LEN + 4 + 2:
            continue
        body = record[LE_PHDR_LEN:]
        if struct.unpack("<I", body[:4])[0] != access_address:
            continue
        # A data PDU header is two bytes: flags, then the payload length.
        header = body[4:6]
        length = header[1]
        whole = body[6:6 + length + 3]
        if len(whole) != length + 3:
            found.append((length, False))
            continue
        expected = crc24(header + whole[:length], crc_init).to_bytes(3, "little")
        found.append((length, whole[length:] == expected))
    return found


SHIFTED = "shifted by 6 bytes (libbtbb#21)"


def connection_info_option(data: bytes) -> bytes | None:
    """The 34 PDU bytes libbtbb records in the PCAPNG's interface option.

    None when the file is not PCAPNG or carries no such option. The option's
    own length field is not trusted: libbtbb sets it to the whole option's
    size including the header, so the payload is read by position.
    """
    if len(data) < 12 or struct.unpack("<I", data[:4])[0] != _PCAPNG_SHB:
        return None
    offset = 0
    while offset + 12 <= len(data):
        block_type, length = struct.unpack("<II", data[offset:offset + 8])
        if length < 12 or length % 4 or offset + length > len(data):
            return None
        if block_type == _PCAPNG_IDB:
            body = data[offset + 8:offset + length - 4]
            at = _PCAPNG_IDB_HEADER - 8  # options start after the fixed fields
            while at + 4 <= len(body):
                code, option_length = struct.unpack("<HH", body[at:at + 4])
                if code == CONNECTION_INFO_OPTION:
                    start = at + 4 + CONNECTION_INFO_NS_LEN
                    pdu = body[start:start + CONNECT_IND_LEN]
                    return bytes(pdu) if len(pdu) == CONNECT_IND_LEN else None
                if code == 0:  # end of options
                    break
                at += 4 + 4 * ((option_length + 3) // 4)
        offset += length
    return None


def classify_connection_info(option: bytes | None, connect_ind_body: bytes) -> str:
    """How libbtbb's recorded option relates to the CONNECT_IND on the air.

    ``connect_ind_body`` is the packet from its access address on, as the
    capture record holds it: access address (4), PDU header (2), then the 34
    bytes of LLData the option is meant to carry.
    """
    if option is None:
        return "absent"
    if option == connect_ind_body[6:6 + CONNECT_IND_LEN]:
        return "correct"
    if option == connect_ind_body[0:CONNECT_IND_LEN]:
        return SHIFTED
    return "neither the PDU nor the 6-byte shift"


@dataclass
class Result:
    records: int = 0
    connect_inds: int = 0
    data_packets: int = 0   # on the connection's access address
    valid_packets: int = 0  # ...of those, with a correct CRC
    long_packets: int = 0
    longest: int = 0
    connection: ConnectInd | None = None
    connection_info: str = "not looked for"
    problems: list = field(default_factory=list)


def check(data: bytes, *, central: str, peripheral: str, interval_units: int,
          latency: int, timeout_units: int, min_data_packets: int = 0,
          min_data_length: int = DEFAULT_DATA_LENGTH + 1,
          long_packets: int = 0,
          connection_info_shifted: bool = False) -> tuple[str, Result]:
    result = Result()
    try:
        file_format, link_type, records = read_records(data)
    except CaptureError as error:
        result.problems.append(str(error))
        return "unreadable", result
    header = f"{file_format}, link type {link_type}"
    if link_type != LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR:
        result.problems.append(
            f"link type {link_type}, not {LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR}")
        return header, result
    result.records = len(records)
    central = central.upper()
    peripheral = peripheral.upper()
    ours = [
        found for found in connect_inds(records)
        if found.initiator == central and found.advertiser == peripheral
    ]
    result.connect_inds = len(ours)
    if not ours:
        result.problems.append(
            f"no CONNECT_IND from {central} to {peripheral}")
        return header, result
    # The pair remakes the link on a schedule, so a capture may hold several
    # connections. Every one of them must carry the parameters asked for.
    result.connection = ours[0]
    wrong = {}
    for connection in ours:
        for name, found, expected in (
            ("connection interval", connection.interval, interval_units),
            ("peripheral latency", connection.latency, latency),
            ("supervision timeout", connection.timeout, timeout_units),
        ):
            if found != expected:
                wrong[f"CONNECT_IND {name} {found}, not {expected}"] = True
        if not HOP_MIN <= connection.hop <= HOP_MAX:
            wrong[f"CONNECT_IND hop increment {connection.hop}, not "
                  f"{HOP_MIN}-{HOP_MAX}"] = True
        if connection.channel_map != ALL_DATA_CHANNELS:
            wrong[f"CONNECT_IND channel map {connection.channel_map:#011x}, not "
                  f"every data channel ({ALL_DATA_CHANNELS:#011x})"] = True
    result.problems.extend(wrong)
    packets = []
    for connection in ours:
        packets += data_packets(records, connection.access_address,
                                connection.crc_init)
    valid = [length for length, crc_ok in packets if crc_ok]
    result.data_packets = len(packets)
    result.valid_packets = len(valid)
    result.longest = max(valid) if valid else 0
    result.long_packets = sum(1 for length in valid if length >= min_data_length)
    if len(valid) < min_data_packets:
        result.problems.append(
            f"{len(valid)} packet(s) on the connection's access address with a "
            f"correct CRC, fewer than {min_data_packets} ({len(packets)} in all)")
    if result.long_packets < long_packets:
        result.problems.append(
            f"{result.long_packets} packet(s) of {min_data_length} bytes or more, "
            f"fewer than {long_packets} (longest {result.longest})")
    bodies = connect_ind_bodies(records)
    if bodies:
        result.connection_info = classify_connection_info(
            connection_info_option(data), bodies[0])
    if connection_info_shifted and result.connection_info != SHIFTED:
        # The case exists to catch the fix: once libbtbb records the right
        # bytes, this is where someone turns it into a real check.
        result.problems.append(
            f"the PCAPNG connection information option is '{result.connection_info}', "
            f"not '{SHIFTED}'; if libbtbb#21 is fixed, check the parameters here "
            "instead of pinning the bug")
    return header, result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="conncapture.py",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("file")
    parser.add_argument("--central", required=True)
    parser.add_argument("--peripheral", required=True)
    parser.add_argument("--interval-units", required=True, type=int)
    parser.add_argument("--latency", required=True, type=int)
    parser.add_argument("--timeout-units", required=True, type=int)
    parser.add_argument("--min-data-packets", type=int, default=0)
    parser.add_argument("--min-data-length", type=int, default=DEFAULT_DATA_LENGTH + 1)
    parser.add_argument("--long-packets", type=int, default=0)
    parser.add_argument("--connection-info-option", action="store_true",
                        help="require libbtbb's recorded option to still be wrong")
    args = parser.parse_args(argv)
    try:
        with open(args.file, "rb") as stream:
            data = stream.read()
    except OSError as error:
        print(f"FAIL: cannot read {args.file}: {error.strerror or error}")
        return 1
    header, result = check(
        data, central=args.central, peripheral=args.peripheral,
        interval_units=args.interval_units, latency=args.latency,
        timeout_units=args.timeout_units, min_data_packets=args.min_data_packets,
        min_data_length=args.min_data_length, long_packets=args.long_packets,
        connection_info_shifted=args.connection_info_option,
    )
    print(f"capture: {header}, {result.records} records")
    print(f"connect_ind: {result.connect_inds}")
    found = result.connection
    if found is not None:
        print(f"parameters: interval {found.interval} units "
              f"({found.interval * 1.25:g} ms) latency {found.latency} timeout "
              f"{found.timeout} units hop {found.hop} window "
              f"{found.window_size}/{found.window_offset}")
        print(f"access address: {found.access_address:08x} crc init "
              f"{found.crc_init:06x}")
    print(f"data packets: {result.data_packets} ({result.valid_packets} with a "
          f"correct CRC, {result.long_packets} of {args.min_data_length} bytes or "
          f"more, longest {result.longest})")
    print(f"connection info option: {result.connection_info}")
    for problem in result.problems:
        print(f"FAIL: {problem}")
    if result.problems:
        return 1
    print("capture checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
