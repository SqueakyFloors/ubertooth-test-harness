"""Build and install resolved Ubertooth and libbtbb commits into a run prefix.

The source checkouts named by the run are never modified: each resolved commit
is cloned into the run directory, built out of tree, and installed into a
per-run prefix. Nothing is installed system-wide, and machine setup (system
packages, udev rules) stays a separate one-time step (the fork's install.sh).
"""

import contextlib
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
from typing import Callable, Mapping, Sequence

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.executor import CommandExecutor, CommandResult
from ubertooth_harness.manifest import FileIdentity, SourceRevision
from ubertooth_harness.preflight import HOST_VERSION_LABEL, host_version_patterns
from ubertooth_harness.pytools import PYTHON_ENV, PYTHON_TOOLS_ENV
from ubertooth_harness.rawlog import record_output
from ubertooth_harness.events import EventPublisher
from ubertooth_harness.hostneeds import BLUEZ_TOOLS


class BuildError(RuntimeError):
    """The resolved revision could not be built or installed."""


_INSTALL_HINTS = {
    "macos": "brew install cmake pkg-config libusb fftw",
    "linux": (
        "sudo apt install cmake build-essential pkg-config libusb-1.0-0-dev "
        "libbluetooth-dev libfftw3-dev"
    ),
}
_FIRMWARE_HINTS = {
    "macos": "brew install --cask gcc-arm-embedded",
    "linux": "sudo apt install gcc-arm-none-eabi make",
}


@dataclass(frozen=True)
class BuildCommand:
    step_id: str
    name: str
    argv: tuple[str, ...]
    timeout_s: float = 900.0
    # Directory the command runs in. CMake configure runs inside the checkout
    # because the projects' version stamps run `git status` in the current
    # directory, not the source directory.
    cwd: Path | None = None
    # The same question on the option an older fork commit answers it on, run
    # only when `argv` does not answer (verify-host-version below).
    fallback_argv: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BuildPlan:
    work_dir: Path
    prefix: Path
    platform: str
    ubertooth: SourceRevision
    libbtbb: SourceRevision
    with_firmware: bool
    host_cmake_flags: tuple[str, ...]
    commands: tuple[BuildCommand, ...]
    # Interpreter the Python tools are built for (ENABLE_PYTHON=ON), or None.
    python: str | None = None

    @property
    def bin_dir(self) -> Path:
        return self.prefix / "bin"

    @property
    def firmware_image(self) -> Path:
        return (
            self.work_dir / "src" / "ubertooth" / "firmware" / "bluetooth_rxtx"
            / "bluetooth_rxtx.dfu"
        )

    def environment(self, base: Mapping[str, str] | None = None) -> dict:
        """Process environment for build commands and for testing the build."""
        env = dict(os.environ if base is None else base)
        pkg = str(self.prefix / "lib" / "pkgconfig")
        env["PKG_CONFIG_PATH"] = os.pathsep.join(
            item for item in (pkg, env.get("PKG_CONFIG_PATH", "")) if item
        )
        env["LIBBTBB_DIR"] = str(self.prefix)
        env["PATH"] = os.pathsep.join(
            item for item in (str(self.bin_dir), env.get("PATH", "")) if item
        )
        if self.python:
            # The Python-tool cases (pytools.py) run this interpreter and the
            # wrappers in the prefix; without these they skip.
            env[PYTHON_ENV] = self.python
            env[PYTHON_TOOLS_ENV] = str(self.bin_dir)
        else:
            env.pop(PYTHON_ENV, None)
            env.pop(PYTHON_TOOLS_ENV, None)
        return env


@dataclass(frozen=True)
class BuildResult:
    plan: BuildPlan
    toolchain: Mapping[str, str]
    host_version: str
    artifacts: tuple[FileIdentity, ...]
    commands: tuple[Mapping, ...] = field(default=())

    def to_manifest(self) -> dict:
        return {
            "prefix": str(self.plan.prefix),
            "platform": self.plan.platform,
            "with_firmware": self.plan.with_firmware,
            "python": self.plan.python,
            "host_cmake_flags": list(self.plan.host_cmake_flags),
            "toolchain": dict(self.toolchain),
            "host_version": self.host_version,
            "commands": [dict(item) for item in self.commands],
        }


