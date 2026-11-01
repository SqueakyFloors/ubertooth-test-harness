"""Reading one page of a run's saved output file, for the GUI.

The GUI lists the files a run saved and shows one on request. Serving them is
deliberately narrow, because the controller has no authentication of its own
(it binds loopback only and is reached through SSH tunnels, DESIGN.md
section 1), so every route has to be safe for whoever reaches that socket:

* the only paths that can be read are the ones the run's own ``run.jsonl``
  records, so a path the caller invents is refused whatever it says;
* the path is then resolved and must still be inside the run directory, with
  no symbolic link on the way out, so a link a test left behind cannot be
  followed out either;
* a page is bounded, so one request cannot read a soak's whole output into
  the controller.

Nothing here writes, creates, or deletes anything.
"""

from pathlib import Path

from ubertooth_harness import outputfiles
from ubertooth_harness.events import RUN_LOG
from ubertooth_harness.outputfiles import OutsideRun, output_records, saved_output

# Bytes of a file returned in one page, and the most a caller may ask for.
# A page is read and decoded in full, so this bounds what one request costs.
PAGE_BYTES = 64 * 1024
MAX_PAGE_BYTES = 1024 * 1024


class OutputFileError(ValueError):
    """The file cannot be served from this run directory."""

    def __init__(self, detail: str, status: int = 404, *, on_target: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.status = status
        # The run recorded this file, but it is not in this run directory.
        # On the controller that means the run's own runner has it, so the
        # controller asks that runner for the page instead of refusing.
        self.on_target = on_target


def saved_in(run_dir: Path) -> tuple:
    """The files a run recorded, read from its run.jsonl a line at a time.

    Each one says whether this host can read it: through the runner service
    the ``output/`` files stay in the runner's run directory and only
    run.jsonl reaches the controller, so for a run the controller did not
    carry out the list is complete but the files are elsewhere.
    """
    log = run_dir / RUN_LOG
    if not log.is_file():
        return ()
    saved = saved_output(output_records(log))
    return tuple(
        (item, _readable(run_dir, item.file.path)) for item in saved
    )


def _readable(run_dir: Path, path: str) -> bool:
    try:
        return confined(run_dir, path).is_file()
    except OutputFileError:
        return False


def listing(run_dir: Path) -> list[dict]:
    """The run's files for the GUI, each marked readable on this host."""
    return [
        {**item.to_dict(), "readable": readable}
        for item, readable in saved_in(run_dir)
    ]


def _recorded(run_dir: Path, path: str):
    for item, _readable_here in saved_in(run_dir):
        if item.file.path == path:
            return item.file
    return None


def confined(directory: Path, path: str) -> Path:
    """``path`` as a readable file inside ``directory``, or raise.

    ``outputfiles.confined`` refuses an absolute path, a path that climbs out
    with ``..``, and a path that reaches outside through a symbolic link; this
    adds that the result must be a regular file, and turns a refusal into the
    status the route answers with.
    """
    try:
        full = outputfiles.confined(directory, path)
    except OutsideRun as error:
        raise OutputFileError(str(error), 400) from error
    except (ValueError, OSError) as error:
        # A path the filesystem itself refuses: an embedded NUL, or a name
        # longer than the filesystem allows. The reason names the path only,
        # never where the run directory is on this host.
        raise OutputFileError(f"{path!r} is not a usable path", 400) from error
    try:
        if not full.is_file():
            raise OutputFileError(
                f"{path!r} is not a file in this run directory", 404)
    except OSError as error:
        raise OutputFileError(f"{path!r} cannot be read", 404) from error
    return full


def read_output_page(
    directory: Path,
    path: str,
    offset: int = 0,
    limit: int = PAGE_BYTES,
) -> dict:
    """One page of a saved output file, with where it sits in the file.

    ``offset`` and ``limit`` are in bytes, so a page of a multi-gigabyte soak
    costs the same as a page of a short answer. A page may start or end inside
    a multi-byte character; those bytes are replaced rather than refused, so a
    reader paging through a file never hits an error at a boundary.
    """
    recorded = _recorded(directory, path)
    if recorded is None:
        raise OutputFileError(f"run has no saved output file {path!r}")
    if offset < 0 or limit < 1:
        raise OutputFileError("offset must be 0 or more and limit 1 or more", 400)
    try:
        full = confined(directory, path)
    except OutputFileError as error:
        if error.status == 404:
            # Recorded, but not on this host: through the runner service the
            # files stay in the runner's run directory.
            raise OutputFileError(
                f"{path!r} was saved on the target, not here; it is in the "
                "target's own run directory", 404, on_target=True,
            ) from error
        raise
    length = min(limit, MAX_PAGE_BYTES)
    try:
        size = full.stat().st_size
        with full.open("rb") as handle:
            handle.seek(min(offset, size))
            body = handle.read(length)
    except OSError as error:
        # Removed or made unreadable since it was recorded, which a run still
        # going can do; answered as a refusal, not as a server failure.
        raise OutputFileError(f"{path!r} cannot be read now", 404) from error
    return {
        # What the run recorded (path, stream, bytes, lines, sha256) and what
        # is there now, so a reader can see when the two differ.
        **recorded.to_dict(),
        # What is on disk now, which is what was read; the record above says
        # what the run wrote, so a reader can see they differ.
        "file_bytes": size,
        "offset": min(offset, size),
        "length": len(body),
        "eof": min(offset, size) + len(body) >= size,
        "page_bytes": length,
        "text": body.decode("utf-8", errors="replace"),
    }
