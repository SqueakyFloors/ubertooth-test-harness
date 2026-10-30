"""Write the fake commands a test puts on PATH, without paying for each one.

macOS checks an executable the first time it is run after being written: here
that costs about 0.3 s, and nothing on later runs of the same file. Tests write
the same fakes over and over into a temporary directory of their own, so the
suite spent most of its time on that check (issue #184).

``write_executable`` writes each distinct content once per test process and
hard links it into place after that, which the check does not apply to again.
The link is the same file, so a test must never write over one in place:
always go through this function (it replaces the path) or unlink it first.
"""

from __future__ import annotations

import atexit
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

_cache: dict[str, Path] = {}
_directory: Path | None = None


def _cache_dir() -> Path:
    global _directory
    if _directory is None:
        _directory = Path(tempfile.mkdtemp(prefix="harness-test-exe-"))
        atexit.register(shutil.rmtree, _directory, ignore_errors=True)
    return _directory


def _intact(source: Path, text: str, mode: int) -> bool:
    """True when the cached file still holds exactly what it was given."""
    try:
        stated = source.stat()
        return (stated.st_mode & 0o7777 == mode
                and source.read_text(encoding="utf-8") == text)
    except OSError:
        return False


def write_executable(path, text: str, mode: int = 0o755) -> Path:
    """Put an executable holding text at path (replacing what is there)."""
    path = Path(path)
    key = hashlib.sha256(f"{mode:o}\0{text}".encode()).hexdigest()
    source = _cache.get(key)
    if source is not None and not _intact(source, text, mode):
        # A test wrote to or chmodded one of the links, which is the same
        # file: start again rather than hand out what it left behind.
        source = None
    if source is None:
        source = _cache_dir() / key
        source.write_text(text, encoding="utf-8")
        source.chmod(mode)
        _cache[key] = source
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    try:
        os.link(source, path)
    except OSError:
        # Another filesystem (a test's own temporary directory elsewhere):
        # a copy costs the check, which is what this avoids, not correctness.
        shutil.copy2(source, path)
        path.chmod(mode)
    return path
