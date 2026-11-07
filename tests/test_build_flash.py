"""Flashing the boards with the run's own build before preflight (#135).

No hardware: fake ubertooth-dfu / ubertooth-util executors and a fake build.
"""

import errno
import fcntl
import io
import json
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ubertooth_harness.build import (
    APPLICATION_IMAGE,
    BTBR_BUILD_ARTIFACT,
    FIRMWARE_IMAGES,
    TEST_BUILD_ARTIFACT,
    BuildPlan,
    BuildResult,
)
from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.cli import main
from ubertooth_harness.events import RUN_LOG, open_run_publisher
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.flash import BuildFlashBoard, BuildFlasher, HarnessError
from ubertooth_harness.gui import failure_summary
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.report import load_events, render_run

from tests import watchdog_samples
from tests.test_cli import make_source


SERIAL_A = "aaaa0000aaaa0000aaaa0000aaaa0000"
SERIAL_B = "bbbb1111bbbb1111bbbb1111bbbb1111"
# The CP2102N bridge of the ESP32 Classic peer the btbr plan binds.
PEER_SERIAL = "cccc2222cccc2222cccc2222cccc2222"
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
        if command == ("ubertooth-util", "-V"):
            # The host tools' own version, printed before any board is looked
            # for. This is the host version check's first question; `-H` below
            # is the fallback for older fork commits.
            output = (
                f"Host tools version: {self.host_revision} "
                f"(libbtbb {self.host_revision})\n"
            )
        elif command == ("ubertooth-util", "-H"):
            output = (
                f"Host tools version: {self.host_revision} "
                f"(libbtbb {self.host_revision})\n"
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
        elif command[-1] == "-w":
            # The run reads each board's firmware watchdog record, before the
            # flash stage and again at the end (watchdog.py).
            output = watchdog_samples.report()
        elif command[-1] == "-v":
            serial = command[2]
            if self.silent_reads.get(serial):
                self.silent_reads[serial] -= 1
                output, returncode = "error: no Ubertooth found\n", 1
            else:
                output = f"Firmware version: {self.revisions[serial]} (API:1.07)\n"
        elif command[-1] == "-V":
            # The whole answer one board gives: the host line and this
            # board's firmware version, which is what the per-board check
            # reads (fork #1233 took the build string out).
            revision = self.revisions[command[2]]
            output = (
                f"Host tools version: {self.host_revision} "
                f"(libbtbb {self.host_revision})\n"
                f"Firmware version: {revision} (API:1.07)\n"
            )
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

    def artifact_names(self):
        """The names the run's manifest recorded its build artifacts under."""
        manifest = next(e for e in self.events() if e["event_type"] == "manifest")
        return [item["name"]
                for item in manifest["details"]["manifest"]["artifacts"]]

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
        self.source, revision = make_source(self.root)
        self.revision = revision
        self.platform = "macos" if sys.platform == "darwin" else "linux"
        self.hardware = self.write_hardware("hardware.yaml", self.platform)
        self.lease_dir = self.root / "leases"
        self.output = self.root / "runs"
        self.builds = []
        self.built_images = []

    def write_hardware(self, name, platform):
        """The fixture registry, for a host of this platform.

        `Classic btbr console` runs only on Linux, so the two tests about
        which images its plan builds name a Linux host whatever this machine
        is. Nothing in those runs reaches a real tool: the build is faked and
        the cases never run.
        """
        path = self.root / name
        path.write_text(f"""resources:
  Host_Local:
    type: Host
    platform: {platform}
    roles: [test target]
    defaults: {{Ubertooth: UT_0, acceptor: ESP32_0}}
  UT_0:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: {SERIAL_A}}}
  UT_1:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: {SERIAL_B}}}
  ESP32_0:
    type: ESP32 Classic peer
    attached to: Host_Local
    roles: [acceptor, initiator]
    identity: {{bridge serial: "{PEER_SERIAL}", BD_ADDR: "C8:00:00:00:00:01"}}
""")
        return path

    def tearDown(self):
        self.directory.cleanup()

    def btbr_plan(self):
        """A plan whose one test declares that it needs the alternate image.

        Which images the run then builds is the derivation #279 added: the
        catalog says this test needs the btbr image, so a firmware build
        makes it without the operator naming it.
        """
        path = self.root / "btbr-plan.yaml"
        path.write_text(
            "plan: p\nsuites:\n  fast:\n    - Classic btbr console:\n"
            "        hosts: each\n        Ubertooth: default\n"
            "        acceptor: default\n"
            "        allow RF transmission: true\n"
            "        coverage: smoke\n"
        )
        return path

    def plan(self, ubertooth="default"):
        path = self.root / "plan.yaml"
        path.write_text(
            "plan: p\nsuites:\n  fast:\n    - Debug register read:\n"
            f"        hosts: each\n        Ubertooth: {ubertooth}\n"
        )
        return path

    def fake_build(self, sources, work_dir, with_firmware, publisher, with_python=None, *,
                   run_dir, firmware_images=FIRMWARE_IMAGES):
        self.builds.append(with_firmware)
        self.built_images.append(tuple(firmware_images))
        by_name = {source.name: source for source in sources}
        plan = BuildPlan(
            work_dir=work_dir, prefix=work_dir / "prefix", platform=self.platform,
            ubertooth=by_name["Ubertooth"], libbtbb=by_name["libbtbb"],
            with_firmware=with_firmware, host_cmake_flags=(), commands=(),
            firmware_images=tuple(firmware_images),
        )
        if with_firmware:
            for image in firmware_images:
                plan.image(image).parent.mkdir(parents=True, exist_ok=True)
                plan.image(image).write_bytes(b"built firmware")
        publisher.emit("build_started", "running", {})
        publisher.emit("build_finished", "passed", {"reason": ""})
        return BuildResult(
            plan, {}, f"Host tools version: {self.revision} (libbtbb {self.revision})", (),
        )

    def run_suite(self, executor, *extra, plan=None, missing=(), hardware=None,
                  platform=None):
        stderr = io.StringIO()
        with (
            patch("ubertooth_harness.cli._detected_platform",
                  return_value=platform or self.platform),
            patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
            patch("ubertooth_harness.cli._build", side_effect=self.fake_build),
            patch("ubertooth_harness.cli.missing_tools", return_value=list(missing)),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            code = main([
                "run-suite", "--plan", str(plan or self.plan()), "--suite", "fast",
                "--hardware", str(hardware or self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
                "--build", "--lease-dir", str(self.lease_dir),
                "--run-id", "flash-run", "--output", str(self.output), *extra,
            ])
        self.stderr = stderr.getvalue()
        return code

    def artifact_names(self):
        """The names the run's manifest recorded its build artifacts under."""
        manifest = next(e for e in self.events() if e["event_type"] == "manifest")
        return [item["name"]
                for item in manifest["details"]["manifest"]["artifacts"]]

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
        # Only the image it writes. The alternate `btbr` image is built when a
        # run asks for it (--with-btbr) or runs a test that needs it, so a
        # plan that never runs it does not fail because it did not build.
        self.assertEqual([(APPLICATION_IMAGE,)], self.built_images)
        self.assertNotIn(BTBR_BUILD_ARTIFACT, self.artifact_names())

    def test_with_firmware_alone_builds_the_application_image_only(self):
        # The firmware build is the application image: this plan runs no test
        # that needs the alternate one, so nothing waits for it to build and
        # the run does not fail because it did not (#279).
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD},
                              lease_dir=self.lease_dir)
        self.assertEqual(0, self.run_suite(executor, "--with-firmware"),
                         self.stderr)
        self.assertEqual([(APPLICATION_IMAGE,)], self.built_images)
        names = self.artifact_names()
        self.assertIn(TEST_BUILD_ARTIFACT, names)
        self.assertNotIn(BTBR_BUILD_ARTIFACT, names)

    def test_with_btbr_builds_and_records_both_images(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD},
                              lease_dir=self.lease_dir)
        self.assertEqual(0, self.run_suite(executor, "--with-btbr"), self.stderr)
        self.assertEqual([FIRMWARE_IMAGES], self.built_images)
        names = self.artifact_names()
        self.assertIn(TEST_BUILD_ARTIFACT, names)
        self.assertIn(BTBR_BUILD_ARTIFACT, names)

    def test_a_selected_test_that_needs_the_alternate_image_gets_it(self):
        # The derivation #279 added, end to end: nobody names the second
        # image, the catalog says this test needs it, and the firmware build
        # makes it. The test itself needs Linux and a peer, so its cases may
        # be left out here; what this checks is which images were built.
        executor = FakeBoards(self.revision, {SERIAL_A: self.revision,
                                              SERIAL_B: self.revision},
                              lease_dir=self.lease_dir)
        self.run_suite(executor, "--with-firmware", plan=self.btbr_plan(),
                       hardware=self.write_hardware("linux.yaml", "linux"),
                       platform="linux")
        self.assertEqual([True], self.builds)
        self.assertEqual([FIRMWARE_IMAGES], self.built_images)

    def test_a_selected_test_does_not_turn_the_firmware_build_on(self):
        # The same plan with no firmware option: the second image is only
        # ever added to a firmware build that is already happening, because
        # turning that build on needs arm-none-eabi-gcc and would make a run
        # that passed with the case left out start failing for a toolchain
        # nobody asked it to use. Without the guard in cli.py this names the
        # btbr image while nothing is built at all.
        executor = FakeBoards(self.revision, {SERIAL_A: self.revision,
                                              SERIAL_B: self.revision},
                              lease_dir=self.lease_dir)
        self.run_suite(executor, "--no-flash-build", plan=self.btbr_plan(),
                       hardware=self.write_hardware("linux.yaml", "linux"),
                       platform="linux")
        self.assertEqual([False], self.builds)
        self.assertEqual([(APPLICATION_IMAGE,)], self.built_images)

    def test_a_run_that_builds_no_firmware_names_no_alternate_image(self):
        # --build --no-flash-build and no build option: no firmware is built
        # at all, so the images the plan would make must not claim the
        # alternate one. A selected test that needs it does not turn the
        # firmware build on by itself, because that needs a toolchain the
        # host may not have; without it the case is left out as not
        # applicable, which is what it was before (#279).
        executor = FakeBoards(self.revision, {SERIAL_A: self.revision,
                                              SERIAL_B: self.revision},
                              lease_dir=self.lease_dir)
        self.assertEqual(0, self.run_suite(executor, "--no-flash-build"),
                         self.stderr)
        self.assertEqual([False], self.builds)
        self.assertEqual([(APPLICATION_IMAGE,)], self.built_images)
        self.assertNotIn(BTBR_BUILD_ARTIFACT, self.artifact_names())

    def test_with_btbr_without_build_is_refused(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = main([
                "run-suite", "--plan", str(self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--lease-dir", str(self.lease_dir),
                "--output", str(self.output), "--with-btbr",
            ])
        self.assertEqual(2, code)
        self.assertIn("--with-btbr requires --build", stderr.getvalue())

    def test_the_flash_finishes_before_the_manifest_and_preflight(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD}, lease_dir=self.lease_dir)
        self.assertEqual(0, self.run_suite(executor), self.stderr)
        types = [e["event_type"] for e in self.events()]
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

    def test_a_cancel_during_the_flash_stage_ends_the_run_canceled(self):
        executor = FakeBoards(self.revision, {SERIAL_A: OLD, SERIAL_B: OLD})
        original = executor.run

        def run(argv, **kwargs):
            if argv[0] == "ubertooth-dfu":
                # What Ctrl-C does: the harness's own handler takes it.
                os.kill(os.getpid(), signal.SIGINT)
            return original(argv, **kwargs)

        executor.run = run
        self.assertEqual(2, self.run_suite(executor))
        finished = self.events()[-1]
        self.assertEqual(
            ("run_finished", "canceled"), (finished["event_type"], finished["status"])
        )
        self.assertIn("canceled", finished["details"]["failure_reason"])
        # The second board is not touched after the cancellation.
        self.assertEqual(
            1, len([c for c in executor.commands if c[0] == "ubertooth-dfu"])
        )

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

    def _run_suite(self, *extra):
        stderr = io.StringIO()
        stdout = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(stdout):
            code = main([
                "run-suite", "--plan", str(self.plan()), "--suite", "fast",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(self.source), "--ubertooth-ref", "HEAD",
                "--output", str(self.output), *extra,
            ])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_comparing_a_bootloader_needs_a_build(self):
        """#298: check and update compare against a bootloader built here."""
        for mode in ("check", "update"):
            with self.subTest(mode):
                code, _out, stderr = self._run_suite(f"--bootloader={mode}")
                self.assertEqual(2, code)
                self.assertIn(f"--bootloader {mode} compares each board", stderr)
                self.assertIn("requires --build", stderr)

    def test_updating_a_bench_with_no_programmer_is_refused_before_the_build(self):
        code, _out, stderr = self._run_suite(
            "--bootloader=update", "--build",
            "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
        )
        self.assertEqual(2, code)
        self.assertIn("only through a debug programmer", stderr)
        self.assertIn("UT_0 has none", stderr)
        self.assertIn("--bootloader check", stderr)

    def test_updating_needs_the_run_s_own_firmware_on_the_board(self):
        """A write happens before preflight, so the board carries this build."""
        code, _out, stderr = self._run_suite(
            "--bootloader=update", "--build", "--no-flash-build",
            "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
        )
        self.assertEqual(2, code)
        self.assertIn("must be carrying this run's own firmware first", stderr)
        self.assertIn("drop --no-flash-build", stderr)

    def test_updating_without_the_programmer_command_is_refused(self):
        with patch("ubertooth_harness.cli.shutil.which", return_value=None):
            code, _out, stderr = self._run_suite(
                "--bootloader=update", "--build",
                "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
            )
        self.assertEqual(2, code)
        # The registry refusal comes first on this bench; with a programmer
        # registered the missing tool is what is left.
        self.assertTrue(
            "lpc21isp" in stderr or "only through a debug programmer" in stderr,
            stderr,
        )

    def test_dry_run_says_what_it_would_do_about_each_board_s_bootloader(self):
        code, stdout, _err = self._run_suite(
            "--bootloader=check", "--build", "--dry-run",
            "--libbtbb-source", str(self.source), "--libbtbb-ref", "HEAD",
        )
        self.assertEqual(0, code)
        self.assertIn("bootloader: check", stdout)
        self.assertIn(f"UT_0 ({SERIAL_A}): no programmer registered; check-only",
                      stdout)

    def test_the_run_s_own_bootloader_artifact_name_is_reserved(self):
        """A given file must not stand in the manifest for the one written."""
        code, _out, stderr = self._run_suite(
            "--artifact", "bootloader region=" + str(self.hardware),
        )
        self.assertEqual(2, code)
        self.assertIn("is the run's own bootloader region", stderr)

    def test_recording_is_the_default(self):
        code, stdout, _err = self._run_suite("--dry-run")
        self.assertEqual(0, code)
        self.assertIn("bootloader: record", stdout)

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
