"""The two places that read a command's saved output through `output_of`.

Each raised `NameError` on the first line that called `output_of`, so these
call them with output that matches and output that does not.

`sequencer._record_bootloader_identity` records preflight's `ubertooth-util
-B` read, and it is the one that reached a real board first: it failed on the
bench after the build and the flash had already run.
`catalog.TestDefinition.result_failure` judges a test's `output_pattern`, and
has no production caller today -- `command_cases` yields `cases.CommandCase`,
whose own module imported the helper -- so that half was latent rather than
live. Both are tested here because either could be called tomorrow.
"""

import unittest

from ubertooth_harness import bootloader, catalog, sequencer
from ubertooth_harness.outputfiles import output_of
from ubertooth_harness.preflight import PreflightCheck


class Result:
    """Enough of a CommandResult for `output_of`: it reads the streams."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = False
        self.cancelled = False
        self.output = None


class Publisher:
    def __init__(self):
        self.events = []

    def emit(self, kind, status, details, **_kwargs):
        self.events.append((kind, status, details))


def Check(expectation, *, serial="0A1B2C3DEEFF"):
    """The real preflight check, so the record reads the real verdict.

    A stand-in with only the fields the record touches would have to repeat
    the verdict rule, which is the one thing the record must not have a
    second copy of.
    """
    return PreflightCheck(
        check_id="preflight-001-bootloader-identity",
        name="Bootloader identity",
        host="Host_Linux",
        resource_alias="UT_Debug",
        resource_identity={"serial": serial},
        argv=bootloader.identity_argv(serial.lower()),
        expected_patterns=expectation.patterns("UT_Debug"),
        expected_revision="git-1a2b3c4",
        bootloader=expectation,
    )


IDENTITY = "Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4\n"
OTHER = "Bootloader: 2.00 git-9999999, CRC32:0x11223344\n"


def expectation(mode, expected):
    return bootloader.BootloaderExpectation(mode=mode, expected=expected, boards={})


class HostVersionOutputPatternTests(unittest.TestCase):
    """catalog.py: the output pattern is judged through `output_of`."""

    def setUp(self):
        self.definition = catalog.builtin_catalog().get("Host version")
        self.assertTrue(self.definition.output_pattern)

    def test_matching_output_is_not_a_failure(self):
        result = Result(
            stdout="Host tools version: libubertooth 1.58 (git-abc1234), "
                   "libbtbb 1.3 (git-7e1cdf0)\n"
        )
        self.assertEqual("", self.definition.result_failure(result))

    def test_missing_pattern_is_reported(self):
        failure = self.definition.result_failure(Result(stdout="nothing useful\n"))
        self.assertIn("Host version", failure)
        self.assertIn("was not found", failure)


class BootloaderIdentityRecordTests(unittest.TestCase):
    """sequencer.py: the `-B` line is read back out of the saved output."""

    def record(self, output, expectation_, failure=""):
        publisher = Publisher()
        sequencer._record_bootloader_identity(
            publisher, Check(expectation_), Result(stdout=output), failure
        )
        self.assertEqual(1, len(publisher.events))
        return publisher.events[0]

    def test_an_identity_that_matches_records_current(self):
        # The same line parsed the same way, so "current" means the board
        # reported exactly the artifact the run expects.
        installed = bootloader.identity_from_match(
            output_of(IDENTITY).search(bootloader.IDENTITY_LINE_PATTERN)
        )
        self.assertIsNotNone(installed)
        kind, status, details = self.record(IDENTITY, expectation("check", installed))
        self.assertEqual("current", details["status"])
        self.assertEqual(("bootloader_identity", "passed"), (kind, status))
        self.assertEqual("UT_Debug", details["resource"])
        # Lower cased, so one serial is one board in the GUI's board list.
        self.assertEqual("0a1b2c3deeff", details["serial"])
        self.assertIsNotNone(details["installed"])
        self.assertFalse(details["written"])

    def test_record_mode_reads_the_line_with_nothing_to_compare(self):
        _, status, details = self.record(IDENTITY, expectation("record", None))
        self.assertEqual("passed", status)
        self.assertEqual("current", details["status"])
        self.assertIsNotNone(details["installed"])

    def test_output_with_no_identity_line_is_unreadable(self):
        _, _, details = self.record("ubertooth-util: no such option\n",
                                    expectation("record", None))
        self.assertIsNone(details["installed"])
        self.assertEqual("unreadable", details["status"])

    def test_an_identity_that_differs_records_stale(self):
        # What `check` mode exists for: the board answered, and with something
        # other than the artifact this run built.
        expected = bootloader.identity_from_match(
            output_of(IDENTITY).search(bootloader.IDENTITY_LINE_PATTERN)
        )
        _, _, details = self.record(OTHER, expectation("check", expected))
        self.assertEqual("stale", details["status"])
        self.assertIsNotNone(details["installed"])
        self.assertNotEqual(details["installed"], details["expected"])
        self.assertFalse(details["written"])

    def test_a_failure_reason_is_carried_and_marks_the_event_failed(self):
        _, status, details = self.record(
            IDENTITY, expectation("record", None), failure="read refused"
        )
        self.assertEqual("failed", status)
        self.assertEqual("read refused", details["reason"])


if __name__ == "__main__":
    unittest.main()
