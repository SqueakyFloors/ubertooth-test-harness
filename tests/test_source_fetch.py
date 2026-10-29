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
from ubertooth_harness.manifest import GitRevisionResolver, ManifestError


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
