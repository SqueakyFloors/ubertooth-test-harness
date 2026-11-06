"""The version a build of one commit reports (release.py, #352).

Each case is a scratch git checkout of its own, laid out the way the fork lays
its sources out, so the answers are about a history written for the case rather
than about whatever the harness checkout happens to be. The file that decides
the string is written here in the two forms the fork has had, which is what
says which style a commit is: a release name, or the commit hash older
commits stamp.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from ubertooth_harness import release


# The identity goes on every git command and nothing is signed, so these do
# not depend on whoever runs them.
GIT_ID = (
    "-c", "user.name=test", "-c", "user.email=test@example.invalid",
    "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false",
)

# The release-name form, as the fork's host/cmake/set_release.cmake has it:
# the three tag globs and the floor a tree with no release tag reports.
RELEASE_FILE = """\
set(LATEST_RELEASE "2020-12-R1")
set(RELEASE_TAG_MATCH
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-R[0-9]*"
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-RC[0-9]*"
\t--match "[0-9][0-9][0-9][0-9]-[0-9][0-9]-DEV[0-9]*")
"""
# The commit-hash form, which is what both repositories had before.
COMMIT_FILE = """\
set(LATEST_RELEASE "2020-12-R1")
set(RELEASE_STRING "git-${GIT_VERSION}${DIRTY_FLAG}")
"""


def git(directory, *arguments):
    done = subprocess.run(
        ["git", *GIT_ID, *arguments], cwd=directory, capture_output=True,
        text=True, check=False,
    )
    if done.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} in {directory}: {done.stderr.strip()}"
        )
    return done.stdout.strip()


class Checkout:
    """A scratch checkout with the version file where that project keeps it."""

    def __init__(self, root: Path, kind: str = "Ubertooth",
                 text: str = RELEASE_FILE):
        self.path = root
        self.kind = kind
        self.name = release.VERSION_FILES[kind]
        root.mkdir(parents=True, exist_ok=True)
        git(root, "init", "-q", "-b", "main")
        self.write_version_file(text)
        self.commit("the version file")

    def write_version_file(self, text: str) -> None:
        target = self.path / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(self.path, "add", self.name)

    def write(self, name: str, text: str) -> None:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(self.path, "add", name)

    def commit(self, message: str) -> str:
        (self.path / "file.txt").write_text(message + "\n", encoding="utf-8")
        git(self.path, "add", "file.txt")
        git(self.path, "commit", "-q", "-m", message)
        return self.head

    def tag(self, name: str) -> None:
        git(self.path, "tag", name)

    @property
    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD")

    def version(self, commit=None):
        return release.build_version(
            self.path, commit or self.head, kind=self.kind
        )


class ReleaseNameTests(unittest.TestCase):
    """What a commit whose build names a release reports."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_a_release_tag_reports_the_release_alone(self):
        tree = Checkout(self.root / "tagged")
        tree.commit("work")
        tree.tag("2026-11-RC1")

        version = tree.version()
        self.assertEqual("2026-11-RC1", version.value)
        self.assertEqual(release.RELEASE_STYLE, version.style)
        self.assertTrue(version.names_release)

    def test_commits_past_a_release_report_the_distance(self):
        tree = Checkout(self.root / "ahead")
        tree.tag("2026-11-DEV1")
        for number in range(3):
            tree.commit(f"work {number}")

        self.assertEqual("2026-11-DEV1+3", tree.version().value)
        # Each of the three tags a release goes out under counts.
        for name in ("2026-12-R1", "2027-01-RC2", "2027-02-DEV9"):
            tree.tag(name)
            tree.commit(f"after {name}")
            self.assertEqual(f"{name}+1", tree.version().value, name)

    def test_an_older_commit_is_measured_from_its_own_release(self):
        """The commit asked about decides, not whatever HEAD happens to be."""
        tree = Checkout(self.root / "history")
        tree.tag("2026-11-DEV1")
        first = tree.commit("one")
        tree.commit("two")
        tree.tag("2026-11-DEV2")
        tree.commit("three")

        self.assertEqual("2026-11-DEV1+1", tree.version(first).value)
        self.assertEqual("2026-11-DEV2+1", tree.version().value)

    def test_no_release_tag_in_reach_says_the_distance_is_unknown(self):
        """A clone fetched without tags, and a shallow one.

        A count from the start of history is not the distance from a release,
        so the floor the file names is reported with "+unknown" after it.
        """
        tree = Checkout(self.root / "untagged")
        tree.commit("work")

        self.assertEqual("2020-12-R1+unknown", tree.version().value)

    def test_the_floor_comes_from_the_checkout_s_own_file(self):
        tree = Checkout(self.root / "floor",
                        text=RELEASE_FILE.replace("2020-12-R1", "2026-10-R1"))
        tree.commit("work")

        self.assertEqual("2026-10-R1+unknown", tree.version().value)

    def test_a_tag_that_is_not_a_release_name_is_not_read_as_one(self):
        """"2026-11-R1-local" matches the first glob and is not a release."""
        tree = Checkout(self.root / "nearly")
        tree.commit("work")
        tree.tag("2026-11-R1-local")

        self.assertEqual("2020-12-R1+unknown", tree.version().value)

    def test_another_project_s_tags_are_not_releases(self):
        tree = Checkout(self.root / "other-tags")
        tree.commit("work")
        tree.tag("v1.2.3")
        tree.tag("shmoocon_merge")

        self.assertEqual("2020-12-R1+unknown", tree.version().value)

    def test_libbtbb_reads_its_own_file(self):
        tree = Checkout(self.root / "libbtbb", kind="libbtbb")
        tree.tag("2020-12-R1")
        tree.commit("work")

        self.assertEqual("2020-12-R1+1", tree.version().value)

    def test_libbtbb_takes_the_tag_its_globs_matched(self):
        """Its own file makes no second check on the name git found.

        The fork's two files do, so `2026-11-R1-local` is no release there;
        libbtbb's asks git with the same globs and takes what comes back, and
        being faithful to each file matters more than the rule being the same
        in both.
        """
        tree = Checkout(self.root / "libbtbb-odd-tag", kind="libbtbb")
        tree.commit("work")
        tree.tag("2026-11-R1-local")
        tree.commit("more")

        self.assertEqual("2026-11-R1-local+1", tree.version().value)

    def test_the_two_fork_files_have_to_name_one_floor(self):
        """With no release tag in reach the floor is the whole answer.

        The tools read it from one file and the firmware and bootloader from
        another, so a tree that bumped one alone would report two versions
        and neither could be predicted.
        """
        tree = Checkout(self.root / "two-floors")
        tree.write(release.FIRMWARE_VERSION_FILE["Ubertooth"],
                   "UBERTOOTH_LATEST_RELEASE := 2026-10-R1\n")
        head = tree.commit("a firmware floor of its own")

        with self.assertRaises(release.ReleaseVersionError) as caught:
            tree.version(head)
        self.assertIn("2020-12-R1", str(caught.exception))
        self.assertIn("2026-10-R1", str(caught.exception))

        # The same tree with the two in step derives the floor as before.
        tree.write(release.FIRMWARE_VERSION_FILE["Ubertooth"],
                   "UBERTOOTH_LATEST_RELEASE := 2020-12-R1\n")
        agreed = tree.commit("the floors agree")
        self.assertEqual("2020-12-R1+unknown", tree.version(agreed).value)


