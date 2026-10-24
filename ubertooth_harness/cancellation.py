"""Cooperative run cancellation primitives."""

import threading


class CancellationToken:
    """Thread-safe cancellation state shared with a managed run worker."""

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancellation requested") -> None:
        normalized = reason.strip() or "cancellation requested"
        with self._lock:
            if self._event.is_set():
                return
            self._reason = normalized
            self._event.set()
