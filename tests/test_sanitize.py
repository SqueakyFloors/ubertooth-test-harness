"""Sanitization and the scan that refuses what it missed (#370).

Every check here is deterministic text in and text out: no run directory, no
hardware, and no clock.
"""

import unittest

from ubertooth_harness import sanitize
from ubertooth_harness.sanitize import (
    ADDRESS,
    EMAIL,
    HOME,
    HOST,
    SERIAL,
    TMP,
    USER,
    Private,
    Unsafe,
    findings,
    line_findings,
    require_safe,
)


BENCH = Private(
    serials=("aaaa0000bbbb1111cccc2222dddd3333", "001050210121"),
    users=("ada",),
    hosts=("bench-linux-7",),
    aliases={"Host_Linux": "host 1", "UT_0": "Ubertooth 1", "UT": "Ubertooth 9"},
)


class SanitizeTests(unittest.TestCase):
    def test_a_home_directory_goes_and_the_checkout_stays(self):
        for path, expected in (
            ("/Users/ada/src/ubertooth/host", f"{HOME}/src/ubertooth/host"),
            ("/home/ada/src/ubertooth", f"{HOME}/src/ubertooth"),
            ("/root/runs/report.md", f"{HOME}/runs/report.md"),
        ):
            with self.subTest(path=path):
                self.assertEqual(sanitize.sanitize(path), expected)

    def test_a_temporary_or_runtime_directory_goes(self):
        for path, expected in (
            ("/tmp/build-1/prefix", f"{TMP}/build-1/prefix"),
            ("/private/var/folders/aa/bb/T/x", f"{TMP}/T/x"),
            ("/run/user/1000/bus", f"{TMP}/bus"),
        ):
            with self.subTest(path=path):
                self.assertEqual(sanitize.sanitize(path), expected)

    def test_a_named_prefix_wins_over_the_general_rules(self):
        private = Private().with_paths(("/Users/ada/bench",))
        self.assertEqual(
            sanitize.sanitize("/Users/ada/bench/runs/r1", private),
            f"{sanitize.PATH}/runs/r1",
        )

    def test_the_lease_holder_and_the_machine_go(self):
        text = "waiting for UT_0, held by ada on bench-linux-7 since 10:00"
        self.assertEqual(
            sanitize.sanitize(text, BENCH),
            f"waiting for Ubertooth 1, held by {USER} on {HOST} since 10:00",
        )

    def test_a_user_and_machine_written_together_become_the_machine(self):
        self.assertEqual(
            sanitize.sanitize("rsync ada@bench-linux-7:/x", BENCH),
            f"rsync {HOST}:/x",
        )

    def test_a_machine_after_a_known_user_is_still_caught(self):
        # The rewriting replaces the user it knows and cannot know every
        # machine, so both `@` rules have to fire on what it leaves.
        line = sanitize.sanitize("cloned from ada@buildbox:ubertooth.git", BENCH)
        self.assertEqual(f"cloned from {USER}@buildbox:ubertooth.git", line)
        self.assertEqual(
            [item.rule for item in line_findings(line, BENCH)],
            [sanitize.RULE_AT_HOST],
        )

    def test_a_remote_login_command_is_caught_after_the_user_is_masked(self):
        line = sanitize.sanitize("ssh ada@buildbox", BENCH)
        self.assertIn(sanitize.RULE_SSH,
                      [item.rule for item in line_findings(line, BENCH)])

    def test_the_gate_leaves_what_the_rewriting_leaves(self):
        # `ada` is a lease holder and `UT_0` an alias; neither is found
        # inside a longer word, by either half.
        for line in ("the adapter was reset", "UT_01 answered"):
            with self.subTest(line=line):
                self.assertEqual(sanitize.sanitize(line, BENCH), line)
                self.assertEqual(line_findings(line, BENCH), [])

    def test_an_ipv6_address_goes_and_a_bluetooth_address_stays(self):
        self.assertEqual(
            sanitize.sanitize("peer at 2001:db8::1 answered"),
            f"peer at {ADDRESS} answered")
        for kept in ("00:00:10.438", "C0:00:00:00:00:01", "::1"):
            with self.subTest(kept=kept):
                self.assertEqual(sanitize.sanitize(kept), kept)
                self.assertEqual(line_findings(kept), [])

    def test_a_prefix_that_would_mangle_the_text_is_not_replaced(self):
        for prefix in ("/", "", "   "):
            with self.subTest(prefix=prefix):
                self.assertFalse(sanitize.usable_prefix(prefix))
                private = Private().with_paths((prefix,))
                self.assertEqual(sanitize.sanitize("suite release", private),
                                 "suite release")

    def test_a_mail_address_goes_whole(self):
        self.assertEqual(
            sanitize.sanitize("filed by ada@example.com", BENCH),
            f"filed by {EMAIL}",
        )

    def test_a_name_in_a_private_domain_goes(self):
        self.assertEqual(
            sanitize.sanitize("dialed runner.bench.internal", Private()),
            f"dialed {HOST}",
        )

    def test_an_address_goes_and_loopback_stays(self):
        self.assertEqual(
            sanitize.sanitize("controller on 127.0.0.1:8765 via 10.1.2.3"),
            f"controller on 127.0.0.1:8765 via {ADDRESS}",
        )

    def test_four_numbers_that_are_not_an_address_are_left(self):
        self.assertEqual(sanitize.sanitize("API 1.2.3.999"), "API 1.2.3.999")

    def test_a_serial_is_masked_the_way_a_report_masks_it(self):
        text = "Ubertooth UT_0, serial aaaa0000bbbb1111cccc2222dddd3333"
        self.assertEqual(
            sanitize.sanitize(text, BENCH),
            f"Ubertooth Ubertooth 1, serial {SERIAL}",
        )

    def test_a_serial_inside_a_port_name_is_masked_and_the_port_stays(self):
        text = "/dev/serial/by-id/usb-SEGGER_J-Link_001050210121-if00"
        self.assertEqual(
            sanitize.sanitize(text, BENCH),
            f"/dev/serial/by-id/usb-SEGGER_J-Link_{SERIAL}-if00",
        )

    def test_an_alias_inside_a_longer_alias_is_not_replaced_on_its_own(self):
        # `UT` is a registered alias of its own and `UT_0` is another: the
        # longer one is replaced as itself, not as `UT` and a stray `_0`.
        self.assertEqual(
            sanitize.sanitize("UT_0 and UT", BENCH), "Ubertooth 1 and Ubertooth 9")

    def test_an_alias_is_not_found_inside_a_word(self):
        self.assertEqual(sanitize.sanitize("UT_01 stays", BENCH), "UT_01 stays")

    def test_the_evidence_a_release_needs_is_kept(self):
        text = (
            "https://github.com/SqueakyFloors/ubertooth at "
            "9903b4251f0e4c0c8b1f2a3d4e5f60718293a4b5, Host tools version: "
            "2026-11-DEV1+22 (libbtbb 2020-12-R1+79), suite release, "
            "2026-11-05T09:12:33.004, passed in 139.0s"
        )
        self.assertEqual(sanitize.sanitize(text, BENCH), text)
        self.assertEqual(findings(text, BENCH), ())


