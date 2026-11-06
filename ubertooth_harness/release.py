"""The version string a build of one commit reports (#352).

Every version line the Ubertooth fork prints names the release a build came
from rather than the commit it was built at::

    Host tools version: 2026-11-DEV1+22 (libbtbb 2020-12-R1+79)
    Firmware version: bluetooth_rxtx 2026-11-DEV1+22 (API:1.58)

The harness compares what a board and the installed tools report against what
a build of the commit under test *would* report, so it has to work that string
out the same way the build does, from the same checkout. Two files in the fork
do it, and they agree with each other on purpose:
``host/cmake/set_release.cmake`` for the host tools and the library, and
``firmware/version.mk`` for the firmware images and the bootloader. libbtbb's
own ``cmake/set_release.cmake`` does the same for ``btbb_get_release()``.

The rule, as those files state it:

* the nearest tag named ``YYYY-MM-Rn``, ``YYYY-MM-RCn`` or ``YYYY-MM-DEVn``
  (``git describe --tags --abbrev=0``, with a ``--match`` for each of the
  three, and the name checked again because a glob cannot say "digits and then
  nothing");
* then how many commits the build is past that tag
  (``git rev-list --count <tag>..<commit>``), appended as ``+N`` when that is
  not 0;
* with no release tag in reach -- a clone fetched without tags, a shallow one
  -- ``<latest release>+unknown``, because a count from the start of history
  is not the distance from a release and a shallow clone would answer it with
  its own depth. The latest release is read out of the checkout's own file, so
  moving it there moves it here.
* a ``*`` for a tree with uncommitted changes. Nothing here appends one: a
  harness run refuses a dirty source checkout (manifest.py) and builds from a
  clean clone of it, so a reported ``*`` is a mismatch to report rather than
  something to predict.

**Older refs still report a commit.** The harness tests older commits of the
fork as well, and before this change a build stamped ``git-<hash>``. So the
style is read off the checkout at that commit rather than assumed: the file
that decides the version string either derives a release name or formats a
hash, and it says which in its own text. A file that does neither is refused
rather than guessed at, because guessing would make every version comparison
in the run compare the wrong thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

# The three tag names a release goes out under, as git globs. The repository
# holds other tags, which name no release and must not be reported as one, so
# git is given a --match for each rather than one YYYY-MM-* glob.
RELEASE_TAG_GLOBS = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-R[0-9]*",
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-RC[0-9]*",
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-DEV[0-9]*",
)
# A glob cannot say "digits and then nothing", so the name git found is
# checked here too: "2026-11-R1-local" matches the first glob and is not a
# release. Both of the fork's own files do the same.
RELEASE_TAG_FORM = re.compile(r"\A[0-9]{4}-[0-9]{2}-(?:R|RC|DEV)[0-9]+\Z")
# What a tree with no release tag in reach reports after the release name,
# instead of a distance it cannot know.
UNKNOWN_DISTANCE = "unknown"
# The release a checkout reports when its own file does not say; every commit
# that derives a release name sets it, so this is only a floor.
DEFAULT_LATEST_RELEASE = "2020-12-R1"

# Which file decides the version string, per repository. The Ubertooth fork's
# firmware works the same string out in firmware/version.mk, and the two agree
# by design, so reading one of them answers for the whole tree.
VERSION_FILES = {
    "Ubertooth": "host/cmake/set_release.cmake",
    "libbtbb": "cmake/set_release.cmake",
}
# The second place the Ubertooth fork derives the string, for the firmware
# images and the bootloader. It arrived with the release names, so a tree that
# has it and no host file above has moved the host one rather than being old.
FIRMWARE_VERSION_FILE = {"Ubertooth": "firmware/version.mk"}

# How a file says which style it derives. The release-name files name the tag
# globs above; the commit-hash files, in both repositories, build the string as
# `set(RELEASE_STRING "git-${GIT_VERSION}...")`. Neither marker appears in the
# other's file, and a file carrying neither is refused.
_RELEASE_STYLE_MARKER = RELEASE_TAG_GLOBS[0]
_COMMIT_STYLE_MARKER = '"git-${GIT_VERSION}'
_LATEST_RELEASE_RE = re.compile(r'^set\(LATEST_RELEASE\s+"([^"]+)"\)', re.M)

# The two styles a build reports in.
RELEASE_STYLE = "release"
COMMIT_STYLE = "commit"

# Each git question here is one short read of local history.
GIT_TIMEOUT_S = 20.0


class ReleaseVersionError(RuntimeError):
    """The version a build of this commit reports cannot be worked out."""


@dataclass(frozen=True)
class BuildVersion:
    """What a build of one commit reports, and how that was arrived at.

    ``value`` is the string itself. ``style`` is RELEASE_STYLE for a commit
    whose build names a release and COMMIT_STYLE for one whose build stamps
    ``git-<hash>``; a caller matching the layout of a version *line* needs it,
    because the line was rewritten in the same change.
    """

    value: str
    style: str

    @property
    def names_release(self) -> bool:
        return self.style == RELEASE_STYLE


def _git(path: Path, *arguments: str) -> str | None:
    """The output of one git command, or None when it would not answer."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseVersionError(f"git command failed in {path}: {error}") from error
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def version_file_text(source_path: Path | str, commit: str, kind: str) -> str | None:
    """The file that decides ``kind``'s version string, as of ``commit``.

    None where that commit has no such file, which every commit from before
    the file existed is; those builds stamp a commit hash. Read out of git
    rather than off disk: the checkout may sit on another commit entirely, and
    the run is about the commit it resolved.
    """
    name = VERSION_FILES.get(kind)
    if name is None:
        return None
    return _git(Path(source_path), "show", f"{commit}:{name}")


