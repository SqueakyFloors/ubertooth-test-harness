import io
import os
import re
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import dfuimage, refusal_cases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.manifest import FileIdentity, SourceRevision, build_run_manifest
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.preflight import PreflightCheck
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.stateful import StateCase
from tests.test_sequencer import CollectSink
from tests.test_tool_cases import failed_names

NAME = "DFU image checks"
NOTE = "note: UBERTOOTH_TEST_NO_DEVICES is set, so no board is selected\n"
_MARKER = re.compile(rb"ubertooth ([\x21-\x7e]+) \(")


class FakeDfu:
    """ubertooth-dfu's file handling and image check as at fork 6fee30c, run
    with UBERTOOTH_TEST_NO_DEVICES=1, plus the case helpers in process.

    ``fixed`` models the fix of fork #305 (-s reads its input before it opens
    the output). ``bugs``: check-late (the board lookup comes before the image
    check), touch-image (a refused -d rewrites its image), double-suffix (-s
    signs an already suffixed .bin), needs-writable (-s opens its input r+b),
    other-product (-s writes product 0x6002). ``switch`` False is a library
    from before b6d8ede."""

    def __init__(self, *, fixed=False, bugs=(), switch=True):
        self.fixed = fixed
        self.bugs = set(bugs)
        self.switch = switch
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-H"):
            return self._result(command, 0, "libubertooth 1.1 (git-test), libbtbb 1.0\n")
        if command[0] == "mkdir":
            os.mkdir(command[1])
            return self._result(command, 0, "")
        if command[0] == sys.executable:
            helper = Path(command[1]).name
            if helper == "refusal.py":
                answer = ("yes (/p/lib/libubertooth.so.1)" if self.switch else
                          "no: /p/lib/libubertooth.so.1 predates UBERTOOTH_TEST_NO_DEVICES")
                return self._result(command, 0, f"No-device switch: {answer}\n")
            self.assert_equal("dfuimage.py", helper)
            out = io.StringIO()
            with redirect_stdout(out):
                code = dfuimage.main(list(command[2:]))
            return self._result(command, code, out.getvalue())
        self.assert_equal(refusal_cases.NO_DEVICES + ("ubertooth-dfu",), command[:3])
        return self._dfu(command, list(command[3:]))

    @staticmethod
    def assert_equal(expected, seen):
        if expected != seen:
            raise AssertionError(f"{expected!r} != {seen!r}")

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _dfu(self, command, words):
        rest = [word for word in words if word != "-r"]
        options = dict(zip(rest[0::2], rest[1::2]))
        sign, image = options.get("-s"), options.get("-d")
        if sign is not None and not sign.endswith(".bin"):
            return self._result(command, 1, "Error: -s requires a .bin\n")
        if image is not None and not Path(image).exists():
            return self._result(command, 1, "", f"{image}: No such file or directory\n")
        if sign is not None:
            failure = self._sign(Path(sign))
            if failure:
                return self._result(command, 1, "", failure)
            if image is None:
                return self._result(command, 0, "")
        out = "Checking firmware signature\n"
        if "check-late" in self.bugs:
            return self._result(command, 1, out, "DFU +0.000s: operation started\n" + NOTE)
        problem = self._check(Path(image), "-r" in words)
        if problem:
            if "touch-image" in self.bugs and Path(image).is_file():
                Path(image).write_bytes(Path(image).read_bytes() + b"\0")
            return self._result(command, 1, out, problem)
        revision = _MARKER.search(Path(image).read_bytes()).group(1).decode()
        return self._result(command, 1, out, f"DFU expected revision: {revision}\n"
                            "DFU +0.000s: operation started\n" + NOTE
                            + "could not open Ubertooth device\n")

    def _sign(self, source: Path) -> str:
        target = source.with_suffix(".dfu")
        if not source.exists():
            return f"{source}: No such file or directory\n"
        if source.is_file():
            data = source.read_bytes()
            if (len(data) >= 16 and data[-5] == 16 and data[-8:-5] == b"UFD"
                    and "double-suffix" not in self.bugs):
                return f"error: {source} already has a DFU suffix, so {target} was not written\n"
            if "needs-writable" in self.bugs and not os.access(source, os.W_OK):
                return f"{source}: Permission denied\n"
        if not self.fixed:
            target.write_bytes(b"")  # fork #305: opened "w+b" before the read
        if source.is_dir():
            return "error: read 0 of 64 byte(s) from the input file\n"
        product = 0x6002 if "other-product" in self.bugs else dfuimage.PRODUCT
        target.write_bytes(dfuimage.with_suffix(source.read_bytes(), product=product))
        return ""

    @staticmethod
    def _check(path: Path, want_revision: bool) -> str:
        failed = "Signature check failed, firmware will not be updated\n"
        if path.is_dir():
            return "Could not read the whole file\n" + failed
        data = path.read_bytes()
        if len(data) < 16:
            return (f"Too short for a 16-byte DFU suffix: the file is {len(data)} byte(s)\n"
                    + failed)
        device, product, vendor, bcd, signature, length, crc = struct.unpack(
            "<HHHH3sBI", data[-16:])
        if length != 16:
            return f"Unknown DFU suffix length: {length}\n" + failed
        if bcd not in (0x0100, 0x0101):
            return f"Unknown DFU version: {bcd:04x}\n" + failed
        if signature != b"UFD":
            return "DFU Signature mismatch: not a DFU file\n" + failed
        if len(data) == 16:
            return "DFU suffix with no firmware: nothing to write\n" + failed
        calculated = dfuimage.dfu_crc(data[:-4])
        if calculated != crc:
            return f"CRC mismatch: calculated: 0x{calculated:x}, found: 0x{crc:x}\n" + failed
        if vendor not in (0x1D50, 0xFFFF):
            return (f"DFU suffix is for USB vendor 0x{vendor:04x}, not 0x1d50: "
                    "this image is for another device\n" + failed)
        if product not in (0x6003, 0xFFFF):
            return (f"DFU suffix is for USB product 0x{product:04x}, not 0x6003: "
                    "this image is for another device\n" + failed)
        if want_revision and not _MARKER.search(data):
            return "Unable to determine expected firmware revision from image\n"
        return ""


