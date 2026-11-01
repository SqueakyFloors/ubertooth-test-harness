"""`Classic btbr console`: the case, the two firmware images, and the catalog."""

from pathlib import Path
import re
import tempfile
import unittest

from ubertooth_harness import btbr_cases, btbrconsole
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
from ubertooth_harness.patterns import firmware_image, image_name_pattern
from ubertooth_harness.plan import image_label
from ubertooth_harness.stateful import (
    APPLICATION_IMAGE_LABEL,
    BTBR_IMAGE_LABEL,
    StateCaseError,
)

SERIAL = "0123456789abcdef0123456789abcdef"
UBERTOOTH = SourceRevision(
    "Ubertooth", "/repos/ubertooth", "https://example.invalid/ubertooth.git",
    "main", "a" * 40, "aaaaaaa",
)
LIBBTBB = SourceRevision(
    "libbtbb", "/repos/libbtbb", "https://example.invalid/libbtbb.git",
    "main", "b" * 40, "bbbbbbb",
)
PEER_ADDRESS = "C8:85:41:2A:1B:2A"
PEER_NAME = "UT-ESP32-2A1B2A"
RESOURCES = {"Ubertooth": "UT_Linux0", "acceptor": "ESP32_Linux0"}


def identities(name=PEER_NAME):
    peer = {"bridge serial": "a" * 32, "BD_ADDR": PEER_ADDRESS,
            "serial device": "/dev/serial/by-id/fake"}
    if name is not None:
        peer["name"] = name
    return {"Ubertooth": {"serial": SERIAL}, "acceptor": peer}


def cases(coverage="smoke", sessions=1, name=PEER_NAME):
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

    def test_a_firmware_build_makes_and_names_both_images(self):
        with tempfile.TemporaryDirectory() as work:
            plan = plan_build(
                ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work),
                platform="linux", with_firmware=True)
            steps = [command.step_id for command in plan.commands]
            self.assertIn("firmware-build", steps)
            self.assertIn("firmware-build-btbr", steps)
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
        self.assertEqual("btbr", firmware_image(self.BTBR_LINE))
        self.assertEqual("bluetooth_rxtx",
                         firmware_image(self.APPLICATION_LINE))
        self.assertIsNone(firmware_image(self.OLD_LINE))
        self.assertIsNone(firmware_image("nothing of the kind"))


class CaseTests(unittest.TestCase):
    def test_one_session_is_one_case_that_flashes_once_and_restores(self):
        (case,) = cases()
        self.assertEqual("btbr-console", case.case_id)
        self.assertEqual(
            ["read-original-image", "flash-btbr", "verify-btbr-image",
             "verify-btbr-build", "reset-peer-before", "console-session"],
            [step.step_id for step in case.steps])
        self.assertEqual(
            ["restore-application", "verify-restored-image",
             "reset-peer-after"],
            [step.step_id for step in case.cleanup])
        # Both images, so a run without either refuses the case instead of
        # flashing a board it cannot put back.
        self.assertEqual((BTBR_IMAGE_LABEL, APPLICATION_IMAGE_LABEL),
                         case.artifacts)
        self.assertEqual(("linux",), case.needs.platforms)
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
        self.assertNotIn("--no-answer", argv)
        self.assertEqual(btbrconsole.SKIP_EXIT, step.skip_returncode)
        # The step's deadline outlasts every window the driver may wait out,
        # so a failing check ends in the driver's reason.
        self.assertGreater(step.timeout_s,
                           btbr_cases.console_budget(no_answer=False))

    def test_the_comprehensive_profile_adds_the_page_nothing_answers(self):
        (case,) = cases("comprehensive")
        step = case.steps[-1]
        self.assertIn("--no-answer", " ".join(step.resolve_argv({})))
        self.assertGreater(step.timeout_s,
                           btbr_cases.console_budget(no_answer=True))
        self.assertIn("12 checks", step.name)

    def test_the_patterns_accept_a_passing_report_and_reject_a_failing_one(self):
        (case,) = cases()
        step = case.steps[-1]
        passing = "\n".join((
            f"peer address: {PEER_ADDRESS}",
            "prompt: up after 1.9 s",
            "check help: 10 commands listed in 0.1 s",
            "check inquiry: peer c885412a1b2a answered after 2.1 s, name "
            f"'{PEER_NAME}', 2 device(s) heard in all",
            f"check page: connected to {PEER_ADDRESS} in 6.6 s, lt_addr=1, "
            "LMP setup complete",
            f"check page-peer-name: LMP_NAME_RES carried '{PEER_NAME}'",
            "check lmp-unhandled: 0x20 refused, each named once",
            "check streams: 181 capture lines on stdout, no progress line "
            "among them",
            "session exit: 0 after 41.2 s",
            "checks: 11 passed of 11, 0 failed",
        ))
        for pattern in step.output_patterns:
            with self.subTest(pattern):
                self.assertRegex(passing, pattern)
        for pattern in step.forbidden_patterns:
            with self.subTest(f"forbidden {pattern}"):
                self.assertIsNone(re.search(pattern, passing))
        failing = passing.replace(
            "check lmp-unhandled: 0x20 refused, each named once",
            "check lmp-unhandled: FAILED: an unhandled LMP opcode was "
            "reported more than once: 0x20 7 times")
        self.assertTrue(any(re.search(pattern, failing)
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

    def test_a_peer_with_no_registered_name_is_refused(self):
        for name in (None, "", "   "):
            with self.subTest(repr(name)):
                with self.assertRaisesRegex(StateCaseError,
                                            "registered name"):
                    cases(name=name)


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

    def test_it_asks_for_an_ordinary_link(self):
        placement = self.definition.placement_requirements(
            {"coverage": "smoke", "sessions": 1})
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
