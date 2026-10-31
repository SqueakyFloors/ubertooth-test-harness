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
import re
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
# The path the shell works with: a `sh` started in ROOT reports the physical
# directory, which differs from ROOT where a parent is a symlink (/tmp on
# macOS), and that is what the script mounts and passes to `west build`.
REPO = Path(os.path.realpath(ROOT))
CI = ROOT / "scripts" / "ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "firmware.yml"
FIRMWARE = ROOT / "firmware" / "nrf52840dk"
NCS_VERSION = (FIRMWARE / "NCS_VERSION").read_text().strip()
BOARD = "nrf52840dk/nrf52840"
# Whether a comparison against a git ref can be tested here: the suite also
# runs from a tree exported with git archive (scripts/local-ci.sh), which has
# no history to resolve a ref against.
HAS_HISTORY = subprocess.run(
    ("git", "-C", str(ROOT), "cat-file", "-e", "HEAD:firmware/nrf52840dk/src/roles.h"),
    capture_output=True,
).returncode == 0 and not subprocess.run(
    ("git", "-C", str(ROOT), "status", "--porcelain", "firmware/nrf52840dk"),
    capture_output=True, text=True,
).stdout.strip()

# Writes the image west build would produce when the command is a build, and
# records every command. $FAKE_HEX_PAYLOAD is the image every build leaves;
# $FAKE_HEX_PAYLOAD_<n> replaces it for the nth build, which is how a test
# gives the comparison build an image of its own. With $FAKE_DOCKER_EXIT it
# fails like a build that does not compile.
FAKE_DOCKER = textwrap.dedent("""\
    #!/bin/sh
    printf '%s\\n' "$*" >> "$FAKE_LOG"
    case "$*" in
        *"west build"*)
            [ "${FAKE_DOCKER_EXIT:-0}" = 0 ] || exit "$FAKE_DOCKER_EXIT"
            builds=$(cat "$FAKE_BUILDS" 2>/dev/null || echo 0)
            builds=$((builds + 1))
            printf '%s\\n' "$builds" > "$FAKE_BUILDS"
            eval "payload=\\${FAKE_HEX_PAYLOAD_$builds:-\\$FAKE_HEX_PAYLOAD}"
            # The sources this build compiled, for a test to look at: the
            # command ends with the directory west build is given.
            all=$*
            sources=${all##* -s }
            (cd "${sources%% *}" && find . -type f | sort) > "$FAKE_BUILDS.$builds"
            mkdir -p "$(dirname "$FAKE_HEX")"
            cp "$payload" "$FAKE_HEX" ;;
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
        env = dict(
            os.environ,
            PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            CI_TEMP=str(self.work),
            FAKE_LOG=str(self.log),
            FAKE_BUILDS=str(self.directory / "builds"),
            FAKE_HEX=str(self.workspace / "utdk-build" / "zephyr" / "zephyr.hex"),
            FAKE_HEX_PAYLOAD=self.image("image", version=version),
        )
        env.update(environment)
        # DK_BASE_REF and DK_BASE_SRC are the environment's, not this
        # developer's: a test asks for a comparison by name.
        for name in ("DK_BASE_REF", "DK_BASE_SRC"):
            if name not in environment:
                env.pop(name, None)
        return subprocess.run(
            ("sh", str(CI), "firmware-dk"), cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=120,
        )

    def image(self, name, *, filler=b"\x00", version=nrfdk.FIRMWARE_VERSION):
        """An image file carrying ``version``, told apart by its filler."""
        return str(hex_file(self.directory / f"{name}.hex",
                            filler * 8 + version.encode() + filler * 8))

    def base_sources(self, version=None, sdk=None):
        """A copy of the DK firmware sources to compare against, with another
        UTDK_FW_VERSION or NCS_VERSION where one is asked for.

        It also carries a file of its own, so a test can tell which sources a
        build compiled.
        """
        base = self.directory / "base"
        shutil.copytree(FIRMWARE, base)
        (base / "src" / "base-only.c").write_text("/* only in the base */\n")
        if version:
            header = base / "src" / "roles.h"
            header.write_text(re.sub(
                r'(#define UTDK_FW_VERSION ")[^"]+', rf"\g<1>{version}",
                header.read_text()))
        if sdk:
            (base / "NCS_VERSION").write_text(f"{sdk}\n")
        return base

    def compiled(self, build: int) -> list[str]:
        """The files the nth build found in the sources it was given."""
        listing = self.directory / f"builds.{build}"
        files = listing.read_text().splitlines() if listing.exists() else []
        # An empty listing would make every assertion on it vacuous.
        self.assertIn("./src/roles.h", files, f"build {build}: {files}")
        return files

    def commands(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def builds(self):
        return [command for command in self.commands() if "west build" in command]

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
        # A copy of this tree's sources, which is where a build of another
        # commit's sources goes too, so the two images can be compared.
        self.assertIn(f"-s {self.workspace}/utdk-src", build)
        self.assertEqual(
            (FIRMWARE / "src" / "roles.h").read_text(),
            (self.workspace / "utdk-src" / "src" / "roles.h").read_text())
        # The image check is the harness's own, and it names the version.
        self.assertIn(f"firmware version: {nrfdk.FIRMWARE_VERSION}", result.stdout)
        self.assertIn("firmware-dk passed", result.stdout)

    def test_the_repository_is_mounted_read_only(self):
        # A build that could write into the working tree would leave the
        # repository dirty; nothing under firmware/ is generated.
        self.run_step()

        for command in self.commands():
            self.assertIn(f"-v {REPO}:{REPO}:ro", command)

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

    def test_a_workspace_for_this_version_is_reused_and_stamped(self):
        (self.workspace / ".west").mkdir(parents=True)
        (self.workspace / ".ncs-version").write_text(f"{NCS_VERSION}\n")

        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("west init", "\n".join(self.commands()))
        self.assertIn(f"reusing the {NCS_VERSION} workspace", result.stdout)

    def test_a_new_workspace_records_the_version_it_was_made_for(self):
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(NCS_VERSION,
                         (self.workspace / ".ncs-version").read_text().strip())

    def test_a_workspace_made_for_another_version_is_refused(self):
        # `west update` moves the projects to the manifest's revisions, but the
        # manifest repository stays where `west init --mr` left it: reusing
        # such a workspace would build sources the firmware does not pin.
        (self.workspace / ".west").mkdir(parents=True)
        (self.workspace / ".ncs-version").write_text("v3.0.0\n")

        result = self.run_step()

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("was made for nRF Connect SDK v3.0.0", result.stderr)
        self.assertNotIn("west build", "\n".join(self.commands()))

    def test_an_unstamped_workspace_is_refused(self):
        (self.workspace / ".west").mkdir(parents=True)

        result = self.run_step()

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("was made for nRF Connect SDK unknown", result.stderr)

    def test_the_workspace_can_be_kept_outside_the_work_directory(self):
        kept = self.directory / "kept"

        result = self.run_step(NCS_WORKSPACE=str(kept),
                               FAKE_HEX=str(kept / "utdk-build/zephyr/zephyr.hex"))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"-v {kept}:{kept}", self.find("west build"))

    def test_without_a_base_nothing_is_compared(self):
        # The schedule, a push, and a developer's own run have no other
        # commit to compare against, and the step says what that means.
        result = self.run_step()

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(1, len(self.builds()), self.builds())
        self.assertIn("no base sources given", result.stdout)
        self.assertIn("a missing version bump would not show up", result.stdout)

    def test_an_image_the_base_sources_also_build_passes(self):
        # A comment or formatting change: the sources differ, the image does
        # not, and asking for a version bump would reflash every board for
        # nothing (#210).
        base = self.base_sources()

        result = self.run_step(DK_BASE_SRC=str(base))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        builds = self.builds()
        self.assertEqual(2, len(builds), builds)
        self.assertIn(f"-s {self.workspace}/utdk-src", builds[1])
        self.assertIn("the image is the one", result.stdout)
        # The second build compiled the base sources, not this tree's again.
        self.assertNotIn("./src/base-only.c", self.compiled(1))
        self.assertIn("./src/base-only.c", self.compiled(2))
        # The workspace is left holding this tree's sources.
        self.assertFalse((self.workspace / "utdk-src" / "src" / "base-only.c").exists())

    def test_a_changed_image_without_a_version_bump_fails(self):
        # What #210 is about: `nrfdk.py ensure` would leave the old image on
        # every board that already reports this version.
        base = self.base_sources()
        other = self.image("other", filler=b"\x11")

        result = self.run_step(DK_BASE_SRC=str(base),
                               FAKE_HEX_PAYLOAD_2=other,
                               FAKE_HEX_PAYLOAD_4=other)

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(f"UTDK_FW_VERSION is still {nrfdk.FIRMWARE_VERSION}",
                      result.stderr)
        self.assertIn("bump it in firmware/nrf52840dk/src/roles.h", result.stderr)
        # Each side was built twice, to show the difference is the change and
        # not the build: this tree, the base, then each again.
        self.assertEqual(4, len(self.builds()), self.builds())
        self.assertNotIn("./src/base-only.c", self.compiled(3))
        self.assertIn("./src/base-only.c", self.compiled(4))
        self.assertNotIn("firmware-dk passed", result.stdout)

    def test_a_version_bump_needs_no_comparison(self):
        base = self.base_sources(version="utdk-1.0.0")

        result = self.run_step(DK_BASE_SRC=str(base))

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(1, len(self.builds()), self.builds())
        self.assertIn(
            f"UTDK_FW_VERSION moved from utdk-1.0.0 to {nrfdk.FIRMWARE_VERSION}",
            result.stdout)

    def test_a_tree_that_does_not_repeat_itself_is_reported_as_that(self):
        # This tree's own build is the one that changed between its two
        # builds, so the verdict is the build and not a missing bump.
        base = self.base_sources()

        result = self.run_step(DK_BASE_SRC=str(base),
                               FAKE_HEX_PAYLOAD_2=self.image("two", filler=b"\x11"),
                               FAKE_HEX_PAYLOAD_3=self.image("three", filler=b"\x22"))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("two builds of this tree gave different images", result.stderr)
        self.assertNotIn("bump it", result.stderr)
        self.assertEqual(3, len(self.builds()), self.builds())

    def test_a_base_that_does_not_repeat_itself_is_reported_as_that(self):
        base = self.base_sources()
        other = self.image("two", filler=b"\x11")

        result = self.run_step(DK_BASE_SRC=str(base),
                               FAKE_HEX_PAYLOAD_2=other,
                               FAKE_HEX_PAYLOAD_4=self.image("four", filler=b"\x22"))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(f"two builds of {base} gave different images", result.stderr)
        self.assertNotIn("bump it", result.stderr)
        self.assertEqual(4, len(self.builds()), self.builds())

    def test_an_sdk_bump_without_a_version_bump_fails(self):
        # Both builds use the SDK this tree pins, so the comparison cannot
        # see what a new toolchain did to the image; it builds a new image,
        # which a board already running this version would never be flashed
        # with.
        base = self.base_sources(sdk="v3.0.0")

        result = self.run_step(DK_BASE_SRC=str(base))

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(f"nRF Connect SDK moved from v3.0.0 to {NCS_VERSION}",
                      result.stderr)
        self.assertIn("bump it in firmware/nrf52840dk/src/roles.h", result.stderr)
        self.assertEqual(1, len(self.builds()), self.builds())

    def test_a_directory_of_sources_wins_over_a_ref(self):
        base = self.base_sources()

        result = self.run_step(DK_BASE_SRC=str(base), DK_BASE_REF="no-such-ref")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"the image against {base}", result.stdout)
        self.assertNotIn("no-such-ref", result.stdout + result.stderr)

    @unittest.skipUnless(
        HAS_HISTORY,
        "needs this repository's git history, with firmware/nrf52840dk clean")
    def test_the_base_sources_can_come_from_a_git_ref(self):
        result = self.run_step(DK_BASE_REF="HEAD")

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("the image is the one HEAD builds", result.stdout)
        # HEAD's sources, exported from this repository and built.
        self.assertTrue(
            (self.workspace / "base-firmware" / "firmware" / "nrf52840dk"
             / "src" / "roles.h").exists(), result.stdout)
        self.assertEqual(2, len(self.builds()), self.builds())

    def test_a_ref_that_does_not_resolve_fails_the_step(self):
        # The ref was asked for, so a missing one means the history the
        # comparison needs is not there; skipping would pass a pull request
        # nobody checked.
        result = self.run_step(DK_BASE_REF="no-such-ref")

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("does not resolve to a tree holding", result.stderr)
        self.assertEqual(1, len(self.builds()), self.builds())

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

    def test_the_job_compares_the_image_with_the_pull_requests_base(self):
        steps = self.workflow["jobs"]["nrf52840dk"]["steps"]
        checkout = next(step for step in steps if "checkout" in step.get("uses", ""))
        # The base branch has to be there to be built.
        self.assertEqual(0, checkout["with"]["fetch-depth"])
        build = next(step for step in steps
                     if step.get("run") == "sh scripts/ci.sh firmware-dk")
        # The whole expression: the commit a pull request is against, and
        # nothing on a push, the schedule, or a dispatch, where there is no
        # other commit to compare against. The base commit rather than the
        # branch, which can advance while the job waits.
        self.assertEqual("${{ github.event.pull_request.base.sha }}",
                         build["env"]["DK_BASE_REF"])

    def test_the_job_runs_the_shared_check_and_leaves_the_tree_clean(self):
        steps = self.workflow["jobs"]["nrf52840dk"]["steps"]
        runs = [step.get("run", "") for step in steps]
        self.assertIn("sh scripts/ci.sh firmware-dk", runs)
        self.assertTrue(any("git status --porcelain" in run for run in runs), runs)