def cases(coverage):
    definition = builtin_catalog().get(NAME)
    arguments = definition.normalize_arguments({"hosts": ["Host_Mac"], "coverage": coverage})
    return arguments, definition.command_cases(arguments, {}, {})


def run(executor, coverage="comprehensive", test_build=None):
    arguments, _cases = cases(coverage)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "plan.yaml").write_text("plan\n")
        (root / "hardware.yaml").write_text("hardware\n")
        artifacts = ()
        if test_build is not None:
            image = root / "bluetooth_rxtx.dfu"
            image.write_bytes(test_build)
            artifacts = (FileIdentity.from_path("test build", image),)
        invocation = TestInvocation(f"001-{NAME}", NAME, "Host_Mac", arguments, {}, {})
        manifest = build_run_manifest(
            run_id="named-run", plan_name="named", suite="host", host="Host_Mac",
            plan_path=root / "plan.yaml", hardware_path=root / "hardware.yaml",
            sources=(SourceRevision("Ubertooth", "/source", "https://example.invalid/u.git",
                                    "test-ref", "a" * 40, "test"),),
            invocations=(invocation,), artifacts=artifacts,
            created_at="2026-10-22T00:00:00+00:00",
        )
        check = PreflightCheck("preflight-001-host-version", "Host version", "Host_Mac",
                               None, {}, ("ubertooth-util", "-H"),
                               (r"libubertooth\s+\S+\s+\(git\-test\)",), "git-test")
        sink = CollectSink()
        summary = NamedTestSequencer(executor, event_sink=sink).run(
            (invocation,), root / "run", run_id="named-run", manifest=manifest,
            preflight_checks=(check,))
        return summary, sink.events


