"""User-facing information about each catalog test, read from a committed file.

`ubertooth_harness/test_info.yaml` holds one entry per catalog test, keyed by
the exact catalog name, with six fields: `purpose`, `theory`, `hardware`,
`expected`, `limitations`, and `related`. It is the source of the prose the
GUI shows beside a test name, so a reader does not have to go to the source
code to learn why a test exists
([#315](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/315)).

What this file is **not** is a second catalog. The catalog remains
authoritative for the arguments a test accepts, the hardware roles it binds,
its coverage profiles, and its wall-time estimates; `/api/plans` reads those
from the catalog and adds the prose from here, so nothing has two sources.
`docs/tests.md` remains the full human-readable document, one section per
test under the exact catalog name, and the prose here is the short form of
that section rather than a separate account of the test.

Nothing here can stop a run. A missing file, a malformed one, or a test with
no entry leaves that test's information unavailable, with the reason named,
and the run goes on: `test_information()` never raises. `load_test_info()` is
the strict reader the unit tests use, and it refuses what a reader would have
to guess at -- a name no catalog test has, a duplicate key, a missing or
empty field, an unknown field, and an issue reference that names no
repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from types import MappingProxyType
from typing import Mapping

import yaml

from ubertooth_harness.catalog import TestCatalog, builtin_catalog

# The committed file, beside the package it describes. Never a path from a
# client: the controller reads this one file and no other.
TEST_INFO_PATH = Path(__file__).resolve().with_name("test_info.yaml")
# Its path as a reader of a message would write it.
TEST_INFO_NAME = "ubertooth_harness/test_info.yaml"

# The full human-readable document, whose headings are the exact catalog
# names, and the public repository a link to it goes to.
DOCUMENTATION_PATH = "docs/tests.md"
HARNESS_REPOSITORY = "SqueakyFloors/ubertooth-test-harness"
_BLOB_URL = f"https://github.com/{HARNESS_REPOSITORY}/blob/main/{DOCUMENTATION_PATH}"

# The prose fields, in the order a reader reads them, and `related` beside
# them. Every entry carries all six: a schema a drifted entry falls out of
# is worth more than one that quietly loses a field.
PROSE_FIELDS: tuple[str, ...] = (
    "purpose", "theory", "hardware", "expected", "limitations",
)
FIELDS: tuple[str, ...] = PROSE_FIELDS + ("related",)
# Long enough for the longest section's summary, short enough that a
# response stays a response. A field over this is an entry to shorten.
MAX_FIELD_CHARS = 4000
MAX_RELATED = 12

# The repositories an entry may reference, each written out so a reference
# can never be read against the wrong one (the harness and the two forks it
# tests). `<repository>#<number>`, as a cross-repository reference is written
# everywhere else here.
REPOSITORIES: Mapping[str, str] = MappingProxyType({
    "ubertooth-test-harness": HARNESS_REPOSITORY,
    "ubertooth": "SqueakyFloors/ubertooth",
    "libbtbb": "SqueakyFloors/libbtbb",
})
_REFERENCE_RE = re.compile(r"^(?P<repository>[a-z0-9][a-z0-9-]*)#(?P<number>[1-9][0-9]{0,5})$")
_ANCHOR_DROP_RE = re.compile(r"[^a-z0-9 -]")
# One type for "no entries", whichever way there are none.
_EMPTY: Mapping[str, "TestInfo"] = MappingProxyType({})
# The last answer per path, keyed by what the file was when it was read
# (`_cache_key`). Read from a worker thread per request, so it takes a lock;
# two threads racing would only parse twice and store the same answer.
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


class TestInfoError(ValueError):
    """The test information file does not say what a reader would need."""


class _StrictLoader(yaml.SafeLoader):
    """A safe loader that refuses a duplicate mapping key.

    PyYAML keeps the last of two entries with the same key, so a second
    `Spectrum analyzer:` would silently replace the first and the file would
    look complete while one test's information had been lost.
    """


def _no_duplicate_keys(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        # Before `key in mapping`: a YAML complex key (`? [a, b]`) builds a
        # list, which is unhashable, and the lookup would raise TypeError
        # out of the loader rather than this module's own error.
        if not isinstance(key, str):
            raise TestInfoError(
                f"a key must be a test name, not {type(key).__name__}")
        if key in mapping:
            raise TestInfoError(f"duplicate entry for {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


@dataclass(frozen=True)
class Reference:
    """One issue or pull request an entry points at."""

    reference: str  # `ubertooth#677`, as the entry wrote it
    url: str

    def view(self) -> dict:
        return {"reference": self.reference, "url": self.url}


@dataclass(frozen=True)
class TestInfo:
    """One catalog test's user-facing information."""

    name: str
    purpose: str
    theory: str
    hardware: str
    expected: str
    limitations: str
    related: tuple[Reference, ...] = ()

    def view(self) -> dict:
        """The entry as `/api/plans` reports it.

        Plain strings and lists: the prose is data, never markup, and
        whoever renders it escapes it. Nothing here builds HTML.
        """
        return {
            "available": True,
            "unavailable_reason": "",
            **{field: getattr(self, field) for field in PROSE_FIELDS},
            "related": [item.view() for item in self.related],
            "documentation": documentation(self.name),
        }


