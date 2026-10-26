"""Compare the libbtbb a run builds with the one the Ubertooth fork pins.

The fork's `install.sh` names the libbtbb it is built and tested against
(`LIBBTBB_URL`, `LIBBTBB_REF`). The harness keeps its own copy of that pin in
`scripts/ci.sh` (#77: it never calls the fork's installer), so a fork bump
leaves the harness building the old commit until someone notices (#85).

Nothing here fails a build: a different libbtbb can be deliberate, so a
mismatch is a warning naming both commits. `run-suite` and `build` record the
check for the Ubertooth commit they resolve as a `libbtbb_pin` record in
run.jsonl (status passed, warning, unchecked or skipped; report.md shows it)
and print it on stderr unless SHOWN_ENV is set. `scripts/ci.sh build` runs
this module against the fork's main once, as a GitHub Actions warning, and
sets SHOWN_ENV so its build and run-suite steps do not print it again:

    python -m ubertooth_harness.libbtbbpin --ubertooth-source DIR \
        [--ubertooth-ref REF] --libbtbb-url URL --libbtbb-ref SHA
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys


_ASSIGNMENT = re.compile(
    r"""^(LIBBTBB_URL|LIBBTBB_REF)=(?:"([^"\n]*)"|'([^'\n]*)'|(\S+))\s*(?:#.*)?$""",
    re.M,
)
_HEX = re.compile(r"[0-9a-fA-F]{7,40}")

# Set (to anything) when the check was already shown for this build, so
# `build` and `run-suite` record it in run.jsonl without printing it again.
SHOWN_ENV = "UBERTOOTH_HARNESS_LIBBTBB_PIN_SHOWN"
EVENT = "libbtbb_pin"


class PinUnreadable(RuntimeError):
    """install.sh names a libbtbb pin this module cannot compare."""


@dataclass(frozen=True)
class ForkPin:
    url: str | None
    ref: str


def parse_install_sh(text: str) -> ForkPin | None:
    """The first top-level LIBBTBB_URL/LIBBTBB_REF assignments, or None
    when install.sh has no LIBBTBB_REF.

    Only unindented assignments count, so the option parser's
    `LIBBTBB_REF="${1:-}"` inside `case` is not mistaken for the pin. A pin
    built from a shell expansion (`$`) raises PinUnreadable.
    """
    found: dict[str, str] = {}
    for match in _ASSIGNMENT.finditer(text):
        name = match.group(1)
        value = next(g for g in match.groups()[1:] if g is not None)
        found.setdefault(name, value)
    ref = found.get("LIBBTBB_REF")
    if not ref:
        return None
    if "$" in ref:
        raise PinUnreadable(
            f"LIBBTBB_REF in install.sh is not a literal commit ({ref}); "
            "it cannot be compared"
        )
    return ForkPin(url=found.get("LIBBTBB_URL"), ref=ref)


