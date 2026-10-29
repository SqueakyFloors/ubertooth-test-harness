"""`ubertooth-util -w` output, as the tools really print it.

The first two were captured from a lab board on 2026-10-29 (firmware
`git-d8e8c549`, API 1.54): one read with host tools from the same tree, one
with an older build that prints neither the capture transfer errors nor the
radio waits. The rest are built from those with ``report()``, and the refusals
use libubertooth's own wording.
"""

# A board that has reset itself, read with host tools from the firmware's tree.
FULL = """Last boot cause: watchdog
Watchdog: armed; 3000 ms without USB being serviced or the mode looping resets the board
Unrequested watchdog resets since power-on: 3 (in 56 boots)
  the last was in BLE follow mode, running a control request
Now in idle mode, not in a wait
Capture transfer errors since power-on: 0
Radio waits that gave up since boot: synthesizer lock 0, unlock 0, oscillator 0, state machine 0, packet bytes 0
"""

# The same board read with older host tools: no transfer-error or radio-wait
# line, and a wait whose name that build does not have.
OLDER_TOOLS = """Last boot cause: watchdog
Watchdog: armed; 3000 ms without USB being serviced or the mode looping resets the board
Unrequested watchdog resets since power-on: 3 (in 55 boots)
  the last was in BLE follow mode, in a wait this version does not know
Now in idle mode, not in a wait
"""

# Firmware without the request: the board answers, the record does not.
FIRMWARE_TOO_OLD = """error: could not read the watchdog record: the board's firmware does not support it (stalled, libusb -9)
note: the board reports firmware API version 1.07, and these tools were built for 1.54
error: watchdog state read failed
"""

# The board is not on the bus.
BOARD_GONE = "error: no Ubertooth found with serial {serial}\n"

# On the bus and refusing to be opened, which is how a board looks for a
# moment after the watchdog has reset it. Captured wording from the lab host.
BOARD_BUSY = (
    "error: Ubertooth device {serial} is in use by another program (libusb -6)\n"
)
CLAIM_FAILED = (
    "error: claiming the USB interface of Ubertooth device {serial} failed "
    "(libusb -5)\n"
)

# A board that has left the bus mid-request.
BOARD_LEFT = (
    "error: could not read the watchdog record: the board is no longer attached\n"
    "error: watchdog state read failed\n"
)


def report(
    *,
    resets=0,
    boots=1,
    cause="power-on",
    armed=True,
    mode="idle",
    wait="not in a wait",
    last="in BLE follow mode, waiting for the 16 MHz crystal oscillator",
    fault="",
    transfer_errors=0,
    radio_waits=(0, 0, 0, 0, 0),
):
    """One `-w` report, in the order and wording the tool prints it.

    ``transfer_errors`` or ``radio_waits`` of None leaves that line out, as
    host tools older than the one that added it do. ``fault`` is the text
    after "Last processor fault: ".
    """
    lines = [f"Last boot cause: {cause}"]
    lines.append(
        "Watchdog: armed; 3000 ms without USB being serviced or the mode "
        "looping resets the board" if armed else "Watchdog: not armed"
    )
    lines.append(
        f"Unrequested watchdog resets since power-on: {resets} "
        f"(in {boots} boot{'' if boots == 1 else 's'})"
    )
    if resets:
        lines.append(f"  the last was {last}")
    lines.append(f"Now in {mode} mode, {wait}" if mode else
                 f"Now in a mode the board does not report, {wait}")
    if fault:
        lines.append(f"Last processor fault: {fault}")
    if transfer_errors is not None:
        lines.append(f"Capture transfer errors since power-on: {transfer_errors}")
    if radio_waits is not None:
        names = ("synthesizer lock", "unlock", "oscillator", "state machine",
                 "packet bytes")
        counts = ", ".join(
            f"{name} {value}" for name, value in zip(names, radio_waits)
        )
        lines.append(f"Radio waits that gave up since boot: {counts}")
    return "\n".join(lines) + "\n"
