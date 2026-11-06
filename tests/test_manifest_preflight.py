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
from ubertooth_harness.outputfiles import output_of
from ubertooth_harness.preflight import (
    HOST_VERSION_ARGV,
    HOST_VERSION_FALLBACK_ARGV,
    HOST_VERSION_LABEL,
    LIBBTBB_BUILT_ORIGIN,
    LIBBTBB_RESOLVED_ORIGIN,
    LibbtbbIdentity,
    PreflightCheck,
    PreflightError,
    PreflightPolicy,
    firmware_identity_patterns,
    host_version_patterns,
    libbtbb_identity,
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
    def __init__(self, revision, *, wrong_serial=False, libbtbb="git-btbb"):
        self.revision = revision
        self.wrong_serial = wrong_serial
        self.libbtbb = libbtbb
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-V"):
            output = (
                f"Host tools version: libubertooth 1.1 ({self.revision}), "
                f"libbtbb 1.3 ({self.libbtbb})\n"
            )
        elif command[-1] == "-v":
            output = f"Firmware version: {self.revision} (API:1.07)\n"
        elif command[-1] == "-V":
            # The whole answer, which is what -V prints for one board since
            # fork f898fe5; the compile-info check reads its last line.
            output = (
                f"Host tools version: libubertooth 1.1 ({self.revision}), "
                f"libbtbb 1.3 ({self.libbtbb})\n"
                "libbtbb loaded from: /p/lib/libbtbb.so.1\n"
                f"Firmware version: {self.revision} (API:1.07)\n"
                f"Firmware build: ubertooth {self.revision} (builder@host) timestamp\n"
            )
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


def make_git_repo(path: Path, *, install_sh: str | None = None) -> str:
    """A one-commit checkout, with an ``install.sh`` when one is wanted.

    Preflight reads that file at the resolved Ubertooth commit for the
    libbtbb the run's tools must be linked against (#294), so a test about
    that check gives the repository one.
    """
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "tracked.txt").write_text("one\n")
    tracked = ["tracked.txt"]
    if install_sh is not None:
        (path / "install.sh").write_text(install_sh)
        tracked.append("install.sh")
    subprocess.run(["git", "-C", str(path), "add", *tracked], check=True)
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


def prepare_preflight(directory, *, install_sh=None, libbtbb=None, build=None):
    """Manifest and preflight checks for one host and its board.

    ``libbtbb`` resolves a libbtbb checkout beside the Ubertooth one, as a
    run given --libbtbb-source does, and ``build`` fills the manifest's build
    record, as a run that built does.
    """
    root = Path(directory)
    source_path = root / "source"
    make_git_repo(source_path, install_sh=install_sh)
    source = GitRevisionResolver().resolve("Ubertooth", source_path, "HEAD")
    sources = (source,)
    if libbtbb is not None:
        libbtbb_path = root / "libbtbb"
        make_git_repo(libbtbb_path)
        sources += (
            GitRevisionResolver().resolve("libbtbb", libbtbb_path, "HEAD"),
        )
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
        sources=sources,
        invocations=invocations,
        created_at="2026-10-22T00:00:00+00:00",
        build=build,
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

    def test_firmware_check_accepts_the_application_image_name(self):
        # Fork 730b4f5 (#523): -v names the image ahead of the revision.
        with tempfile.TemporaryDirectory() as directory:
            _root, source, _manifest, checks = prepare_preflight(directory)
            check = next(c for c in checks if c.name == "Firmware version")
            revision = source.expected_revision

            def failure(text):
                return check.result_failure(
                    CommandResult(check.argv, 0, text, "", 0.01, False))

            self.assertFalse(failure(f"Firmware version: {revision} (API:1.35)\n"))
            self.assertFalse(
                failure(f"Firmware version: bluetooth_rxtx {revision} (API:1.35)\n"))
            # Another image at the run's own commit: the revisions name no
            # difference, so the reason names the image instead, because the
            # other tools do not run on it.
            btbr = failure(f"Firmware version: btbr {revision} (API:1.35)\n")
            self.assertIn(f"runs the btbr image at {revision}, not "
                          "bluetooth_rxtx", btbr)
            self.assertNotIn("identity mismatch", btbr)
            # A line no version pattern reads at all says what had to match.
            self.assertIn("identity mismatch", failure(
                f"Firmware version: bluetooth_rxtx git-other {revision} "
                "(API:1.35)\n"))
            # A different revision is named as one, with how to fix it.
            other_revision = failure(
                f"Firmware version: bluetooth_rxtx {revision}x (API:1.35)\n")
            self.assertIn(f"runs {revision}x, the tools are {revision}",
                          other_revision)

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


