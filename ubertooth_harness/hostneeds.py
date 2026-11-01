"""What a test or case needs from its host, and whether a host has it.

One declaration, ``Needs``, sits on a case (``CommandCase.needs``,
``StateCase.needs``) or a whole test (``TestDefinition.needs``, plus the plan's
``platforms:`` argument). It is resolved before anything runs, against the
``HostCapabilities`` of the host:

- the host platform, from the resource registry;
- the tools built only with BlueZ (libbluetooth): ubertooth-scan and
  ubertooth-follow. macOS has no BlueZ, so never; a Linux build of the run
  (``--build``, which configures with REQUIRE_ALL=ON) always has them: the
  build fails unless both are in the run prefix, so their cases are required;
  installed Linux tools have them when they are on the test PATH;
- the Python tools build: ``run-suite --build --with-python``;
- the host programs a case runs that nothing here installs (``HOST_TOOLS``,
  tshark so far): present on the host's PATH or not;
- a firmware build: the run's "test build" image (``--build --with-firmware``
  or ``--artifact "test build=PATH"``).

What a host lacks is not run, and is recorded as "not applicable on <host>:
<reason>", apart from skips and failures. Runtime skips stay only for what
cannot be known before a command runs (for example a built libubertooth that
predates UBERTOOTH_TEST_NO_DEVICES, or a Python interpreter without numpy).

``None`` in a capability means "not known yet" (plan validation, before any
run options): such a need counts as met, so only the platform can make a plan
entry fail planning.
"""

from __future__ import annotations

from dataclasses import dataclass
import shutil
from typing import Iterable, Mapping, Sequence

PLATFORMS = ("linux", "macos")
# Built by the fork only when libbluetooth is found (BlueZ, Linux only).
BLUEZ_TOOLS = ("ubertooth-scan", "ubertooth-follow")
# Programs a case runs that are not the fork's and that the harness does not
# install: present or not on the host (#148).
HOST_TOOLS = ("tshark",)
TEST_BUILD_LABEL = "test_build"
# The alternate `btbr` firmware image, which only the ubertooth-btbr case
# needs and only a run that asked for --with-firmware builds.
BTBR_BUILD_LABEL = "btbr_build"
_PLATFORM_NAMES = {"linux": "Linux", "macos": "macOS"}


@dataclass(frozen=True)
class Needs:
    """What a case or test needs from the host; the default needs nothing."""

    # Host platforms it runs on; () runs on every platform.
    platforms: tuple[str, ...] = ()
    # Tools built only with BlueZ that it runs.
    bluez_tools: tuple[str, ...] = ()
    # The Python tools build (run-suite --build --with-python).
    python_tools: bool = False
    # The run's firmware test build ("test build" image).
    firmware_build: bool = False
    # The run's alternate firmware image ("btbr build"), which no other test
    # needs: without this, a run carrying the test build alone would be
    # judged able to run the btbr case and then stop at case planning for
    # want of the image.
    btbr_build: bool = False
    # Host programs it runs that nothing here installs (HOST_TOOLS).
    host_tools: tuple[str, ...] = ()

    def __post_init__(self):
        unknown = [item for item in self.platforms if item not in PLATFORMS]
        if unknown:
            raise ValueError(f"unknown platform(s): {', '.join(unknown)}")
        other = [tool for tool in self.bluez_tools if tool not in BLUEZ_TOOLS]
        if other:
            raise ValueError(f"not a BlueZ-only tool: {', '.join(other)}")
        unknown_tools = [tool for tool in self.host_tools if tool not in HOST_TOOLS]
        if unknown_tools:
            raise ValueError(f"not a known host tool: {', '.join(unknown_tools)}")

    def __bool__(self) -> bool:
        return bool(self.platforms or self.bluez_tools or self.python_tools
                    or self.firmware_build or self.btbr_build
                    or self.host_tools)

    def describe(self) -> str:
        parts = []
        if self.platforms:
            parts.append("platform " + " or ".join(self.platforms))
        parts += [f"{tool} (BlueZ)" for tool in self.bluez_tools]
        parts += [f"{tool} installed" for tool in self.host_tools]
        if self.python_tools:
            parts.append("Python tools build")
        if self.firmware_build:
            parts.append("firmware build")
        if self.btbr_build:
            parts.append("btbr firmware image")
        return ", ".join(parts) or "nothing"


