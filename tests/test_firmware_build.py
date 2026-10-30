"""`scripts/ci.sh firmware-dk`: the nRF52840 DK firmware build in CI (#147).

The step drives Nordic's toolchain image through `docker`, so a fake `docker`
on PATH runs these tests: it records the shell command it was handed and,
for the build, writes the image a real `west build` would leave behind. No
container runs, nothing is downloaded, and the pinned SDK version and board
target are read from the repository, so a drift between the firmware's
`NCS_VERSION` and what CI builds fails here.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

from tests.executables import write_executable
from tests.test_nrfdk import hex_file
from ubertooth_harness import nrfdk

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "firmware.yml"
FIRMWARE = ROOT / "firmware" / "nrf52840dk"
NCS_VERSION = (FIRMWARE / "NCS_VERSION").read_text().strip()
BOARD = "nrf52840dk/nrf52840"

# Writes the image west build would produce when the command is a build, and
# records every command. $FAKE_HEX_PAYLOAD is the version string the image
# carries; with $FAKE_DOCKER_EXIT it fails like a build that does not compile.
FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    printf '%s\\n' "$*" >> "$FAKE_LOG"
    case "$*" in
        *"west build"*)
            [ "${FAKE_DOCKER_EXIT:-0}" = 0 ] || exit "$FAKE_DOCKER_EXIT"
            mkdir -p "$(dirname "$FAKE_HEX")"
            cp "$FAKE_HEX_PAYLOAD" "$FAKE_HEX" ;;
    esac
    exit 0
    """)


class FirmwareStepTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="harness-firmware-ci-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "docker.log"
        self.work = self.directory / "work"
        self.workspace = self.work / "ncs"
        self.work.mkdir()
        write_executable(self.bin / "docker", FAKE_DOCKER)

    def run_step(self, version=nrfdk.FIRMWARE_VERSION, **environment):
        """Run `ci.sh firmware-dk`; the fake build writes an image holding
        ``version``."""
        payload = hex_file(self.directory / "image.hex",
                           b"\x00" * 8 + version.encode() + b"\x00" * 8)
        env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            CI_TEMP=str(self.work),
            FAKE_LOG=str(self.log),
            FAKE_HEX=str(self.workspace / "utdk-build" / "zephyr" / "zephyr.hex"),
            FAKE_HEX_PAYLOAD=str(payload),
        )
        env.update(environment)
        return subprocess.run(
            ("sh", str(CI), "firmware-dk"), cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=120,
        )

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def find(self, word):
        """The one recorded command holding ``word``."""
        found = [command for command in self.commands() if word in command]
        self.assertEqual(1, len(found), f"{word}: {found}")
        return found[0]

    def test_the_pinned_sdk_builds_the_firmware_and_the_image_is_checked(self):
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # Nordic's image for exactly the version the firmware pins.
        for command in self.commands():
            self.assertIn(f"ghcr.io/nrfconnect/sdk-nrf-toolchain:{NCS_VERSION}", command)
        init = self.find("west init")
        self.assertIn(f"--mr {NCS_VERSION}", init)
        self.assertIn("https://github.com/nrfconnect/sdk-nrf", init)
        self.assertIn("--narrow", self.find("west update"))
        build = self.find("west build")
        self.assertIn(f"-b {BOARD}", build)
        self.assertIn("--no-sysbuild", build)
        self.assertIn("-p always", build)
        self.assertIn(f"-s {ROOT}/firmware/nrf52840dk", build)
        # The image check is the harness's own, and it names the version.
        self.assertIn(f"firmware version: {nrfdk.FIRMWARE_VERSION}", result.stdout)
        self.assertIn("firmware-dk passed", result.stdout)

    def test_the_repository_is_mounted_read_only(self):
        # A build that could write into the working tree would leave the
        # repository dirty; nothing under firmware/ is generated.
        self.run_step()

        for command in self.commands():
            self.assertIn(f"-v {ROOT}:{ROOT}:ro", command)

    def test_an_image_without_the_pinned_version_fails_the_step(self):
        # `nrfdk.py ensure` flashes only when the board reports another
        # version, so an image built from a changed source without a version
        # bump would leave the old one on the boards (#147).
        result = self.run_step(version="utdk-0.0.1")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("does not contain firmware version", result.stderr)

    def test_a_build_failure_fails_the_step(self):
        result = self.run_step(FAKE_DOCKER_EXIT="1")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertNotIn("firmware-dk passed", result.stdout)

    def test_an_existing_workspace_is_reused(self):
        (self.workspace / ".west").mkdir(parents=True)

        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("west init", "\n".join(self.commands()))
        self.assertIn("reusing the workspace", result.stdout)

    def test_the_workspace_can_be_kept_outside_the_work_directory(self):
        kept = self.directory / "kept"

        result = self.run_step(NCS_WORKSPACE=str(kept),
                               FAKE_HEX=str(kept / "utdk-build/zephyr/zephyr.hex"))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"-v {kept}:{kept}", self.find("west build"))

    def test_the_firmware_build_is_not_part_of_the_other_check_sets(self):
        # It needs docker and gigabytes of downloads: `all` must stay the
        # check set a developer and the Test workflow can run.
        text = CI.read_text()
        self.assertIn("all) run_test; run_build ;;", text)
        self.assertIn("firmware-dk) run_firmware_dk ;;", text)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_it_runs_on_a_firmware_change_a_schedule_and_by_hand(self):
        # `on` is YAML's true unless quoted; both spellings are accepted here.
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("workflow_dispatch", triggers)
        self.assertIn("schedule", triggers)
        for event in ("push", "pull_request"):
            paths = triggers[event]["paths"]
            self.assertIn("firmware/nrf52840dk/**", paths)
            self.assertIn("scripts/ci.sh", paths)
            self.assertIn(".github/workflows/firmware.yml", paths)

    def test_the_job_runs_the_shared_check_and_leaves_the_tree_clean(self):
        steps = self.workflow["jobs"]["nrf52840dk"]["steps"]
        runs = [step.get("run", "") for step in steps]
        self.assertIn("sh scripts/ci.sh firmware-dk", runs)
        self.assertTrue(any("git status --porcelain" in run for run in runs), runs)
