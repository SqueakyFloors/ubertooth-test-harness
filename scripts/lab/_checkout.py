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

``CHILD_PYTHON_OPTIONS`` is for an interpreter a helper starts to run
``-m ubertooth_harness``, which ``hold`` does for ``ubertooth-harness
lease``. With ``-m``, Python puts the *working directory* first on the
child's import path, ahead of anything ``PYTHONPATH`` says, so a helper run
from inside another checkout would hand its work to that checkout's harness.
``-P`` leaves the working directory off, and the caller names its own
checkout first on ``PYTHONPATH``, so that checkout answers the import.
(``peer`` starts ``hold`` and ``esp32peer.py`` by path, where the working
directory is not on the import path to begin with, so they need none of
this.)

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
# this loop while nothing further down inherits a spent note. It names the
# process and the checkout, and `os.execv` keeps the process id, so the only
# note this reads is one it left itself: another process's, or a caller's own,
# cannot talk a helper out of its checkout's environment.
REEXEC_ENV = "UBERTOOTH_HARNESS_LAB_REEXEC"

# For a child started as `-m ubertooth_harness`; see the module docstring.
CHILD_PYTHON_OPTIONS = ("-P",)

# Reading the checkout's commit is for a message, so it never delays a
# command for long and never fails one.
REVISION_TIMEOUT_S = 5


def in_checkout_environment() -> bool:
    """Is this interpreter the one this checkout *bootstrapped*?

    Sitting at `.venv` is not enough. A plain `python -m venv .venv` put
    there by hand is a virtual environment and not a harness environment,
    and running a device helper in one is what `bootstrap.py` exists to
    avoid, so the marker decides here as it does anywhere else.
    """
    try:
        return (Path(sys.prefix).resolve() == VENV.resolve()
                and in_harness_environment())
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


def importable_checkout_path() -> bool:
    """Can this checkout's path be named on an import path at all?

    `PYTHONPATH` is a list separated by `os.pathsep`, so a checkout whose
    own path contains that character cannot be put on one: it would arrive
    as two entries, neither of them this checkout, and one of them possibly
    relative and so read from the caller's working directory. That is the
    #312 failure over again, by another route, and it is better refused
    than worked around.

    `hold` is the helper that needs it, for the delegate it starts; the
    other two reach the harness through their own `sys.path`. One rule for
    all three is deliberate, so that a checkout either serves as a bench's
    helpers or says plainly that it cannot, rather than two of three
    working and the one that takes the locks refusing.
    """
    return os.pathsep not in str(CHECKOUT)


def run_in_checkout_environment(script: str, name: str | None = None) -> None:
    """Put `script` in a harness environment, its own checkout's for choice.

    A helper calls this when it is being run as a program, before it imports
    the harness. A checkout whose path cannot be named on an import path is
    refused outright. Otherwise, three outcomes:

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
    label = name or Path(script).name
    if not importable_checkout_path():
        raise SystemExit(
            f"{label}: this checkout's path contains {os.pathsep!r}, which "
            "separates entries on an import path, so this checkout cannot be "
            f"named on one: {CHECKOUT}. Move the checkout somewhere without "
            "that character."
        )
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
        os.environ[REEXEC_ENV] = reexec_note()
        try:
            os.execv(str(VENV_PYTHON), reexec_argv(script))
        except OSError:
            # A file that is executable and not an executable image. Nothing
            # has happened yet, so the two outcomes below are still open:
            # another bootstrapped environment will do, and otherwise this
            # stops with the advice.
            os.environ.pop(REEXEC_ENV, None)
    if in_harness_environment():
        return
    raise SystemExit(bootstrap_advice(label))


def reexec_note() -> str:
    """What this process writes to say it has re-run itself once."""
    return f"{os.getpid()} {CHECKOUT}"


def already_tried(environ=None) -> bool:
    """Did *this* process already re-run itself for this checkout?

    `os.execv` keeps the process id, so a note worth believing names this
    pid and this checkout. One with another pid was left by some other
    process -- a command running under `hold`, which has its own re-run to
    make -- or set by a caller by hand, and either way it says nothing
    about what this process has done.
    """
    tried = (os.environ if environ is None else environ).get(REEXEC_ENV, "")
    return tried == reexec_note()


def reexec_argv(script: str) -> list[str]:
    """The command line the re-run uses, with this script's own arguments.

    `-u` is carried over where the caller asked for it: a hold can run for
    as long as its command does, and someone who asked for unbuffered output
    through a pipe should not lose it to the re-run. `PYTHONUNBUFFERED`
    needs no carrying, being in the environment the re-run inherits; the
    flag leaves no trace but this one.
    """
    options = list(CHILD_PYTHON_OPTIONS)
    if unbuffered():
        options.append("-u")
    return [
        str(VENV_PYTHON), *options,
        str(Path(script).resolve()), *sys.argv[1:],
    ]


def unbuffered() -> bool:
    """Was this interpreter started with `-u`?

    There is no flag in `sys.flags` for it; what it leaves behind is a
    standard output that writes through rather than buffering.
    """
    return bool(getattr(sys.stdout, "write_through", False))


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