class HostToolsLibbtbbTests(unittest.TestCase):
    """Which libbtbb the installed tools must be linked against (#294).

    A run that builds nothing used to accept whatever libbtbb the host's
    tools happened to load, and reported a pass while measuring a different
    decoder from the one it named. The expectation now comes from the
    ``LIBBTBB_REF`` the Ubertooth commit under test sets in its own
    ``install.sh``.
    """

    PIN = "52b2afcb0c4b8b03412dac759aad88debdfb18e2"
    OTHER = "0176769fb4b5b1b4a7d07fd1dbe8e4b0ef4d11c2"

    def host_check(self, install_sh=None, **resolved):
        with tempfile.TemporaryDirectory() as directory:
            _root, _source, _manifest, checks = prepare_preflight(
                directory, install_sh=install_sh, **resolved
            )
        self.assertEqual("Host version", checks[0].name)
        return checks[0]

    @staticmethod
    def answer(check, *, ubertooth=None, libbtbb="git-52b2afc"):
        """What `ubertooth-util -V` prints for those two versions.

        The Ubertooth revision defaults to the one the check resolved, so
        only the libbtbb half is under test.
        """
        line = f"Host tools version: libubertooth 1.1 ({ubertooth or check.expected_revision})"
        if libbtbb is not None:
            line += f", libbtbb 1.3 ({libbtbb})"
        return CommandResult(
            HOST_VERSION_ARGV, 0,
            f"{line}\nlibbtbb loaded from: /usr/local/lib/libbtbb.so.1\n",
            "", 0.01, False,
        )

    def test_a_run_that_builds_nothing_holds_the_tools_to_the_commits_pin(self):
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')

        self.assertEqual(f"git-{self.PIN}", check.libbtbb.expected_revision)
        self.assertIn("install.sh", check.libbtbb.origin)
        # The library stamps its own short hash, so the pin's abbreviation
        # and its full commit name the same thing.
        self.assertEqual("", check.result_failure(self.answer(check)))
        self.assertEqual(
            "", check.result_failure(self.answer(check, libbtbb=f"git-{self.PIN}"))
        )

    def test_another_libbtbb_fails_naming_both_and_how_to_fix_it(self):
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')

        reason = check.result_failure(self.answer(check, libbtbb="git-0176769"))
        self.assertIn("report libbtbb git-0176769", reason)
        self.assertIn(self.PIN, reason)
        self.assertIn("--libbtbb-source", reason)

    def test_a_library_that_cannot_name_a_commit_fails_too(self):
        # A libbtbb built from a tarball, or from a modified checkout, reports
        # a release or a `*` instead: neither says it is the pinned commit.
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')

        for release in ("2020-12-R1", "git-52b2afc*"):
            with self.subTest(release=release):
                reason = check.result_failure(self.answer(check, libbtbb=release))
                self.assertIn(f"report libbtbb {release}", reason)
        # Tools that print no libbtbb version at all are reported as such,
        # not as a mismatch with something they never said.
        self.assertIn(
            "no libbtbb version at all",
            check.result_failure(self.answer(check, libbtbb=None)),
        )

    def test_the_run_records_what_it_required_and_what_it_found(self):
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')
        result = self.answer(check)
        # The run reads a command's output through the CommandOutput it
        # carries, which is where a check's record lands (outputfiles.py).
        result = replace(result, output=output_of(result))

        self.assertEqual("", check.result_failure(result))
        (recorded,) = [item for item in result.output.checks if "libbtbb" in item.name]
        self.assertEqual(("expected", True), (recorded.kind, recorded.found))
        self.assertIn("git-52b2afc", recorded.matched)
        self.assertIn(f"git-{self.PIN}, the LIBBTBB_REF", check.libbtbb.describe())

    def test_an_ubertooth_commit_that_pins_nothing_requires_nothing(self):
        # Fork commits from before the pin landed claim no libbtbb, so there
        # is nothing to hold the installed library to. The run says so rather
        # than staying silent: that is the condition #294 is about.
        for install_sh in (None, "#!/bin/sh\necho hi\n"):
            with self.subTest(install_sh=install_sh):
                check = self.host_check(install_sh)
                self.assertFalse(check.libbtbb.required)
                self.assertEqual(
                    "", check.result_failure(self.answer(check, libbtbb="git-other"))
                )
                (reason,) = check.libbtbb.to_details().values()
                self.assertIn("sets no LIBBTBB_REF", reason)
                self.assertEqual(
                    ["libbtbb_not_checked"], list(check.libbtbb.to_details())
                )

    def test_a_run_that_required_nothing_says_so_in_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(directory)
            invocations = make_invocations()[:1]

            summary = NamedTestSequencer(
                FakeExecutor(source.expected_revision), event_sink=CollectSink()
            ).run(
                invocations,
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(invocations),
                preflight_checks=checks[:1],
            )

            self.assertTrue(summary.passed)
            report = (root / "run" / "report.md").read_text()
            self.assertIn(
                "| libbtbb in the tools | **not checked**: ", report
            )
            self.assertIn("sets no LIBBTBB_REF", report)

    def test_a_failure_on_the_ubertooth_revision_does_not_blame_the_library(self):
        # The check stops at the first pattern it cannot match, so a run that
        # failed on the tools' own revision never compared the library. The
        # report must not report that as a libbtbb failure.
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(
                directory, install_sh=f'LIBBTBB_REF="{self.PIN}"\n'
            )
            invocations = make_invocations()[:1]

            summary = NamedTestSequencer(
                FakeExecutor("git-0000000", libbtbb="git-52b2afc"),
                event_sink=CollectSink(),
            ).run(
                invocations,
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(invocations),
                preflight_checks=checks[:1],
            )

            self.assertFalse(summary.passed)
            self.assertIn("identity mismatch", summary.failure_reason)
            report = (root / "run" / "report.md").read_text()
            self.assertIn("| libbtbb in the tools | **not reached**: ", report)
            self.assertNotIn("| libbtbb in the tools | **failed**", report)

    def test_the_ubertooth_revision_is_reported_before_the_libbtbb_one(self):
        # Tools from another commit are the bigger problem, and the reason
        # line is often all an operator sees.
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')

        reason = check.result_failure(
            self.answer(check, ubertooth="git-0000000", libbtbb="git-0176769")
        )
        self.assertIn("identity mismatch", reason)

    def test_a_pin_that_cannot_be_compared_refuses_the_run(self):
        # Nothing the tools report can be compared with a name that moves or
        # with a pin built from a shell variable, so the run stops here
        # rather than measure an unverified library.
        for pin, problem in (
            ("v1.0", "not a commit"),
            ("${REF}", "not a literal commit"),
        ):
            with self.subTest(pin=pin):
                with self.assertRaises(PreflightError) as raised:
                    self.host_check(f'LIBBTBB_REF="{pin}"\n')
                message = str(raised.exception)
                self.assertIn("cannot be checked", message)
                self.assertIn(problem, message)
                self.assertIn("--libbtbb-ref", message)

    def test_a_run_records_the_library_it_required_and_the_report_shows_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(
                directory, install_sh=f'LIBBTBB_REF="{self.PIN}"\n'
            )
            sink = CollectSink()
            invocations = make_invocations()[:1]
            executor = FakeExecutor(source.expected_revision, libbtbb="git-52b2afc")

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                invocations,
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(invocations),
                preflight_checks=checks[:1],
            )

            self.assertTrue(summary.passed)
            started = next(
                event for event in sink.events
                if event.event_type == "check_started"
            )
            self.assertEqual(
                f"git-{self.PIN}, the LIBBTBB_REF install.sh sets at "
                f"Ubertooth {source.short_commit}",
                started.details["expected_libbtbb"],
            )
            report = (root / "run" / "report.md").read_text()
            self.assertIn(f"| libbtbb in the tools | git-{self.PIN}, ", report)
            self.assertIn("(verified) |", report)

    def test_a_run_whose_tools_load_another_library_runs_no_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = prepare_preflight(
                directory, install_sh=f'LIBBTBB_REF="{self.PIN}"\n'
            )
            sink = CollectSink()
            invocations = make_invocations()[:1]
            # What the lab host did: tools of the right Ubertooth commit,
            # linked against a libbtbb several commits behind the pin.
            executor = FakeExecutor(source.expected_revision, libbtbb="git-0176769")

            summary = NamedTestSequencer(executor, event_sink=sink).run(
                invocations,
                root / "run",
                run_id="preflight-run",
                manifest=manifest.with_invocations(invocations),
                preflight_checks=checks[:1],
            )

            self.assertFalse(summary.passed)
            self.assertIn("libbtbb git-0176769", summary.failure_reason)
            self.assertNotIn(
                "test_started", [event.event_type for event in sink.events]
            )
            report = (root / "run" / "report.md").read_text()
            self.assertIn("| libbtbb in the tools | **failed**: ", report)

    def test_a_run_that_builds_libbtbb_holds_the_tools_to_that_one(self):
        libbtbb = SourceRevision(
            name="libbtbb", source_path="/btbb", repository="example",
            requested_ref="HEAD", commit=self.OTHER, short_commit=self.OTHER[:7],
        )
        ubertooth = SourceRevision(
            name="Ubertooth", source_path="/src", repository="example",
            requested_ref="HEAD", commit="a" * 40, short_commit="aaaaaaa",
        )

        # The pin is not read at all: the run built a libbtbb of its own, and
        # whether that one is the fork's pin is the libbtbb_pin record's
        # question (#85), not preflight's. The full commit is what is
        # required, since a short hash would also accept a longer one that
        # merely starts with it.
        identity = libbtbb_identity(ubertooth, libbtbb, built=True)
        self.assertEqual(f"git-{self.OTHER}", identity.expected_revision)
        self.assertEqual(LIBBTBB_BUILT_ORIGIN, identity.origin)
        # A run that resolved that libbtbb without building it says so
        # instead, which is what a deployed runner's unticked-Build run does:
        # it passes --libbtbb-source and --libbtbb-ref to every run.
        resolved = libbtbb_identity(ubertooth, libbtbb)
        self.assertEqual(LIBBTBB_RESOLVED_ORIGIN, resolved.origin)
        self.assertNotIn("build", resolved.fix)

    def test_a_resolved_libbtbb_wins_over_the_commits_pin_in_a_whole_run(self):
        # The path a deployed runner always takes: a libbtbb source resolved
        # for every run, build or not, so the pin is not what is required.
        for build, origin in (
            (None, LIBBTBB_RESOLVED_ORIGIN), ({"toolchain": {}}, LIBBTBB_BUILT_ORIGIN),
        ):
            with self.subTest(built=bool(build)):
                check = self.host_check(
                    f'LIBBTBB_REF="{self.PIN}"\n', libbtbb="HEAD", build=build,
                )

                self.assertEqual(origin, check.libbtbb.origin)
                self.assertNotEqual(
                    f"git-{self.PIN}", check.libbtbb.expected_revision
                )
                self.assertTrue(
                    check.libbtbb.expected_revision.startswith("git-"),
                    check.libbtbb.expected_revision,
                )
                # The pin is not what the tools are held to any more.
                self.assertIn(
                    "report libbtbb git-52b2afc",
                    check.result_failure(self.answer(check)),
                )

    def test_a_commit_longer_than_a_revision_string_is_compared_as_a_prefix(self):
        # A repository using sha256 object names has 64-digit commits, and no
        # build stamps more than 40 into a revision string: comparing the
        # whole value would turn down every library.
        sha256 = "4" * 64
        libbtbb = SourceRevision(
            name="libbtbb", source_path="/btbb", repository="example",
            requested_ref="HEAD", commit=sha256, short_commit=sha256[:12],
        )
        ubertooth = SourceRevision(
            name="Ubertooth", source_path="/src", repository="example",
            requested_ref="HEAD", commit="a" * 40, short_commit="aaaaaaa",
        )

        identity = libbtbb_identity(ubertooth, libbtbb, built=True)
        self.assertEqual(f"git-{sha256[:40]}", identity.expected_revision)
        self.assertRegex(
            "Host tools version: libubertooth 1.1 (git-aaaaaaa), "
            f"libbtbb 1.3 (git-{sha256[:12]})\n",
            identity.pattern,
        )

    def test_a_pin_is_compared_on_every_digit_it_carries(self):
        # A pin the fork wrote abbreviated identifies only those digits, and
        # git abbreviates to a length that is unique in the repository, so a
        # longer hash starting with them is that same commit.
        short = self.host_check(f'LIBBTBB_REF="{self.PIN[:7]}"\n')
        self.assertEqual("", short.result_failure(self.answer(short)))
        self.assertEqual(
            "", short.result_failure(self.answer(short, libbtbb=f"git-{self.PIN[:12]}"))
        )
        self.assertIn(
            "report libbtbb git-52b2afd",
            short.result_failure(self.answer(short, libbtbb="git-52b2afd")),
        )
        # A full pin is compared on all forty, so a longer hash that merely
        # starts with its first digits is turned down.
        full = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')
        other = f"git-{self.PIN[:7]}0000000000"
        self.assertIn(
            f"report libbtbb {other}",
            full.result_failure(self.answer(full, libbtbb=other)),
        )

    def test_an_uppercase_pin_still_matches_the_hash_the_library_reports(self):
        check = self.host_check(f'LIBBTBB_REF="{self.PIN.upper()}"\n')

        self.assertEqual(f"git-{self.PIN}", check.libbtbb.expected_revision)
        self.assertEqual("", check.result_failure(self.answer(check)))

    def test_only_the_host_version_line_can_satisfy_the_requirement(self):
        # The other lines of the `-V` answer name libbtbb too, and one of
        # them carries a path that can hold anything.
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')
        elsewhere = CommandResult(
            HOST_VERSION_ARGV, 0,
            f"Host tools version: libubertooth 1.1 ({check.expected_revision})\n"
            f"libbtbb loaded from: /build/libbtbb 1.3 (git-52b2afc)/lib.so\n",
            "", 0.01, False,
        )

        self.assertIn("no libbtbb version at all", check.result_failure(elsewhere))

    def test_a_libbtbb_only_mismatch_does_not_ask_the_older_option(self):
        # `-H` prints the same line from the same function, so asking it
        # again would only report the mismatch twice.
        check = self.host_check(f'LIBBTBB_REF="{self.PIN}"\n')
        result = self.answer(check, libbtbb="git-0176769")

        self.assertTrue(check.result_failure(result))
        self.assertFalse(check.needs_fallback(result))

    def test_a_checkout_that_cannot_be_read_refuses_the_run(self):
        gone = SourceRevision(
            name="Ubertooth", source_path="/no/such/checkout", repository="example",
            requested_ref="HEAD", commit="a" * 40, short_commit="aaaaaaa",
        )

        with self.assertRaises(PreflightError) as raised:
            libbtbb_identity(gone)
        self.assertIn("could not be read", str(raised.exception))


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


