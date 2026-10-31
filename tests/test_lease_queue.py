"""The lease queue: waiters are served in the order they arrived (#202)."""

import fcntl
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ubertooth_harness import leases
from ubertooth_harness.cli import main

from tests.test_leases import A, B, HARDWARE, dead_holder, holder


class QueueFixture(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.dir = self.root / "leases"
        self.raw = []
        self.addCleanup(self._close_raw)

    def _close_raw(self):
        for fd in self.raw:
            try:
                os.close(fd)
            except OSError:
                pass

    def ticket_dir(self, board):
        return self.dir / leases.TICKET_DIR / board.key

    def tickets(self, board):
        try:
            return sorted(path.name for path in self.ticket_dir(board).iterdir())
        except OSError:
            return []

    def hold_ticket(self, board, name, record, *, locked=True):
        """A ticket of another command, with a name and a record we choose."""
        directory = self.ticket_dir(board)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        os.pwrite(fd, (json.dumps(record) + "\n").encode(), 0)
        if locked:
            self.raw.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            os.close(fd)
        return path

    def live_holder(self, **fields):
        record = dict(holder("waiting-agent"), pid=os.getpid(), pgid=os.getpgrp(),
                      hostname=socket.gethostname(), purpose="lease: run-suite")
        record.update(fields)
        return record

    def older(self):
        """A ticket name from before any this test's commands will take."""
        return f"{1:020d}-{os.getpid()}-aaaaaa"

    def newer(self):
        return f"{leases.time.time_ns() * 2:020d}-{os.getpid()}-ffffff"


class QueueTests(QueueFixture):

    def test_a_waiting_run_and_a_rush_of_leases_both_get_their_turn(self):
        """#202's shape under real contention: nothing is left behind.

        A run that wants the host lock and a board, while board-only leases
        take that board one after another with nothing in between. flock is
        per open file, so threads here contend exactly as processes do. What
        the queue does when both are ready at once is pinned by the
        deterministic tests below; this one is about the whole thing
        working under load, where the run would also often win on timing
        alone.
        """
        held_first = leases.acquire([A], holder("holder"), directory=self.dir)
        stop = threading.Event()
        outcome = {}
        counts = {"taken": 0, "refused": 0}

        def starver():
            while not stop.is_set():
                try:
                    with leases.acquire([A], holder("quick"), directory=self.dir):
                        counts["taken"] += 1
                except leases.LeaseError:
                    counts["refused"] += 1

        def waiting_run():
            try:
                with leases.acquire(
                    [A], holder("waiting-run"), directory=self.dir,
                    host_lock=True, wait_s=20,
                ) as held:
                    outcome["boards"] = [lease.board for lease in held.leases]
            except leases.LeaseError as error:
                outcome["error"] = str(error)

        run = threading.Thread(target=waiting_run)
        run.start()
        time.sleep(0.3)  # it is waiting, and its ticket is the older one
        rush = threading.Thread(target=starver)
        rush.start()
        try:
            time.sleep(0.2)
            held_first.release()  # the board every one of them wants
            run.join(timeout=30)
        finally:
            stop.set()
            rush.join(timeout=30)
        self.assertFalse(run.is_alive())
        self.assertEqual([leases.HOST_LOCK, A], outcome.get("boards"),
                         outcome.get("error"))
        # The contention was real: the board-only leases did run, and each
        # either took the board or was told it was busy.
        self.assertGreater(counts["taken"] + counts["refused"], 0,
                           "no board-only lease ever ran")

    def test_the_queue_is_empty_again_when_a_command_stops_waiting(self):
        with leases.acquire([A], holder("agent-1"), directory=self.dir):
            self.assertEqual([], self.tickets(A))
        # And after a failed wait, not only a successful one.
        with leases.acquire([A], holder("agent-1"), directory=self.dir):
            with self.assertRaises(leases.LeaseError):
                leases.acquire([A], holder("agent-2"), directory=self.dir)
        self.assertEqual([], self.tickets(A))

    def test_a_ticket_of_a_process_that_is_gone_expires(self):
        # The same test that frees a stale lease: nothing is waiting on this.
        path = self.hold_ticket(A, self.older(), dead_holder(), locked=False)
        leases.acquire([A], holder("agent-2"), directory=self.dir).release()
        self.assertFalse(path.exists())

    def test_a_ticket_nobody_holds_is_dropped(self):
        # A ticket is locked from the moment it is created, so an unlocked one
        # is litter: a command whose pid the system has given to someone else
        # (as here), one that wrote nothing, or one from another host. None of
        # them holds anyone back, and a bench must not collect them forever.
        kept = self.hold_ticket(A, self.older(), self.live_holder(), locked=False)
        empty = self.hold_ticket(A, f"{3:020d}-{os.getpid()}-cccccc", {},
                                 locked=False)
        rounds = []
        leases.acquire([A], holder("agent-2"), directory=self.dir,
                       sleep=rounds.append).release()
        self.assertEqual([], rounds)
        self.assertFalse(kept.exists())
        self.assertFalse(empty.exists())

    def test_a_ticket_of_another_host_holds_a_free_board_only_briefly(self):
        # Another host's record cannot be checked here, so it counts while
        # its lock is held, like any other: this command gives way for its
        # stretch and then takes the board.
        self.hold_ticket(A, self.older(), self.live_holder(hostname="another-host"))
        rounds = []
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir, wait_s=60,
            clock=lambda: 0.0, sleep=rounds.append,
        ).release()
        self.assertGreaterEqual(len(rounds), 2)
        self.assertLessEqual(len(rounds), 4)

    def test_a_newer_ticket_does_not_hold_anyone_back(self):
        self.hold_ticket(A, self.newer(), self.live_holder())
        rounds = []
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir,
            sleep=rounds.append,
        ).release()
        self.assertEqual([], rounds)

    def test_a_queue_for_another_board_does_not_hold_this_one_back(self):
        self.hold_ticket(B, self.older(), self.live_holder())
        rounds = []
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir,
            sleep=rounds.append,
        ).release()
        self.assertEqual([], rounds)

    def test_the_queue_is_what_holds_a_newcomer_back_at_a_free_board(self):
        """What the queue adds, and what the bench looked like without it.

        An older command is waiting for UT_0 and the board is free. With the
        queue, this command gives way for its stretch before competing; with
        the queue taken out, it takes the board on its first round, which is
        how a short lease could keep taking a board a run was waiting for.
        """
        self.hold_ticket(A, self.older(), self.live_holder())
        with_queue = []
        leases.acquire(
            [A], holder("agent-2"), directory=self.dir, wait_s=60,
            clock=lambda: 0.0, sleep=with_queue.append,
        ).release()
        self.assertGreaterEqual(len(with_queue), 2)

        without_queue = []
        with mock.patch.object(leases.TicketQueue, "ahead", lambda _self: None):
            leases.acquire(
                [A], holder("agent-3"), directory=self.dir, wait_s=60,
                clock=lambda: 0.0, sleep=without_queue.append,
            ).release()
        self.assertEqual([], without_queue)

    def test_a_command_with_no_wait_competes_rather_than_failing(self):
        # Every manual board command goes through `lease`, which waits for
        # nothing by default: it gives way to the queue for a moment and then
        # takes a free board, rather than failing for as long as somebody is
        # waiting for it.
        self.hold_ticket(A, self.older(), self.live_holder())
        with leases.acquire([A], holder("manual"), directory=self.dir) as held:
            self.assertEqual([A], [lease.board for lease in held.leases])

    def test_giving_way_starts_again_when_the_board_is_taken(self):
        """A stretch of giving way is bounded; the whole wait is not.

        The bound exists for a resource nobody holds. A command that has used
        it up must not stop honoring the queue for the rest of its wait, or
        it becomes the queue-jumper the queue was added to stop.
        """
        # (Half a second of giving way, then it competes; see _FREE_YIELD_S.)
        self.hold_ticket(A, self.older(), self.live_holder())
        rounds = []
        raw = []

        def sleep(_seconds):
            rounds.append(len(rounds) + 1)
            if len(rounds) == 2:  # the board is taken while this command waits
                fd = os.open(self.dir / f"{A.key}.lock", os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raw.append(fd)
            if len(rounds) == 6:  # and given back
                os.close(raw.pop())

        with leases.acquire(
            [A], holder("agent-2"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=sleep,
        ) as held:
            self.assertEqual([A], [lease.board for lease in held.leases])
        # Two rounds of giving way before the board was taken, four while it
        # was held, and two more after it came back: a command that kept its
        # spent budget would have taken it on the first free round instead.
        self.assertGreaterEqual(len(rounds), 8)

    def test_a_ticket_is_never_left_locked_when_queuing_fails(self):
        stamp = leases._stamp

        def stamping(fd, record):
            if record and "ticket" in record:
                raise OSError(28, "No space left on device")
            stamp(fd, record)

        with mock.patch.object(leases, "_stamp", stamping):
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire([A], holder("agent-2"), directory=self.dir)
        self.assertIn("cannot queue for UT_0", str(caught.exception))
        # Nothing of that command is left behind to hold up the next one.
        self.assertEqual([], self.tickets(A))
        rounds = []
        leases.acquire([A], holder("agent-3"), directory=self.dir,
                       sleep=rounds.append).release()
        self.assertEqual([], rounds)

    def test_giving_way_at_a_free_board_is_bounded(self):
        """A wrapper outside the harness must not be held back forever.

        It can hold a board's own lock file and then wait for the lease, so a
        command that gives way forever to a waiter stuck on that file would
        deadlock. After a second of giving way to a board that is free, this
        command takes it.
        """
        self.hold_ticket(A, self.older(), self.live_holder())
        rounds = []
        with leases.acquire(
            [A], holder("agent-2"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=rounds.append,
        ) as held:
            self.assertEqual([A], [lease.board for lease in held.leases])
        self.assertLessEqual(len(rounds), 4, "gave way for too long")
        self.assertGreaterEqual(len(rounds), 2, "did not give the waiter a turn")

    def test_giving_way_at_a_held_board_is_not_bounded(self):
        # Nothing here can be stuck on a lock somebody holds, so the queue
        # keeps its order for as long as the wait lasts.
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        with leases.acquire([A], holder("holder"), directory=self.dir):
            self.hold_ticket(A, self.older(), self.live_holder())
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("agent-2"), directory=self.dir, wait_s=30,
                    clock=lambda: now[0], sleep=sleep,
                )
        self.assertIn("UT_0 is queued after waiting 30s", str(caught.exception))
        self.assertIn("waiting-agent", str(caught.exception))

    def test_the_host_lock_has_a_queue_of_its_own(self):
        now = [0.0]
        with leases.acquire([], holder("holder"), directory=self.dir, host_lock=True):
            self.hold_ticket(leases.HOST_LOCK, self.older(), self.live_holder())
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [], holder("agent-2"), directory=self.dir, host_lock=True,
                    wait_s=30, clock=lambda: now[0],
                    sleep=lambda s: now.__setitem__(0, now[0] + s),
                )
        self.assertIn("this host's power/firmware lock is queued", str(caught.exception))

    def test_a_ticket_is_taken_for_every_resource_and_given_back(self):
        queue = leases.TicketQueue(self.dir, [A, B, leases.HOST_LOCK], holder("agent"))
        queue.take()
        try:
            for board in (A, B, leases.HOST_LOCK):
                self.assertEqual([queue.name], self.tickets(board))
                self.assertEqual("agent", leases.read_holder(
                    self.ticket_dir(board) / queue.name)["owner"])
            self.assertEqual(1, queue.waiting_for(A))
        finally:
            queue.release()
        for board in (A, B, leases.HOST_LOCK):
            self.assertEqual([], self.tickets(board))

    def test_ticket_names_put_the_oldest_first(self):
        one = leases.TicketQueue(self.dir, [A], holder("first"))
        two = leases.TicketQueue(self.dir, [A], holder("second"))
        self.assertLess(one.name, two.name)
        self.assertEqual(20, len(one.name.split("-")[0]))

    def test_an_unfamiliar_file_in_a_queue_directory_is_left_alone(self):
        directory = self.ticket_dir(A)
        directory.mkdir(parents=True)
        stray = directory / "notes.txt"
        stray.write_text("someone's scratch file")
        leases.acquire([A], holder("agent-2"), directory=self.dir).release()
        self.assertTrue(stray.exists())

    def test_waiting_commands_are_counted_for_a_listing(self):
        self.hold_ticket(A, self.older(), self.live_holder())
        self.hold_ticket(A, self.newer(), self.live_holder(owner="second"))
        self.hold_ticket(A, f"{2:020d}-{os.getpid()}-bbbbbb",
                         self.live_holder(), locked=False)  # nobody is waiting
        states = leases.board_states([A, B], directory=self.dir)
        self.assertEqual([2, 0], [state["waiting"] for state in states])


class QueueCliTests(QueueFixture):

    def setUp(self):
        super().setUp()
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(HARDWARE)

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_boards_says_how_many_commands_are_waiting(self):
        self.hold_ticket(A, self.older(), self.live_holder())
        self.hold_ticket(B, self.older(), self.live_holder())
        self.hold_ticket(B, self.newer(), self.live_holder(owner="second"))
        code, out, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.dir),
        )
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^UT_0 +free$")
        self.assertRegex(lines[1], r"^ +queue: 1 command is waiting for it$")
        self.assertRegex(lines[2], r"^UT_1 +free$")
        self.assertRegex(lines[3], r"^ +queue: 2 commands are waiting for it$")
        _code, raw, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.dir),
            "--json",
        )
        self.assertEqual([1, 2], [state["waiting"] for state in json.loads(raw)[:2]])

    def test_a_lease_that_has_to_wait_its_turn_says_so(self):
        with leases.acquire([A], holder("holder"), directory=self.dir):
            self.hold_ticket(A, self.older(), self.live_holder())
            code, _out, err = self._main(
                "lease", "--hardware", str(self.hardware), "--lease-dir", str(self.dir),
                "UT_0", "--", "true",
            )
        self.assertEqual(2, code)
        self.assertIn("error: UT_0 is queued", err)
        self.assertIn("asked for it first", err)


if __name__ == "__main__":
    unittest.main()