def skipped_names(events):
    return sorted(e.details["case_name"] for e in events
                  if e.event_type == "check_finished" and e.status == "skipped")


def names(*case_ids, coverage="comprehensive"):
    _a, found = cases(coverage)
    by_id = {case.case_id: case.name for case in found}
    return sorted(by_id[case_id] for case_id in case_ids)


STRICT = names("sign-unreadable-input")
REJECTS = [f"reject-{kind}" for kind in dfuimage.BAD_KINDS]


class HelperTests(unittest.TestCase):

    def test_edge_images_fail_the_suffix_check_except_the_accepted_ones(self):
        image = dfuimage.with_suffix(dfuimage.synthetic_binary())
        self.assertEqual([], dfuimage.suffix_problems(image))
        for kind in dfuimage.EDGE_KINDS:
            edge = dfuimage.make_edge(kind, image)
            problems = dfuimage.suffix_problems(edge)
            # suffix_problems() checks the suffix only, not the payload.
            if kind in ("dfu-1.1", "no-marker", "suffix-only"):
                self.assertEqual([], problems, kind)
            elif kind == "wildcard":
                self.assertEqual(["VID:PID ffff:ffff"], problems)
            else:
                self.assertTrue(problems, kind)
        long_length = dfuimage.make_edge("long-suffix-length", image)
        self.assertEqual(80, len(long_length))
        self.assertEqual(200, long_length[-5])
        self.assertNotIn(b"ubertooth ", dfuimage.make_edge("no-marker", image))

    def test_synthetic_binary_names_its_revision_and_is_not_whole_blocks(self):
        binary = dfuimage.synthetic_binary()
        self.assertEqual(dfuimage.SYNTHETIC_REVISION.encode(), _MARKER.search(binary).group(1))
        self.assertNotEqual(0, len(binary) % dfuimage.BLOCK_SIZE)
        self.assertEqual(binary, dfuimage.synthetic_binary())

    def test_state_reports_added_removed_and_changed_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.dfu").write_bytes(b"one")
            (root / "gone.bin").write_bytes(b"x")
            (root / "folder.bin").mkdir()
            before = dfuimage.directory_state(root)
            self.assertEqual("dir", before["folder.bin"])
            self.assertEqual([], dfuimage.state_changes(before, before))
            (root / "a.dfu").write_bytes(b"")
            (root / "gone.bin").unlink()
            (root / "a.dfu.XXXXXX").write_bytes(b"")
            changes = dfuimage.state_changes(before, dfuimage.directory_state(root))
            self.assertEqual(3, len(changes))
            self.assertTrue(changes[0].startswith("a.dfu.XXXXXX added"))
            self.assertEqual("gone.bin removed", changes[1])
            self.assertTrue(changes[2].startswith("a.dfu changed (3 bytes"))

    def test_test_build_line_without_a_path_says_none(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(0, dfuimage.main(["test-build", ""]))
        self.assertTrue(out.getvalue().startswith("Test build: none: "))


class CaseTests(unittest.TestCase):

    def test_case_sets_and_no_board(self):
        definition = builtin_catalog().get(NAME)
        self.assertTrue(definition.implemented)
        self.assertEqual({}, dict(definition.hardware_roles or {}))
        _a, smoke = cases("smoke")
        self.assertEqual(7 + 1 + 5, len(smoke))
        _a, full = cases("comprehensive")
        self.assertEqual(len(smoke) + 13, len(full))
        self.assertEqual(len(full), len({case.case_id for case in full}))
        context = {"capture dir": "/run/captures/x", "directory before": "{}"}
        for case in full:
            self.assertIsInstance(case, StateCase)
            self.assertTrue(case.capture_dir)
            self.assertEqual((), case.artifacts)
            self.assertEqual("probe", case.steps[0].step_id)
            for step in case.steps + case.cleanup:
                if step.step_id in ("strip", "check") and case.case_id == "sign-test-build":
                    continue  # need the test build's path
                argv = step.resolve_argv(context)
                self.assertFalse(step.mutates or step.continuous)
                if argv[0] in ("env", "ubertooth-dfu"):
                    # Every tool runs with the switch, one command per step.
                    self.assertEqual(refusal_cases.NO_DEVICES + ("ubertooth-dfu",), argv[:3])
                    self.assertTrue(step.name.startswith("env UBERTOOTH_TEST_NO_DEVICES=1 "))
                    if "-d" in argv:
                        self.assertIn("-r", argv)
                for path in (a for a in argv if a.startswith("/run")):
                    self.assertTrue(path.startswith("/run/captures/x"), path)
        test_build = next(case for case in smoke if case.case_id == "sign-test-build")
        self.assertEqual(("test_build",), test_build.optional_artifacts)

    def test_fork_tools_pass_smoke_and_fail_only_the_strict_case(self):
        summary, events = run(FakeDfu(), coverage="smoke")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(names("sign-test-build"), skipped_names(events))
        summary, events = run(FakeDfu())
        self.assertFalse(summary.passed)
        self.assertEqual(STRICT, failed_names(events))

    def test_fixed_tools_pass_everything(self):
        summary, _events = run(FakeDfu(fixed=True))
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_test_build_is_signed_and_compared_when_the_run_has_one(self):
        build = dfuimage.with_suffix(b"ubertooth git-test (builder) " + bytes(range(200)))
        tools = FakeDfu(fixed=True)
        summary, events = run(tools, coverage="smoke", test_build=build)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual([], skipped_names(events))
        self.assertTrue(any(c[-1].endswith("build.bin") and c[3:4] == ("-s",)
                            for c in tools.commands))
        summary, events = run(FakeDfu(fixed=True, bugs=("other-product",)),
                              coverage="smoke", test_build=build)
        self.assertFalse(summary.passed)
        self.assertEqual(names("sign-synthetic", "sign-test-build", "sign-read-only-input",
                               coverage="smoke"), failed_names(events))

    def test_a_board_lookup_before_the_image_check_fails_every_refusal(self):
        summary, events = run(FakeDfu(fixed=True, bugs=("check-late",)), coverage="smoke")
        self.assertFalse(summary.passed)
        # The good image never reaches a check that names its revision.
        self.assertEqual(names(*REJECTS, "accept-synthetic", coverage="smoke"),
                         failed_names(events))

    def test_a_refusal_that_rewrites_the_image_fails(self):
        summary, events = run(FakeDfu(fixed=True, bugs=("touch-image",)))
        self.assertFalse(summary.passed)
        edge = [f"reject-{kind}" for kind in ("empty", "short", "suffix-only",
                                              "long-suffix-length", "dfu-2.0", "no-marker")]
        self.assertEqual(names(*REJECTS, *edge), failed_names(events))

    def test_double_suffix_and_writable_input_fail(self):
        summary, events = run(FakeDfu(fixed=True, bugs=("double-suffix",)))
        self.assertEqual(names("sign-refuses-suffixed-bin", "sign-refuses-corrupt-suffix"),
                         failed_names(events))
        summary, events = run(FakeDfu(fixed=True, bugs=("needs-writable",)), coverage="smoke")
        self.assertEqual(names("sign-read-only-input", coverage="smoke"), failed_names(events))

    def test_old_library_skips_without_running_a_tool(self):
        tools = FakeDfu(switch=False)
        summary, events = run(tools)
        self.assertTrue(summary.passed, summary.failure_reason)
        _a, full = cases("comprehensive")
        self.assertEqual(len(full), len(skipped_names(events)))
        self.assertFalse(any(c[:2] == refusal_cases.NO_DEVICES or c[0] == "mkdir"
                             for c in tools.commands))


if __name__ == "__main__":
    unittest.main()
