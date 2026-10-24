"""Command execution boundary for harness operations."""

import codecs
from dataclasses import dataclass
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Callable, Mapping, Protocol, Sequence

from ubertooth_harness.cancellation import CancellationToken


OutputCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    cancelled: bool = False


class CommandExecutor(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cancellation: CancellationToken | None = None,
        on_output: OutputCallback | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        """Run one command without a shell."""


class SubprocessCommandExecutor:
    """Execute argv directly with live output, timeout, and cancellation."""

    _CHUNK_BYTES = 4096
    _POLL_SECONDS = 0.05
    _TERMINATE_GRACE_SECONDS = 2.0

    def __init__(self, environment: Mapping[str, str] | None = None):
        """``environment`` replaces the inherited process environment."""
        self.environment = dict(environment) if environment is not None else None

    @classmethod
    def _read_stream(cls, pipe, stream_name: str, output_queue: queue.Queue) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                data = os.read(pipe.fileno(), cls._CHUNK_BYTES)
                if not data:
                    break
                text = decoder.decode(data)
                if text:
                    output_queue.put((stream_name, text))
            final_text = decoder.decode(b"", final=True)
            if final_text:
                output_queue.put((stream_name, final_text))
        finally:
            pipe.close()
            output_queue.put((stream_name, None))

    @staticmethod
    def _signal_process(process: subprocess.Popen, sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _deliver(
        stream_name: str,
        text: str,
        output: dict[str, list[str]],
        on_output: OutputCallback | None,
    ) -> str:
        output[stream_name].append(text)
        if on_output is None:
            return ""
        try:
            on_output(stream_name, text)
            return ""
        except Exception as error:
            return str(error)

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        cancellation: CancellationToken | None = None,
        on_output: OutputCallback | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        command = tuple(str(arg) for arg in argv)
        started = time.monotonic()
        if cancellation is not None and cancellation.is_cancelled():
            return CommandResult(
                command, 130, "", "canceled before start\n", 0.0, False, True
            )
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                shell=False,
                start_new_session=True,
                bufsize=0,
                env=self.environment,
                cwd=cwd,
            )
        except OSError as error:
            message = f"unable to start command: {error}\n"
            self._deliver(
                "stderr",
                message,
                {"stdout": [], "stderr": []},
                on_output,
            )
            return CommandResult(
                command,
                127,
                "",
                message,
                time.monotonic() - started,
                False,
            )
        output_queue: queue.Queue = queue.Queue()
        readers = (
            threading.Thread(
                target=self._read_stream,
                args=(process.stdout, "stdout", output_queue),
            ),
            threading.Thread(
                target=self._read_stream,
                args=(process.stderr, "stderr", output_queue),
            ),
        )
        for reader in readers:
            reader.start()

        output: dict[str, list[str]] = {"stdout": [], "stderr": []}
        completed_streams = 0
        deadline = started + timeout_s
        terminate_deadline = None
        timed_out = False
        cancelled = False
        callback_error = ""

        while process.poll() is None or completed_streams < len(readers):
            now = time.monotonic()
            if terminate_deadline is None:
                if cancellation is not None and cancellation.is_cancelled():
                    cancelled = True
                    terminate_deadline = now + self._TERMINATE_GRACE_SECONDS
                    self._signal_process(process, signal.SIGTERM)
                elif now >= deadline:
                    timed_out = True
                    terminate_deadline = now + self._TERMINATE_GRACE_SECONDS
                    self._signal_process(process, signal.SIGTERM)
            elif process.poll() is None and now >= terminate_deadline:
                self._signal_process(process, signal.SIGKILL)

            try:
                stream_name, text = output_queue.get(timeout=self._POLL_SECONDS)
            except queue.Empty:
                continue
            if text is None:
                completed_streams += 1
                continue
            error = self._deliver(stream_name, text, output, on_output)
            if error and not callback_error:
                callback_error = error

        process.wait()
        for reader in readers:
            reader.join(timeout=1)

        marker = ""
        returncode = process.returncode if process.returncode is not None else 130
        if cancelled:
            marker = f"\nCANCELED: {cancellation.reason}\n"
        elif timed_out:
            marker = f"\nHARD TIMEOUT after {timeout_s:.3f}s\n"
            returncode = 124
        if callback_error:
            marker += f"\nOUTPUT SINK ERROR: {callback_error}\n"
        if marker:
            error = self._deliver("stderr", marker, output, on_output)
            if error and not callback_error:
                callback_error = error

        return CommandResult(
            command,
            returncode,
            "".join(output["stdout"]),
            "".join(output["stderr"]),
            time.monotonic() - started,
            timed_out,
            cancelled,
        )
