"""The warning when a libbtbb pin falls behind the fork's install.sh (#85).

Offline: the fork is a throwaway local git repository, never a clone.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ubertooth_harness import libbtbbpin
from ubertooth_harness.libbtbbpin import (
    ForkPin,
    drift_warning,
    fork_pin_at,
    parse_install_sh,
    warning_for_sources,
)
from ubertooth_harness.manifest import SourceRevision


OLD = "84ab41fc99d172e812438bc9188f30a67216f748"
NEW = "0123456789abcdef0123456789abcdef01234567"
URL = "https://github.com/SqueakyFloors/libbtbb.git"

# The shape of the fork's install.sh: the pin at top level, an override in
# the option parser that must not be read as the pin.
INSTALL_SH = """#!/bin/sh
LIBBTBB_URL="@URL@"
LIBBTBB_REF="@REF@"

while [ $# -gt 0 ]; do
	case "$1" in
		--libbtbb-url) shift; LIBBTBB_URL="${1:-}" ;;
		--libbtbb-ref) shift; LIBBTBB_REF="${1:-}" ;;
	esac
done
"""

def _install_sh(ref: str) -> str:
    return INSTALL_SH.replace("@URL@", URL).replace("@REF@", ref)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env={**os.environ, **GIT_ENV},
    ).stdout.strip()


class ParseTests(unittest.TestCase):

    def test_reads_the_top_level_pin_not_the_option_override(self):
        self.assertEqual(ForkPin(URL, OLD), parse_install_sh(_install_sh(OLD)))

    def test_unquoted_and_single_quoted_values(self):
        self.assertEqual(
            ForkPin("u", OLD),
            parse_install_sh(f"LIBBTBB_URL='u'\nLIBBTBB_REF={OLD}  # pin\n"),
        )

    def test_no_pin_or_a_variable_pin_is_none(self):
        self.assertIsNone(parse_install_sh("#!/bin/sh\necho hi\n"))
        self.assertIsNone(parse_install_sh('LIBBTBB_REF="${REF}"\n'))


class DriftWarningTests(unittest.TestCase):

    def test_same_commit_is_quiet_full_or_abbreviated(self):
        pin = ForkPin(URL, OLD)
        self.assertIsNone(drift_warning(pin, OLD, libbtbb_url=URL))
        self.assertIsNone(drift_warning(pin, OLD[:7]))
        self.assertIsNone(drift_warning(ForkPin(URL, OLD[:7]), OLD.upper()))
        # A branch name that resolved to the pinned commit also matches.
        self.assertIsNone(drift_warning(pin, "master", libbtbb_commit=OLD))

    def test_a_bump_names_both_commits(self):
        warning = drift_warning(ForkPin(URL, NEW), OLD, fork_label="Ubertooth main")
        self.assertIn(OLD, warning)
        self.assertIn(NEW, warning)
        self.assertIn("Ubertooth main", warning)

    def test_a_moving_ref_is_reported_by_its_commit(self):
        warning = drift_warning(ForkPin(URL, NEW), "master", libbtbb_commit=OLD)
        self.assertIn(f"builds libbtbb {OLD}", warning)

    def test_another_repository_is_reported(self):
        warning = drift_warning(
            ForkPin(URL, OLD), OLD, libbtbb_url="https://example.invalid/libbtbb.git"
        )
        self.assertIn("example.invalid", warning)
        self.assertNotIn("pins", warning)

    def test_no_fork_pin_is_quiet(self):
        self.assertIsNone(drift_warning(None, OLD))


class ForkCheckoutTests(unittest.TestCase):
    """fork_pin_at, warning_for_sources and the CLI against a local repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "ubertooth"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README").write_text("before the pin\n")
        _git(self.repo, "add", "README")
        _git(self.repo, "commit", "-q", "-m", "no pin yet")
        self.unpinned = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "install.sh").write_text(_install_sh(NEW))
        _git(self.repo, "add", "install.sh")
        _git(self.repo, "commit", "-q", "-m", "pin libbtbb")
        self.pinned = _git(self.repo, "rev-parse", "HEAD")

    def _sources(self, commit, libbtbb_ref, libbtbb_commit):
        return (
            SourceRevision("Ubertooth", str(self.repo), "", "main", commit, commit[:7]),
            SourceRevision("libbtbb", "/x", "", libbtbb_ref, libbtbb_commit,
                           libbtbb_commit[:7]),
        )

    def test_fork_pin_at_a_commit(self):
        self.assertEqual(ForkPin(URL, NEW), fork_pin_at(self.repo, "main"))
        self.assertIsNone(fork_pin_at(self.repo, self.unpinned))

    def test_fork_pin_at_a_bad_ref_raises(self):
        with self.assertRaises(RuntimeError):
            fork_pin_at(self.repo, "no-such-ref")

    def test_run_sources_warn_only_on_a_mismatch(self):
        warning = warning_for_sources(self._sources(self.pinned, OLD, OLD))
        self.assertIn(NEW, warning)
        self.assertIn(f"Ubertooth {self.pinned[:7]}", warning)
        self.assertIsNone(warning_for_sources(self._sources(self.pinned, NEW, NEW)))
        # Before the fork pinned libbtbb there is nothing to compare.
        self.assertIsNone(warning_for_sources(self._sources(self.unpinned, OLD, OLD)))
        # No libbtbb source: nothing is built from one.
        self.assertIsNone(warning_for_sources(self._sources(self.pinned, OLD, OLD)[:1]))

    def test_run_sources_in_a_missing_checkout_are_quiet(self):
        sources = self._sources(self.pinned, OLD, OLD)
        missing = (SourceRevision("Ubertooth", str(self.repo / "gone"), "", "main",
                                  self.pinned, self.pinned[:7]), sources[1])
        self.assertIsNone(warning_for_sources(missing))

    def _main(self, *argv, github=False):
        out, err = io.StringIO(), io.StringIO()
        env = {"GITHUB_ACTIONS": "true"} if github else {"GITHUB_ACTIONS": ""}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = libbtbbpin.main(["--ubertooth-source", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_cli_match(self):
        code, out, err = self._main("--ubertooth-ref", "main", "--libbtbb-url", URL,
                                    "--libbtbb-ref", NEW)
        self.assertEqual((0, ""), (code, err))
        self.assertIn("matches", out)

    def test_cli_mismatch_warns_and_still_exits_zero(self):
        code, out, err = self._main("--ubertooth-ref", "main", "--libbtbb-ref", OLD,
                                    github=True)
        self.assertEqual(0, code)
        self.assertIn(OLD, err)
        self.assertIn(NEW, err)
        self.assertTrue(out.startswith("::warning title=libbtbb pin::"), out)

    def test_cli_without_a_pin_or_ref_warns(self):
        code, out, err = self._main("--ubertooth-ref", self.unpinned, "--libbtbb-ref", OLD)
        self.assertEqual((0, ""), (code, out))
        self.assertIn("no LIBBTBB_REF", err)
        code, _, err = self._main("--ubertooth-ref", "no-such-ref", "--libbtbb-ref", OLD)
        self.assertEqual(0, code)
        self.assertIn("cannot read install.sh", err)


class CiScriptTests(unittest.TestCase):

    def test_ci_build_runs_the_check_with_the_pin(self):
        text = (Path(__file__).resolve().parents[1] / "scripts" / "ci.sh").read_text()
        self.assertIn("-m ubertooth_harness.libbtbbpin", text)
        self.assertIn('--libbtbb-url "$LIBBTBB_URL"', text)


if __name__ == "__main__":
    unittest.main()