class ReleaseVersionLineTests(unittest.TestCase):
    """The version lines as the fork prints them since #352 (fork #1230).

        Host tools version: 2026-11-DEV1+22 (libbtbb 2020-12-R1+79)
        Firmware version: bluetooth_rxtx 2026-11-DEV1+22 (API:1.58)
        Firmware build: Thu Nov  6 02:11:04 UTC 2026

    The library ABI numbers and the parenthesized commits have gone, the
    libbtbb path line has gone, and the build string is the build date. Which
    layout a commit prints is decided by the resolved commit, so a run of an
    older ref is still held to the old one.
    """

    RELEASE = "2026-11-DEV1+22"
    LIBBTBB_RELEASE = "2020-12-R1+79"
    ANSWER = (
        f"Host tools version: {RELEASE} (libbtbb {LIBBTBB_RELEASE})\n"
        f"Firmware version: bluetooth_rxtx {RELEASE} (API:1.58)\n"
        "Firmware build: Thu Nov  6 02:11:04 UTC 2026\n"
    )

    @staticmethod
    def source(name, build_version, commit="a" * 40, source_path="/src"):
        return SourceRevision(
            name=name, source_path=source_path, repository="example",
            requested_ref="HEAD", commit=commit, short_commit=commit[:7],
            build_version=build_version,
        )

    def check(self, patterns, *, libbtbb=None, name="Host version"):
        return PreflightCheck(
            check_id="preflight-001-host-version", name=name,
            host="Host_Linux", resource_alias=None,
            resource_identity=MappingProxyType({}), argv=HOST_VERSION_ARGV,
            expected_patterns=patterns, expected_revision=self.RELEASE,
            accepted_returncodes=(0, 1), libbtbb=libbtbb,
        )

    @staticmethod
    def result(output, argv=HOST_VERSION_ARGV):
        return CommandResult(argv, 0, output, "", 0.01, False)

    def test_the_release_line_passes_and_an_older_layout_does_not(self):
        ubertooth = self.source("Ubertooth", self.RELEASE)
        libbtbb = self.source("libbtbb", self.LIBBTBB_RELEASE, commit="b" * 40)
        check = self.check(host_version_patterns(ubertooth, libbtbb))

        self.assertEqual("", check.result_failure(self.result(self.ANSWER)))
        # A build of this commit reports a release, so the layout a commit
        # before the change prints is not that answer.
        older = (
            f"Host tools version: libubertooth 1.1 (git-aaaaaaa), "
            f"libbtbb 1.3 (git-bbbbbbb)\n"
        )
        self.assertIn("identity mismatch",
                      check.result_failure(self.result(older)))

    def test_another_release_fails_and_the_marker_fails_with_it(self):
        check = self.check(
            host_version_patterns(self.source("Ubertooth", self.RELEASE))
        )
        for version in ("2026-11-DEV1+23", "2026-11-DEV1", "2026-11-DEV1+22*",
                        "2026-11-DEV1+220", "2020-12-R1+unknown"):
            with self.subTest(version=version):
                answer = self.ANSWER.replace(
                    f"Host tools version: {self.RELEASE}",
                    f"Host tools version: {version}", 1,
                )
                self.assertIn("identity mismatch",
                              check.result_failure(self.result(answer)))

    def test_the_firmware_lines_cannot_stand_in_for_the_host_line(self):
        """The version on them is the same string, so the label is required."""
        check = self.check(
            host_version_patterns(self.source("Ubertooth", self.RELEASE))
        )
        firmware_only = self.ANSWER.split("\n", 1)[1]

        self.assertIn("identity mismatch",
                      check.result_failure(self.result(firmware_only)))

    def test_the_libbtbb_half_is_read_off_the_new_line(self):
        identity = LibbtbbIdentity(
            self.LIBBTBB_RELEASE, LIBBTBB_RESOLVED_ORIGIN, "fix it",
            names_release=True,
        )
        check = self.check(
            host_version_patterns(self.source("Ubertooth", self.RELEASE)),
            libbtbb=identity,
        )

        self.assertEqual("", check.result_failure(self.result(self.ANSWER)))
        other = self.ANSWER.replace(self.LIBBTBB_RELEASE, "2020-12-R1+80")
        reason = check.result_failure(self.result(other))
        self.assertIn("report libbtbb 2020-12-R1+80", reason)
        self.assertIn(self.LIBBTBB_RELEASE, reason)

    def test_a_resolved_libbtbb_is_required_by_its_release(self):
        ubertooth = self.source("Ubertooth", self.RELEASE)
        libbtbb = self.source("libbtbb", self.LIBBTBB_RELEASE, commit="b" * 40)

        identity = libbtbb_identity(ubertooth, libbtbb, built=True)

        self.assertTrue(identity.required)
        self.assertTrue(identity.names_release)
        self.assertEqual(self.LIBBTBB_RELEASE, identity.expected_revision)
        self.assertEqual(LIBBTBB_BUILT_ORIGIN, identity.origin)

    def test_a_commit_style_libbtbb_under_the_new_line_is_still_a_commit(self):
        """The two repositories moved separately, so the line may carry either.

        The Ubertooth commit decides the layout; what libbtbb's half must
        carry comes from the libbtbb commit.
        """
        ubertooth = self.source("Ubertooth", self.RELEASE)
        older_libbtbb = self.source("libbtbb", "", commit="b" * 40)

        identity = libbtbb_identity(ubertooth, older_libbtbb)

        self.assertEqual("git-" + "b" * 40, identity.expected_revision)
        self.assertTrue(identity.names_release)
        check = self.check(
            host_version_patterns(ubertooth), libbtbb=identity
        )
        answer = self.ANSWER.replace(
            f"(libbtbb {self.LIBBTBB_RELEASE})", "(libbtbb git-bbbbbbb)"
        )
        self.assertEqual("", check.result_failure(self.result(answer)))

    def test_a_pin_with_no_libbtbb_checkout_is_recorded_as_not_checked(self):
        """A commit, and the line may carry a release the pin cannot give.

        Turning a commit into the release a build of it names needs that
        checkout, which a run given no --libbtbb-source has not got. So the
        run says so and measures everything else, rather than comparing a
        commit against a release name and failing for no reason.
        """
        pin = "52b2afcb0c4b8b03412dac759aad88debdfb18e2"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            commit = make_git_repo(path, install_sh=f'LIBBTBB_REF="{pin}"\n')
            ubertooth = self.source(
                "Ubertooth", self.RELEASE, commit=commit, source_path=str(path)
            )

            identity = libbtbb_identity(ubertooth)

            self.assertFalse(identity.required)
            self.assertEqual(["libbtbb_not_checked"],
                             list(identity.to_details()))
            (reason,) = identity.to_details().values()
            self.assertIn(pin, reason)
            self.assertIn("--libbtbb-source", reason)
            # Nothing is compared, so any library passes the check.
            check = self.check(
                host_version_patterns(ubertooth), libbtbb=identity
            )
            self.assertEqual("", check.result_failure(self.result(
                self.ANSWER.replace(self.LIBBTBB_RELEASE, "1999-01-R1+3")
            )))

    def test_an_older_ref_still_has_its_pin_checked_by_commit(self):
        pin = "52b2afcb0c4b8b03412dac759aad88debdfb18e2"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source"
            commit = make_git_repo(path, install_sh=f'LIBBTBB_REF="{pin}"\n')
            ubertooth = self.source(
                "Ubertooth", "", commit=commit, source_path=str(path)
            )

            identity = libbtbb_identity(ubertooth)

            self.assertTrue(identity.required)
            self.assertFalse(identity.names_release)
            self.assertEqual(f"git-{pin}", identity.expected_revision)

    def test_minus_V_reads_the_firmware_version_where_the_build_string_cannot(self):
        """The build string names no version now, so the same answer's own
        `Firmware version:` line is what the second command reads."""
        patterns = firmware_identity_patterns(self.RELEASE, names_release=True)
        check = self.check(patterns, name="Firmware compile info")

        self.assertEqual("", check.result_failure(self.result(self.ANSWER)))
        # Another revision on that line fails, and the reason names both.
        other = self.ANSWER.replace(
            f"bluetooth_rxtx {self.RELEASE}", "bluetooth_rxtx 2026-11-DEV1+21"
        )
        reason = check.result_failure(self.result(other))
        self.assertIn("runs 2026-11-DEV1+21", reason)
        self.assertIn(self.RELEASE, reason)
        # A board with nothing to show for its build string fails too.
        for missing in ("read failed", "not reported"):
            with self.subTest(missing=missing):
                answer = self.ANSWER.replace(
                    "Thu Nov  6 02:11:04 UTC 2026", missing
                )
                self.assertIn("identity mismatch",
                              check.result_failure(self.result(answer)))

    def test_an_older_ref_reads_the_revision_off_the_build_string(self):
        patterns = firmware_identity_patterns("git-1c7f06d", names_release=False)
        self.assertEqual((compile_info_pattern("git-1c7f06d"),), patterns)

    def test_the_manifest_records_the_release_a_build_reports(self):
        source = self.source("Ubertooth", self.RELEASE)

        self.assertEqual(self.RELEASE, source.expected_revision)
        self.assertTrue(source.names_release)
        self.assertEqual(self.RELEASE, source.to_dict()["build_version"])
        self.assertEqual(self.RELEASE, source.to_dict()["expected_revision"])
        # An older commit records no build_version and falls back to the hash
        # its build stamps, which is what every run did before #352.
        older = self.source("Ubertooth", "")
        self.assertEqual("git-aaaaaaa", older.expected_revision)
        self.assertFalse(older.names_release)
        self.assertNotIn("build_version", older.to_dict())


if __name__ == "__main__":
    unittest.main()
