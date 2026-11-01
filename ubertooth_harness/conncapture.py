"""Check a followed connection in an ``ubertooth-btle -f`` capture file.

Run as a script by the Capture known BLE connection cases::

    python conncapture.py FILE --central ADDR --peripheral ADDR
        --interval-units N --latency N --timeout-units N
        [--min-data-packets N --min-valid-packets N
         --min-data-length N --long-packets N]
        [--check-connection-info]
        [--min-connections N --min-connection-packets N
         --after-update-ms MS --require-announced]

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
  capture really followed a connection, of which at least
  ``--min-valid-packets`` have a correct CRC, so the access address and CRC
  init really decode the connection (a sniffer that is not the connection's
  peer catches only a fraction of the traffic intact);
- at least ``--long-packets`` of those data packets at least
  ``--min-data-length`` bytes long, which is what Data Length Extension is
  for: without it the link layer would cap the payload at 27 bytes;
- with the cycle options, a capture that spans several of the pair's
  connections rather than one: at least ``--min-connections`` of them,
  counted by access address, each carrying at least
  ``--min-connection-packets`` packets, and at least one packet with a
  correct CRC ``--after-update-ms`` or more after its own CONNECT_IND, which
  is long enough that the central's parameter update has taken effect. With
  ``--require-announced``, no packet may be on an access address that no
  CONNECT_IND in the capture handed out: in a follow the board writes the
  address it synchronized on into each record, so such a packet is a
  connection the board followed without the file saying where it came from;
- with ``--check-connection-info``, that one of the connection information
  options libbtbb recorded in the PCAPNG is a CONNECT_IND this capture
  decoded from this pair, byte for byte. Those 34 bytes are what a reader
  takes from the file to follow the connection in it, and until libbtbb#21
  was fixed they were copied 6 bytes early, so every parameter in them
  belonged to another field. libbtbb records one option per CONNECT_IND it
  decodes, so a capture with other devices in range holds theirs as well:
  every option is read, not just the first. When none of them matches, the
  pair's own option (if there is one) is compared field by field and each
  difference is named.

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
    _PCAPNG_IDB_FIXED,
    _PCAPNG_SHB,
    crc24,
    format_address,
    read_timed_records,
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


def _connect_ind_body(record, central: str | None = None,
                      peripheral: str | None = None) -> bytes | None:
    """The CONNECT_IND this record carries, from its access address on, or None.

    With ``central`` and ``peripheral`` a CONNECT_IND between any other pair
    of addresses is left out.
    """
    if len(record) < LE_PHDR_LEN + 4 + 2 + CONNECT_IND_LEN:
        return None
    body = record[LE_PHDR_LEN:]
    if struct.unpack("<I", body[:4])[0] != ADV_ACCESS_ADDRESS:
        return None
    header = body[4:6]
    if header[0] & 0x0F != CONNECT_IND or header[1] & 0x3F != CONNECT_IND_LEN:
        return None
    pdu = body[6:6 + CONNECT_IND_LEN]
    # The advertising CRC, so a corrupted packet is not read as one.
    if (len(body) < 6 + CONNECT_IND_LEN + 3
            or body[6 + CONNECT_IND_LEN:9 + CONNECT_IND_LEN]
            != crc24(header + pdu).to_bytes(3, "little")):
        return None
    if central is not None and format_address(pdu[0:6]) != central.upper():
        return None
    if peripheral is not None and format_address(pdu[6:12]) != peripheral.upper():
        return None
    return bytes(body[:6 + CONNECT_IND_LEN])


def timed_connect_inds(records, central: str | None = None,
                       peripheral: str | None = None) -> list[tuple]:
    """(time, CONNECT_IND) of each one in the records, in capture order.

    The time is what a later packet's distance from the CONNECT_IND is
    measured against, so a connection whose CONNECT_IND the file carries no
    time for keeps None and is left out of that measurement.
    """
    found = []
    for record in records:
        body = _connect_ind_body(record.data, central, peripheral)
        if body is not None:
            found.append((record.micros,
                          ConnectInd.parse(body[6:6 + CONNECT_IND_LEN])))
    return found


def _data_packet(record, access_address: int, crc_init: int):
    """(payload length, CRC is right) if the record is on this access address."""
    if len(record) < LE_PHDR_LEN + 4 + 2:
        return None
    body = record[LE_PHDR_LEN:]
    if struct.unpack("<I", body[:4])[0] != access_address:
        return None
    # A data PDU header is two bytes: flags, then the payload length.
    header = body[4:6]
    length = header[1]
    whole = body[6:6 + length + 3]
    if len(whole) != length + 3:
        return length, False
    expected = crc24(header + whole[:length], crc_init).to_bytes(3, "little")
    return length, whole[length:] == expected


def data_packets(records, access_address: int, crc_init: int) -> list[tuple[int, bool]]:
    """(payload length, CRC is right) of each packet on this access address.

    The CRC is computed here with the connection's own CRC init, so a
    corrupted length byte cannot pass for a long packet.
    """
    found = (_data_packet(record, access_address, crc_init) for record in records)
    return [item for item in found if item is not None]


def timed_data_packets(records, access_address: int, crc_init: int) -> list[tuple]:
    """(time, payload length, CRC is right) of each packet on this address."""
    found = []
    for record in records:
        item = _data_packet(record.data, access_address, crc_init)
        if item is not None:
            found.append((record.micros,) + item)
    return found


def connection_addresses(records) -> list[int]:
    """The access address of every record that is not an advertising packet.

    In a follow the board writes the access address it synchronized on into
    the record itself, and it synchronizes on exactly one at a time: the
    advertising address while it hunts, then the connection's own. So each
    address here is a connection the board followed, and one that no
    CONNECT_IND in the file handed out is a connection the file cannot
    explain.
    """
    found = []
    for record in records:
        if len(record.data) < LE_PHDR_LEN + 4:
            continue
        address, = struct.unpack("<I", record.data[LE_PHDR_LEN:LE_PHDR_LEN + 4])
        if address != ADV_ACCESS_ADDRESS:
            found.append(address)
    return found


NO_OPTION = "none recorded"


def connection_info_options(data: bytes) -> list[bytes]:
    """The 34 PDU bytes of each connection information option in the PCAPNG.

    libbtbb records one for every CONNECT_IND it decodes, which in a capture
    with other devices in range includes theirs, so there may be several and
    the pair's own may be any of them. Empty when the file is not PCAPNG or
    carries no such option. An option whose own length does not cover the
    timestamp and the PDU is left out: the bytes after it belong to the next
    option, not to this one.
    """
    found = []
    if len(data) < 12 or struct.unpack("<I", data[:4])[0] != _PCAPNG_SHB:
        return found
    offset = 0
    while offset + 12 <= len(data):
        block_type, length = struct.unpack("<II", data[offset:offset + 8])
        if length < 12 or length % 4 or offset + length > len(data):
            return found
        if block_type == _PCAPNG_IDB:
            body = data[offset + 8:offset + length - 4]
            at = _PCAPNG_IDB_FIXED  # options start after the fixed fields
            while at + 4 <= len(body):
                code, option_length = struct.unpack("<HH", body[at:at + 4])
                if code == 0:  # end of options
                    break
                end = at + 4 + 4 * ((option_length + 3) // 4)
                if end > len(body):
                    break  # the option claims more than the block holds
                start = at + 4 + CONNECTION_INFO_NS_LEN
                if (code == CONNECTION_INFO_OPTION
                        and option_length >= CONNECTION_INFO_NS_LEN + CONNECT_IND_LEN):
                    found.append(bytes(body[start:start + CONNECT_IND_LEN]))
                at = end
        offset += length
    return found


def recorded_connections(data: bytes) -> list[ConnectInd]:
    """Each recorded option read as the CONNECT_IND it is meant to carry."""
    return [ConnectInd.parse(option) for option in connection_info_options(data)]


@dataclass
class Connection:
    """One connection of the pair the capture announced, and what followed it."""

    access_address: int
    micros: int | None
    packets: int = 0
    valid: int = 0
    # How long after this connection's own CONNECT_IND the last packet with a
    # correct CRC arrived. None where either has no time in the file.
    last_valid_after_ms: int | None = None


@dataclass
class Result:
    records: int = 0
    connect_inds: int = 0
    data_packets: int = 0   # on the connection's access address
    valid_packets: int = 0  # ...of those, with a correct CRC
    long_packets: int = 0
    longest: int = 0
    connection: ConnectInd | None = None
    connections: list = field(default_factory=list)
    # Access address -> how many packets, for addresses no CONNECT_IND in the
    # capture handed out.
    unannounced: dict = field(default_factory=dict)
    span_ms: int | None = None
    connection_info: str = "not looked for"
    connection_info_options: int = 0
    problems: list = field(default_factory=list)


def check(data: bytes, *, central: str, peripheral: str, interval_units: int,
          latency: int, timeout_units: int, min_data_packets: int = 0,
          min_valid_packets: int = 0,
          min_data_length: int = DEFAULT_DATA_LENGTH + 1,
          long_packets: int = 0,
          check_connection_info: bool = False,
          min_connections: int = 0, min_connection_packets: int = 0,
          after_update_ms: int | None = None,
          require_announced: bool = False) -> tuple[str, Result]:
    result = Result()
    try:
        file_format, link_type, timed = read_timed_records(data)
    except CaptureError as error:
        result.problems.append(str(error))
        return "unreadable", result
    header = f"{file_format}, link type {link_type}"
    if link_type != LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR:
        result.problems.append(
            f"link type {link_type}, not {LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR}")
        return header, result
    records = [record.data for record in timed]
    result.records = len(records)
    times = [record.micros for record in timed if record.micros is not None]
    result.span_ms = (max(times) - min(times)) // 1000 if times else None
    central = central.upper()
    peripheral = peripheral.upper()
    # Only our pair's CONNECT_INDs, so another device in range cannot decide
    # what the checks below are measured against.
    ours_timed = timed_connect_inds(timed, central=central, peripheral=peripheral)
    ours = [connection for _at, connection in ours_timed]
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
    for at, connection in ours_timed:
        if any(item.access_address == connection.access_address
               for item in result.connections):
            # One connection announced twice. Its packets are counted once,
            # against the CONNECT_IND that opened it: a second CONNECT_IND
            # for an access address already in the file adds no traffic, and
            # counting the same packets again would raise the packet floors
            # without a packet arriving.
            continue
        found = timed_data_packets(timed, connection.access_address,
                                   connection.crc_init)
        packets += [(length, crc_ok) for _when, length, crc_ok in found]
        # Only packets at or after this connection's own CONNECT_IND: the
        # search above reads the whole file, so an access address that came
        # round again would otherwise give a negative distance.
        after = [when - at for when, _length, crc_ok in found
                 if crc_ok and when is not None and at is not None and when >= at]
        result.connections.append(Connection(
            access_address=connection.access_address,
            micros=at,
            packets=len(found),
            valid=sum(1 for _when, _length, crc_ok in found if crc_ok),
            last_valid_after_ms=max(after) // 1000 if after else None,
        ))
    valid = [length for length, crc_ok in packets if crc_ok]
    result.data_packets = len(packets)
    result.valid_packets = len(valid)
    result.longest = max(valid) if valid else 0
    result.long_packets = sum(1 for length in valid if length >= min_data_length)
    if len(packets) < min_data_packets:
        result.problems.append(
            f"{len(packets)} packet(s) on the connection's access address, fewer "
            f"than {min_data_packets}")
    if len(valid) < min_valid_packets:
        result.problems.append(
            f"{len(valid)} packet(s) with a correct CRC, fewer than "
            f"{min_valid_packets} (of {len(packets)} on the access address)")
    if result.long_packets < long_packets:
        result.problems.append(
            f"{result.long_packets} packet(s) of {min_data_length} bytes or more, "
            f"fewer than {long_packets} (longest {result.longest})")
    # Every connection the capture announced, whoever made it: a followed
    # connection whose CONNECT_IND is not in the file is what the check
    # below is for, and another pair's CONNECT_IND explains their packets
    # just as well as ours explains ours.
    announced = {connection.access_address
                 for _at, connection in timed_connect_inds(timed)}
    for address in connection_addresses(timed):
        if address not in announced:
            result.unannounced[address] = result.unannounced.get(address, 0) + 1
    if len(result.connections) < min_connections:
        result.problems.append(
            f"{len(result.connections)} connection(s) from {central} to "
            f"{peripheral}, fewer than {min_connections}")
    for item in result.connections:
        if item.packets < min_connection_packets:
            result.problems.append(
                f"connection {item.access_address:08x} carries {item.packets} "
                f"packet(s), fewer than {min_connection_packets}")
    if after_update_ms is not None:
        latest = max((item.last_valid_after_ms for item in result.connections
                      if item.last_valid_after_ms is not None), default=None)
        if latest is None or latest < after_update_ms:
            result.problems.append(
                f"no packet with a correct CRC {after_update_ms} ms or more after "
                "its own CONNECT_IND, so the capture does not show a followed "
                "connection surviving the parameter update (the latest was "
                + (f"{latest} ms" if latest is not None else "none") + ")")
    if require_announced and result.unannounced:
        result.problems.append(
            "packet(s) on an access address no CONNECT_IND in this capture "
            "handed out: "
            + ", ".join(f"{address:08x} x{count}"
                        for address, count in sorted(result.unannounced.items())))
    recorded = recorded_connections(data)
    result.connection_info_options = len(recorded)
    ours_recorded = [found for found in recorded
                     if found.initiator == central and found.advertiser == peripheral]
    # The option a reader would use is one that is a CONNECT_IND this capture
    # decoded, byte for byte. Anything else sends a reader to a connection
    # that is not in the file.
    decoded = [found for found in ours_recorded if found in ours]
    result.connection_info = (
        NO_OPTION if not recorded
        else f"this pair's CONNECT_IND as the capture decoded it, {len(recorded)} recorded"
        if decoded
        else f"{len(recorded)} recorded, none a CONNECT_IND this capture decoded "
             "from the pair")
    if check_connection_info and not decoded:
        if not recorded:
            result.problems.append(
                "the PCAPNG carries no connection information option, so a "
                "reader cannot follow the connections in it")
        else:
            # Name what is wrong with the pair's own recorded option, where
            # there is one: those are the numbers a reader would follow.
            differences = []
            if ours_recorded:
                option = ours_recorded[0]
                for name, found, expected in (
                    ("connection interval", option.interval, interval_units),
                    ("peripheral latency", option.latency, latency),
                    ("supervision timeout", option.timeout, timeout_units),
                    ("channel map", option.channel_map, ALL_DATA_CHANNELS),
                ):
                    if found != expected:
                        differences.append(f"{name} {found}, not {expected}")
                for name, found, decoded_values in (
                    ("access address", option.access_address,
                     {connection.access_address for connection in ours}),
                    ("CRC init", option.crc_init,
                     {connection.crc_init for connection in ours}),
                    ("hop increment", option.hop,
                     {connection.hop for connection in ours}),
                ):
                    if found not in decoded_values:
                        differences.append(
                            f"{name} {found} belongs to no CONNECT_IND the "
                            "capture decoded")
            result.problems.append(
                f"none of the {len(recorded)} recorded connection information "
                f"option(s) is a CONNECT_IND this capture decoded from {central} "
                f"to {peripheral}"
                + (": " + "; ".join(differences) if differences else ""))
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
    parser.add_argument("--min-valid-packets", type=int, default=0)
    parser.add_argument("--min-data-length", type=int, default=DEFAULT_DATA_LENGTH + 1)
    parser.add_argument("--long-packets", type=int, default=0)
    parser.add_argument("--check-connection-info", action="store_true",
                        help="require the recorded connection information option to "
                             "hold this pair's CONNECT_IND and its parameters")
    parser.add_argument("--min-connections", type=int, default=0,
                        help="how many of the pair's connections the capture must "
                             "hold, counted by access address")
    parser.add_argument("--min-connection-packets", type=int, default=0,
                        help="packets each of those connections must carry")
    parser.add_argument("--after-update-ms", type=int,
                        help="require a packet with a correct CRC this long after "
                             "its own CONNECT_IND")
    parser.add_argument("--require-announced", action="store_true",
                        help="refuse a packet on an access address no CONNECT_IND "
                             "in the capture handed out")
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
        min_valid_packets=args.min_valid_packets,
        min_data_length=args.min_data_length, long_packets=args.long_packets,
        check_connection_info=args.check_connection_info,
        min_connections=args.min_connections,
        min_connection_packets=args.min_connection_packets,
        after_update_ms=args.after_update_ms,
        require_announced=args.require_announced,
    )
    span = "unknown" if result.span_ms is None else f"{result.span_ms / 1000:.1f}s"
    print(f"capture: {header}, {result.records} records, span {span}")
    print(f"connect_ind: {result.connect_inds}")
    print(f"connections: {len(result.connections)}")
    for index, item in enumerate(result.connections, start=1):
        after = ("no timed packet" if item.last_valid_after_ms is None
                 else f"last correct CRC {item.last_valid_after_ms} ms after its "
                      "CONNECT_IND")
        print(f"connection {index}: access address {item.access_address:08x}, "
              f"{item.packets} packet(s), {item.valid} with a correct CRC, {after}")
    unannounced = ", ".join(f"{address:08x} x{count}" for address, count
                            in sorted(result.unannounced.items()))
    print(f"unannounced access addresses: {unannounced or 'none'}")
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
