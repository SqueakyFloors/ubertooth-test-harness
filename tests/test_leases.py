import fcntl
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from ubertooth_harness import leases
from ubertooth_harness.cli import main
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.topology import PowerIsolationAction

A = leases.Board("UT_0", "aaaa0000aaaa0000aaaa0000aaaa0000")
B = leases.Board("UT_1", "bbbb1111bbbb1111bbbb1111bbbb1111")
PLATFORM = "macos" if sys.platform == "darwin" else "linux"
HARDWARE = textwrap.dedent(f"""\
    resources:
      Host_Local:
        type: Host
        platform: {PLATFORM}
        roles: [test target]
        defaults: {{Ubertooth: UT_0}}
      UT_0:
        type: Ubertooth
        attached to: Host_Local
        identity: {{serial: {A.key}}}
      UT_1:
        type: Ubertooth
        attached to: Host_Local
        identity: {{serial: {B.key}}}
    """)


def holder(name):
    return leases.holder_info(run_id=f"run-{name}", owner=name, purpose="test")


class LeaseTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_a_held_board_is_refused_and_the_error_names_its_holder(self):
        with leases.acquire([A], holder("agent-1"), directory=self.dir):
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire([A], holder("agent-2"), directory=self.dir)
        message = str(caught.exception)
        self.assertIn("UT_0 is in use", message)
        self.assertIn("agent-1", message)
        self.assertIn("run run-agent-1", message)
        self.assertIn("--wait-for-boards", message)

    def test_release_frees_the_board(self):
        leases.acquire([A], holder("agent-1"), directory=self.dir).release()
        leases.acquire([A], holder("agent-2"), directory=self.dir).release()

    def test_leasing_is_all_or_nothing(self):
        with leases.acquire([B], holder("agent-1"), directory=self.dir):
            with self.assertRaises(leases.LeaseError):
                leases.acquire([A, B], holder("agent-2"), directory=self.dir)
            # The failed attempt must not keep UT_0.
            leases.acquire([A], holder("agent-3"), directory=self.dir).release()

    def test_waits_for_a_busy_board_up_to_the_deadline(self):
        first = leases.acquire([A], holder("agent-1"), directory=self.dir)
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 5:
                first.release()

        got = leases.acquire(
            [A], holder("agent-2"), directory=self.dir, wait_s=60,
            clock=lambda: now[0], sleep=sleep,
        )
        self.assertEqual(1, len(got.leases))
        got.release()

    def test_wait_gives_up_at_the_deadline(self):
        with leases.acquire([A], holder("agent-1"), directory=self.dir):
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("agent-2"), directory=self.dir, wait_s=30,
                    clock=lambda: now[0], sleep=sleep,
                )
            self.assertIn("after waiting 30s", str(caught.exception))
            self.assertGreaterEqual(now[0], 30)

    def test_a_killed_holder_never_leaves_a_stale_lease(self):
        code = (
            "import sys, time\n"
            "from ubertooth_harness import leases\n"
            f"leases.acquire([leases.Board('UT_0', '{A.key}')], "
            "leases.holder_info(run_id='r', owner='doomed', purpose='t'), "
            "directory=sys.argv[1])\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.dir)], stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual("held", child.stdout.readline().strip())
            state = leases.board_states([A], directory=self.dir)[0]
            self.assertTrue(state["held"])
            self.assertEqual("doomed", state["holder"]["owner"])
        finally:
            child.kill()
            child.wait()
            child.stdout.close()
        leases.acquire([A], holder("agent-2"), directory=self.dir).release()
        self.assertFalse(leases.board_states([A], directory=self.dir)[0]["held"])

    def test_board_states_report_free_and_held_boards(self):
        with leases.acquire([B], holder("agent-1"), directory=self.dir):
            states = leases.board_states([A, B], directory=self.dir)
        self.assertEqual([False, True], [s["held"] for s in states])
        self.assertEqual("UT_1", states[1]["holder"]["resource"])
        self.assertEqual("agent-1", states[1]["holder"]["owner"])
        # A probe creates no lock file for a board nobody has leased.
        self.assertFalse((self.dir / f"{A.key}.lock").exists())

    def test_run_boards_include_guards_alternates_and_power_isolated_boards(self):
        registry = FileResourceRegistry.load(self._hardware())

        def invocation(name="Radio setting transitions", **fields):
            values = dict(
                test_name=name, host="Host_Local", resources={"Ubertooth": "UT_0"},
                power_isolation=(), exclusive_ubertooth=False,
            )
            values.update(fields)
            return SimpleNamespace(**values)

        only = leases.boards_for_invocations([invocation()], registry)
        self.assertEqual([A], list(only))
        cases = {
            "guard role": invocation(resources={"Ubertooth": "UT_0", "guard Ubertooth 1": "UT_1"}),
            "power isolated": invocation(power_isolation=(PowerIsolationAction("UT_1", "Hub", 1),)),
            "flash guards every board": invocation("Flash reliability"),
            "exclusive enumerates every board": invocation(exclusive_ubertooth=True),
        }
        for label, item in cases.items():
            with self.subTest(label):
                self.assertEqual([A, B], list(leases.boards_for_invocations([item], registry)))
        both = leases.boards_for_invocations([cases["guard role"], invocation()], registry)
        self.assertEqual([A, B], list(both))

    def test_board_key_needs_a_full_serial(self):
        self.assertEqual(A.key, leases.board_key("UT", {"serial": A.key.upper()}))
        # A short serial selects the same board as the full one (libubertooth
        # matches the end), so it would take a different lock: refuse it.
        for serial in ("abcd", "", "1" * 32, "g" * 32):
            with self.subTest(serial=serial), self.assertRaises(leases.LeaseError):
                leases.board_key("UT", {"serial": serial})

    def test_a_repeated_board_is_leased_once(self):
        with leases.acquire([A, A], holder("agent-1"), directory=self.dir) as held:
            self.assertEqual(1, len(held.leases))

    def test_release_clears_the_holder_record(self):
        leases.acquire([A], holder("agent-1"), directory=self.dir).release()
        self.assertEqual({}, leases.read_holder(self.dir / f"{A.key}.lock"))

    def test_signal_exit_is_reported_shell_style(self):
        self.assertEqual(143, leases.exit_status(-15))
        self.assertEqual(3, leases.exit_status(3))

    def _hardware(self):
        path = self.dir / "hardware.yaml"
        path.write_text(HARDWARE)
        return path


def reaped_pid() -> int:
    """A pid of a process that has ended and been waited for: nothing holds it."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def dead_holder(**fields) -> dict:
    """A holder record of a session that is gone, as #215's lease file was."""
    pid = reaped_pid()
    record = dict(
        holder("gone-agent"), pid=pid, pgid=pid,
        purpose="lease: bash soak.sh", hostname=socket.gethostname(),
    )
    record.update(fields)
    return record


class RawHoldMixin:
    """Holding a lease file with a holder record the test chooses."""

    def setUp(self):
        super().setUp()
        self.raw = []
        self.addCleanup(self._close_raw)

    def _close_raw(self):
        for fd in self.raw:
            try:
                os.close(fd)
            except OSError:
                pass

    def hold_raw(self, directory, board, record):
        """Hold a board's lease file, with a record of our choosing.

        flock belongs to an open file, not to a process, so a second
        descriptor on the same file conflicts with the first even in one
        process: this is a lease that is really locked, saying whatever the
        test needs about who took it.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = Path(directory) / f"{board.key}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        self.raw.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.pwrite(fd, (json.dumps(record) + "\n").encode(), 0)
        return path


class StaleLeaseTests(RawHoldMixin, unittest.TestCase):
    """A lease whose holder is gone must not hold a board forever (#215)."""

    def setUp(self):
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.notes = []

    def hold(self, board, record):
        return self.hold_raw(self.dir, board, record)

    def take(self, board=A, **kwargs):
        return leases.acquire(
            [board], holder("agent-2"), directory=self.dir,
            note=self.notes.append, **kwargs,
        )

    def test_a_lease_whose_process_is_gone_is_taken_over(self):
        record = dead_holder()
        path = self.hold(A, record)
        held = self.take()
        self.addCleanup(held.release)
        self.assertEqual([A], [lease.board for lease in held.leases])
        # The board's lock file is a new one, recording the new holder.
        self.assertEqual("agent-2", leases.read_holder(path)["owner"])
        self.assertEqual(1, len(self.notes), self.notes)
        self.assertIn("UT_0 was held by a lease that no longer holds it", self.notes[0])
        self.assertIn(f"pid {record['pid']}", self.notes[0])
        self.assertIn("process group", self.notes[0])

    def test_a_takeover_says_when_the_session_is_still_running(self):
        # The lease process is gone but its process group is not, which is
        # what an orphaned capture looks like: the board is freed, and the
        # note says which group to look at before trusting it.
        self.hold(A, dead_holder(pgid=os.getpgrp()))
        self.take().release()
        self.assertIn(f"ps -g {os.getpgrp()}", self.notes[0])
        self.assertIn("may still be using the board", self.notes[0])

    def test_a_lease_of_a_live_process_is_left_alone(self):
        self.hold(A, dict(holder("busy-agent"), pid=os.getpid(),
                              pgid=os.getpgrp(), hostname=socket.gethostname()))
        with self.assertRaises(leases.LeaseError) as caught:
            self.take()
        self.assertIn("UT_0 is in use", str(caught.exception))
        self.assertEqual([], self.notes)

    def test_a_lease_from_another_host_is_left_alone(self):
        # A pid of another machine says nothing here, so its lease stands
        # however dead that pid is locally.
        self.hold(A, dead_holder(hostname="another-host"))
        with self.assertRaises(leases.LeaseError):
            self.take()
        self.assertEqual([], self.notes)

    def test_a_lease_file_with_no_record_is_left_alone(self):
        self.hold(A, {})
        with self.assertRaises(leases.LeaseError):
            self.take()
        self.assertEqual([], self.notes)

    def test_a_stale_lease_is_taken_over_without_waiting(self):
        self.hold(A, dead_holder())
        slept = []
        # No wait was asked for, so the takeover must happen inside the brief
        # grace the first attempt has, not only after a --wait-for-boards.
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir, note=self.notes.append,
            sleep=slept.append,
        ).release()
        self.assertEqual([], slept)
        self.assertEqual(1, len(self.notes))

    def test_a_stale_host_lock_is_taken_over(self):
        self.hold(leases.HOST_LOCK, dead_holder())
        held = leases.acquire(
            [], holder("agent-2"), directory=self.dir, host_lock=True,
            note=self.notes.append,
        )
        self.addCleanup(held.release)
        self.assertTrue(held.holds_host_lock())
        self.assertIn("this host's power/firmware lock", self.notes[0])

    def test_one_board_of_a_set_can_be_stale(self):
        self.hold(B, dead_holder())
        held = leases.acquire(
            [A, B], holder("agent-2"), directory=self.dir, note=self.notes.append,
        )
        self.addCleanup(held.release)
        self.assertEqual([A, B], [lease.board for lease in held.leases])
        self.assertIn("UT_1", self.notes[0])

    def test_a_takeover_leaves_the_other_boards_free(self):
        # The attempt that found the stale lease released everything it had
        # taken before taking over, as every other failed attempt does.
        self.hold(A, dead_holder())
        leases.acquire([A, B], holder("agent-2"), directory=self.dir,
                       note=self.notes.append).release()
        leases.acquire([A, B], holder("agent-3"), directory=self.dir).release()

    def test_a_stale_lease_is_only_taken_over_once_per_command(self):
        # A file that keeps coming back must not make acquire loop on it: the
        # second attempt waits like any busy board and then gives up.
        self.hold(A, dead_holder())
        now = [0.0]

        def note(message):
            # Called when the lease has just been taken over: another equally
            # stale lease appears before the retry can lock the board.
            self.notes.append(message)
            self.hold(A, dead_holder())

        def sleep(seconds):
            now[0] += seconds

        with self.assertRaises(leases.LeaseError):
            leases.acquire(
                [A], holder("agent-2"), directory=self.dir, wait_s=10,
                clock=lambda: now[0], sleep=sleep, note=note,
            )
        self.assertEqual(1, len(self.notes))

    def test_boards_reports_a_stale_lease_without_removing_it(self):
        path = self.hold(A, dead_holder())
        state = leases.board_states([A], directory=self.dir)[0]
        self.assertTrue(state["held"])
        self.assertIn("is gone", state["stale"])
        self.assertTrue(path.exists())
        self.assertEqual("gone-agent", leases.read_holder(path)["owner"])

    def test_boards_reports_a_live_lease_as_held_and_not_stale(self):
        with leases.acquire([A], holder("agent-1"), directory=self.dir):
            state = leases.board_states([A], directory=self.dir)[0]
        self.assertTrue(state["held"])
        self.assertNotIn("stale", state)

    def test_liveness_of_a_holder(self):
        here = socket.gethostname()
        pid = reaped_pid()
        cases = {
            "live": dict(pid=os.getpid(), pgid=os.getpgrp(), hostname=here),
            "stale": dict(pid=pid, pgid=pid, hostname=here),
            "foreign": dict(pid=pid, pgid=pid, hostname="elsewhere"),
            "unknown": dict(pid=pid, pgid=pid),
        }
        for kind, record in cases.items():
            with self.subTest(kind):
                self.assertEqual(kind, leases.lease_liveness(record)[0])
        self.assertEqual("unknown", leases.lease_liveness({})[0])
        # A record of a harness that did not keep the process group yet.
        self.assertEqual(
            "stale", leases.lease_liveness({"pid": pid, "hostname": here})[0])
        # A pid value that kill() would read as a group, not a process.
        for bad in (0, -1, "2380347", None, True):
            with self.subTest(pid=bad):
                self.assertEqual(
                    "live", leases.lease_liveness({"pid": bad, "hostname": here})[0])

    def test_an_inherited_lock_outlives_its_lease_and_is_taken_over(self):
        """The real #215: the command outlives the lease that took the board.

        `lease` hands its lock files to the command it runs, so a session
        killed part way through can leave a locked file behind with nothing
        waiting on it. Here the lease process exits while a command it
        started, in a session of its own, keeps the lock.
        """
        code = textwrap.dedent(f"""
            import os, subprocess, sys, time
            from ubertooth_harness import leases
            held = leases.acquire(
                [leases.Board('UT_0', '{A.key}')],
                leases.holder_info(run_id=None, owner='gone-agent',
                                   purpose='lease: bash soak.sh'),
                directory=sys.argv[1])
            fd = held.leases[0].fd
            os.set_inheritable(fd, True)
            child = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(120)'],
                pass_fds=(fd,), start_new_session=True,
                # Not this process's output: a pipe the orphan holds open
                # would keep the reader waiting for the orphan, not for the
                # lease.
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            print(child.pid, flush=True)
            """)
        started = subprocess.run(
            [sys.executable, "-c", code, str(self.dir)],
            capture_output=True, text=True, timeout=60, start_new_session=True,
        )
        self.assertEqual(0, started.returncode, started.stderr)
        orphan = int(started.stdout.strip())
        self.addCleanup(lambda: os.kill(orphan, 9))
        # The lease process is gone, the orphan still holds its lock file.
        state = leases.board_states([A], directory=self.dir)[0]
        self.assertTrue(state["held"])
        self.assertIn("is gone", state["stale"])
        self.take().release()
        self.assertEqual(1, len(self.notes), self.notes)


class LeaseCliTests(RawHoldMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.lease_dir = self.root / "leases"
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(HARDWARE)

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_run_suite_refuses_a_leased_board_before_any_device_command(self):
        from tests.test_cli import make_source

        source, _short = make_source(self.root)
        kgv = self.root / "kgv.dfu"
        kgv.write_bytes(b"kgv")
        test = self.root / "test.dfu"
        test.write_bytes(b"test")
        plan = self.root / "plan.yaml"
        plan.write_text(textwrap.dedent(f"""\
            plan: lease cli
            suites:
              slow:
                - Flash reliability:
                    hosts: each
                    platforms: [{PLATFORM}]
                    Ubertooth: default
                    flashes: 2
                    image sequence: [test build, KGV]
                    KGV sha256: {hashlib.sha256(b"kgv").hexdigest()}
                    KGV revision: 2020-12-R1
            """))
        output = self.root / "runs"
        with leases.acquire([A], holder("other-agent"), directory=self.lease_dir):
            code, _out, err = self._main(
                "run-suite", "--plan", str(plan), "--suite", "slow",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                "--artifact", f"test build={test}", "--artifact", f"KGV={kgv}",
                "--run-id", "blocked", "--output", str(output),
                "--lease-dir", str(self.lease_dir),
            )
        self.assertEqual(2, code)
        self.assertIn("UT_0 is in use", err)
        self.assertIn("other-agent", err)
        self.assertFalse((output / "blocked").exists())

    def test_boards_lists_free_and_held_boards(self):
        with leases.acquire([B], holder("agent-7"), directory=self.lease_dir):
            code, out, _err = self._main(
                "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            )
            _code, raw, _err = self._main(
                "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
                "--json",
            )
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^UT_0 +free$")
        self.assertRegex(lines[1], r"^UT_1 +held  agent-7, run run-agent-7")
        # The host lock is the one on the machine this command ran on.
        self.assertRegex(lines[2], r"^host lock \(this machine\) +free$")
        states = json.loads(raw)
        self.assertEqual([False, True, False], [s["held"] for s in states])
        self.assertTrue(states[2]["host_lock"])

    def test_lease_holds_the_board_while_the_command_runs(self):
        probe = (
            "import json, sys; from ubertooth_harness import leases; "
            f"s = leases.board_states([leases.Board('UT_0', '{A.key}')], directory=sys.argv[1]); "
            "print(json.dumps(s[0]['holder'].get('owner')) if s[0]['held'] else 'free')"
        )
        result = subprocess.run(
            [sys.executable, "-m", "ubertooth_harness", "lease",
             "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
             "--owner", "manual", "UT_0", "--", sys.executable, "-c", probe, str(self.lease_dir)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('"manual"', result.stdout.strip())
        self.assertFalse(leases.board_states([A], directory=self.lease_dir)[0]["held"])

    def test_boards_needs_a_real_host_when_the_registry_has_several(self):
        two_hosts = self.root / "two.yaml"
        two_hosts.write_text(
            HARDWARE
            + f"  Host_Other:\n    type: Host\n    platform: {PLATFORM}\n    roles: [test target]\n"
        )
        code, _out, err = self._main(
            "boards", "--hardware", str(two_hosts), "--lease-dir", str(self.lease_dir),
        )
        self.assertEqual(2, code)
        self.assertIn("boards needs --host", err)
        code, _out, err = self._main(
            "boards", "--hardware", str(two_hosts), "--lease-dir", str(self.lease_dir),
            "--host", "UT_0",
        )
        self.assertEqual(2, code)
        self.assertIn("UT_0 is not a Host", err)

    def test_boards_lists_a_short_serial_board_as_not_leasable(self):
        short = self.root / "short.yaml"
        short.write_text(HARDWARE + "  UT_2:\n    type: Ubertooth\n    attached to: Host_Local\n"
                         "    identity: {serial: abcd}\n")
        code, out, _err = self._main(
            "boards", "--hardware", str(short), "--lease-dir", str(self.lease_dir),
        )
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^UT_0 +free$")
        self.assertRegex(lines[1], r"^UT_1 +free$")
        self.assertRegex(lines[2], r"^UT_2 +not leasable: UT_2 needs a full 32-hex-digit serial")

    def test_wait_for_boards_rejects_negative_minutes(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["lease", "--hardware", str(self.hardware), "--wait-for-boards", "-5",
                  "UT_0", "--", "true"])

    def test_boards_says_a_lease_is_stale_and_lease_takes_it_over(self):
        self.hold_raw(self.lease_dir, A, dead_holder())
        code, out, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
        )
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^UT_0 +held  gone-agent")
        self.assertRegex(lines[1], r"^ +stale: its process \(pid \d+\) is gone")
        self.assertIn("takes it over", lines[1])
        _code, raw, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            "--json",
        )
        self.assertIn("is gone", json.loads(raw)[0]["stale"])
        # The next command that wants the board takes it over, and says so.
        code, _out, err = self._main(
            "lease", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            "UT_0", "--", sys.executable, "-c", "pass",
        )
        self.assertEqual(0, code)
        self.assertIn("note: UT_0 was held by a lease that no longer holds it", err)

    def test_lease_returns_the_command_exit_code(self):
        code, _out, _err = self._main(
            "lease", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            "UT_0", "--", sys.executable, "-c", "raise SystemExit(3)",
        )
        self.assertEqual(3, code)


if __name__ == "__main__":
    unittest.main()
