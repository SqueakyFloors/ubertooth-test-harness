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
devices is no place to go without it. The re-run happens only once that
interpreter has confirmed it would arrive, so there is no second attempt to
guard against and nothing a bench can arrange, deliberately or by accident,
that leaves a helper trying for ever instead of answering.

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

# scripts/lab/_checkout.py, so the directory of the helpers and the checkout
# that owns them.
_HERE = Path(__file__).resolve().parent
CHECKOUT = Path(__file__).resolve().parents[2]
VENV = CHECKOUT / ".venv"
VENV_PYTHON = VENV / "bin" / "python"

# What bootstrap.py leaves in an environment it built, named here rather than
# imported: this module runs before the harness is importable, and importing
# the package under the wrong Python is itself an error. The two names are
# checked against each other by tests/test_lab_scripts.py.
VENV_MARKER = ".ubertooth-harness-venv"

# For a child started as `-m ubertooth_harness`; see the module docstring.
CHILD_PYTHON_OPTIONS = ("-P",)

# Reading the checkout's commit is for a message, so it never delays a
# command for long and never fails one.
REVISION_TIMEOUT_S = 5

# Asking the checkout's interpreter what it is. Generous, because a cold
# interpreter on a loaded host is slow, and bounded, because a helper that
# cannot get an answer must refuse rather than wait for a bench.
PROBE_TIMEOUT_S = 30

# A second pair of braces under `venv_python_would_arrive`, and nothing more.
# The probe is what decides; this only says an attempt has been made, so that
# a file answering one way and behaving another cannot be tried for ever.
# Setting it by hand buys a caller a refusal, never a different harness, and
# it is dropped before any command runs.
ATTEMPT_ENV = "UBERTOOTH_HARNESS_LAB_ATTEMPTED"


def attempted_once(environ=None) -> bool:
    """Has a re-run for this checkout already been attempted?"""
    return (os.environ if environ is None else environ).get(
        ATTEMPT_ENV, ""
    ) == str(CHECKOUT)


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
      script under that interpreter, once that interpreter has said it
      would arrive, which is what makes the re-run the last thing that
      happens rather than something to guard against happening twice.
      Nothing is written to the environment, so the command run under
      `hold` inherits nothing of this.
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
        # Arrived, so the note is spent: the command run under `hold` may be
        # a helper of this same checkout with its own re-run to make.
        os.environ.pop(ATTEMPT_ENV, None)
        return
    usable = VENV_PYTHON.is_file() and os.access(VENV_PYTHON, os.X_OK)
    if usable and not attempted_once() and venv_python_would_arrive():
        os.environ[ATTEMPT_ENV] = str(CHECKOUT)
        # Nothing to clean up if this raises: a file that is executable and
        # not an executable image leaves the process as it was, with both
        # outcomes below still open.
        try:
            os.execv(str(VENV_PYTHON), reexec_argv(script))
        except OSError:
            del os.environ[ATTEMPT_ENV]
    if in_harness_environment():
        os.environ.pop(ATTEMPT_ENV, None)
        return
    if usable:
        # The checkout has an environment and the interpreter would not put
        # this script in it, which "run bootstrap.py" does not describe.
        raise SystemExit(unreachable_environment_advice(label))
    raise SystemExit(bootstrap_advice(label))


def venv_python_would_arrive() -> bool:
    """Would running `VENV_PYTHON` land a helper in this checkout's environment?

    This is the whole loop guard, and it is the reason there is no second
    attempt to prevent: the re-run happens only once it is known to be the
    last one. The question is put to that interpreter, as the same
    predicate in this same file, so the answer is what the re-run process
    will decide for itself a moment later: `True` means it returns at
    `in_checkout_environment` and does nothing further.

    Asked rather than assumed, and asked before the `os.execv` rather than
    after, because afterwards there is no process left to notice a wrong
    answer. A `bin/python` with no `pyvenv.cfg`, a `#!` wrapper, a
    `PYTHONEXECUTABLE` in the environment that makes an interpreter report a
    name that is not its own: each of those answers `False`, and a helper
    that cannot reach its own environment says so instead of trying again.

    What this cannot cover is a file that answers one way when asked and
    behaves another way when run. Nobody who can put such a file at
    `.venv/bin/python` needs it, since the same write access reaches this
    module, but a helper on a bench must end whatever it is given, so
    `attempted_once` bounds the attempts as well.
    """
    try:
        asked = subprocess.run(
            [
                str(VENV_PYTHON), "-P", "-c",
                # -P and an explicit directory: the answer must come from
                # this file, never from one in the caller's working
                # directory, which is the whole subject of #312.
                f"import sys; sys.path.insert(0, {str(_HERE)!r});"
                " import _checkout;"
                " print(_checkout.in_checkout_environment())",
            ],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if asked.returncode != 0:
        return False
    # The last line, not the whole output: an interpreter may have a
    # `sitecustomize` or a `.pth` of its own that prints at startup, and a
    # good environment must not be turned down over someone else's banner.
    # It still cannot say yes by accident, since the answer is printed last.
    lines = [line.strip() for line in asked.stdout.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "True"


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


def unreachable_environment_advice(name: str) -> str:
    """The checkout has an interpreter, and it would not arrive.

    A different thing from a checkout nobody bootstrapped, and the advice
    for that one would be wrong here: the environment may be perfectly
    good and `bootstrap.py` will not change the answer. What does: a
    `PYTHONEXECUTABLE` in the environment, which decides what an
    interpreter calls itself and which some launchers set; something in
    place of the interpreter rather than the interpreter; or an
    environment that needs rebuilding.
    """
    return (
        f"{name}: {VENV_PYTHON} did not confirm that it would run this "
        f"script in the harness environment in {CHECKOUT}, so it was not "
        "used. Check for PYTHONEXECUTABLE or PYTHONHOME in the environment, "
        f"check that {VENV_PYTHON} is that environment's own interpreter, "
        f"and rebuild it if need be with 'python3 "
        f"{CHECKOUT / 'bootstrap.py'} --recreate'."
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
