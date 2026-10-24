"""Run the fork's Python tools for the Python-tool cases.

Run as a script by the harness (``python pytools.py <command> ...``), so a case
command can find the tool and interpreter of this run, and skip with a reason
instead of failing when the run has no Python-tool build:

- ``specan-ui [ARGS...]`` runs the ``ubertooth-specan-ui`` wrapper that
  ``run-suite --build --with-python`` installed, with no display. Its imports
  (numpy, qtpy and a Qt binding) run before argparse, so the interpreter is
  probed first; a missing module skips the case.
- ``extcap [ARGS...]`` runs ``host/python/extcap/btle-extcap.py`` exactly as it
  is in the resolved Ubertooth commit (CMake never installs it), with the
  --with-python interpreter or else ``python3``.

Exit status is the tool's; ``SKIP_EXIT`` with a ``SKIP: <reason>`` line means
the case cannot run on this host. The run sets the environment variables
below (cli.py and build.py).
"""

import os
import shutil
import subprocess
import sys
import tempfile

PYTHON_ENV = "UBERTOOTH_HARNESS_PYTHON"
PYTHON_TOOLS_ENV = "UBERTOOTH_HARNESS_PYTHON_TOOLS"
SOURCE_ENV = "UBERTOOTH_HARNESS_SOURCE"
COMMIT_ENV = "UBERTOOTH_HARNESS_COMMIT"
EXTCAP_PATH = "host/python/extcap/btle-extcap.py"
SKIP_EXIT = 77
TOOL_TIMEOUT_S = 20.0

# What specan-ui imports before argparse; any missing one skips the case.
_SPECAN_UI_PROBE = (
    "import numpy, qtpy\n"
    "from qtpy import QtCore, QtGui, QtWidgets\n"
    "print('numpy', numpy.__version__, 'qtpy', qtpy.__version__, qtpy.API_NAME)\n"
)


def _skip(reason: str) -> int:
    print(f"SKIP: {reason}")
    return SKIP_EXIT


def _no_display(environ) -> dict:
    """The environment without a display: -h must not need one."""
    env = dict(environ)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        env.pop(name, None)
    return env


def _run(argv, env) -> int:
    try:
        result = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"error: {argv[0]} did not exit within {TOOL_TIMEOUT_S:g}s", file=sys.stderr)
        return 124
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def specan_ui(args, environ=os.environ) -> int:
    python = environ.get(PYTHON_ENV)
    tools = environ.get(PYTHON_TOOLS_ENV)
    if not python or not tools:
        return _skip(
            "ubertooth-specan-ui is built only by run-suite --build --with-python"
        )
    env = _no_display(environ)
    try:
        probe = subprocess.run(
            (python, "-c", _SPECAN_UI_PROBE), env=env, capture_output=True,
            text=True, timeout=TOOL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print(f"error: importing numpy and qtpy in {python} hung", file=sys.stderr)
        return 124
    if probe.returncode != 0:
        lines = (probe.stderr or probe.stdout).strip().splitlines()
        return _skip(
            f"{python} cannot import numpy, qtpy and a Qt binding "
            f"(ubertooth-specan-ui needs them): {lines[-1] if lines else 'no output'}"
        )
    print(f"{python}: {probe.stdout.strip()}", file=sys.stderr)
    return _run((os.path.join(tools, "ubertooth-specan-ui"), *args), env)


def extcap(args, environ=os.environ) -> int:
    source = environ.get(SOURCE_ENV)
    commit = environ.get(COMMIT_ENV)
    if not source or not commit:
        print(
            f"error: {SOURCE_ENV} and {COMMIT_ENV} are not set; run the case "
            "with run-suite", file=sys.stderr,
        )
        return 2
    python = environ.get(PYTHON_ENV) or shutil.which("python3", path=environ.get("PATH"))
    if not python:
        return _skip("no python3 to run btle-extcap.py")
    try:
        script = subprocess.run(
            ("git", "-C", source, "show", f"{commit}:{EXTCAP_PATH}"),
            capture_output=True, timeout=TOOL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        print("error: git show timed out", file=sys.stderr)
        return 124
    if script.returncode != 0:
        print(
            f"error: cannot read {EXTCAP_PATH} at {commit[:7]}: "
            + script.stderr.decode(errors="replace").strip(),
            file=sys.stderr,
        )
        return 2
    print(f"{EXTCAP_PATH} at {commit[:7]}, run with {python}", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="btle-extcap-") as directory:
        path = os.path.join(directory, "btle-extcap.py")
        with open(path, "wb") as file:
            file.write(script.stdout)
        return _run((python, path, *args), dict(environ))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("specan-ui", "extcap"):
        print("usage: pytools.py {specan-ui|extcap} [ARGS...]", file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    return specan_ui(args) if command == "specan-ui" else extcap(args)


if __name__ == "__main__":
    sys.exit(main())