NO_NEEDS = Needs()


def tool_needs(tool: str) -> Needs:
    """The needs of a case that runs ``tool``."""
    return Needs(bluez_tools=(tool,)) if tool in BLUEZ_TOOLS else NO_NEEDS


def platform_needs(platforms: Sequence[str] | None) -> Needs:
    """The plan's ``platforms:`` argument as needs."""
    return Needs(platforms=tuple(platforms)) if platforms else NO_NEEDS


@dataclass(frozen=True)
class HostCapabilities:
    host: str
    platform: str
    # BlueZ-only tools this run has; None: not known yet.
    bluez_tools: frozenset | None = None
    python_tools: bool | None = None
    firmware_build: bool | None = None
    btbr_build: bool | None = None
    # The HOST_TOOLS this host has; None: not known yet.
    host_tools: frozenset | None = None
    # Where the tools come from: "build", "installed tools", or "not known".
    basis: str = "not known"

    def to_dict(self) -> dict:
        def known(value):
            return value if value is None or isinstance(value, bool) else sorted(value)
        return {
            "host": self.host,
            "platform": self.platform,
            "basis": self.basis,
            "bluez_tools": known(self.bluez_tools),
            "python_tools": self.python_tools,
            "firmware_build": self.firmware_build,
            "btbr_build": self.btbr_build,
            "host_tools": known(self.host_tools),
        }

    def describe(self) -> str:
        def flag(value):
            return "not known" if value is None else "yes" if value else "no"
        tools = ("not known" if self.bluez_tools is None
                 else ", ".join(sorted(self.bluez_tools)) or "none")
        host_tools = ("not known" if self.host_tools is None
                      else ", ".join(sorted(self.host_tools)) or "none")
        return (f"{self.host} ({self.platform}, {self.basis}): BlueZ tools {tools}; "
                f"Python tools build {flag(self.python_tools)}; "
                f"firmware build {flag(self.firmware_build)}; "
                f"btbr image {flag(self.btbr_build)}; "
                f"host tools {host_tools}")


def _platform_name(platform: str) -> str:
    return _PLATFORM_NAMES.get(platform, platform)


def unmet(needs: Needs, capabilities: HostCapabilities | None) -> str:
    """Why the host cannot run something with these needs, or '' if it can
    (or if the answer is not known yet)."""
    if capabilities is None or not needs:
        return ""
    platform = capabilities.platform
    if needs.platforms and platform not in needs.platforms:
        return (f"runs only on {' or '.join(_platform_name(p) for p in needs.platforms)}, "
                f"and {capabilities.host} is {_platform_name(platform)}")
    if capabilities.bluez_tools is not None:
        for tool in needs.bluez_tools:
            if tool in capabilities.bluez_tools:
                continue
            if platform != "linux":
                return (f"{tool} is built only with BlueZ (libbluetooth), which "
                        f"{_platform_name(platform)} does not have")
            return (f"{tool} is not in this run's tools ({capabilities.basis}); it is "
                    "built only when libbluetooth is found")
    if needs.python_tools and capabilities.python_tools is False:
        return "needs the Python tools build (run-suite --build --with-python)"
    if needs.firmware_build and capabilities.firmware_build is False:
        return ("needs a firmware build (run-suite --build --with-firmware, or "
                "--artifact \"test build=PATH\")")
    if needs.btbr_build and capabilities.btbr_build is False:
        return ("needs the alternate btbr firmware image (run-suite --build "
                "--with-firmware, or --artifact \"btbr build=PATH\")")
    if capabilities.host_tools is not None:
        for tool in needs.host_tools:
            if tool not in capabilities.host_tools:
                return f"{tool} is not installed on {capabilities.host}"
    return ""


