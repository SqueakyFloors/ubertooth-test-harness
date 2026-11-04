"""The checkout a bench helper belongs to, and the harness it must use.

A helper in this directory is a script inside a harness checkout, and the
harness it runs is that checkout's: the ``ubertooth_harness`` package beside
it, and the interpreter that checkout bootstrapped. Neither comes from the
caller, and neither may.

That is not fussiness. A bench wrapper that named another checkout's
interpreter, and ran from inside that checkout, is how one ``hold`` came to
read the bench registry with one version of the harness and take its leases
with a much older one: the older half refused a registry that was perfectly
good, and every device command on that host stopped working
([#312](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/312)).

Three separate things keep that from happening again, and the first is the
one that makes a bench wrapper unnecessary:

``run_in_checkout_environment`` re-runs the script under its own checkout's
``.venv`` interpreter when it was started with a different one, so a caller
never has to name an interpreter and cannot impose the wrong one. Plain
``python3 scripts/lab/hold ...``, or the script's own path, is enough. An
interpreter nobody bootstrapped is turned down rather than run with: the
harness has an environment of its own for a reason, and a helper that drives
devices is no place to go without it.

``CHILD_PYTHON_OPTIONS`` is what a helper passes an interpreter it starts for
``-m ubertooth_harness``. With ``-m``, Python puts the *working directory*
first on the child's import path, ahead of anything ``PYTHONPATH`` says, so a
helper run from inside another checkout would hand its work to that
checkout's harness. ``-P`` leaves the working directory off, and the caller
names its own checkout first on ``PYTHONPATH``, so that checkout answers the
import.

``harness_identity`` names the harness that read something, for the error
message of a registry a different version wrote. The registry is not wrong
in that case and the message should not read as though it were.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# scripts/lab/_checkout.py, so the checkout that owns these helpers.
CHECKOUT = Path(__file__).resolve().parents[2]
VENV = CHECKOUT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

# What bootstrap.py leaves in an environment it built, named here rather than
# imported: this module runs before the harness is importable, and importing
# the package under the wrong Python is itself an error. The two names are
# checked against each other by tests/test_lab_scripts.py.
VENV_MARKER = ".ubertooth-harness-venv"

# Set across the re-run and dropped the moment it has arrived, so a .venv
# whose interpreter does not report that environment as its own cannot make
# this loop while nothing further down inherits a spent note. It holds the
# checkout that re-ran rather than a flag, so one checkout's attempt is never
# read as another's.
REEXEC_ENV = "UBERTOOTH_HARNESS_LAB_REEXEC"

# For a child started as `-m ubertooth_harness`; see the module docstring.
CHILD_PYTHON_OPTIONS = ("-P",)

# Reading the checkout's commit is for a message, so it never delays a
# command for long and never fails one.
REVISION_TIMEOUT_S = 5


def in_checkout_environment() -> bool:
    """Is this interpreter the one this checkout bootstrapped?"""
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def in_harness_environment() -> bool:
    """Is this interpreter an environment bootstrap.py built, any checkout's?

    Not as good as the checkout's own, and good enough to run with: the
    package still comes from beside the script, so the two halves of a hold
    cannot be different versions of the harness. What this rules out is an
    interpreter nobody bootstrapped, which has whatever versions of the
    harness's dependencies happen to be installed, or none.
    """
    try:
        return (Path(sys.prefix) / VENV_MARKER).is_file()
    except OSError:
        return False


def run_in_checkout_environment(script: str, name: str | None = None) -> None:
    """Put `script` in a harness environment, its own checkout's for choice.

    A helper calls this when it is being run as a program, before it imports
    the harness. Three outcomes:

    - Already in this checkout's own environment, or in another bootstrapped
      one with no environment here to prefer: carry on.
    - This checkout has an environment and we are not in it: re-run the
      script under that interpreter. Once per checkout, which is what keeps
      a broken environment from looping, and per checkout rather than once
      overall, because a command run under `hold` may be a helper from
      somewhere else with its own re-run to make.
    - No environment anywhere: stop, and say which checkout to bootstrap.
      Carrying on would run the harness with whichever versions of its
      dependencies this interpreter happens to have.
    """
    if in_checkout_environment():
        # Arrived: the note is spent. Dropping it here is what keeps it from
        # reaching the command run under `hold` -- which may be a helper of
        # this same checkout, started by its shebang, with its own re-run to
        # make -- and what stops a stray value in the caller's environment
        # from standing in for an attempt.
        os.environ.pop(REEXEC_ENV, None)
        return
    if (VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK)
            and not already_tried()):
        os.environ[REEXEC_ENV] = str(CHECKOUT)
        try:
            os.execv(str(VENV_PYTHON), reexec_argv(script))
        except OSError:
            # A file that is executable and not an executable image. Nothing
            # has happened yet, so the refusal below is still the right end.
            os.environ.pop(REEXEC_ENV, None)
    if in_harness_environment():
        return
    raise SystemExit(bootstrap_advice(name or Path(script).name))


def already_tried(environ=None) -> bool:
    """Has this checkout re-run a helper in this process's ancestry?

    Another checkout's attempt is not this one's: a command run under `hold`
    may be a helper from elsewhere, and it has to reach its own environment.
    """
    tried = (os.environ if environ is None else environ).get(REEXEC_ENV, "")
    return tried == str(CHECKOUT)


def reexec_argv(script: str) -> list[str]:
    """The command line the re-run uses, with this script's own arguments."""
    return [
        str(VENV_PYTHON), *CHILD_PYTHON_OPTIONS,
        str(Path(script).resolve()), *sys.argv[1:],
    ]


def bootstrap_advice(name: str, error: BaseException | None = None) -> str:
    """Why a helper could not import the harness, and what to do about it.

    Usually the checkout has no usable environment of its own, since a
    helper started with another interpreter re-runs itself under that one
    first. So it names the checkout: a bench runs helpers from a path, often
    more than one checkout's, and "run bootstrap.py" without saying where is
    no help at all. The import's own words come too, because the other
    reason to land here is an import that is broken rather than missing.
    """
    advice = (
        f"{name}: cannot import the harness in {CHECKOUT}. If that checkout "
        f"has no environment yet, create it with "
        f"'python3 {CHECKOUT / 'bootstrap.py'}' and run this script again "
        "(it then uses that environment whichever interpreter starts it)."
    )
    return advice if error is None else f"{advice} The import said: {error}"


def revision() -> str | None:
    """The checkout's commit, short, or None when it cannot be read."""
    try:
        done = subprocess.run(
            ["git", "-C", str(CHECKOUT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=REVISION_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def harness_identity() -> str:
    """Which harness read something, for a message a stale one explains."""
    commit = revision()
    where = f"read by the harness in {CHECKOUT}"
    return where if commit is None else f"{where}, commit {commit}"
