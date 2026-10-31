"""Command execution boundary for harness operations."""

import codecs
import collections
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

# Characters of each stream kept in memory for CommandResult: the first half
# and the last half, with a line naming what is between them. A mode that
# streams for hours would otherwise grow without bound. The full stream is
# written to the run's output files as it arrives (rawlog.py), and checks
# read it there (outputfiles.py), so this only has to carry the live view and
# the short answers the harness reads directly.
MEMORY_WINDOW_CHARS = 256 * 1024
WINDOW_NOTE = (
    "\n[... {left_out} characters left out of this copy; "
    "the whole stream is in the run's output files ...]\n"
)


class _StreamWindow:
    """A bounded in-memory copy of one stream: its start and its end."""

    def __init__(self, limit: int = MEMORY_WINDOW_CHARS):
        self._half = max(limit, 2) // 2
        self._head: list[str] = []
        self._head_chars = 0
        self._tail: collections.deque = collections.deque()
        self._tail_chars = 0
        self._left_out = 0

    def add(self, text: str) -> None:
        if not text:
            return
        if self._head_chars + len(text) <= self._half and not self._tail:
            self._head.append(text)
            self._head_chars += len(text)
            return
        self._tail.append(text)
        self._tail_chars += len(text)
        # Always keep the newest chunk: the executor appends its timeout and
        # cancellation markers last, and they must stay visible.
        while len(self._tail) > 1 and self._tail_chars > self._half:
            dropped = self._tail.popleft()
            self._tail_chars -= len(dropped)
            self._left_out += len(dropped)

    def text(self) -> str:
        note = WINDOW_NOTE.format(left_out=self._left_out) if self._left_out else ""
        return "".join(self._head) + note + "".join(self._tail)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool
    cancelled: bool = False
    # The files this command's output was saved to, read back on demand
    # (outputfiles.CommandOutput). ``stdout`` and ``stderr`` above hold at
    # most MEMORY_WINDOW_CHARS of each stream, so a check reads the stream
    # through this instead.
    output: object | None = None


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
        output: Mapping[str, "_StreamWindow"],
        on_output: OutputCallback | None,
    ) -> str:
        output[stream_name].add(text)
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
                {"stdout": _StreamWindow(), "stderr": _StreamWindow()},
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

        output = {"stdout": _StreamWindow(), "stderr": _StreamWindow()}
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
            output["stdout"].text(),
            output["stderr"].text(),
            time.monotonic() - started,
            timed_out,
            cancelled,
        )
