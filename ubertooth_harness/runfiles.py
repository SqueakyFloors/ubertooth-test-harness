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
from typing import Mapping, Sequence

from ubertooth_harness import outputfiles
from ubertooth_harness.outputfiles import OutsideRun, saved_output

# Bytes of a file returned in one page, and the most a caller may ask for.
# A page is read and decoded in full, so this bounds what one request costs.
PAGE_BYTES = 64 * 1024
MAX_PAGE_BYTES = 1024 * 1024


class OutputFileError(ValueError):
    """The file cannot be served from this run directory."""

    def __init__(self, detail: str, status: int = 404):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def _recorded(events: Sequence[Mapping], path: str):
    for saved in saved_output(events):
        if saved.file.path == path:
            return saved.file
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
    if not full.is_file():
        raise OutputFileError(f"{path!r} is not a file in this run directory", 404)
    return full


def read_output_page(
    directory: Path,
    events: Sequence[Mapping],
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
    recorded = _recorded(events, path)
    if recorded is None:
        raise OutputFileError(f"run has no saved output file {path!r}")
    if offset < 0 or limit < 1:
        raise OutputFileError("offset must be 0 or more and limit 1 or more", 400)
    full = confined(directory, path)
    size = full.stat().st_size
    length = min(limit, MAX_PAGE_BYTES)
    with full.open("rb") as handle:
        handle.seek(min(offset, size))
        body = handle.read(length)
    return {
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
