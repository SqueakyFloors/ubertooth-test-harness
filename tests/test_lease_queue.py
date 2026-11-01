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
                    with leases.acquire([A], holder("quick"), directory=self.dir):
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
            self.assertEqual(1, queue.waiting_for(A))
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
            self.assertEqual(1, queue.waiting_for(A))
            self.assertIsNone(queue.ahead())
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

    def test_a_free_board_is_kept_for_a_waiter_held_up_at_another(self):
        self.hold_ticket(A, self.older(), self.wanting(A, B))
        clock, sleep = self._virtual_clock()
        with leases.acquire([B], holder("holder"), directory=self.dir):
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep,
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
                    clock=lambda: 0.0, sleep=rounds.append,
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
            clock=lambda: 0.0, sleep=sleep,
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
                            [board], holder("quick"), directory=self.dir
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
        self.assertEqual(
            [leases.Board("UT_1", B.key), leases.Board(B.key, B.key)],
            leases.waiting_resources({"waiting_for": [
                {"key": B.key, "resource": "UT_1"}, {"key": B.key},
            ]}, A),
        )

    def test_a_host_lock_somebody_holds_keeps_the_queue(self):
        clock, sleep = self._virtual_clock()
        with leases.acquire([], holder("holder"), directory=self.dir, host_lock=True):
            self.hold_ticket(A, self.older(), self.wanting(leases.HOST_LOCK, A))
            with self.assertRaises(leases.LeaseError) as caught:
                leases.acquire(
                    [A], holder("newcomer"), directory=self.dir, wait_s=30,
                    clock=clock, sleep=sleep,
                )
        self.assertIn("that command is waiting for host lock too",
                      str(caught.exception))

    def test_a_host_lock_whose_holder_is_gone_does_not_keep_the_queue(self):
        """A wedged host lock must not hold the bench in the queue.

        The host lock is never taken over, so one whose holder is gone stays
        locked until a person clears it. Counting it as held would keep every
        command that wants a board an older waiter asked for giving way until
        it gave up.
        """
        path = self.dir / f"{leases.HOST_LOCK.key}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        self.raw.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.pwrite(fd, (json.dumps(dead_holder()) + "\n").encode(), 0)
        self.hold_ticket(A, self.older(), self.wanting(leases.HOST_LOCK, A))
        rounds = []
        leases.acquire(
            [A], holder("newcomer"), directory=self.dir, wait_s=600,
            clock=lambda: 0.0, sleep=rounds.append,
        ).release()
        self.assertLessEqual(len(rounds), 4, "gave way to a waiter with no way through")

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
