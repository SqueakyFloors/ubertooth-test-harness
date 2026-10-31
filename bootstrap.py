#!/usr/bin/env python3
"""Create or update the harness-owned Python 3.12 virtual environment.

Usage (any Python 3 can launch it; the environment itself is always 3.12):

    python3 bootstrap.py [--venv PATH] [--python PATH] [--recreate]
                         [--with-acroname]

Idempotent: an existing harness environment is reused and its pinned
requirements are reinstalled only when they are not already satisfied. It
never installs into a system, user, or another application's interpreter:
packages are installed only by the new environment's own
interpreter, with pip's virtualenv guard on and the user site disabled, and an
existing directory without the harness marker is refused rather than adopted.

--with-acroname also installs the hash-checked requirements-acroname.txt
(Acroname's proprietary BrainStem API, needed only to switch an Acroname
USBHub3+); without it, an environment that already has it keeps it.

Standard library only, so it runs before any dependency is installed, and
compatible with Python 3.8+ launchers (such as macOS /usr/bin/python3).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REQUIRED_VERSION = (3, 12)
MARKER = ".ubertooth-harness-venv"
ROOT = Path(__file__).resolve().parent
COMMAND_TIMEOUT_S = 600
ACRONAME_REQUIREMENTS = "requirements-acroname.txt"


class BootstrapError(RuntimeError):
    """The harness environment cannot be created safely."""


def _run(argv, *, env=None, timeout=COMMAND_TIMEOUT_S, capture=True):
    try:
        return subprocess.run(
            [str(item) for item in argv],
            check=False,
            capture_output=capture,
            text=True,
            env=env,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise BootstrapError(f"cannot run {argv[0]}: {error}") from error
    except subprocess.TimeoutExpired as error:
        raise BootstrapError(
            f"{' '.join(str(item) for item in argv)} did not finish in {timeout}s"
        ) from error


_PROBE = (
    "import json, sys; print(json.dumps({"
    "'version': list(sys.version_info[:3]), "
    "'executable': sys.executable, "
    "'prefix': sys.prefix, "
    "'base_prefix': sys.base_prefix}))"
)


def probe(python) -> dict:
    result = _run([python, "-I", "-c", _PROBE], timeout=30)
    if result.returncode != 0:
        raise BootstrapError(
            f"{python} could not be run: {result.stderr.strip() or result.returncode}"
        )
    try:
        info = json.loads(result.stdout)
        version = [int(part) for part in info["version"]]
        info["version"] = version
        str(info["prefix"])
    except (ValueError, KeyError, TypeError) as error:
        raise BootstrapError(f"{python} is not a working Python interpreter") from error
    return info


def find_base_python(requested: str | None) -> str:
    """Return a Python 3.12 interpreter, or fail with how to install one."""
    candidates = [requested] if requested else []
    if not requested:
        if tuple(sys.version_info[:2]) == REQUIRED_VERSION:
            candidates.append(sys.executable)
        found = shutil.which("python3.12")
        if found:
            candidates.append(found)
    for candidate in candidates:
        info = probe(candidate)
        if tuple(info["version"][:2]) == REQUIRED_VERSION:
            return candidate
        if requested:
            version = ".".join(str(part) for part in info["version"])
            raise BootstrapError(
                f"{requested} is Python {version}; Python 3.12 is required"
            )
    raise BootstrapError(
        "Python 3.12 was not found. Install it (macOS: python.org installer or "
        "'brew install python@3.12'; Ubuntu 24.04: 'sudo apt install python3.12 "
        "python3.12-venv'), or pass --python PATH."
    )


def check_venv_support(python: str) -> None:
    result = _run([python, "-I", "-c", "import venv, ensurepip"], timeout=30)
    if result.returncode != 0:
        raise BootstrapError(
            f"{python} cannot create virtual environments (venv/ensurepip "
            "missing). On Debian/Ubuntu install python3.12-venv."
        )


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def ensure_venv(venv: Path, base_python: str, *, recreate: bool) -> bool:
    """Create the environment if needed; return True when it was created."""
    marker = venv / MARKER
    if venv.exists():
        if not marker.is_file():
            raise BootstrapError(
                f"{venv} exists but is not a harness environment (no {MARKER}); "
                "refusing to modify it. Remove it or choose --venv."
            )
        if recreate:
            shutil.rmtree(venv)
        else:
            python = venv_python(venv)
            if not python.exists():
                raise BootstrapError(
                    f"{venv} is a harness environment without an interpreter; "
                    "rerun with --recreate"
                )
            info = probe(python)
            if tuple(info["version"][:2]) != REQUIRED_VERSION:
                version = ".".join(str(part) for part in info["version"])
                raise BootstrapError(
                    f"{venv} uses Python {version}; rerun with --recreate"
                )
            return False
    result = _run([base_python, "-m", "venv", venv])
    if result.returncode != 0:
        raise BootstrapError(f"venv creation failed: {result.stderr.strip()}")
    marker.write_text(
        json.dumps({"created_by": "bootstrap.py", "root": str(ROOT)}) + "\n",
        encoding="utf-8",
    )
    return True


def install_requirements(venv: Path, requirements: Path, *, require_hashes: bool = False) -> None:
    python = venv_python(venv)
    info = probe(python)
    if Path(info["prefix"]).resolve() != venv.resolve():
        raise BootstrapError(
            f"{python} does not belong to {venv}; refusing to install packages"
        )
    env = dict(os.environ)
    env.update({
        "PIP_REQUIRE_VIRTUALENV": "1",
        "PIP_USER": "0",
        "PYTHONNOUSERSITE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    })
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    for argv in (
        [python, "-m", "pip", "install",
         *(["--require-hashes"] if require_hashes else []), "-r", requirements],
        [python, "-m", "pip", "check"],
    ):
        result = _run(argv, env=env, capture=False)
        if result.returncode != 0:
            raise BootstrapError(
                f"{' '.join(str(item) for item in argv[1:])} failed "
                f"(exit {result.returncode})"
            )


def main(argv=None) -> int:
    if sys.version_info < (3, 8):
        print("bootstrap: error: launch with Python 3.8 or newer", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--venv", type=Path, default=ROOT / ".venv")
    parser.add_argument("--python", help="Python 3.12 interpreter to build from")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete and rebuild an existing harness environment",
    )
    parser.add_argument(
        "--with-acroname",
        action="store_true",
        help="also install the Acroname BrainStem API (requirements-acroname.txt; "
        "proprietary, needed only for an Acroname USBHub3+)",
    )
    args = parser.parse_args(argv)
    try:
        base = find_base_python(args.python)
        check_venv_support(base)
        created = ensure_venv(args.venv, base, recreate=args.recreate)
        install_requirements(args.venv, ROOT / "requirements.txt")
        if args.with_acroname:
            install_requirements(
                args.venv, ROOT / ACRONAME_REQUIREMENTS, require_hashes=True
            )
    except BootstrapError as error:
        print(f"bootstrap: error: {error}", file=sys.stderr)
        return 1
    action = "created" if created else "updated"
    print(f"bootstrap: {action} {args.venv}")
    print(f"bootstrap: run the harness with {venv_python(args.venv)} -m ubertooth_harness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