class CommitStyleTests(unittest.TestCase):
    """What a commit from before the release names reports: git-<hash>."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_a_commit_hash_file_reports_the_abbreviated_commit(self):
        tree = Checkout(self.root / "old", text=COMMIT_FILE)
        head = tree.commit("work")
        # A release tag in reach changes nothing: that commit's build does
        # not look for one.
        tree.tag("2026-11-R1")

        version = tree.version()
        short = git(tree.path, "rev-parse", "--short", head)
        self.assertEqual(f"git-{short}", version.value)
        self.assertEqual(release.COMMIT_STYLE, version.style)
        self.assertFalse(version.names_release)

    def test_a_commit_with_no_version_file_reports_a_hash(self):
        """Every commit from before the file stamped one."""
        tree = Checkout(self.root / "ancient", text=COMMIT_FILE)
        git(tree.path, "rm", "-q", tree.name)
        head = tree.commit("no version file")

        short = git(tree.path, "rev-parse", "--short", head)
        self.assertEqual(f"git-{short}", tree.version().value)

    def test_the_caller_s_abbreviation_is_used_when_given(self):
        tree = Checkout(self.root / "short", text=COMMIT_FILE)
        head = tree.commit("work")

        version = release.build_version(
            tree.path, head, kind="Ubertooth", short_commit="ABCDEF1"
        )
        self.assertEqual("git-abcdef1", version.value)

    def test_a_repository_the_harness_does_not_build_reports_a_hash(self):
        tree = Checkout(self.root / "unknown-kind", text=COMMIT_FILE)
        head = tree.commit("work")

        short = git(tree.path, "rev-parse", "--short", head)
        version = release.build_version(tree.path, head, kind="something else")
        self.assertEqual(f"git-{short}", version.value)


class RefusalTests(unittest.TestCase):
    """Where guessing the style would compare the wrong string."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_a_file_that_derives_the_string_a_third_way_is_refused(self):
        tree = Checkout(self.root / "third-way",
                        text='set(RELEASE_STRING "whatever")\n')
        head = tree.commit("work")

        with self.assertRaises(release.ReleaseVersionError) as caught:
            tree.version(head)
        self.assertIn("does not recognize", str(caught.exception))
        self.assertIn(release.VERSION_FILES["Ubertooth"], str(caught.exception))

    def test_a_tree_whose_host_file_moved_is_refused(self):
        """firmware/version.mk and no host file: the host file moved.

        Reading that absence as "an older commit" would compare every version
        in the run against a commit hash the build never stamps.
        """
        tree = Checkout(self.root / "moved", text=COMMIT_FILE)
        tree.write(release.FIRMWARE_VERSION_FILE["Ubertooth"],
                   "UBERTOOTH_LATEST_RELEASE := 2020-12-R1\n")
        git(tree.path, "rm", "-q", tree.name)
        head = tree.commit("the host file moved")

        with self.assertRaises(release.ReleaseVersionError) as caught:
            tree.version(head)
        self.assertIn("firmware/version.mk", str(caught.exception))
        self.assertIn("has to follow it", str(caught.exception))


