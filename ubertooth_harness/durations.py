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
    value = float(seconds)
    if not math.isfinite(value):
        raise ValueError(f"not a duration: {seconds!r}")
    sign = "-" if value < 0 else ""
    total_ms = round(abs(value) * _MS)
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
    parts.append(f"{second}.{milliseconds:0{PLACES}d}s")
    return sign + " ".join(parts)


def human_duration_or_none(seconds: float | None) -> str | None:
    """:func:`human_duration`, passing through the ``None`` of a duration
    that was never measured (a step that has not finished, a run with no
    recorded end), so a caller can carry the pair of fields unconditionally.
    """
    return None if seconds is None else human_duration(seconds)
