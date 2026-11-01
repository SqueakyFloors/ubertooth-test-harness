"""killedcapture.py against real processes: the kill, the reap, what is left.

The point of the module is that the signal reaches the tool itself and that
nothing of it is left afterwards, so these tests run real child processes
rather than a model of them.
"""

import io
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import killedcapture

# A tool that writes to its capture file until it is killed.
STREAMER = """#!/usr/bin/env python3
import sys, time
path = sys.argv[sys.argv.index("-d") + 1]
with open(path, "ab", buffering=0) as handle:
    while True:
        handle.write(b"x" * 1024)
        time.sleep(0.01)
"""

# A tool that ends by itself, which leaves the board idle and tests nothing.
QUITTER = """#!/usr/bin/env python3
import sys
sys.exit(0)
"""

# A tool that opens nothing and writes nothing.
SILENT = """#!/usr/bin/env python3
import time
time.sleep(600)
"""

# A tool that starts a process of its own, which killing the tool does not
# take with it. It stays in the tool's process group, as a child does.
SPAWNER = """#!/usr/bin/env python3
import subprocess, sys, time
path = sys.argv[sys.argv.index("-d") + 1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(path + ".helper", "w").write(str(child.pid))
with open(path, "ab", buffering=0) as handle:
    while True:
        handle.write(b"x" * 1024)
        time.sleep(0.01)
"""

SERIAL = "0" * 31 + "f"


def run(argv):
    """(exit status, the one line it printed)."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = killedcapture.main(argv)
    return code, out.getvalue().strip()


class KilledCaptureTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.capture = self.root / "capture.bin"
        self.record = self.root / "capture.json"

    def tool(self, source, name="ubertooth-dump"):
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def kill(self, source, *, min_bytes=1024, max_wait=10.0, name="ubertooth-dump"):
        return run([
            "kill", "--capture", str(self.capture), "--record", str(self.record),
            "--min-bytes", str(min_bytes), "--max-wait", str(max_wait), "--",
            self.tool(source, name), "-U", SERIAL, "-d", str(self.capture),
        ])

    def test_the_tool_itself_is_killed_reaped_and_gone(self):
        code, line = self.kill(STREAMER)
        self.assertEqual(0, code, line)
        self.assertIn("SIGKILL reaped as signal 9, pid gone, nothing left in "
                      "its process group", line)
        self.assertIn("ubertooth-dump pid ", line)
        self.assertRegex(line, r"streamed \d+ bytes in \d+\.\d s")

    def test_the_record_is_what_the_killed_tool_left(self):
        code, line = self.kill(STREAMER)
        self.assertEqual(0, code, line)
        saved = json.loads(self.record.read_text(encoding="utf-8"))
        size, checksum = killedcapture.digest(self.capture)
        self.assertEqual((size, checksum), (saved["size"], saved["sha256"]))
        self.assertEqual(signal.SIGKILL, saved["signal"])
        self.assertEqual("capture.bin", saved["capture"])
        self.assertIn(SERIAL, saved["argv"])
        self.assertGreaterEqual(size, 1024)

    def test_a_tool_that_exits_by_itself_is_a_problem(self):
        code, line = self.kill(QUITTER)
        self.assertEqual(1, code)
        self.assertIn("exited 0 by itself", line)
        self.assertIn("nothing was killed", line)
        self.assertFalse(self.record.exists())

    def test_a_tool_that_produces_nothing_is_a_problem_and_is_killed_anyway(self):
        code, line = self.kill(SILENT, max_wait=0.5)
        self.assertEqual(1, code)
        self.assertIn("wrote 0 bytes in 0.5 s", line)
        self.assertIn("less than the 1024", line)

    def test_a_process_the_tool_left_behind_is_reported_and_killed(self):
        code, line = self.kill(SPAWNER)
        helper = int((self.root / "capture.bin.helper").read_text(encoding="utf-8"))
        self.addCleanup(self.reap, helper)
        self.assertEqual(1, code)
        self.assertIn("was not empty", line)
        self.assertIn("may still own the device", line)
        self.assertFalse(self.record.exists())
        # Reported is not enough: it must not hold the device through the rest
        # of the case.
        self.assertTrue(self.gone(helper), "the process left behind is still running")

    def gone(self, pid):
        for _ in range(200):
            if killedcapture.pid_gone(pid):
                return True
            time.sleep(0.05)
        return False

    def test_a_tool_that_produces_nothing_leaves_nothing_running(self):
        # The failure path kills the group too: the SPAWNER writes its helper's
        # pid before it writes any capture data, so this run gives up on the
        # data and still has a process to clean up.
        code, line = self.kill(SPAWNER, min_bytes=1 << 30, max_wait=1.0)
        helper = int((self.root / "capture.bin.helper").read_text(encoding="utf-8"))
        self.addCleanup(self.reap, helper)
        self.assertEqual(1, code)
        self.assertIn("less than the", line)
        self.assertTrue(self.gone(helper), "the process left behind is still running")

    def reap(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        for _ in range(200):
            if killedcapture.pid_gone(pid):
                return
            time.sleep(0.05)

    def test_verify_accepts_the_file_the_kill_left_and_refuses_a_changed_one(self):
        code, line = self.kill(STREAMER)
        self.assertEqual(0, code, line)
        code, line = run(["verify", "--capture", str(self.capture),
                          "--record", str(self.record)])
        self.assertEqual(0, code, line)
        self.assertRegex(line, r"^Killed capture evidence: capture\.bin unchanged, "
                               r"\d+ bytes, SHA-256 [0-9a-f]{64}$")
        with self.capture.open("ab") as handle:
            handle.write(b"later")
        code, line = run(["verify", "--capture", str(self.capture),
                          "--record", str(self.record)])
        self.assertEqual(1, code)
        self.assertIn("not the", line)

    def test_a_missing_capture_is_a_problem_not_a_traceback(self):
        code, line = run(["verify", "--capture", str(self.root / "gone.bin"),
                          "--record", str(self.record)])
        self.assertEqual(1, code)
        self.assertIn("Killed capture problem:", line)

    def test_an_empty_command_is_refused(self):
        code, line = run(["kill", "--capture", str(self.capture),
                          "--record", str(self.record), "--"])
        self.assertEqual(1, code)
        self.assertIn("no command to run after --", line)


class ProcessCheckTests(unittest.TestCase):

    def test_a_process_whose_status_nobody_has_read_is_not_gone(self):
        # Why the module checks the pid as well as the exit status: a process
        # that has exited still owns its open device until it is reaped.
        pid = os.posix_spawn("/bin/sh", ["sh", "-c", "exit 0"], {})
        self.assertFalse(killedcapture.pid_gone(pid))
        os.waitpid(pid, 0)
        self.assertTrue(killedcapture.pid_gone(pid))

    def test_this_process_and_its_group_are_there(self):
        self.assertFalse(killedcapture.pid_gone(os.getpid()))
        self.assertFalse(killedcapture.group_gone(os.getpgid(0)))

    def test_a_group_nothing_is_left_in_is_gone(self):
        process = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
        pgid = os.getpgid(process.pid)
        process.wait(timeout=30)
        self.assertTrue(killedcapture.group_gone(pgid))


if __name__ == "__main__":
    unittest.main()
