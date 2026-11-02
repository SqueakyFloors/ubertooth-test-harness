"""Case builders for ubertooth-dfu beyond the write path Flash reliability
covers: host-side suffixing (-s), read-back (-u), and refusal of bad images.

Command contracts come from the fork sources (host/ubertooth-tools/src/
ubertooth-dfu.c, dfu.h, and firmware/bootloader); file layouts and the
checks are in dfuimage.py. The test writes the selected board's firmware, so
it establishes the firmware it verifies (it must be the last test on its
board) and every case ends with the test image in flash, read back and
verified, and the board in its application. Other registered boards are
guards: read before and after every case, never written.

Since fork 6fee30c (SqueakyFloors/ubertooth#198, #199, fixed by e8edd15;
#200, fixed by 9d710fe) -d checks the whole image before the board leaves its
application: a bad image, including a suffix for another vendor or product, is
refused before a device is opened, with exit 1 and one line naming the file,
what is wrong with it, and that the firmware was left alone. -s refuses a
.bin that already ends in a DFU suffix and writes nothing, and -s and -d only
read their input.

SqueakyFloors/ubertooth#267 (6fee30c, fixed in 89d9197): -d <image> -r
writes and resets correctly but exits 1 ("Detach failed") and skips "Verified
firmware revision", so write-read-back and every cleanup that rewrites the
test image fail on that commit. The cases are not changed for it.

SqueakyFloors/ubertooth#208: a failed -u read-back (seen once in about 20)
leaves the board in DFU despite -r. A case step that hits it fails and the
cleanup returns the board; a cleanup read-back that hits it returns the board
and fails the cleanup, so the run stops.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from ubertooth_harness import dfuimage
from ubertooth_harness.cases import guard_roles
from ubertooth_harness.outputfiles import search_lines, text_lines
from ubertooth_harness.patterns import IMAGE_NAME_PATTERN
from ubertooth_harness.stateful import (
    APPLICATION_IMAGE_LABEL,
    CAPTURE_DIR_KEY,
    EXPECTED_REVISION_KEY,
    Observation,
    StateCase,
    Step,
    artifact_key,
    require,
)
from ubertooth_harness.transitions import _serial, _util

TEST_IMAGE = APPLICATION_IMAGE_LABEL
_CHECKER = str(Path(dfuimage.__file__).resolve())
_REVISION_PATTERN = (
    rf"Firmware version:\s+{IMAGE_NAME_PATTERN}(?P<value>\S+)\s+\(API:"
)
# ubertooth-dfu logs this stage before it sends the first block.
_PROGRAMMING = r"firmware programming attempt"
# ...and this one once the image is accepted, before it opens a device.
_DEVICE_OPENED = r"DFU \+\d+\.\d+s: operation started"
# A write, a read-back. Helper steps that run device commands take their
# timeout from dfuimage.step_timeout_s (its own per-command timeouts and the
# guard count), so a slow cleanup is never killed mid-flash.
WRITE_TIMEOUT_S = 45.0
READ_TIMEOUT_S = 35.0
# A line of a command record the helper prints (rawlog format, UTC stamp).
_RECORD_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[")


def _refused(file_name: str, reason: str) -> str:
    """check_download_image(): a refused image gets one line naming the file,
    what is wrong with it, and that the board was left alone. ``reason`` is a
    pattern; the DFU image checks test covers every reason one by one."""
    return (rf"(?m)^error: \S*{re.escape(file_name)}: {reason}; "
            r"firmware will not be updated$")


def _image(context):
    return require(context, artifact_key(TEST_IMAGE))


def _revision(context):
    return require(context, EXPECTED_REVISION_KEY)


def _file(name):
    return lambda context: str(Path(require(context, CAPTURE_DIR_KEY)) / name)


def _helper(*parts):
    """A dfuimage.py command; callable parts are resolved from the context."""
    def argv(context):
        return (sys.executable, _CHECKER) + tuple(
            part(context) if callable(part) else part for part in parts
        )
    return argv


def _tool(*parts):
    def argv(context):
        return tuple(part(context) if callable(part) else part for part in parts)
    return argv


def _helper_step(step_id, name, *parts, pattern, timeout_s=10.0, mutates=False):
    """A dfuimage.py step. Its exit 1 is reported with the helper's own lines;
    ``pattern`` matches only the line it prints when the check holds."""
    def check(output):
        if search_lines(output, pattern):
            return ""
        # Command records stay in the transcript; the reason keeps the
        # helper's own lines.
        lines = [line.strip() for line in text_lines(output)
                 if line.strip() and not _RECORD_LINE.match(line)]
        if not lines:
            return "no output"
        # The helper prints its verdict last; the earlier lines are evidence.
        detail = f" ({'; '.join(lines[:-1])})" if len(lines) > 1 else ""
        return (lines[-1] + detail)[:600]

    return Step(step_id, name, _helper(*parts), timeout_s=timeout_s,
                expected_returncodes=(0, 1), output_check=check, mutates=mutates)


def _board_args(serial, guards, readback):
    args = ("--serial", serial)
    for guard in guards:
        args += ("--guard", guard)
    return args + ("--image", _image, "--revision", _revision, "--readback", _file(readback))


def _revision_step(serial, step_id, name, key):
    return Step(step_id, name, _util(serial, "-v"),
                observe=Observation(key, _REVISION_PATTERN, _revision))


def _restore(serial, guards):
    """Cleanup: the board in its application with the test image in flash."""
    return (
        _helper_step("restore", "Return to the application with the test image in flash",
                     "restore", *_board_args(serial, guards, "restore-readback.bin"),
                     pattern=r"Restored: application flash holds the test image",
                     timeout_s=dfuimage.step_timeout_s("restore", len(guards)), mutates=True),
        _revision_step(serial, "verify-restored-revision", "Verify restored firmware revision",
                       "restored revision"),
    )


def _case(serial, guards, case_id, name, steps, expected_state):
    return StateCase(case_id, name, tuple(steps), _restore(serial, guards),
                     expected_state=expected_state, capture_dir=True,
                     artifacts=(TEST_IMAGE,))


def _host_case(serial, case_id, name, steps, expected_state):
    """A -s case: no device command, so cleanup only reads the revision."""
    return StateCase(case_id, name, tuple(steps),
                     (_revision_step(serial, "verify-revision", "Board firmware unchanged",
                                     "revision after case"),),
                     expected_state=expected_state, capture_dir=True,
                     artifacts=(TEST_IMAGE,))


# ---------------------------------------------------------------------------
# -s: host only. sign() appends the suffix to <name>.bin as <name>.dfu.

def _suffix_case(serial):
    return _host_case(serial, "suffix", "-s suffixes a binary like the build's .dfu", (
        _helper_step("strip", "Binary from the test image", "strip", _image, _file("image.bin"),
                     pattern=r"Stripped suffix"),
        Step("suffix", "ubertooth-dfu -s image.bin", _tool("ubertooth-dfu", "-s", _file("image.bin"))),
        _helper_step("check", "image.dfu is the binary plus the Ubertooth suffix",
                     "check-suffix", _file("image.bin"), _file("image.dfu"), _image,
                     pattern=r"identical to the build's \.dfu"),
    ), "image.dfu identical to the build's .dfu; the board untouched")


def _suffix_extra_cases(serial):
    return [
        _host_case(serial, "suffix-refuses-dfu", "-s refuses a .dfu file", (
            _helper_step("copy", "Copy the test image", "copy", _image, _file("signed.dfu"),
                         pattern=r"Copied"),
            Step("suffix", "ubertooth-dfu -s signed.dfu",
                 _tool("ubertooth-dfu", "-s", _file("signed.dfu")),
                 expected_returncodes=(1,), output_patterns=(r"error: -s requires a \.bin",)),
            _helper_step("unchanged", "signed.dfu unchanged", "same", _file("signed.dfu"), _image,
                         pattern=r"Unchanged"),
        ), "refused with exit 1; the file unchanged"),
        # Refused since 9d710fe (SqueakyFloors/ubertooth#200).
        _host_case(serial, "suffix-already-suffixed",
              "-s refuses a .bin that already has a DFU suffix", (
            _helper_step("copy", "Suffixed image as signed.bin", "copy", _image,
                         _file("signed.bin"), pattern=r"Copied"),
            Step("suffix", "ubertooth-dfu -s signed.bin",
                 _tool("ubertooth-dfu", "-s", _file("signed.bin")), expected_returncodes=(1,),
                 output_patterns=(r"error: .*signed\.bin already has a DFU suffix, so "
                                  r".*signed\.dfu was not written",)),
            _helper_step("not-created", "No signed.dfu written", "absent", _file("signed.dfu"),
                         pattern=r"Not created"),
        ), "refused with exit 1; no second suffix written"),
        # -s only reads its input since 9d710fe (SqueakyFloors/ubertooth#200).
        _host_case(serial, "suffix-read-only-input", "-s suffixes a read-only binary", (
            _helper_step("strip", "Read-only binary from the test image", "strip", _image,
                         _file("readonly.bin"), "--read-only", pattern=r"Stripped suffix"),
            Step("suffix", "ubertooth-dfu -s readonly.bin",
                 _tool("ubertooth-dfu", "-s", _file("readonly.bin"))),
            _helper_step("check", "readonly.dfu is the binary plus the Ubertooth suffix",
                         "check-suffix", _file("readonly.bin"), _file("readonly.dfu"), _image,
                         pattern=r"identical to the build's \.dfu"),
        ), "readonly.dfu identical to the build's .dfu"),
    ]


# ---------------------------------------------------------------------------
# -d then -u: the written image reads back from flash 0x4000.

def _write_read_back_case(serial, guards):
    return _case(serial, guards, "write-read-back", "-u reads back the image -d wrote", (
        Step("write", "ubertooth-dfu -d <test image> -r",
             _tool("ubertooth-dfu", "-U", serial, "-d", _image, "-r"),
             timeout_s=WRITE_TIMEOUT_S, mutates=True,
             observe=Observation("written revision",
                                 r"Verified firmware revision: (?P<value>\S+)", _revision)),
        _revision_step(serial, "verify-written", "Verify written firmware revision",
                       "revision after write"),
        Step("read-back", "ubertooth-dfu -u readback.bin -r",
             _tool("ubertooth-dfu", "-U", serial, "-u", _file("readback.bin"), "-r"),
             timeout_s=READ_TIMEOUT_S, mutates=True, forbidden_patterns=(_PROGRAMMING,)),
        _helper_step("compare", "Read-back holds the written image", "check-readback",
                     _file("readback.bin"), _image, pattern=r"image and 0xff padding match"),
        _helper_step("returned", "Board back in its application after -u -r", "answers",
                     "--serial", serial, "--revision", _revision, pattern=r"Board answers with the expected revision",
                     timeout_s=dfuimage.step_timeout_s("answers")),
    ), "test image written, verified, read back identical; board in its application")


# ---------------------------------------------------------------------------
# -d <bad image> -r: refused before any block is written; the board keeps its
# firmware and stays in its application.

# Cutting the suffix out of firmware bytes leaves bLength whatever byte
# landed there, so the number it reports is not pinned.
_SUFFIX_LENGTH = r"Unknown DFU suffix length: \d+"
_REJECTIONS = (
    ("corrupt", "reject-corrupt", "Refuse an image with a wrong CRC",
     r"CRC mismatch: calculated: 0x[0-9a-f]+, found: 0x[0-9a-f]+"),
    ("truncated", "reject-truncated", "Refuse a truncated image", _SUFFIX_LENGTH),
    ("no-suffix", "reject-no-suffix", "Refuse a binary without a DFU suffix",
     _SUFFIX_LENGTH),
    ("text", "reject-text", "Refuse a text file", _SUFFIX_LENGTH),
    ("bad-signature", "reject-bad-signature", "Refuse a suffix without the UFD signature",
     r"DFU Signature mismatch: not a DFU file"),
    # The suffix must name 1d50:6003 since e8edd15 (SqueakyFloors/ubertooth#199).
    ("wrong-vendor", "reject-wrong-vendor", "Refuse a suffix for another vendor ID",
     r"DFU suffix is for USB vendor 0x1234, not 0x1d50: this image is for another device"),
    ("wrong-product", "reject-wrong-product", "Refuse a suffix for another product ID",
     r"DFU suffix is for USB product 0x0001, not 0x6003: this image is for another device"),
)


def _reject_case(serial, guards, kind, case_id, name, pattern):
    bad = f"{kind}.dfu"
    return _case(serial, guards, case_id, name, (
        _revision_step(serial, "read-original-revision", "Read firmware revision",
                       "original revision"),
        _helper_step("make", f"Write the {kind} image", "make", kind, _image, _file(bad),
                     pattern=r"Bad image"),
        Step("attempt", f"ubertooth-dfu -d {bad} -r",
             _tool("ubertooth-dfu", "-U", serial, "-d", _file(bad), "-r"),
             timeout_s=WRITE_TIMEOUT_S, mutates=True, expected_returncodes=(1,),
             output_patterns=(_refused(bad, pattern),),
             forbidden_patterns=(_DEVICE_OPENED, _PROGRAMMING, r"Verified firmware revision")),
        # Still in the application since e8edd15 (SqueakyFloors/ubertooth#198).
        _helper_step("after", "Board in its application, flash unchanged", "after-reject",
                     *_board_args(serial, guards, "after-readback.bin"),
                     pattern=r"After rejection: ok",
                     timeout_s=dfuimage.step_timeout_s("after-reject", len(guards)),
                     mutates=True),
    ), "refused with exit 1 before a device is opened; same firmware, still in the application")


def dfu_cases(arguments, _resources, resource_identities):
    serial = _serial(resource_identities, "DFU images")
    guards = tuple(
        resource_identities[role]["serial"].lower()
        for role in guard_roles(resource_identities)
    )
    # The write comes first: the board runs the test image from then on,
    # whatever it ran before (preflight does not require it).
    cases = [_write_read_back_case(serial, guards), _suffix_case(serial)]
    if arguments["coverage"] == "comprehensive":
        cases += _suffix_extra_cases(serial)
        cases += [_reject_case(serial, guards, *row) for row in _REJECTIONS]
    return tuple(cases)