# Host tools every build must install into the run prefix (the catalog cases
# with no host need). A Linux build must also install BLUEZ_TOOLS, and a
# --with-python build PYTHON_TOOLS; required_tools() is the full rule.
BUILT_TOOLS = (
    "ubertooth-util", "ubertooth-debug", "ubertooth-specan", "ubertooth-rx",
    "ubertooth-tx", "ubertooth-dump", "ubertooth-btle", "ubertooth-dfu",
    "ubertooth-ego", "ubertooth-afh", "ubertooth-ducky",
)
# Installed by ENABLE_PYTHON=ON; their first line names the interpreter.
PYTHON_TOOLS = ("ubertooth-specan-ui", "ubertooth-btbr")
BUILD_DEADLINE_S = 3600.0


def required_tools(plan: BuildPlan) -> tuple[str, ...]:
    """Tools the build must install into the run prefix.

    A Linux build configures with REQUIRE_ALL=ON, so the BlueZ-only tools are
    required there too: without them a case would be left out as not
    applicable, hiding the build problem, or a stale installed copy later on
    PATH could run in their place (#40).
    """
    return (
        BUILT_TOOLS
        + (BLUEZ_TOOLS if plan.platform == "linux" else ())
        + (PYTHON_TOOLS if plan.python else ())
    )


def resolve_python(value: str, which: Callable[[str], str | None] = shutil.which) -> str:
    """Absolute path of the interpreter named by --with-python.

    Symlinks are kept: a virtual environment's python is a symlink, and
    resolving it would lose that environment's packages.
    """
    found = which(value)
    if found is None:
        raise BuildError(f"--with-python: no Python interpreter {value!r} found")
    return os.path.abspath(found)