class PartsTests(unittest.TestCase):
    """The pieces, where a whole checkout is more than the case needs."""

    def test_the_style_markers(self):
        self.assertEqual(
            release.RELEASE_STYLE,
            release.version_style(RELEASE_FILE, "Ubertooth", "a" * 40),
        )
        self.assertEqual(
            release.COMMIT_STYLE,
            release.version_style(COMMIT_FILE, "Ubertooth", "a" * 40),
        )

    def test_the_release_tag_form_accepts_only_release_names(self):
        for name in ("2026-11-R1", "2026-11-RC2", "2026-11-DEV10",
                     "2020-12-R1"):
            self.assertTrue(release.RELEASE_TAG_FORM.match(name), name)
        for name in ("2026-11-R1-local", "2026-11-R", "26-11-R1", "v1.2.3",
                     "2026-11-BETA1", "2026-11-R1\n2026-11-R2"):
            self.assertIsNone(release.RELEASE_TAG_FORM.match(name), name)

    def test_the_floor_falls_back_where_the_file_names_none(self):
        self.assertEqual(release.DEFAULT_LATEST_RELEASE,
                         release.latest_release("set(SOMETHING_ELSE 1)\n"))

    def test_a_dirty_checkout_never_reaches_the_derivation(self):
        """The `*` a modified tree gets is never predicted, and need not be.

        Both fork derivations append it, and nothing here does, because a run
        refuses a dirty source checkout before it resolves anything and
        builds from a clean clone of it. So a reported `*` is a mismatch to
        report rather than something to expect. This pins the refusal, which
        is what makes that safe.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = Checkout(root / "dirty")
            tree.tag("2026-11-DEV1")
            tree.commit("work")
            # Clean, so far: the release name, with no marker.
            self.assertEqual("2026-11-DEV1+1", tree.version().value)

            (tree.path / "file.txt").write_text("edited\n", encoding="utf-8")

            from ubertooth_harness.manifest import (
                GitRevisionResolver, ManifestError,
            )
            with self.assertRaises(ManifestError) as caught:
                GitRevisionResolver().resolve("Ubertooth", tree.path, "HEAD")
            self.assertIn("dirty", str(caught.exception))

    def test_a_path_git_will_not_answer_about_is_refused(self):
        """"No such file" and "no answer at all" mean opposite things.

        Both come back from one failed command, so the commit itself is asked
        for: a caller that passed a path git cannot read is refused rather
        than handed the commit hash an older build would have stamped.
        """
        with tempfile.TemporaryDirectory() as directory:
            os.makedirs(Path(directory) / "empty")
            with self.assertRaises(release.ReleaseVersionError) as caught:
                release.build_version(
                    Path(directory) / "empty", "a" * 40, kind="Ubertooth",
                    short_commit="aaaaaaa",
                )
        self.assertIn("would not answer", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
