import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ubertooth_harness.cli import main
from ubertooth_harness.executor import CommandResult


class HostVersionExecutor:
    def __init__(self, revision):
        self.revision = revision
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        output = (
            f"libubertooth 1.1 ({self.revision}), "
            "libbtbb 1.0 (git-btbb)\n"
        )
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


class FlashDeviceExecutor:
    """Host tools plus one selected and one guard Ubertooth, for flash runs."""

    def __init__(self, host_revision, images, selected, guard):
        self.host_revision = host_revision
        self.images = images  # path -> revision
        self.revisions = {selected: "2020-12-R1", guard: "git-guard"}
        self.commands = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None, cwd=None):
        command = tuple(argv)
        self.commands.append(command)
        if command == ("ubertooth-util", "-H"):
            output = f"libubertooth 1.1 ({self.host_revision}), libbtbb 1.0 (git-btbb)\n"
        elif command[0] == "ubertooth-dfu":
            serial = command[command.index("-U") + 1]
            self.revisions[serial] = self.images[command[command.index("-d") + 1]]
            output = "Verified firmware revision\n"
        elif command[-1] == "-s":
            output = f"Serial No: {command[2]}\n"
        elif command[-1] == "-v":
            output = f"Firmware version: {self.revisions[command[2]]} (API:1.07)\n"
        else:
            raise AssertionError(f"unexpected command {command}")
        if on_output:
            on_output("stdout", output)
        return CommandResult(command, 0, output, "", 0.01, False)


def make_source(root):
    source = root / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("source\n")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        check=True,
    )
    short = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return source, short


