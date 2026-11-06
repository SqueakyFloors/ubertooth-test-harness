"""`scripts/ci.sh firmware-esp32`: the ESP32 Classic peer firmware build in CI
(#326).

Nothing else in this repository compiles that firmware: the unit suite reads
its sources as text and `tests/fake_esp32.py` implements the protocol its
README documents, so a source change that does not build used to pass every
check here.

The step drives Espressif's toolchain image through `docker`, so a fake
`docker` on PATH runs these tests: it records the shell command it was handed
and, for a build, writes the image and the resolved configuration a real
`idf.py` would leave behind. No container runs, nothing is downloaded, and
the pinned ESP-IDF version, the target, and the firmware version string are
read from the repository, so a drift between the firmware's `IDF_VERSION` and
what CI builds fails here.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

from tests.executables import write_executable
from ubertooth_harness import targets

ROOT = Path(__file__).resolve().parents[1]
# The path the shell works with: a `sh` started in ROOT reports the physical
# directory, which differs from ROOT where a parent is a symlink (/tmp on
# macOS), and that is what the step mounts.
REPO = Path(os.path.realpath(ROOT))
CI = ROOT / "scripts" / "ci.sh"
LOCAL_CI = ROOT / "scripts" / "local-ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "firmware-esp32.yml"
FIRMWARE = ROOT / "firmware" / "esp32-classic-peer"
IDF_VERSION = (FIRMWARE / "IDF_VERSION").read_text().strip()
TARGET = "esp32"
IMAGE = f"espressif/idf:{IDF_VERSION}"

# Records every command, and for the two that do something writes what
# `idf.py` leaves behind: `set-target` the resolved configuration in the
# project directory, `build` the image in the build directory.
#
# $FAKE_SDKCONFIG replaces the configuration `set-target` resolves, which is
# how a test drops a setting the defaults file asks for.
# $FAKE_BUILD_OUTPUT is a file of lines the build prints, for the warning
# check. $FAKE_BIN_PAYLOAD is the image every build leaves, and
# $FAKE_DOCKER_EXIT fails the build like one that does not compile.
FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    printf '%s\\n' "$*" >> "$FAKE_LOG"
    all=$*
    project=${all##*-C }
    project=${project%% *}
    out=${all##*-B }
    out=${out%% *}
    case "$all" in
        *set-target*)
            if [ -n "${FAKE_SDKCONFIG:-}" ]; then
                cp "$FAKE_SDKCONFIG" "$project/sdkconfig"
            else
                # What Kconfig resolves from the defaults: every setting as
                # it was asked for, with a disabled one written as a comment,
                # which is how ESP-IDF writes it.
                grep '^CONFIG_' "$project/sdkconfig.defaults" |
                    sed -e 's/^\\(CONFIG_[A-Z0-9_]*\\)=n$/# \\1 is not set/' \\
                    > "$project/sdkconfig"
            fi
            mkdir -p "$out" ;;
        *" build"*)
            builds=$(cat "$FAKE_BUILDS" 2>/dev/null || echo 0)
            builds=$((builds + 1))
            printf '%s\\n' "$builds" > "$FAKE_BUILDS"
            # The sources this build compiled, for a test to look at.
            (cd "$project" && find . -type f | sort) > "$FAKE_BUILDS.$builds"
            [ -z "${FAKE_BUILD_OUTPUT:-}" ] || cat "$FAKE_BUILD_OUTPUT"
            code=${FAKE_DOCKER_EXIT:-0}
            [ "$code" = 0 ] || exit "$code"
            mkdir -p "$out"
            cp "$FAKE_BIN_PAYLOAD" "$out/esp32-classic-peer.bin" ;;
    esac
    exit 0
    """)


class FirmwareStepTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="harness-esp32-ci-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        self.log = self.directory / "docker.log"
        self.work = self.directory / "work"
        self.workspace = self.work / "esp"
        self.work.mkdir()
        write_executable(self.bin / "docker", FAKE_DOCKER)

    def run_step(self, version=targets.PEER_FIRMWARE, **environment):
        """Run `ci.sh firmware-esp32`; the fake build writes an image holding
        ``version``."""
        env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            CI_TEMP=str(self.work),
            FAKE_LOG=str(self.log),
            FAKE_BUILDS=str(self.directory / "builds"),
            FAKE_BIN_PAYLOAD=str(self.image(version=version)),
        )
        env.update(environment)
        # The workspace and the image are the step's own, not this
        # developer's: a test asks for either by name.
        for name in ("IDF_WORKSPACE", "IDF_IMAGE"):
            if name not in environment:
                env.pop(name, None)
        return subprocess.run(
            ("sh", str(CI), "firmware-esp32"), cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=120,
        )

    def image(self, name="image", *, filler=b"\x00", version=targets.PEER_FIRMWARE):
        """An image file carrying ``version``, told apart by its filler."""
        path = self.directory / f"{name}.bin"
        path.write_bytes(filler * 64 + version.encode() + filler * 64)
        return path

    def build_output(self, *lines: str) -> str:
        """A file of lines the fake build prints."""
        path = self.directory / "build-output"
        path.write_text("".join(f"{line}\n" for line in lines))
        return str(path)

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def builds(self):
        return [command for command in self.commands()
                if command.endswith(" build")]

    def find(self, word):
        """The one recorded command holding ``word``."""
        found = [command for command in self.commands() if word in command]
        self.assertEqual(1, len(found), f"{word}: {found}")
        return found[0]

    def compiled(self, build: int) -> list[str]:
        """The files the nth build found in the sources it was given."""
        listing = self.directory / f"builds.{build}"
        files = listing.read_text().splitlines() if listing.exists() else []
        # An empty listing would make every assertion on it vacuous.
        self.assertIn("./main/peer.h", files, f"build {build}: {files}")
        return files

    def test_the_pinned_idf_builds_the_firmware_and_the_image_is_checked(self):
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        # Espressif's image for exactly the version the firmware pins.
        for command in self.commands():
            self.assertIn(IMAGE, command)
        self.assertIn(f"set-target {TARGET}", self.find("set-target"))
        build = self.find(" build")
        # Out of tree, from a copy of this tree's sources: `set-target`
        # writes the resolved configuration into the project directory, and
        # the working tree is mounted read-only.
        self.assertIn(f"-C {self.workspace}/utesp-src", build)
        self.assertIn(f"-B {self.workspace}/utesp-build", build)
        self.assertEqual(
            (FIRMWARE / "main" / "peer.h").read_text(),
            (self.workspace / "utesp-src" / "main" / "peer.h").read_text())
        self.assertIn("./bluedroid/utesp_afh.c", self.compiled(1))
        # The version a peer's shell reports and a run checks it by.
        self.assertIn(f"firmware version: {targets.PEER_FIRMWARE}", result.stdout)
        self.assertIn("firmware-esp32 passed", result.stdout)

    def test_the_repository_is_mounted_read_only(self):
        # A build that could write into the working tree would leave the
        # repository dirty; nothing under firmware/ is generated.
        self.run_step()

        for command in self.commands():
            self.assertIn(f"-v {REPO}:{REPO}:ro", command)

    def test_the_toolchain_versions_are_reported_before_anything_is_built(self):
        # What a job log needs to say which compiler produced the image.
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"ESP-IDF {IDF_VERSION} toolchain ({IMAGE})", result.stdout)
        self.assertIn("xtensa-esp32-elf-gcc --version", self.find("--version"))

    def test_one_build_and_nothing_compared_with_another_commit(self):
        # Nothing in the harness flashes a peer, so a missing version bump
        # fails a run on the bench rather than silently leaving the old
        # image; a second build would only cost runtime here.
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(1, len(self.builds()), self.builds())

    def test_a_build_failure_fails_the_step(self):
        result = self.run_step(FAKE_DOCKER_EXIT="1")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("did not build with ESP-IDF", result.stderr)
        self.assertNotIn("firmware-esp32 passed", result.stdout)

    def test_the_build_output_reaches_the_log_and_the_step_still_passes(self):
        # The output is teed, so a build's own lines are in the job log as
        # well as in the file the warning check reads.
        result = self.run_step(FAKE_BUILD_OUTPUT=self.build_output(
            "Project build complete.",
            "esp32-classic-peer.bin binary size 0x9ffff bytes"))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Project build complete.", result.stdout)

    def test_a_warning_about_the_peers_own_sources_fails_the_step(self):
        warning = (f"{self.workspace}/utesp-src/main/bt_peer.c:120:5: "
                   "warning: unused variable 'spare'")
        result = self.run_step(FAKE_BUILD_OUTPUT=self.build_output(warning))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(warning, result.stderr)
        self.assertIn("the build warned about the lines above in "
                      "firmware/esp32-classic-peer", result.stderr)

    def test_a_warning_named_by_a_path_from_the_build_directory_counts(self):
        # A compiler may spell the file from where it was run, so the check
        # matches the directory and the name together rather than the whole
        # path.
        warning = ("../utesp-src/bluedroid/utesp_sco_type.c:44:9: "
                   "warning: implicit declaration of function 'spare'")
        result = self.run_step(FAKE_BUILD_OUTPUT=self.build_output(warning))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(warning, result.stderr)

    def test_a_warning_in_esp_idfs_own_sources_does_not_fail_the_step(self):
        # Those are Espressif's to fix, not this repository's, and failing on
        # them would make the step red on an SDK bump nobody here can mend.
        result = self.run_step(FAKE_BUILD_OUTPUT=self.build_output(
            "/opt/esp/idf/components/bt/host/bluedroid/stack/btm/btm_sco.c:200:1: "
            "warning: unused parameter 'p_data'",
            "/opt/esp/idf/components/esp_system/startup.c:12:3: "
            "warning: this is Espressif's"))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("accounted for 2 warning line(s)", result.stdout)

    def test_an_image_without_the_pinned_version_fails_the_step(self):
        # A peer reports this string and a run checks it by that, so an image
        # that does not carry it cannot be the one the sources describe.
        result = self.run_step(version="utesp-0.0.1")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(f"does not carry firmware version {targets.PEER_FIRMWARE}",
                      result.stderr)

    def test_a_build_that_leaves_no_image_fails_the_step(self):
        result = self.run_step(FAKE_BIN_PAYLOAD=str(self.directory / "absent.bin"))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("the build left no image at", result.stderr)

    def test_the_image_hash_is_printed_and_compared_with_nothing(self):
        # This image is not reproducible, so docs/hardware.md records the
        # hash of what a bench was flashed with and the step only says what
        # it built.
        payload = self.image("hashed", filler=b"\x77")

        result = self.run_step(FAKE_BIN_PAYLOAD=str(payload))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(hashlib.sha256(payload.read_bytes()).hexdigest(),
                      result.stdout)

    def test_every_default_the_firmware_sets_has_to_be_resolved(self):
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        wanted = [line for line in
                  (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
                  if line.startswith("CONFIG_") and not line.endswith("=n")]
        self.assertIn(f"all {len(wanted)} of them", result.stdout)

    def test_a_default_the_resolved_configuration_drops_fails_the_step(self):
        # An option that was renamed or withdrawn is ignored with a warning
        # nobody reads, and the image then runs with another configuration
        # than this directory asks for, which no compiler can catch.
        defaults = (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
        resolved = self.directory / "sdkconfig"
        resolved.write_text("".join(
            f"{line}\n" for line in defaults
            if line.startswith("CONFIG_") and not line.endswith("=n")
            and not line.startswith("CONFIG_BT_CLASSIC_ENABLED")))

        result = self.run_step(FAKE_SDKCONFIG=str(resolved))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("CONFIG_BT_CLASSIC_ENABLED=y", result.stderr)
        self.assertIn("did not resolve the settings above", result.stderr)
        # Before the build, so nothing is compiled against it.
        self.assertEqual(0, len(self.builds()), self.builds())

    def test_a_default_resolved_to_another_value_fails_the_step(self):
        defaults = (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
        resolved = self.directory / "sdkconfig"
        resolved.write_text("".join(
            f"{line}\n".replace("CONFIG_BTDM_CTRL_BR_EDR_MAX_ACL_CONN=3",
                                "CONFIG_BTDM_CTRL_BR_EDR_MAX_ACL_CONN=2")
            for line in defaults
            if line.startswith("CONFIG_") and not line.endswith("=n")))

        result = self.run_step(FAKE_SDKCONFIG=str(resolved))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("CONFIG_BTDM_CTRL_BR_EDR_MAX_ACL_CONN=3", result.stderr)

    def test_the_workspace_can_be_kept_outside_the_work_directory(self):
        kept = self.directory / "kept"

        result = self.run_step(IDF_WORKSPACE=str(kept))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"-v {kept}:{kept}", self.find(" build"))

    def test_the_image_can_be_replaced(self):
        result = self.run_step(IDF_IMAGE="idf-local:test")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for command in self.commands():
            self.assertIn("idf-local:test", command)
            self.assertNotIn(IMAGE, command)

    def test_a_developers_build_directory_is_not_carried_into_the_build(self):
        # Both are git-ignored products of a local build, and a stale
        # configuration would make the check above judge another ESP-IDF's
        # resolution.
        self.run_step()

        copied = self.workspace / "utesp-src"
        self.assertFalse((copied / "build").exists())
        self.assertFalse((copied / "sdkconfig").is_dir())
        self.assertNotIn("./sdkconfig.old", self.compiled(1))

    def test_the_firmware_build_is_not_part_of_the_other_check_sets(self):
        # It needs docker and gigabytes of downloads: `all` must stay the
        # check set a developer and the Test workflow can run.
        text = CI.read_text()
        self.assertIn("all) run_test; run_build ;;", text)
        self.assertIn("firmware-esp32) run_firmware_esp32 ;;", text)
        self.assertIn("firmware-dk|firmware-esp32", text)

    def test_local_ci_can_run_it_against_a_commit(self):
        text = LOCAL_CI.read_text()
        self.assertIn("test|build|all|firmware-dk|firmware-esp32) ;;", text)


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
            self.assertIn("firmware/esp32-classic-peer/**", paths)
            self.assertIn("scripts/ci.sh", paths)
            self.assertIn(".github/workflows/firmware-esp32.yml", paths)
            # The image is gigabytes, so a DK change must not pull it.
            self.assertNotIn("firmware/nrf52840dk/**", paths)

    def test_the_job_runs_the_shared_check_and_leaves_the_tree_clean(self):
        steps = self.workflow["jobs"]["esp32-classic-peer"]["steps"]
        runs = [step.get("run", "") for step in steps]
        self.assertIn("sh scripts/ci.sh firmware-esp32", runs)
        self.assertTrue(any("git status --porcelain" in run for run in runs), runs)

    def test_the_job_has_a_deadline_and_needs_no_history(self):
        job = self.workflow["jobs"]["esp32-classic-peer"]
        self.assertIsInstance(job["timeout-minutes"], int)
        checkout = next(step for step in job["steps"]
                        if "checkout" in step.get("uses", ""))
        # Nothing is compared with another commit, so the whole history the
        # DK's job fetches is not needed here.
        self.assertNotIn("fetch-depth", checkout.get("with", {}))

    def test_the_dk_workflow_does_not_build_the_peer_firmware(self):
        # Paths are filtered per workflow, which is why this is a workflow of
        # its own.
        dk = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "firmware.yml").read_text())
        triggers = dk.get("on", dk.get(True))
        for event in ("push", "pull_request"):
            self.assertNotIn("firmware/esp32-classic-peer/**",
                             triggers[event]["paths"])
