"""Case builder for DFU image checks: ubertooth-dfu with no Ubertooth.

Since fork e8edd15 (#217) ``ubertooth-dfu -d`` checks the whole image before
any device is opened (host/doc/ubertooth-dfu.md: the suffix and its length,
the DFU version, the UFD signature, a non-empty payload, the CRC, and the
vendor and product, which must be 1d50:6003 or the 0xffff wildcard; with -r
also the revision marker), and a file that fails is reported with exit 1
"without opening a device". ``-s`` never needs one. So both run here as
``env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu ...`` with no board at all:
a refusal must come before the board lookup, so the switch's "no board is
selected" note and the DFU stage log are forbidden in it, and the case
directory (the image included) must be exactly as it was. An accepted image
must reach that lookup instead, so the note is required there.

The first step of every case reads the built libubertooth (refusal.py probe,
running no tool) and the case skips when it predates the switch (fork
b6d8ede). That commit also made ubertooth-dfu's own DFU-device search honor
the switch, and it already contains the image check (e8edd15) and the -s
file fixes (9d710fe, fork #200), so a library that knows the switch comes
with a tool that touches no board here.

Images. The run's firmware build (``--build --with-firmware`` or
``--artifact "test build=PATH"``) is not available to the host-only plan,
which CI runs without an ARM toolchain. So every case works from a
deterministic synthetic image (dfuimage.py synth: filler bytes with a
``ubertooth harness-synthetic (`` revision marker, and the suffix computed by
the harness itself); the bad images derive from it as DFU images derives
them from the test build. ``sign-test-build`` checks ``-s`` against the
build's own ``.dfu``, byte for byte, when the run has one, and skips with
that reason when it does not.

Comprehensive coverage adds edge images (empty, 15 bytes, a suffix with no
payload, a suffix length larger than the file, DFU version 2.0, no revision
marker, a directory, a missing file), the accepted DFU 1.1 and wildcard
suffixes, -s inputs it cannot use, and how -s puts its output in place. Since
fork f2e06fa (#305) -s replaces ``<file>.dfu`` only once the whole signed
image is ready, through a temporary file beside it (ubertooth-dfu.md): an
input it cannot read leaves an existing ``.dfu`` alone, a ``.dfu`` symlink is
replaced rather than written through, the replaced file's permissions are
kept, a read-only directory refuses the signing, no temporary file is left,
and ``-s`` with an accepted ``-d`` signs before the board lookup that fails.
"""

from __future__ import annotations

from pathlib import Path
import sys

from ubertooth_harness import dfuimage
from ubertooth_harness.refusal_cases import NO_BOARD_NOTE, probe_step, tool_step
from ubertooth_harness.stateful import (
    CAPTURE_DIR_KEY,
    Observation,
    StateCase,
    Step,
    artifact_key,
    require,
)

TOOL = "ubertooth-dfu"
TEST_IMAGE = "test_build"
_HELPER = str(Path(dfuimage.__file__).resolve())
_HELPER_TIMEOUT_S = 15.0
SYNTHETIC_BIN = "synthetic.bin"
SYNTHETIC_DFU = "synthetic.dfu"
_OWN_SUFFIX = "the harness's own suffix of it"

# A refusal ends before the board lookup: no switch note, no DFU stage log
# ("DFU +0.000s: operation started"), no expected revision announced.
REFUSED_FORBIDDEN = (NO_BOARD_NOTE, r"DFU \+\d", r"DFU expected revision")
_CHECK_FAILED = r"Signature check failed, firmware will not be updated"
_CHECKING = r"Checking firmware signature"


def _path(name):
    return lambda context: str(Path(require(context, CAPTURE_DIR_KEY)) / name)


def _run(step_id, name, *argv, **extra) -> Step:
    """A plain command; ("file", name) words are paths in the case directory."""
    return Step(step_id, name, lambda context: tuple(
        _path(word[1])(context) if isinstance(word, tuple) else word for word in argv),
        timeout_s=5.0, **extra)


def _regular(name, mode=None) -> Step:
    label = f"{name} is a regular file" + (f" with mode {mode}" if mode else "")
    return _helper("regular", label, "regular", _path(name),
                   *(("--mode", mode) if mode else ()), pattern=r"Regular file: ")


def _helper(step_id, name, *parts, pattern, observe=None):
    """A dfuimage.py command; callable parts are resolved from the context."""
    def argv(context):
        return (sys.executable, _HELPER) + tuple(
            part(context) if callable(part) else part for part in parts)
    return Step(step_id, name, argv, timeout_s=_HELPER_TIMEOUT_S,
                output_patterns=(pattern,), observe=observe)