class CliTests(unittest.TestCase):

    def test_a_build_run_that_fails_before_tests_still_closes_its_record(self):
        from ubertooth_harness import cli
        from ubertooth_harness.manifest import ManifestError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = make_source(root)
            platform = "macos" if sys.platform == "darwin" else "linux"
            plan = root / "plan.yaml"
            plan.write_text("plan: p\nsuites:\n  fast:\n    - Host version:\n        hosts: each\n")
            hardware = root / "hardware.yaml"
            hardware.write_text(
                f"resources:\n  Host_Local:\n    type: Host\n    platform: {platform}\n"
                "    roles: [test target]\n"
            )
            output = root / "runs"
            with (
                patch.object(cli, "_prepare_run", side_effect=ManifestError("artifact vanished")),
                redirect_stderr(io.StringIO()),
                redirect_stdout(io.StringIO()),
            ):
                result = main([
                    "run-suite", "--plan", str(plan), "--suite", "fast",
                    "--hardware", str(hardware), "--host", "Host_Local",
                    "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                    "--libbtbb-source", str(source), "--libbtbb-ref", "HEAD",
                    "--build", "--run-id", "early-fail", "--output", str(output),
                ])

            self.assertEqual(2, result)
            run_dir = output / "early-fail"
            events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text().splitlines()]
            self.assertEqual("run_started", events[0]["event_type"])
            self.assertTrue(events[0]["details"]["sources"])
            self.assertEqual("run_finished", events[-1]["event_type"])
            self.assertEqual("failed", events[-1]["status"])
            report = (run_dir / "report.md").read_text()
            self.assertIn("artifact vanished", report)
            self.assertIn("| Ubertooth | `HEAD` = ", report)

    def test_flash_reliability_runs_as_a_plan_test_into_one_run_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, short = make_source(root)
            test_image = root / "test.dfu"
            kgv_image = root / "kgv.dfu"
            test_image.write_bytes(b"test")
            kgv_image.write_bytes(b"kgv")
            kgv_hash = hashlib.sha256(kgv_image.read_bytes()).hexdigest()
            platform = "macos" if sys.platform == "darwin" else "linux"
            plan = root / "plan.yaml"
            plan.write_text(f"""plan: flash cli
suites:
  slow:
    - Flash reliability:
        hosts: each
        platforms: [{platform}]
        Ubertooth: default
        flashes: 4
        image sequence: [test build, KGV]
        KGV sha256: {kgv_hash}
        KGV revision: 2020-12-R1
""")
            hardware = root / "hardware.yaml"
            hardware.write_text(f"""resources:
  Host_Local:
    type: Host
    platform: {platform}
    roles: [test target]
    defaults: {{Ubertooth: UT_0}}
  UT_0:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: aaaa0000}}
  UT_1:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: bbbb1111}}
""")
            executor = FlashDeviceExecutor(
                f"git-{short}",
                {str(test_image.resolve()): f"git-{short}", str(kgv_image.resolve()): "2020-12-R1"},
                "aaaa0000", "bbbb1111",
            )
            output = root / "runs"
            summary_output = io.StringIO()
            with (
                patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
                redirect_stderr(io.StringIO()),
                redirect_stdout(summary_output),
            ):
                result = main([
                    "run-suite", "--plan", str(plan), "--suite", "slow",
                    "--hardware", str(hardware), "--host", "Host_Local",
                    "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                    "--artifact", f"test build={test_image}",
                    "--artifact", f"KGV={kgv_image}",
                    "--run-id", "flash-run", "--output", str(output),
                ])

            self.assertEqual(0, result, summary_output.getvalue())
            run_dir = output / "flash-run"
            self.assertEqual(["report.md", "run.jsonl"], sorted(p.name for p in run_dir.iterdir()))
            flashes = [c for c in executor.commands if c[0] == "ubertooth-dfu"]
            self.assertEqual(4, len(flashes))
            self.assertTrue(all(c[2] == "aaaa0000" for c in flashes))
            # The guard board is read before the run and after every flash.
            self.assertEqual(5, executor.commands.count(("ubertooth-util", "-U", "bbbb1111", "-v")))
            # Firmware is written by the test, so preflight checks identity only.
            self.assertNotIn(("ubertooth-util", "-U", "aaaa0000", "-V"), executor.commands)
            events = [json.loads(line) for line in (run_dir / "run.jsonl").read_text().splitlines()]
            types = [e["event_type"] for e in events]
            self.assertEqual("run_started", types[0])
            self.assertEqual("run_finished", types[-1])
            self.assertEqual(4, types.count("flash_finished"))
            report = (run_dir / "report.md").read_text()
            self.assertIn("=== TEST 001-Flash reliability: Flash reliability on Host_Local", report)
            self.assertIn("    +++ flash 1: test_build", report)
            self.assertIn("[C] ubertooth-dfu -U aaaa0000 -d", report)
            self.assertIn("    --- flash reliability EXIT: passed, 4/4 verified ---", report)
            # The selected and guard boards were leased for the whole run.
            manifest = next(e for e in events if e["event_type"] == "manifest")
            self.assertEqual(
                ["UT_0", "UT_1"],
                [item["resource"] for item in manifest["details"]["manifest"]["leases"]],
            )
            self.assertIn("| Board leases | UT_0 since ", report)
            self.assertIn("aaaa0000", report)  # serials are shown by default
            self.assertIn("UT_0 serial aaaa0000: firmware written by this run", report)


    def test_alternate_ubertooths_rotates_flashes_across_registered_boards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, short = make_source(root)
            test_image = root / "test.dfu"
            kgv_image = root / "kgv.dfu"
            test_image.write_bytes(b"test")
            kgv_image.write_bytes(b"kgv")
            kgv_hash = hashlib.sha256(kgv_image.read_bytes()).hexdigest()
            platform = "macos" if sys.platform == "darwin" else "linux"
            plan = root / "plan.yaml"
            plan.write_text(f"""plan: flash alternate
suites:
  slow:
    - Flash reliability:
        hosts: [Host_Local]
        platforms: [{platform}]
        Ubertooth: default
        flashes: 4
        image sequence: [test build, KGV]
        KGV sha256: {kgv_hash}
        KGV revision: 2020-12-R1
        alternate Ubertooths: true
""")
            hardware = root / "hardware.yaml"
            hardware.write_text(f"""resources:
  Host_Local:
    type: Host
    platform: {platform}
    roles: [test target]
    defaults: {{Ubertooth: UT_0}}
  UT_0:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}}
  UT_1:
    type: Ubertooth
    attached to: Host_Local
    identity: {{serial: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}}
""")
            executor = FlashDeviceExecutor(
                f"git-{short}",
                {str(test_image.resolve()): f"git-{short}", str(kgv_image.resolve()): "2020-12-R1"},
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            )
            output = root / "runs"
            summary_output = io.StringIO()
            with (
                patch("ubertooth_harness.cli.SubprocessCommandExecutor", return_value=executor),
                redirect_stderr(io.StringIO()),
                redirect_stdout(summary_output),
            ):
                result = main([
                    "run-suite", "--plan", str(plan), "--suite", "slow",
                    "--hardware", str(hardware), "--host", "Host_Local",
                    "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                    "--artifact", f"test build={test_image}",
                    "--artifact", f"KGV={kgv_image}",
                    "--run-id", "alt-run", "--output", str(output),
                ])

            self.assertEqual(0, result, summary_output.getvalue())
            targets = [c[2] for c in executor.commands if c[0] == "ubertooth-dfu"]
            self.assertEqual(["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"] * 2, targets)
            report = (output / "alt-run" / "report.md").read_text()
            self.assertIn("4 flashes alternating across aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", report)
            self.assertIn("4/4 verified (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa: 2, bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb: 2)", report)
            # Alternated boards are bound resources: serial preflight, Devices row.
            self.assertIn(("ubertooth-util", "-U", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "-s"), executor.commands)
            self.assertIn("UT_1 serial bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb: firmware written by this run; final 2020-12-R1", report)
            self.assertIn("alternated Ubertooth 2=UT_1", report)

    def test_run_suite_resolves_source_writes_manifest_and_runs_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "tracked.txt").write_text("source\n")
            subprocess.run(
                ["git", "-C", str(source), "add", "tracked.txt"], check=True
            )
            subprocess.run(
                [
                    "git", "-C", str(source), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit",
                    "-qm", "source",
                ],
                check=True,
            )
            short_commit = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            plan = root / "plan.yaml"
            plan.write_text(
                """plan: cli named
suites:
  fast:
    - Host version:
        hosts: each
"""
            )
            platform = "macos" if sys.platform == "darwin" else "linux"
            hardware = root / "hardware.yaml"
            hardware.write_text(
                f"""resources:
  Host_Local:
    type: Host
    platform: {platform}
    roles: [test target]
"""
            )
            artifact = root / "host-package.tar"
            artifact.write_bytes(b"host artifact")
            output = root / "runs"
            terminal = io.StringIO()
            summary_output = io.StringIO()
            executor = HostVersionExecutor(f"git-{short_commit}")

            with (
                patch(
                    "ubertooth_harness.cli.SubprocessCommandExecutor",
                    return_value=executor,
                ),
                redirect_stderr(terminal),
                redirect_stdout(summary_output),
            ):
                result = main(
                    [
                        "run-suite",
                        "--plan", str(plan),
                        "--suite", "fast",
                        "--hardware", str(hardware),
                        "--host", "Host_Local",
                        "--ubertooth-source", str(source),
                        "--ubertooth-ref", "HEAD",
                        "--artifact", f"host package={artifact}",
                        "--run-id", "manifest-cli-run",
                        "--output", str(output),
                    ]
                )

            self.assertEqual(0, result)
            summary = json.loads(summary_output.getvalue())
            self.assertEqual(1, summary["completed_preflight_checks"])
            events = [
                json.loads(line)
                for line in (
                    output / "manifest-cli-run" / "run.jsonl"
                ).read_text().splitlines()
            ]
            manifest = next(
                e for e in events if e["event_type"] == "manifest"
            )["details"]["manifest"]
            self.assertEqual("HEAD", manifest["sources"][0]["requested_ref"])
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                manifest["artifacts"][0]["sha256"],
            )
            self.assertEqual(
                ["report.md", "run.jsonl"],
                sorted(path.name for path in (output / "manifest-cli-run").iterdir()),
            )
            event_types = [event["event_type"] for event in events]
            self.assertLess(
                event_types.index("preflight_finished"),
                event_types.index("test_started"),
            )
            self.assertEqual(
                [("ubertooth-util", "-H"), ("ubertooth-util", "-H")],
                executor.commands,
            )


if __name__ == "__main__":
    unittest.main()
