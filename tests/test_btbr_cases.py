"""`Classic btbr console`: the case, the two firmware images, and the catalog."""

import io
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest

from tests.fake_btbr import (
    PEER_ADDRESS,
    PEER_NAME,
    write_fake_btbr,
    write_fake_peer_reset,
)
from ubertooth_harness import btbr_cases, btbrconsole, esp32peer
from ubertooth_harness.pytools import PYTHON_TOOLS_ENV
from ubertooth_harness.btbr_cases import btbr_console_cases
from ubertooth_harness.build import (
    APPLICATION_IMAGE,
    BTBR_BUILD_ARTIFACT,
    BTBR_IMAGE,
    FIRMWARE_IMAGES,
    TEST_BUILD_ARTIFACT,
    plan_build,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.manifest import SourceRevision
from ubertooth_harness.patterns import (
    FIRMWARE_VERSION_RE,
    image_name_pattern,
    matched_image,
)
from ubertooth_harness.plan import image_label
from ubertooth_harness.stateful import (
    APPLICATION_IMAGE_LABEL,
    BTBR_IMAGE_LABEL,
    StateCaseError,
    Unrestorable,
)

SERIAL = "0123456789abcdef0123456789abcdef"


def fake_session_report(*, faults=(), comprehensive=False) -> str:
    """One console session against the fake, and the report it printed."""
    with tempfile.TemporaryDirectory() as directory:
        tools = Path(directory) / "bin"
        reset, reset_environment = write_fake_peer_reset(tools)
        environment = {"PATH": "/usr/bin:/bin",
                       PYTHON_TOOLS_ENV: str(tools),
                       **reset_environment,
                       **write_fake_btbr(tools, faults=faults)}
        out, err = io.StringIO(), io.StringIO()
        btbrconsole.run_session(
            SimpleNamespace(
                serial=SERIAL, peer=PEER_ADDRESS, peer_name=PEER_NAME,
                inquiry_seconds=2.0, page_seconds=2.0, hold_seconds=0.05,
                no_answer_seconds=2.0, comprehensive=comprehensive,
                peer_reset=reset, peer_device="/dev/serial/by-id/fake"),
            out, err, environment)
        return err.getvalue()
UBERTOOTH = SourceRevision(
    "Ubertooth", "/repos/ubertooth", "https://example.invalid/ubertooth.git",
    "main", "a" * 40, "aaaaaaa",
)
LIBBTBB = SourceRevision(
    "libbtbb", "/repos/libbtbb", "https://example.invalid/libbtbb.git",
    "main", "b" * 40, "bbbbbbb",
)
RESOURCES = {"Ubertooth": "UT_Linux0", "acceptor": "ESP32_Linux0"}


def identities(name=None):
    """The registry entries one invocation binds; ``name`` writes the peer's
    device name down beside its address, which is optional."""
    peer = {"bridge serial": "a" * 32, "BD_ADDR": PEER_ADDRESS,
            "serial device": "/dev/serial/by-id/fake"}
    if name is not None:
        peer["name"] = name
    return {"Ubertooth": {"serial": SERIAL}, "acceptor": peer}


def cases(coverage="smoke", sessions=1, name=None):
    return btbr_console_cases({"coverage": coverage, "sessions": sessions},
                              RESOURCES, identities(name))


class ImageLabelTests(unittest.TestCase):
    def test_the_labels_a_case_asks_for_are_the_artifact_names_recorded(self):
        # cli.py records the images under these names; a case asks for the
        # labels they normalize to. If the two drift, a run would refuse the
        # case for want of an image it had.
        self.assertEqual(BTBR_IMAGE_LABEL, image_label(BTBR_BUILD_ARTIFACT))
        self.assertEqual(APPLICATION_IMAGE_LABEL,
                         image_label(TEST_BUILD_ARTIFACT))

    def test_a_build_asked_for_both_images_makes_and_names_both(self):
        # Which images a run builds is the run's own decision (cli.py): a
        # firmware build makes the application image, and the alternate one
        # when it was asked for. Given both, the plan has a step each (#279).
        with tempfile.TemporaryDirectory() as work:
            plan = plan_build(
                ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work),
                platform="linux", with_firmware=True,
                firmware_images=FIRMWARE_IMAGES)
            steps = [command.step_id for command in plan.commands]
            self.assertIn("firmware-build", steps)
            self.assertIn("firmware-build-btbr", steps)
            # The default is the application image alone, so a plan nobody
            # asked for the alternate one does not build it.
            application_only = plan_build(
                ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work),
                platform="linux", with_firmware=True)
            self.assertNotIn(
                "firmware-build-btbr",
                [command.step_id for command in application_only.commands],
            )
            self.assertEqual(plan.image(APPLICATION_IMAGE), plan.firmware_image)
            self.assertEqual(plan.image(BTBR_IMAGE), plan.btbr_image)
            self.assertEqual(("bluetooth_rxtx", "btbr"), FIRMWARE_IMAGES)
            # Each image is built in its own directory, from the same clone.
            for image in FIRMWARE_IMAGES:
                command = next(c for c in plan.commands
                               if c.step_id.endswith(image)
                               or (image == APPLICATION_IMAGE
                                   and c.step_id == "firmware-build"))
                self.assertIn(f"firmware/{image}", " ".join(command.argv))

    def test_a_build_without_firmware_builds_neither(self):
        with tempfile.TemporaryDirectory() as work:
            plan = plan_build(
                ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work),
                platform="linux")
            steps = [command.step_id for command in plan.commands]
            self.assertNotIn("firmware-build", steps)
            self.assertNotIn("firmware-build-btbr", steps)