def _synth() -> Step:
    return _helper("synth", f"Write the synthetic image ({SYNTHETIC_BIN}, {SYNTHETIC_DFU})",
                   "synth", _path(SYNTHETIC_BIN), _path(SYNTHETIC_DFU),
                   pattern=r"Synthetic image: \d+ bytes")


def _make(kind, output) -> Step:
    label = "Bad" if kind in dfuimage.BAD_KINDS else "Edge"
    return _helper("make", f"Write the {kind} image as {output}", "make", kind,
                   _path(SYNTHETIC_DFU), _path(output),
                   pattern=rf"{label} image \({kind}\): \d+ bytes")


_ENTRIES = r"Directory entries: (?P<value>\{[^\n]*\})"


def _state_before(sub="") -> Step:
    where = f"<dir>/{sub}" if sub else "the case directory"
    return _helper("state-before", f"Record {where} (names, sizes, hashes)",
                   "state", _path(sub), pattern=_ENTRIES,
                   observe=Observation("directory before", _ENTRIES))


def _changes(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("Directory changed: "):
            return line
    return ""


def _state_after(sub="", allow=()) -> Step:
    """The directory as _state_before recorded it, apart from the ``allow``
    entries (a replaced output): no temporary file, nothing else touched."""
    where = f"<dir>/{sub}" if sub else "The case directory"
    name = (f"{where} is unchanged apart from {', '.join(allow)}: no other file added"
            if allow else f"{where} is unchanged: image intact, no file added")
    return Step(
        "state-after", name,
        lambda context: (sys.executable, _HELPER, "state", _path(sub)(context),
                         "--before", require(context, "directory before"),
                         *(word for entry in allow for word in ("--allow", entry))),
        timeout_s=_HELPER_TIMEOUT_S, expected_returncodes=(0, 1),
        output_patterns=(r"Directory (unchanged|changed): ",), output_check=_changes)


def _list_files() -> tuple[Step, ...]:
    # Nothing on a device to restore; record what the case left.
    return (_helper("list-files", "List the files in the case directory", "state",
                    lambda context: require(context, CAPTURE_DIR_KEY), pattern=_ENTRIES),)


def _case(case_id, name, steps, expected_state, **extra) -> StateCase:
    return StateCase(case_id, name, (probe_step(TOOL), *steps), _list_files(),
                     expected_state=expected_state, capture_dir=True, **extra)


def _download(file_name, patterns, *, refused=True, step_id="attempt") -> Step:
    """``-d <file> -r``: refused before the board lookup, or (accepted) up to
    the lookup, which finds no board with the switch set."""
    if refused:
        return tool_step(step_id, TOOL, ("-d", ("file", file_name), "-r"),
                         patterns=patterns, forbidden=REFUSED_FORBIDDEN)
    return tool_step(step_id, TOOL, ("-d", ("file", file_name), "-r"),
                     patterns=(_CHECKING, *patterns, NO_BOARD_NOTE),
                     forbidden=(_CHECK_FAILED, r"firmware programming attempt"))


def _refusal_case(case_id, name, prepare, file_name, patterns, expected):
    return _case(case_id, name, (
        _synth(), *prepare, _state_before(), _download(file_name, patterns), _state_after(),
    ), expected)


# (kind, what the image check says beyond "Signature check failed")
_BAD = (
    ("corrupt", r"CRC mismatch: calculated: 0x[0-9a-f]+, found: 0x[0-9a-f]+"),
    ("truncated", None),
    ("no-suffix", None),
    ("text", None),
    ("bad-signature", r"DFU Signature mismatch: not a DFU file"),
    ("wrong-vendor", r"DFU suffix is for USB vendor 0x1234, not 0x1d50"),
    ("wrong-product", r"DFU suffix is for USB product 0x0001, not 0x6003"),
)
_BAD_NAMES = {
    "corrupt": "a wrong CRC", "truncated": "a truncated image",
    "no-suffix": "a binary without a suffix", "text": "a text file",
    "bad-signature": "a suffix without UFD", "wrong-vendor": "another vendor ID",
    "wrong-product": "another product ID",
}
_REFUSED = ("exit 1 with the image check's message, before any board lookup; "
            "the image and the directory unchanged")


def _bad_image_cases():
    cases = []
    for kind, message in _BAD:
        bad = f"{kind}.dfu"
        patterns = (_CHECKING, *((message,) if message else ()), _CHECK_FAILED)
        cases.append(_refusal_case(
            f"reject-{kind}", f"-d refuses {_BAD_NAMES[kind]} with no Ubertooth",
            (_make(kind, bad),), bad, patterns, _REFUSED))
    return cases


def _accept_case(case_id, name, prepare, file_name):
    return _case(case_id, name, (
        _synth(), *prepare, _state_before(),
        _download(file_name, (rf"DFU expected revision: {dfuimage.SYNTHETIC_REVISION}",),
                  refused=False),
        _state_after(),
    ), "the image check passes and the command goes on to the board lookup, which "
       "finds none (exit 1); the directory unchanged")


def _sign(file_name, *, returncodes=(0,), patterns=()) -> Step:
    return tool_step("sign", TOOL, ("-s", ("file", file_name)), returncodes=returncodes,
                     patterns=patterns, forbidden=REFUSED_FORBIDDEN)


def _check_suffix(binary, output, reference, reference_name) -> Step:
    return _helper(
        "check", f"{output} is {binary} plus the Ubertooth suffix, identical to "
        f"{reference_name}", "check-suffix", _path(binary), _path(output), reference,
        "--reference-name", reference_name,
        pattern=r"DFU suffix: VID:PID 1d50:6003, bcdDFU 0100, CRC ok, identical to ")


def _sign_cases():
    cases = [
        _case("sign-synthetic", "-s suffixes a binary as the harness computes it", (
            _synth(),
            _copy_bin(),
            _sign("image.bin"),
            _check_suffix("image.bin", "image.dfu", _path(SYNTHETIC_DFU), _OWN_SUFFIX),
        ), "exit 0; image.dfu is image.bin plus the suffix and CRC the harness computes"),
        _case("sign-test-build", "-s of the test build's binary gives the build's .dfu", (
            Step("test-build", "Does this run have a firmware build",
                 lambda context: (sys.executable, _HELPER, "test-build",
                                  context.get(artifact_key(TEST_IMAGE), "")),
                 timeout_s=_HELPER_TIMEOUT_S,
                 observe=Observation(
                     "test build", r"Test build: (?P<value>[^\n]+)",
                     reject=lambda value: value if value.startswith("none") else "")),
            _helper("strip", "The test build without its suffix as build.bin", "strip",
                    lambda context: require(context, artifact_key(TEST_IMAGE)),
                    _path("build.bin"), pattern=r"Stripped suffix: \d+ bytes"),
            _sign("build.bin"),
            _check_suffix("build.bin", "build.dfu",
                          lambda context: require(context, artifact_key(TEST_IMAGE)),
                          "the build's .dfu"),
        ), "exit 0; build.dfu identical to the build's .dfu, byte for byte; skipped "
           "when the run has no firmware build", optional_artifacts=(TEST_IMAGE,)),
        _case("sign-refuses-dfu", "-s refuses a .dfu", (
            _synth(), _state_before(),
            _sign(SYNTHETIC_DFU, returncodes=(1,), patterns=(r"Error: -s requires a \.bin",)),
            _state_after(),
        ), "exit 1; nothing written, the .dfu unchanged"),
        _case("sign-refuses-suffixed-bin", "-s refuses a .bin that already has a suffix", (
            _synth(),
            _helper("copy", f"Copy {SYNTHETIC_DFU} to signed.bin", "copy",
                    _path(SYNTHETIC_DFU), _path("signed.bin"), pattern=r"Copied"),
            _state_before(),
            _sign("signed.bin", returncodes=(1,), patterns=(
                r"signed\.bin already has a DFU suffix, so \S*signed\.dfu was not written",)),
            _state_after(),
        ), "exit 1; no signed.dfu, signed.bin unchanged"),
        _case("sign-read-only-input", "-s suffixes a read-only binary", (
            _synth(),
            _helper("strip", "Read-only binary readonly.bin", "strip", _path(SYNTHETIC_DFU),
                    _path("readonly.bin"), "--read-only", pattern=r"Stripped suffix"),
            _sign("readonly.bin"),
            _check_suffix("readonly.bin", "readonly.dfu", _path(SYNTHETIC_DFU), _OWN_SUFFIX),
        ), "exit 0; readonly.dfu is readonly.bin plus the suffix"),
    ]
    return cases


def _copy_bin() -> Step:
    return _helper("copy", f"Copy {SYNTHETIC_BIN} to image.bin", "copy",
                   _path(SYNTHETIC_BIN), _path("image.bin"), pattern=r"Copied")


def _mkdir(name) -> Step:
    return Step("mkdir", f"mkdir <dir>/{name}",
                lambda context: ("mkdir", _path(name)(context)), timeout_s=5.0)


# (kind, message) of the edge images -d must refuse.
_EDGE_REFUSED = (
    ("empty", (_CHECKING, r"Too short for a 16-byte DFU suffix: the file is 0 byte\(s\)",
               _CHECK_FAILED)),
    ("short", (_CHECKING, r"Too short for a 16-byte DFU suffix: the file is 15 byte\(s\)",
               _CHECK_FAILED)),
    ("suffix-only", (_CHECKING, r"DFU suffix with no firmware: nothing to write",
                     _CHECK_FAILED)),
    ("long-suffix-length", (_CHECKING, r"Unknown DFU suffix length: 200", _CHECK_FAILED)),
    ("dfu-2.0", (_CHECKING, r"Unknown DFU version: 0200", _CHECK_FAILED)),
    # The suffix is fine; -r needs the revision the image names.
    ("no-marker", (_CHECKING,
                   r"Unable to determine expected firmware revision from image")),
)
_EDGE_NAMES = {
    "empty": "an empty file", "short": "a 15-byte file",
    "suffix-only": "a suffix with no firmware",
    "long-suffix-length": "a suffix length larger than the file",
    "dfu-2.0": "DFU version 2.0", "no-marker": "an image with no revision marker (-r)",
}


def _comprehensive_cases():
    cases = []
    for kind, patterns in _EDGE_REFUSED:
        image = f"{kind}.dfu"
        cases.append(_refusal_case(
            f"reject-{kind}", f"-d refuses {_EDGE_NAMES[kind]}", (_make(kind, image),),
            image, patterns, _REFUSED))
    cases.append(_refusal_case(
        "reject-directory", "-d refuses a directory", (_mkdir("folder.dfu"),),
        "folder.dfu", (r"error: \S*folder\.dfu is not a regular file",), _REFUSED))
    cases.append(_refusal_case(
        "reject-missing", "-d refuses a missing file", (), "missing.dfu",
        (r"missing\.dfu: No such file or directory",),
        "exit 1 naming the file, before any board lookup; nothing created"))
    cases.append(_accept_case("accept-dfu-1.1", "-d accepts a DFU 1.1 suffix",
                              (_make("dfu-1.1", "dfu-1.1.dfu"),), "dfu-1.1.dfu"))
    cases.append(_accept_case("accept-wildcard", "-d accepts the ffff:ffff wildcard",
                              (_make("wildcard", "wildcard.dfu"),), "wildcard.dfu"))
    cases.append(_case("sign-missing-input", "-s of a missing .bin writes nothing", (
        _synth(), _state_before(),
        _sign("missing.bin", returncodes=(1,), patterns=(r"missing\.bin: No such file",)),
        _state_after(),
    ), "exit 1 naming the file; no missing.dfu"))
    cases.append(_case("sign-refuses-corrupt-suffix",
                       "-s refuses a .bin whose suffix has a wrong CRC", (
        _synth(), _make("corrupt", "corrupt.bin"), _state_before(),
        _sign("corrupt.bin", returncodes=(1,), patterns=(
            r"corrupt\.bin already has a DFU suffix, so \S*corrupt\.dfu was not written",)),
        _state_after(),
    ), "exit 1; no corrupt.dfu, corrupt.bin unchanged"))
    # Fork #305, fixed in f2e06fa: the existing .dfu is no longer truncated
    # when the input cannot be read.
    cases += _sign_output_cases()
    cases.append(_case("sign-unreadable-input",
                       "-s of an input it cannot read leaves an existing .dfu alone", (
        _synth(), _mkdir("folder.bin"),
        _helper("copy", f"Copy {SYNTHETIC_DFU} to folder.dfu", "copy",
                _path(SYNTHETIC_DFU), _path("folder.dfu"), pattern=r"Copied"),
        _state_before(),
        _sign("folder.bin", returncodes=(1,)),
        _state_after(),
    ), "exit 1; folder.dfu unchanged"))
    return cases


def _sign_output_cases():
    """How -s puts <file>.dfu in place (fork f2e06fa, ubertooth-dfu.md)."""
    def seed_bin():
        return _helper("copy", f"Copy {SYNTHETIC_BIN} to image.bin", "copy",
                       _path(SYNTHETIC_BIN), _path("image.bin"), pattern=r"Copied")

    def stale(output):
        # An earlier output: the binary itself, so it is not what -s writes.
        return _helper("stale", f"An earlier {output} (the unsigned binary)", "copy",
                       _path(SYNTHETIC_BIN), _path(output), pattern=r"Copied")

    check = _check_suffix("image.bin", "image.dfu", _path(SYNTHETIC_DFU), _OWN_SUFFIX)
    read_only = (
        _synth(), _mkdir("ro"),
        _helper("copy", f"Copy {SYNTHETIC_BIN} to ro/image.bin", "copy",
                _path(SYNTHETIC_BIN), _path("ro/image.bin"), pattern=r"Copied"),
        _helper("copy-dfu", "An earlier ro/image.dfu, writable", "copy",
                _path(SYNTHETIC_DFU), _path("ro/image.dfu"), pattern=r"Copied"),
        _helper("root", "Does this run as root", "not-root", pattern=r"Root: ",
                observe=Observation("root", r"Root: (?P<value>[^\n]+)",
                                    reject=lambda v: v if v.startswith("yes") else "")),
        _run("chmod-ro", "chmod 555 <dir>/ro", "chmod", "555", ("file", "ro")),
        _state_before("ro"),
        tool_step("sign", TOOL, ("-s", ("file", "ro/image.bin")), returncodes=(1,),
                  patterns=(r"ro/image\.dfu: Permission denied",),
                  forbidden=REFUSED_FORBIDDEN),
        _state_after("ro"),
    )
    return [
        _case("sign-no-temporary-file", "-s leaves only <file>.dfu behind", (
            _synth(), seed_bin(), _state_before(),
            _sign("image.bin"),
            _state_after(allow=("image.dfu",)),
            check,
        ), "exit 0; image.dfu added, nothing else in the directory changed"),
        _case("sign-replaces-existing", "-s replaces an earlier .dfu and keeps its mode", (
            _synth(), seed_bin(), stale("image.dfu"),
            _run("chmod", "chmod 600 <dir>/image.dfu", "chmod", "600", ("file", "image.dfu")),
            _state_before(),
            _sign("image.bin"),
            _state_after(allow=("image.dfu",)),
            check, _regular("image.dfu", "600"),
        ), "exit 0; image.dfu replaced by the signed image, mode 600 kept, no "
           "temporary file"),
        _case("sign-replaces-symlink", "-s replaces a .dfu symlink, not what it points at", (
            _synth(), seed_bin(), stale("target.dfu"),
            _run("symlink", "ln -s target.dfu <dir>/image.dfu", "ln", "-s", "target.dfu",
                 ("file", "image.dfu")),
            _sign("image.bin"),
            _helper("target", "target.dfu is untouched", "same", _path("target.dfu"),
                    _path(SYNTHETIC_BIN), pattern=r"Unchanged"),
            _regular("image.dfu"), check,
        ), "exit 0; image.dfu is now a regular file holding the signed image; "
           "target.dfu unchanged"),
        StateCase(
            "sign-read-only-directory", "-s into a read-only directory writes nothing",
            (probe_step(TOOL), *read_only),
            (_run("cleanup-chmod", "chmod 755 <dir>/ro", "chmod", "755", ("file", "ro"),
                  expected_returncodes=(0, 1)), *_list_files()),
            expected_state="exit 1 naming ro/image.dfu; ro/image.dfu and the directory "
                           "unchanged (skipped as root)",
            capture_dir=True),
        _case("sign-then-board-lookup", "-s with an accepted -d signs, then the board "
              "lookup fails", (
            _synth(), seed_bin(),
            tool_step("sign", TOOL, ("-s", ("file", "image.bin"), "-d",
                                     ("file", SYNTHETIC_DFU), "-r"),
                      returncodes=(1,), patterns=(_CHECKING, NO_BOARD_NOTE),
                      forbidden=(_CHECK_FAILED, r"firmware programming attempt")),
            check,
        ), "exit 1 at the board lookup, with image.dfu already written"),
    ]


def dfu_host_cases(arguments, _resources, _resource_identities):
    """Host-only: every ubertooth-dfu command runs with UBERTOOTH_TEST_NO_DEVICES=1."""
    cases = _bad_image_cases()
    cases.append(_accept_case("accept-synthetic",
                              "-d accepts a good image and goes on to the board lookup",
                              (), SYNTHETIC_DFU))
    cases += _sign_cases()
    if arguments["coverage"] == "comprehensive":
        cases += _comprehensive_cases()
    return tuple(cases)