def missing_tools(
    platform: str,
    with_firmware: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    # make: CMake's default generator on macOS and Linux drives every build.
    tools = ["git", "cmake", "make", "pkg-config", "cc"]
    if with_firmware:
        tools += ["arm-none-eabi-gcc"]
    return [tool for tool in tools if which(tool) is None]


def require_tools(platform: str, with_firmware: bool, which=shutil.which) -> None:
    missing = missing_tools(platform, with_firmware, which)
    if not missing:
        return
    hints = [_INSTALL_HINTS.get(platform, "")]
    if with_firmware and "arm-none-eabi-gcc" in missing:
        hints.append(_FIRMWARE_HINTS.get(platform, ""))
    raise BuildError(
        "missing build tool(s): " + ", ".join(missing)
        + "; install with: " + " && ".join(hint for hint in hints if hint)
    )


def plan_build(
    *,
    ubertooth: SourceRevision,
    libbtbb: SourceRevision,
    work_dir: Path,
    platform: str,
    with_firmware: bool = False,
    extra_include_dir: str | None = None,
    jobs: int | None = None,
    python: str | None = None,
) -> BuildPlan:
    if platform not in ("macos", "linux"):
        raise BuildError(f"unsupported build platform: {platform}")
    work = Path(work_dir).resolve()
    prefix = work / "prefix"
    jobs = jobs or os.cpu_count() or 2
    common = (
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={prefix}",
        f"-DCMAKE_PREFIX_PATH={prefix}",
        f"-DCMAKE_INSTALL_RPATH={prefix / 'lib'}",
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=ON",
    )
    if python:
        # Python3_EXECUTABLE makes FindPython3 use exactly this interpreter;
        # the wrappers' first line then names it.
        python_flags = ("-DENABLE_PYTHON=ON", f"-DPython3_EXECUTABLE={python}")
    else:
        python_flags = ("-DENABLE_PYTHON=OFF",)
    host_flags = common + python_flags + (
        "-DINSTALL_UDEV_RULES=OFF",
    )
    if platform == "linux":
        # BlueZ (libbluetooth) exists only on Linux; elsewhere REQUIRE_ALL fails.
        host_flags += ("-DREQUIRE_ALL=ON",)
    if extra_include_dir:
        # Homebrew's libusb headers are not on the default include path. The
        # run prefix still wins: CMake places target includes before C_FLAGS,
        # and CMAKE_PREFIX_PATH makes FindBTBB prefer the prefix's libbtbb.
        host_flags += (f"-DCMAKE_C_FLAGS=-I{extra_include_dir}",)

    commands: list[BuildCommand] = []
    for source in (libbtbb, ubertooth):
        name = source.name.lower()
        checkout = work / "src" / name
        commands += [
            BuildCommand(
                f"{name}-clone",
                f"Clone {source.name} source",
                ("git", "clone", "--quiet", "--no-checkout", "--no-hardlinks",
                 source.source_path, str(checkout)),
                timeout_s=300.0,
            ),
            BuildCommand(
                f"{name}-checkout",
                f"Check out {source.name} {source.short_commit}",
                ("git", "-C", str(checkout), "checkout", "--quiet", "--detach",
                 source.commit),
                timeout_s=120.0,
            ),
            # The fork's version stamp uses `git diff-index`, which does not
            # refresh file stat data; a fresh checkout can then look modified
            # and stamp the build "git-<sha>*".
            BuildCommand(
                f"{name}-refresh-index",
                f"Refresh {source.name} file index",
                ("git", "-C", str(checkout), "update-index", "-q", "--refresh"),
                timeout_s=120.0,
            ),
            BuildCommand(
                f"{name}-verify-clean",
                f"{source.name} checkout is unmodified",
                ("git", "-C", str(checkout), "diff-index", "--quiet", "HEAD", "--"),
                timeout_s=120.0,
            ),
        ]
    btbb_src = work / "src" / "libbtbb"
    btbb_build = work / "build" / "libbtbb"
    commands += [
        BuildCommand(
            "libbtbb-configure",
            "Configure libbtbb",
            # ENABLE_PYTHON=OFF skips libbtbb's pcaptools: the Ubertooth tools
            # link only its C library, and building those tools would add a
            # pip install into the run prefix that no test uses. The pinned
            # libbtbb fork commit declares a cmake_minimum_required range, so
            # CMake 4 needs no CMAKE_POLICY_VERSION_MINIMUM here.
            ("cmake", "-S", str(btbb_src), "-B", str(btbb_build)) + common + (
                "-DENABLE_PYTHON=OFF",
            ),
            cwd=btbb_src,
        ),
        BuildCommand(
            "libbtbb-build", "Build libbtbb",
            ("cmake", "--build", str(btbb_build), "-j", str(jobs)),
        ),
        BuildCommand(
            "libbtbb-install", "Install libbtbb into the run prefix",
            ("cmake", "--install", str(btbb_build)),
        ),
    ]
    host_src = work / "src" / "ubertooth" / "host"
    host_build = work / "build" / "ubertooth-host"
    commands += [
        BuildCommand(
            "host-configure",
            "Configure Ubertooth host tools",
            ("cmake", "-S", str(host_src), "-B", str(host_build)) + host_flags,
            cwd=host_src,
        ),
        BuildCommand(
            "host-build", "Build Ubertooth host tools",
            ("cmake", "--build", str(host_build), "-j", str(jobs)),
        ),
        BuildCommand(
            "host-install", "Install host tools into the run prefix",
            ("cmake", "--install", str(host_build)),
        ),
    ]
    if with_firmware:
        commands.append(
            BuildCommand(
                "firmware-build",
                "Build bluetooth_rxtx firmware",
                ("make", "-C",
                 str(work / "src" / "ubertooth" / "firmware" / "bluetooth_rxtx"),
                 f"DFU_TOOL={prefix / 'bin' / 'ubertooth-dfu'}"),
            )
        )
    commands.append(
        BuildCommand(
            "verify-host-version",
            "Built host tools report the resolved revision",
            (str(prefix / "bin" / "ubertooth-util"), "-V"),
            timeout_s=30.0,
            fallback_argv=(str(prefix / "bin" / "ubertooth-util"), "-H"),
        )
    )
    return BuildPlan(
        work, prefix, platform, ubertooth, libbtbb, with_firmware,
        host_flags, tuple(commands), python,
    )


_VERSION_COMMANDS = (
    ("cmake", ("cmake", "--version")),
    ("cc", ("cc", "--version")),
    ("pkg-config", ("pkg-config", "--version")),
    ("libusb-1.0", ("pkg-config", "--modversion", "libusb-1.0")),
)


def _host_version_matches(plan: BuildPlan, text: str) -> bool:
    """Does `text` carry the host version line of the plan's resolved sources?"""
    return all(
        re.search(pattern, text)
        for pattern in host_version_patterns(plan.ubertooth, plan.libbtbb)
    )


def _host_version_line(text: str) -> str:
    """The one line the version patterns matched.

    What the manifest and the report call the built tools' version, so the
    firmware lines `-V` prints under it, which belong to whatever board
    happened to be attached, stay out of that field.
    """
    for line in text.splitlines():
        if line.startswith(HOST_VERSION_LABEL) or line.startswith("libubertooth"):
            return line
    return text


# A step's or the build's reason is a sentence, and it lands in a one-line
# marker in the transcript. The bound is a backstop: a reason built from a
# tool's output or from a cancellation message someone else wrote must still
# fit the record. It is well under the limit a case's reason uses
# (sequencer._MAX_REASON_CHARS), because this one is a marker, not a report.
_MAX_MARKER_REASON_CHARS = 500


def _one_line_reason(reason: object) -> str:
    """One bounded line, whatever the reason was built from.

    A line break becomes "; " rather than a space, so a reason built from
    several lines still reads as several statements. There is nowhere else to
    name for the rest of a reason this long: the step's and the build's
    markers are both bounded, and the command's own output is in the
    transcript above them.
    """
    text = "; ".join(
        " ".join(line.split())
        for line in str(reason).splitlines() if line.strip()
    )
    if len(text) <= _MAX_MARKER_REASON_CHARS:
        return text
    marker = " ... [truncated]"
    return text[:_MAX_MARKER_REASON_CHARS - len(marker)] + marker


def _first_line(result: CommandResult) -> str:
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0] if text else ""


