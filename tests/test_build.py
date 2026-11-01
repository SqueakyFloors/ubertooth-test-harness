import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.build import (
    BUILT_TOOLS,
    BuildError,
    Builder,
    missing_tools,
    plan_build,
    require_tools,
    required_tools,
)
from ubertooth_harness.events import open_run_publisher
from ubertooth_harness.executor import CommandResult, SubprocessCommandExecutor
from ubertooth_harness.hostneeds import BLUEZ_TOOLS
from ubertooth_harness.manifest import SourceRevision
from ubertooth_harness.report import load_events, render_run


# libbtbb's default branch: that repository's name for it, not ours to
# rename, and what a ref that is a branch name looks like here.
LIBBTBB_BRANCH = "master"

UBERTOOTH = SourceRevision(
    "Ubertooth", "/repos/ubertooth", "https://example.invalid/ubertooth.git",
    "main", "a" * 40, "aaaaaaa",
)
LIBBTBB = SourceRevision(
    "libbtbb", "/repos/libbtbb", "https://example.invalid/libbtbb.git",
    LIBBTBB_BRANCH, "b" * 40, "bbbbbbb",
)


def make_plan(work, platform="linux", with_firmware=False, include=None):
    return plan_build(
        ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work),
        platform=platform, with_firmware=with_firmware,
        extra_include_dir=include, jobs=4,
    )


def publisher_in(work):
    return open_run_publisher(Path(work), "build-run")


class FakeBuildExecutor:
    """Succeeds for every command and installs fake artifacts on install."""

    def __init__(self, plan, *, host_version=None, fail_step=None, missing_library=None,
                 installed_tools=None):
        self.plan = plan
        # By default every tool the plan's platform builds (the BlueZ tools
        # on Linux).
        self.installed_tools = (
            required_tools(plan) if installed_tools is None else installed_tools
        )
        self.host_version = host_version or (
            "Host tools version: libubertooth 1.1 (git-aaaaaaa), libbtbb 1.0 (git-bbbbbbb)\n"
        )
        self.fail_step = fail_step
        self.missing_library = missing_library
        self.calls = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        command = tuple(argv)
        self.calls.append((command, cwd))
        if command[:2] == ("pkg-config", "--modversion"):
            if command[-1] == self.missing_library:
                return CommandResult(command, 1, "", "not found\n", 0.01, False)
            return CommandResult(command, 0, "1.0.30\n", "", 0.01, False)
        if command[0] in ("cmake", "cc", "pkg-config", "arm-none-eabi-gcc") and "--version" in command:
            return CommandResult(command, 0, f"{command[0]} version 1\n", "", 0.01, False)
        step = next(c for c in self.plan.commands if c.argv == command)
        if step.step_id == self.fail_step:
            return CommandResult(command, 2, "", "boom\n", 0.01, False)
        if step.step_id == "host-install":
            bin_dir = self.plan.prefix / "bin"
            lib = self.plan.prefix / "lib"
            bin_dir.mkdir(parents=True, exist_ok=True)
            lib.mkdir(parents=True, exist_ok=True)
            for tool in self.installed_tools:
                (bin_dir / tool).write_text(f"{tool} binary")
            (lib / "libubertooth.so.1.1").write_text("lib")
            (lib / "libubertooth.so").symlink_to("libubertooth.so.1.1")
        if step.step_id == "firmware-build":
            self.plan.firmware_image.parent.mkdir(parents=True, exist_ok=True)
            self.plan.firmware_image.write_text("dfu")
        if step.step_id == "verify-host-version":
            if on_output:
                on_output("stdout", self.host_version)
            return CommandResult(command, 0, self.host_version, "", 0.01, False)
        return CommandResult(command, 0, "", "", 0.01, False)


class OlderForkBuildExecutor(FakeBuildExecutor):
    """Built tools from a fork commit that answers the host version on -H.

    `-V` there is the board's compile info, so with no board attached it
    reports nothing about the host and exits 1.
    """

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        command = tuple(argv)
        verify = next(c for c in self.plan.commands
                      if c.step_id == "verify-host-version")
        if command == verify.argv:
            self.calls.append((command, cwd))
            message = "error: unrecognized option '-V'\n"
            if on_output:
                on_output("stderr", message)
            return CommandResult(command, 1, "", message, 0.01, False)
        if command == verify.fallback_argv:
            self.calls.append((command, cwd))
            if on_output:
                on_output("stdout", self.host_version)
            return CommandResult(command, 0, self.host_version, "", 0.01, False)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output, cwd=cwd)


