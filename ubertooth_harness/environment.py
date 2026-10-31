"""Identity of the Python environment a run executes in."""

from importlib import metadata
import platform
import re
import sys
from pathlib import Path
from typing import Mapping


MARKER = ".ubertooth-harness-venv"
REQUIRED_VERSION = (3, 12)
REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


class HarnessEnvironmentError(RuntimeError):
    """The interpreter is not the harness-owned environment."""


def requirement_names(path: Path = REQUIREMENTS) -> tuple[str, ...]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.append(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0])
    return tuple(names)


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _host_tools() -> dict:
    """Versions of the host programs a case runs that are not the fork's.

    Only tshark so far (BLE extcap in tshark, harness issue #148): its
    dissector is what that test checks the plugin's records against, so which
    one read them belongs in the run manifest. None where it is not
    installed, which is where those cases are skipped.
    """
    from ubertooth_harness import tsharkcapture

    return {"tshark": tsharkcapture.version()}


def _os_identity() -> dict:
    identity = {
        "system": platform.system(),
        "release": platform.release(),
        "architecture": platform.machine(),
    }
    if sys.platform == "darwin":
        identity["macos_version"] = platform.mac_ver()[0]
    elif sys.platform.startswith("linux"):
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        identity["distribution"] = release.get("PRETTY_NAME")
    return identity


def capture() -> dict:
    """JSON-serializable identity of this interpreter and its packages."""
    prefix = Path(sys.prefix)
    return {
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
            "prefix": str(prefix),
            "virtual_environment": sys.prefix != sys.base_prefix,
            "harness_environment": (prefix / MARKER).is_file(),
        },
        "pip": _version("pip"),
        "dependencies": {name: _version(name) for name in requirement_names()},
        "host_tools": _host_tools(),
        "os": _os_identity(),
    }


def require_harness_environment(identity: Mapping | None = None) -> None:
    """Fail unless running in a bootstrapped Python 3.12 harness environment."""
    current = identity or capture()
    python = current["python"]
    if not python["harness_environment"]:
        raise HarnessEnvironmentError(
            f"{python['executable']} is not the harness environment; run "
            "'python3 bootstrap.py' and use .venv/bin/python -m ubertooth_harness"
        )
    major_minor = tuple(int(part) for part in python["version"].split(".")[:2])
    if major_minor != REQUIRED_VERSION:
        raise HarnessEnvironmentError(
            f"harness environment uses Python {python['version']}; "
            "rerun 'python3 bootstrap.py --recreate'"
        )
    missing = sorted(name for name, version in current["dependencies"].items() if not version)
    if missing:
        raise HarnessEnvironmentError(
            "harness environment is missing " + ", ".join(missing)
            + "; rerun 'python3 bootstrap.py'"
        )