def version_style(text: str, kind: str, commit: str) -> str:
    """RELEASE_STYLE or COMMIT_STYLE, as ``text`` derives it."""
    if _RELEASE_STYLE_MARKER in text:
        return RELEASE_STYLE
    if _COMMIT_STYLE_MARKER in text:
        return COMMIT_STYLE
    raise ReleaseVersionError(
        f"{kind} {commit[:12]} derives its version string in "
        f"{VERSION_FILES[kind]} in a way this does not recognize: it names "
        f"neither a release tag glob ({_RELEASE_STYLE_MARKER}) nor "
        f"{_COMMIT_STYLE_MARKER}..., so what a build of it reports is not "
        "known and nothing in the run could be compared against it"
    )


def latest_release(text: str) -> str:
    """The release the file names as its floor (LATEST_RELEASE)."""
    found = _LATEST_RELEASE_RE.search(text)
    return found.group(1) if found else DEFAULT_LATEST_RELEASE


def release_name(source_path: Path | str, commit: str, floor: str) -> str:
    """The release name a build at ``commit`` reports, as the build derives it."""
    path = Path(source_path)
    matches: list[str] = []
    for glob in RELEASE_TAG_GLOBS:
        matches += ["--match", glob]
    tag = _git(path, "describe", "--tags", "--abbrev=0", *matches, commit)
    if not tag or not RELEASE_TAG_FORM.match(tag):
        # No release tag in reach, or a tag a glob let through that is not a
        # release name after all. Either way the distance is not known.
        return f"{floor}+{UNKNOWN_DISTANCE}"
    ahead = _git(path, "rev-list", "--count", f"{tag}..{commit}")
    # Nothing to count is the same answer as none: the release name alone.
    if not ahead or not ahead.isdigit():
        ahead = "0"
    return tag if ahead == "0" else f"{tag}+{ahead}"


def build_version(
    source_path: Path | str, commit: str, *, kind: str = "Ubertooth",
    short_commit: str | None = None,
) -> BuildVersion:
    """The version a build of ``commit`` in ``source_path`` would report.

    ``short_commit`` is the abbreviation to use for a commit-style build; git's
    own abbreviation for that checkout when it is not given, which is what the
    build would stamp.

    A commit with no version file at all, and a repository the harness does
    not build, are read as commit style: that is what every such build stamps,
    and it is what the harness required before releases were named. A file
    that is there and derives the string in a third way is refused instead, as
    is a tree whose firmware names releases while its host file has gone --
    the host file moved, and reading the absence as "old" would compare every
    version in the run against the wrong string.
    """
    text = version_file_text(source_path, commit, kind)
    if text is None:
        firmware = FIRMWARE_VERSION_FILE.get(kind)
        if firmware is not None and _git(
            Path(source_path), "show", f"{commit}:{firmware}"
        ) is not None:
            raise ReleaseVersionError(
                f"{kind} {commit[:12]} has {firmware}, so its build names a "
                f"release, but it has no {VERSION_FILES[kind]} to read that "
                "from: the file this reads has moved and release.py has to "
                "follow it"
            )
        style = COMMIT_STYLE
    else:
        style = version_style(text, kind, commit)
    if style == RELEASE_STYLE:
        return BuildVersion(
            release_name(source_path, commit, latest_release(text)), style
        )
    digits = short_commit
    if not digits:
        digits = _git(Path(source_path), "rev-parse", "--short", commit) or ""
    if not digits:
        raise ReleaseVersionError(
            f"{kind} {commit[:12]} stamps a commit hash, which this checkout "
            "would not abbreviate"
        )
    return BuildVersion(f"git-{digits.lower()}", style)
