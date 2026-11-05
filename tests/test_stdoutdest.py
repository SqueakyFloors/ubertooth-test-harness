"""stdoutdest.py: the descriptors it hands a tool, and what it judges after."""

import fcntl
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from hashlib import sha256
from pathlib import Path

from ubertooth_harness import stdoutdest

HELPER = str(Path(stdoutdest.__file__).resolve())
# A stand-in for a capture tool: it writes bytes to standard output, to a
# descriptor it was given, and a line to standard error.
TOOL = (
    "import os, sys\n"
    "sys.stdout.buffer.write(b'CAPTURE')\n"
    "sys.stdout.buffer.flush()\n"
    "if len(sys.argv) > 1:\n"
    "    os.write(int(sys.argv[1]), b'ON-FD')\n"
    "sys.stderr.write('warning: a diagnostic\\n')\n"
    "sys.exit(int(sys.argv[2]) if len(sys.argv) > 2 else 0)\n"
)


def run(argv):
    """The helper in this process: its exit status and what it printed."""
    out = io.StringIO()
    with redirect_stdout(out):
        status = stdoutdest.main(argv)
    return status, out.getvalue()


@contextmanager
def inherited_streams(stdout_path, stderr_path):
    """Descriptors 1 and 2 on these files, as a calling shell would put them.

    redirect_stdout moves only this process's sys.stdout, and a tool the
    helper runs inherits the descriptors, so a test about what an inherited
    stream receives has to move the descriptors themselves -- and so that a
    tool's own output does not land in the test runner's log.
    """
    # Well above the descriptors the helper hands a tool, which start at 3:
    # os.dup() would answer with one of those and the helper would duplicate
    # a file over it.
    # Descriptor 3 as well as 1 and 2: run() duplicates a file onto the
    # number a caller asks for and closes that number afterwards, so a
    # descriptor this process (or another test module) held there would come
    # back dead. In the harness the helper is a process of its own and there
    # is nothing to protect; these tests call it in this one.
    saved = {number: fcntl.fcntl(number, fcntl.F_DUPFD, 20)
             for number in (1, 2, 3) if _is_open(number)}
    for number, path in ((1, stdout_path), (2, stderr_path)):
        with open(path, "wb") as handle:
            os.dup2(handle.fileno(), number)
    try:
        yield
    finally:
        for number, original in saved.items():
            os.dup2(original, number)
            os.close(original)


def _is_open(number):
    try:
        os.fstat(number)
    except OSError:
        return False
    return True


