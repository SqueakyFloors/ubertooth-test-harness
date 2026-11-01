"""A stand-in for the fork's btle-extcap.py, for the tshark tests.

tshark runs an extcap program itself, so this is a program, not a fixture
function: ``tsharkcapture.py --program`` installs it in the temporary
Wireshark configuration and tshark calls it as it calls the real plugin. It
answers the four calls Wireshark makes (``--extcap-interfaces``,
``--extcap-dlts``, ``--extcap-config``, ``--capture``) the way the plugin at
fork d04f79e answers them, and writes advertising records to the FIFO instead
of running ``ubertooth-btle`` against a board. So a test can drive the whole
tshark path with no Ubertooth and no DK, which is what runs in CI.

The records are the ones ``libbtbb`` writes through ``ubertooth-btle -q``: a
PCAP stream of LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR (256) whose records are a
10-byte LE pseudo-header, the advertising access address, the PDU and its
CRC. ``tests/test_tshark_capture.py`` reads the same records back with the
harness's own parser (``advcapture.py``), so a fixture that drifts from what
the harness expects fails there rather than quietly becoming meaningless.

It is a program: only the standard library, and nothing imported from the
harness.

Environment:

- ``FAKE_EXTCAP_ADDRESS``: the advertiser's address (default C3:11:22:33:44:55)
- ``FAKE_EXTCAP_SERIAL``: the serial, or comma-separated serials, the
  interfaces are listed under
- ``FAKE_EXTCAP_PERIOD_S``: seconds between records (default 0.05)
- ``FAKE_EXTCAP_REFUSE``: refuse ``--capture`` with this message, exit 1
- ``FAKE_EXTCAP_QUIET_FILTER``: take a capture filter without warning about
  it, which the real plugin never does and the checks must notice
- ``FAKE_EXTCAP_SILENT``: open the FIFO and write the PCAP header, then no
  records, as a capture with no advertising traffic in range looks
- ``FAKE_EXTCAP_BUSY``: list the interfaces, then report this many boards as
  held by another program and exit 1, as the plugin does when
  ``ubertooth-util`` could not open every board on the host
- ``FAKE_EXTCAP_LISTING_ERROR``: report this on ``--extcap-interfaces`` and
  exit 1, for a listing that failed for some other reason
- ``FAKE_EXTCAP_LISTING_BARE``: the same, written without the ``error: ``
  prefix, as libubertooth writes some of its failures
- ``FAKE_EXTCAP_LISTING_STATUS``: the exit status of ``--extcap-interfaces``,
  whatever it reported, for a status the plugin would never give

``FAKE_EXTCAP_SERIAL`` takes several serials separated by commas, which is a
host with several boards attached.
"""

import getopt
import os
import struct
import sys
import time

DLT = 256
ADV_ACCESS_ADDRESS = 0x8E89BED6
ADV_CRC_INIT = 0x555555
PDU_NONCONN = 0x2
# The harness payload the DK firmware sends: company 0xffff, magic, format,
# channel map, counter, tag (firmware/nrf52840dk/README.md).
TAG = bytes.fromhex("c0ffee")


def _reverse24(value: int) -> int:
    return int(f"{value & 0xFFFFFF:024b}"[::-1], 2)


def crc24(data: bytes, init: int = ADV_CRC_INIT) -> int:
    """The LE link-layer CRC over the PDU header and PDU.

    The same register as ``advcapture.crc24``, which is what reads these
    records back; this file stays standard library only because tshark runs
    it as a program of its own.
    """
    state = _reverse24(init)
    for byte in data:
        for _bit in range(8):
            feedback = (state ^ byte) & 1
            byte >>= 1
            state >>= 1
            if feedback:
                state |= 1 << 23
                state ^= 0x5A6000
    return state


def advertising_payload(counter: int, chmap: int = 0x01) -> bytes:
    manufacturer = (struct.pack("<H", 0xFFFF) + b"UTAD" + bytes([1, chmap])
                    + struct.pack("<I", counter) + TAG)
    return bytes([2, 0x01, 0x04, len(manufacturer) + 1, 0xFF]) + manufacturer