class Builder:
    """Run a build plan with hard timeouts, recording every command in the run."""

    def __init__(
        self,
        executor: CommandExecutor,
        publisher: EventPublisher,
        *,
        run_dir: Path,
        cancellation: CancellationToken | None = None,
        deadline_s: float = BUILD_DEADLINE_S,
        clock: Callable[[], float] | None = None,
    ):
        import time

        self.executor = executor
        self.publisher = publisher
        # The run directory: a command whose output passes the run.jsonl
        # record limits keeps its full output in output/<command id>.log here.
        self.run_dir = Path(run_dir)
        self.cancellation = cancellation or CancellationToken()
        self.deadline_s = deadline_s
        self.clock = clock or time.monotonic
        self._deadline = None

    @contextlib.contextmanager
    def _step(self, step_id: str, name: str):
        """One build step, bracketed by its own started and finished records.

        A step's outcome is not the outcome of its commands: it may accept a
        command that exited non-zero, and may run a second command when the
        first did not answer (verify-host-version below). So the step says
        what it decided itself, in ``build_step_finished``, which is the line
        report.md and the GUI show. Set ``status`` and ``reason`` on the
        yielded outcome to say more than "passed"; a BuildError raised inside
        is the step's failure and its reason. Reasons stay to one line: the
        marker they land in is one line.

        Anything that is not a BuildError (an executor OSError, Ctrl-C in a
        place nothing handles) leaves the step open with no record, as it
        leaves the build itself without a ``build_finished``; report.md then
        says the step did not finish, which is what happened. A canceled
        build reaches here as a BuildError, so its step reads failed with the
        cancellation as its reason, as ``build_finished`` does.
        """
        self.publisher.emit(
            "build_step_started", "running",
            {"step_id": step_id, "step_name": name},
        )
        outcome = {"status": "passed", "reason": ""}
        try:
            yield outcome
        except BuildError as error:
            self._finish_step(step_id, name, "failed", str(error))
            raise
        self._finish_step(step_id, name, outcome["status"], outcome["reason"])

    def _finish_step(self, step_id: str, name: str, status: str, reason: str) -> None:
        text = _one_line_reason(reason)
        # A BuildError names the step it came from, because the BUILD marker
        # needs it; inside the step's own marker that says the name twice.
        for prefix in (f"{name}: ", f"{name} "):
            if text.startswith(prefix):
                text = text[len(prefix):]
                break
        self.publisher.emit(
            "build_step_finished", status,
            {"step_id": step_id, "step_name": name, "reason": text},
        )

    def _run(self, argv: Sequence[str], timeout_s: float, label: str, cwd=None):
        owner = {"phase": "build", "step_id": label}
        command_id = f"build-{label}"
        self.publisher.emit(
            "command_started", "running",
            {**owner, "command_id": command_id, "argv": list(argv),
             "cwd": str(cwd) if cwd else None},
        )
        if self.cancellation.is_cancelled():
            raise BuildError(f"build canceled: {self.cancellation.reason}")
        if self._deadline is not None:
            remaining = self._deadline - self.clock()
            if remaining <= 0:
                raise BuildError(f"build exceeded {self.deadline_s:.0f}s")
            timeout_s = min(timeout_s, remaining)
        options = {
            "timeout_s": timeout_s,
            "cancellation": self.cancellation,
        }
        if cwd is not None:
            options["cwd"] = str(cwd)
        result, spilled = record_output(
            self.publisher,
            lambda on_output: self.executor.run(argv, on_output=on_output, **options),
            details={**owner, "command_id": command_id},
            output_dir=self.run_dir,
            spill_name=command_id,
        )
        status = (
            "canceled" if result.cancelled
            else "passed" if result.returncode == 0 and not result.timed_out
            else "failed"
        )
        self.publisher.emit(
            "command_finished", status,
            {**owner, "command_id": command_id, "returncode": result.returncode,
             "duration_s": result.duration_s, "timed_out": result.timed_out,
             "cancelled": result.cancelled, **spilled},
        )
        return result

    def toolchain(self, plan: BuildPlan) -> dict:
        versions = {}
        commands = list(_VERSION_COMMANDS)
        if plan.platform == "linux":
            commands.append(("bluez", ("pkg-config", "--modversion", "bluez")))
        if plan.with_firmware:
            commands.append(("arm-none-eabi-gcc", ("arm-none-eabi-gcc", "--version")))
        if plan.python:
            commands.append(("python", (
                plan.python, "-c",
                "import platform, sys; "
                "print('Python', platform.python_version(), sys.executable)",
            )))
        missing_libraries = []
        with self._step("toolchain", "Read toolchain versions"):
            for name, argv in commands:
                result = self._run(argv, 30.0, f"toolchain-{name}")
                if result.returncode != 0 or result.timed_out:
                    if argv[0] == "pkg-config" and len(argv) > 2:
                        missing_libraries.append(argv[-1])
                        continue
                    raise BuildError(
                        f"cannot read the {name} version: {_first_line(result)}"
                    )
                versions[name] = _first_line(result)
            if missing_libraries:
                hint = _INSTALL_HINTS.get(plan.platform, "")
                raise BuildError(
                    "missing library: " + ", ".join(missing_libraries)
                    + (f"; install with: {hint}" if hint else "")
                )
        return versions

    def run(self, plan: BuildPlan) -> BuildResult:
        """Build the plan; build_started/build_finished bracket its commands."""
        self.publisher.emit(
            "build_started", "running",
            {"prefix": str(plan.prefix), "platform": plan.platform,
             "with_firmware": plan.with_firmware,
             # Which optional parts the build includes, so a reader of the
             # records, and the learned build estimate, can tell how much
             # work this build was. The interpreter itself is in the build
             # manifest; what changes the duration is building for one at all.
             "with_python": bool(plan.python)},
        )
        try:
            result = self._run_plan(plan)
        except BuildError as error:
            self.publisher.emit(
                "build_finished", "failed",
                # The BUILD marker is one line too, and a cancellation reason
                # comes from whoever asked for it.
                {"reason": _one_line_reason(error)},
            )
            raise
        self.publisher.emit(
            "build_finished", "passed",
            {"reason": "", "host_version": result.host_version},
        )
        return result

    def _run_plan(self, plan: BuildPlan) -> BuildResult:
        self._deadline = self.clock() + self.deadline_s
        plan.work_dir.mkdir(parents=True, exist_ok=True)
        records = []
        toolchain = self.toolchain(plan)
        host_version = ""

        def run_step(command: BuildCommand, argv, step_id: str) -> CommandResult:
            result = self._run(argv, command.timeout_s, step_id, command.cwd)
            records.append({
                "step_id": step_id,
                "argv": list(argv),
                "cwd": str(command.cwd) if command.cwd else None,
                "returncode": result.returncode,
                "duration_s": round(result.duration_s, 3),
            })
            if result.cancelled:
                raise BuildError(f"build canceled: {self.cancellation.reason}")
            return result

        for command in plan.commands:
            with self._step(command.step_id, command.name) as step:
                result = run_step(command, command.argv, command.step_id)
                if command.step_id == "verify-host-version":
                    host_version = (result.stdout + result.stderr).strip()
                    tried = [(command.argv, result)]
                    if (command.fallback_argv and not result.timed_out
                            and HOST_VERSION_LABEL not in host_version):
                        # Nothing answered under the label, so this commit of
                        # the fork answers the host version on the older
                        # option: ask that one. A version under the label that
                        # is not the resolved one is a failure, reported
                        # below, and so is a command killed at its deadline:
                        # the fallback answering afterwards would hide that
                        # the first command hung.
                        result = run_step(command, command.fallback_argv,
                                          f"{command.step_id}-fallback")
                        host_version = (result.stdout + result.stderr).strip()
                        tried.append((command.fallback_argv, result))
                    # Each command that did not exit 0, named by the option
                    # asked, for the step's own reason line either way.
                    failures = "; ".join(
                        f"{argv[-1]} "
                        + ("timed out" if attempt.timed_out
                           else f"exited {attempt.returncode}")
                        for argv, attempt in tried
                        if attempt.timed_out or attempt.returncode != 0
                    )
                    # `-V` goes on to read any attached board and exits 1 if
                    # one cannot be read -- busy, or held by another run. The
                    # built tools' own version is what this step checks, so
                    # the line decides it and the exit status does not, and
                    # the step says which exit status it accepted. A command
                    # killed at its deadline is still a failed step: it
                    # answered and then hung.
                    if _host_version_matches(plan, host_version) and not result.timed_out:
                        host_version = _host_version_line(host_version)
                        if failures:
                            # Which option answered, so a fallback that
                            # answered is not read as a busy board.
                            step["reason"] = (
                                "the version line is the resolved revision, "
                                f"from {tried[-1][0][-1]}; the exit status is "
                                f"not what this step reads ({failures})"
                            )
                        continue
                    # Either command may be the real problem, and the reason
                    # line is what an operator reads.
                    if failures:
                        raise BuildError(f"{command.name}: {failures}")
                    # Both answered, with a version this build did not ask
                    # for; _run_plan raises below with both revisions, and
                    # this is the last of plan.commands (plan_build), so no
                    # step runs after one that failed here. The
                    # reason carries one line, not the whole of -V: the
                    # firmware lines it prints under the version would break
                    # the step's own marker in the transcript.
                    reported = _host_version_line(host_version).splitlines()
                    step["status"] = "failed"
                    step["reason"] = (
                        "not the resolved revision: "
                        + (reported[0] if reported else "no version line")
                    )
                    continue
                if result.timed_out or result.returncode != 0:
                    reason = (
                        "timed out" if result.timed_out
                        else f"exited {result.returncode}"
                    )
                    raise BuildError(f"{command.name} {reason}")
        if not _host_version_matches(plan, host_version):
            # What was expected first: the version read can be long enough to
            # be cut by the reason bound, and it is in the transcript anyway.
            raise BuildError(
                f"built ubertooth-util expected {plan.ubertooth.expected_revision} "
                f"and {plan.libbtbb.expected_revision}, reports {host_version!r}"
            )
        expected_tools = required_tools(plan)
        missing = [tool for tool in expected_tools if not (plan.bin_dir / tool).is_file()]
        if missing:
            # Without this a missing tool would fall through to an installed one.
            raise BuildError(
                "build did not install: " + ", ".join(missing)
                + f" into {plan.bin_dir}"
            )
        if plan.python:
            for tool in PYTHON_TOOLS:
                with open(plan.bin_dir / tool, encoding="utf-8", errors="replace") as file:
                    first = file.readline().strip()
                if first != f"#!{plan.python}":
                    raise BuildError(
                        f"{tool} runs {first[2:] or 'nothing'!r}, not the "
                        f"--with-python interpreter {plan.python}"
                    )
        return BuildResult(
            plan,
            toolchain,
            host_version,
            self._artifacts(plan),
            tuple(records),
        )

    @staticmethod
    def _artifacts(plan: BuildPlan) -> tuple[FileIdentity, ...]:
        artifacts = [
            FileIdentity.from_path(f"built {path.name}", path)
            for path in sorted(plan.bin_dir.iterdir())
            if path.is_file()
        ]
        libraries = sorted(
            path for path in (plan.prefix / "lib").glob("lib*")
            if path.is_file() and not path.is_symlink()
            and re.search(r"\.(so|dylib)(\.|$)", path.name)
        )
        artifacts += [
            FileIdentity.from_path(f"built {path.name}", path) for path in libraries
        ]
        if plan.with_firmware:
            artifacts.append(
                FileIdentity.from_path("built bluetooth_rxtx.dfu", plan.firmware_image)
            )
        return tuple(artifacts)


def homebrew_include_dir(executor: CommandExecutor, which=shutil.which) -> str | None:
    """Homebrew's include directory, needed for libusb headers on macOS."""
    if which("brew") is None:
        return None
    result = executor.run(("brew", "--prefix"), timeout_s=30.0)
    if result.returncode != 0:
        return None
    return str(Path(result.stdout.strip()) / "include")
