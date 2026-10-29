"""Flashing the boards with the run's own build before preflight (#135).

No hardware: fake ubertooth-dfu / ubertooth-util executors and a fake build.
"""

import errno
import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ubertooth_harness.build import BuildPlan, BuildResult
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.cli import main
from ubertooth_harness.events import RUN_LOG, open_run_publisher
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.flash import BuildFlashBoard, BuildFlasher, HarnessError
from ubertooth_harness.gui import failure_summary
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.report import load_events, render_run

from tests.test_cli import make_source


SERIAL_A = "aaaa0000aaaa0000aaaa0000aaaa0000"
SERIAL_B = "bbbb1111bbbb1111bbbb1111bbbb1111"
OLD = "git-a8e65d9"


def _locked(path: Path) -> bool:
    """True when another open file description holds an flock on path."""
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return True
        raise
    finally:
        os.close(fd)
    return False


class FakeBoards:
    """Host tools and Ubertooths answering ubertooth-util, -dfu and -debug."""

    def __init__(self, host_revision, revisions, *, lease_dir=None):
        self.host_revision = host_revision
        self.revisions = dict(revisions)  # serial -> firmware revision
        self.image_revision = host_revision  # what a write puts on a board
        self.fail_write = set()
        self.after_write = {}  # serial -> revision reported after a write
        self.silent_reads = {}  # serial -> -v reads left unanswered after a write
        self.lease_dir = lease_dir
        self.commands = []
        self.locks_at_write = []  # (host lock held, board lease held)
        self.locks_at_test = []
        self.cancel_on_write = None

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        command = tuple(argv)
        self.commands.append(command)
        assert timeout_s > 0, command
        returncode = 0
        if command == ("ubertooth-util", "-H"):
            output = (
                f"libubertooth 1.1 ({self.host_revision}), "
                f"libbtbb 1.0 ({self.host_revision})\n"
            )
        elif command[0] == "ubertooth-dfu":
            serial = command[command.index("-U") + 1]
            if self.lease_dir is not None:
                self.locks_at_write.append((
                    _locked(self.lease_dir / "host-power.lock"),
                    _locked(self.lease_dir / f"{serial}.lock"),
                ))
            if self.cancel_on_write is not None:
                self.cancel_on_write.cancel("test cancel")
            if serial in self.fail_write:
                output, returncode = "error: DFU write failed\n", 1
            else:
                self.revisions[serial] = self.after_write.get(serial, self.image_revision)
                output = "Verified firmware revision\n"
        elif command[0] == "ubertooth-debug":
            serial = command[2]
            if self.lease_dir is not None:
                self.locks_at_test.append((
                    _locked(self.lease_dir / "host-power.lock"),
                    _locked(self.lease_dir / f"{serial}.lock"),
                ))
            output = "%MAIN = 0x1234\n"
        elif command[-1] == "-v":
            serial = command[2]
            if self.silent_reads.get(serial):
                self.silent_reads[serial] -= 1
                output, returncode = "error: no Ubertooth found\n", 1
            else:
                output = f"Firmware version: {self.revisions[serial]} (API:1.07)\n"
        elif command[-1] == "-V":
            output = f"Compile info: ubertooth {self.revisions[command[2]]} (builder@host)\n"
        elif command[-1] == "-s":
            output = f"Serial No: {command[2]}\n"
        else:
            raise AssertionError(f"unexpected command {command}")
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, returncode, output, "", 0.01, False)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BuildFlasherTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.image = self.root / "bluetooth_rxtx.dfu"
        self.image.write_bytes(b"firmware")
        self.run_dir = self.root / "run"
        self.publisher = open_run_publisher(self.run_dir, "flash-unit")
        self.clock = FakeClock()

    def tearDown(self):
        self.directory.cleanup()

    def flash(self, boards, executor, **kwargs):
        return BuildFlasher(executor, clock=self.clock, sleep=self.clock.sleep).run(
            boards, self.image, "git-new", self.publisher, output_dir=self.run_dir,
            **kwargs,
        )

    def events(self):
        return load_events(self.run_dir / RUN_LOG)

    def test_each_board_is_written_then_verified_in_order(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD, SERIAL_B: OLD})
        results = self.flash(
            [BuildFlashBoard("UT_0", SERIAL_A), BuildFlashBoard("UT_1", SERIAL_B)], executor
        )
        self.assertEqual(["flashed", "flashed"], [r["status"] for r in results])
        self.assertEqual([OLD, OLD], [r["before"] for r in results])
        self.assertEqual(["git-new", "git-new"], [r["after"] for r in results])
        writes = [c for c in executor.commands if c[0] == "ubertooth-dfu"]
        self.assertEqual(
            [("ubertooth-dfu", "-U", s, "-d", str(self.image), "-r") for s in (SERIAL_A, SERIAL_B)],
            writes,
        )
        # Sequential: board A is read back before board B is written.
        index = executor.commands.index
        self.assertLess(
            index(("ubertooth-util", "-U", SERIAL_A, "-v"), 1), index(writes[1])
        )
        finished = [e for e in self.events() if e["event_type"] == "build_flash_finished"]
        self.assertEqual(["passed"], [e["status"] for e in finished])

    def test_a_board_already_running_the_build_is_not_written(self):
        executor = FakeBoards("git-new", {SERIAL_A: "git-new"})
        results = self.flash([BuildFlashBoard("UT_0", SERIAL_A)], executor)
        self.assertFalse(results[0]["written"])
        self.assertEqual("skipped", results[0]["status"])
        self.assertEqual(("git-new", "git-new"), (results[0]["before"], results[0]["after"]))
        self.assertFalse([c for c in executor.commands if c[0] == "ubertooth-dfu"])

    def test_another_abbreviation_of_the_same_commit_is_the_same_build(self):
        # The board and the build can stamp one commit with hashes of
        # different lengths, which is not a reason to write or to fail.
        executor = FakeBoards("git-1c7f06d6", {SERIAL_A: "git-1c7f06d6"})
        results = BuildFlasher(
            executor, clock=self.clock, sleep=self.clock.sleep
        ).run(
            [BuildFlashBoard("UT_0", SERIAL_A)], self.image, "git-1c7f06d",
            self.publisher, output_dir=self.run_dir,
        )
        self.assertEqual("skipped", results[0]["status"])
        self.assertFalse([c for c in executor.commands if c[0] == "ubertooth-dfu"])

    def test_another_abbreviation_read_back_after_the_write_is_accepted(self):
        executor = FakeBoards("git-1c7f06d6", {SERIAL_A: OLD})
        results = BuildFlasher(
            executor, clock=self.clock, sleep=self.clock.sleep
        ).run(
            [BuildFlashBoard("UT_0", SERIAL_A)], self.image, "git-1c7f06d",
            self.publisher, output_dir=self.run_dir,
        )
        self.assertEqual("flashed", results[0]["status"])
        self.assertEqual("git-1c7f06d6", results[0]["after"])

    def test_no_write_starts_without_its_whole_budget(self):
        # A write killed part-way leaves the board in its bootloader.
        executor = FakeBoards("git-new", {SERIAL_A: OLD})
        original = executor.run

        def run(argv, **kwargs):
            self.clock.now += 6.0  # the read leaves under a write's budget
            return original(argv, **kwargs)

        executor.run = run
        with self.assertRaises(HarnessError) as raised:
            self.flash([BuildFlashBoard("UT_0", SERIAL_A)], executor,
                       total_timeout_s=30.0)
        self.assertIn("is left of the 30s deadline", str(raised.exception))
        self.assertFalse([c for c in executor.commands if c[0] == "ubertooth-dfu"])

    def test_a_failed_write_stops_before_the_next_board(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD, SERIAL_B: OLD})
        executor.fail_write.add(SERIAL_A)
        with self.assertRaises(HarnessError) as raised:
            self.flash(
                [BuildFlashBoard("UT_0", SERIAL_A), BuildFlashBoard("UT_1", SERIAL_B)], executor
            )
        self.assertIn(f"ubertooth-dfu exited 1 writing UT_0 (serial {SERIAL_A})", str(raised.exception))
        self.assertNotIn(SERIAL_B, [c[2] for c in executor.commands])
        finished = [e for e in self.events() if e["event_type"] == "build_flash_finished"][0]
        self.assertEqual("failed", finished["status"])

    def test_a_read_back_mismatch_fails_with_both_revisions(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD})
        executor.after_write[SERIAL_A] = "git-other"
        with self.assertRaises(HarnessError) as raised:
            self.flash([BuildFlashBoard("UT_0", SERIAL_A)], executor)
        self.assertIn("runs git-other after the write, expected git-new", str(raised.exception))

    def test_the_read_back_is_retried_while_the_board_re_enumerates(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD})
        executor.silent_reads[SERIAL_A] = 0
        # The first read after the write is unanswered, the second answers.
        original = executor.run

        def run(argv, **kwargs):
            if argv[0] == "ubertooth-dfu":
                executor.silent_reads[SERIAL_A] = 1
            return original(argv, **kwargs)

        executor.run = run
        results = self.flash([BuildFlashBoard("UT_0", SERIAL_A)], executor)
        self.assertEqual("git-new", results[0]["after"])
        self.assertEqual(3, executor.commands.count(("ubertooth-util", "-U", SERIAL_A, "-v")))

    def test_a_board_that_never_answers_after_the_write_fails(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD})
        original = executor.run

        def run(argv, **kwargs):
            if argv[0] == "ubertooth-dfu":
                executor.silent_reads[SERIAL_A] = 99
            return original(argv, **kwargs)

        executor.run = run
        with self.assertRaises(HarnessError) as raised:
            self.flash([BuildFlashBoard("UT_0", SERIAL_A)], executor)
        self.assertIn("did not report its firmware revision after the write (3 reads)",
                      str(raised.exception))

    def test_the_total_deadline_stops_the_stage(self):
        executor = FakeBoards("git-new", {SERIAL_A: OLD, SERIAL_B: OLD})
        original = executor.run

        def run(argv, **kwargs):
            self.clock.now += 30.0
            return original(argv, **kwargs)

        executor.run = run
        with self.assertRaises(HarnessError) as raised:
            self.flash(
                [BuildFlashBoard("UT_0", SERIAL_A), BuildFlashBoard("UT_1", SERIAL_B)],
                executor, total_timeout_s=50.0,
            )
        self.assertIn("is left of the 50s deadline", str(raised.exception))

    def test_cancellation_lets_the_write_finish_and_stops_after_it(self):
        token = CancellationToken()
        executor = FakeBoards("git-new", {SERIAL_A: OLD, SERIAL_B: OLD})
        executor.cancel_on_write = token
        with self.assertRaises(HarnessError) as raised:
            self.flash(
                [BuildFlashBoard("UT_0", SERIAL_A), BuildFlashBoard("UT_1", SERIAL_B)],
                executor, cancellation=token,
            )
        self.assertIn("canceled: test cancel", str(raised.exception))
        self.assertEqual(1, len([c for c in executor.commands if c[0] == "ubertooth-dfu"]))
        finished = [e for e in self.events() if e["event_type"] == "build_flash_finished"][0]
        self.assertEqual("canceled", finished["status"])


