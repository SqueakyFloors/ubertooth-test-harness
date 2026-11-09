"""The one human-readable duration format the harness reports.

A measured duration is recorded as a number of seconds, unrounded, because
that is what a machine reading ``run.jsonl`` or the controller's API needs:
``duration_s``, ``elapsed_s``, ``estimated_s``. A person reading the same
figure wants hours, minutes, and seconds. So every place that summarizes a
finished run, test, or step keeps the number and carries the text from here
beside it (``duration_hms``), and no caller formats a duration of its own:
the terminal summary, ``report.md``, the GUI and its history, the completion
email, and a release's public evidence all say the same words for the same
length of time.

Two forms, one wording. :func:`human_duration` writes a measured duration to
the millisecond, which is how finely the harness times a command. Some
lengths of time are not measurements of a finished thing: how old a lease is,
how long a board has been held, how long a command has been waiting. Those
are read off a clock that is still running, in a table somebody is scanning,
and the milliseconds are noise there, so :func:`coarse_duration` writes the
same units to the whole second. Which form a caller wants is its own
decision; how an hour, a minute, and a second are written is this module's,
so ``1h 05m 03s`` and ``1h 05m 03.000s`` can never come to disagree about
the words between the numbers.

Nothing here reads a clock or a record. It converts one number of seconds,
however that number was arrived at.
"""

from __future__ import annotations

import math

# The printed precision, in decimal places of a second. Milliseconds: the
# harness times commands to that, and a run of days is still read to the
# same place, so two durations never differ only in how finely they were
# written.
PLACES = 3

_MS = 10 ** PLACES
_SECOND_MS = _MS
_MINUTE_MS = 60 * _SECOND_MS
_HOUR_MS = 60 * _MINUTE_MS
_DAY_MS = 24 * _HOUR_MS


def human_duration(seconds: float) -> str:
    """``4234.536`` seconds as ``1h 10m 34.536s``.

    The largest unit the duration reaches leads and every smaller one
    follows, down to seconds with three decimals, so a sub-second duration
    still says how long it was (``0.004s``) and a soak says its days
    (``2d 03h 10m 34.536s``). Days are the largest unit: a soak is measured
    in them, and nothing on a bench runs for a month.

    Every unit after the first is zero-padded, which keeps two durations of
    the same length the same width in a table column, and makes
    ``1h 00m 05.500s`` read as a duration rather than as five seconds.

    The arithmetic is done in whole milliseconds, so the parts always add
    back up to the printed total and no rounding can produce ``60.000s``.
    A negative duration -- only a clock that went backwards between two
    records -- keeps its sign instead of being quietly flattened to zero.
    Infinity and NaN are not durations and raise :class:`ValueError`.
    """
    value = _finite(seconds)
    sign = "-" if value < 0 else ""
    return sign + _written(round(abs(value) * _MS), PLACES)


def coarse_duration(seconds: float) -> str:
    """``3723`` seconds as ``1h 02m 03s``: the same words, whole seconds.

    For a length of time that is still being counted rather than measured:
    how old a lease is, how long a board has been held, how long a command
    has been waiting (``ubertooth-harness boards``). The units, their order,
    and the padding are :func:`human_duration`'s, so a reader who has seen
    one form reads the other; only the milliseconds go, because a figure
    taken off a running clock does not have them to the millisecond and a
    board table is easier to scan without them.

    Rounded down, not to the nearest second, because this is a figure that
    is counting up: a lease taken 12.9s ago has been held for 12 whole
    seconds, and ``13s`` would claim a second that has not passed. A
    negative value keeps its sign, as above; a caller reading a clock that
    may have gone backwards clamps it before it gets here.
    """
    value = _finite(seconds)
    sign = "-" if value < 0 else ""
    return sign + _written(int(abs(value)) * _SECOND_MS, 0)


def _finite(seconds: float) -> float:
    """The value as a float, refusing what is not a length of time."""
    value = float(seconds)
    if not math.isfinite(value):
        raise ValueError(f"not a duration: {seconds!r}")
    return value


def _written(total_ms: int, places: int) -> str:
    """The unsigned duration in whole milliseconds, in words.

    One place decides the units and their spelling for both forms above;
    ``places`` is only how many decimals of a second are written, and 0
    writes none.
    """
    days, rest = divmod(total_ms, _DAY_MS)
    hours, rest = divmod(rest, _HOUR_MS)
    minutes, rest = divmod(rest, _MINUTE_MS)
    whole, milliseconds = divmod(rest, _SECOND_MS)
    parts: list[str] = []
    for amount, unit in ((days, "d"), (hours, "h"), (minutes, "m")):
        if parts:
            parts.append(f"{amount:02d}{unit}")
        elif amount:
            parts.append(f"{amount}{unit}")
    second = f"{whole:02d}" if parts else str(whole)
    if places:
        second = f"{second}.{milliseconds:0{places}d}"
    parts.append(f"{second}s")
    return " ".join(parts)


def human_duration_or_none(seconds: float | None) -> str | None:
    """:func:`human_duration`, passing through the ``None`` of a duration
    that was never measured (a step that has not finished, a run with no
    recorded end), so a caller can carry the pair of fields unconditionally.
    """
    return None if seconds is None else human_duration(seconds)