class ScanTests(unittest.TestCase):
    def test_a_system_path_is_not_a_finding(self):
        for path in ("/usr/local/bin/ubertooth-util", "/dev/bus/usb/001/004",
                     "/opt/homebrew/lib/libbtbb.dylib", "/etc/udev/rules.d"):
            with self.subTest(path=path):
                self.assertEqual(line_findings(path), [])

    def test_an_unrecognized_absolute_path_is_a_finding(self):
        found = line_findings("built in /srv/bench/runs/r1")
        self.assertEqual([item.rule for item in found], [sanitize.RULE_PATH])
        self.assertEqual(found[0].text, "/srv/bench/runs/r1")

    def test_a_placeholder_is_not_a_finding(self):
        line = " ".join(sanitize.PLACEHOLDERS) + f" {HOME}/src {TMP}/build"
        self.assertEqual(line_findings(line), [])

    def test_a_relative_path_and_a_count_are_not_paths(self):
        self.assertEqual(
            line_findings("output/016-dump.stderr.log: 12/12 passed"), [])

    def test_a_path_after_a_colon_or_a_redirect_is_read(self):
        for line in ("PKG_CONFIG_PATH=/usr/lib:/srv/bench/lib",
                     "ubertooth-dump 2>/srv/bench/out.log"):
            with self.subTest(line=line):
                self.assertIn(
                    sanitize.RULE_PATH,
                    [item.rule for item in line_findings(line)])

    def test_a_url_and_a_placeholder_path_are_not_paths(self):
        self.assertEqual(line_findings(
            "https://github.com/SqueakyFloors/ubertooth/releases"), [])
        self.assertEqual(line_findings(f"built in {sanitize.PATH}/host"), [])

    def test_a_builds_own_relative_arguments_are_not_paths(self):
        # The firmware compiles with its include and listing arguments
        # written relative to the directory make ran in, and the run records
        # the command. None of those slashes starts a path on any host, and
        # the export refused the whole DEV2 bundle over them (#413).
        line = (
            "arm-none-eabi-gcc -D__thumb2__=1 -msoft-float -mtune=cortex-m3 "
            "-Wa,-alhms=./bluetooth_rxtx.lst -I../common "
            "-I../common/lpcusb/target -I../../host/libubertooth/src "
            "-std=gnu99 -I. -MMD -MP -MD bluetooth_rxtx.c"
        )
        self.assertEqual(sanitize.sanitize(line, BENCH), line)
        self.assertEqual(line_findings(line, BENCH), [])

    def test_a_relative_argument_is_read_the_same_way_quoted(self):
        for line in ('-I"../common"', "-I'../common'", '-I "../common"',
                     "cc -o ./build/x.o -c ../common/x.c"):
            with self.subTest(line=line):
                self.assertEqual(line_findings(line), [])

    def test_an_absolute_argument_of_the_same_form_is_still_a_finding(self):
        # The relative form is what a build writes; the absolute form names a
        # directory on the test host, and naming it is what the gate is for.
        for line, path in (
            ("-I /srv/bench/common", "/srv/bench/common"),
            ("-Wa,-alhms=/srv/bench/bluetooth_rxtx.lst",
             "/srv/bench/bluetooth_rxtx.lst"),
            ('-I"/srv/bench/common"', "/srv/bench/common"),
            ("make -C /srv/bench/firmware", "/srv/bench/firmware"),
        ):
            with self.subTest(line=line):
                found = line_findings(line)
                self.assertEqual([item.rule for item in found],
                                 [sanitize.RULE_PATH])
                self.assertEqual(found[0].text, path)

    def test_traversal_neither_launders_a_path_nor_makes_one(self):
        # Climbing out of an allowed root does not make the bench directory
        # it lands in publishable; climbing about inside one is what a
        # toolchain path does; and a traversal the rewriting left behind a
        # placeholder is not a path of its own.
        found = line_findings("-I /usr/../srv/bench/include")
        self.assertEqual([item.rule for item in found], [sanitize.RULE_PATH])
        self.assertEqual(found[0].text, "/usr/../srv/bench/include")
        self.assertEqual(line_findings("-I /usr/lib/gcc/../../include"), [])
        self.assertEqual(
            line_findings(f"-I{sanitize.PATH}/../host/libubertooth/src"), [])

    def test_a_relative_path_is_read_the_same_way_however_it_is_spelled(self):
        # This rule reads absolute paths, and a relative one names no
        # directory on any host. The same tail without the dots was always
        # published, so the dots cannot be what decides it.
        for line in ("-I../srv/bench/private/include",
                     "-Isrv/bench/private/include",
                     "-I./srv/bench/private/include"):
            with self.subTest(line=line):
                self.assertEqual(line_findings(line), [])
        # What the export does know to be private is still found inside one,
        # and a prefix an operator named is rewritten there too, so a bench
        # directory is answered in either spelling.
        self.assertEqual(sanitize.sanitize("-I../bench-linux-7/include", BENCH),
                         f"-I../{HOST}/include")
        named = Private().with_paths(("/srv/bench",))
        self.assertEqual(
            sanitize.sanitize("-I../srv/bench/private/include", named),
            f"-I..{sanitize.PATH}/private/include")

    def test_a_path_after_text_this_module_cut_is_still_read(self):
        # Three dots are how a cut is marked, not a path component, so a
        # bench path that lands against one is still refused.
        found = line_findings("logged to.../srv/bench/out.log")
        self.assertEqual([item.text for item in found], ["/srv/bench/out.log"])

    def test_a_command_line_of_several_path_forms_names_only_the_bench_one(self):
        line = (
            f"/usr/bin/make -C {sanitize.PATH}/firmware/bluetooth_rxtx "
            "-I../common -I '/srv/bench/vendor/include' -L/opt/homebrew/lib "
            f"-o {HOME}/out.elf 2>/srv/bench/build.log"
        )
        found = line_findings(line, BENCH)
        self.assertEqual([item.rule for item in found],
                         [sanitize.RULE_PATH, sanitize.RULE_PATH])
        self.assertEqual([item.text for item in found],
                         ["/srv/bench/vendor/include", "/srv/bench/build.log"])

    def test_a_sanitized_line_of_everything_comes_back_clean(self):
        line = (
            "UT_0 serial aaaa0000bbbb1111cccc2222dddd3333 held by "
            "ada@bench-linux-7, built in /Users/ada/src, logs in /tmp/x, "
            "runner.bench.internal 10.0.0.9, ada@example.com"
        )
        self.assertEqual(findings(sanitize.sanitize(line, BENCH), BENCH), ())

    def test_each_rule_is_read_over_the_finished_text(self):
        for line, rule in (
            ("/home/ada/x", sanitize.RULE_HOME),
            ("/tmp/x", sanitize.RULE_TMP),
            ("/run/user/1000", sanitize.RULE_RUNTIME),
            ("ada@example.com", sanitize.RULE_MAIL),
            ("ada@bench-linux-7", sanitize.RULE_AT_HOST),
            ("runner.bench.internal", sanitize.RULE_HOST),
            ("10.0.0.9", sanitize.RULE_ADDRESS),
            ("aaaa0000bbbb1111cccc2222dddd3333", sanitize.RULE_SERIAL),
            ("ssh -o BatchMode=yes somewhere", sanitize.RULE_SSH),
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, [item.rule for item in line_findings(line)])

    def test_a_private_name_that_survived_is_a_finding(self):
        found = line_findings("held by ada", BENCH)
        self.assertEqual([item.rule for item in found], [sanitize.RULE_NAME])

    def test_a_long_run_of_digits_is_not_read_as_a_serial(self):
        self.assertEqual(line_findings("counter 111122223333444455556666"), [])

    def test_a_hex_piece_of_a_serial_is_read_as_one(self):
        found = line_findings("a1b2c3d4e5f6face7890abcdef1234")
        self.assertEqual([item.rule for item in found], [sanitize.RULE_SERIAL])

    def test_a_private_bundle_keeps_its_serials_and_every_other_rule(self):
        private = Private(keep_serials=True)
        self.assertEqual(line_findings("serial aaaa0000bbbb1111cccc2222dddd3333",
                                       private), [])
        self.assertIn(
            sanitize.RULE_HOME,
            [item.rule for item in line_findings("/home/ada/x", private)],
        )

    def test_a_finding_names_where_and_which_line(self):
        found = findings("clean\n/srv/bench\n", where="runs/r1/report.md")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].line, 2)
        self.assertEqual(found[0].where, "runs/r1/report.md")
        self.assertIn("runs/r1/report.md:2:", found[0].describe())

    def test_require_safe_refuses_and_names_what_it_found(self):
        with self.assertRaises(Unsafe) as refused:
            require_safe("held by ada on bench-linux-7", BENCH, where="summary")
        message = str(refused.exception)
        self.assertIn("summary", message)
        self.assertIn("--strip-path", message)

    def test_require_safe_returns_text_it_allows(self):
        self.assertEqual(require_safe("suite release passed"),
                         "suite release passed")

    def test_a_refusal_naming_many_findings_stays_bounded(self):
        text = "\n".join(f"/srv/bench/run-{number}" for number in range(100))
        with self.assertRaises(Unsafe) as refused:
            require_safe(text)
        self.assertIn("and 80 more", str(refused.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