class ImagePatternTests(unittest.TestCase):
    BTBR_LINE = "Firmware version: btbr git-b2cd6c68 (API:1.57)"
    APPLICATION_LINE = ("Firmware version: bluetooth_rxtx git-b2cd6c68 "
                        "(API:1.57)")
    OLD_LINE = "Firmware version: 2020-12-R1 (API:1.07)"

    def test_the_image_pattern_tells_the_two_images_apart(self):
        btbr = image_name_pattern(BTBR_IMAGE)
        application = image_name_pattern(APPLICATION_IMAGE)
        self.assertRegex(self.BTBR_LINE, btbr)
        self.assertNotRegex(self.APPLICATION_LINE, btbr)
        self.assertRegex(self.APPLICATION_LINE, application)
        self.assertNotRegex(self.BTBR_LINE, application)
        # Firmware older than the info block names no image, so neither
        # pattern takes it: a case that needs to know which image is running
        # needs the name.
        self.assertNotRegex(self.OLD_LINE, btbr)
        self.assertNotRegex(self.OLD_LINE, application)

    def test_the_image_a_board_reports_is_readable(self):
        # The way the cases read it: the version line's match, which carries
        # the image name as a group of its own.
        def image(line):
            return matched_image(FIRMWARE_VERSION_RE.search(line))

        self.assertEqual("btbr", image(self.BTBR_LINE))
        self.assertEqual("bluetooth_rxtx", image(self.APPLICATION_LINE))
        self.assertIsNone(image(self.OLD_LINE))
        self.assertIsNone(image("nothing of the kind"))


