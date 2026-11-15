"""Probe whether the built ubertooth-util refuses what `-V` would suppress.

Run as ``python <this file> probe [TOOL]`` from a transition step, so the
answer is an ordinary logged command. It imports only the standard library,
so it runs from any working directory, and prints one summary line:

    Version conflicts: yes (<tool>)
    Version conflicts: no: <reason>

`-V` is terminal: the tool prints the version answer and returns before any
report, setter or mode runs (ubertooth-util.c). Until
[ubertooth#1255](https://github.com/SqueakyFloors/ubertooth/issues/1255) a
command line such as `-V -B` therefore exits 0 with the suppressed option
dropped without a word, and `Util version conflicts` has nothing to measure:
the probe says `no` and the cases skip, as `Refused command lines` skips a
fork from before the no-devices switch.

The probe asks the behavior rather than reading the binary for the message,
so a build that refuses the combination in other words still reads as `yes`
and its cases then fail on the wording. A probe that looked for the message
would read such a build as `no` and skip silently, which is the one outcome
worth designing against: a fork that has the check and a harness that never
runs it.

It runs one command, ``env UBERTOOTH_TEST_NO_DEVICES=1 <tool> -V -B``, and
only after reading the libubertooth beside the tool for that switch
(`refusal.probe`), so no board can be opened by it: a fork from before the
switch (b6d8ede) is reported as `no` without running anything.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script: make the package importable for the library probe.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import refusal  # noqa: E402

# The option the probe pairs with `-V`: a report that takes no value, needs
# no board to be refused, and is the one ubertooth#1255 names first. One
# option decides for all of them, which is the probe's own limit: a build that
# refused every option but this one would read as `no` and skip the lot.
PROBE_OPTION = "-B"
# What the probed command must say about itself. The tool is given a deadline
# of its own: it opens no board here, so it has nothing to wait for.
PROBE_TIMEOUT_S = 10.0
# A refusal says so on standard error, and prints no part of the version
# answer: exit 1 on its own is also what a build stopped by something else
# gives.
_ERROR_LINE = re.compile(r"(?m)^error: ")
_VERSION_LINE = re.compile(r"(?m)^(?:Host tools version|Firmware version)")


def probe(tool: str = "ubertooth-util", path: str | None = None) -> str:
    """``yes (<tool>)`` when this build refuses `-V -B`, else ``no: <reason>``."""
    found = shutil.which(tool, path=path)
    if found is None:
        return f"no: {tool} is not installed in this run (not on PATH)"
    switch = refusal.probe(tool, path)
    if not switch.startswith("yes"):
        # The reason refusal.probe gives, which already names the library.
        return switch
    environment = dict(os.environ, UBERTOOTH_TEST_NO_DEVICES="1")
    command = (found, "-V", PROBE_OPTION)
    try:
        result = subprocess.run(
            command, env=environment, capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        return (f"no: {found} -V {PROBE_OPTION} did not finish in "
                f"{PROBE_TIMEOUT_S:g}s")
    except OSError as error:
        return f"no: {found} could not be run ({error.strerror})"
    if result.returncode == 0:
        return (f"no: {Path(found).name} -V {PROBE_OPTION} exits 0, so this "
                f"build drops {PROBE_OPTION} rather than refusing it "
                "(ubertooth#1255 is not in it)")
    if result.returncode != 1:
        return (f"no: {Path(found).name} -V {PROBE_OPTION} exited "
                f"{result.returncode}, which is neither the version answer "
                "nor a refusal")
    # Exit 1 is not enough on its own: a build that cannot list the USB bus,
    # or one stopped by something else entirely, exits 1 too, and arming the
    # cases on that would report a build's own trouble as 23 failed cases.
    # What says the command line was refused is a refusal: an error line, and
    # no part of the version answer printed before it.
    if not _ERROR_LINE.search(result.stderr or ""):
        return (f"no: {Path(found).name} -V {PROBE_OPTION} exited 1 without "
                "an error: line, so it was stopped by something other than "
                "the command line")
    if _VERSION_LINE.search(result.stdout or ""):
        return (f"no: {Path(found).name} -V {PROBE_OPTION} printed the "
                "version answer and then exited 1, so it did not refuse the "
                "command line")
    return f"yes ({found})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="utilconflict.py")
    sub = parser.add_subparsers(dest="command", required=True)
    probe_parser = sub.add_parser(
        "probe", help="does this build refuse the options -V would suppress")
    probe_parser.add_argument("tool", nargs="?", default="ubertooth-util")
    args = parser.parse_args(argv)
    print(f"Version conflicts: {probe(args.tool)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