def anchor(test_name: str) -> str:
    """The `docs/tests.md` heading anchor of a test, from its catalog name.

    The headings are the exact catalog names, so the anchor is the usual
    slug of one: lowercased, spaces to hyphens, anything else dropped.
    """
    slug = _ANCHOR_DROP_RE.sub("", test_name.strip().lower())
    return "-".join(slug.split())


def documentation(test_name: str) -> dict:
    """Where the full section for this test is, and a link to it."""
    slug = anchor(test_name)
    return {
        "path": DOCUMENTATION_PATH,
        "anchor": slug,
        "url": f"{_BLOB_URL}#{slug}",
    }


def unavailable(reason: str, test_name: str) -> dict:
    """An information block for a test that has none, and why.

    The shape is the one an entry gives, field for field and type for type,
    so a client reads the same response either way and only has to look at
    `available`. The link to the full documentation is still a link, because
    it is derived from the name rather than read from the file.
    """
    return {
        "available": False,
        "unavailable_reason": reason,
        **{field: "" for field in PROSE_FIELDS},
        "related": [],
        "documentation": documentation(test_name),
    }


def _reference(raw, where: str) -> Reference:
    if not isinstance(raw, str) or not raw.strip():
        raise TestInfoError(f"{where}: a related reference must be a non-empty string")
    text = raw.strip()
    match = _REFERENCE_RE.fullmatch(text)
    if not match:
        raise TestInfoError(
            f"{where}: related reference {text!r} must be written "
            "<repository>#<number>, for example ubertooth-test-harness#315"
        )
    repository = match["repository"]
    if repository not in REPOSITORIES:
        raise TestInfoError(
            f"{where}: related reference {text!r} names no known repository "
            "(" + ", ".join(sorted(REPOSITORIES)) + ")"
        )
    # `/issues/N` for a pull request too: GitHub sends it to `/pull/N`, so
    # one form covers both and an entry does not have to know which it is.
    return Reference(
        text, f"https://github.com/{REPOSITORIES[repository]}/issues/{match['number']}"
    )


def _related(raw, where: str) -> tuple[Reference, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise TestInfoError(f"{where}: related must be a list of references")
    if len(raw) > MAX_RELATED:
        raise TestInfoError(
            f"{where}: related lists {len(raw)} references, at most "
            f"{MAX_RELATED} are shown"
        )
    references = [_reference(item, where) for item in raw]
    seen = [item.reference for item in references]
    duplicates = sorted({item for item in seen if seen.count(item) > 1})
    if duplicates:
        raise TestInfoError(f"{where}: related repeats " + ", ".join(duplicates))
    return tuple(references)


def _prose(raw, where: str, field: str) -> str:
    if not isinstance(raw, str):
        raise TestInfoError(f"{where}: {field} must be a string")
    text = raw.strip()
    if not text:
        raise TestInfoError(
            f"{where}: {field} must not be empty; say \"not established\" "
            "where nothing is written down"
        )
    if len(text) > MAX_FIELD_CHARS:
        raise TestInfoError(
            f"{where}: {field} is {len(text)} characters, at most "
            f"{MAX_FIELD_CHARS} are shown; the full account belongs in "
            f"{DOCUMENTATION_PATH}"
        )
    return text


def _entry(name: str, raw, where: str) -> TestInfo:
    if not isinstance(raw, Mapping):
        raise TestInfoError(f"{where}: an entry must be a mapping of {', '.join(FIELDS)}")
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        raise TestInfoError(f"{where}: unknown field(s) " + ", ".join(unknown))
    missing = [field for field in FIELDS if field not in raw]
    if missing:
        raise TestInfoError(f"{where}: missing field(s) " + ", ".join(missing))
    return TestInfo(
        name=name,
        **{field: _prose(raw[field], where, field) for field in PROSE_FIELDS},
        related=_related(raw["related"], where),
    )


def load_test_info(
    path: Path | str | None = None, catalog: TestCatalog | None = None
) -> Mapping[str, TestInfo]:
    """Every entry of the file, keyed by catalog name. Strict.

    Raises `TestInfoError` for anything a reader would have to guess at: a
    file that is not a mapping, a key no catalog test has, a duplicate key, a
    missing or empty field, an unknown field, and a malformed reference. A
    test the file has no entry for is **not** an error here -- that is what
    `unavailable()` is for -- but the unit tests require an entry for every
    catalog test, so a new test without one fails there rather than quietly
    losing its information in the GUI.
    """
    info_path = Path(path) if path is not None else TEST_INFO_PATH
    test_catalog = catalog or builtin_catalog()
    try:
        with info_path.open("r", encoding="utf-8") as stream:
            raw = yaml.load(stream, Loader=_StrictLoader)
    except TestInfoError as error:
        raise TestInfoError(f"{info_path}: {error}") from error
    # UnicodeDecodeError for a byte an editor saved in another encoding, and
    # a bare ValueError or TypeError for what PyYAML cannot construct: all
    # of them are this file being unreadable, not a fault to propagate.
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError,
            TypeError) as error:
        raise TestInfoError(f"unable to load {info_path}: {error}") from error
    if not isinstance(raw, Mapping) or not raw:
        raise TestInfoError(f"{info_path}: the file must be a non-empty mapping")
    known = set(test_catalog.names)
    entries = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name:
            raise TestInfoError(f"{info_path}: a test name must be a non-empty string")
        if name not in known:
            raise TestInfoError(
                f"{info_path}: no catalog test is named {name!r}; the keys here "
                "are the exact catalog names"
            )
        entries[name] = _entry(name, value, f"{info_path}: {name}")
    return MappingProxyType(entries)


