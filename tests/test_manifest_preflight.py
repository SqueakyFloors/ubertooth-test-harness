import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.events import RunEvent
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.manifest import (
    GitRevisionResolver,
    ManifestError,
    SourceRevision,
    build_run_manifest,
)
from ubertooth_harness.patterns import (
    HOST_VERSION_TIMEOUT_S,
    compile_info_pattern,
    firmware_version_pattern,
    revision_pattern,
)
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.preflight import (
    HOST_VERSION_ARGV,
    HOST_VERSION_FALLBACK_ARGV,
    HOST_VERSION_LABEL,
    PreflightCheck,
    PreflightError,
    PreflightPolicy,
    host_version_patterns,
)
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.sequencer import NamedTestSequencer
from ubertooth_harness.topology import PowerIsolationAction


HARDWARE = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {Ubertooth: UT_Linux0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: abcdef0123456789}
"""


class CollectSink:
    def __init__(self):
        self.events: list[RunEvent] = []

    def emit(self, event):
        self.events.append(event)


class FakeExecutor:
    def __init__(self, revision, *, wrong_serial=False):
        self.revision = revision
        self.wrong_serial = wrong_serial
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            output = (
                f"Host tools version: libubertooth 1.1 ({self.revision}), "
                "libbtbb 1.0 (git-btbb)\n"
            )
        elif command[-1] == "-v":
            output = f"Firmware version: {self.revision} (API:1.07)\n"
        elif command[-1] == "-V":
            output = f"ubertooth {self.revision} (builder@host) timestamp\n"
        elif command[-1] == "-s":
            serial = "wrong" if self.wrong_serial else "abcdef0123456789"
            output = f"Serial No: {serial}\n"
        elif command == ("ubertooth-util", "-h"):
            output = "Usage: ubertooth-util\n"
        else:
            return CommandResult(command, 127, "", "unsupported", 0.01, False)
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


def make_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(path), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "initial",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_invocations():
    identity = MappingProxyType({"serial": "abcdef0123456789"})
    return (
        TestInvocation(
            "001-Help smoke",
            "Help smoke",
            "Host_Linux",
            MappingProxyType({"hosts": ("Host_Linux",)}),
            MappingProxyType({}),
        ),
        TestInvocation(
            "002-Firmware version",
            "Firmware version",
            "Host_Linux",
            MappingProxyType({"hosts": ("Host_Linux",), "Ubertooth": "default"}),
            MappingProxyType({"Ubertooth": "UT_Linux0"}),
            MappingProxyType({"Ubertooth": identity}),
        ),
        TestInvocation(
            "003-Board ID",
            "Board ID",
            "Host_Linux",
            MappingProxyType({"hosts": ("Host_Linux",), "Ubertooth": "default"}),
            MappingProxyType({"Ubertooth": "UT_Linux0"}),
            MappingProxyType({"Ubertooth": identity}),
        ),
    )


class ManifestTests(unittest.TestCase):

    def test_resolves_branch_tag_or_commit_to_immutable_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            commit = make_git_repo(source)
            subprocess.run(
                ["git", "-C", str(source), "tag", "release-test"], check=True
            )
            subprocess.run(
                [
                    "git", "-C", str(source), "remote", "add", "origin",
                    "https://user:secret@example.invalid/ubertooth.git?token=value",
                ],
                check=True,
            )

            branch = GitRevisionResolver().resolve("Ubertooth", source, "HEAD")
            tag = GitRevisionResolver().resolve(
                "Ubertooth", source, "release-test"
            )
            explicit = GitRevisionResolver().resolve(
                "Ubertooth", source, commit
            )

            self.assertEqual(commit, branch.commit)
            self.assertEqual(commit, tag.commit)
            self.assertEqual(commit, explicit.commit)
            self.assertEqual(
                "https://example.invalid/ubertooth.git", branch.repository
            )
            self.assertNotIn("secret", branch.repository)
            self.assertEqual(f"git-{branch.short_commit}", branch.expected_revision)
            with self.assertRaises(Exception):
                branch.commit = "other"
            self.assertNotIn("ref_name", branch.to_dict())
            named = GitRevisionResolver().resolve(
                "Ubertooth", source, commit, "main"
            )
            self.assertEqual(
                (commit, commit, "main"),
                (named.requested_ref, named.commit, named.to_dict()["ref_name"]),
            )
            with self.assertRaisesRegex(ManifestError, "single line"):
                GitRevisionResolver().resolve("Ubertooth", source, commit, "a\nb")

    def test_rejects_dirty_or_unknown_source_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            make_git_repo(source)
            (source / "tracked.txt").write_text("dirty\n")

            with self.assertRaisesRegex(ManifestError, "dirty"):
                GitRevisionResolver().resolve("Ubertooth", source, "HEAD")
            subprocess.run(
                ["git", "-C", str(source), "checkout", "--", "tracked.txt"],
                check=True,
            )
            (source / "untracked.c").write_text("untracked\n")
            with self.assertRaisesRegex(ManifestError, "dirty"):
                GitRevisionResolver().resolve("Ubertooth", source, "HEAD")
            (source / "untracked.c").unlink()
            with self.assertRaises(ManifestError):
                GitRevisionResolver().resolve("Ubertooth", source, "missing-ref")

    def test_manifest_snapshots_file_hashes_and_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source"
            make_git_repo(source_path)
            source = GitRevisionResolver().resolve(
                "Ubertooth", source_path, "HEAD"
            )
            plan = root / "plan.yaml"
            hardware = root / "hardware.yaml"
            plan.write_text("plan contents\n")
            hardware.write_text("hardware contents\n")
            original = make_invocations()[0]
            invocation = TestInvocation(
                original.invocation_id,
                original.test_name,
                original.host,
                original.arguments,
                original.resources,
                original.resource_identities,
                (
                    PowerIsolationAction(
                        "UT_Linux1", "Hub_Linux0", 1, False
                    ),
                ),
            )

            manifest = build_run_manifest(
                run_id="manifest-run",
                plan_name="test plan",
                suite="fast",
                host="Host_Linux",
                plan_path=plan,
                hardware_path=hardware,
                sources=(source,),
                invocations=(invocation,),
                created_at="2026-10-22T00:00:00+00:00",
            )
            original_hash = manifest.inputs[0].sha256
            plan.write_text("changed\n")

            self.assertEqual(original_hash, manifest.inputs[0].sha256)
            self.assertNotEqual(original_hash, manifest.hash_file(plan))
            with self.assertRaisesRegex(ManifestError, "changed after snapshot"):
                manifest.verify_files()
            data = manifest.to_dict()
            self.assertEqual(source.commit, data["sources"][0]["commit"])
            self.assertEqual("001-Help smoke", data["invocations"][0]["id"])
            self.assertEqual(
                {
                    "resource": "UT_Linux1",
                    "controller": "Hub_Linux0",
                    "port": 1,
                    "power_enabled": False,
                },
                data["invocations"][0]["power_isolation"][0],
            )


def prepare_preflight(directory):
    root = Path(directory)
    source_path = root / "source"
    make_git_repo(source_path)
    source = GitRevisionResolver().resolve("Ubertooth", source_path, "HEAD")
    hardware_path = root / "hardware.yaml"
    hardware_path.write_text(HARDWARE)
    plan_path = root / "plan.yaml"
    plan_path.write_text("plan\n")
    registry = FileResourceRegistry.load(hardware_path)
    invocations = make_invocations()
    manifest = build_run_manifest(
        run_id="preflight-run",
        plan_name="test plan",
        suite="fast",
        host="Host_Linux",
        plan_path=plan_path,
        hardware_path=hardware_path,
        sources=(source,),
        invocations=invocations,
        created_at="2026-10-22T00:00:00+00:00",
    )
    checks = PreflightPolicy(registry).compile(invocations, manifest)
    return root, source, manifest, checks


class PreflightTests(unittest.TestCase):

    def test_policy_deduplicates_host_and_device_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, source, _manifest, checks = prepare_preflight(directory)

            self.assertEqual(4, len(checks))
            self.assertEqual(
                ("Host version", "Firmware version", "Firmware compile info",
                 "Device serial"),
                tuple(check.name for check in checks),
            )
            self.assertEqual(
                ("ubertooth-util", "-U", "abcdef0123456789", "-v"),
                checks[1].argv,
            )
            self.assertEqual(source.expected_revision, checks[1].expected_revision)

    def flash_invocation(self, invocation_id="004-Flash reliability"):
        base = make_invocations()[1]
        return TestInvocation(
            invocation_id, "Flash reliability", base.host,
            MappingProxyType({"hosts": ("Host_Linux",), "Ubertooth": "default"}),
            base.resources, base.resource_identities,
        )

    def test_firmware_writing_test_skips_revision_checks_on_its_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = (make_invocations()[0], self.flash_invocation())
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            checks = PreflightPolicy(registry).compile(
                invocations, manifest.with_invocations(invocations)
            )

            self.assertEqual(("Host version", "Device serial"), tuple(c.name for c in checks))

    def test_tests_before_a_firmware_write_still_need_the_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = make_invocations() + (self.flash_invocation(),)
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            checks = PreflightPolicy(registry).compile(
                invocations, manifest.with_invocations(invocations)
            )

            self.assertIn("Firmware version", [c.name for c in checks])

    def test_two_firmware_writing_tests_may_share_a_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = (
                make_invocations()[1],
                self.flash_invocation("004-Flash reliability"),
                self.flash_invocation("005-Flash reliability"),
            )
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            PreflightPolicy(registry).compile(
                invocations, manifest.with_invocations(invocations)
            )

    def test_no_test_may_follow_a_firmware_write_on_the_same_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            invocations = (self.flash_invocation("002-Flash reliability"), make_invocations()[2])
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            with self.assertRaisesRegex(PreflightError, "last test on UT_Linux0"):
                PreflightPolicy(registry).compile(
                    invocations, manifest.with_invocations(invocations)
                )

    def test_policy_rejects_binding_drift_from_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = prepare_preflight(directory)
            changed = list(make_invocations())
            original = changed[1]
            changed[1] = TestInvocation(
                original.invocation_id,
                original.test_name,
                original.host,
                original.arguments,
                MappingProxyType({"Ubertooth": "UT_Other"}),
                original.resource_identities,
            )

            with self.assertRaisesRegex(PreflightError, "do not match"):
                PreflightPolicy(
                    FileResourceRegistry.load(root / "hardware.yaml")
                ).compile(tuple(changed), manifest)

    def test_preflight_runs_before_tests_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(directory)
            sink = CollectSink()
            executor = FakeExecutor(source.expected_revision)

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                make_invocations()[:1],
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(make_invocations()[:1]),
                preflight_checks=checks[:1],
            )

            self.assertTrue(summary.passed)
            event_types = [event.event_type for event in sink.events]
            self.assertLess(
                event_types.index("preflight_finished"),
                event_types.index("test_started"),
            )
            self.assertEqual(1, summary.completed_preflight_checks)
            first = [
                json.loads(line)
                for line in (root / "run" / "run.jsonl").read_text().splitlines()
            ]
            persisted = next(e for e in first if e["event_type"] == "manifest")
            self.assertEqual(
                source.commit, persisted["details"]["manifest"]["sources"][0]["commit"]
            )

    def test_preflight_failure_prevents_test_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(directory)
            sink = CollectSink()
            executor = FakeExecutor(source.expected_revision, wrong_serial=True)

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                make_invocations()[1:2],
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(make_invocations()[1:2]),
                preflight_checks=checks,
            )

            self.assertFalse(summary.passed)
            self.assertIn("serial", summary.failure_reason.lower())
            self.assertNotIn("test_started", [event.event_type for event in sink.events])
            self.assertEqual(0, summary.completed_tests)

    def test_sequencer_rejects_missing_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "preflight"):
                NamedTestSequencer(FakeExecutor("git-test")).run(
                    make_invocations()[:1],
                    Path(directory),
                    run_id="missing-preflight",
                    manifest=None,
                    preflight_checks=(),
                )


class HostVersionCheckTests(unittest.TestCase):
    """The host tools' version, in the two shapes the fork's tools print it."""

    # ubertooth-util -V, the one version answer (fork #813).
    V_ANSWER = (
        "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
        "libbtbb loaded from: /usr/local/lib/libbtbb.so.1\n"
        "Firmware version: bluetooth_rxtx git-test (API:1.35)\n"
        "Firmware build: ubertooth git-test (builder@host) Mon Oct 27 11:22:33 2025\n"
    )
    # ubertooth-util -H on a fork commit before it: the same two host lines,
    # printed by the same function, and no board read.
    H_ANSWER = (
        "Host tools version: libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
        "libbtbb loaded from: /usr/local/lib/libbtbb.so.1\n"
    )
    # -H before the fork labelled its version lines (fork 201c3c49): the same
    # two versions, on a line of their own.
    UNLABELLED_H_ANSWER = "libubertooth 1.1 (git-test), libbtbb 1.0 (git-btbb)\n"
    # What -V was on those commits: the board's build string, nothing about
    # the host, and with no board attached it fails.
    OLD_V_ANSWER = "ubertooth git-test (builder@host) Mon Oct 27 11:22:33 2025\n"

    @staticmethod
    def source(name, short_commit):
        return SourceRevision(
            name=name,
            source_path=f"/src/{name}",
            repository="example",
            requested_ref="HEAD",
            commit=short_commit.removeprefix("git-").ljust(40, "0"),
            short_commit=short_commit.removeprefix("git-"),
        )

    def check(self):
        return PreflightCheck(
            check_id="preflight-001-host-version",
            name="Host version",
            host="Host_Linux",
            resource_alias=None,
            resource_identity=MappingProxyType({}),
            argv=HOST_VERSION_ARGV,
            expected_patterns=host_version_patterns(
                self.source("Ubertooth", "git-test"), self.source("libbtbb", "git-btbb")
            ),
            expected_revision="git-test",
            fallback_argv=HOST_VERSION_FALLBACK_ARGV,
            fallback_when_missing=re.escape(HOST_VERSION_LABEL),
            accepted_returncodes=(0, 1),
        )

    @staticmethod
    def result(output, returncode=0):
        return CommandResult(HOST_VERSION_ARGV, returncode, output, "", 0.01, False)

    def test_the_run_asks_V_and_keeps_H_as_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, _source, _manifest, checks = prepare_preflight(directory)

            check = checks[0]
            self.assertEqual(("ubertooth-util", "-V"), check.argv)
            self.assertEqual(("ubertooth-util", "-H"), check.fallback_argv)
            # The rest of the wiring, which only a real run exercises: with
            # no marker the fallback never runs, and without the tolerated
            # status and the longer deadline a board the run does not even
            # use fails preflight.
            self.assertTrue(check.needs_fallback(
                CommandResult(check.argv, 1, "", "error: unrecognized option\n",
                              0.01, False)
            ))
            self.assertEqual((0, 1), check.accepted_returncodes)
            self.assertEqual(HOST_VERSION_TIMEOUT_S, check.timeout_s)

    def test_every_shape_of_the_answer_passes(self):
        check = self.check()
        for output in (self.V_ANSWER, self.H_ANSWER, self.UNLABELLED_H_ANSWER):
            with self.subTest(output=output.splitlines()[0]):
                self.assertEqual("", check.result_failure(self.result(output)))

    def test_a_labelled_answer_does_not_ask_the_older_option(self):
        # The unlabelled line is left out: it only ever arrives as the
        # fallback's own answer, which is not asked again.
        check = self.check()
        for output in (self.V_ANSWER, self.H_ANSWER):
            with self.subTest(output=output.splitlines()[0]):
                self.assertFalse(check.needs_fallback(self.result(output)))

    def test_an_answer_about_no_host_asks_the_older_option(self):
        result = self.result(self.OLD_V_ANSWER, returncode=1)

        self.assertTrue(self.check().needs_fallback(result))

    def test_a_command_killed_at_its_deadline_is_not_asked_again(self):
        # The older `-V` reads a board, so it can hang on one; answering the
        # host version on `-H` afterwards would hide that.
        timed_out = replace(self.result("", returncode=124), timed_out=True)

        check = self.check()
        self.assertFalse(check.needs_fallback(timed_out))
        self.assertIn("timed out", check.result_failure(timed_out))

    def test_a_wrong_revision_is_reported_rather_than_asked_again(self):
        other = self.V_ANSWER.replace("libubertooth 1.1 (git-test)",
                                      "libubertooth 1.1 (git-other)")
        result = self.result(other)

        self.assertIn("identity mismatch", self.check().result_failure(result))
        self.assertFalse(self.check().needs_fallback(result))

    def test_a_board_that_cannot_be_read_still_passes(self):
        # -V reads every attached board after the host lines and exits 1 if
        # one cannot be read; that board may not even be in this run.
        busy = self.V_ANSWER.replace(
            "Firmware version: bluetooth_rxtx git-test (API:1.35)",
            "Firmware version: unavailable, in use by another program",
        )

        self.assertEqual("", self.check().result_failure(self.result(busy, returncode=1)))

    def test_the_firmware_lines_cannot_stand_in_for_the_host_line(self):
        # "Firmware build: ubertooth git-test (...)" names the same revision,
        # so the patterns are anchored to the host tools' own line.
        firmware_only = self.V_ANSWER.split("libbtbb loaded from", 1)[1]

        self.assertIn("identity mismatch",
                      self.check().result_failure(self.result(firmware_only)))

    def test_a_modified_source_marker_fails(self):
        marked = self.V_ANSWER.replace("git-test)", "git-test*)")

        self.assertIn("identity mismatch",
                      self.check().result_failure(self.result(marked)))


class RevisionAbbreviationTests(unittest.TestCase):
    """The same commit stamped with hashes of different lengths still matches."""

    HASH = "1c7f06d621579382c2fedc64fda4e3450da8c965"

    @staticmethod
    def matches(expected, reported, image="bluetooth_rxtx ") -> bool:
        output = f"Firmware version: {image}{reported} (API:1.35)"
        return re.search(firmware_version_pattern(expected), output) is not None

    def test_firmware_that_names_its_image_matches(self):
        # Firmware with an info block reports the image it came from; older
        # firmware and release builds report only the revision.
        self.assertTrue(self.matches("git-1c7f06d6", "git-1c7f06d6"))
        self.assertTrue(self.matches("git-1c7f06d6", "git-1c7f06d6", image=""))
        self.assertTrue(self.matches("2020-12-R1", "2020-12-R1", image=""))

    def test_compile_info_accepts_the_image_name_before_the_build_string(self):
        self.assertRegex(
            "bluetooth_rxtx: ubertooth git-1c7f06d6 (2026-10-27 12:00:00)",
            compile_info_pattern("git-1c7f06d"),
        )
        self.assertRegex(
            "ubertooth git-1c7f06d6 (2026-10-27 12:00:00)",
            compile_info_pattern("git-1c7f06d"),
        )

    def test_board_reports_a_longer_abbreviation_than_expected(self):
        # The run that found this: the board said git-1c7f06d6, the host
        # tools git-1c7f06d, and preflight called it an identity mismatch.
        self.assertTrue(self.matches("git-1c7f06d", "git-1c7f06d6"))
        self.assertTrue(self.matches("git-1c7f06d", "git-" + self.HASH))

    def test_board_reports_a_shorter_abbreviation_than_expected(self):
        self.assertTrue(self.matches("git-1c7f06d6", "git-1c7f06d"))
        self.assertTrue(self.matches("git-" + self.HASH, "git-1c7f06d"))

    def test_equal_abbreviations_match(self):
        self.assertTrue(self.matches("git-1c7f06d", "git-1c7f06d"))

    def test_a_different_commit_does_not_match(self):
        self.assertFalse(self.matches("git-1c7f06d", "git-2fd0d39"))
        self.assertFalse(self.matches("git-1c7f06d", "git-1c7f06e"))

    def test_a_hash_shorter_than_git_ever_abbreviates_does_not_match(self):
        self.assertFalse(self.matches("git-1c7f06d621", "git-1c7f0"))

    def test_a_modified_source_marker_does_not_match(self):
        self.assertFalse(self.matches("git-1c7f06d", "git-1c7f06d*"))
        self.assertFalse(self.matches("git-1c7f06d", "git-1c7f06d6*"))
        self.assertFalse(self.matches("git-1c7f06d6", "git-1c7f06d*"))

    def test_a_revision_that_is_not_a_hash_is_matched_exactly(self):
        self.assertEqual("git\\-test", revision_pattern("git-test"))
        self.assertTrue(self.matches("git-test", "git-test"))
        self.assertFalse(self.matches("git-test", "git-tested"))

    def test_host_version_patterns_accept_either_abbreviation(self):
        source = SourceRevision(
            name="Ubertooth",
            source_path="/src",
            repository="example",
            requested_ref="HEAD",
            commit=self.HASH,
            short_commit="1c7f06d",
        )
        libbtbb = SourceRevision(
            name="libbtbb",
            source_path="/btbb",
            repository="example",
            requested_ref="HEAD",
            commit="2fd0d391d99e9b0620146f63f439c1ec74f79861",
            short_commit="2fd0d3912",
        )
        output = "Host tools version: libubertooth 1.1 (git-1c7f06d621), libbtbb 1.1 (git-2fd0d39)\n"

        for pattern in host_version_patterns(source, libbtbb):
            self.assertRegex(output, pattern)

    def test_preflight_check_accepts_a_longer_abbreviation_on_the_board(self):
        check = PreflightCheck(
            check_id="preflight-002-firmware-version",
            name="Firmware version",
            host="Host_Linux",
            resource_alias="UT_Linux0",
            resource_identity=MappingProxyType({"serial": "abcdef0123456789"}),
            argv=("ubertooth-util", "-U", "abcdef0123456789", "-v"),
            expected_patterns=(firmware_version_pattern("git-1c7f06d"),),
            expected_revision="git-1c7f06d",
        )
        result = CommandResult(
            argv=check.argv,
            returncode=0,
            stdout="Firmware version: bluetooth_rxtx git-1c7f06d6 (API:1.35)\n",
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )

        self.assertEqual("", check.result_failure(result))

    def test_a_mismatch_reason_says_what_was_expected(self):
        check = PreflightCheck(
            check_id="preflight-002-firmware-version",
            name="Firmware version",
            host="Host_Linux",
            resource_alias="UT_Linux0",
            resource_identity=MappingProxyType({}),
            argv=("ubertooth-util", "-v"),
            expected_patterns=("Firmware version:\\s+git\\-other",),
            expected_revision="git-other",
        )
        result = CommandResult(
            argv=check.argv,
            returncode=0,
            stdout="Firmware version: bluetooth_rxtx git-1c7f06d6 (API:1.35)\n",
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )

        reason = check.result_failure(result)

        # A readable revision is named along with the one the tools expect.
        self.assertIn("UT_Linux0 runs git-1c7f06d6", reason)
        self.assertIn("git-other", reason)

        # Output with no revision in it falls back to naming the pattern.
        unreadable = CommandResult(
            argv=check.argv,
            returncode=0,
            stdout="Firmware version: unknown\n",
            stderr="",
            duration_s=0.05,
            timed_out=False,
        )

        reason = check.result_failure(unreadable)

        self.assertIn("identity mismatch for UT_Linux0", reason)
        self.assertIn("git\\-other", reason)


if __name__ == "__main__":
    unittest.main()
