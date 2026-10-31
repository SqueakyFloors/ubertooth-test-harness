"""External per-board lock files taken with each lease (issue #123)."""

import asyncio
import errno
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import leases
from ubertooth_harness.cli import main
from ubertooth_harness.report import _Run, _setup_rows
from ubertooth_harness.runner_service import RunnerConfigError, RunnerService
from tests.test_leases import A, B, HARDWARE, holder

# Another tool on the host: holds a board's own lock file (writing what it
# likes into it) until killed.
_LAB_HOLDER = textwrap.dedent("""\
    import fcntl, os, sys, time
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.ftruncate(fd, 0)
    os.write(fd, sys.argv[2].encode())
    print("held", flush=True)
    time.sleep(60)
    """)

# A harness process that leases boards with the external template until killed.
_LEASE_HOLDER = textwrap.dedent("""\
    import sys, time
    from ubertooth_harness import leases
    leases.acquire(
        [leases.Board("UT_%d" % i, key) for i, key in enumerate(sys.argv[3:])],
        leases.holder_info(run_id="run-held", owner="harness-agent", purpose="t"),
        directory=sys.argv[1], external_template=sys.argv[2],
    )
    print("held", flush=True)
    time.sleep(60)
    """)


def _locked_elsewhere(path: Path) -> bool:
    """True when another open file holds an exclusive or shared flock on path."""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return True
        raise
    finally:
        os.close(fd)
    return False


