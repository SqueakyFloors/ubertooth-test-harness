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

from types import SimpleNamespace

from ubertooth_harness import cli, leases
from ubertooth_harness.cli import main
from ubertooth_harness.report import _Run, _setup_rows, render_run

from tests.test_leases import A, B, HARDWARE, RawHoldMixin, dead_holder, holder


class QueueFixture(RawHoldMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()  # self.raw, and closing it when the test ends
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.dir = self.root / "leases"
        # Where a test reads the lines a wait says on standard error, rather
        # than printing them in the suite's output (note=self.notes.append).
        self.notes = []

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

    def waited(self, seconds):
        """A ticket name of a command that started waiting that long ago.

        A ticket's number is the wall clock when its command began waiting,
        so this is how a test gets a wait a person would recognize.
        """
        return f"{leases.time.time_ns() - int(seconds * 1e9):020d}-{os.getpid()}-aaaaaa"

    def newer(self):
        return f"{leases.time.time_ns() * 2:020d}-{os.getpid()}-ffffff"

    def wanting(self, *boards, **fields):
        """A waiting command's record, naming every resource it asked for."""
        return self.live_holder(
            waiting_for=[{"key": board.key, "resource": board.alias}
                         for board in boards],
            **fields,
        )

    def _virtual_clock(self):
        """(clock, sleep) that let a wait reach its deadline at once."""
        now = [0.0]
        return (lambda: now[0]), (lambda seconds: now.__setitem__(0, now[0] + seconds))

    def hold_lease(self, board, record):
        """A lease file of another command, locked, with a record we choose."""
        return self.hold_raw(self.dir, board, record)


class QueueTests(QueueFixture):

    def test_a_waiting_run_is_served_and_the_rush_keeps_working(self):
        """#202's shape under real contention, end to end.

        A run that wants the host lock and a board waits while board-only
        leases take that board one after another. The run is served, the rush
        takes the board again afterwards, and nothing wedges. What the queue
        does when both are ready at once is pinned by the deterministic tests
        below, which fail with the queue taken out; with threads this shape
        would often come out right on timing alone, so read this one as the
        whole thing working under load.
        """
        held_first = leases.acquire([A], holder("holder"), directory=self.dir)
        stop = threading.Event()
        outcome = {}
        counts = {"took it first": 0, "tried while it waited": 0, "took it after": 0}

        def rush():
            while not stop.is_set():
                waiting = not outcome
                try:
                    with leases.acquire([A], holder("quick"), directory=self.dir,
                                        note=self.notes.append):
                        counts["took it after" if outcome else "took it first"] += 1
                except leases.LeaseError:
                    pass  # busy, or told to wait its turn
                if waiting:
                    counts["tried while it waited"] += 1

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
        rushing = threading.Thread(target=rush)
        rushing.start()
        try:
            time.sleep(0.2)
            held_first.release()  # the board every one of them wants
            run.join(timeout=30)
            time.sleep(0.5)  # the rush gets its turn now
        finally:
            stop.set()
            rushing.join(timeout=30)
        self.assertFalse(run.is_alive())
        self.assertEqual([leases.HOST_LOCK, A], outcome.get("boards"),
                         outcome.get("error"))
        self.assertEqual(0, counts["took it first"],
                         "a board-only lease took the board the run was waiting for")
        self.assertGreater(counts["tried while it waited"], 0,
                           "the rush never ran while the run waited")
        self.assertGreater(counts["took it after"], 0,
                           "the rush never got the board at all")

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
        with mock.patch.object(leases.TicketQueue, "ahead_all", lambda _self: []):
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
        """A command ahead in the queue must not hold the others forever.

        It can be stuck for reasons the queue can neither see nor help: a
        wrapper outside the harness holding a board's own lock file while it
        waits for the lease, or a run still blocked on the host lock. So
        after half a second of giving way to a board that is free, this
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
            self.assertEqual(1, queue.waiting_count(A))
        finally:
            queue.release()
        for board in (A, B, leases.HOST_LOCK):
            self.assertEqual([], self.tickets(board))

    def test_a_ticket_is_in_place_before_any_lock_is_taken(self):
        # And it is locked and written when it appears, never half made: a
        # ticket nobody holds is read as litter and removed.
        seen = []
        real = leases._try_lock

        def recording(path, mode, **kwargs):
            seen.append((Path(path).name, self.tickets(A)))
            return real(path, mode, **kwargs)

        with mock.patch.object(leases, "_try_lock", recording):
            leases.acquire([A], holder("agent-1"), directory=self.dir).release()
        self.assertEqual(leases.TAKEOVER_LOCK, seen[0][0])
        self.assertEqual(1, len(seen[0][1]), seen[0])
        ticket = self.ticket_dir(A) / seen[0][1][0]
        self.assertEqual([], self.tickets(A))  # given back at the end
        self.assertFalse(ticket.exists())

    def test_a_ticket_is_locked_and_written_when_it_appears(self):
        """Nothing may ever see a ticket that is not yet its command's.

        An unlocked ticket is read as litter and removed, so a file created
        under its queue name and locked a moment later could lose its place
        to any command polling at that moment. It is made under a name no
        scan reads and renamed into place instead.
        """
        queue = leases.TicketQueue(self.dir, [A], holder("agent-1")).take()
        try:
            path = self.ticket_dir(A) / queue.name
            self.assertTrue(leases._probe(path), "its lock is not held")
            self.assertEqual("agent-1", leases.read_holder(path)["owner"])
        finally:
            queue.release()
        self.assertEqual([], self.tickets(A))

    def test_a_ticket_half_made_by_a_command_that_died_is_cleared(self):
        directory = self.ticket_dir(A)
        directory.mkdir(parents=True)
        # One command is making its ticket right now; another died making one.
        making, mine = tempfile.mkstemp(dir=directory, prefix=".making-")
        self.raw.append(making)
        fcntl.flock(making, fcntl.LOCK_EX | fcntl.LOCK_NB)
        left, theirs = tempfile.mkstemp(dir=directory, prefix=".making-")
        os.close(left)
        old = time.time() - leases._UNFINISHED_AGE_S - 1
        os.utime(theirs, (old, old))
        # And one that has only just appeared: another command may be making
        # it this moment, between its file and its lock.
        fresh, just_now = tempfile.mkstemp(dir=directory, prefix=".making-")
        os.close(fresh)
        queue = leases.TicketQueue(self.dir, [A], holder("agent-2")).take()
        try:
            self.assertTrue(os.path.exists(mine), "took a ticket being made")
            self.assertTrue(os.path.exists(just_now), "took a new file away")
            self.assertFalse(os.path.exists(theirs), "left a dead command's file")
            # Neither counts as a command waiting: a ticket is only a ticket
            # once it is under its queue name.
            self.assertEqual(1, queue.waiting_count(A))
            self.assertEqual([], queue.ahead_all())
        finally:
            queue.release()

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


class MultiResourceQueueTests(QueueFixture):
    """A command that wants several resources keeps the free ones (#225).

    Taking locks is all or nothing, so such a command needs every resource
    free in one attempt. Each ticket records the whole set its command is
    waiting for, and giving way to that command is not bounded while any of
    them is held, so the free ones are still free when the last one arrives.
    """

    def test_a_free_board_is_kept_for_a_waiter_held_up_at_another(self):
        self.hold_ticket(A, self.older(), self.wanting(A, B))
        clock, sleep = self._virtual_clock()
        with leases.acquire([B], holder("holder"), directory=self.dir):
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep, note=self.notes.append,
                )
        message = str(caught.exception)
        self.assertIn("UT_0 is queued after waiting 30s", message)
        self.assertIn("waiting-agent", message)
        # The error points at what the command ahead is really held up by.
        self.assertIn("that command is waiting for UT_1 too", message)
        self.assertIn("holder", message)

    def test_what_the_recorded_set_adds(self):
        """The case #225 reported, with the set taken out of the ticket.

        Reading only the board it is queued at, the newcomer finds that board
        free and takes it, which is how a multi-board run could watch its
        boards come free one at a time and never hold them all.
        """
        self.hold_ticket(A, self.older(), self.wanting(A, B))
        rounds = []
        with leases.acquire([B], holder("holder"), directory=self.dir):
            with mock.patch.object(
                leases, "waiting_resources", lambda _ticket, board: [board]
            ):
                with leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=600,
                    clock=lambda: 0.0, sleep=rounds.append, note=self.notes.append,
                ) as taken:
                    self.assertEqual([A], [lease.board for lease in taken.leases])
        self.assertLessEqual(len(rounds), 4, "gave way for longer than one stretch")

    def test_giving_way_is_bounded_again_once_the_waiter_could_take_them_all(self):
        """Not bounded is not forever: the stretch starts when they are free.

        Once nothing the command ahead asked for is held, it could take them
        all, and the bound against a command stuck on something the queue
        cannot see applies as before.
        """
        self.hold_ticket(A, self.older(), self.wanting(A, B))
        held = leases.acquire([B], holder("holder"), directory=self.dir)
        rounds = []

        def sleep(_seconds):
            rounds.append(len(rounds) + 1)
            if len(rounds) == 8:  # the last board the waiter needs comes free
                held.release()

        with leases.acquire(
            [A], holder("newcomer"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=sleep, note=self.notes.append,
        ) as taken:
            self.assertEqual([A], [lease.board for lease in taken.leases])
        self.assertGreater(len(rounds), 8, "took UT_0 while UT_1 was held")
        self.assertLessEqual(len(rounds), 12, "gave way for too long afterwards")

    def test_a_multi_board_run_is_served_when_the_boards_never_free_together(self):
        """#225's shape under real contention: two boards, one at a time.

        A run wants both boards; each is held by someone else and comes free
        on its own, with single-board leases trying for them throughout. The
        run gets both, and the rush gets its turn afterwards.
        """
        first = leases.acquire([A], holder("holder-a"), directory=self.dir)
        second = leases.acquire([B], holder("holder-b"), directory=self.dir)
        stop = threading.Event()
        outcome = {}
        counts = {"before the run": 0, "after the run": 0}

        def rush(board):
            def loop():
                while not stop.is_set():
                    try:
                        with leases.acquire(
                            [board], holder("quick"), directory=self.dir,
                            note=self.notes.append,
                        ):
                            counts["after the run" if outcome else "before the run"] += 1
                    except leases.LeaseError:
                        pass  # busy, or told to wait its turn
                    time.sleep(0.05)
            return loop

        def multi_board_run():
            try:
                with leases.acquire(
                    [A, B], holder("run"), directory=self.dir, wait_s=20,
                    note=self.notes.append,
                ) as held:
                    outcome["boards"] = [lease.board for lease in held.leases]
            except leases.LeaseError as error:
                outcome["error"] = str(error)

        run = threading.Thread(target=multi_board_run)
        run.start()
        time.sleep(0.3)  # it is waiting, and its tickets are the older ones
        rushes = [threading.Thread(target=rush(A)), threading.Thread(target=rush(B))]
        for thread in rushes:
            thread.start()
        try:
            time.sleep(0.2)
            first.release()
            time.sleep(1.0)  # UT_0 is free on its own, longer than one stretch
            second.release()
            run.join(timeout=30)
            time.sleep(0.5)  # the rush gets its turn now
        finally:
            stop.set()
            for thread in rushes:
                thread.join(timeout=30)
        self.assertFalse(run.is_alive())
        self.assertEqual([A, B], outcome.get("boards"), outcome.get("error"))
        self.assertEqual(
            0, counts["before the run"],
            "a single-board lease took a board the run was waiting for",
        )
        self.assertGreater(counts["after the run"], 0,
                           "the rush never got a board at all")

    def test_waiting_one_command_out_does_not_take_another_one_s_resource(self):
        """Each resource is given way at in its own right.

        This command wants both boards. The command ahead at UT_0 has
        nothing in its way, so this one waits out its stretch there; the
        command ahead at UT_1 is waiting for the host lock as well and
        cannot have it, so UT_1 is still its turn and this command gets
        neither board.
        """
        self.hold_ticket(A, self.older(), self.wanting(A))
        self.hold_ticket(B, self.older(), self.wanting(leases.HOST_LOCK, B))
        clock, sleep = self._virtual_clock()
        with leases.acquire([], holder("holder"), directory=self.dir, host_lock=True):
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A, B], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep, note=self.notes.append,
                )
            message = str(caught.exception)
            self.assertIn("UT_1 is queued after waiting 30s", message)
            self.assertIn("that command is waiting for host lock too", message)
            # With that command out of the queue, the one at UT_0 is waited
            # out as it would be on its own, and both boards are taken.
            os.unlink(self.ticket_dir(B) / self.older())
            with leases.acquire(
                [A, B], holder("newcomer"), directory=self.dir, wait_s=600,
                clock=lambda: 0.0, sleep=lambda _seconds: None,
            ) as taken:
                self.assertEqual([A, B], [lease.board for lease in taken.leases])

    def test_a_ticket_from_before_the_set_is_read_as_its_own_resource(self):
        # A ticket of an earlier harness records no set, so the queue reads
        # the one resource, as it did before multi-resource waiters.
        self.hold_ticket(A, self.older(), self.live_holder())
        rounds = []
        with leases.acquire([B], holder("holder"), directory=self.dir):
            leases.acquire(
                [A], holder("newcomer"), directory=self.dir, wait_s=600,
                clock=lambda: 0.0, sleep=rounds.append,
            ).release()
        self.assertLessEqual(len(rounds), 4)

    def test_a_set_naming_anything_but_a_resource_is_read_as_the_one_resource(self):
        # A key becomes a lock file name, so nothing that could name another
        # file is read from a ticket.
        self.assertEqual([A], leases.waiting_resources({}, A))
        self.assertEqual([A], leases.waiting_resources({"waiting_for": "UT_1"}, A))
        self.assertEqual([A], leases.waiting_resources(
            {"waiting_for": ["UT_1", {"key": "../elsewhere"}, {"key": ".."},
                             {"key": "."}, {"key": ""}, {}]}, A))
        # An alias is only shown to a person, so a record cannot put a line
        # break or a page of text into an error; a resource named twice
        # costs one probe, not two.
        self.assertEqual(
            [leases.Board("UT_1", B.key)],
            leases.waiting_resources({"waiting_for": [
                {"key": B.key, "resource": "UT_1"}, {"key": B.key},
            ]}, A),
        )
        self.assertEqual(
            [leases.Board("UT_1 oh no", B.key)],
            leases.waiting_resources(
                {"waiting_for": [{"key": B.key, "resource": "UT_1\noh no"}]}, A),
        )
        self.assertEqual(
            60, len(leases.waiting_resources(
                {"waiting_for": [{"key": B.key, "resource": "x" * 200}]}, A)[0].alias))
        self.assertEqual(
            leases._MAX_WAITING_FOR,
            len(leases.waiting_resources({"waiting_for": [
                {"key": f"nrf52840dk-{number}"}
                for number in range(leases._MAX_WAITING_FOR + 10)
            ]}, A)))

    def test_a_host_lock_somebody_holds_keeps_the_queue(self):
        clock, sleep = self._virtual_clock()
        with leases.acquire([], holder("holder"), directory=self.dir, host_lock=True):
            self.hold_ticket(A, self.older(), self.wanting(leases.HOST_LOCK, A))
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep, note=self.notes.append,
                )
        self.assertIn("that command is waiting for host lock too",
                      str(caught.exception))

    def test_a_host_lock_no_running_command_holds_does_not_keep_the_queue(self):
        """A wedged host lock must not hold the bench in the queue.

        The host lock is never taken over, so one no running command holds
        stays locked until a person clears it, whether its record says the
        command is gone, came from another host, or says nothing at all (a
        lease file a release has emptied, still held by a command's child).
        Counting it as held would keep every command that wants a board an
        older waiter asked for giving way until it gave up.
        """
        records = {
            "gone": dead_holder(),
            "another host": self.live_holder(hostname="another-host"),
            "nothing": {},
        }
        for what, record in records.items():
            with self.subTest(record=what):
                path = self.dir / f"{leases.HOST_LOCK.key}.lock"
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
                self.raw.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.ftruncate(fd, 0)
                if record:
                    os.pwrite(fd, (json.dumps(record) + "\n").encode(), 0)
                ticket = self.hold_ticket(
                    A, self.older(), self.wanting(leases.HOST_LOCK, A))
                rounds = []
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=600,
                    clock=lambda: 0.0, sleep=rounds.append,
                ).release()
                self.assertLessEqual(
                    len(rounds), 4, "gave way to a waiter with no way through")
                os.close(self.raw.pop())
                os.unlink(path)
                os.unlink(ticket)

    def test_a_ticket_naming_the_queue_s_own_lock_is_not_read_as_a_resource(self):
        # Every attempt holds takeover.lock shared while it takes its locks,
        # so a ticket naming it would read as a resource a running command
        # holds and giving way to that ticket would never be bounded.
        self.assertEqual(
            [A],
            leases.waiting_resources(
                {"waiting_for": [{"key": leases.TAKEOVER_LOCK.removesuffix(".lock")}]},
                A),
        )
        self.hold_ticket(A, self.older(), self.live_holder(waiting_for=[
            {"key": leases.TAKEOVER_LOCK.removesuffix(".lock"), "resource": "nice try"},
            {"key": A.key, "resource": "UT_0"},
        ]))
        rounds = []
        leases.acquire(
            [A], holder("newcomer"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=rounds.append,
        ).release()
        self.assertLessEqual(len(rounds), 4)

    def test_a_ticket_records_every_resource_its_command_wants(self):
        queue = leases.TicketQueue(
            self.dir, [leases.HOST_LOCK, A, A, B], holder("agent")
        ).take()
        try:
            record = leases.read_holder(self.ticket_dir(A) / queue.name)
            self.assertEqual(
                [{"key": leases.HOST_LOCK.key, "resource": "host lock"},
                 {"key": A.key, "resource": "UT_0"},
                 {"key": B.key, "resource": "UT_1"}],
                record["waiting_for"],
            )
        finally:
            queue.release()


class QueuedReportTests(QueueFixture):
    """A free resource nobody can take says why, and what to clear (#234).

    While a command that wants several resources waits for the last of
    them, giving way to it is not bounded, so the ones already free stay
    queued. That is seconds when what holds the last one is a run that
    ends, and the whole of that command's wait when it is a lease nobody
    will give back. The wait stays bounded by that command's own
    --wait-for-boards; what the bench gets is the reason in words.
    """

    def test_a_free_board_says_which_waiter_has_it_and_what_is_in_the_way(self):
        name = self.waited(252)
        self.hold_ticket(A, name, self.wanting(A, B, wait_s=600.0))
        self.hold_ticket(B, name, self.wanting(A, B, wait_s=600.0))
        with leases.acquire([B], holder("holder"), directory=self.dir):
            states = leases.board_states([A, B], directory=self.dir)
        queued = states[0]["queued_for"]
        self.assertEqual("UT_0", queued["resource"])
        self.assertEqual(name, queued["ticket"])
        self.assertEqual("waiting-agent", queued["command"]["owner"])
        self.assertEqual(
            [{"key": A.key, "resource": "UT_0"}, {"key": B.key, "resource": "UT_1"}],
            queued["waiting_for"],
        )
        self.assertAlmostEqual(252, queued["waited_s"], delta=5)
        self.assertEqual(600.0, queued["wait_s"])
        self.assertEqual(["UT_1"], [item["resource"] for item in queued["blocked_by"]])
        self.assertEqual("live", queued["blocked_by"][0]["lease"])
        self.assertEqual("holder", queued["blocked_by"][0]["holder"]["owner"])
        # The board that is held needs no explaining: it is held.
        self.assertNotIn("queued_for", states[1])
        line = leases.describe_queued_resource(queued)
        self.assertIn("UT_0 is free but queued for waiting-agent", line)
        self.assertIn("it asked for UT_0 and UT_1", line)
        self.assertIn("has waited 4m1", line)
        self.assertIn("of the 10m00s it asked to wait", line)
        self.assertIn("is still waiting for UT_1, held by holder", line)

    def test_what_the_lease_in_the_way_records_decides_how_long_this_lasts(self):
        """The forms of a lease that keeps a waiter queued.

        A board lease whose command is gone is taken over within a poll
        round, so the queue clears itself. One recorded on another host, or
        holding no record at all, is left alone: it stays held until
        somebody clears it, and until then this waiter keeps its other
        boards queued for its whole wait. That difference is the reason to
        report any of this, so each form says which it is.
        """
        cases = {
            "gone": (dead_holder(), True,
                     "the command that took it is gone", "takes it over"),
            "another host": (self.live_holder(hostname="another-host"), False,
                             "that lease is left alone",
                             "stays held until somebody clears it"),
            "no record": ({}, False, "its lock file records no holder",
                          "stays held until somebody clears it"),
        }
        for what, (record, takeover, expected, ending) in cases.items():
            with self.subTest(lease=what):
                lock = self.hold_lease(B, record)
                ticket = self.hold_ticket(A, self.waited(600), self.wanting(A, B))
                queued = leases.board_states([A], directory=self.dir)[0]["queued_for"]
                blocking = queued["blocked_by"][0]
                self.assertEqual("UT_1", blocking["resource"])
                self.assertEqual(takeover, blocking["takeover"])
                line = leases.describe_queued_resource(queued)
                self.assertIn(expected, line)
                self.assertIn(ending, line)
                for _held_file in (lock, ticket):
                    os.close(self.raw.pop())
                os.unlink(lock)
                os.unlink(ticket)

    def test_the_record_goes_when_the_waiter_does(self):
        """A record must never outlive the wait it describes.

        It is read from the waiter's own ticket every time, so a waiter
        that exits (its ticket removed with its wait) or one whose process
        is gone (an expired ticket, dropped by the listing) leaves nothing
        behind to explain.
        """
        with leases.acquire([B], holder("holder"), directory=self.dir):
            ticket = self.hold_ticket(A, self.waited(300), self.wanting(A, B))
            self.assertIn("queued_for", leases.board_states([A], directory=self.dir)[0])
            os.close(self.raw.pop())  # the waiter exits: its ticket is released
            os.unlink(ticket)
            self.assertNotIn(
                "queued_for", leases.board_states([A], directory=self.dir)[0])
            # And a ticket whose command is gone: expired, and removed.
            expired = self.hold_ticket(
                A, self.waited(300),
                dict(dead_holder(), waiting_for=[
                    {"key": A.key, "resource": "UT_0"},
                    {"key": B.key, "resource": "UT_1"},
                ]),
                locked=False,
            )
            self.assertNotIn(
                "queued_for", leases.board_states([A], directory=self.dir)[0])
            self.assertFalse(expired.exists())

    def test_a_waiter_nothing_is_holding_back_is_not_reported(self):
        # Two commands the queue serves in seconds, not the case in #234:
        # one that wants a single board (giving way to it is bounded at half
        # a second), and one that could take everything it asked for right
        # now and simply has not yet.
        single = self.hold_ticket(A, self.waited(300), self.live_holder())
        with leases.acquire([B], holder("holder"), directory=self.dir):
            self.assertNotIn(
                "queued_for", leases.board_states([A], directory=self.dir)[0])
        os.close(self.raw.pop())
        os.unlink(single)
        self.hold_ticket(A, self.waited(200), self.wanting(A, B))
        self.assertNotIn("queued_for", leases.board_states([A], directory=self.dir)[0])

    def test_a_command_that_gives_way_records_it_once(self):
        """What a run carries out of its wait, for its own record.

        The command that waited is the one whose report has to explain a
        late start, so acquire() keeps a record of every free resource it
        gave way at, and says it once on standard error rather than on
        every poll round.
        """
        name = self.waited(120)
        self.hold_ticket(A, name, self.wanting(A, B, wait_s=600.0))
        self.hold_ticket(B, name, self.wanting(A, B, wait_s=600.0))
        held = leases.acquire([B], holder("holder"), directory=self.dir)
        notes, rounds = [], []

        def sleep(_seconds):
            rounds.append(len(rounds) + 1)
            if len(rounds) == 4:
                # The waiter ahead gets UT_1 and its turn is over; the
                # tickets go with it, and this command takes UT_0.
                held.release()
                os.close(self.raw.pop())
                os.close(self.raw.pop())
                os.unlink(self.ticket_dir(A) / name)
                os.unlink(self.ticket_dir(B) / name)

        with leases.acquire(
            [A], holder("newcomer"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=sleep, note=notes.append,
        ) as taken:
            self.assertEqual([A], [lease.board for lease in taken.leases])
        self.assertEqual(1, len(notes), notes)
        self.assertIn("UT_0 is free but queued for waiting-agent", notes[0])
        self.assertEqual(1, len(taken.queued))
        self.assertEqual("UT_0", taken.queued[0]["resource"])
        self.assertEqual(["UT_1"],
                         [item["resource"] for item in taken.queued[0]["blocked_by"]])

    def test_a_command_says_nothing_about_a_resource_that_is_held(self):
        # Giving way at a board somebody is using explains itself, and the
        # error that follows names its holder: nothing to report here.
        name = self.waited(120)
        self.hold_ticket(A, name, self.wanting(A, B))
        self.hold_ticket(B, name, self.wanting(A, B))
        notes = []
        clock, sleep = self._virtual_clock()
        with leases.acquire([A, B], holder("holder"), directory=self.dir):
            with self.assertRaises(leases.LeaseError):
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep, note=notes.append,
                )
        self.assertEqual([], notes)

    def test_the_queued_error_says_whether_what_is_in_the_way_comes_back(self):
        # The command that is told to wait its turn needs the same thing the
        # listing shows: whether the lease in the way will be taken over, or
        # whether somebody has to clear it.
        self.hold_lease(B, self.live_holder(hostname="another-host", owner="elsewhere"))
        self.hold_ticket(A, self.waited(300), self.wanting(A, B))
        clock, sleep = self._virtual_clock()
        with self.assertRaises(leases.LeaseError) as caught:
            leases.acquire(
                [A], holder("newcomer"), directory=self.dir, wait_s=30,
                clock=clock, sleep=sleep, note=lambda _line: None,
            )
        message = str(caught.exception)
        self.assertIn("that command is waiting for UT_1 too", message)
        self.assertIn("that lease is left alone: it was taken on another-host", message)
        self.assertIn("stays held until somebody clears it", message)

    def test_a_wait_reads_as_a_person_would_say_it(self):
        self.assertEqual("0s", leases.describe_seconds(0))
        self.assertEqual("45s", leases.describe_seconds(45.4))
        self.assertEqual("4m12s", leases.describe_seconds(252))
        self.assertEqual("1h02m03s", leases.describe_seconds(3723))
        self.assertEqual("", leases.describe_seconds(None))
        self.assertEqual("", leases.describe_seconds("soon"))
        self.assertEqual("0s", leases.describe_seconds(-5))

    def test_how_long_a_waiter_has_waited_comes_from_its_ticket_name(self):
        now = leases.time.time_ns()
        name = f"{now - 90_000_000_000:020d}-{os.getpid()}-aaaaaa"
        self.assertAlmostEqual(90.0, leases.ticket_waited_s(name, now_ns=now), places=3)
        # A clock set backwards reads as no time at all, never a negative wait.
        self.assertEqual(0.0, leases.ticket_waited_s(
            f"{now + 10_000_000_000:020d}-7-aaaaaa", now_ns=now))
        self.assertIsNone(leases.ticket_waited_s("not-a-ticket"))
        # A ticket of an earlier harness records no deadline, and the line
        # says so rather than making one up.
        record = leases.queued_record(
            A, name, self.wanting(A, B), [], now_ns=now,
        )
        self.assertIsNone(record["wait_s"])
        self.assertIn("of a deadline its ticket does not record",
                      leases.describe_queued(record))


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

    def test_boards_says_why_a_free_board_is_nobody_s_to_take(self):
        """#234: a bench that looks idle explains itself.

        UT_1 is held by a lease recorded on another host, which the harness
        never takes over, and the command whose turn it is wants both
        boards. So UT_0 is free and still nobody's, for as long as that
        command waits or until somebody clears UT_1.
        """
        name = self.waited(252)
        self.hold_lease(B, self.live_holder(hostname="another-host", owner="elsewhere"))
        self.hold_ticket(A, name, self.wanting(A, B, wait_s=600.0, owner="flash-run"))
        self.hold_ticket(B, name, self.wanting(A, B, wait_s=600.0, owner="flash-run"))
        code, out, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.dir),
        )
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^UT_0 +free$")
        self.assertRegex(lines[1], r"^ +queue: 1 command is waiting for it$")
        self.assertIn("queued for flash-run", lines[2])
        self.assertIn("it asked for UT_0 and UT_1", lines[2])
        self.assertIn("of the 10m00s it asked to wait", lines[2])
        self.assertIn("still waiting for UT_1, held by elsewhere", lines[2])
        self.assertIn("that lease is left alone: it was taken on another-host", lines[2])
        self.assertIn("stays held until somebody clears it", lines[2])
        # The held board says it is held, and nothing about the queue's shape.
        self.assertRegex(lines[3], r"^UT_1 +held  elsewhere")
        _code, raw, _err = self._main(
            "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.dir),
            "--json",
        )
        states = json.loads(raw)
        self.assertEqual("UT_1", states[0]["queued_for"]["blocked_by"][0]["resource"])
        self.assertEqual("foreign", states[0]["queued_for"]["blocked_by"][0]["lease"])
        self.assertNotIn("queued_for", states[1])
        # Nothing in any of it names a lock file, which lives in a private
        # directory; the resources and the commands are what a reader needs.
        self.assertNotIn(str(self.dir), out)
        self.assertNotIn(str(self.dir), raw)

    def test_a_run_record_and_a_report_carry_what_the_run_waited_behind(self):
        name = self.waited(252)
        record = leases.queued_record(
            A, name, self.wanting(A, B, wait_s=600.0, owner="flash-run"),
            [{"resource": "UT_1", "key": B.key, "lease": "unknown",
              "why": "its lock file records no holder", "takeover": False,
              "holder": {}}],
        )
        events = []

        class Publisher:
            def emit(self, kind, status, details, **_kwargs):
                events.append((kind, status, details))

        cli._record_lease_queued(Publisher(), SimpleNamespace(queued=[record]))
        self.assertEqual(1, len(events))
        kind, status, details = events[0]
        self.assertEqual(("lease_queued", "warning"), (kind, status))
        self.assertEqual("UT_0", details["resource"])
        self.assertIn("UT_0 is free but queued for flash-run", details["message"])
        # The report's setup table, and the transcript line.
        rows = dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000",
            "details": {"manifest": {"lease_queue_waits": [record]}},
        }])))
        self.assertIn("UT_0 is free but queued for flash-run",
                      rows["Queued behind another command"])
        self.assertNotIn("Queued behind another command", dict(_setup_rows(_Run([{
            "run_id": "r", "event_type": "manifest",
            "timestamp": "2026-01-01T00:00:00.000", "details": {"manifest": {}},
        }]))))
        transcript = render_run([{
            "schema_version": 1, "run_id": "r", "sequence": 1,
            "timestamp": "2026-01-01T00:00:00.000", "event_type": "lease_queued",
            "status": "warning", "details": details,
        }])
        self.assertIn("[M] UT_0 is free but queued for flash-run", transcript)
        # And the browser live view shows the same line.
        js = (Path(__file__).resolve().parents[1] / "ubertooth_harness" / "web"
              / "app.js").read_text(encoding="utf-8")
        self.assertIn('kind === "lease_queued"', js)
        self.assertIn('"Queued behind"', js)

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
