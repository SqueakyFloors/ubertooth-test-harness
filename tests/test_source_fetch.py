"""GitRevisionResolver fetches a ref its source checkout does not have yet.

Each test builds a bare "origin" and a clone of it in a temporary directory,
then moves origin ahead so the clone is stale, as a runner's checkout is when
the controller lists a commit pushed after the runner last fetched.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ubertooth_harness import manifest
from ubertooth_harness import release
from ubertooth_harness.manifest import GitRevisionResolver, ManifestError

from tests.test_manifest_preflight import FIRMWARE_VERSION_MK, RELEASE_CMAKE


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True,
        text=True, env=GIT_ENV,
    ).stdout.strip()


def commit_file(path, name, text) -> str:
    (Path(path) / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", f"add {name}")
    return git(path, "rev-parse", "HEAD")


class StaleCheckout:
    """origin.git, a work clone that pushes to it, and a stale checkout."""

    def __init__(self, test: unittest.TestCase):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.origin = root / "origin.git"
        self.work = root / "work"
        self.checkout = root / "checkout"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env=GIT_ENV)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)],
                       check=True, env=GIT_ENV, capture_output=True)
        git(self.work, "checkout", "-q", "-b", "main")
        self.first = commit_file(self.work, "a.txt", "one\n")
        git(self.work, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)],
                       check=True, env=GIT_ENV)
        self.head_before = git(self.checkout, "rev-parse", "HEAD")

    def push_newer(self, branch="main", tag=None) -> str:
        if branch != "main":
            git(self.work, "checkout", "-q", "-B", branch)
        commit = commit_file(self.work, f"{branch}.txt", "newer\n")
        git(self.work, "push", "-q", "origin", branch)
        if tag:
            git(self.work, "tag", tag)
            git(self.work, "push", "-q", "origin", tag)
        return commit


class SourceFetchTest(unittest.TestCase):
    def setUp(self):
        self.repos = StaleCheckout(self)
        self.logged = []
        self.resolver = GitRevisionResolver(log=self.logged.append)

    def assertCheckoutUntouched(self):
        checkout = self.repos.checkout
        self.assertEqual(self.repos.head_before, git(checkout, "rev-parse", "HEAD"))
        self.assertEqual("main", git(checkout, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertEqual(self.repos.first, git(checkout, "rev-parse", "refs/heads/main"))
        self.assertEqual("", git(checkout, "status", "--porcelain", "--untracked-files=all"))

    def test_fetches_a_commit_the_checkout_lacks(self):
        newer = self.repos.push_newer()
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.repos.checkout, "cat-file", "-e", f"{newer}^{{commit}}")
        source = self.resolver.resolve("Ubertooth", self.repos.checkout, newer, "main")
        self.assertEqual(newer, source.commit)
        self.assertEqual("main", source.ref_name)
        self.assertEqual(1, len(self.logged))
        self.assertIn("fetching from origin", self.logged[0])
        self.assertCheckoutUntouched()

    def test_fetches_a_branch_or_tag_only_the_remote_has(self):
        branch_commit = self.repos.push_newer("feature", tag="v9")
        by_branch = self.resolver.resolve("Ubertooth", self.repos.checkout, "feature")
        self.assertEqual(branch_commit, by_branch.commit)
        by_tag = self.resolver.resolve("Ubertooth", self.repos.checkout, "v9")
        self.assertEqual(branch_commit, by_tag.commit)
        # The first resolve fetched the tag too, so the second needed no fetch.
        self.assertEqual(1, len(self.logged))
        self.assertCheckoutUntouched()

    def test_no_fetch_when_the_ref_already_resolves(self):
        self.repos.push_newer()
        with mock.patch.object(manifest.subprocess, "run", wraps=subprocess.run) as run:
            source = self.resolver.resolve("Ubertooth", self.repos.checkout, self.repos.first)
        self.assertEqual(self.repos.first, source.commit)
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args[0]])
        self.assertEqual([], self.logged)

    def test_fetch_failure_names_the_fetch_and_why(self):
        newer = self.repos.push_newer()
        git(self.repos.checkout, "remote", "set-url", "origin",
            str(self.repos.origin) + "-missing")
        with self.assertRaises(ManifestError) as caught:
            self.resolver.resolve("Ubertooth", self.repos.checkout, newer)
        message = str(caught.exception)
        self.assertIn(f"unable to resolve Ubertooth ref '{newer}'", message)
        self.assertIn("fetching from origin failed:", message)
        self.assertCheckoutUntouched()

    def test_commit_missing_on_the_remote_too(self):
        missing = "0123456789abcdef" * 2 + "01234567"
        with self.assertRaises(ManifestError) as caught:
            self.resolver.resolve("libbtbb", self.repos.checkout, missing)
        message = str(caught.exception)
        self.assertIn(f"unable to resolve libbtbb ref '{missing}'", message)
        self.assertRegex(message, "fetch(ed|ing) from origin")

    def test_no_remote_is_said_plainly(self):
        git(self.repos.checkout, "remote", "remove", "origin")
        with self.assertRaisesRegex(ManifestError, "no remote to fetch it from"):
            self.resolver.resolve("Ubertooth", self.repos.checkout, "nope")

    def test_dirty_checkout_is_refused_before_any_fetch(self):
        newer = self.repos.push_newer()
        (self.repos.checkout / "a.txt").write_text("edited\n")
        with self.assertRaisesRegex(ManifestError, "dirty"):
            self.resolver.resolve("Ubertooth", self.repos.checkout, newer)
        self.assertEqual([], self.logged)

    def test_fetch_timeout_is_enforced(self):
        newer = self.repos.push_newer()
        resolver = GitRevisionResolver(fetch_timeout_s=5, log=self.logged.append)
        real_run = subprocess.run

        def slow_fetch(command, *args, **kwargs):
            if "fetch" in command:
                self.assertLessEqual(kwargs["timeout"], 5)
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return real_run(command, *args, **kwargs)

        with mock.patch.object(manifest.subprocess, "run", side_effect=slow_fetch):
            with self.assertRaisesRegex(ManifestError, "timed out after 5s"):
                resolver.resolve("Ubertooth", self.repos.checkout, newer)


if __name__ == "__main__":
    unittest.main()


class ReleaseTagFetchTest(unittest.TestCase):
    """A checkout holding the commit but not the release tag it was named for.

    What the version a build reports is worked out from is this checkout's own
    tags, so a missing tag makes a build of an exactly tagged commit report
    the release before it and a count: the bench reported `2026-11-DEV1+25`
    for a build of the commit tagged `2026-11-DEV2`, and the harness predicted
    the same wrong release from the same clone, so nothing said so (#358).
    """

    RELEASE = "2026-11-DEV2"

    def setUp(self):
        self.repos = StaleCheckout(self)
        self.logged = []
        self.resolver = GitRevisionResolver(log=self.logged.append)
        # The files that decide what a build reports, so build_version reads
        # a release name rather than the hash a tree without them stamps.
        for name, text in (("host/cmake/set_release.cmake", RELEASE_CMAKE),
                           ("firmware/version.mk", FIRMWARE_VERSION_MK)):
            target = self.repos.work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            git(self.repos.work, "add", name)
        git(self.repos.work, "commit", "-qm", "the version files")
        git(self.repos.work, "tag", "2026-11-DEV1")
        self.tagged = commit_file(self.repos.work, "b.txt", "tagged\n")
        git(self.repos.work, "tag", self.RELEASE)
        git(self.repos.work, "push", "-q", "origin", "main")
        git(self.repos.work, "push", "-q", "origin", "2026-11-DEV1")
        git(self.repos.work, "push", "-q", "origin", self.RELEASE)
        # The commit arrives without the tag on it, which is what a checkout
        # that fetches one branch has: `--no-tags` leaves the auto-followed
        # tags behind. The earlier release tag is taken, as a checkout that
        # has been around has it.
        git(self.repos.checkout, "fetch", "-q", "--no-tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main")
        git(self.repos.checkout, "fetch", "-q", "--no-tags", "origin",
            "refs/tags/2026-11-DEV1:refs/tags/2026-11-DEV1")

    def assertCheckoutUntouched(self):
        checkout = self.repos.checkout
        self.assertEqual("main", git(checkout, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertEqual("", git(checkout, "status", "--porcelain",
                                 "--untracked-files=all"))

    def test_the_tag_is_fetched_so_the_release_is_the_one_it_names(self):
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.repos.checkout, "rev-parse", "--verify", self.RELEASE)

        source = self.resolver.resolve(
            "Ubertooth", self.repos.checkout, self.tagged, self.RELEASE
        )

        self.assertEqual(self.tagged, source.commit)
        self.assertEqual(self.RELEASE, source.build_version)
        self.assertEqual(self.RELEASE, source.expected_revision)
        self.assertEqual(1, len(self.logged), self.logged)
        self.assertIn("fetching the tag", self.logged[0])
        # Without the fetch it is the release before it and the distance, and
        # the build's own clone of this checkout would say the same.
        self.assertEqual("2026-11-DEV1+1", self._release_without_the_tag())

    def _release_without_the_tag(self) -> str:
        """What a checkout with the commit and not the tag would report."""
        git(self.repos.checkout, "tag", "-d", self.RELEASE)
        return release.build_version(self.repos.checkout, self.tagged).value

    def test_a_release_tag_already_here_is_not_fetched_again(self):
        git(self.repos.checkout, "fetch", "-q", "origin",
            f"refs/tags/{self.RELEASE}:refs/tags/{self.RELEASE}")
        self.logged.clear()

        with mock.patch.object(manifest.subprocess, "run",
                               wraps=subprocess.run) as run:
            source = self.resolver.resolve(
                "Ubertooth", self.repos.checkout, self.tagged, self.RELEASE
            )

        self.assertEqual(self.RELEASE, source.build_version)
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args[0]])
        self.assertEqual([], self.logged)

    def test_a_ref_name_that_is_not_a_release_tag_costs_no_fetch(self):
        # Every run names the branch or tag it was started from, and most of
        # those are not release tags: a fetch for each would be paid by runs
        # that have no use for it. Only the name the release comes from is
        # worth one.
        with mock.patch.object(manifest.subprocess, "run",
                               wraps=subprocess.run) as run:
            source = self.resolver.resolve(
                "Ubertooth", self.repos.checkout, self.tagged, "2026-11-DEV2-rc"
            )

        self.assertEqual(self.tagged, source.commit)
        self.assertFalse([c for c in run.call_args_list if "fetch" in c.args[0]])
        self.assertEqual([], self.logged)

    def test_a_fetch_that_fails_refuses_the_run_rather_than_mislabel_it(self):
        # Carrying on would report the release before it and a count: a
        # version that is not the one the run was named for, agreed on by the
        # build and by every expectation derived from the same checkout, so
        # reported by nobody as wrong.
        git(self.repos.checkout, "remote", "set-url", "origin",
            str(self.repos.origin) + "-missing")

        with self.assertRaises(ManifestError) as caught:
            self.resolver.resolve(
                "Ubertooth", self.repos.checkout, self.tagged, self.RELEASE
            )

        message = str(caught.exception)
        self.assertIn(f"ref '{self.RELEASE}' names a release", message)
        self.assertIn("fetching it from origin failed", message)
        self.assertCheckoutUntouched()

    def test_a_remote_without_the_tag_is_refused_too(self):
        git(self.repos.origin, "tag", "-d", self.RELEASE)

        with self.assertRaises(ManifestError) as caught:
            self.resolver.resolve(
                "Ubertooth", self.repos.checkout, self.tagged, self.RELEASE
            )

        message = str(caught.exception)
        self.assertIn(f"ref '{self.RELEASE}' names a release", message)
        # git refuses a refspec for a tag the remote has not got, so that is
        # what the reason carries; the "has no it either" wording is the
        # backstop for a remote that answers the fetch and still has none.
        self.assertIn("fetching it from origin failed", message)

    def test_a_checkout_with_no_remote_is_refused_by_name(self):
        git(self.repos.checkout, "remote", "remove", "origin")

        with self.assertRaisesRegex(
            ManifestError, "no remote to fetch it from"
        ):
            self.resolver.resolve(
                "Ubertooth", self.repos.checkout, self.tagged, self.RELEASE
            )

    def test_the_tag_arrives_when_the_commit_had_to_be_fetched_too(self):
        # The deployed-runner path: the controller resolves the tag and sends
        # the commit, and the checkout has neither. The fetch that brings a
        # named commit asks for that commit alone and brings no tag with it,
        # so the tag is asked for after the commit resolved either way.
        later = commit_file(self.repos.work, "c.txt", "later\n")
        git(self.repos.work, "tag", "2026-12-R1")
        git(self.repos.work, "push", "-q", "origin", "main")
        git(self.repos.work, "push", "-q", "origin", "2026-12-R1")
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.repos.checkout, "cat-file", "-e", f"{later}^{{commit}}")

        source = self.resolver.resolve(
            "Ubertooth", self.repos.checkout, later, "2026-12-R1"
        )

        self.assertEqual(later, source.commit)
        self.assertEqual("2026-12-R1", source.build_version)

    def test_a_tag_naming_another_commit_is_said_and_not_refused(self):
        # A run may be asked for a commit past a tag, and the release a build
        # reports then carries the distance, which is right. The two came
        # from different places, so it may not go unsaid.
        past = commit_file(self.repos.work, "d.txt", "past\n")
        git(self.repos.work, "push", "-q", "origin", "main")

        source = self.resolver.resolve(
            "Ubertooth", self.repos.checkout, past, self.RELEASE
        )

        self.assertEqual(f"{self.RELEASE}+1", source.build_version)
        self.assertTrue(
            any("names" in line and "this run resolved" in line
                for line in self.logged), self.logged
        )