class _Fixture(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name).resolve()
        self.lease_dir = self.root / "leases"
        self.lab = self.root / "lab-locks"
        self.lab.mkdir()
        self.template = str(self.lab / "board-{serial}.lock")
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
        self._dir.cleanup()

    def ext(self, board) -> Path:
        return self.lab / f"board-{board.key}.lock"

    def spawn(self, code, *args):
        child = subprocess.Popen(
            [sys.executable, "-c", code, *map(str, args)], stdout=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        self.assertEqual("held", child.stdout.readline().strip())
        return child

    def acquire(self, boards, name="agent", **kwargs):
        kwargs.setdefault("external_template", self.template)
        return leases.acquire(boards, holder(name), directory=self.lease_dir, **kwargs)


class TemplateTests(unittest.TestCase):

    def test_unset_or_empty_means_no_external_lock(self):
        self.assertIsNone(leases.external_lock_template(None, environ={}))
        self.assertIsNone(leases.external_lock_template("", environ={}))
        # '' on the command line turns the environment's template off.
        env = {leases.EXTERNAL_LOCK_ENV: "/locks/{serial}.lock"}
        self.assertIsNone(leases.external_lock_template("", environ=env))

    def test_the_option_wins_over_the_environment(self):
        env = {leases.EXTERNAL_LOCK_ENV: "/env/{serial}.lock"}
        self.assertEqual("/env/{serial}.lock", leases.external_lock_template(None, environ=env))
        self.assertEqual(
            "/opt/board-{serial}.lock",
            leases.external_lock_template("/opt/board-{serial}.lock", environ=env),
        )

    def test_home_is_expanded(self):
        self.assertEqual(
            os.path.expanduser("~/locks/board-{serial}.lock"),
            leases.external_lock_template("~/locks/board-{serial}.lock", environ={}),
        )

    def test_bad_templates_are_refused(self):
        for value, complaint in (
            ("locks/board-{serial}.lock", "absolute path"),
            ("/locks/board.lock", "exactly once"),
            ("/locks/{serial}-{serial}.lock", "exactly once"),
            ("/locks/{serial}-{alias}.lock", "no brace"),
            ("/locks/{serial}}.lock", "no brace"),
            ("/locks/{SERIAL}.lock", "exactly once"),
            ("/locks/{serial}/", "must name a file"),
        ):
            with self.subTest(value), self.assertRaises(leases.LeaseError) as caught:
                leases.external_lock_template(value, environ={})
            self.assertIn(complaint, str(caught.exception))

    def test_the_path_uses_the_full_serial(self):
        self.assertEqual(
            Path(f"/locks/board-{A.key}.lock"),
            leases.external_lock_path("/locks/board-{serial}.lock", A),
        )


class ExternalLockTests(_Fixture):

    def test_a_board_held_outside_the_harness_is_refused_and_named(self):
        self.spawn(_LAB_HOLDER, self.ext(A), "soak script pid 4242\nsecond line\n")
        started = time.monotonic()
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A], host_lock=True)
        self.assertLess(time.monotonic() - started, 5)
        message = str(caught.exception)
        self.assertIn(f"UT_0 is in use: held outside the harness: {self.ext(A)}", message)
        self.assertIn("(soak script pid 4242)", message)
        self.assertNotIn("second line", message)
        self.assertIn("--wait-for-boards", message)
        # All or nothing: no lease and no host lock were kept.
        self.assertFalse(leases.board_states([A], directory=self.lease_dir)[0]["held"])
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])

    def test_a_json_holder_record_is_described_and_an_empty_file_is_fine(self):
        record = json.dumps({"owner": "lab-wrapper", "pid": 77})
        self.spawn(_LAB_HOLDER, self.ext(A), record)
        self.spawn(_LAB_HOLDER, self.ext(B), "")
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A])
        self.assertIn("(lab-wrapper, pid 77)", str(caught.exception))
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([B])
        self.assertTrue(str(caught.exception).endswith(
            f"held outside the harness: {self.ext(B)}; "
            "use --wait-for-boards MINUTES to wait for it"
        ))

    def test_waits_for_an_external_lock_up_to_the_deadline(self):
        child = self.spawn(_LAB_HOLDER, self.ext(A), "lab")
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 5 and child.poll() is None:
                child.kill()
                child.wait()

        with self.acquire([A], wait_s=60, clock=lambda: now[0], sleep=sleep) as held:
            self.assertGreaterEqual(now[0], 5)
            self.assertEqual(
                [(self.ext(A), True), (self.lease_dir / f"{A.key}.lock", False)],
                [(lease.path, lease.external) for lease in held.leases],
            )

    def test_a_lease_blocks_the_external_lock_until_released(self):
        Path(self.ext(A)).write_text("lab's own content\n")
        held = self.acquire([A])
        self.assertTrue(_locked_elsewhere(self.ext(A)))
        self.assertTrue(_locked_elsewhere(self.lease_dir / f"{A.key}.lock"))
        held.release()
        self.assertFalse(_locked_elsewhere(self.ext(A)))
        # The harness never writes to the lab's file.
        self.assertEqual("lab's own content\n", self.ext(A).read_text())

    def test_another_process_blocks_on_the_external_lock_while_leased(self):
        child = self.spawn(_LEASE_HOLDER, self.lease_dir, self.template, A.key)
        waiter = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys\n"
             "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('got', flush=True)\n",
             str(self.ext(A))],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                waiter.wait(timeout=1)
            child.terminate()
            self.assertEqual("got", waiter.communicate(timeout=30)[0].strip())
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.wait()

    def test_kill_9_releases_the_external_lock_and_the_lease(self):
        child = self.spawn(_LEASE_HOLDER, self.lease_dir, self.template, A.key, B.key)
        self.assertTrue(_locked_elsewhere(self.ext(A)))
        self.assertTrue(_locked_elsewhere(self.ext(B)))
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
        self.assertFalse(_locked_elsewhere(self.ext(A)))
        self.assertFalse(_locked_elsewhere(self.ext(B)))
        self.acquire([A, B]).release()

    def test_a_busy_external_lock_releases_everything_already_taken(self):
        self.spawn(_LAB_HOLDER, self.ext(B), "lab")
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A, B], host_lock=True)
        self.assertIn("UT_1 is in use", str(caught.exception))
        self.assertFalse(_locked_elsewhere(self.ext(A)))
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])
        self.assertFalse(leases.board_states([A], directory=self.lease_dir)[0]["held"])

    def test_a_busy_lease_releases_the_external_locks_it_took(self):
        with leases.acquire([B], holder("lease-only"), directory=self.lease_dir):
            with self.assertRaises(leases.LeaseError) as caught:
                self.acquire([A, B])
            self.assertIn("lease-only", str(caught.exception))
            self.assertFalse(_locked_elsewhere(self.ext(A)))
            self.assertFalse(_locked_elsewhere(self.ext(B)))

    def test_lock_order_external_locks_then_host_lock_then_leases(self):
        order = []
        real = leases._try_lock

        def recording(path, mode, **kwargs):
            order.append(str(Path(path).relative_to(self.root)))
            return real(path, mode, **kwargs)

        with mock.patch.object(leases, "_try_lock", recording):
            self.acquire([B, A], host_lock=True).release()
        # The takeover lock first (it orders this attempt against a takeover
        # of a stale lease and is no resource of its own), then the lab's
        # files, the host lock, and the leases.
        self.assertEqual([
            f"leases/{leases.TAKEOVER_LOCK}",
            f"lab-locks/board-{A.key}.lock", f"lab-locks/board-{B.key}.lock",
            "leases/host-power.lock", f"leases/{A.key}.lock", f"leases/{B.key}.lock",
        ], order)

    def test_a_missing_external_file_is_created_0644_but_not_its_directory(self):
        previous = os.umask(0o022)
        try:
            self.acquire([A]).release()
        finally:
            os.umask(previous)
        self.assertEqual(0o644, stat.S_IMODE(self.ext(A).stat().st_mode))
        missing = str(self.root / "no-such-dir" / "board-{serial}.lock")
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A], external_template=missing)
        self.assertIn("does not exist", str(caught.exception))
        self.assertFalse((self.root / "no-such-dir").exists())

    def test_a_created_file_is_0644_whatever_the_umask_is(self):
        # A tight umask must not make the lab's own scripts unable to read
        # (or lock) the file the harness created for them.
        previous = os.umask(0o077)
        try:
            self.acquire([A]).release()
        finally:
            os.umask(previous)
        self.assertEqual(0o644, stat.S_IMODE(self.ext(A).stat().st_mode))

    def test_a_path_that_is_not_a_regular_file_is_refused(self):
        # A FIFO would make the open itself wait forever.
        os.mkfifo(self.ext(A))
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A])
        self.assertIn("is not a regular file", str(caught.exception))
        # The listing must not hang on it either.
        state = leases.board_states(
            [A], directory=self.lease_dir, external_template=self.template
        )[0]
        self.assertTrue(state["held"])
        self.assertIn("not a regular file", state["external"]["error"])

    def test_two_paths_that_are_one_file_are_refused(self):
        # A hard link to another board's file: flock is per open file
        # description, so the process would wait for its own lock.
        self.ext(A).write_text("")
        os.link(self.ext(A), self.ext(B))
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A, B])
        self.assertIn("same file", str(caught.exception))
        self.assertFalse(_locked_elsewhere(self.ext(A)))  # nothing left held

    def test_an_external_lock_hard_linked_to_a_lease_file_is_refused(self):
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        own = self.lease_dir / f"{A.key}.lock"
        own.write_text("")
        os.link(own, self.ext(A))
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A])
        self.assertIn("same file", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_an_unreadable_lease_file_is_not_called_an_external_lock(self):
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        own = self.lease_dir / f"{A.key}.lock"
        own.write_text("")
        own.chmod(0o000)
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A])
        self.assertIn("cannot lease boards in", str(caught.exception))
        self.assertNotIn("external board lock", str(caught.exception))
        self.assertFalse(_locked_elsewhere(self.ext(A)))  # nothing left held

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_an_unopenable_external_file_stops_the_command_at_once(self):
        self.ext(A).write_text("")
        self.ext(A).chmod(0o000)
        started = time.monotonic()
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A], wait_s=600.0)
        self.assertLess(time.monotonic() - started, 30.0)  # it does not wait
        message = str(caught.exception)
        self.assertIn("UT_0", message)
        self.assertIn(str(self.ext(A)), message)
        self.assertIn("read access", message)

    @unittest.skipIf(os.geteuid() == 0, "root reads every file")
    def test_boards_takes_an_unopenable_external_file_as_held(self):
        self.ext(A).write_text("")
        self.ext(A).chmod(0o000)
        state = leases.board_states(
            [A], directory=self.lease_dir, external_template=self.template
        )[0]
        self.assertTrue(state["held"])
        self.assertTrue(state["external"]["held"])
        self.assertIn("Permission denied", state["external"]["error"])

    def test_an_existing_mode_is_kept_and_a_read_only_file_is_still_locked(self):
        self.ext(A).write_text("")
        self.ext(A).chmod(0o444)
        with self.acquire([A]):
            self.assertTrue(_locked_elsewhere(self.ext(A)))
        self.assertEqual(0o444, stat.S_IMODE(self.ext(A).stat().st_mode))

    def test_the_template_cannot_point_at_the_lease_files(self):
        with self.assertRaises(leases.LeaseError) as caught:
            self.acquire([A], external_template=str(self.lease_dir / "{serial}.lock"))
        self.assertIn("harness's own lease file", str(caught.exception))

    def test_without_a_template_nothing_outside_the_lease_dir_is_touched(self):
        self.acquire([A], external_template=None).release()
        self.assertEqual([], list(self.lab.iterdir()))

    def test_the_manifest_records_the_external_locks(self):
        with self.acquire([A], host_lock=True) as held:
            manifest = held.to_manifest()
        self.assertEqual(
            [("UT_0", True), ("host lock", False), ("UT_0", False)],
            [(item["resource"], item.get("external", False)) for item in manifest],
        )
        self.assertEqual(str(self.ext(A)), manifest[0]["lock"])
        rows = dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000",
            "details": {"manifest": {"leases": manifest}},
        }])))
        self.assertEqual(f"UT_0 {self.ext(A)} (held for the whole run)",
                         rows["External board locks"])
        self.assertTrue(rows["Board leases"].startswith("UT_0 since "))

    def test_board_states_show_a_board_held_only_outside_the_harness(self):
        self.spawn(_LAB_HOLDER, self.ext(A), "soak pid 9")
        states = leases.board_states(
            [A, B], directory=self.lease_dir, external_template=self.template
        )
        self.assertEqual([True, False], [s["held"] for s in states])
        self.assertEqual({}, states[0]["holder"])
        self.assertEqual(
            {"lock": str(self.ext(A)), "held": True, "holder": "soak pid 9"},
            states[0]["external"],
        )
        # The probe creates no lab file for a board nobody locked.
        self.assertFalse(self.ext(B).exists())
        # Without the template the lab's lock is invisible, as before.
        self.assertFalse(leases.board_states([A], directory=self.lease_dir)[0]["held"])

    def test_no_deadlock_against_a_wrapper_that_takes_external_locks_then_leases(self):
        # The lab's wrapper: blocking flock on each board's file in the order
        # it was given, then a plain lease (it has no template), sometimes
        # with the host lock. flock is per open file, so threads contend
        # exactly as processes do.
        errors = []
        stop = time.monotonic() + 60

        def wrapper(boards, host_lock):
            try:
                for _ in range(10):
                    fds = []
                    try:
                        for board in boards:
                            fd = os.open(self.ext(board), os.O_RDWR | os.O_CREAT, 0o644)
                            fds.append(fd)
                            fcntl.flock(fd, fcntl.LOCK_EX)
                        with leases.acquire(
                            boards, holder("wrapper"), directory=self.lease_dir,
                            wait_s=30, host_lock=host_lock,
                        ):
                            time.sleep(0.005)
                    finally:
                        for fd in fds:
                            os.close(fd)
                    time.sleep(0.01)
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        def harness(boards, host_lock):
            try:
                for _ in range(10):
                    with self.acquire(boards, wait_s=30, host_lock=host_lock):
                        time.sleep(0.005)
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [
            threading.Thread(target=wrapper, args=([A, B], False)),
            threading.Thread(target=wrapper, args=([B], True)),
            threading.Thread(target=harness, args=([B, A], True)),
            threading.Thread(target=harness, args=([A, B], False)),
            threading.Thread(target=harness, args=([A], True)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=max(0, stop - time.monotonic()))
        self.assertFalse([t for t in threads if t.is_alive()], "deadlocked")
        self.assertEqual([], errors)
        for board in (A, B):
            self.assertFalse(_locked_elsewhere(self.ext(board)))
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])


