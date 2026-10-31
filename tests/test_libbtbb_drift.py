"""The warning when a libbtbb pin falls behind the fork's install.sh (#85).

Offline: the fork is a throwaway local git repository, never a clone.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ubertooth_harness import cli, libbtbbpin
from ubertooth_harness.events import (
    REPORT,
    RUN_LOG,
    EventPublisher,
    JsonlEventSink,
    RunEvent,
    TerminalEventSink,
)
from ubertooth_harness.libbtbbpin import (
    ForkPin,
    PinUnreadable,
    check_sources,
    drift_warning,
    fork_pin_at,
    parse_install_sh,
)
from ubertooth_harness.manifest import SourceRevision
from ubertooth_harness.report import write_report


OLD = "84ab41fc99d172e812438bc9188f30a67216f748"
NEW = "0123456789abcdef0123456789abcdef01234567"
URL = "https://github.com/SqueakyFloors/libbtbb.git"
# libbtbb's default branch: that repository's name for it, not ours to
# rename, and what a ref that is a branch name looks like here.
LIBBTBB_BRANCH = "master"

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

    def test_no_pin_is_none(self):
        self.assertIsNone(parse_install_sh("#!/bin/sh\necho hi\n"))

    def test_a_variable_pin_is_reported_not_ignored(self):
        with self.assertRaisesRegex(PinUnreadable, r"not a literal commit \(\$\{REF\}\)"):
            parse_install_sh('LIBBTBB_REF="${REF}"\n')


class DriftWarningTests(unittest.TestCase):

    def test_same_commit_is_quiet_full_or_abbreviated(self):
        pin = ForkPin(URL, OLD)
        self.assertIsNone(drift_warning(pin, OLD, libbtbb_url=URL))
        self.assertIsNone(drift_warning(pin, OLD[:7]))
        self.assertIsNone(drift_warning(ForkPin(URL, OLD[:7]), OLD.upper()))
        # A branch name that resolved to the pinned commit also matches.
        self.assertIsNone(drift_warning(pin, LIBBTBB_BRANCH, libbtbb_commit=OLD))

    def test_a_bump_names_both_commits(self):
        warning = drift_warning(ForkPin(URL, NEW), OLD, fork_label="Ubertooth main")
        self.assertIn(OLD, warning)
        self.assertIn(NEW, warning)
        self.assertIn("Ubertooth main", warning)

    def test_a_moving_ref_is_reported_by_its_commit(self):
        warning = drift_warning(ForkPin(URL, NEW), LIBBTBB_BRANCH, libbtbb_commit=OLD)
        self.assertIn(f"builds libbtbb {OLD}", warning)

    def test_another_repository_is_reported(self):
        warning = drift_warning(
            ForkPin(URL, OLD), OLD, libbtbb_url="https://example.invalid/libbtbb.git"
        )
        self.assertIn("example.invalid", warning)
        self.assertNotIn("pins", warning)

    def test_no_fork_pin_is_quiet(self):
        self.assertIsNone(drift_warning(None, OLD))


class _ForkRepo:
    """A throwaway fork: one commit without the pin, then one with it."""

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


class ForkCheckoutTests(_ForkRepo, unittest.TestCase):
    """fork_pin_at, check_sources and the CLI against a local repo."""

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

    def test_run_sources_status_and_both_commits(self):
        check = check_sources(self._sources(self.pinned, OLD, OLD))
        self.assertEqual("warning", check.status)
        self.assertIn(NEW, check.message)
        self.assertIn(f"Ubertooth {self.pinned[:7]}", check.message)
        self.assertEqual(
            {"message": check.message, "ubertooth_commit": self.pinned,
             "libbtbb_ref": OLD, "libbtbb_commit": OLD,
             "fork_pin_ref": NEW, "fork_pin_url": URL},
            check.details(),
        )
        self.assertEqual("passed", check_sources(self._sources(self.pinned, NEW, NEW)).status)
        # Before the fork pinned libbtbb there is nothing to compare.
        skipped = check_sources(self._sources(self.unpinned, OLD, OLD))
        self.assertEqual("skipped", skipped.status)
        self.assertIn("no LIBBTBB_REF", skipped.message)
        # No libbtbb source: nothing is built from one.
        self.assertIsNone(check_sources(self._sources(self.pinned, OLD, OLD)[:1]))

    def test_run_sources_say_why_the_check_could_not_run(self):
        sources = self._sources(self.pinned, OLD, OLD)
        missing = (SourceRevision("Ubertooth", str(self.repo / "gone"), "", "main",
                                  self.pinned, self.pinned[:7]), sources[1])
        check = check_sources(missing)
        self.assertEqual("unchecked", check.status)
        self.assertIn("libbtbb pin not checked: cannot read install.sh", check.message)
        (self.repo / "install.sh").write_text('LIBBTBB_REF="${PIN}"\n')
        _git(self.repo, "commit", "-q", "-am", "pin from a variable")
        variable = _git(self.repo, "rev-parse", "HEAD")
        check = check_sources(self._sources(variable, OLD, OLD))
        self.assertEqual("unchecked", check.status)
        self.assertIn("not a literal commit", check.message)

    def test_the_check_is_recorded_in_run_jsonl_and_report_md(self):
        out = Path(self.tmp.name) / "run"
        out.mkdir()
        publisher = EventPublisher("run", JsonlEventSink(out / RUN_LOG))
        publisher.emit("run_started", "running", {})
        cli._record_libbtbb_pin(publisher, self._sources(self.pinned, OLD, OLD))
        publisher.emit("run_finished", "passed", {})
        write_report(out)
        events = [json.loads(line) for line in (out / RUN_LOG).read_text().splitlines()]
        pin = events[1]
        self.assertEqual(("libbtbb_pin", "warning"), (pin["event_type"], pin["status"]))
        self.assertEqual((OLD, NEW), (pin["details"]["libbtbb_commit"],
                                      pin["details"]["fork_pin_ref"]))
        report = (out / REPORT).read_text()
        self.assertRegex(report, rf"\| libbtbb pin \| \*\*warning\*\*: .*{OLD}.*{NEW}")

    def test_terminal_prints_the_check_unless_already_shown(self):
        event = RunEvent(1, "run", 2, "2026-10-25T00:00:00.000", "libbtbb_pin",
                         "warning", {"message": "libbtbb pin differs"})
        shown, quiet = io.StringIO(), io.StringIO()
        TerminalEventSink(shown).emit(event)
        TerminalEventSink(quiet, quiet_events={"libbtbb_pin"}).emit(event)
        self.assertEqual("warning: libbtbb pin differs\n", shown.getvalue())
        self.assertEqual("", quiet.getvalue())
        with mock.patch.dict(os.environ, {libbtbbpin.SHOWN_ENV: "1"}):
            self.assertEqual({"libbtbb_pin"}, cli._terminal_sink().quiet_events)
        with mock.patch.dict(os.environ, {libbtbbpin.SHOWN_ENV: ""}):
            self.assertEqual(frozenset(), cli._terminal_sink().quiet_events)
        # The browser live view shows it as well.
        js = (Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
              / "app.js").read_text(encoding="utf-8")
        self.assertIn('kind === "libbtbb_pin" && status !== "passed"', js)

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


class ForkPinRefTests(unittest.TestCase):
    """`--libbtbb-ref fork-pin` builds the libbtbb the commit under test pins.

    Two throwaway repositories: a libbtbb with two commits, and a fork whose
    install.sh pins a different one of them at each of two commits. Resolving
    the same fork-pin value at each commit must give that commit's own pin, so
    a long-lived runner follows a pin bump without being redeployed (#139).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.libbtbb = root / "libbtbb"
        self.libbtbb.mkdir()
        _git(self.libbtbb, "init", "-q", "-b", LIBBTBB_BRANCH)
        self.first, self.second = (
            self._commit(self.libbtbb, "lell.c", text) for text in ("one\n", "two\n")
        )
        # A branch and a tag that both resolve here, so a pin naming one is
        # refused for being a name, not for being unresolvable.
        _git(self.libbtbb, "tag", "v1.0")
        self.fork = root / "ubertooth"
        self.fork.mkdir()
        _git(self.fork, "init", "-q", "-b", "main")
        self.unpinned = self._commit(self.fork, "README", "no pin yet\n")
        self.pins_first = self._commit(self.fork, "install.sh", _install_sh(self.first))
        self.pins_second = self._commit(self.fork, "install.sh", _install_sh(self.second))

    def _commit(self, repo: Path, name: str, text: str) -> str:
        (repo / name).write_text(text)
        _git(repo, "add", name)
        _git(repo, "commit", "-q", "-m", f"{name}: {text.strip()}")
        return _git(repo, "rev-parse", "HEAD")

    def _resolve(self, ubertooth_ref: str, libbtbb_ref: str = libbtbbpin.FORK_PIN):
        args = argparse.Namespace(
            ubertooth_source=self.fork, ubertooth_ref=ubertooth_ref,
            libbtbb_source=self.libbtbb, libbtbb_ref=libbtbb_ref,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sources = cli._resolve_sources(args)
        return sources, err.getvalue()

    def test_each_commit_gets_the_libbtbb_it_pins(self):
        for ubertooth_commit, pinned in (
            (self.pins_first, self.first), (self.pins_second, self.second)
        ):
            with self.subTest(ubertooth=ubertooth_commit[:7]):
                sources, note = self._resolve(ubertooth_commit)
                libbtbb = sources[1]
                self.assertEqual(pinned, libbtbb.commit)
                # Recorded as the commit it resolved to, named fork-pin.
                self.assertEqual(pinned, libbtbb.requested_ref)
                self.assertEqual("fork-pin", libbtbb.ref_name)
                self.assertIn(f"libbtbb ref 'fork-pin' is {pinned}", note)
                self.assertIn(f"Ubertooth {ubertooth_commit[:7]}", note)
                # The run records the pin check as a match, not drift.
                self.assertEqual("passed", check_sources(sources).status)

    def test_a_named_ref_is_still_used_as_given(self):
        sources, note = self._resolve(self.pins_second, libbtbb_ref=LIBBTBB_BRANCH)
        self.assertEqual((LIBBTBB_BRANCH, self.second, None),
                         (sources[1].requested_ref, sources[1].commit, sources[1].ref_name))
        self.assertEqual("", note)

    def test_a_commit_without_a_pin_says_so(self):
        # No install.sh at all, and an install.sh without the pin: the same
        # refusal, since neither commit says which libbtbb it wants.
        no_pin = self._commit(self.fork, "install.sh", "#!/bin/sh\necho hi\n")
        for commit in (self.unpinned, no_pin):
            with self.subTest(ubertooth=commit[:7]):
                with self.assertRaises(cli.ManifestError) as raised:
                    self._resolve(commit)
                self._assertRefusal(raised.exception, commit,
                                    "sets no LIBBTBB_REF")

    def test_a_pin_that_is_not_a_commit_says_so(self):
        variable = self._commit(self.fork, "install.sh", 'LIBBTBB_REF="${REF}"\n')
        with self.assertRaises(cli.ManifestError) as raised:
            self._resolve(variable)
        self._assertRefusal(raised.exception, variable, "not a literal commit")

    def _assertRefusal(self, error, ubertooth_commit: str, problem: str) -> None:
        """Every refusal names the option, the commit, the problem, and both
        ways to name a libbtbb ref instead."""
        message = str(error)
        self.assertIn("--libbtbb-ref fork-pin", message)
        self.assertIn(f"Ubertooth {ubertooth_commit[:7]}", message)
        self.assertIn(problem, message)
        self.assertIn("name a libbtbb ref with --libbtbb-ref", message)
        self.assertIn("redeploy the runner", message)

    def test_a_pin_that_can_move_is_refused_not_resolved(self):
        # fork-pin is a promise of one exact commit, so a branch or tag name
        # is refused rather than built at whatever it points to today.
        for name in (LIBBTBB_BRANCH, "v1.0"):
            with self.subTest(pin=name):
                moving = self._commit(self.fork, "install.sh", _install_sh(name))
                self.assertIsNotNone(
                    _git(self.libbtbb, "rev-parse", "--verify", f"{name}^{{commit}}"),
                    "the name must resolve, so the refusal is about the name",
                )
                with self.assertRaises(cli.ManifestError) as raised:
                    self._resolve(moving)
                self._assertRefusal(raised.exception, moving,
                                    f"sets LIBBTBB_REF={name}, which is not a commit")

    def test_a_pin_the_libbtbb_checkout_lacks_is_reported(self):
        missing = self._commit(self.fork, "install.sh", _install_sh("b" * 40))
        with self.assertRaises(cli.ManifestError) as raised:
            self._resolve(missing)
        # Resolved like any other ref: fetched if possible, else refused.
        self.assertIn("unable to resolve libbtbb ref", str(raised.exception))
        self.assertIn("b" * 40, str(raised.exception))


class CiScriptTests(_ForkRepo, unittest.TestCase):
    """scripts/ci.sh runs the check once in its build job."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_ci_libbtbb_pin_step_warns_with_both_commits_and_passes(self):
        env = {**os.environ, "UBERTOOTH_SOURCE": str(self.repo), "LIBBTBB_REF": OLD,
               "UBERTOOTH_REF": "main", "GITHUB_ACTIONS": "true", "CI_TEMP": self.tmp.name}
        result = subprocess.run(["sh", "scripts/ci.sh", "libbtbb-pin"], cwd=self.ROOT,
                                capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("=== ci: libbtbb pin against the fork's install.sh ===", result.stdout)
        warning = next(line for line in result.stdout.splitlines()
                       if line.startswith("::warning title=libbtbb pin::"))
        self.assertIn(OLD, warning)
        self.assertIn(NEW, warning)

    def test_ci_build_checks_once_before_the_build_and_quiets_the_rest(self):
        text = (self.ROOT / "scripts" / "ci.sh").read_text()
        body = text[text.index("run_build() {"):]
        body = body[:body.index("\n}\n")]
        self.assertEqual(1, body.count('check_libbtbb_pin "$ut"'))
        self.assertLess(body.index('check_libbtbb_pin "$ut"'),
                        body.index("-m ubertooth_harness build"))
        check = text[text.index("check_libbtbb_pin() {"):]
        check = check[:check.index("\n}\n")]
        self.assertIn(f"export {libbtbbpin.SHOWN_ENV}", check)


if __name__ == "__main__":
    unittest.main()
