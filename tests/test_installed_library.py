"""The installed-library checks of `scripts/ci.sh build` (#333).

The steps themselves need cmake, a compiler, pkg-config and an installed
libubertooth, so they are the build job's; what is tested here is the
judgements they rest on (`installedlib.py`) and that the build step still
makes them.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import installedlib
from ubertooth_harness.installedlib import (
    InstalledLibraryError, age_headers, another_libbtbb, configure_identity,
    consumer_identity, same_path, under_prefix,
)

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci.sh"
PREFIX = "/tmp/build/prefix"
# The two wordings FindBTBB.cmake's btbb_report_pin() prints, as CMake
# prefixes them, with the pin written out in full in both.
AGREED = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from commit "
          "52b2afc, which is the commit this tree is built against "
          "(52b2afcb0c4b8b03412dac759aad88debdfb18e2)\n")
# The mismatch wording names the pin without parentheses, as FindBTBB's
# final message does; the agreeing one puts it in them.
DIFFERED = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from commit "
            "52b2afc; this tree is built against "
            "d356e9da7fb03969371aa14d603a8aad2f0ef198\n")
UNKNOWN = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, commit unknown "
           "(nothing in it reads as the commit it was built from); this tree "
           "is built against d356e9da7fb03969371aa14d603a8aad2f0ef198\n")
OTHER_LINES = "-- The C compiler identification is GNU 13.2.0\n-- Found USB1\n"


class ConfigureIdentityTests(unittest.TestCase):

    def identity(self, text, prefix=PREFIX):
        return configure_identity(text, prefix)

    def test_either_commit_naming_wording_passes(self):
        for text in (AGREED, DIFFERED):
            with self.subTest(text.split(",")[1][:20]):
                line = self.identity(OTHER_LINES + text)
                self.assertIn("built from commit 52b2afc", line)
                self.assertRegex(line, r"built against \(?[0-9a-f]{40}")

    def test_a_line_naming_no_found_commit_is_refused(self):
        # CI builds libbtbb from a clone at a pinned commit, so this wording
        # there means the stamp, or FindBTBB's reading of it, stopped working.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + UNKNOWN)
        self.assertIn("built from", str(caught.exception))

    def test_a_modified_tree_still_names_its_commit(self):
        # FindBTBB strips libbtbb's "*" and says so in words instead.
        text = AGREED.replace("built from commit 52b2afc,",
                              "built from commit 52b2afc with uncommitted "
                              "changes;")
        self.assertIn("uncommitted changes", self.identity(OTHER_LINES + text))

    def test_a_sibling_whose_name_begins_with_the_prefix_is_refused(self):
        text = AGREED.replace("/tmp/build/prefix/lib",
                              "/tmp/build/prefix-old/lib")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + text)
        self.assertIn("not in", str(caught.exception))

    def test_a_tag_the_tree_pins_is_compared_as_it_stands(self):
        # FindBTBB's third wording, for a pin that names no commit.
        text = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from "
                "commit 52b2afc; this tree is built against 2024-12-R1, which "
                "names no commit to compare it with\n")
        self.assertIn("2024-12-R1",
                      configure_identity(OTHER_LINES + text, PREFIX, "2024-12-R1"))
        with self.assertRaises(InstalledLibraryError):
            configure_identity(OTHER_LINES + text, PREFIX, "2024-12-R2")

    def test_without_a_tree_to_read_the_line_must_name_a_commit(self):
        text = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from "
                "commit 52b2afc; this tree is built against 2024-12-R1, which "
                "names no commit to compare it with\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            configure_identity(OTHER_LINES + text, PREFIX)
        self.assertIn("which is no commit", str(caught.exception))

    def test_the_pin_is_compared_with_the_tree_s_own(self):
        pin = "52b2afcb0c4b8b03412dac759aad88debdfb18e2"
        self.assertIn(pin, configure_identity(OTHER_LINES + AGREED, PREFIX, pin))
        self.assertIn(pin.upper(),
                      configure_identity(
                          OTHER_LINES + AGREED.replace(pin, pin.upper()),
                          PREFIX, pin))
        with self.assertRaises(InstalledLibraryError) as caught:
            configure_identity(OTHER_LINES + AGREED, PREFIX,
                               "d356e9da7fb03969371aa14d603a8aad2f0ef198")
        self.assertIn("where its own FindBTBB.cmake sets",
                      str(caught.exception))

    def test_an_overlong_commit_or_pin_is_not_one(self):
        for old, new in (("built from commit 52b2afc,",
                          "built from commit 52b2afcdeadbeefdeadbeefdeadbeef"
                          "deadbeefdeadbeef0,"),
                         ("52b2afcb0c4b8b03412dac759aad88debdfb18e2",
                          "52b2afcb0c4b8b03412dac759aad88debdfb18e2a")):
            with self.subTest(new[:24]):
                with self.assertRaises(InstalledLibraryError):
                    self.identity(OTHER_LINES + AGREED.replace(old, new))

    def test_a_pin_in_upper_case_is_a_pin(self):
        text = AGREED.replace("52b2afcb0c4b8b03412dac759aad88debdfb18e2",
                              "52B2AFCB0C4B8B03412DAC759AAD88DEBDFB18E2")
        self.assertIn("52B2AFCB", self.identity(OTHER_LINES + text))

    def test_a_configure_with_no_such_line(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES)
        self.assertIn("printed 0", str(caught.exception))

    def test_two_such_lines_are_refused(self):
        # One build, one libbtbb: two lines mean two searches and no answer
        # about which library the tools will link.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(AGREED + DIFFERED)
        self.assertIn("printed 2", str(caught.exception))

    def test_a_libbtbb_outside_the_prefix_is_refused(self):
        text = AGREED.replace("/tmp/build/prefix", "/usr/local")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + text)
        self.assertIn("not in", str(caught.exception))

    def test_a_line_without_the_found_commit_is_refused(self):
        text = AGREED.replace("built from commit 52b2afc, which is", "which is")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + text)
        self.assertIn("built from", str(caught.exception))

    def test_a_line_without_the_pin_is_refused(self):
        text = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from "
                "commit 52b2afc\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + text)
        self.assertIn("the commit the tree is built against",
                      str(caught.exception))

    def test_an_abbreviated_pin_is_not_a_pin(self):
        # The pin is written in full wherever it is named, so a short one is
        # a line this check cannot read a libbtbb back from.
        text = AGREED.replace("52b2afcb0c4b8b03412dac759aad88debdfb18e2",
                              "52b2afc")
        with self.assertRaises(InstalledLibraryError):
            self.identity(OTHER_LINES + text)


BOARD_LINE = "Firmware version: no Ubertooth attached"
# What a build of the current fork prints: the release the build came from,
# with libbtbb's beside it, and no line naming the libbtbb it loaded
# (fork #1230, harness #352).
VERSION_LINE = "Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)"
# What a commit from before that printed: the two library interface versions,
# each with the commit it was built from, and the libbtbb it loaded on a line
# of its own. It is here for the checks about such an install and nowhere
# else; everything below is about the fork as it stands.
OLDER_VERSION_LINE = ("Host tools version: libubertooth 1.1 (git-7cbc191a8), "
                      "libbtbb 1.3 (git-52b2afc)")


class ConsumerIdentityTests(unittest.TestCase):
    """What a program built against the install printed."""

    def setUp(self):
        self.prefix = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.prefix, ignore_errors=True)
        (self.prefix / "lib").mkdir()
        self.library = self.prefix / "lib" / "libbtbb.so.1.3"
        self.library.write_bytes(b"")

    def identity(self, text, built_libbtbb=None):
        return consumer_identity(text, str(self.prefix), VERSION_LINE,
                                 built_libbtbb)

    def test_another_release_is_another_library(self):
        other = VERSION_LINE.replace("2026-11-DEV2", "2026-11-DEV1+22")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(f"{other}\n{BOARD_LINE}\n", str(self.library.parent))
        self.assertIn("version line", str(caught.exception))

    def test_a_run_that_looked_for_boards_is_refused(self):
        # Without the switch the report opens every attached board, and two
        # runs on a bench host would not answer the same way.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(f"{VERSION_LINE}\n", str(self.library.parent))
        self.assertIn("UBERTOOTH_TEST_NO_DEVICES", str(caught.exception))

    def test_an_install_that_names_the_path_it_loaded_is_refused(self):
        """No commit this checks prints one (#358).

        An install named the libbtbb it loaded on a line of its own until
        fork #1230 replaced it with the warning below. Accepting one here
        would be a second way to pass, against output the release under test
        does not produce.
        """
        text = (f"{OLDER_VERSION_LINE}\n"
                f"libbtbb loaded from: {self.library}\n{BOARD_LINE}\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            consumer_identity(text, str(self.prefix), OLDER_VERSION_LINE,
                              str(self.library.parent))
        self.assertIn("not as a path line", str(caught.exception))

    NO_PATH = f"{VERSION_LINE}\n{BOARD_LINE}\n"

    def test_the_install_s_own_answer_passes_without_the_warning(self):
        """Since fork #1230 there is no path line.

        A libbtbb other than the one the tools were built against is a
        warning instead, so the loader having reached this install is
        established by that warning not being there -- together with the
        install having been built against a libbtbb inside the prefix, which
        is the other side of the comparison that warning comes from.
        """
        self.assertEqual(
            VERSION_LINE, self.identity(self.NO_PATH, str(self.library.parent))
        )

    def test_a_warning_about_another_libbtbb_is_refused(self):
        text = (f"{VERSION_LINE}\n"
                "warning: libbtbb was loaded from /usr/lib, not the "
                "/opt/run/lib these tools were built against; packets may "
                f"decode differently\n{BOARD_LINE}\n")

        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(text, str(self.library.parent))
        self.assertIn("other than the one the install was built against",
                      str(caught.exception))

    def test_no_path_line_and_no_recorded_libbtbb_establishes_nothing(self):
        """The warning is skipped when the install recorded no directory.

        ubertooth_libbtbb_is_elsewhere() returns 0 with nothing to compare
        against, so "no warning" would then say nothing at all -- which is
        the drift #294 exists to catch.
        """
        for built in (None, ""):
            with self.subTest(built=built):
                with self.assertRaises(InstalledLibraryError) as caught:
                    self.identity(self.NO_PATH, built)
                self.assertIn("--built-libbtbb", str(caught.exception))

    def test_a_recorded_libbtbb_outside_the_prefix_is_refused(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(self.NO_PATH, "/usr/lib")
        self.assertIn("establish nothing", str(caught.exception))


# The warning as warn_about_another_libbtbb() writes it, with the two
# directories it names.
WARNING = ("warning: libbtbb was loaded from {loaded}, not the {built} these "
           "tools were built against; packets may decode differently")


class AnotherLibbtbbTests(unittest.TestCase):
    """The live control the absence above rests on (#365).

    The step runs the install's own consumer again with the loader pointed at
    a copy of the install's libbtbb in a directory of its own. The warning
    has to be there and to name both directories: a warning that had stopped
    working, and a loader that will not say which file it opened, both leave
    the step above passing with nothing behind it.
    """

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.built = self.work / "prefix" / "lib"
        self.built.mkdir(parents=True)
        self.decoy = self.work / "another-libbtbb"
        self.decoy.mkdir()

    def output(self, *, loaded=None, built=None, version=VERSION_LINE,
               board=True, warning=True):
        lines = [version]
        if warning:
            lines.append(WARNING.format(loaded=loaded or self.decoy,
                                        built=built or self.built))
        if board:
            lines.append(BOARD_LINE)
        return "\n".join(lines) + "\n"

    def control(self, text, loaded_from=None, built_libbtbb=None):
        return another_libbtbb(text, str(loaded_from or self.decoy),
                               str(built_libbtbb or self.built), VERSION_LINE)

    def test_a_warning_naming_both_directories_passes(self):
        accepted = self.control(self.output())
        self.assertIn(str(self.decoy), accepted)
        self.assertIn(str(self.built), accepted)

    def test_links_and_dots_in_either_directory_are_reduced(self):
        # The fork resolves both paths, and on macOS a temporary directory is
        # reached through a link, so comparing the text would fail for a
        # reason that is not the install's.
        text = self.output(loaded=f"{self.decoy}/../another-libbtbb",
                           built=f"{self.built}/.")
        self.assertIn("libbtbb was loaded from", self.control(text))

    def test_no_warning_at_all_is_the_failure_this_exists_to_catch(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(warning=False))
        self.assertIn("0 warnings about another libbtbb",
                      str(caught.exception))

    def test_a_second_warning_is_refused(self):
        text = self.output() + WARNING.format(loaded=self.decoy,
                                              built=self.built) + "\n"
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(text)
        self.assertIn("2 warnings about another libbtbb",
                      str(caught.exception))

    def test_a_warning_naming_another_loaded_directory_is_refused(self):
        # The loader reaching a third libbtbb would say nothing about the
        # copy this step put in front of it.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(loaded="/usr/lib"))
        self.assertIn("where the loader was pointed at", str(caught.exception))

    def test_a_warning_naming_another_built_directory_is_refused(self):
        # The other side of the comparison: a warning measured against a
        # libbtbb this install was not built with is not this install's.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(built="/usr/lib"))
        self.assertIn("where the install was built against",
                      str(caught.exception))

    def test_a_copy_in_the_install_s_own_directory_is_refused(self):
        # Refused before the output is read: there is no second install to
        # warn about, so whatever the program printed says nothing.
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control("", loaded_from=self.built)
        self.assertIn("a directory of its own", str(caught.exception))

    def test_a_link_to_the_install_s_own_directory_is_refused(self):
        # same_directory() in libbtbb_origin.c resolves both paths, so a link
        # is one install to the fork and would print nothing.
        link = self.work / "linked-libbtbb"
        link.symlink_to(self.built)
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(), loaded_from=link)
        self.assertIn("a directory of its own", str(caught.exception))

    def test_a_wording_this_cannot_read_is_refused(self):
        # A warning that stopped naming both directories leaves a reader of a
        # failed job unable to see what happened.
        text = (f"{VERSION_LINE}\n"
                f"warning: libbtbb was loaded from {self.decoy}\n"
                f"{BOARD_LINE}\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(text)
        self.assertIn("names both directories", str(caught.exception))

    def test_a_line_that_merely_holds_the_warning_s_words_is_refused(self):
        # The warning is read as the whole of a line, so neither a line that
        # carries it inside something else nor one that goes on after it is
        # the warning: both would leave a reader of a failed job guessing
        # which directories were compared.
        warning = WARNING.format(loaded=self.decoy, built=self.built)
        for line in (f"note: {warning}",
                     f"{warning}, or so it says"):
            with self.subTest(line[:30]):
                text = f"{VERSION_LINE}\n{line}\n{BOARD_LINE}\n"
                with self.assertRaises(InstalledLibraryError) as caught:
                    self.control(text)
                self.assertIn("names both directories", str(caught.exception))

    def test_an_install_that_names_the_path_it_loaded_is_refused(self):
        # No install this checks prints one, and such an install would be
        # the positive evidence itself; reaching here means the two ends
        # disagree.
        text = (f"{OLDER_VERSION_LINE}\n"
                f"libbtbb loaded from: {self.decoy}/libbtbb.so.1\n"
                f"{BOARD_LINE}\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            another_libbtbb(text, str(self.decoy), str(self.built),
                            OLDER_VERSION_LINE)
        self.assertIn("nothing here for this control", str(caught.exception))

    def test_another_version_line_is_another_library(self):
        other = VERSION_LINE.replace("2026-11-DEV2", "2026-11-DEV1+22")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(version=other))
        self.assertIn("version line", str(caught.exception))

    def test_a_run_that_looked_for_boards_is_refused(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            self.control(self.output(board=False))
        self.assertIn("UBERTOOTH_TEST_NO_DEVICES", str(caught.exception))


class PathTests(unittest.TestCase):

    def setUp(self):
        self.prefix = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.prefix, ignore_errors=True)

    def test_same_path_reduces_links_and_dots(self):
        inside = self.prefix / "lib"
        inside.mkdir()
        self.assertEqual(inside.resolve(),
                         same_path(f"{self.prefix}/lib/pkgconfig/../../lib",
                                   str(inside)))

    def test_same_path_refuses_two_places(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            same_path(str(self.prefix), "/usr/local", "the prefix")
        self.assertIn("the prefix is", str(caught.exception))

    def test_under_prefix_takes_the_marked_path(self):
        library = self.prefix / "lib" / "libubertooth.so"
        library.parent.mkdir()
        library.write_bytes(b"")
        log = f"-- Found USB1\n-- Found LIBUBERTOOTH: {library}\n"
        self.assertEqual(library.resolve(),
                         under_prefix(log, str(self.prefix),
                                      "-- Found LIBUBERTOOTH: "))

    def test_under_prefix_refuses_the_prefix_itself(self):
        log = f"-- Found LIBUBERTOOTH: {self.prefix}\n"
        with self.assertRaises(InstalledLibraryError):
            under_prefix(log, str(self.prefix), "-- Found LIBUBERTOOTH: ")

    def test_under_prefix_refuses_a_path_outside(self):
        log = "-- Found LIBUBERTOOTH: /usr/lib/libubertooth.so\n"
        with self.assertRaises(InstalledLibraryError) as caught:
            under_prefix(log, str(self.prefix), "-- Found LIBUBERTOOTH: ")
        self.assertIn("not in", str(caught.exception))

    def test_under_prefix_needs_exactly_one_marked_line(self):
        for log in ("", "-- Found LIBUBERTOOTH: /a\n-- Found LIBUBERTOOTH: /b\n"):
            with self.subTest(log[:30]):
                with self.assertRaises(InstalledLibraryError) as caught:
                    under_prefix(log, str(self.prefix), "-- Found LIBUBERTOOTH: ")
                self.assertIn("expected exactly one", str(caught.exception))


class ForkPinTests(unittest.TestCase):

    def test_a_tag_pin_is_read_as_it_stands(self):
        # FindBTBB handles a pin that is a tag, which a release points it at,
        # so reading one must not end the build job.
        text = 'set(LIBBTBB_PINNED_REF "2024-12-R1")\n'
        self.assertEqual("2024-12-R1", installedlib.fork_pin(text))

    def test_a_commented_out_pin_is_not_the_pin(self):
        text = ('# set(LIBBTBB_PINNED_REF "0000000")\n'
                'set(LIBBTBB_PINNED_REF "d356e9da7fb03969371aa14d603a8aad2f0ef198")\n')
        self.assertEqual("d356e9da7fb03969371aa14d603a8aad2f0ef198",
                         installedlib.fork_pin(text))

    def test_the_pin_is_read_out_of_the_module(self):
        text = ('set(LIBBTBB_PINNED_REPO "SqueakyFloors/libbtbb")\n'
                'set(LIBBTBB_PINNED_REF "d356e9da7fb03969371aa14d603a8aad2f0ef198")\n')
        self.assertEqual("d356e9da7fb03969371aa14d603a8aad2f0ef198",
                         installedlib.fork_pin(text))

    def test_a_module_that_sets_none_is_refused(self):
        with self.assertRaises(InstalledLibraryError) as caught:
            installedlib.fork_pin("set(LIBBTBB_PINNED_REPO \"x\")\n")
        self.assertIn("LIBBTBB_PINNED_REF", str(caught.exception))

    def test_a_real_module_is_read_where_one_is_named(self):
        # UBERTOOTH_SOURCE is how the sibling fork-reading tests are pointed
        # at a tree (test_dfu_part_table.py, test_libbtbb_drift.py); without
        # it there is no fork here to read.
        source = os.environ.get("UBERTOOTH_SOURCE")
        if not source:
            self.skipTest("UBERTOOTH_SOURCE names no fork tree")
        module = Path(source) / "host/cmake/modules/FindBTBB.cmake"
        if not module.is_file():
            self.skipTest(f"{module} is not there")
        # A commit or, for a release, a tag; either is a pin this reads.
        self.assertTrue(installedlib.fork_pin(module.read_text()).strip())


class AgeHeadersTests(unittest.TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def header(self, name, text):
        (self.directory / name).write_text(text)

    def test_the_function_is_renamed_in_every_header_that_declares_it(self):
        self.header("ubertooth.h", "void other(void);\n")
        self.header("ubertooth_control.h",
                    "int cmd_get_bootloader_info(struct libusb_device_handle *);\n")
        self.header("ubertooth_interface.h",
                    "/* see cmd_get_bootloader_info */\n")
        self.assertEqual(["ubertooth_control.h", "ubertooth_interface.h"],
                         age_headers(self.directory))
        for name in ("ubertooth_control.h", "ubertooth_interface.h"):
            text = (self.directory / name).read_text()
            self.assertNotIn("cmd_get_bootloader_info", text)
            self.assertIn("cmd_absent_bootloader_info", text)
        self.assertEqual("void other(void);\n",
                         (self.directory / "ubertooth.h").read_text())

    def test_a_header_of_another_name_is_left_alone(self):
        self.header("btbb.h", "int cmd_get_bootloader_info(void);\n")
        with self.assertRaises(InstalledLibraryError):
            age_headers(self.directory)
        self.assertIn("cmd_get_bootloader_info",
                      (self.directory / "btbb.h").read_text())

    def test_an_install_that_does_not_declare_it_is_refused(self):
        # The fork renaming or dropping that function would otherwise leave
        # the step passing without having made the install look older.
        self.header("ubertooth.h", "void other(void);\n")
        with self.assertRaises(InstalledLibraryError) as caught:
            age_headers(self.directory)
        self.assertIn("FindUBERTOOTH.cmake", str(caught.exception))

    def test_a_longer_name_beginning_with_it_is_left_alone(self):
        # cmd_get_bootloader_info_quiet sits beside the default in the fork's
        # own header; renaming it as well would make the aged install miss
        # more than the step says.
        self.header("ubertooth_control.h",
                    "int cmd_get_bootloader_info(void);\n"
                    "int cmd_get_bootloader_info_quiet(void);\n")
        self.assertEqual(["ubertooth_control.h"], age_headers(self.directory))
        text = (self.directory / "ubertooth_control.h").read_text()
        self.assertIn("cmd_get_bootloader_info_quiet(void)", text)
        self.assertIn("cmd_absent_bootloader_info(void)", text)

    def test_another_function_can_be_named(self):
        self.header("ubertooth.h", "int ubertooth_start_select(void);\n")
        self.assertEqual(["ubertooth.h"],
                         age_headers(self.directory,
                                     "ubertooth_start_select"))


class CommandLineTests(unittest.TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def log(self, text):
        path = self.directory / "configure.txt"
        path.write_text(text)
        return str(path)

    def run_module(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ubertooth_harness.installedlib", *arguments],
            cwd=ROOT, capture_output=True, text=True, check=False)

    def test_configure_identity_prints_the_line_it_accepted(self):
        done = self.run_module("configure-identity",
                               self.log(OTHER_LINES + AGREED),
                               "--prefix", PREFIX)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("built from commit 52b2afc", done.stdout)

    def test_a_refusal_is_one_line_on_standard_error(self):
        done = self.run_module("configure-identity", self.log(OTHER_LINES),
                               "--prefix", PREFIX)
        self.assertEqual(1, done.returncode)
        self.assertEqual("", done.stdout)
        self.assertIn("installed library:", done.stderr)

    def test_a_directory_that_is_not_there(self):
        done = self.run_module("age-headers", "/nonexistent/include")
        self.assertEqual(1, done.returncode)
        # Named as the missing path, not as a fork module that declares
        # nothing.
        self.assertIn("is not a directory", done.stderr)
        self.assertNotIn("FindUBERTOOTH", done.stderr)

    def test_another_libbtbb_prints_the_warning_it_accepted(self):
        # So a reader of a failed job sees which directory the program
        # loaded libbtbb from.
        decoy, built = self.directory / "decoy", self.directory / "lib"
        decoy.mkdir()
        built.mkdir()
        output = self.directory / "another-libbtbb.txt"
        output.write_text(f"{VERSION_LINE}\n"
                          + WARNING.format(loaded=decoy, built=built)
                          + f"\n{BOARD_LINE}\n")
        done = self.run_module("another-libbtbb", str(output),
                               "--loaded-from", str(decoy),
                               "--built-libbtbb", str(built),
                               "--version-line", VERSION_LINE)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn(str(decoy), done.stdout)

    def test_another_libbtbb_refuses_a_run_with_no_warning(self):
        decoy, built = self.directory / "decoy", self.directory / "lib"
        decoy.mkdir()
        built.mkdir()
        output = self.directory / "another-libbtbb.txt"
        output.write_text(f"{VERSION_LINE}\n{BOARD_LINE}\n")
        done = self.run_module("another-libbtbb", str(output),
                               "--loaded-from", str(decoy),
                               "--built-libbtbb", str(built),
                               "--version-line", VERSION_LINE)
        self.assertEqual(1, done.returncode)
        self.assertEqual("", done.stdout)
        self.assertIn("installed library:", done.stderr)


class BuildStepTests(unittest.TestCase):
    """The build step still makes these checks, in the right order."""

    def setUp(self):
        self.text = CI.read_text()

    def test_the_build_step_calls_the_installed_library_checks(self):
        self.assertIn("check_installed_library()", self.text)
        # The sources the build used, at the ref it resolved, not the clone
        # the job was pointed at.
        self.assertIn('check_installed_library "$WORK/build/src/ubertooth" '
                      '"$WORK/build/prefix"', self.text)
        # Every program it runs is run with the no-devices switch, so the
        # step is about the install and not about attached boards. The
        # assignment and its export, not the words in a comment.
        self.assertIn("    UBERTOOTH_TEST_NO_DEVICES=1\n"
                      "    export UBERTOOTH_TEST_NO_DEVICES\n", self.text)
        self.assertIn("    unset UBERTOOTH_TEST_NO_DEVICES\n", self.text)
        # After the build and before the host-only tests, which need the
        # prefix the build wrote.
        build = self.text.index('check_installed_library "$WORK/build/src')
        self.assertLess(self.text.index('--with-python --output "$WORK/build"'),
                        build)
        self.assertLess(build, self.text.index(
            'step "host-only tests with the built tools'))

    def test_it_asks_pkg_config_and_cmake_for_the_same_install(self):
        for fragment in (
            "pkg-config --exists libubertooth",
            "pkg-config --cflags libubertooth",
            "find_package(ubertooth ${UBERTOOTH_EXPECTED_VERSION} EXACT REQUIRED)",
            "target_link_libraries(consumer PRIVATE ubertooth::ubertooth)",
            # The two files describe one install, so the two consumers must
            # print the same thing.
            'cmp "$il_work/pc-consumer.txt" "$il_work/cmake-consumer.txt"',
            "installedlib consumer-identity",
            "installedlib same-path",
            "installedlib under-prefix",
            "installedlib fork-pin",
            "--variable=$il_variable",
            "--expected-pin",
            "EXACT REQUIRED",
            "--variable=$il_variable",
        ):
            with self.subTest(fragment):
                self.assertIn(fragment, self.text)

    def test_it_requires_the_libbtbb_warning_to_fire_for_a_second_install(self):
        """The live control behind the absence above (#365)."""
        for fragment in (
            "installedlib another-libbtbb",
            # A copy, not a link: the fork resolves both directories, so a
            # link to the install's own would read as one install.
            'cp -R "$il_btbb_libdir"/libbtbb.* "$il_decoy/"',
            # The copy before the install's own directories on both loader
            # paths, so libbtbb comes from the copy and libubertooth from
            # the install.
            "DYLD_LIBRARY_PATH=$il_decoy:$il_libpath",
            "LD_LIBRARY_PATH=$il_decoy:$il_libpath",
            '--loaded-from "$il_decoy"',
        ):
            with self.subTest(fragment):
                self.assertIn(fragment, self.text)
        # After the step whose absence it backs, and before the next one.
        control = self.text.index("installedlib another-libbtbb")
        self.assertLess(self.text.index("installedlib consumer-identity"),
                        control)
        self.assertLess(control, self.text.index(
            'step "a CMake project finds the installed ubertooth package"'))
        # No install this checks prints a path line, so the control runs
        # unconditionally and the step above refuses one (#358).
        self.assertNotIn("libbtbb loaded from", self.text)

    def test_it_configures_the_tools_against_the_install(self):
        self.assertIn('cmake -S "$il_source/host/ubertooth-tools" '
                      '-B "$il_work/tools-build"', self.text)
        self.assertIn('"$il_source/host/cmake/modules/FindBTBB.cmake"', self.text)
        # A packager does not build the tools' own tests, and leaving them on
        # widens what can fail the step for reasons that are not the
        # install's.
        self.assertEqual(2, self.text.count("-DBUILD_TESTING=OFF"))
        self.assertIn("ubertooth_harness.installedlib configure-identity",
                      self.text)
        self.assertIn("--target ubertooth-util", self.text)

    def test_it_requires_an_older_install_to_be_refused_by_name(self):
        self.assertIn("ubertooth_harness.installedlib age-headers", self.text)
        self.assertIn("is older than these tools", self.text)
        self.assertIn(installedlib.DEFAULT_FUNCTION, self.text)
        # The message the step prints where the configure accepted the aged
        # install. This reads the script's text, so it says the message is
        # there and not that the branch printing it is the right one; the
        # build job is what runs the branch.
        self.assertIn("error: the configure accepted a libubertooth missing a "
                      "function", self.text)

    def test_the_script_parses(self):
        done = subprocess.run(["sh", "-n", str(CI)], capture_output=True,
                              text=True, check=False)
        self.assertEqual(0, done.returncode, done.stderr)


if __name__ == "__main__":
    unittest.main()