class BuildPlanTests(unittest.TestCase):

    def test_builds_resolved_commits_out_of_tree_into_a_run_prefix(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            argv = {c.step_id: c.argv for c in plan.commands}
            prefix = str(Path(work).resolve() / "prefix")

            self.assertEqual(
                ["libbtbb-clone", "libbtbb-checkout", "libbtbb-refresh-index",
                 "libbtbb-verify-clean", "ubertooth-clone", "ubertooth-checkout",
                 "ubertooth-refresh-index", "ubertooth-verify-clean",
                 "libbtbb-configure", "libbtbb-build", "libbtbb-install",
                 "host-configure", "host-build", "host-install", "verify-host-version"],
                [c.step_id for c in plan.commands],
            )
            self.assertEqual(UBERTOOTH.source_path, argv["ubertooth-clone"][-2])
            self.assertEqual(UBERTOOTH.commit, argv["ubertooth-checkout"][-1])
            self.assertEqual(LIBBTBB.commit, argv["libbtbb-checkout"][-1])
            for step in ("libbtbb-configure", "host-configure"):
                self.assertIn(f"-DCMAKE_INSTALL_PREFIX={prefix}", argv[step])
            self.assertIn(f"-DCMAKE_PREFIX_PATH={prefix}", argv["host-configure"])
            for flag in ("-DINSTALL_UDEV_RULES=OFF", "-DENABLE_PYTHON=OFF", "-DREQUIRE_ALL=ON"):
                self.assertIn(flag, argv["host-configure"])
            # Only libbtbb's C library is built; the pinned libbtbb fork
            # commit declares a cmake_minimum_required range, so CMake 4 needs
            # no CMAKE_POLICY_VERSION_MINIMUM as upstream libbtbb does.
            self.assertIn("-DENABLE_PYTHON=OFF", argv["libbtbb-configure"])
            self.assertNotIn(
                "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", argv["libbtbb-configure"]
            )
            for command in plan.commands:
                self.assertNotIn("sudo", command.argv)
                self.assertFalse(
                    any(str(UBERTOOTH.source_path) in a for a in command.argv)
                    and command.step_id != "ubertooth-clone",
                    command.step_id,
                )

    def test_configure_runs_inside_each_checkout(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            cwd = {c.step_id: c.cwd for c in plan.commands}

            self.assertEqual(plan.work_dir / "src" / "libbtbb", cwd["libbtbb-configure"])
            self.assertEqual(plan.work_dir / "src" / "ubertooth" / "host", cwd["host-configure"])

    def test_macos_skips_require_all_and_adds_homebrew_headers(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work, platform="macos", include="/opt/homebrew/include")

            self.assertNotIn("-DREQUIRE_ALL=ON", plan.host_cmake_flags)
            self.assertIn("-DCMAKE_C_FLAGS=-I/opt/homebrew/include", plan.host_cmake_flags)

    def test_firmware_is_built_only_on_request_with_built_dfu_tool(self):
        with tempfile.TemporaryDirectory() as work:
            without = make_plan(work)
            with_fw = make_plan(work, with_firmware=True)

            self.assertNotIn("firmware-build", [c.step_id for c in without.commands])
            firmware = next(c for c in with_fw.commands if c.step_id == "firmware-build")
            self.assertIn(f"DFU_TOOL={with_fw.prefix / 'bin' / 'ubertooth-dfu'}", firmware.argv)

    def test_environment_puts_the_run_prefix_first(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            env = plan.environment({"PATH": "/usr/bin", "PKG_CONFIG_PATH": "/usr/lib/pkgconfig"})

            self.assertEqual(f"{plan.prefix / 'bin'}:/usr/bin", env["PATH"])
            self.assertTrue(env["PKG_CONFIG_PATH"].startswith(str(plan.prefix / "lib" / "pkgconfig")))
            self.assertEqual(str(plan.prefix), env["LIBBTBB_DIR"])

    def test_missing_tools_are_named_with_install_hints(self):
        def which(tool):
            return None if tool in ("cmake", "arm-none-eabi-gcc") else f"/usr/bin/{tool}"

        self.assertEqual(["cmake"], missing_tools("linux", False, which))
        self.assertIn("make", missing_tools("linux", False, lambda tool: None))
        with self.assertRaisesRegex(BuildError, r"cmake, arm-none-eabi-gcc.*apt install.*gcc-arm-none-eabi"):
            require_tools("linux", True, which)
        with self.assertRaisesRegex(BuildError, "brew install"):
            require_tools("macos", False, which)


class BuilderTests(unittest.TestCase):

    def test_successful_build_records_toolchain_artifacts_and_log(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work, with_firmware=True)
            executor = FakeBuildExecutor(plan)

            result = Builder(executor, publisher_in(work), run_dir=work).run(plan)

            self.assertEqual(
                {"cmake", "cc", "pkg-config", "libusb-1.0", "bluez", "arm-none-eabi-gcc"},
                set(result.toolchain),
            )
            self.assertEqual(
                [f"built {tool}" for tool in sorted(BUILT_TOOLS + BLUEZ_TOOLS)]
                + ["built libubertooth.so.1.1", "built bluetooth_rxtx.dfu"],
                [item.name for item in result.artifacts],
            )
            events = load_events(Path(work) / "run.jsonl")
            self.assertEqual("build_started", events[0]["event_type"])
            self.assertEqual("build_finished", events[-1]["event_type"])
            report = render_run(events)
            self.assertIn("    === BUILD ===", report)
            self.assertIn("    +++ Configure Ubertooth host tools +++", report)
            self.assertIn("[C] cmake -S", report)
            self.assertIn("Host tools version: libubertooth 1.1 (git-aaaaaaa)", result.host_version)
            manifest = result.to_manifest()
            self.assertEqual(len(plan.commands), len(manifest["commands"]))
            host_configure = next(c for c in plan.commands if c.step_id == "host-configure")
            self.assertIn(
                (host_configure.argv, str(plan.work_dir / "src" / "ubertooth" / "host")),
                executor.calls,
            )

    def test_a_failed_step_stops_the_build_and_names_it(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(plan, fail_step="host-build")

            with self.assertRaisesRegex(BuildError, "Build Ubertooth host tools exited 2"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

            self.assertNotIn("host-install", [c[0] for c, _cwd in executor.calls])

    def test_built_tools_must_report_the_resolved_revision(self):
        for reported in (
            "Host tools version: libubertooth 1.1 (git-ccccccc), libbtbb 1.0 (git-bbbbbbb)\n",
            "Host tools version: libubertooth 1.1 (git-aaaaaaa*), libbtbb 1.0 (git-bbbbbbb)\n",
            "Host tools version: libubertooth 1.1 (git-aaaaaaa), libbtbb 1.0 (git-ddddddd)\n",
        ):
            with tempfile.TemporaryDirectory() as work, self.subTest(reported=reported):
                plan = make_plan(work)
                with self.assertRaisesRegex(BuildError, "expected"):
                    Builder(FakeBuildExecutor(plan, host_version=reported), publisher_in(work), run_dir=work).run(plan)

    def test_tools_that_do_not_know_V_are_asked_H(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = OlderForkBuildExecutor(plan)

            result = Builder(executor, publisher_in(work), run_dir=work).run(plan)

            self.assertIn("libubertooth 1.1 (git-aaaaaaa)", result.host_version)
            commands = [c for c, _cwd in executor.calls
                        if c[0].endswith("ubertooth-util")]
            self.assertEqual([("-V",), ("-H",)], [c[1:] for c in commands])
            steps = [item["step_id"] for item in result.commands]
            self.assertEqual(["verify-host-version", "verify-host-version-fallback"],
                             steps[-2:])
            # The step's reason names the option that answered, so the
            # fallback is not read as a busy board.
            step = [e for e in load_events(Path(work) / "run.jsonl")
                    if e["event_type"] == "build_step_finished"][-1]
            self.assertEqual(
                "the version line is the resolved revision, from -H; the exit "
                "status is not what this step reads (-V exited 1)",
                step["details"]["reason"],
            )

    def test_a_reported_revision_is_not_asked_again_on_H(self):
        # The label is there, so -V answered: the wrong revision is the
        # build's problem, not a reason to try the older option.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(
                plan,
                host_version=("Host tools version: libubertooth 1.1 (git-ccccccc), "
                              "libbtbb 1.0 (git-bbbbbbb)\n"),
            )

            with self.assertRaisesRegex(BuildError, "expected"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

            self.assertNotIn(
                "-H", [c[-1] for c, _cwd in executor.calls],
            )

    def test_a_verify_step_killed_at_its_deadline_fails_without_the_fallback(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            class Hangs(FakeBuildExecutor):
                def run(self, argv, **kwargs):
                    result = super().run(argv, **kwargs)
                    if tuple(argv)[-1] == "-V":
                        return replace(result, returncode=124, stdout="",
                                       timed_out=True)
                    return result

            executor = Hangs(plan)
            with self.assertRaisesRegex(BuildError, "timed out"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

            self.assertNotIn("-H", [c[-1] for c, _cwd in executor.calls])

    def test_both_options_are_named_when_neither_answers(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            class Neither(OlderForkBuildExecutor):
                def run(self, argv, **kwargs):
                    result = super().run(argv, **kwargs)
                    if tuple(argv)[-1] == "-H":
                        return replace(result, returncode=1, stdout="",
                                       stderr="error: unrecognized option '-H'\n")
                    return result

            with self.assertRaisesRegex(
                BuildError, r"-V exited 1; -H exited 1"
            ):
                Builder(Neither(plan), publisher_in(work), run_dir=work).run(plan)

    def test_the_recorded_version_is_the_host_line_alone(self):
        # -V prints a board's firmware lines under the host line; the
        # manifest and the report call this field the built tools' version.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            whole = (
                "Host tools version: libubertooth 1.1 (git-aaaaaaa), "
                "libbtbb 1.0 (git-bbbbbbb)\n"
                "libbtbb loaded from: /p/lib/libbtbb.so.1\n"
                "Firmware version: bluetooth_rxtx git-aaaaaaa (API:1.35)\n"
                "Firmware build: ubertooth git-aaaaaaa (builder@host) now\n"
            )

            result = Builder(FakeBuildExecutor(plan, host_version=whole),
                             publisher_in(work), run_dir=work).run(plan)

            self.assertEqual(
                "Host tools version: libubertooth 1.1 (git-aaaaaaa), "
                "libbtbb 1.0 (git-bbbbbbb)",
                result.host_version,
            )

    def test_a_board_that_cannot_be_read_does_not_fail_the_build(self):
        # -V prints the host version first, then reads every attached board
        # and exits 1 if one cannot be read -- a board this build does not
        # need. The version line is what the step checks.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            class BusyBoard(FakeBuildExecutor):
                def run(self, argv, **kwargs):
                    result = super().run(argv, **kwargs)
                    if tuple(argv)[-1] == "-V":
                        return replace(
                            result, returncode=1,
                            stdout=result.stdout + "Firmware version: unavailable, "
                                   "in use by another program\n",
                        )
                    return result

            result = Builder(BusyBoard(plan), publisher_in(work), run_dir=work).run(plan)

            self.assertIn("libubertooth 1.1 (git-aaaaaaa)", result.host_version)

    def test_every_step_says_how_it_ended(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            Builder(FakeBuildExecutor(plan), publisher_in(work), run_dir=work).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            started = [e["details"]["step_id"] for e in events
                       if e["event_type"] == "build_step_started"]
            finished = [(e["details"]["step_id"], e["status"]) for e in events
                        if e["event_type"] == "build_step_finished"]

            self.assertEqual(["toolchain"] + [c.step_id for c in plan.commands],
                             started)
            self.assertEqual([(step, "passed") for step in started], finished)
            self.assertIn(
                "--- Built host tools report the resolved revision EXIT: passed ---",
                render_run(events),
            )

    def test_an_accepted_exit_status_leaves_the_step_passed(self):
        # -V prints the host version and then reads every attached board,
        # exiting 1 if one cannot be read. The step accepts that, so it must
        # not read as failed; its reason says which exit status it accepted.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            class BusyBoard(FakeBuildExecutor):
                def run(self, argv, **kwargs):
                    result = super().run(argv, **kwargs)
                    if tuple(argv)[-1] == "-V":
                        return replace(result, returncode=1)
                    return result

            Builder(BusyBoard(plan), publisher_in(work), run_dir=work).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            step = next(e for e in events
                        if e["event_type"] == "build_step_finished"
                        and e["details"]["step_id"] == "verify-host-version")

            self.assertEqual("passed", step["status"])
            self.assertEqual(
                "the version line is the resolved revision, from -V; the exit "
                "status is not what this step reads (-V exited 1)",
                step["details"]["reason"],
            )
            report = render_run(events)
            self.assertIn(
                "--- Built host tools report the resolved revision EXIT: passed: "
                "the version line is the resolved revision, from -V; the exit "
                "status is not what this step reads (-V exited 1) ---",
                report,
            )
            self.assertIn("=== BUILD EXIT: passed ===", report)
            # One marker for the step, from its own record: the latch that
            # reads the commands must not add a second.
            self.assertNotIn(
                "Built host tools report the resolved revision EXIT: failed",
                report,
            )

    def test_a_failed_step_says_so_with_its_reason(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)

            with self.assertRaises(BuildError):
                Builder(FakeBuildExecutor(plan, fail_step="host-build"),
                        publisher_in(work), run_dir=work).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            step = next(e for e in events
                        if e["event_type"] == "build_step_finished")
            last = [e for e in events if e["event_type"] == "build_step_finished"][-1]

            self.assertEqual("toolchain", step["details"]["step_id"])
            self.assertEqual(("host-build", "failed"),
                             (last["details"]["step_id"], last["status"]))
            # The step's marker does not repeat its own name, which the
            # BuildError carries for the BUILD marker.
            self.assertEqual("exited 2", last["details"]["reason"])
            self.assertIn(
                "--- Build Ubertooth host tools EXIT: failed: exited 2 ---",
                render_run(events),
            )

    def test_a_version_neither_option_answered_fails_its_own_step(self):
        # Both commands exited 0 and the version is not the resolved one:
        # the step decided that, so it says so rather than reading passed.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(
                plan,
                host_version=("Host tools version: libubertooth 1.1 "
                              "(git-ccccccc), libbtbb 1.0 (git-bbbbbbb)\n"),
            )

            with self.assertRaisesRegex(BuildError, "expected"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            step = [e for e in events if e["event_type"] == "build_step_finished"][-1]

            self.assertEqual(("verify-host-version", "failed"),
                             (step["details"]["step_id"], step["status"]))
            self.assertIn("not the resolved revision", step["details"]["reason"])

    def test_a_reason_is_one_bounded_line_whatever_it_came_from(self):
        # Reasons are built from a tool's output and from cancellation
        # messages someone else wrote; the marker they land in is one line,
        # and the record has a size limit.
        from ubertooth_harness.build import (
            _MAX_MARKER_REASON_CHARS, _one_line_reason,
        )

        self.assertEqual("canceled: a; b", _one_line_reason("canceled: a\nb"))
        bounded = _one_line_reason("x" * 9000)
        self.assertEqual(_MAX_MARKER_REASON_CHARS, len(bounded))
        self.assertTrue(bounded.endswith("... [truncated]"), bounded)

    def test_a_canceled_build_leaves_its_step_failed(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            cancellation = CancellationToken()

            class Cancels(FakeBuildExecutor):
                def run(self, argv, **kwargs):
                    cancellation.cancel("operator")
                    return super().run(argv, **kwargs)

            with self.assertRaises(BuildError):
                Builder(Cancels(plan), publisher_in(work), run_dir=work,
                        cancellation=cancellation).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            step = [e for e in events if e["event_type"] == "build_step_finished"][-1]

            self.assertEqual("failed", step["status"])
            self.assertIn("canceled", step["details"]["reason"])

    def test_a_mismatched_version_reason_is_one_line(self):
        # -V prints the attached board's firmware lines under the version;
        # the step's reason lands in a one-line marker in the transcript.
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(
                plan,
                host_version=(
                    "Host tools version: libubertooth 1.1 (git-ccccccc), "
                    "libbtbb 1.0 (git-bbbbbbb)\n"
                    "Firmware version: bluetooth_rxtx git-ccccccc (API:1.07)\n"
                    "libusb_claim_interface error -6\n"
                ),
            )

            with self.assertRaisesRegex(BuildError, "expected"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

            events = load_events(Path(work) / "run.jsonl")
            step = [e for e in events if e["event_type"] == "build_step_finished"][-1]

            self.assertEqual(
                "not the resolved revision: Host tools version: libubertooth "
                "1.1 (git-ccccccc), libbtbb 1.0 (git-bbbbbbb)",
                step["details"]["reason"],
            )
            marker = next(text for text in render_run(events).splitlines()
                          if "EXIT: failed" in text)
            self.assertTrue(marker.endswith("---"), marker)

    def test_every_catalog_tool_must_come_from_the_prefix(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(
                plan, installed_tools=tuple(t for t in BUILT_TOOLS if t != "ubertooth-btle")
            )
            with self.assertRaisesRegex(BuildError, "did not install: ubertooth-btle"):
                Builder(executor, publisher_in(work), run_dir=work).run(plan)

    def test_a_linux_build_must_install_the_bluez_tools(self):
        # REQUIRE_ALL=ON on Linux: a build without them is a build problem,
        # not a reason to leave their cases out (#40).
        for tool in BLUEZ_TOOLS:
            with tempfile.TemporaryDirectory() as work, self.subTest(tool=tool):
                plan = make_plan(work)
                executor = FakeBuildExecutor(
                    plan, installed_tools=tuple(t for t in required_tools(plan) if t != tool)
                )
                with self.assertRaisesRegex(BuildError, f"did not install: {tool} into"):
                    Builder(executor, publisher_in(work), run_dir=work).run(plan)

    def test_a_macos_build_does_not_need_the_bluez_tools(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work, platform="macos")
            self.assertEqual(BUILT_TOOLS, required_tools(plan))
            result = Builder(FakeBuildExecutor(plan), publisher_in(work), run_dir=work).run(plan)
            names = {item.name for item in result.artifacts}
            self.assertFalse({f"built {tool}" for tool in BLUEZ_TOOLS} & names)

    def test_catalog_tools_match_the_built_tool_list(self):
        from ubertooth_harness.catalog import builtin_catalog

        cases = builtin_catalog().get("Host CLI surface").command_cases({}, {}, {})

        required = {case.argv[0] for case in cases if not case.needs}
        self.assertEqual(set(BUILT_TOOLS), required)

    def test_cancellation_stops_before_the_next_command(self):
        from ubertooth_harness.cancellation import CancellationToken

        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            token = CancellationToken()
            executor = FakeBuildExecutor(plan)
            original = executor.run

            def run(argv, **options):
                result = original(argv, **options)
                if tuple(argv)[:2] == ("cmake", "--build"):
                    token.cancel("received SIGINT")
                return result

            executor.run = run
            with self.assertRaisesRegex(BuildError, "build canceled: received SIGINT"):
                Builder(executor, publisher_in(work), run_dir=work, cancellation=token).run(plan)
            self.assertEqual(1, sum(c[0][:2] == ("cmake", "--build") for c in executor.calls))

    def test_whole_build_deadline_caps_command_timeouts(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            executor = FakeBuildExecutor(plan)
            timeouts = []
            original = executor.run

            def run(argv, **options):
                timeouts.append(options["timeout_s"])
                return original(argv, **options)

            executor.run = run
            now = [0.0]

            def clock():
                now[0] += 1.0
                return now[0]

            with self.assertRaisesRegex(BuildError, "exceeded 10s"):
                Builder(executor, publisher_in(work), run_dir=work, deadline_s=10.0, clock=clock).run(plan)
            self.assertTrue(all(timeout <= 10.0 for timeout in timeouts))

    def test_missing_library_is_named(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            with self.assertRaisesRegex(BuildError, "missing library: bluez.*libbluetooth-dev"):
                Builder(FakeBuildExecutor(plan, missing_library="bluez"), publisher_in(work), run_dir=work).run(plan)


class ExecutorEnvironmentTests(unittest.TestCase):

    def test_subprocess_executor_uses_given_environment_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = SubprocessCommandExecutor({"HARNESS_TEST": "yes", "PATH": "/usr/bin:/bin"})
            result = executor.run(
                (sys.executable, "-c", "import os; print(os.environ.get('HARNESS_TEST'), os.getcwd())"),
                timeout_s=10, cwd=directory,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            value, cwd = result.stdout.split()
            self.assertEqual("yes", value)
            self.assertEqual(Path(directory).resolve(), Path(cwd).resolve())


class BuildCliTests(unittest.TestCase):

    def run_suite(self, *extra):
        argv = ["run-suite", "--plan", "testplans/fast.yaml", "--suite", "fast",
                "--hardware", "hardware.example.yaml", "--host", "Host_Mac",
                "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
                "--output", "/tmp/never-created", "--test", "Host CLI surface", *extra]
        with mock.patch.object(cli, "_detected_platform", return_value="macos"), \
                mock.patch.object(cli, "_resolve_sources", return_value=(UBERTOOTH,)), \
                mock.patch("sys.stderr") as stderr:
            code = cli.main(argv)
        return code, "".join(call.args[0] for call in stderr.write.call_args_list)

    def test_build_requires_a_resolved_libbtbb(self):
        code, stderr = self.run_suite("--build")
        self.assertEqual(2, code)
        self.assertIn("--build requires --libbtbb-source", stderr)

    def test_firmware_requires_build(self):
        code, stderr = self.run_suite("--with-firmware")
        self.assertEqual(2, code)
        self.assertIn("--with-firmware requires --build", stderr)


if __name__ == "__main__":
    unittest.main()
