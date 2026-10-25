import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ubertooth_harness.cancellation import CancellationToken
from ubertooth_harness.executor import SubprocessCommandExecutor


class SubprocessExecutorTests(unittest.TestCase):

    def test_runs_argv_without_a_shell(self):
        result = SubprocessCommandExecutor().run(
            [sys.executable, "-c", "print('ok')"], timeout_s=2
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("ok\n", result.stdout)
        self.assertFalse(result.timed_out)

    def test_missing_executable_returns_streamed_127_result(self):
        chunks = []

        result = SubprocessCommandExecutor().run(
            ["/definitely/not/an/ubertooth-tool"],
            timeout_s=2,
            on_output=lambda stream, text: chunks.append((stream, text)),
        )

        self.assertEqual(127, result.returncode)
        self.assertIn("unable to start command", result.stderr)
        self.assertEqual(result.stderr, "".join(text for _stream, text in chunks))

    def test_timeout_returns_bounded_result(self):
        result = SubprocessCommandExecutor().run(
            [sys.executable, "-c", "import time; time.sleep(2)"], timeout_s=0.02
        )

        self.assertEqual(124, result.returncode)
        self.assertTrue(result.timed_out)
        self.assertIn("HARD TIMEOUT", result.stderr)
        self.assertLess(result.duration_s, 1)

    def test_cancellation_terminates_running_process(self):
        token = CancellationToken()
        chunks = []

        def cancel_after_output(stream, text):
            chunks.append((stream, text))
            if stream == "stdout" and "before-cancel" in text:
                token.cancel("operator requested")

        result = SubprocessCommandExecutor().run(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('before-cancel', flush=True); time.sleep(2)",
            ],
            timeout_s=5,
            cancellation=token,
            on_output=cancel_after_output,
        )

        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertLess(result.duration_s, 1)
        self.assertIn("before-cancel", result.stdout)
        self.assertIn("before-cancel", "".join(text for _stream, text in chunks))

    def test_streams_stdout_and_stderr_before_process_completion(self):
        chunks = []
        started = __import__("time").monotonic()

        result = SubprocessCommandExecutor().run(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys,time; "
                "sys.stdout.write('out-1'); sys.stdout.flush(); time.sleep(.12); "
                "sys.stderr.write('err-1'); sys.stderr.flush(); time.sleep(.12); "
                "sys.stdout.write('out-2'); sys.stdout.flush()",
            ],
            timeout_s=2,
            on_output=lambda stream, text: chunks.append(
                (__import__("time").monotonic() - started, stream, text)
            ),
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("out-1out-2", result.stdout)
        self.assertEqual("err-1", result.stderr)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertLess(chunks[0][0], result.duration_s)
        self.assertEqual("out-1out-2", "".join(
            text for _when, stream, text in chunks if stream == "stdout"
        ))
        self.assertEqual("err-1", "".join(
            text for _when, stream, text in chunks if stream == "stderr"
        ))

    def test_stream_preserves_unicode_split_across_writes(self):
        chunks = []
        code = (
            "import os,time; data='☃'.encode(); "
            "os.write(1,data[:1]); time.sleep(.03); os.write(1,data[1:])"
        )

        result = SubprocessCommandExecutor().run(
            [sys.executable, "-u", "-c", code],
            timeout_s=2,
            on_output=lambda stream, text: chunks.append((stream, text)),
        )

        self.assertEqual("☃", result.stdout)
        self.assertEqual("☃", "".join(text for stream, text in chunks if stream == "stdout"))

    def test_timeout_preserves_output_emitted_before_termination(self):
        chunks = []
        result = SubprocessCommandExecutor().run(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys,time; print('before'); sys.stdout.flush(); time.sleep(10)",
            ],
            # Room for the interpreter to start on a loaded host (0.08 s
            # missed 'before' now and then).
            timeout_s=1.0,
            on_output=lambda stream, text: chunks.append((stream, text)),
        )

        self.assertTrue(result.timed_out)
        self.assertIn("before", result.stdout)
        self.assertIn("before", "".join(text for _stream, text in chunks))


if __name__ == "__main__":
    unittest.main()
