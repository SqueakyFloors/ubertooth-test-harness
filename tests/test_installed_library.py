"""The installed-library checks of `scripts/ci.sh build` (#333).

The steps themselves need cmake, a compiler, pkg-config and an installed
libubertooth, so they are the build job's; what is tested here is the two
judgements they rest on (`installedlib.py`) and that the build step still
makes them.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import installedlib
from ubertooth_harness.installedlib import (
    InstalledLibraryError, age_headers, configure_identity,
)

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci.sh"
PREFIX = "/tmp/build/prefix"
# The two wordings FindBTBB.cmake's btbb_report_pin() prints, as CMake
# prefixes them, with the pin written out in full in both.
AGREED = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from commit "
          "52b2afc, which is the commit this tree is built against "
          "(52b2afcb0c4b8b03412dac759aad88debdfb18e2)\n")
DIFFERED = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, built from commit "
            "52b2afc; this tree is built against "
            "(7e1cdf0336857a714da1af8c7496fe32453ac6d0)\n")
UNKNOWN = ("-- libbtbb: /tmp/build/prefix/lib/libbtbb.so, commit unknown "
           "(nothing in it reads as the commit it was built from); this tree "
           "is built against 7e1cdf0336857a714da1af8c7496fe32453ac6d0\n")
OTHER_LINES = "-- The C compiler identification is GNU 13.2.0\n-- Found USB1\n"


class ConfigureIdentityTests(unittest.TestCase):

    def identity(self, text, prefix=PREFIX):
        return configure_identity(text, prefix)

    def test_either_wording_names_both_commits(self):
        for text in (AGREED, DIFFERED, UNKNOWN):
            with self.subTest(text.split(",")[1][:20]):
                line = self.identity(OTHER_LINES + text)
                self.assertTrue(line.startswith("-- libbtbb: "))

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
        self.assertIn("outside", str(caught.exception))

    def test_a_line_without_the_found_commit_is_refused(self):
        text = AGREED.replace("built from commit 52b2afc, which is", "which is")
        with self.assertRaises(InstalledLibraryError) as caught:
            self.identity(OTHER_LINES + text)
        self.assertIn("which commit", str(caught.exception))

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


class AgeHeadersTests(unittest.TestCase):

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())

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

    def test_another_function_can_be_named(self):
        self.header("ubertooth.h", "int ubertooth_start_select(void);\n")
        self.assertEqual(["ubertooth.h"],
                         age_headers(self.directory,
                                     "ubertooth_start_select"))


class CommandLineTests(unittest.TestCase):

    def run_module(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ubertooth_harness.installedlib", *arguments],
            cwd=ROOT, capture_output=True, text=True, check=False)

    def test_configure_identity_prints_the_line_it_accepted(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as log:
            log.write(OTHER_LINES + AGREED)
        done = self.run_module("configure-identity", log.name, "--prefix", PREFIX)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("built from commit 52b2afc", done.stdout)

    def test_a_refusal_is_one_line_on_standard_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as log:
            log.write(OTHER_LINES)
        done = self.run_module("configure-identity", log.name, "--prefix", PREFIX)
        self.assertEqual(1, done.returncode)
        self.assertEqual("", done.stdout)
        self.assertIn("installed library:", done.stderr)

    def test_a_directory_that_is_not_there(self):
        done = self.run_module("age-headers", "/nonexistent/include")
        self.assertEqual(1, done.returncode)
        self.assertIn("installed library:", done.stderr)


class BuildStepTests(unittest.TestCase):
    """The build step still makes these checks, in the right order."""

    def setUp(self):
        self.text = CI.read_text()

    def test_the_build_step_calls_the_installed_library_checks(self):
        self.assertIn("check_installed_library()", self.text)
        self.assertIn('check_installed_library "$ut" "$WORK/build/prefix"',
                      self.text)
        # After the build and before the host-only tests, which need the
        # prefix the build wrote.
        build = self.text.index('check_installed_library "$ut"')
        self.assertLess(self.text.index('--with-python --output "$WORK/build"'),
                        build)
        self.assertLess(build, self.text.index(
            'step "host-only tests with the built tools'))

    def test_it_asks_pkg_config_and_cmake_for_the_same_install(self):
        for fragment in (
            "pkg-config --exists libubertooth",
            "pkg-config --cflags libubertooth",
            "find_package(ubertooth REQUIRED)",
            "target_link_libraries(consumer PRIVATE ubertooth::ubertooth)",
            # The two files describe one install, so the two consumers must
            # print the same thing.
            'cmp "$work/pc-consumer.txt" "$work/cmake-consumer.txt"',
        ):
            with self.subTest(fragment):
                self.assertIn(fragment, self.text)

    def test_it_configures_the_tools_against_the_install(self):
        self.assertIn('cmake -S "$source/host/ubertooth-tools" '
                      '-B "$work/tools-build"', self.text)
        self.assertIn("ubertooth_harness.installedlib configure-identity",
                      self.text)
        self.assertIn("--target ubertooth-util", self.text)

    def test_it_requires_an_older_install_to_be_refused_by_name(self):
        self.assertIn("ubertooth_harness.installedlib age-headers", self.text)
        self.assertIn("is older than these tools", self.text)
        self.assertIn(installedlib.DEFAULT_FUNCTION, self.text)
        # A configure that accepted it is the failure, so the success branch
        # of that `if` has to be the one that fails the step.
        self.assertIn("error: the configure accepted a libubertooth missing a "
                      "function", self.text)

    def test_the_script_parses(self):
        done = subprocess.run(["sh", "-n", str(CI)], capture_output=True,
                              text=True, check=False)
        self.assertEqual(0, done.returncode, done.stderr)


if __name__ == "__main__":
    unittest.main()