class MismatchReasonTests(unittest.TestCase):

    def check(self, name, argv, pattern, fix=""):
        return PreflightCheck(
            check_id="c", name=name, host="Host_Linux", resource_alias="UT_Linux0",
            resource_identity={}, argv=argv, expected_patterns=(pattern,),
            expected_revision="git-7388776", fix=fix,
        )

    def test_firmware_mismatch_names_both_revisions_and_the_fix(self):
        check = self.check(
            "Firmware version", ("ubertooth-util", "-v"),
            r"Firmware version:\s+git\-7388776(?:\s|\()",
        )
        result = CommandResult(("x",), 0, "Firmware version: git-a8e65d9 (API:1.07)\n", "", 0.0, False)
        self.assertEqual(
            "Firmware version: UT_Linux0 runs git-a8e65d9, the tools are git-7388776: "
            "flash it with this build (the default when building)",
            check.result_failure(result),
        )

    def test_compile_info_mismatch_uses_the_run_kinds_fix(self):
        check = self.check(
            "Firmware compile info", ("ubertooth-util", "-V"),
            r"\bubertooth\s+git\-7388776\s+\(", fix="build and flash it",
        )
        result = CommandResult(("x",), 0, "ubertooth git-a8e65d9 (u@h)\n", "", 0.0, False)
        self.assertEqual(
            "Firmware compile info: UT_Linux0 runs git-a8e65d9, the tools are "
            "git-7388776: build and flash it",
            check.result_failure(result),
        )

    def test_unreadable_output_keeps_the_generic_reason(self):
        check = self.check("Device serial", ("ubertooth-util", "-s"), r"Serial No:\s+abc")
        result = CommandResult(("x",), 0, "Serial No: def\n", "", 0.0, False)
        self.assertEqual(
            "Device serial: identity mismatch for UT_Linux0: "
            r"output does not match Serial No:\s+abc",
            check.result_failure(result),
        )


class RunSuiteFlashTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source, short = make_source(self.root)
        self.revision = f"git-{short}"
        self.platform = "macos" if sys.platform == "darwin" else "linux"
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(f"""resources:
  Host_Local:
    type: Host
    platform: {self.platform}
    roles: [test target]
    defaults: {{Ubertooth: UT_0}}
  UT_0:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: {SERIAL_A}}}
  UT_1:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: {SERIAL_B}}}
""")
        self.lease_dir = self.root / "leases"
        self.output = self.root / "runs"
        self.builds = []

    def tearDown(self):
        self.directory.cleanup()

    def plan(self, ubertooth="default"):
        path = self.root / "plan.yaml"
        path.write_text(
            "plan: p\nsuites:\n  fast:\n    - Debug register read:\n"
            f"        hosts: each\n        Ubertooth: {ubertooth}\n"
        )
        return path

    def fake_build(self, sources, work_dir, with_firmware, publisher, with_python=None, *, run_dir):
        self.builds.append(with_firmware)
        by_name = {source.name: source for source in sources}
        plan = BuildPlan(
            work_dir=work_dir, prefix=work_dir / "prefix", platform=self.platform,
            ubertooth=by_name["Ubertooth"], libbtbb=by_name["libbtbb"],
            with_firmware=with_firmware, host_cmake_flags=(), commands=(),
        )
        if with_firmware:
            plan.firmware_image.parent.mkdir(parents=True)
            plan.firmware_image.write_bytes(b"built firmware")
        publisher.emit("build_started", "running", {})
        publisher.emit("build_finished", "passed", {"reason": ""})
        return BuildResult(plan, {}, f"libubertooth 1.1 ({self.revision})", ())

    def run_suite(self, executor, *extra, plan=None, missing=()):
        stderr = io.StringIO()
        with (
            patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
            patch("ubertooth_harness.cli._build", side_effect=self.fake_build),
            patch("ubertooth_harness.cli.missing_tools", return_value=list(missing)),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            code = main([
                "run-suite", "--plan", str(plan or self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
                "--build", "--lease-dir", str(self.lease_dir),
                "--run-id", "flash-run", "--output", str(self.output), *extra,
            ])
        self.stderr = stderr.getvalue()
        return code

    def events(self):
        return load_events(self.output / "flash-run" / RUN_LOG)

    def report(self):
        return (self.output / "flash-run" / "report.md").read_text()

    def test_a_build_run_flashes_its_boards_before_preflight_by_default(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD}, lease_dir=self.lease_dir)
        self.assertEqual(0, self.run_suite(executor), self.stderr)
        # The firmware is built for flashing without --with-firmware.
        self.assertEqual([True], self.builds)
        image = self.output / "flash-run" / "build" / "src" / "ubertooth" / "firmware" / "bluetooth_rxtx" / "bluetooth_rxtx.dfu"
        writes = [c for c in executor.commands if c[0] == "ubertooth-dfu"]
        # Only the board the run tests; UT_1 is not used by the plan.
        self.assertEqual([("ubertooth-dfu", "-U", SERIAL_A, "-d")], [w[:4] for w in writes])
        self.assertEqual(image.resolve(), Path(writes[0][4]).resolve())
        self.assertEqual("-r", writes[0][5])
        self.assertEqual(self.revision, executor.revisions[SERIAL_A])
        self.assertEqual(OLD, executor.revisions[SERIAL_B])
        types = [e["event_type"] for e in self.events()]
        self.assertLess(types.index("build_finished"), types.index("build_flash_started"))
        self.assertLess(types.index("build_flash_finished"), types.index("manifest"))
        self.assertLess(types.index("build_flash_finished"), types.index("preflight_started"))
        # The host lock and the board's lease were held while writing; the
        # host lock was given back before the tests (the plan writes no
        # firmware itself), the lease kept.
        self.assertEqual([(True, True)], executor.locks_at_write)
        self.assertEqual([(False, True)], executor.locks_at_test)
        manifest = next(e for e in self.events() if e["event_type"] == "manifest")
        self.assertEqual(["UT_0"], [i["resource"] for i in manifest["details"]["manifest"]["leases"]])
        report = self.report()
        self.assertIn(
            f"| Flash | yes, verified before preflight; with the build's firmware {self.revision}: "
            f"UT_0 written, was {OLD}, now {self.revision} (host lock held while writing) |",
            report,
        )
        self.assertIn(f"=== FLASH: 1 board(s) with the build's firmware {self.revision} ===", report)
        self.assertIn(f"+++ flash UT_0 ({SERIAL_A}) +++", report)
        self.assertIn("=== FLASH EXIT: passed ===", report)
        self.assertIn(f"UT_0 serial {SERIAL_A}: firmware {self.revision} (verified)", report)

    def test_every_selected_board_is_flashed(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD})
        self.assertEqual(0, self.run_suite(executor, plan=self.plan("each")), self.stderr)
        writes = [c[2] for c in executor.commands if c[0] == "ubertooth-dfu"]
        self.assertEqual([SERIAL_A, SERIAL_B], writes)

    def test_only_boards_on_other_firmware_are_flashed(self):
        executor = FakeBoards(self.revision, {SERIAL_A: self.revision, SERIAL_B: OLD})
        self.assertEqual(0, self.run_suite(executor, plan=self.plan("each")), self.stderr)
        writes = [c[2] for c in executor.commands if c[0] == "ubertooth-dfu"]
        self.assertEqual([SERIAL_B], writes)
        boards = [e for e in self.events() if e["event_type"] == "build_flash_board_finished"]
        self.assertEqual(
            [("UT_0", "skipped", self.revision, self.revision), ("UT_1", "flashed", OLD, self.revision)],
            [(e["details"]["resource"], e["status"], e["details"]["before"], e["details"]["after"])
             for e in boards],
        )
        self.assertIn(f"UT_0 already ran {self.revision}", self.report())

    def test_no_flash_build_keeps_the_firmware_and_names_the_fix(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD})
        self.assertEqual(1, self.run_suite(executor, "--no-flash-build"))
        self.assertEqual([False], self.builds)
        self.assertFalse([c for c in executor.commands if c[0] == "ubertooth-dfu"])
        skipped = [e for e in self.events() if e["event_type"] == "build_flash_finished"]
        self.assertEqual(["skipped"], [e["status"] for e in skipped])
        report = self.report()
        self.assertIn("| Flash | no (--no-flash-build) |", report)
        self.assertIn(
            f"Firmware version: UT_0 runs {OLD}, the tools are {self.revision}: flash it "
            "with this build (the default when building: leave out --no-flash-build",
            report,
        )

    def test_a_flash_failure_stops_the_run_before_preflight(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD})
        executor.fail_write.add(SERIAL_A)
        self.assertEqual(2, self.run_suite(executor))
        self.assertIn(f"flashing the run's build failed: ubertooth-dfu exited 1 writing UT_0 (serial {SERIAL_A})", self.stderr)
        types = [e["event_type"] for e in self.events()]
        self.assertNotIn("preflight_started", types)
        self.assertNotIn("manifest", types)
        finished = self.events()[-1]
        self.assertEqual(("run_finished", "failed"), (finished["event_type"], finished["status"]))
        self.assertIn("ubertooth-dfu exited 1", finished["details"]["failure_reason"])
        self.assertIn("| Flash | **failed**: ubertooth-dfu exited 1", self.report())
        failures = failure_summary({"status": "failed"}, self.events())
        flash = [item for item in failures if item["step"] == "flash"]
        self.assertEqual(1, len(flash))
        self.assertIn("error: DFU write failed", flash[0]["log"])

    def test_a_read_back_mismatch_stops_the_run(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD})
        executor.after_write[SERIAL_A] = OLD
        self.assertEqual(2, self.run_suite(executor))
        self.assertIn(f"runs {OLD} after the write, expected {self.revision}", self.stderr)
        self.assertNotIn("preflight_started", [e["event_type"] for e in self.events()])

    def test_missing_arm_toolchain_is_refused_before_anything_runs(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD})
        self.assertEqual(2, self.run_suite(executor, missing=["arm-none-eabi-gcc"]))
        self.assertIn("arm-none-eabi-gcc is not installed", self.stderr)
        self.assertIn("--no-flash-build", self.stderr)
        self.assertEqual([], self.builds)
        self.assertFalse((self.output / "flash-run").exists())

    def test_a_run_without_build_names_build_and_flash_as_the_fix(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD})
        stderr = io.StringIO()
        with (
            patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
            redirect_stderr(stderr), redirect_stdout(io.StringIO()),
        ):
            code = main([
                "run-suite", "--plan", str(self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--lease-dir", str(self.lease_dir),
                "--run-id", "flash-run", "--output", str(self.output),
            ])
        self.assertEqual(1, code)
        report = self.report()
        self.assertIn("| Flash | no (the run does not build) |", report)
        self.assertIn(
            f"UT_0 runs {OLD}, the tools are {self.revision}: build and flash it", report
        )

    def test_no_flash_build_requires_build(self):
        with redirect_stderr(io.StringIO()) as stderr, redirect_stdout(io.StringIO()):
            code = main([
                "run-suite", "--plan", str(self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--no-flash-build", "--output", str(self.output),
            ])
        self.assertEqual(2, code)
        self.assertIn("--no-flash-build requires --build", stderr.getvalue())

    def test_dry_run_names_the_boards_it_would_flash(self):
        stdout = io.StringIO()
        with (
            patch("ubertooth_harness.cli.missing_tools", return_value=["arm-none-eabi-gcc"]),
            redirect_stderr(io.StringIO()), redirect_stdout(stdout),
        ):
            code = main([
                "run-suite", "--plan", str(self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
                "--build", "--dry-run", "--output", str(self.output),
            ])
        self.assertEqual(0, code)
        self.assertIn(
            f"flash before preflight with the build's firmware: UT_0 ({SERIAL_A})",
            stdout.getvalue(),
        )


class ReportTests(unittest.TestCase):

    def test_an_unfinished_flash_stage_is_shown(self):
        events = [
            {"run_id": "r", "sequence": 1, "timestamp": "2026-01-01T00:00:00",
             "event_type": "run_started", "status": "running", "details": {}},
            {"run_id": "r", "sequence": 2, "timestamp": "2026-01-01T00:00:01",
             "event_type": "build_flash_started", "status": "running",
             "details": {"boards": [{"resource": "UT_0", "serial": SERIAL_A}],
                         "expected_revision": "git-new"}},
            {"run_id": "r", "sequence": 3, "timestamp": "2026-01-01T00:00:02",
             "event_type": "build_flash_board_started", "status": "running",
             "details": {"resource": "UT_0", "serial": SERIAL_A}},
        ]
        text = render_run(events)
        self.assertIn("| Flash | **did not finish** |", text)
        self.assertIn(SERIAL_A, text)
        self.assertNotIn(SERIAL_A, render_run(events, redact_serials=True))


if __name__ == "__main__":
    unittest.main()