def record(address: str, counter: int, rf_channel: int = 0) -> bytes:
    """One LE pseudo-header record, as libbtbb writes it."""
    adva = bytes(reversed(bytes.fromhex(address.replace(":", ""))))
    pdu = adva + advertising_payload(counter)
    header = bytes([PDU_NONCONN | 1 << 6, len(pdu)])
    crc = crc24(header + pdu).to_bytes(3, "little")
    access = struct.pack("<I", ADV_ACCESS_ADDRESS)
    # RF channel, signal, noise, access-address offenses, reference access
    # address, flags.
    phdr = bytes([rf_channel, 0xC0, 0x9C, 0]) + access + b"\x00\x00"
    return phdr + access + header + pdu + crc


def pcap_header() -> bytes:
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, DLT)


def pcap_record(body: bytes, when: float) -> bytes:
    return struct.pack("<IIII", int(when), int(when % 1 * 1_000_000),
                       len(body), len(body)) + body


def _capture(fifo: str, address: str, period: float) -> int:
    silent = bool(os.environ.get("FAKE_EXTCAP_SILENT"))
    with open(fifo, "wb", buffering=0) as out:
        out.write(pcap_header())
        counter = 0
        while True:
            counter += 1
            if not silent:
                out.write(pcap_record(record(address, counter), time.time()))
            time.sleep(period)


def main(argv) -> int:
    serial = os.environ.get("FAKE_EXTCAP_SERIAL", "deadbeef")
    address = os.environ.get("FAKE_EXTCAP_ADDRESS", "C3:11:22:33:44:55")
    period = float(os.environ.get("FAKE_EXTCAP_PERIOD_S", "0.05"))
    try:
        options, rest = getopt.getopt(argv, "h", [
            "extcap-interfaces", "extcap-dlts", "extcap-config", "capture",
            "extcap-interface=", "fifo=", "channel=", "extcap-version==",
            "extcap-capture-filter=",
        ])
    except getopt.GetoptError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if rest:
        print(f"error: unexpected argument '{rest[0]}'", file=sys.stderr)
        return 1
    names = [name for name, _value in options]
    values = dict(options)
    if "--extcap-interfaces" in names:
        print("extcap {version=1.0}{display=Ubertooth One}")
        for name in serial.split(","):
            print(f"interface {{value=ubertooth{name}}}"
                  f"{{display=Ubertooth One {name}}}")
        # A board another program holds: the plugin lists the ones it could
        # open and exits 1 all the same, so the listing is a partial answer
        # with a non-zero status.
        busy = int(os.environ.get("FAKE_EXTCAP_BUSY", "0"))
        for index in range(busy):
            print(f"error: Ubertooth at index {index} is in use by another "
                  "program (libusb -6)", file=sys.stderr)
        other = os.environ.get("FAKE_EXTCAP_LISTING_ERROR")
        if other:
            print(f"error: {other}", file=sys.stderr)
        # libubertooth writes some of its failures with no "error: " prefix,
        # and the plugin passes those on as they are.
        bare = os.environ.get("FAKE_EXTCAP_LISTING_BARE")
        if bare:
            print(bare, file=sys.stderr)
        status = 1 if (busy or other or bare) else 0
        return int(os.environ.get("FAKE_EXTCAP_LISTING_STATUS", status))
    if "--extcap-dlts" in names:
        print(f"dlt {{number={DLT}}}{{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}}"
              "{display=Bluetooth LE}")
        return 0
    if "--extcap-config" in names:
        print("arg {number=0}{call=--channel}{display=Advertising Channel}"
              "{type=selector}")
        for channel in (37, 38, 39):
            default = "true" if channel == 37 else "false"
            print(f"value {{arg=0}}{{value={channel}}}{{display={channel}}}"
                  f"{{default={default}}}")
        return 0
    if "--capture" in names:
        refuse = os.environ.get("FAKE_EXTCAP_REFUSE")
        if refuse:
            print(f"error: {refuse}", file=sys.stderr)
            return 1
        if "--extcap-capture-filter" in values and not os.environ.get(
                "FAKE_EXTCAP_QUIET_FILTER"):
            # The plugin's own wording: it captures anyway.
            print("warning: this interface applies no capture filter; filter the "
                  "packets in Wireshark instead (got "
                  f"{values['--extcap-capture-filter']})", file=sys.stderr)
        if "--fifo" not in values:
            print("error: --capture needs --fifo=PATH", file=sys.stderr)
            return 1
        return _capture(values["--fifo"], address, period)
    print("error: no operation given", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (BrokenPipeError, KeyboardInterrupt):
        # Wireshark stopped reading: the capture is over, and that is not a
        # failure (the plugin exits 0 the same way).
        sys.exit(0)
