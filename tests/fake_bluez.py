"""Output samples of the BlueZ tools and the two tools that drive them.

The cases in ``ubertooth_harness/bluez_cases.py`` read what ``hciconfig``,
``hcitool``, ``ubertooth-scan`` and ``ubertooth-follow`` print. No bench has
a Bluetooth adapter yet (issue #88), so these samples stand in for the real
thing: they are built line by line from the format strings the code prints,
so a pattern or a check that does not match them would not match a real run
either.

Where each sample comes from:

- ``hciconfig`` and ``hcitool con``: BlueZ's own tools, as the lab host has
  them. The fields after a connection's handle hold link-mode flags that
  nothing here reads, so they are elided with ``...``.
- ``ubertooth-scan``: ``print_name_and_class()``, ``extra_info()`` and
  ``main()`` in the fork's ``host/ubertooth-tools/src/ubertooth-scan.c``, and
  ``afh_map_line()`` in ``follow_afh.c``. The headings (``HCI scan``,
  ``Ubertooth scan``, ``Scan results:``) are on standard error, the devices
  and the maps on standard output.
- ``ubertooth-follow``: ``main()`` in ``ubertooth-follow.c``, with the packet
  lines as ``cb_rx`` prints them (the same lines ``ubertooth-rx`` prints, so
  ``receive_cases.PACKET_LINE`` reads them).

Every address here is the bench's own: the two ESP32 peers and a made-up
adapter address.
"""

from __future__ import annotations

ADAPTER = "hci0"
ADAPTER_ADDRESS = "00:1A:7D:DA:71:13"
# The two peers, as tests/test_classic_cases.py registers them. The initiator
# pages, so it leads the piconet and its LAP is the one on the air.
ACCEPTOR_ADDRESS = "C8:85:41:2A:1B:2A"
INITIATOR_ADDRESS = "20:50:0D:E9:C5:BA"
ACCEPTOR_NAME = "UT-ESP32-2A1B2A"
CENTRAL_LAP, CENTRAL_UAP = "e9c5ba", "0d"
# A device of someone else's that an inquiry in a shared room may report as
# well. The checks must not mind it.
OTHER_ADDRESS = "11:22:33:44:55:66"


def hciconfig(up: bool = True, address: str = ADAPTER_ADDRESS,
              device: str = ADAPTER) -> str:
    """``hciconfig <device>`` for a controller that is up, or down."""
    return "\n".join((
        f"{device}:\tType: Primary  Bus: USB",
        f"\tBD Address: {address}  ACL MTU: 310:10  SCO MTU: 64:8",
        "\tUP RUNNING " if up else "\tDOWN ",
        "\tRX bytes:1543 acl:0 sco:0 events:76 errors:0",
        "\tTX bytes:1049 acl:0 sco:0 commands:76 errors:0",
        "",
    ))


def hcitool_con(*addresses: str) -> str:
    """``hcitool -i <device> con``: the links the controller holds."""
    lines = ["Connections:"]
    for index, address in enumerate(addresses):
        lines.append(f"\t> ACL {address} handle {42 + index} state 1 lm ...")
    return "\n".join(lines) + "\n"


# The AFH map of a piconet, as afh_map_line() writes it: ten bytes, two hex
# digits each. This one keeps every channel.
WHOLE_BAND_MAP = "AFH map: 0x" + "ff" * 9 + "7f"


def scan_inquiry(*devices: tuple[str, str]) -> str:
    """The standard output of ``ubertooth-scan -s``: the inquiry's devices.

    Each is one address and the name read back from it, separated by a tab.
    """
    return "".join(f"{address}\t{name}\n" for address, name in devices)


def scan_survey(lap: str = CENTRAL_LAP, uap: str = CENTRAL_UAP,
                name: str = "") -> str:
    """The standard output of the Ubertooth half: one piconet and its map.

    A piconet whose UAP the sweep recovered is paged for its name, which is
    appended after a tab; one whose UAP is unknown is printed with ``??``
    there and no name.
    """
    printable = ":".join(lap[index:index + 2].upper() for index in (0, 2, 4))
    head = f"??:??:{uap.upper() if uap else '??'}:{printable}"
    return f"{head}\t{name}\n{WHOLE_BAND_MAP}\n" if name else \
        f"{head}\n{WHOLE_BAND_MAP}\n"


def scan_extended(address: str = ACCEPTOR_ADDRESS,
                  name: str = ACCEPTOR_NAME) -> str:
    """The standard output ``-x`` adds for one device it reached."""
    return "\n".join((
        f"\tBD Address:  {address}",
        f"\tDevice Name: {name}",
        "\tLMP Version: 4.2 (0x8) LMP Subversion: 0x2043",
        "\tManufacturer: Espressif Incorporated (741)",
        "\tFeatures page 0: 0xbf 0xfe 0xcf 0xfe 0xdb 0xff 0x7b 0x87",
        "\t\t<3-slot packets> <5-slot packets> <encryption>",
        "\tClock offset: 0x4d2e",
        "\tAFH Map: 0x" + "ff" * 9 + "7f",
        "",
    ))


def packet_lines(lap: str = CENTRAL_LAP, count: int = 12) -> str:
    """Packet lines of one piconet, as cb_rx prints them."""
    return "".join(
        f"systime=1764000{index:03d} ch={index % 79:2d} LAP={lap} err=0 "
        f"clkn={1000 + index} clk_offset={index} s=-55 n=-105 snr=50\n"
        for index in range(count)
    )


def follow_progress(target: str = INITIATOR_ADDRESS, lap: str = CENTRAL_LAP,
                    uap: str = CENTRAL_UAP, afh: bool = False) -> str:
    """The standard error of ``ubertooth-follow -t <target>``, with or without
    ``-a``."""
    lines = [f"Target {target}: LAP={lap} UAP={uap}"]
    lines.append("AFH map read: 79 of 79 channels in use" if afh
                 else "Not use AFH")
    return "\n".join(lines) + "\n"
