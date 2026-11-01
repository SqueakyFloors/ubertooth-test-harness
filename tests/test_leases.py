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
from unittest import mock

from ubertooth_harness import leases
from ubertooth_harness.cli import main
from ubertooth_harness.report import _Run, _setup_rows
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.topology import PowerIsolationAction

# How long an attempt sleeps between rounds, for tests that count them.
_POLL_S = leases._POLL_S

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

    def test_a_takeover_is_recorded_with_the_leases_it_was_taken_for(self):
        """The record a run carries into run.jsonl and its report (#224).

        A takeover does not promise the board was idle, so a reader of an odd
        result later in the run needs to know one happened, and standard
        error is not where a deployed runner's reader looks.
        """
        record = dead_holder()
        path = self.hold(A, record)
        held = self.take()
        self.addCleanup(held.release)
        self.assertEqual(1, len(held.takeovers), held.takeovers)
        taken = held.takeovers[0]
        self.assertEqual("UT_0", taken["resource"])
        self.assertEqual(A.key, taken["key"])
        self.assertEqual(str(path), taken["lock"])
        self.assertEqual("gone-agent", taken["holder"]["owner"])
        self.assertEqual(record["pid"], taken["holder"]["pid"])
        self.assertIn(f"pid {record['pid']}", taken["reason"])
        self.assertRegex(taken["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}$")
        # The line on standard error is that record, said once.
        self.assertEqual([leases.describe_takeover(taken)], self.notes)

    def test_a_record_a_run_could_not_carry_is_cut_down(self):
        """A lock file is another command's: a run record has a size limit.

        Its holder record may hold anything at all (a hand-written file, a
        wrapper's own fields, a long command line), and an event that
        exceeds the limit would end the run it was meant to explain.
        """
        self.hold(A, dict(
            dead_holder(purpose="lease: " + "x" * 40_000),
            notes=["something else entirely"] * 1000,
        ))
        held = self.take()
        self.addCleanup(held.release)
        taken = held.takeovers[0]
        self.assertNotIn("notes", taken["holder"])
        self.assertEqual(200, len(taken["holder"]["purpose"]))
        self.assertTrue(taken["holder"]["purpose"].endswith("..."))
        self.assertLess(len(json.dumps(taken)), 2048)

    def test_a_lease_nobody_had_to_be_taken_over_for_records_nothing(self):
        held = self.take()
        self.addCleanup(held.release)
        self.assertEqual([], held.takeovers)

    def test_a_takeover_says_when_the_session_is_still_running(self):
        # The lease process is gone but its process group is not, which is
        # what an orphaned capture looks like: the board is freed, and the
        # note says which group to look at before trusting it.
        self.hold(A, dead_holder(pgid=os.getpgrp()))
        self.take().release()
        self.assertIn(f"pgrep -g {os.getpgrp()}", self.notes[0])
        self.assertIn("may still be using the board", self.notes[0])

    def test_a_host_lock_whose_command_is_gone_is_never_taken_over(self):
        """The one lock a person has to clear.

        It is all that keeps a power change or a firmware write from
        overlapping another, and what still holds its file could be a write
        in flight: a process that left the recorded session, or one of a
        group the record does not name at all. So the error says what is
        known and what to look for, whether or not the recorded group still
        has processes.
        """
        for record in (dead_holder(pgid=os.getpgrp()),  # its group lives
                       dead_holder(),                   # its group is gone
                       {k: v for k, v in dead_holder().items()
                        if k != "pgid"}):               # an older lease file
            with self.subTest(pgid=record.get("pgid")):
                path = self.hold(leases.HOST_LOCK, record)
                with self.assertRaises(leases.LeaseError) as caught:
                    leases.acquire([], holder("agent-2"), directory=self.dir,
                                   host_lock=True, note=self.notes.append)
                message = str(caught.exception)
                self.assertIn("this host's power/firmware lock is in use", message)
                self.assertIn("the command that took it is gone", message)
                self.assertIn("it is never taken over", message)
                self.assertIn("remove the lock file", message)
                self.assertEqual([], self.notes)
                self.assertEqual("gone-agent", leases.read_holder(path)["owner"])
                os.close(self.raw.pop())
                os.unlink(path)

    def test_a_busy_board_says_when_the_command_that_took_it_is_gone(self):
        # A board whose takeover did not happen (another command was in the
        # middle of one) still explains itself.
        self.hold(A, dead_holder())
        shared = os.open(self.dir / leases.TAKEOVER_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        self.raw.append(shared)
        fcntl.flock(shared, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with self.assertRaises(leases.LeaseError) as caught:
            self.take()
        message = str(caught.exception)
        self.assertIn("the command that took it is gone", message)
        # The tail other tools trim is still the last thing in the message.
        self.assertTrue(
            message.endswith("use --wait-for-boards MINUTES to wait for it"),
            message)

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
        # No wait was asked for, so the takeover and the round it costs must
        # both fit in the brief grace every attempt has, rather than needing
        # a --wait-for-boards.
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir, note=self.notes.append,
            sleep=slept.append,
        ).release()
        self.assertEqual([_POLL_S], slept)
        self.assertEqual(1, len(self.notes))


    def test_one_board_of_a_set_can_be_stale(self):
        self.hold(B, dead_holder())
        held = leases.acquire(
            [A, B], holder("agent-2"), directory=self.dir, note=self.notes.append,
        )
        self.addCleanup(held.release)
        self.assertEqual([A, B], [lease.board for lease in held.leases])
        self.assertIn("UT_1", self.notes[0])

    def test_a_takeover_leaves_the_other_boards_free(self):
        # The stale lease is on the second board in lock order, so the
        # attempt that finds it is already holding the first one and has to
        # give it back before taking over, as every failed attempt does.
        self.hold(B, dead_holder())
        taken = []

        def note(message):
            self.notes.append(message)
            # Nothing of that attempt is still held at this point.
            taken.append(leases.acquire([A], holder("passer-by"),
                                        directory=self.dir))
            taken[-1].release()

        leases.acquire([A, B], holder("agent-2"), directory=self.dir,
                       note=note).release()
        self.assertEqual(1, len(taken))
        leases.acquire([A, B], holder("agent-3"), directory=self.dir).release()

    def test_a_lease_file_that_keeps_coming_back_cannot_make_a_loop(self):
        # Every round sleeps or reaches the deadline, so a stale lease that
        # is replaced as soon as it is taken over costs poll rounds and ends
        # at the deadline, and each one is still taken over and reported.
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
        # One takeover per round, and the rounds are the wait divided by
        # the poll: bounded, not a spin.
        self.assertGreater(len(self.notes), 1)
        self.assertLessEqual(len(self.notes), 10 / 0.25 + 1)
        self.assertGreaterEqual(now[0], 10)

    def test_a_lease_whose_file_was_removed_under_it_is_not_trusted(self):
        # The narrow race a takeover leaves: it decided to remove a file that
        # became somebody's live lease in between. The lock then excludes
        # nobody, so the attempt that took it must start again instead of
        # reporting a board it does not hold.
        removed = []
        stamp = leases._stamp

        def stamping(fd, record):
            # Right after the first lease is locked and recorded, as a
            # takeover that had already decided to remove this file would.
            stamp(fd, record)
            if not removed and "ticket" not in record:  # the lease, not a ticket
                removed.append(record)
                os.unlink(self.dir / f"{A.key}.lock")

        with mock.patch.object(leases, "_stamp", stamping):
            held = leases.acquire(
                [A], holder("agent-2"), directory=self.dir, wait_s=5,
                sleep=lambda _s: None, note=self.notes.append,
            )
        self.addCleanup(held.release)
        self.assertEqual(1, len(removed))
        # The lease it returns is the file that is really the board's now.
        path = self.dir / f"{A.key}.lock"
        self.assertEqual("agent-2", leases.read_holder(path)["owner"])
        self.assertEqual(
            os.stat(path).st_ino, os.fstat(held.leases[0].fd).st_ino)

    def test_the_takeover_lock_is_taken_before_any_board(self):
        order = []
        real = leases._try_lock

        def recording(path, mode, **kwargs):
            order.append(Path(path).name)
            return real(path, mode, **kwargs)

        with mock.patch.object(leases, "_try_lock", recording):
            leases.acquire([A], holder("agent-1"), directory=self.dir).release()
        # The takeover lock, then the board. Its place in the queue is taken
        # before both, with a lock of its own that is not taken through
        # _try_lock (tests/test_lease_queue.py).
        self.assertEqual([leases.TAKEOVER_LOCK, f"{A.key}.lock"], order)

    def test_no_lease_is_taken_while_a_takeover_is_in_progress(self):
        # The takeover lock, held exclusively: an attempt must not take a
        # lock that a takeover may be about to remove under it.
        fd = os.open(self.dir / leases.TAKEOVER_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        self.raw.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(leases.LeaseError) as caught:
            self.take()
        self.assertIn("taking over a stale lease", str(caught.exception))
        self.assertFalse((self.dir / f"{A.key}.lock").exists())

    def test_a_takeover_is_left_for_later_while_an_attempt_is_running(self):
        # The other side of that lock: while any command is between taking a
        # lock and checking it (it holds the takeover lock shared), a stale
        # lease file stays where it is.
        path = self.hold(A, dead_holder())
        shared = os.open(self.dir / leases.TAKEOVER_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        self.raw.append(shared)
        fcntl.flock(shared, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with self.assertRaises(leases.LeaseError) as caught:
            self.take()
        self.assertIn("UT_0 is in use", str(caught.exception))
        self.assertEqual([], self.notes)
        self.assertEqual("gone-agent", leases.read_holder(path)["owner"])
        # Once that command is done, the next one takes the board over.
        os.close(self.raw.pop())
        self.take().release()
        self.assertEqual(1, len(self.notes))

    def test_boards_reports_a_stale_lease_without_removing_it(self):
        path = self.hold(A, dead_holder())
        state = leases.board_states([A], directory=self.dir)[0]
        self.assertTrue(state["held"])
        self.assertIn("is gone, and so is the process group", state["stale"])
        self.assertTrue(state["takeover"])
        self.assertTrue(path.exists())
        self.assertEqual("gone-agent", leases.read_holder(path)["owner"])

    def test_boards_says_a_host_lock_of_a_gone_command_stays_held(self):
        for record in (dead_holder(pgid=os.getpgrp()), dead_holder()):
            with self.subTest(pgid=record["pgid"]):  # group alive, then gone
                path = self.hold(leases.HOST_LOCK, record)
                state = leases.host_lock_state(directory=self.dir)
                self.assertIn("is gone", state["stale"])
                self.assertFalse(state["takeover"])
                os.close(self.raw.pop())
                os.unlink(path)

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
            "orphan": dict(pid=pid, pgid=os.getpgrp(), hostname=here),
            "foreign": dict(pid=pid, pgid=pid, hostname="elsewhere"),
            "unknown": dict(pid=pid, pgid=pid),
        }
        for kind, record in cases.items():
            with self.subTest(kind):
                self.assertEqual(kind, leases.lease_liveness(record)[0])
        self.assertEqual("unknown", leases.lease_liveness({})[0])
        # A record of a harness that did not keep the process group yet, or
        # a group value kill() would not read as a group: the pid decides,
        # and the reason claims nothing about a group it could not check.
        for record in ({"pid": pid}, {"pid": pid, "pgid": 0},
                       {"pid": pid, "pgid": "2380347"}):
            with self.subTest(record=record):
                kind, why = leases.lease_liveness(dict(record, hostname=here))
                self.assertEqual("stale", kind)
                self.assertEqual(f"its process (pid {pid}) is gone", why)
        # A pid value that kill() would read as a group, not a process: it
        # cannot be checked, so nothing is taken over.
        for bad in (0, -1, "2380347", None, True):
            with self.subTest(pid=bad):
                kind, why = leases.lease_liveness({"pid": bad, "hostname": here})
                self.assertEqual("live", kind)
                self.assertIn("cannot be checked", why)
        self.assertTrue(leases.takeover_allowed(A, "stale"))
        self.assertTrue(leases.takeover_allowed(A, "orphan"))
        for kind in ("live", "foreign", "unknown"):
            self.assertFalse(leases.takeover_allowed(A, kind))
        # The host lock is never taken over, whatever its record says.
        for kind in ("stale", "orphan", "live", "foreign", "unknown"):
            self.assertFalse(leases.takeover_allowed(leases.HOST_LOCK, kind))

    def _orphaned_lease(self, *, same_session: bool) -> int:
        """Leave A leased by a command that outlived the lease that took it.

        `lease` hands its lock files to the command it runs, so a session
        killed part way through leaves a locked file with nothing waiting on
        it. The lease process here exits while a command it started keeps the
        lock, either in the lease's own session (a plain child, which the
        holder record's process group covers) or in a session of its own
        (as `setsid` leaves one, which it cannot).
        """
        code = textwrap.dedent(f"""
            import os, subprocess, sys
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
                pass_fds=(fd,), start_new_session={not same_session},
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
        self.addCleanup(self._kill, orphan)
        return orphan

    @staticmethod
    def _kill(pid: int) -> None:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass  # it ended on its own

    def test_an_inherited_lock_outlives_its_lease_and_is_taken_over(self):
        # The orphan is in a session of its own, so nothing of the lease's
        # session is left to report.
        self._orphaned_lease(same_session=False)
        state = leases.board_states([A], directory=self.dir)[0]
        self.assertTrue(state["held"])
        self.assertIn("is gone, and so is the process group", state["stale"])
        self.take().release()
        self.assertEqual(1, len(self.notes), self.notes)
        self.assertIn("and so is the process group", self.notes[0])

    def test_an_orphan_in_the_lease_s_own_session_is_named_in_the_note(self):
        # The common shape: a plain child of the command `lease` ran. The
        # board is taken over, and the note says which group still holds
        # processes that may be on it.
        self._orphaned_lease(same_session=True)
        state = leases.board_states([A], directory=self.dir)[0]
        self.assertIn("still running", state["stale"])
        self.take().release()
        self.assertEqual(1, len(self.notes), self.notes)
        self.assertIn("may still be using the board", self.notes[0])
        self.assertIn("pgrep -g", self.notes[0])


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

    def test_a_run_that_takes_over_a_lease_records_it(self):
        """#224: the one place a reader of the run can see it happened."""
        from tests.test_cli import FlashDeviceExecutor, make_source

        source, short = make_source(self.root)
        kgv = self.root / "kgv.dfu"
        kgv.write_bytes(b"kgv")
        test = self.root / "test.dfu"
        test.write_bytes(b"test")
        plan = self.root / "plan.yaml"
        plan.write_text(textwrap.dedent(f"""\
            plan: takeover cli
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
        executor = FlashDeviceExecutor(
            f"git-{short}",
            {str(test.resolve()): f"git-{short}", str(kgv.resolve()): "2020-12-R1"},
            A.key, B.key,
        )
        output = self.root / "runs"
        gone = dead_holder()
        self.hold_raw(self.lease_dir, A, gone)
        with mock.patch("ubertooth_harness.cli.SubprocessCommandExecutor",
                        return_value=executor):
            code, _out, err = self._main(
                "run-suite", "--plan", str(plan), "--suite", "slow",
                "--hardware", str(self.hardware), "--host", "Host_Local",
                "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
                "--artifact", f"test build={test}", "--artifact", f"KGV={kgv}",
                "--run-id", "took-over", "--output", str(output),
                "--lease-dir", str(self.lease_dir),
            )
        self.assertEqual(0, code, err)
        self.assertIn("UT_0 was held by a lease that no longer holds it", err)
        run_dir = output / "took-over"
        events = [json.loads(line)
                  for line in (run_dir / "run.jsonl").read_text().splitlines()]
        taken_over = [e for e in events if e["event_type"] == "lease_takeover"]
        self.assertEqual(1, len(taken_over), [e["event_type"] for e in events])
        self.assertEqual("warning", taken_over[0]["status"])
        details = taken_over[0]["details"]
        self.assertEqual("UT_0", details["resource"])
        self.assertEqual(gone["pid"], details["holder"]["pid"])
        self.assertEqual("gone-agent", details["holder"]["owner"])
        self.assertIn(f"pid {gone['pid']}", details["reason"])
        # In the manifest too, with the leases it was taken for.
        manifest = next(e for e in events if e["event_type"] == "manifest")
        self.assertEqual(
            ["UT_0"],
            [item["resource"]
             for item in manifest["details"]["manifest"]["lease_takeovers"]],
        )
        report = (run_dir / "report.md").read_text()
        self.assertIn("| Leases taken over | UT_0 at ", report)
        self.assertIn("gone-agent", report)
        self.assertIn("a takeover does not promise the device was idle", report)
        self.assertIn("[M] UT_0 was held by a lease that no longer holds it", report)

    def test_a_run_that_took_over_nothing_records_no_takeover(self):
        rows = dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000",
            "details": {"manifest": {"leases": [
                {"resource": "UT_0", "key": A.key, "acquired_at": "2026-01-01"},
            ]}},
        }])))
        self.assertIn("Board leases", rows)
        self.assertNotIn("Leases taken over", rows)

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