class CaseTests(unittest.TestCase):
    def test_one_session_is_one_case_that_flashes_once_and_restores(self):
        (case,) = cases()
        self.assertEqual("btbr-console", case.case_id)
        self.assertEqual(
            ["option-values", "read-original-image", "flash-btbr",
             "verify-btbr-image", "read-btbr-bootloader", "verify-btbr-build",
             "reset-peer-before", "console-session"],
            [step.step_id for step in case.steps])
        self.assertEqual(
            ["restore-application", "verify-restored-image",
             "verify-restored-bootloader", "reset-peer-after"],
            [step.step_id for step in case.cleanup])
        # Both images, so a run without either refuses the case instead of
        # flashing a board it cannot put back.
        self.assertEqual((BTBR_IMAGE_LABEL, APPLICATION_IMAGE_LABEL),
                         case.artifacts)
        # Decided before anything is flashed: the peer's bridge path is
        # Linux's, ubertooth-btbr comes from a --with-python build, and both
        # images from a --with-firmware one.
        self.assertEqual(("linux",), case.needs.platforms)
        self.assertTrue(case.needs.python_tools)
        self.assertTrue(case.needs.firmware_build)
        self.assertIn("written back", case.expected_state)

    def test_only_one_flash_each_way_whatever_the_profile(self):
        for coverage in ("smoke", "comprehensive"):
            with self.subTest(coverage):
                (case,) = cases(coverage)
                writes = [step for step in case.steps + case.cleanup
                          if "ubertooth-dfu" in " ".join(
                              step.resolve_argv({
                                  "artifact btbr_build": "/tmp/btbr.dfu",
                                  "artifact test_build": "/tmp/app.dfu",
                                  "expected firmware revision": "git-aaaaaaa",
                              }))]
                self.assertEqual(2, len(writes))

    def test_the_cleanup_writes_the_application_image_and_reads_it_back(self):
        (case,) = cases()
        context = {"artifact btbr_build": "/tmp/btbr.dfu",
                   "artifact test_build": "/tmp/app.dfu",
                   "expected firmware revision": "git-aaaaaaa"}
        restore = case.cleanup[0]
        self.assertEqual(
            ("ubertooth-dfu", "-U", SERIAL, "-d", "/tmp/app.dfu", "-r"),
            restore.resolve_argv(context))
        self.assertTrue(restore.mutates)
        verify = case.cleanup[1]
        self.assertEqual((image_name_pattern(APPLICATION_IMAGE),),
                         verify.output_patterns)
        self.assertEqual("git-aaaaaaa", verify.observe.expected_value(context))

    def test_both_images_must_report_the_same_bootloader(self):
        """ubertooth-dfu writes the application region only (#327/#329).

        The baseline is read while the board runs the run's own `btbr` image
        and the cleanup's read, with the application image back, is held to
        it. Both images implement the request, so neither read can be
        firmware too old to answer it.
        """
        (case,) = cases()
        read = next(step for step in case.steps
                    if step.step_id == "read-btbr-bootloader")
        verify = next(step for step in case.cleanup
                      if step.step_id == "verify-restored-bootloader")
        for step in (read, verify):
            self.assertEqual(("ubertooth-util", "-U", SERIAL, "-B"),
                             step.resolve_argv({}))
            self.assertFalse(step.mutates)
        # The baseline is recorded with nothing to compare; the second read
        # is held to it, under a key of its own so both are in the record.
        self.assertIsNone(read.observe.expected)
        self.assertNotEqual(read.observe.key, verify.observe.key)
        line = "Bootloader: 2.00 git-abc1234, CRC32:0x11112222"
        self.assertEqual(line, read.observe.extract(line + "\n"))
        self.assertEqual(
            line, verify.observe.expected_value({read.observe.key: line}))

    def test_the_read_is_after_the_write_so_it_is_of_the_runs_own_image(self):
        """A board can arrive on firmware older than the bootloader read."""
        (case,) = cases()
        order = [step.step_id for step in case.steps]
        self.assertLess(order.index("flash-btbr"),
                        order.index("read-btbr-bootloader"))
        cleanup = [step.step_id for step in case.cleanup]
        self.assertLess(cleanup.index("restore-application"),
                        cleanup.index("verify-restored-bootloader"))

    def test_without_a_baseline_the_cleanup_read_is_skipped_not_failed(self):
        """A case that failed at the write needs its restore, not a second
        failure about a comparison it never got far enough to set up."""
        (case,) = cases()
        verify = next(step for step in case.cleanup
                      if step.step_id == "verify-restored-bootloader")
        with self.assertRaises(Unrestorable) as caught:
            verify.observe.expected_value({})
        self.assertIn("never reported the board's bootloader",
                      caught.exception.args[0])

    def test_the_read_is_given_the_same_deadline_as_the_preflight_one(self):
        from ubertooth_harness import bootloader

        (case,) = cases()
        read = next(step for step in case.steps
                    if step.step_id == "read-btbr-bootloader")
        self.assertEqual(bootloader.IDENTITY_TIMEOUT_S, read.timeout_s)
        self.assertEqual(bootloader.IDENTITY_TIMEOUT_S,
                         btbr_cases.BOOTLOADER_TIMEOUT_S)

    def test_the_read_matches_the_line_the_harness_renders(self):
        """The case's pattern and bootloader.py's own must stay in step."""
        from ubertooth_harness import bootloader

        identity = bootloader.BootloaderIdentity(
            region_crc32=0x11112222, detail="2.00 git-abc1234",
            version="2.00", build="git-abc1234",
        )
        (case,) = cases()
        read = next(step for step in case.steps
                    if step.step_id == "read-btbr-bootloader")
        self.assertEqual(identity.line(),
                         read.observe.extract(identity.line() + "\n"))

    def test_the_btbr_image_is_verified_by_name_at_the_runs_revision(self):
        (case,) = cases()
        verify = next(step for step in case.steps
                      if step.step_id == "verify-btbr-image")
        self.assertEqual((image_name_pattern(BTBR_IMAGE),),
                         verify.output_patterns)
        self.assertEqual("git-aaaaaaa", verify.observe.expected_value(
            {"expected firmware revision": "git-aaaaaaa"}))

    def test_the_console_step_reads_the_drivers_verdict(self):
        (case,) = cases()
        step = case.steps[-1]
        argv = " ".join(step.resolve_argv({}))
        self.assertIn("btbrconsole.py session", argv)
        self.assertIn(f"--peer {PEER_ADDRESS}", argv)
        self.assertIn(f"--peer-name {PEER_NAME}", argv)
        # The peer driver and its bridge, so the session can reset the peer
        # between pages and the run record shows how.
        self.assertIn("esp32peer.py", argv)
        self.assertIn("--peer-device /dev/serial/by-id/fake", argv)
        self.assertNotIn("--comprehensive", argv)
        self.assertEqual(btbrconsole.SKIP_EXIT, step.skip_returncode)

    def test_the_comprehensive_profile_adds_more_of_each(self):
        (case,) = cases("comprehensive")
        step = case.steps[-1]
        self.assertIn("--comprehensive", " ".join(step.resolve_argv({})))
        self.assertIn(f"{len(btbrconsole.checks(True))} checks", step.name)
        (smoke,) = cases("smoke")
        self.assertIn(f"{len(btbrconsole.checks(False))} checks",
                      smoke.steps[-1].name)

    def test_the_cases_patterns_match_a_real_session_report(self):
        """The case's patterns are matched against what the driver prints.

        Not against a report written here: a fixture of its own would agree
        with whatever the patterns say, which is how the `checks:` line and
        the pattern that reads it drifted apart once already. The driver is
        run against tests/fake_btbr.py, a stand-in console, and its own
        report is what the patterns have to match.
        """
        for coverage, comprehensive in (("smoke", False),
                                        ("comprehensive", True)):
            report = fake_session_report(comprehensive=comprehensive)
            (case,) = cases(coverage)
            step = case.steps[-1]
            with self.subTest(coverage):
                for pattern in step.output_patterns:
                    self.assertRegex(report, pattern, pattern)
                for pattern in step.forbidden_patterns:
                    self.assertIsNone(re.search(pattern, report), pattern)

    def test_the_option_step_runs_before_the_board_is_touched(self):
        (case,) = cases()
        first = case.steps[0]
        self.assertEqual("option-values", first.step_id)
        self.assertIn("btbrconsole.py options",
                      " ".join(first.resolve_argv({})))
        # It writes nothing: no board, no image, no serial.
        self.assertNotIn(SERIAL, " ".join(first.resolve_argv({})))
        self.assertFalse(first.mutates)
        self.assertEqual(btbrconsole.SKIP_EXIT, first.skip_returncode)

    def test_the_option_steps_patterns_match_a_real_options_report(self):
        # As with the session below: the driver is run against the fake and
        # its own report is what the patterns have to match.
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory) / "bin"
            environment = {"PATH": "/usr/bin:/bin",
                           PYTHON_TOOLS_ENV: str(tools),
                           **write_fake_btbr(tools)}
            out, err = io.StringIO(), io.StringIO()
            status = btbrconsole.run_options(out, err, environment)
        report = err.getvalue()
        self.assertEqual(0, status, report)
        (case,) = cases()
        step = case.steps[0]
        for pattern in step.output_patterns:
            self.assertRegex(report, pattern, pattern)
        for pattern in step.forbidden_patterns:
            self.assertIsNone(re.search(pattern, report), pattern)

    def test_a_failed_check_trips_the_forbidden_patterns(self):
        report = fake_session_report(faults=("quiet-lmp",))
        (case,) = cases()
        step = case.steps[-1]
        self.assertTrue(any(re.search(pattern, report)
                            for pattern in step.forbidden_patterns))

    def test_repeated_sessions_are_separate_cases_with_their_own_flash(self):
        repeated = cases(sessions=3)
        self.assertEqual(["btbr-console-1", "btbr-console-2", "btbr-console-3"],
                         [case.case_id for case in repeated])
        for number, case in enumerate(repeated, start=1):
            self.assertIn(f"({number} of 3)", case.name)
            self.assertIn("flash-btbr", [step.step_id for step in case.steps])
            self.assertIn("restore-application",
                          [step.step_id for step in case.cleanup])

    def test_the_peer_reset_checks_the_board_against_the_registered_name(self):
        (case,) = cases()
        reset = next(step for step in case.steps
                     if step.step_id == "reset-peer-before")
        report = (f"acceptor role: acceptor name: {PEER_NAME}")
        for pattern in reset.output_patterns:
            with self.subTest(pattern):
                self.assertRegex(report, pattern)
        self.assertFalse(any(
            re.search(pattern, "acceptor role: acceptor name: UT-ESP32-OTHER")
            for pattern in reset.output_patterns
            if PEER_NAME in pattern))
        # Both ends of the case reset it, so an earlier test's role, link or
        # `disc off` cannot reach this one and the next test finds it reset.
        self.assertIn("reset-peer-after",
                      [step.step_id for step in case.cleanup])

    def test_the_peers_name_comes_from_its_address(self):
        # The firmware builds it from the board's own LAP and the shell cannot
        # set it, so a registry need not carry it.
        (case,) = cases()
        self.assertIn(PEER_NAME, case.name)
        self.assertEqual(PEER_NAME, esp32peer.device_name(PEER_ADDRESS))
        # A registry may write it down, and then it has to agree.
        (same,) = cases(name=PEER_NAME.lower())
        self.assertEqual(case.name, same.name)
        with self.assertRaisesRegex(StateCaseError,
                                    "registered name is UT-ESP32-OTHER"):
            cases(name="UT-ESP32-OTHER")

    def test_the_step_deadline_outlasts_every_wait_the_driver_may_sit_out(self):
        # Counted from the driver's own windows, check by check, so a check
        # that grows a wait grows the budget with it.
        for coverage, comprehensive in (("smoke", False),
                                        ("comprehensive", True)):
            (case,) = cases(coverage)
            step = case.steps[-1]
            with self.subTest(coverage):
                self.assertGreater(
                    step.timeout_s,
                    btbrconsole.worst_case_seconds(comprehensive))
        self.assertGreater(btbrconsole.worst_case_seconds(True),
                           btbrconsole.worst_case_seconds(False))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.definition = builtin_catalog().get("Classic btbr console")

    def test_it_binds_one_board_and_one_peer_and_writes_firmware(self):
        self.assertEqual({"Ubertooth": "Ubertooth",
                          "acceptor": "ESP32 Classic peer"},
                         dict(self.definition.hardware_roles))
        # It writes the board's firmware and ends with the application image
        # verified, so it must be the last test on that board and the run
        # holds the host lock.
        self.assertTrue(self.definition.establishes_firmware)
        self.assertTrue(self.definition.writes_firmware)
        self.assertTrue(self.definition.guards_other_ubertooths)
        self.assertTrue(self.definition.continue_on_case_failure)

    def test_it_asks_for_consent_to_transmit(self):
        # The board transmits: an inquiry train, a page train, and then a
        # live connection it holds as the piconet central.
        rule = self.definition.arguments["allow RF transmission"]
        self.assertTrue(rule.required)

    def test_it_asks_for_an_ordinary_link(self):
        placement = self.definition.placement_requirements(
            {"coverage": "smoke", "sessions": 1,
             "allow RF transmission": True})
        self.assertTrue(placement["Ubertooth"].antenna)
        self.assertEqual(0, placement["Ubertooth"].max_attenuation_db)

    def test_the_estimate_scales_with_the_session_count(self):
        for coverage in ("smoke", "comprehensive"):
            one = self.definition.estimated_seconds(
                {"coverage": coverage, "sessions": 1})
            three = self.definition.estimated_seconds(
                {"coverage": coverage, "sessions": 3})
            with self.subTest(coverage):
                self.assertAlmostEqual(3 * one, three, places=1)
        self.assertGreater(
            self.definition.estimated_seconds(
                {"coverage": "comprehensive", "sessions": 1}),
            self.definition.estimated_seconds(
                {"coverage": "smoke", "sessions": 1}))


if __name__ == "__main__":
    unittest.main()
