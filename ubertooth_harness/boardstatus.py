"""What one registered board's status is, and how to say it in words.

The runner reports the three facts it can establish about a board without
opening it: whether USB still has it, whether a lease holds it, and the
firmware revision the last run that read it found. This turns those into the
status the Targets card shows, with a sentence that says why, so a reader
never has to work it out from a colored dot.

The derivation lives here, in one tested place, rather than in the page:
a browser then renders a status it was given instead of inventing one, and
the same words appear wherever a board is listed.
"""


# A board that is registered, on USB, free, and whose firmware a run has
# read. Nothing stops a run using it.
READY = "ready"
# On USB, but a lease holds it: a run or a person has it now.
BUSY = "busy"
# On USB and free, but something about it is not established: no run has
# read its firmware yet, or this target cannot enumerate USB at all.
UNKNOWN = "unknown"
# Registered, and USB has not got it: unplugged, powered off, or wedged
# badly enough to have left the bus.
OFFLINE = "offline"

STATUSES = (READY, BUSY, UNKNOWN, OFFLINE)

# How each status is drawn. Green for a board a run can take, yellow for one
# that needs looking at, red for one that is not there. The page shows the
# text beside it either way; the color only repeats what the words say.
LEVELS = {
    READY: "ready",
    BUSY: "attention",
    UNKNOWN: "attention",
    OFFLINE: "offline",
}


def board_status(board) -> tuple[str, str]:
    """``(status, why)`` for one board as the runner reported it.

    ``board`` is one entry of the protocol's ``boards`` list: ``present``
    (true, false, or null when enumeration is unavailable), ``held``, the
    ``holder`` line a lease carries, and ``firmware_revision``.

    An established fact comes before an unestablished one, so a board a
    lease holds reads as busy even on a target that cannot say whether USB
    still has it.
    """
    present = board.get("present")
    held = bool(board.get("held"))
    holder = str(board.get("holder") or "").strip()
    revision = str(board.get("firmware_revision") or "").strip()
    if present is False:
        return OFFLINE, (
            "registered, but USB on this target has not got its serial: "
            "unplugged, powered off, no longer answering, or sitting in its "
            "DFU bootloader, which reports no serial at all"
        )
    if held:
        return BUSY, (
            f"leased by {holder}" if holder else "leased by another command"
        ) + (" (on USB)" if present else "")
    if present is None:
        return UNKNOWN, (
            "this target cannot list USB devices, so whether the board is "
            "attached is not established"
        )
    if revision:
        return READY, f"on USB and free; last read running {revision}"
    return UNKNOWN, (
        "on USB and free, but no run has read its firmware yet, so which "
        "revision it carries is not established"
    )


def with_status(boards) -> list[dict]:
    """Every board with ``status``, ``status_detail`` and ``level`` added.

    The controller's own view of a target, so the GUI and any other client
    read one answer rather than two that could disagree.
    """
    listed = []
    for board in boards or ():
        status, why = board_status(board)
        listed.append({
            **board, "status": status, "status_detail": why,
            "level": LEVELS[status],
        })
    return listed


def summary(boards) -> str:
    """One line counting a target's boards by status.

    For the Targets card's heading, where a count is read before the list.
    """
    listed = with_status(boards)
    if not listed:
        return "no board registered on this target"
    counts = {status: 0 for status in STATUSES}
    for board in listed:
        counts[board["status"]] += 1
    parts = [
        f"{counts[status]} {status}" for status in STATUSES if counts[status]
    ]
    total = len(listed)
    return f"{total} board{'s' if total != 1 else ''}: " + ", ".join(parts)