def platform_capabilities(host: str, platform: str) -> HostCapabilities:
    """What the platform alone decides: no BlueZ tools except on Linux."""
    return HostCapabilities(
        host, platform, None if platform == "linux" else frozenset(), basis="not known")


def _installed_host_tools() -> frozenset:
    """The HOST_TOOLS on this host, from the environment's own PATH: that is
    what a case's command finds when it runs."""
    return frozenset(tool for tool in HOST_TOOLS if shutil.which(tool))


def _installed_bluez(platform: str, path: str | None) -> frozenset:
    if platform != "linux":
        return frozenset()
    return frozenset(tool for tool in BLUEZ_TOOLS if shutil.which(tool, path=path))


def planned_capabilities(
    host: str, platform: str, *, build: bool, with_python: bool, with_firmware: bool,
    artifact_labels: Iterable[str] = (), path: str | None = None,
    with_btbr: bool | None = None,
) -> HostCapabilities:
    """What a run with these options will have, before it builds anything.

    A Linux build configures with REQUIRE_ALL=ON, so it has every BlueZ tool
    or fails to build.
    """
    labels = set(artifact_labels)
    # Only a run that asked for --with-firmware builds the alternate image;
    # one that turned the firmware build on just to flash its boards does not.
    with_btbr = with_firmware if with_btbr is None else with_btbr
    if build:
        bluez = frozenset(BLUEZ_TOOLS) if platform == "linux" else frozenset()
    else:
        bluez = _installed_bluez(platform, path)
    return HostCapabilities(
        host, platform, bluez,
        python_tools=bool(build and with_python),
        firmware_build=bool(build and with_firmware) or TEST_BUILD_LABEL in labels,
        btbr_build=bool(build and with_btbr) or BTBR_BUILD_LABEL in labels,
        host_tools=_installed_host_tools(),
        basis="build" if build else "installed tools",
    )


def run_capabilities(
    host: str, platform: str, build: Mapping | None, artifact_names: Iterable[str],
    path: str | None = None,
) -> HostCapabilities:
    """What a run has, from its manifest: the build record and artifacts.

    A Linux build has every BlueZ tool: build.py fails the build unless both
    are in the run prefix (REQUIRE_ALL=ON), so their cases are required there
    and never left out as not applicable (#40).
    """
    names = set(artifact_names)
    labels = {name.lower().replace(" ", "_") for name in names}
    if build:
        bluez = frozenset(BLUEZ_TOOLS) if platform == "linux" else frozenset()
    else:
        bluez = _installed_bluez(platform, path)
    return HostCapabilities(
        host, platform, bluez,
        python_tools=bool(build and build.get("python")),
        firmware_build=TEST_BUILD_LABEL in labels,
        btbr_build=BTBR_BUILD_LABEL in labels,
        host_tools=_installed_host_tools(),
        basis="build" if build else "installed tools",
    )


@dataclass(frozen=True)
class NotApplicable:
    """A test or case left out because its host cannot run it."""

    host: str
    test_name: str
    reason: str
    # Empty when the whole test was left out for the host at planning.
    invocation_id: str = ""
    # Empty when the whole test (every case) was left out.
    case_id: str = ""
    case_name: str = ""

    def describe(self) -> str:
        what = self.test_name + (f" / {self.case_name}" if self.case_name else "")
        return f"{what}: not applicable on {self.host}: {self.reason}"

    def to_dict(self) -> dict:
        return {
            "host": self.host, "test_name": self.test_name,
            "invocation_id": self.invocation_id, "case_id": self.case_id,
            "case_name": self.case_name, "reason": self.reason,
        }


def split_cases(cases, capabilities: HostCapabilities | None):
    """(cases the host can run, [(case, reason)] it cannot)."""
    runnable, left_out = [], []
    for case in cases:
        reason = unmet(getattr(case, "needs", NO_NEEDS), capabilities)
        if reason:
            left_out.append((case, reason))
        else:
            runnable.append(case)
    return tuple(runnable), left_out