class ExternalLockCliTests(_Fixture):

    def setUp(self):
        super().setUp()
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(HARDWARE)

    def _main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env or {}),
            redirect_stdout(out), redirect_stderr(err),
        ):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _boards(self, *extra, env=None):
        return self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            *extra, env=env,
        )

    def test_boards_shows_a_board_held_only_by_its_external_lock(self):
        self.spawn(_LAB_HOLDER, self.ext(A), "soak script pid 4242")
        code, out, _err = self._boards("--external-board-lock", self.template)
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertEqual(
            f"UT_0         held  outside the harness: {self.ext(A)} (soak script pid 4242)",
            lines[0],
        )
        self.assertRegex(lines[1], r"^UT_1 +free$")
        # The environment variable does the same; '' turns it off again.
        env = {leases.EXTERNAL_LOCK_ENV: self.template}
        _code, raw, _err = self._boards("--json", env=env)
        states = json.loads(raw)
        self.assertTrue(states[0]["held"])
        self.assertTrue(states[0]["external"]["held"])
        _code, out, _err = self._boards("--external-board-lock", "", env=env)
        self.assertRegex(out.splitlines()[0], r"^UT_0 +free$")

    def test_boards_names_the_lease_holder_when_both_are_held(self):
        with self.acquire([B], "agent-7"):
            _code, out, _err = self._boards("--external-board-lock", self.template)
        self.assertRegex(out.splitlines()[1], r"^UT_1 +held  agent-7, run run-agent-7")

    def test_lease_holds_the_external_lock_while_the_command_runs(self):
        probe = (
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
            "try:\n"
            "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "    print('free')\n"
            "except BlockingIOError:\n"
            "    print('held')\n"
        )
        # The command inherits the locks; a fresh open of the file conflicts.
        result = subprocess.run(
            [sys.executable, "-m", "ubertooth_harness", "lease",
             "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
             "--external-board-lock", self.template,
             "UT_0", "--", sys.executable, "-c", probe, str(self.ext(A))],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("held", result.stdout.strip())
        self.assertFalse(_locked_elsewhere(self.ext(A)))

    def test_lease_is_refused_while_the_external_lock_is_held(self):
        self.spawn(_LAB_HOLDER, self.ext(A), "lab")
        code, _out, err = self._main(
            "lease", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            "UT_0", "--", sys.executable, "-c", "raise SystemExit(0)",
            env={leases.EXTERNAL_LOCK_ENV: self.template},
        )
        self.assertEqual(2, code)
        self.assertIn(f"held outside the harness: {self.ext(A)} (lab)", err)

    def test_a_bad_template_fails_every_command_before_it_does_anything(self):
        bad = "relative/board-{serial}.lock"
        for argv in (
            ("boards", "--hardware", str(self.hardware)),
            ("lease", "--hardware", str(self.hardware), "UT_0", "--", "true"),
            ("run-suite", "--plan", "p.yaml", "--suite", "s", "--hardware", "h.yaml",
             "--host", "H", "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
             "--output", str(self.root / "runs"), "--dry-run"),
        ):
            with self.subTest(argv[0]):
                code, _out, err = self._main(
                    argv[0], "--lease-dir", str(self.lease_dir), *argv[1:],
                    env={leases.EXTERNAL_LOCK_ENV: bad},
                )
                self.assertEqual(2, code)
                self.assertIn("must be an absolute path", err)
        self.assertFalse(self.lease_dir.exists())


class RunnerExternalLockTests(unittest.TestCase):

    def setUp(self):
        from tests.test_runner_service import RunnerFixture

        self._dir = tempfile.TemporaryDirectory()
        self.fixture = RunnerFixture(Path(self._dir.name))

    def tearDown(self):
        self._dir.cleanup()

    def test_a_bad_template_stops_the_runner_at_start(self):
        config = self.fixture.config("ws://127.0.0.1:1/ws/runner",
                                     external_board_lock="board-{serial}.lock")
        with self.assertRaises(RunnerConfigError):
            RunnerService(config)
        with mock.patch.dict(os.environ, {leases.EXTERNAL_LOCK_ENV: "/x/{serial}{"}):
            with self.assertRaises(RunnerConfigError):
                RunnerService(self.fixture.config("ws://127.0.0.1:1/ws/runner"))

    def test_the_template_is_passed_to_every_run_suite_child(self):
        from tests.test_runner_service import StubController, StubEnumerator, run_request

        recorded = []
        real = asyncio.create_subprocess_exec

        async def recording(*argv, **kwargs):
            recorded.append(argv)
            return await real(*argv, **kwargs)

        async def scenario(value):
            async with StubController() as controller:
                service = RunnerService(
                    self.fixture.config(controller.url, external_board_lock=value),
                    enumerator=StubEnumerator(), log=lambda _line: None,
                )
                task = asyncio.create_task(service.run_forever())
                try:
                    await controller.next("register")
                    await controller.send(run_request())
                    await controller.next("run_accepted")
                    await controller.next("run_finished", skip=("heartbeat", "event"))
                finally:
                    service.request_stop()
                    await asyncio.wait_for(task, 60)

        with mock.patch("asyncio.create_subprocess_exec", recording):
            asyncio.run(scenario("~/locks/board-{serial}.lock"))
            asyncio.run(scenario(""))
            asyncio.run(scenario(None))
        flags = [[a for a in argv if a.startswith("--external-board-lock")] for argv in recorded]
        self.assertEqual([
            ["--external-board-lock=" + os.path.expanduser("~/locks/board-{serial}.lock")],
            ["--external-board-lock="],
            [],
        ], flags)


if __name__ == "__main__":
    unittest.main()