def fork_pin_at(source: Path | str, ref: str) -> ForkPin | None:
    """The pin in install.sh at `ref` of a fork checkout, or None if absent.

    None covers a commit without install.sh or without the pin (fork commits
    from before the pin landed); git errors other than a missing path, and a
    pin that is not a literal ref, raise RuntimeError.
    """
    result = subprocess.run(
        ["git", "-C", str(source), "show", f"{ref}:install.sh"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        if "does not exist" in result.stderr or "exists on disk, but not in" in result.stderr:
            return None
        raise RuntimeError(
            f"cannot read install.sh at {ref} in {source}: {result.stderr.strip()}"
        )
    return parse_install_sh(result.stdout)


def _same_commit(a: str, b: str) -> bool:
    """Equal refs, or two hex commits where one is a prefix of the other."""
    if a == b:
        return True
    if _HEX.fullmatch(a) and _HEX.fullmatch(b):
        a, b = a.lower(), b.lower()
        return a.startswith(b) or b.startswith(a)
    return False


def drift_warning(
    pin: ForkPin | None,
    libbtbb_ref: str,
    *,
    libbtbb_commit: str | None = None,
    libbtbb_url: str | None = None,
    fork_label: str = "the Ubertooth fork",
) -> str | None:
    """A one-line warning when the libbtbb differs from the fork's pin.

    `libbtbb_ref` is the ref as given, `libbtbb_commit` the commit it
    resolved to (when known); either matching the pin is a match. The URL is
    compared only when both sides name one.
    """
    if pin is None:
        return None
    problems = []
    candidates = [libbtbb_ref] + ([libbtbb_commit] if libbtbb_commit else [])
    if not any(_same_commit(candidate, pin.ref) for candidate in candidates):
        built = libbtbb_commit or libbtbb_ref
        problems.append(f"builds libbtbb {built}, but {fork_label} pins {pin.ref}")
    if libbtbb_url and pin.url and libbtbb_url.rstrip("/") != pin.url.rstrip("/"):
        problems.append(f"clones libbtbb from {libbtbb_url}, but {fork_label} uses {pin.url}")
    if not problems:
        return None
    return "libbtbb pin differs from the fork's install.sh: " + "; ".join(problems)


@dataclass(frozen=True)
class PinCheck:
    """The outcome of comparing a run's libbtbb with the fork's pin.

    ``status``: passed (same commit), warning (differs), unchecked (the
    check could not run; ``message`` says why), or skipped (the fork commit
    has no pin to compare with).
    """

    status: str
    message: str
    ubertooth_commit: str
    libbtbb_ref: str
    libbtbb_commit: str
    fork_pin: ForkPin | None = None

    def details(self) -> dict:
        return {
            "message": self.message,
            "ubertooth_commit": self.ubertooth_commit,
            "libbtbb_ref": self.libbtbb_ref,
            "libbtbb_commit": self.libbtbb_commit,
            "fork_pin_ref": self.fork_pin.ref if self.fork_pin else None,
            "fork_pin_url": self.fork_pin.url if self.fork_pin else None,
        }


def check_sources(sources) -> PinCheck | None:
    """Compare resolved run sources (manifest.SourceRevision) with the pin.

    None when the run has no Ubertooth or no libbtbb source.
    """
    by_name = {source.name: source for source in sources}
    ubertooth, libbtbb = by_name.get("Ubertooth"), by_name.get("libbtbb")
    if ubertooth is None or libbtbb is None:
        return None
    label = f"Ubertooth {ubertooth.short_commit}"

    def result(status, message, pin=None):
        return PinCheck(status, message, ubertooth.commit, libbtbb.requested_ref,
                        libbtbb.commit, pin)

    try:
        pin = fork_pin_at(ubertooth.source_path, ubertooth.commit)
    except (OSError, RuntimeError) as error:
        return result("unchecked", f"libbtbb pin not checked: {error}")
    if pin is None:
        return result(
            "skipped", f"libbtbb pin not checked: {label} has no LIBBTBB_REF in install.sh"
        )
    warning = drift_warning(
        pin, libbtbb.requested_ref, libbtbb_commit=libbtbb.commit, fork_label=label,
    )
    if warning:
        return result("warning", warning, pin)
    return result(
        "passed", f"libbtbb {libbtbb.short_commit} matches the pin in {label} install.sh", pin
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ubertooth_harness.libbtbbpin",
        description="warn when a libbtbb pin differs from the fork's install.sh",
    )
    parser.add_argument("--ubertooth-source", type=Path, required=True)
    parser.add_argument("--ubertooth-ref", default="HEAD")
    parser.add_argument("--libbtbb-url")
    parser.add_argument("--libbtbb-ref", required=True)
    args = parser.parse_args(argv)
    label = f"Ubertooth {args.ubertooth_ref}"
    try:
        pin = fork_pin_at(args.ubertooth_source, args.ubertooth_ref)
    except RuntimeError as error:
        message = str(error)
    else:
        if pin is None:
            message = f"{label} has no LIBBTBB_REF in install.sh to compare with"
        else:
            message = drift_warning(
                pin, args.libbtbb_ref, libbtbb_url=args.libbtbb_url, fork_label=label
            )
            if message is None:
                print(f"libbtbb pin {args.libbtbb_ref} matches {label} install.sh")
                return 0
    # A warning, not a failure (#85); GitHub Actions shows it on the run page.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=libbtbb pin::{message}")
    print(f"warning: {message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