class RunTests(unittest.TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.inherited_out = self.directory / "inherited.out"
        self.inherited_err = self.directory / "inherited.err"

    def tool(self, *arguments):
        return [sys.executable, "-c", TOOL, *[str(item) for item in arguments]]

    def helper(self, *arguments):
        """The helper with the calling streams on files of this test's own."""
        with inherited_streams(self.inherited_out, self.inherited_err):
            return run(["run", *arguments])

    def test_standard_output_goes_to_the_named_file(self):
        target = self.directory / "capture.bin"
        status, printed = self.helper("--stdout", str(target), "--", *self.tool())
        self.assertEqual(0, status)
        self.assertEqual(b"CAPTURE", target.read_bytes())
        # The diagnostic is standard error's, which no capture ever takes.
        self.assertEqual(b"warning: a diagnostic\n", self.inherited_err.read_bytes())
        self.assertIn("stdout -> capture.bin (new)", printed)
        self.assertIn("capture.bin: 7 bytes", printed)

    def test_an_append_keeps_what_the_file_held(self):
        target = self.directory / "dump.bin"
        target.write_bytes(b"RECORDS")
        status, _printed = self.helper(
            "--stdout", str(target), "--append", "--", *self.tool())
        self.assertEqual(0, status)
        self.assertEqual(b"RECORDSCAPTURE", target.read_bytes())

    def test_a_new_destination_must_not_exist(self):
        target = self.directory / "capture.bin"
        target.write_bytes(b"")
        status, printed = self.helper("--stdout", str(target), "--", *self.tool())
        self.assertEqual(2, status)
        self.assertIn("Stdout destination problem:", printed)

    def test_a_descriptor_reaches_the_file_it_was_opened_on(self):
        target = self.directory / "fd3.bin"
        status, printed = self.helper("--fd", f"3={target}", "--", *self.tool(3))
        self.assertEqual(0, status)
        self.assertEqual(b"ON-FD", target.read_bytes())
        # Without --stdout the tool keeps the caller's standard output.
        self.assertEqual(b"CAPTURE", self.inherited_out.read_bytes())
        self.assertIn("fd 3 -> fd3.bin", printed)
        self.assertIn("fd3.bin: 5 bytes", printed)

    def test_a_descriptor_can_be_pointed_at_the_same_stream_as_stdout(self):
        target = self.directory / "both.bin"
        status, _printed = self.helper(
            "--stdout", str(target), "--fd", "3=stdout", "--", *self.tool(3))
        self.assertEqual(0, status)
        self.assertEqual(b"CAPTUREON-FD", target.read_bytes())

    def test_that_descriptor_needs_a_stream_to_point_at(self):
        status, printed = self.helper("--fd", "3=stdout", "--", *self.tool())
        self.assertEqual(2, status)
        self.assertIn("needs --stdout", printed)

    def test_the_tools_status_is_the_helpers(self):
        status, printed = self.helper("--", *self.tool(0, 1))
        self.assertEqual(1, status)
        self.assertIn("exit 1", printed)

    def test_stdout_and_an_extra_descriptor_reach_their_own_files(self):
        # The stdout file's own open may land on the very number a --fd asks
        # for, so the descriptors are taken first: otherwise the --fd would
        # replace it and both outputs would go to one file.
        out = self.directory / "out.bin"
        three = self.directory / "three.bin"
        status, _printed = self.helper(
            "--stdout", str(out), "--fd", f"3={three}", "--", *self.tool(3))
        self.assertEqual(0, status)
        self.assertEqual(b"CAPTURE", out.read_bytes())
        self.assertEqual(b"ON-FD", three.read_bytes())

    def test_a_descriptor_number_may_be_given_once(self):
        status, printed = self.helper(
            "--fd", f"3={self.directory / 'a.bin'}",
            "--fd", f"3={self.directory / 'b.bin'}", "--", *self.tool(3))
        self.assertEqual(2, status)
        self.assertIn("may be given once", printed)

    def test_the_descriptors_it_opened_are_closed_again(self):
        # A run leaves nothing open: the file the descriptor was opened on
        # would otherwise still be held after the tool it was for had gone.
        before = len(os.listdir("/dev/fd"))
        for index in range(3):
            self.helper("--fd", f"3={self.directory / f'f{index}.bin'}",
                        "--", *self.tool(3))
        self.assertLessEqual(len(os.listdir("/dev/fd")), before)

    def test_a_standard_stream_is_refused_as_an_extra_descriptor(self):
        # argparse writes its usage and reason to standard error, which is
        # the suite's own here, so it is redirected rather than printed in
        # the middle of a test run.
        with open(self.directory / "argparse.err", "w") as handle:
            with redirect_stderr(handle), self.assertRaises(SystemExit):
                run(["run", "--fd", "1=/tmp/x", "--", "true"])
        self.assertIn("descriptor 1 is a standard stream",
                      (self.directory / "argparse.err").read_text())

    def test_no_tool_is_a_usage_failure(self):
        status, printed = run(["run", "--"])
        self.assertEqual(2, status)
        self.assertIn("no tool to run", printed)

    def test_the_helper_runs_as_a_script_by_path(self):
        # The cases run it with the harness's interpreter and no package on
        # the path, so it must import nothing of its own.
        target = self.directory / "script.bin"
        completed = subprocess.run(
            [sys.executable, HELPER, "run", "--stdout", str(target), "--",
             *self.tool()],
            capture_output=True, text=True, cwd="/", check=True)
        self.assertEqual(b"CAPTURE", target.read_bytes())
        self.assertIn("warning: a diagnostic", completed.stderr)
        self.assertIn("Stdout destination:", completed.stdout)


class CheckTests(unittest.TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

    def check(self, *arguments):
        return run(["check", str(self.directory), *arguments])

    def test_an_unchanged_file_and_an_empty_directory(self):
        (self.directory / "input.bin").write_bytes(b"records")
        digest = sha256(b"records").hexdigest()
        status, printed = self.check("--sha256", f"input.bin={digest}")
        self.assertEqual(0, status)
        self.assertIn("1 unchanged", printed)

    def test_a_rewritten_file_is_reported(self):
        (self.directory / "input.bin").write_bytes(b"gone")
        digest = sha256(b"records").hexdigest()
        status, printed = self.check("--sha256", f"input.bin={digest}")
        self.assertEqual(1, status)
        self.assertIn("input.bin holds 4 bytes hashing to", printed)

    def test_a_missing_file_is_reported(self):
        status, printed = self.check("--sha256", "input.bin=" + sha256(b"").hexdigest())
        self.assertEqual(1, status)
        self.assertIn("input.bin is gone", printed)

    def test_a_prefix_needs_the_start_kept_and_something_added(self):
        digest = sha256(b"records").hexdigest()
        path = self.directory / "input.bin"
        path.write_bytes(b"records" + b"decode")
        self.assertEqual(0, self.check("--prefix", f"input.bin={digest}:7")[0])
        path.write_bytes(b"records")
        status, printed = self.check("--prefix", f"input.bin={digest}:7")
        self.assertEqual(1, status)
        self.assertIn("0 more than the 7 it started with", printed)
        path.write_bytes(b"spoiled" + b"decode")
        status, printed = self.check("--prefix", f"input.bin={digest}:7")
        self.assertEqual(1, status)
        self.assertIn("no longer starts with", printed)

    def test_a_prefix_can_require_a_minimum_of_added_bytes(self):
        digest = sha256(b"records").hexdigest()
        path = self.directory / "input.bin"
        path.write_bytes(b"records" + b"x")
        # One byte is something, and not the report the case is about.
        self.assertEqual(0, self.check("--prefix", f"input.bin={digest}:7")[0])
        status, printed = self.check("--prefix", f"input.bin={digest}:7:500")
        self.assertEqual(1, status)
        self.assertIn("1 more than the 7 it started with, where 500 were "
                      "expected", printed)
        path.write_bytes(b"records" + b"x" * 500)
        self.assertEqual(0, self.check("--prefix", f"input.bin={digest}:7:500")[0])

    def test_a_prefix_without_a_length_is_a_usage_failure(self):
        status, printed = self.check("--prefix", "input.bin=abc")
        self.assertEqual(1, status)
        self.assertIn("is not NAME=HEX:LENGTH", printed)

    def test_an_absent_name_must_not_exist(self):
        self.assertEqual(0, self.check("--absent", "cap.pcap")[0])
        (self.directory / "cap.pcap").write_bytes(b"")
        status, printed = self.check("--absent", "cap.pcap")
        self.assertEqual(1, status)
        self.assertIn("cap.pcap exists", printed)

    def test_a_dangling_symbolic_link_counts_as_existing(self):
        (self.directory / "cap.pcap").symlink_to("nowhere")
        status, printed = self.check("--absent", "cap.pcap")
        self.assertEqual(1, status)
        self.assertIn("cap.pcap exists", printed)

    def test_a_seeded_file_must_be_as_it_was_seeded(self):
        run(["seed", str(self.directory), "collide.pcap"])
        self.assertEqual(0, self.check("--kept", "collide.pcap")[0])
        (self.directory / "collide.pcap").write_bytes(b"truncated")
        status, printed = self.check("--kept", "collide.pcap")
        self.assertEqual(1, status)
        self.assertIn("collide.pcap changed", printed)

    def test_links_must_still_be_the_links_that_were_made(self):
        (self.directory / "input.bin").write_bytes(b"records")
        run(["link", str(self.directory), "--symlink", "link.bin=input.bin",
             "--hardlink", "hard.bin=input.bin"])
        digest = sha256(b"records").hexdigest()
        arguments = ["--sha256", f"input.bin={digest}",
                     "--symlink", "link.bin=input.bin",
                     "--hardlink", "hard.bin=input.bin"]
        self.assertEqual(0, self.check(*arguments)[0])
        os.remove(self.directory / "link.bin")
        (self.directory / "link.bin").write_bytes(b"not a link")
        status, printed = self.check(*arguments)
        self.assertEqual(1, status)
        self.assertIn("link.bin is no longer a symbolic link", printed)

    def test_a_hard_link_that_is_now_its_own_file_is_reported(self):
        (self.directory / "input.bin").write_bytes(b"records")
        run(["link", str(self.directory), "--hardlink", "hard.bin=input.bin"])
        os.remove(self.directory / "hard.bin")
        (self.directory / "hard.bin").write_bytes(b"records")
        status, printed = self.check("--hardlink", "hard.bin=input.bin")
        self.assertEqual(1, status)
        self.assertIn("no longer the same file", printed)

    def test_anything_else_in_the_directory_is_reported(self):
        (self.directory / "stray.tmp").write_bytes(b"")
        status, printed = self.check()
        self.assertEqual(1, status)
        self.assertIn("unexpected file(s) left behind: stray.tmp", printed)

    def test_a_named_extra_file_is_allowed(self):
        (self.directory / "stdout.pcap").write_bytes(b"anything")
        self.assertEqual(0, self.check("--also", "stdout.pcap")[0])

    def test_a_directory_that_is_not_there(self):
        status, printed = run(["check", str(self.directory / "missing")])
        self.assertEqual(1, status)
        self.assertIn("is not a directory", printed)


if __name__ == "__main__":
    unittest.main()
