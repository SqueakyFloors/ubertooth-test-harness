"""Sample the identify blink with ``ubertooth-util -U SERIAL -d``.

Run as ``python <this file> TOOL SERIAL ...`` from a transition step, so it is
an ordinary logged command. It imports only the standard library:

    python ledblink.py TOOL SERIAL --max-samples N --deadline S
        [--interval S] [--jitter S] [--seed N]

It prints each ``-d`` report on stdout, as the shell LED sampler does, and
stops once a whole-on and a whole-off sample have both appeared, after N
samples, or at the deadline, whichever comes first. The checks in util_cases
decide pass or fail from the reports; this exits 0 unless a read fails twice
in a row (exit 1, the shell sampler's retry rule, SqueakyFloors/ubertooth#213).

Why Python and not sh: POSIX sh has no monotonic clock (``date +%s`` is wall
time in whole seconds) and no way to bound one command's run, so a read in
flight could overrun the deadline. Here the deadline is on time.monotonic()
and covers the reads too: a read still running at the deadline is stopped
with SIGTERM (a -d read holds no stream, so stopping it leaves no mode
running) and waited for, at most GRACE_S more. The worst case is therefore
deadline + GRACE_S, while ubertooth-util honors SIGTERM; the step timeout
still bounds a tool that does not.

The pause before each read is the interval plus a uniform random jitter. The
identify blink toggles every 167 ms (a 334 ms period); a -d read takes about
280 ms, so a fixed 50 ms pause samples once per blink period and can see one
phase only. A jitter of one blink period spreads the samples over the whole
period. The seed is printed so a run can be repeated.
"""

from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import time

GRACE_S = 2.0
BLINK_PERIOD_S = 0.334
_STATUS = re.compile(r"(?m)^(?:USR|RX|TX) LED status *: ([01])$")


def phase(report: str) -> str | None:
    """'on' or 'off' when all three LEDs agree, else None."""
    states = _STATUS.findall(report)
    if len(states) == 3 and len(set(states)) == 1:
        return "on" if states[0] == "1" else "off"
    return None


def run_read(argv, timeout_s):
    """(exit code, stdout) of one read; (None, stdout) when it was stopped."""
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    try:
        out, _ = process.communicate(timeout=max(timeout_s, 0.0))
        return process.returncode, out
    except subprocess.TimeoutExpired:
        process.terminate()  # SIGTERM only
        try:
            out, _ = process.communicate(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            out = ""
            sys.stderr.write(f"LED blink sampling: {argv[0]} still running "
                             f"{GRACE_S:g} s after SIGTERM\n")
        return None, out or ""


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def sample(tool, serial, *, max_samples, deadline_s, interval_s, jitter_s, rng,
           clock=time.monotonic, sleep=time.sleep, read=run_read,
           out=sys.stdout, err=sys.stderr) -> int:
    argv = (tool, "-U", serial, "-d")
    start = clock()
    end = start + deadline_s
    seen = set()
    samples = 0
    stop = "the sample bound"
    code = 0
    while samples < max_samples:
        pause = interval_s + rng.uniform(0.0, jitter_s)
        if clock() + pause >= end:
            stop = "the deadline"
            break
        sleep(pause)
        status, report = read(argv, end - clock())
        if status is None:
            stop = "the deadline (a read was stopped)"
            break
        if status != 0:
            err.write(f"LED sample {samples}: -d exited {status}, retrying\n")
            status, report = read(argv, end - clock())
            if status is None:
                stop = "the deadline (the retry was stopped)"
                break
            if status != 0:
                stop = f"a failed retry (exit {status})"
                code = 1
                break
        out.write(report if report.endswith("\n") else report + "\n")
        out.flush()
        samples += 1
        state = phase(report)
        if state:
            seen.add(state)
        if seen == {"on", "off"}:
            stop = "both phases"
            break
    err.write(f"LED blink sampling: {plural(samples, 'sample')} in "
              f"{clock() - start:.1f} s, stopped at {stop}\n")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledblink.py")
    parser.add_argument("tool")
    parser.add_argument("serial")
    parser.add_argument("--max-samples", type=int, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--jitter", type=float, default=BLINK_PERIOD_S)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)
    seed = random.randrange(2 ** 32) if args.seed is None else args.seed
    sys.stderr.write(f"LED blink sampling: seed {seed}\n")
    return sample(args.tool, args.serial, max_samples=args.max_samples,
                  deadline_s=args.deadline, interval_s=args.interval,
                  jitter_s=args.jitter, rng=random.Random(seed))


if __name__ == "__main__":
    sys.exit(main())
