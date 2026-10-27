import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType

from ubertooth_harness.events import RunEvent
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.manifest import (
    GitRevisionResolver,
    ManifestError,
    build_run_manifest,
)
from ubertooth_harness.planner import TestInvocation
from ubertooth_harness.preflight import PreflightError, PreflightPolicy
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
        if command == ("ubertooth-util", "-H"):
            output = (
                f"libubertooth 1.1 ({self.revision}), "
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


class PreflightTests(unittest.TestCase):

    def prepare(self, directory):
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

    def test_policy_deduplicates_host_and_device_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            _root, source, _manifest, checks = self.prepare(directory)

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
            root, _source, manifest, _checks = self.prepare(directory)
            invocations = (make_invocations()[0], self.flash_invocation())
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            checks = PreflightPolicy(registry).compile(
                invocations, manifest.with_invocations(invocations)
            )

            self.assertEqual(("Host version", "Device serial"), tuple(c.name for c in checks))

    def test_tests_before_a_firmware_write_still_need_the_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = self.prepare(directory)
            invocations = make_invocations() + (self.flash_invocation(),)
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            checks = PreflightPolicy(registry).compile(
                invocations, manifest.with_invocations(invocations)
            )

            self.assertIn("Firmware version", [c.name for c in checks])

    def test_two_firmware_writing_tests_may_share_a_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = self.prepare(directory)
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
            root, _source, manifest, _checks = self.prepare(directory)
            invocations = (self.flash_invocation("002-Flash reliability"), make_invocations()[2])
            registry = FileResourceRegistry.load(root / "hardware.yaml")

            with self.assertRaisesRegex(PreflightError, "last test on UT_Linux0"):
                PreflightPolicy(registry).compile(
                    invocations, manifest.with_invocations(invocations)
                )

    def test_policy_rejects_binding_drift_from_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _source, manifest, _checks = self.prepare(directory)
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
            root, source, manifest, checks = self.prepare(directory)
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
            _, source, _, checks = self.prepare(directory)
            check = next(c for c in checks if c.name == "Firmware version")
            revision = source.expected_revision

            def failure(text):
                return check.result_failure(
                    CommandResult(check.argv, 0, text, "", 0.01, False))

            self.assertFalse(failure(f"Firmware version: {revision} (API:1.35)\n"))
            self.assertFalse(
                failure(f"Firmware version: bluetooth_rxtx {revision} (API:1.35)\n"))
            for other in (f"btbr {revision}", f"bluetooth_rxtx {revision}x",
                          f"bluetooth_rxtx git-other {revision}"):
                self.assertIn("identity mismatch",
                              failure(f"Firmware version: {other} (API:1.35)\n"))

    def test_preflight_failure_prevents_test_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root, source, manifest, checks = self.prepare(directory)
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


if __name__ == "__main__":
    unittest.main()
