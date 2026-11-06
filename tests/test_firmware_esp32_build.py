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
# how a test drops a setting the defaults file asks for or turns one on that
# it asks to stay off. $FAKE_CONFIGURE_OUTPUT and $FAKE_BUILD_OUTPUT are
# files of lines those two stages print, for the warning check.
# $FAKE_BIN_PAYLOAD is the image every build leaves. $FAKE_CONFIGURE_EXIT and
# $FAKE_DOCKER_EXIT fail those two stages, like sources that do not configure
# or do not compile.
FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    printf '%s\\n' "$*" >> "$FAKE_LOG"
    all=$*
    project=${all##*-C }
    project=${project%% *}
    out=${all##*-B }
    out=${out%% *}
    # The step quotes those two paths inside the string it hands `bash -c`,
    # so that a workspace with a space in its name reaches idf.py whole; the
    # shell in the container strips the quotes, and so does this.
    project=$(printf '%s' "$project" | sed "s/^'//; s/'$//")
    out=$(printf '%s' "$out" | sed "s/^'//; s/'$//")
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
            [ -z "${FAKE_CONFIGURE_OUTPUT:-}" ] || cat "$FAKE_CONFIGURE_OUTPUT"
            code=${FAKE_CONFIGURE_EXIT:-0}
            [ "$code" = 0 ] || exit "$code"
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
        # Quoted inside the string the container's shell runs, so a
        # workspace with a space in its name reaches idf.py whole.
        self.assertIn(f"-C '{self.workspace}/utesp-src'", build)
        self.assertIn(f"-B '{self.workspace}/utesp-build'", build)
        self.assertEqual(
            (FIRMWARE / "main" / "peer.h").read_text(),
            (self.workspace / "utesp-src" / "main" / "peer.h").read_text())
        self.assertIn("./bluedroid/utesp_afh.c", self.compiled(1))
        # The version a peer's shell reports and a run checks it by.
        self.assertIn(f"firmware version: {targets.PEER_FIRMWARE}", result.stdout)
        self.assertIn("firmware-esp32 passed", result.stdout)

    def test_the_repository_is_mounted_read_only(self):
        # A precaution rather than a need: the sources are copied on the
        # host, so no command reads under the mount. A command that did
        # could not write into the working tree, and nothing under firmware/
        # is generated.
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(self.commands())
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

    def test_a_configure_failure_fails_the_step(self):
        result = self.run_step(FAKE_CONFIGURE_EXIT="2")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("did not configure with ESP-IDF", result.stderr)
        self.assertEqual(0, len(self.builds()), self.builds())

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
        self.assertIn("the toolchain warned about the lines above in "
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

    def test_a_configure_warning_about_the_peers_own_files_fails_the_step(self):
        # The one line that catches a default this step cannot check for
        # itself: a setting that was withdrawn and so disables nothing. The
        # line names this directory's own defaults file.
        warning = (f"warning: unknown kconfig symbol "
                   f"'CONFIG_BT_HFP_WBS_ENABLE' assigned to 'n' in "
                   f"{self.workspace}/utesp-src/sdkconfig.defaults")
        result = self.run_step(
            FAKE_CONFIGURE_OUTPUT=self.build_output(warning))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(warning, result.stderr)
        self.assertIn("the toolchain warned about the lines above in "
                      "firmware/esp32-classic-peer", result.stderr)

    def test_a_default_that_resolved_on_instead_of_off_fails_the_step(self):
        # The polarity Kconfig does not write as a line: BLE is off in this
        # firmware so the whole radio goes to Classic, and an image with it
        # on is not the one these sources describe.
        defaults = (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
        resolved = self.directory / "sdkconfig"
        resolved.write_text("".join(
            f"{line}\n" if not line.endswith("=n")
            else f"{line[:-1]}y\n" if line.startswith("CONFIG_BT_BLE_ENABLED")
            else f"# {line.split('=')[0]} is not set\n"
            for line in defaults if line.startswith("CONFIG_")))

        result = self.run_step(FAKE_SDKCONFIG=str(resolved))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("CONFIG_BT_BLE_ENABLED=n (it resolved to "
                      "CONFIG_BT_BLE_ENABLED=y)", result.stderr)
        self.assertIn("did not resolve the settings above", result.stderr)
        self.assertEqual(0, len(self.builds()), self.builds())

    def test_a_default_asked_to_stay_off_and_left_out_passes(self):
        # Kconfig writes a disabled option as a comment, and a disabled
        # member of a choice may not be written at all, so demanding a line
        # for it would fail on a correct resolution.
        defaults = (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
        resolved = self.directory / "sdkconfig"
        resolved.write_text("".join(
            f"{line}\n" for line in defaults
            if line.startswith("CONFIG_") and not line.endswith("=n")))

        result = self.run_step(FAKE_SDKCONFIG=str(resolved))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_configuration_that_was_not_written_fails_with_its_own_reason(self):
        absent = self.directory / "no-such-sdkconfig"

        result = self.run_step(FAKE_SDKCONFIG=str(absent))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("configuring left no sdkconfig", result.stderr)

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
        settings = [line for line in
                    (FIRMWARE / "sdkconfig.defaults").read_text().splitlines()
                    if line.startswith("CONFIG_")]
        off = [line for line in settings if line.endswith("=n")]
        # Both polarities are counted, so a defaults file that grew a setting
        # this step does not look at shows up here.
        self.assertIn(f"all {len(settings) - len(off)} asked for by value, "
                      f"and {len(off)} asked to stay off", result.stdout)
        self.assertTrue(off, settings)

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

    def test_the_build_compiles_the_configuration_this_run_resolved(self):
        # The copy the build compiles holds the sdkconfig configuring just
        # wrote, and nothing from an earlier build of it.
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        files = self.compiled(1)
        self.assertIn("./sdkconfig", files)
        self.assertNotIn("./sdkconfig.old", files)
        self.assertEqual([], [name for name in files
                              if name.startswith("./build/")], files)

    def test_a_local_builds_leftovers_are_removed_before_configuring(self):
        # A developer's tree may hold these three, all git-ignored products
        # of a local build: a stale configuration would make the check above
        # judge another ESP-IDF's resolution, and a stale image could satisfy
        # the version check. They cannot be seeded here, because the step
        # copies from this repository's own working tree and seeding them
        # would dirty it, so this reads the script.
        text = CI.read_text()
        self.assertIn(
            'rm -rf "$src/build" "$src/sdkconfig" "$src/sdkconfig.old"', text)
        # And the build directory itself, so no check can be satisfied by the
        # image an earlier run with the same IDF_WORKSPACE left.
        self.assertIn('rm -rf "$src" "$build"', text)

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
