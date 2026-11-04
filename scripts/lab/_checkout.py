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
``python3 scripts/lab/hold ...``, or the script's own path, is enough.

``CHILD_PYTHON_OPTIONS`` is what a helper passes an interpreter it starts for
``-m ubertooth_harness``. With ``-m``, Python puts the *working directory*
first on the child's import path, ahead of anything ``PYTHONPATH`` says, so a
helper run from inside another checkout would hand its work to that
checkout's harness. ``-P`` leaves the working directory off.

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

# Set across the re-run, so a .venv whose interpreter does not report that
# environment as its own cannot make this loop.
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


def run_in_checkout_environment(script: str) -> None:
    """Re-run `script` under this checkout's own interpreter, if it must.

    A helper calls this when it is being run as a program, before it imports
    the harness. It does nothing when this interpreter is already the
    checkout's own, when the checkout has no bootstrapped environment (the
    caller then gets the helper's own advice to create one), and when a
    re-run has already happened, which is what keeps a broken environment
    from looping.
    """
    if in_checkout_environment() or os.environ.get(REEXEC_ENV):
        return
    if not (VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK)):
        return
    os.environ[REEXEC_ENV] = "1"
    os.execv(str(VENV_PYTHON), reexec_argv(script))


def reexec_argv(script: str) -> list[str]:
    """The command line the re-run uses, with this script's own arguments."""
    return [
        str(VENV_PYTHON), *CHILD_PYTHON_OPTIONS,
        str(Path(script).resolve()), *sys.argv[1:],
    ]


def bootstrap_advice(name: str) -> str:
    """Why a helper could not import the harness, and what to do about it.

    Reached only when the checkout has no usable environment of its own,
    since a helper started with another interpreter re-runs itself under
    that one first. So it names the checkout: a bench runs helpers from a
    path, often more than one checkout's, and "run bootstrap.py" without
    saying where is no help at all.
    """
    return (
        f"{name}: no harness environment in {CHECKOUT}. Create it with "
        f"'python3 {CHECKOUT / 'bootstrap.py'}' and run this script again "
        "(it then uses that environment whichever interpreter starts it)."
    )


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