def test_information(
    path: Path | str | None = None, catalog: TestCatalog | None = None
) -> "TestInformation":
    """The file as a reader of it, whether or not it loaded.

    Never raises: a missing or malformed file gives a set that has no entry
    for any test and says why, so a run is never blocked and the GUI says
    the information is unavailable rather than failing the request.

    Held between calls while the file on disk is the same one: reading and
    validating 62 entries takes about 70 ms, and ``/api/plans`` is asked for
    on every page load and every change of target, so re-reading it per
    request would be the slowest thing in that response.

    What makes a held answer still this file's own is `_cache_key`: the
    file's modification time, size and inode, plus the catalog the entries
    were checked against, since a different catalog would accept or refuse
    different names. The key is taken again after the read and the answer is
    held only if it has not moved, so a file replaced while it was being
    read is not stored under the identity of the one before it. A file that
    cannot be asked about at all (it is gone, or its directory refuses) is
    never held, so the answer for it is worked out again every time.
    """
    info_path = Path(path) if path is not None else TEST_INFO_PATH
    resolved = catalog or builtin_catalog()
    before = _cache_key(info_path, resolved)
    if before is not None:
        with _CACHE_LOCK:
            cached = _CACHE.get(info_path)
        if cached is not None and cached[0] == before:
            return cached[1]
    information = _read(info_path, resolved)
    after = _cache_key(info_path, resolved)
    if after is not None and after == before:
        with _CACHE_LOCK:
            _CACHE.clear()  # one file in practice; never a growing map
            _CACHE[info_path] = (after, information)
    return information


def _cache_key(path: Path, catalog: TestCatalog):
    """The file's identity on disk, and the catalog it was read against.

    None when the file cannot be asked about, which is never held and never
    matches a held key, so a missing file is not answered from a copy taken
    when it was still missing: the moment it appears it is read.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino,
            tuple(catalog.names))


def _read(info_path: Path, catalog: TestCatalog) -> "TestInformation":
    try:
        return TestInformation(load_test_info(info_path, catalog), "")
    # Nothing about this file may reach the caller as an exception: the
    # controller serves the plans listing from here, so a file one byte of
    # which an editor saved in another encoding would take the whole Run
    # setup card down rather than one information block. `load_test_info`
    # is where a fault is named and where CI fails on it.
    except Exception as error:  # noqa: BLE001
        if _absent(info_path):
            return TestInformation(_EMPTY, f"{TEST_INFO_NAME} is missing")
        return TestInformation(
            _EMPTY,
            f"{TEST_INFO_NAME} could not be read: {_one_line(error, info_path)}",
        )


def _absent(path: Path) -> bool:
    """True when the file is not there. A path that cannot be asked about
    (a permission error on a parent directory) counts as present, so the
    reason names the error rather than claiming the file is missing."""
    try:
        return not path.exists()
    except OSError:
        return False


def _one_line(error: Exception, path: Path) -> str:
    """An error as one line, with the filesystem path left out of it.

    The reason reaches a browser, so it says the repository-relative name
    (`TEST_INFO_NAME`) rather than where this checkout happens to be. The
    path is taken out as written and as resolved, and so is its directory,
    because a YAML parse error names the file more than once and sometimes
    by its directory. A directory of one or two characters (`/`, or a
    relative `.`) is left alone: replacing it would rewrite every separator
    in the message.
    """
    text = " ".join(str(error).split()) or type(error).__name__
    directory = str(Path(path).parent)
    found = [str(path)]
    try:
        # A symbolic link loop makes this raise, and this is the handler for
        # a file that could not be read, so it may not raise in turn.
        found.append(str(Path(path).resolve()))
    except (OSError, RuntimeError):
        pass
    if len(directory) > 2:
        found.append(directory)
    for item in found:
        text = text.replace(item, TEST_INFO_NAME)
    return text


@dataclass(frozen=True)
class TestInformation:
    """The entries, and why there are none when there are none."""

    entries: Mapping[str, TestInfo]
    error: str

    def view(self, test_name: str) -> dict:
        """One test's information block for `/api/plans`."""
        entry = self.entries.get(test_name)
        if entry is not None:
            return entry.view()
        return unavailable(
            self.error or f"no {TEST_INFO_NAME} entry for this test", test_name
        )

    def missing(self, catalog: TestCatalog | None = None) -> tuple[str, ...]:
        """Catalog tests with no entry, in catalog order."""
        test_catalog = catalog or builtin_catalog()
        return tuple(
            name for name in test_catalog.names if name not in self.entries
        )
