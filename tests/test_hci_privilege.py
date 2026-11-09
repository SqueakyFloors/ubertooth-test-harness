"""The privilege an HCI connection needs, and the copy it is granted to.

Nothing here touches a real capability: `setcap` and `getcap` are fakes the
tests drive, which is what lets the failure paths be exercised at all -- a
grant that cannot be verified, a copy that cannot load the run's libraries,
a removal that did not take. What a real `setcap` does is a one-line
difference from the fake (it writes an extended attribute); what this module
is about is everything around it, which is where a privileged file gets left
behind.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest

from ubertooth_harness import hciprivilege
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.esp32peer import _LastError
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.hciprivilege import (
    BASIS_COPY,
    BASIS_NONE,
    BASIS_PROCESS,
    GRANT,
    HciPrivilegeError,
    availability,
    decide,
    grant,
    net_admin_in_status,
    release,
    tests_needing_privilege,
)
from ubertooth_harness.hostneeds import HostCapabilities, Needs, unmet
from ubertooth_harness.preflight import host_version_patterns
from tests.executables import write_executable

# The line the fork's tools print first, which a privileged copy has to
# answer with for the grant to stand: the release and the libbtbb beside it.
VERSION_LINE = "Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)"
PATTERNS = (r"(?m)^Host tools version: 2026-11-DEV2 \(libbtbb [^)\n]+\)$",)
TOOL = "ubertooth-follow"


class _Fake:
    """A `setcap`, `getcap` and privileged copy that answer out of a dict.

    The capability a file carries lives here rather than in the filesystem,
    so a test can say that `setcap` worked and `getcap` disagreed, which is
    exactly what a nosuid mount does and what no real pair of them would do
    on demand.
    """

    def __init__(self, *, version=VERSION_LINE, reported=None, refuse_set=False,
                 refuse_removal=False, sticky=False, exit_code=0):
        self.caps: dict[str, str] = {}
        self.version = version
        self.reported = reported
        self.refuse_set = refuse_set
        self.refuse_removal = refuse_removal
        self.sticky = sticky
        self.exit_code = exit_code
        self.argv: list[tuple[str, ...]] = []
        self.environments: list[dict] = []

    def which(self, name, path=None):
        """libcap's programs and sudo are the fakes; anything else is real.

        The tool itself is looked up the way the run does it, on the path it
        is given, so a test cannot accidentally privilege something that is
        not in its own prefix.
        """
        if name in ("setcap", "getcap"):
            return f"/usr/sbin/{name}"
        if name == "sudo":
            return "/usr/bin/sudo"
        return shutil.which(name, path=path)

    def run(self, argv, *, timeout_s, env=None):
        argv = tuple(str(item) for item in argv)
        self.argv.append(argv)
        words = [word for word in argv if not word.startswith("-")]
        program = Path(words[0]).name if words else ""
        if program == "sudo":
            program = Path(words[1]).name
        if program == "setcap":
            return self._setcap(argv)
        if program == "getcap":
            return self._getcap(argv)
        self.environments.append(dict(env or {}))
        return CommandResult(argv, self.exit_code, self.version + "\n", "",
                             0.1, False)

    def _setcap(self, argv):
        target = argv[-1]
        if "-r" in argv:
            if self.refuse_removal:
                return CommandResult(argv, 1, "", "unable to set\n", 0.1, False)
            if not self.sticky:
                self.caps.pop(target, None)
            return CommandResult(argv, 0, "", "", 0.1, False)
        if self.refuse_set:
            return CommandResult(argv, 1, "", "setcap: Operation not supported\n",
                                 0.1, False)
        self.caps[target] = GRANT
        return CommandResult(argv, 0, "", "", 0.1, False)

    def _getcap(self, argv):
        target = argv[-1]
        held = self.caps.get(target, "")
        if held and self.reported is not None:
            line = f"{target} {self.reported}"
        elif held:
            line = f"{target} {held.replace('+', '=')}"
        else:
            line = ""
        return CommandResult(argv, 0, line + ("\n" if line else ""), "", 0.1, False)


class _Bench:
    """A run prefix with the tool in it, and somewhere for the copy to go."""

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.__enter__())
        self.prefix = root / "prefix" / "bin"
        self.prefix.mkdir(parents=True)
        self.source = write_executable(
            self.prefix / TOOL, "#!/bin/sh\nexit 0\n", mode=0o755)
        self.directory = root / "run" / hciprivilege.DIRECTORY_NAME
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        return self

    def __exit__(self, *exception):
        self._temporary.__exit__(*exception)
        return False

    def grant(self, fake, **extra):
        return grant(
            (TOOL,), directory=self.directory, path=str(self.prefix),
            version_patterns=extra.pop("version_patterns", PATTERNS),
            runner=fake.run, which=fake.which, **extra,
        )


class StatusTests(unittest.TestCase):

    def test_the_effective_set_is_what_is_read(self):
        # Bit 12. A process holding it permitted but not effective cannot
        # page either, so the permitted set is not what decides.
        self.assertTrue(net_admin_in_status(
            "Name:\tpython3\nCapEff:\t0000000000001000\n"))
        self.assertTrue(net_admin_in_status("CapEff:\t000001ffffffffff\n"))
        self.assertFalse(net_admin_in_status(
            "CapPrm:\t0000000000001000\nCapEff:\t0000000000000000\n"))
        self.assertFalse(net_admin_in_status("CapEff:\t0000000000000800\n"))

    def test_a_status_this_cannot_read_is_not_a_capability(self):
        # Fail closed: a run that cannot establish it holds the capability
        # must not assume it does.
        self.assertFalse(net_admin_in_status(""))
        self.assertFalse(net_admin_in_status("CapEff:\tnot a number\n"))
        self.assertFalse(net_admin_in_status("CapEffective:\tffffffffffffffff\n"))

    def test_a_missing_proc_file_is_not_a_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(hciprivilege.process_holds_net_admin(
                Path(directory) / "nothing"))


class AvailabilityTests(unittest.TestCase):

    def _status(self, text):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".status", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_off_linux_there_is_no_capability_to_have(self):
        decided = availability("macos", allow=True)
        self.assertFalse(decided.available)
        self.assertEqual(BASIS_NONE, decided.basis)
        self.assertIn("Linux", decided.reason)

    def test_a_run_that_holds_it_grants_nothing_to_a_file(self):
        decided = availability(
            "linux", allow=False,
            status=self._status("CapEff:\t0000000000001000\n"))
        self.assertTrue(decided.available)
        self.assertEqual(BASIS_PROCESS, decided.basis)
        # Even without the option: there is nothing to arm, because nothing
        # is granted.
        self.assertIn("nothing is granted to a file", decided.reason)

    def test_without_the_option_the_reason_names_the_tool_s_own_error(self):
        decided = availability(
            "linux", allow=False, status=self._status("CapEff:\t0\n"))
        self.assertFalse(decided.available)
        self.assertEqual(BASIS_NONE, decided.basis)
        # The sentence a reader gets has to be the one that was costing the
        # diagnosis: the tool's own message, and what to do about it (#401).
        self.assertIn("Operation not permitted", decided.reason)
        self.assertIn("--allow-hci-capability", decided.reason)

    def test_the_option_without_libcap_is_still_no(self):
        fake = _Fake()
        decided = availability(
            "linux", allow=True, status=self._status("CapEff:\t0\n"),
            which=lambda name, path=None: None)
        self.assertFalse(decided.available)
        self.assertIn("setcap and getcap", decided.reason)
        self.assertIn("libcap2-bin", decided.reason)
        # And with libcap but no sudo, which is what sets it.
        decided = availability(
            "linux", allow=True, status=self._status("CapEff:\t0\n"),
            which=lambda name, path=None: (
                fake.which(name, path) if name != "sudo" else None))
        self.assertFalse(decided.available)
        self.assertIn("sudo", decided.reason)

    def test_the_option_with_libcap_is_the_private_copy(self):
        decided = availability(
            "linux", allow=True, status=self._status("CapEff:\t0\n"),
            which=_Fake().which)
        self.assertTrue(decided.available)
        self.assertEqual(BASIS_COPY, decided.basis)


class DecisionTests(unittest.TestCase):

    def setUp(self):
        self.catalog = builtin_catalog()

    def test_the_catalog_says_which_tests_page(self):
        # One statement of it, so nothing here holds a second list: the
        # follow test pages in every case, and the scan's name reads are
        # inside what an unprivileged HCI socket may send.
        self.assertTrue(
            self.catalog.get("Follow Classic piconet over HCI").needs.hci_connect)
        self.assertFalse(
            self.catalog.get("Bluetooth inquiry scan").needs.hci_connect)

    def test_a_run_that_cannot_page_blocks_those_tests_and_runs_the_rest(self):
        invocations = (SimpleNamespace(test_name="Follow Classic piconet over HCI"),
                       SimpleNamespace(test_name="Bluetooth inquiry scan"))
        decided, wanted, blocked, kept = decide(
            self.catalog, invocations, host="Host_Linux", platform="linux",
            allow=False, status=Path("/nonexistent"))
        self.assertFalse(decided.available)
        # Blocked, not "not applicable": the test is a release gate, and a
        # gate that was never measured must fail the run rather than leave
        # it reading as a pass.
        self.assertEqual(["Follow Classic piconet over HCI"],
                         [item.test_name for item in blocked])
        self.assertIn("Operation not permitted", blocked[0].reason)
        self.assertEqual(["Bluetooth inquiry scan"],
                         [item.test_name for item in kept])
        # Nothing is granted for a run that cannot page.
        self.assertEqual((), wanted)

    def test_a_run_that_can_page_keeps_them_and_names_them_for_the_grant(self):
        invocations = (SimpleNamespace(test_name="Follow Classic piconet over HCI"),)
        _decided, wanted, blocked, kept = decide(
            self.catalog, invocations, host="Host_Linux", platform="linux",
            allow=True, which=_Fake().which, status=Path("/nonexistent"))
        self.assertEqual(("Follow Classic piconet over HCI",), wanted)
        self.assertEqual((), blocked)
        self.assertEqual(1, len(kept))

    def test_a_suite_that_pages_nothing_asks_for_nothing(self):
        invocations = (SimpleNamespace(test_name="Bluetooth inquiry scan"),)
        _decided, wanted, blocked, kept = decide(
            self.catalog, invocations, host="Host_Linux", platform="linux",
            allow=False, status=Path("/nonexistent"))
        self.assertEqual(((), (), 1), (wanted, blocked, len(kept)))
        self.assertEqual((), tests_needing_privilege(self.catalog, invocations))

    def test_the_case_level_need_leaves_the_cases_out_as_well(self):
        # Defense in depth for a caller that is not run-suite: the reason
        # comes from the capability record, so it is the same sentence.
        capabilities = HostCapabilities(
            "Host_Linux", "linux", frozenset(("ubertooth-follow",)),
            host_tools=frozenset(("hciconfig", "hcitool")),
            hci_connect=False, hci_connect_reason="no capability here",
        )
        self.assertEqual("no capability here", unmet(
            Needs(platforms=("linux",), bluez_tools=("ubertooth-follow",),
                  host_tools=("hciconfig", "hcitool"), hci_connect=True),
            capabilities))
        # Not known yet counts as met, as every other need does.
        self.assertEqual("", unmet(
            Needs(hci_connect=True),
            HostCapabilities("Host_Linux", "linux", frozenset())))

    def test_the_missing_tool_is_said_before_the_missing_privilege(self):
        # A host without the tool cannot run the case whatever it holds, and
        # naming the capability there would send a reader after the wrong
        # thing.
        reason = unmet(
            Needs(platforms=("linux",), bluez_tools=("ubertooth-follow",),
                  hci_connect=True),
            HostCapabilities("Host_Linux", "linux", frozenset(),
                             hci_connect=False, hci_connect_reason="no cap"))
        self.assertIn("built only when libbluetooth is found", reason)


class GrantTests(unittest.TestCase):

    def test_the_copy_is_the_run_s_own_binary_privileged_and_verified(self):
        with _Bench() as bench:
            fake = _Fake()
            granted = bench.grant(fake)
            copy = Path(granted.copies[0].path)
            self.assertTrue(granted.granted)
            self.assertEqual(BASIS_COPY, granted.basis)
            # The copy is the source, byte for byte, and the record says so
            # with the digest that was compared.
            self.assertEqual(bench.source.read_bytes(), copy.read_bytes())
            self.assertEqual(bench.digest, granted.copies[0].source_sha256)
            # Owner-only, and not writable: nothing has to change its mode
            # again for it to be used, and nobody else can reach it.
            self.assertEqual(0o500, stat.S_IMODE(copy.stat().st_mode))
            self.assertEqual("0500", granted.copies[0].mode)
            self.assertEqual(0o700, stat.S_IMODE(bench.directory.stat().st_mode))
            self.assertEqual(os.geteuid(), copy.stat().st_uid)
            # What getcap read back is recorded, not what setcap was asked for.
            self.assertEqual("cap_net_admin=ep", granted.copies[0].capability)
            self.assertEqual(VERSION_LINE, granted.copies[0].version_line)
            release(granted)

    def test_the_source_in_the_run_prefix_is_never_privileged(self):
        with _Bench() as bench:
            fake = _Fake()
            granted = bench.grant(fake)
            self.assertEqual({str(Path(granted.copies[0].path))},
                             set(fake.caps))
            self.assertNotIn(str(bench.source), fake.caps)
            release(granted)

    def test_the_copy_is_verified_with_no_library_path_in_its_environment(self):
        # A file capability puts the loader in secure-execution mode, where
        # LD_LIBRARY_PATH is dropped. So the check has to be made without
        # one: a copy that only loads the run's libraries through the
        # environment must fail here rather than at a case.
        with _Bench() as bench:
            fake = _Fake()
            granted = bench.grant(fake)
            self.assertEqual(1, len(fake.environments))
            environment = fake.environments[0]
            self.assertEqual([], [key for key in environment
                                  if key.startswith("LD_")])
            self.assertEqual("1", environment["UBERTOOTH_TEST_NO_DEVICES"])
            release(granted)

    def test_a_copy_that_cannot_load_the_run_s_libraries_fails_closed(self):
        with _Bench() as bench:
            # The version line a system install would answer with.
            fake = _Fake(version="Host tools version: 2020-12-R1 (libbtbb 2020-12-R1)")
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            self.assertIn("secure-execution mode", str(raised.exception))
            self.assertFalse(bench.directory.exists())
            self.assertEqual({}, fake.caps)

    def test_a_copy_that_reached_another_libbtbb_fails_closed(self):
        with _Bench() as bench:
            fake = _Fake(version=VERSION_LINE + "\nwarning: libbtbb was loaded "
                         "from /usr/lib, not the /run/lib these tools were "
                         "built against; packets may decode differently")
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            self.assertIn("loaded a libbtbb other than", str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_a_capability_that_did_not_take_fails_closed(self):
        # What a nosuid mount, or a filesystem without extended attributes,
        # leaves: setcap says it worked and getcap reports nothing.
        with _Bench() as bench:
            fake = _Fake()
            fake._setcap = lambda argv: CommandResult(argv, 0, "", "", 0.1, False)
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            self.assertIn("nosuid", str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_a_setcap_that_refused_fails_closed(self):
        with _Bench() as bench:
            fake = _Fake(refuse_set=True)
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            self.assertIn("Operation not supported", str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_more_than_this_grant_fails_closed(self):
        # A file that came back carrying more than was asked for is not what
        # this granted, whoever put the rest there.
        with _Bench() as bench:
            fake = _Fake(reported="cap_net_admin,cap_net_raw=ep")
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            self.assertIn("cap_net_admin+ep and nothing else",
                          str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_the_older_getcap_wording_is_the_same_file(self):
        with _Bench() as bench:
            fake = _Fake(reported="= cap_net_admin+ep")
            granted = bench.grant(fake)
            self.assertEqual("cap_net_admin+ep", granted.copies[0].capability)
            release(granted)

    def test_a_directory_left_behind_is_never_reused(self):
        with _Bench() as bench:
            bench.directory.mkdir(parents=True)
            (bench.directory / TOOL).write_text("something else")
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(_Fake())
            self.assertIn("already exists", str(raised.exception))
            # And what was there is left alone: it is not this run's.
            self.assertEqual("something else",
                             (bench.directory / TOOL).read_text())

    def test_a_tool_not_on_the_run_s_path_is_a_reason_not_a_system_binary(self):
        with _Bench() as bench:
            with self.assertRaises(HciPrivilegeError) as raised:
                grant((TOOL,), directory=bench.directory, path="/nonexistent",
                      version_patterns=PATTERNS, runner=_Fake().run,
                      which=_Fake().which)
            self.assertIn("not on this run's PATH", str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_only_the_named_tools_may_be_privileged(self):
        with _Bench() as bench:
            with self.assertRaises(HciPrivilegeError) as raised:
                grant(("ubertooth-scan",), directory=bench.directory,
                      path=str(bench.prefix), version_patterns=PATTERNS,
                      runner=_Fake().run, which=_Fake().which)
            self.assertIn("not a tool this grants a capability to",
                          str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_a_run_that_named_no_version_line_verifies_nothing_and_refuses(self):
        with _Bench() as bench:
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(_Fake(), version_patterns=())
            self.assertIn("named none", str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_a_world_writable_source_is_not_this_run_s_to_privilege(self):
        with _Bench() as bench:
            # Written here rather than chmodded: write_executable hands out
            # hard links to one cached file, and changing its mode would
            # change every other test's copy of it.
            bench.source.unlink()
            bench.source.write_text("#!/bin/sh\nexit 0\n")
            bench.source.chmod(0o777)
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(_Fake())
            self.assertIn("writable by its group or by everyone",
                          str(raised.exception))
            self.assertFalse(bench.directory.exists())

    def test_a_failed_grant_keeps_its_own_reason_first(self):
        # A cleanup that also went wrong is added to the reason, never
        # substituted for it: why the grant failed is what a reader needs,
        # and a removal problem on top of it is the second sentence.
        with _Bench() as bench:
            fake = _Fake(version="Host tools version: nothing like it", sticky=True)
            with self.assertRaises(HciPrivilegeError) as raised:
                bench.grant(fake)
            reason = str(raised.exception)
            self.assertIn("secure-execution mode", reason)
            self.assertIn("; and the HCI capability could not be taken back",
                          reason)
            self.assertFalse(bench.directory.exists())

    def test_the_patterns_are_the_ones_preflight_holds_the_tools_to(self):
        # Not a second expectation written here: the copy answers the same
        # version line preflight requires of the run's tools.
        source = SimpleNamespace(expected_revision="2026-11-DEV2",
                                 names_release=True)
        patterns = host_version_patterns(source)
        self.assertTrue(patterns)
        with _Bench() as bench:
            granted = bench.grant(_Fake(), version_patterns=patterns)
            self.assertEqual(VERSION_LINE, granted.copies[0].version_line)
            release(granted)


class ReleaseTests(unittest.TestCase):

    def test_the_capability_is_taken_back_and_verified_gone(self):
        with _Bench() as bench:
            fake = _Fake()
            granted = bench.grant(fake)
            copy = Path(granted.copies[0].path)
            release(granted)
            self.assertTrue(granted.released)
            self.assertEqual({}, fake.caps)
            self.assertFalse(copy.exists())
            self.assertFalse(bench.directory.exists())
            # The record says what was established, not only that it ran.
            self.assertTrue(
                any("getcap reports nothing" in note for note in granted.removal))
            self.assertTrue(any("unlinked" in note for note in granted.removal))
            self.assertIn("-r", [word for argv in fake.argv for word in argv])

    def test_releasing_twice_is_the_same_as_once(self):
        with _Bench() as bench:
            granted = bench.grant(_Fake())
            release(granted)
            notes = granted.removal
            release(granted)
            self.assertEqual(notes, granted.removal)

    def test_a_capability_that_will_not_come_off_is_reported_and_the_file_goes(self):
        # Unlinking the file takes its extended attributes with it, so the
        # worst case is an empty directory -- but a removal that did not come
        # back clean is said out loud, because the run's verdict turns on it.
        with _Bench() as bench:
            fake = _Fake(sticky=True)
            granted = bench.grant(fake)
            copy = Path(granted.copies[0].path)
            with self.assertRaises(HciPrivilegeError) as raised:
                release(granted)
            self.assertIn("still reports", str(raised.exception))
            self.assertFalse(copy.exists())
            self.assertFalse(bench.directory.exists())

    def test_a_setcap_r_that_failed_is_reported(self):
        with _Bench() as bench:
            fake = _Fake(refuse_removal=True, sticky=True)
            granted = bench.grant(fake)
            with self.assertRaises(HciPrivilegeError):
                release(granted)
            self.assertTrue(any("setcap -r" in note for note in granted.removal))
            self.assertFalse(bench.directory.exists())

    def test_a_grant_that_was_never_made_releases_nothing(self):
        held = hciprivilege.Grant(BASIS_PROCESS, "the run holds it")
        self.assertFalse(held.granted)
        self.assertIs(held, release(held))
        self.assertTrue(held.released)

    def test_the_atexit_backstop_knows_about_a_live_grant(self):
        # A path that reached no finally still gives the capability back.
        with _Bench() as bench:
            fake = _Fake()
            granted = bench.grant(fake)
            self.assertIn(granted, hciprivilege._live)
            hciprivilege._at_exit()
            self.assertEqual({}, fake.caps)
            self.assertFalse(bench.directory.exists())
            self.assertNotIn(granted, hciprivilege._live)


class EnvironmentTests(unittest.TestCase):

    def test_the_copies_come_first_and_keep_the_tool_s_own_name(self):
        # Which is what leaves every command line, step name and output
        # pattern in the suite exactly as a reader already knows it.
        with _Bench() as bench:
            granted = bench.grant(_Fake())
            environment = granted.environment({"PATH": "/usr/bin"})
            self.assertEqual(f"{bench.directory}{os.pathsep}/usr/bin",
                             environment["PATH"])
            self.assertEqual(TOOL, Path(granted.copies[0].path).name)
            release(granted)

    def test_a_grant_of_nothing_leaves_the_path_alone(self):
        held = hciprivilege.Grant(BASIS_PROCESS, "the run holds it")
        self.assertEqual({"PATH": "/usr/bin"},
                         held.environment({"PATH": "/usr/bin"}))

    def test_the_record_carries_the_copy_the_hash_and_the_removal(self):
        with _Bench() as bench:
            granted = bench.grant(_Fake())
            release(granted)
            details = granted.to_details()
            self.assertEqual(GRANT, details["capability"])
            self.assertTrue(details["released"])
            self.assertEqual(bench.digest, details["copies"][0]["source_sha256"])
            # Resolved, as every path a record carries is: on macOS /var is a
            # link to /private/var, and a record that said one and meant the
            # other could not be compared with anything.
            self.assertEqual(str(bench.source.resolve()),
                             details["copies"][0]["source"])
            self.assertTrue(details["removal"])


class ToolErrorLineTests(unittest.TestCase):
    """The tool's own last error line, kept for the step's failure (#401)."""

    def test_the_last_error_line_is_what_a_failure_names(self):
        tracker = _LastError()
        tracker.write("Target 20:50:0D:E9:C5:BA: LAP=e9c5ba UAP=0d\n")
        tracker.write("error: connecting to 20:50:0D:E9:C5:BA failed: "
                      "Operation not permitted\n")
        self.assertEqual(
            "ubertooth-follow exited 1: error: connecting to "
            "20:50:0D:E9:C5:BA failed: Operation not permitted",
            tracker.exit_problem(("ubertooth-follow", "-t", "x"), 1))

    def test_a_later_error_replaces_an_earlier_one(self):
        tracker = _LastError()
        tracker.write("error: first\nsomething else\nerror: second\n")
        self.assertIn("error: second",
                      tracker.exit_problem(("ubertooth-rx",), 1))

    def test_a_command_that_exited_zero_adds_nothing(self):
        tracker = _LastError()
        tracker.write("error: something it recovered from\n")
        self.assertEqual("", tracker.exit_problem(("ubertooth-rx",), 0))

    def test_a_non_zero_exit_with_no_error_line_adds_nothing(self):
        # A tool interrupted at the planned end of its capture may exit
        # non-zero with nothing wrong, and the exit status is already in the
        # session's report and in the pattern every case that pins it checks.
        tracker = _LastError()
        tracker.write("AFH map: 0x7fffffffff\n")
        self.assertEqual("", tracker.exit_problem(("ubertooth-afh",), -2))

    def test_a_word_that_merely_starts_with_error_is_not_an_error_line(self):
        tracker = _LastError()
        tracker.write("errors 0\nerror:\n")
        self.assertEqual("", tracker.exit_problem(("ubertooth-rx",), 1))


if __name__ == "__main__":
    unittest.main()
