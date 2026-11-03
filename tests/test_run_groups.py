"""Run groups: one plan on a Linux host and a Mac, one result (#154).

The controller is driven with two scripted runners over real loopback
WebSockets, one registering as Linux and one as macOS. No hardware, no target
and no planner: each run's records are scripted, which is exactly what the
controller has to work from.

The step model (``ubertooth_harness/rungroups.py``) is also tested on its own,
from records alone, because that is what a per-host pill is drawn from.
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from websockets.exceptions import ConnectionClosed, InvalidStatus

from ubertooth_harness.controller import UNKNOWN_GROUP, UNKNOWN_RUN, Controller
from ubertooth_harness.rungroups import (
    BUILD_STEP,
    FLASH_STEP,
    GROUPS_DIR,
    MAX_GROUP_HOSTS,
    MAX_HOST_PARTS,
    MAX_TEST_STEPS,
    NOT_REACHED,
    PREFLIGHT_STEP,
    TEST_STEP,
    combine,
    group_from_record,
    group_progress,
    group_status,
    group_view,
    host_steps,
    implicit_group,
    merge_steps,
    new_group,
)

try:
    from test_controller import (
        COMMIT,
        TIMEOUT_S,
        ControllerTestCase,
        FakeRunner,
        wait_until,
    )
except ImportError:  # run as tests.test_run_groups
    from tests.test_controller import (
        COMMIT,
        TIMEOUT_S,
        ControllerTestCase,
        FakeRunner,
        wait_until,
    )

LINUX = "Host_Linux"
MAC = "Host_Mac"
OPERATING_SYSTEMS = {
    "linux": {"system": "Linux", "release": "6.8", "distribution": "Ubuntu 24.04"},
    "macos": {"system": "Darwin", "release": "24.1.0", "macos": "15.1"},
}
# A test both hosts run, and one only the Linux host plans.
SHARED_TEST = "Host version"
LINUX_ONLY_TEST = "Two-device range test"


def record(
    run_id, sequence, event_type, status="running", timestamp=None, **details
):
    """One run.jsonl record, with a timestamp a duration can be read from."""
    second = sequence if timestamp is None else timestamp
    return {
        "schema_version": 1, "run_id": run_id, "sequence": sequence,
        "timestamp": f"2026-01-01T00:00:{second:02d}.000",
        "event_type": event_type, "status": status, "details": details,
    }


def manifest(run_id, sequence, host, tests, checks=3, timestamp=None):
    """The run's manifest record: what the planner compiled for that host."""
    return record(
        run_id, sequence, "manifest", "recorded", timestamp,
        requested_preflight_checks=checks,
        manifest={"invocations": [
            {
                "id": f"{index:03d}-{name}", "test_name": name, "host": host,
                "arguments": arguments,
            }
            for index, (name, arguments) in enumerate(tests, 1)
        ]},
    )


def test_records(run_id, host, tests, start=10):
    """``test_started``/``test_finished`` pairs for finished invocations."""
    events = []
    sequence = start
    for index, (name, status) in enumerate(tests, 1):
        invocation = f"{index:03d}-{name}"
        events.append(record(
            run_id, sequence, "test_started", "running",
            invocation_id=invocation, test_name=name, host=host,
        ))
        sequence += 1
        events.append(record(
            run_id, sequence, "test_finished", status,
            invocation_id=invocation, test_name=name,
            reason="" if status == "passed" else f"{name} did not pass",
        ))
        sequence += 1
    return events


class ScriptedRunner(FakeRunner):
    """A scripted runner that registers as a Linux host or as a Mac."""

    def __init__(self, port, host, platform="linux", serial="a" * 32):
        super().__init__(port)
        self.host = host
        self.platform = platform
        self.serial = serial

    async def register(self, host=None, interval=0.2, active=None):
        await self.send(
            "register", host=host or self.host, hostname=f"{self.host}-box",
            os=OPERATING_SYSTEMS[self.platform], python="3.12.6",
            ubertooth_serials=[self.serial], enumeration_error=None,
            heartbeat_interval_s=interval, active_run_id=active,
        )


# ----- the step model, from records alone ----------------------------------


class StepModelTests(unittest.TestCase):
    """What a run's records say each of its steps did."""

    def test_statuses_combine_worst_first(self):
        self.assertEqual("passed", combine(["passed", "passed"]))
        self.assertEqual("failed", combine(["passed", "failed"]))
        self.assertEqual("running", combine(["running", "failed"]))
        # A test that passed on one board and does not apply on another
        # passed as far as it could.
        self.assertEqual("passed", combine(["passed", "not_applicable"]))
        # One invocation done and another not yet started is still running.
        self.assertEqual("running", combine(["passed", "pending"]))
        self.assertEqual("pending", combine(["pending", "pending"]))
        self.assertEqual("pending", combine([]))
        # A status this code does not know is a problem, not a pass.
        self.assertEqual("blocked", combine(["passed", "blocked"]))

    def test_a_group_passes_only_when_every_host_passed(self):
        self.assertEqual("passed", group_status([{"status": "passed"}] * 2))
        self.assertEqual(
            "running", group_status([{"status": "passed"}, {"status": "running"}])
        )
        self.assertEqual(
            "failed", group_status([{"status": "passed"}, {"status": "failed"}])
        )
        # A host that never started is an error of the group, and a real
        # failure elsewhere still outranks a cancel.
        self.assertEqual(
            "error", group_status([{"status": "passed"}, {"status": "error"}])
        )
        self.assertEqual(
            "failed", group_status([{"status": "canceled"}, {"status": "failed"}])
        )
        # A run the runner no longer has is worse than a cancel somebody
        # asked for.
        self.assertEqual(
            "lost", group_status([{"status": "canceled"}, {"status": "lost"}])
        )
        # A host still being started has not gone wrong: the group is running.
        self.assertEqual(
            "running", group_status([{"status": "passed"}, {"status": "starting"}])
        )
        self.assertEqual("error", group_status([]))

    def test_steps_carry_each_stage_and_each_test(self):
        events = [
            record("run-1", 1, "run_started"),
            record("run-1", 2, "build_started"),
            record("run-1", 3, "build_finished", "passed", host_version="1.1"),
            record("run-1", 4, "build_flash_started"),
            record("run-1", 5, "build_flash_board_started", resource="UT_0"),
            record("run-1", 6, "build_flash_board_finished", "passed", resource="UT_0"),
            record("run-1", 7, "build_flash_finished", "passed"),
            record("run-1", 8, "preflight_started"),
            record("run-1", 9, "preflight_finished", "passed"),
            *test_records("run-1", LINUX, [(SHARED_TEST, "passed")]),
            record("run-1", 20, "run_finished", "passed"),
        ]
        steps = host_steps(
            events, build=True, flash_build=True, run_status="passed",
            invocations=[{
                "invocation_id": f"001-{SHARED_TEST}", "test_name": SHARED_TEST,
                "estimated_s": 0.5,
            }],
            stages=[{"stage": BUILD_STEP, "estimated_s": 120.0},
                    {"stage": PREFLIGHT_STEP, "estimated_s": 9.0}],
        )
        self.assertEqual(
            [BUILD_STEP, FLASH_STEP, PREFLIGHT_STEP, TEST_STEP],
            [step["kind"] for step in steps],
        )
        build, flash, preflight, test = steps
        self.assertEqual(("passed", 1.0, 120.0),
                         (build["status"], build["elapsed_s"], build["estimated_s"]))
        self.assertEqual(("passed", 3.0), (flash["status"], flash["elapsed_s"]))
        # The boards a flash touched come with it; the step's own record is
        # what says whether flashing passed.
        self.assertEqual(["UT_0"], [item["name"] for item in flash["items"]])
        # Flashing the run's build is not a learned stage, so it has no
        # estimate to fill a progress bar from.
        self.assertIsNone(flash["estimated_s"])
        self.assertEqual(("passed", 9.0), (preflight["status"], preflight["estimated_s"]))
        self.assertEqual((SHARED_TEST, "passed", 0.5), (test["key"], test["status"], test["estimated_s"]))
        self.assertEqual([f"001-{SHARED_TEST}"], [item["id"] for item in test["items"]])

    def test_a_per_board_test_is_one_step_with_one_part_per_board(self):
        run = "run-2"
        events = [
            record(run, 1, "test_started", "running",
                   invocation_id="001-Raw receive", test_name="Raw receive"),
            record(run, 2, "test_finished", "passed",
                   invocation_id="001-Raw receive", test_name="Raw receive"),
            record(run, 3, "test_started", "running",
                   invocation_id="002-Raw receive", test_name="Raw receive"),
            record(run, 4, "test_finished", "failed",
                   invocation_id="002-Raw receive", test_name="Raw receive",
                   reason="no packets"),
        ]
        invocations = [
            {"invocation_id": "001-Raw receive", "test_name": "Raw receive",
             "estimated_s": 20.0},
            {"invocation_id": "002-Raw receive", "test_name": "Raw receive",
             "estimated_s": 20.0},
        ]
        (preflight, test) = host_steps(
            events, run_status="failed", invocations=invocations
        )
        self.assertEqual(PREFLIGHT_STEP, preflight["kind"])
        self.assertEqual(("Raw receive", "failed", 40.0),
                         (test["key"], test["status"], test["estimated_s"]))
        self.assertEqual(["passed", "failed"], [item["status"] for item in test["items"]])
        self.assertEqual("no packets", test["reason"])

    def test_a_planned_step_the_run_never_reached_says_so(self):
        """A run that ended leaves no step pending: it was not reached."""
        events = [record("run-3", 1, "run_started"),
                  record("run-3", 2, "build_started"),
                  record("run-3", 3, "build_finished", "failed", reason="cmake failed")]
        steps = host_steps(
            events, build=True, flash_build=True, run_status="failed",
            invocations=[{"invocation_id": f"001-{SHARED_TEST}",
                          "test_name": SHARED_TEST, "estimated_s": 0.5}],
        )
        self.assertEqual(
            {BUILD_STEP: "failed", FLASH_STEP: NOT_REACHED,
             PREFLIGHT_STEP: NOT_REACHED, SHARED_TEST: NOT_REACHED},
            {step["key"]: step["status"] for step in steps},
        )
        self.assertEqual("cmake failed", steps[0]["reason"])

    def test_a_step_still_open_when_the_run_ended_takes_the_runs_outcome(self):
        events = [record("run-4", 1, "preflight_started"),
                  record("run-4", 2, "preflight_finished", "passed"),
                  *test_records("run-4", MAC, [(SHARED_TEST, "passed")])[:1]]
        (preflight, test) = host_steps(events, run_status="canceled")
        self.assertEqual("passed", preflight["status"])
        self.assertEqual("canceled", test["status"])
        self.assertEqual("canceled", test["items"][0]["status"])

    def test_a_test_that_failed_before_the_cancel_still_reads_as_failed(self):
        """A step is the worst of its parts, settled ones included."""
        run = "run-6"
        events = [
            record(run, 1, "test_started", "running",
                   invocation_id="001-Raw receive", test_name="Raw receive"),
            record(run, 2, "test_finished", "failed",
                   invocation_id="001-Raw receive", test_name="Raw receive",
                   reason="no packets"),
            record(run, 3, "test_started", "running",
                   invocation_id="002-Raw receive", test_name="Raw receive"),
        ]
        (_preflight, test) = host_steps(events, run_status="canceled")
        self.assertEqual(["failed", "canceled"],
                         [item["status"] for item in test["items"]])
        self.assertEqual("failed", test["status"])

    def test_a_blocked_test_has_a_step_of_its_own(self):
        """A required test the planner could not compile here (#273).

        It has no invocation, so without a step of its own the host would
        show nothing for a test that fails its run.
        """
        run = "run-8"
        events = [
            manifest(run, 1, LINUX, [(SHARED_TEST, {"hosts": "each"})]),
            record(run, 2, "blocked", "blocked", host=LINUX,
                   test_name="Flash reliability",
                   reason="alternate Ubertooths needs a second board"),
            *test_records(run, LINUX, [(SHARED_TEST, "passed")]),
        ]
        steps = host_steps(events, run_status="failed")
        rows = {step["key"]: step for step in steps}
        self.assertEqual("blocked", rows["Flash reliability"]["status"])
        self.assertIn("second board", rows["Flash reliability"]["reason"])
        self.assertEqual("passed", rows[SHARED_TEST]["status"])
        # A blocked test ranks worse than a pass, so a host where one board
        # ran and another was blocked does not read as passed.
        both = host_steps(
            [record(run, 1, "blocked", "blocked", host=LINUX,
                    test_name=SHARED_TEST, reason="no switchable port"),
             *test_records(run, LINUX, [(SHARED_TEST, "passed")])],
            run_status="failed",
        )
        self.assertEqual("blocked", both[-1]["status"])

    def test_a_status_out_of_a_record_is_bounded(self):
        events = [record("run-9", 1, "preflight_started"),
                  record("run-9", 2, "preflight_finished", "P" * 5000)]
        (preflight,) = host_steps(events, run_status="running")
        self.assertEqual("error", preflight["status"])

    def test_a_step_that_starts_again_has_not_finished(self):
        run = "run-7"
        events = [
            record(run, 1, "test_started", "running",
                   invocation_id=f"001-{SHARED_TEST}", test_name=SHARED_TEST),
            record(run, 2, "test_finished", "failed",
                   invocation_id=f"001-{SHARED_TEST}", test_name=SHARED_TEST,
                   reason="first try"),
            record(run, 3, "test_started", "running",
                   invocation_id=f"001-{SHARED_TEST}", test_name=SHARED_TEST),
        ]
        (_preflight, test) = host_steps(events, run_status="running")
        (part,) = test["items"]
        self.assertEqual(("running", None, None, ""),
                         (part["status"], part["finished_at"], part["elapsed_s"],
                          part["reason"]))

    def test_what_records_can_turn_into_is_bounded(self):
        """The records are data: a runner's message may be megabytes."""
        long_name = "N" * 5000
        events = [
            # Dropped, not shortened: two invocations that differ past a cut
            # must not become one part.
            record("run-5", index, "test_started", "running",
                   invocation_id=f"{index:03d}-{long_name}", test_name=long_name)
            for index in range(1, 4)
        ] + [
            record("run-5", 100 + index, "test_started", "running",
                   invocation_id=f"{index:03d}-T{index}", test_name=f"T{index}")
            for index in range(300)
        ] + [
            record("run-5", 500 + index, "test_started", "running",
                   invocation_id=f"{index:03d}-many", test_name="many")
            for index in range(200)
        ]
        steps = host_steps(events, run_status="running")
        tests = [step for step in steps if step["kind"] == TEST_STEP]
        self.assertEqual(MAX_TEST_STEPS, len(tests))
        self.assertNotIn(long_name, [step["key"] for step in tests])
        self.assertTrue(all(len(step["key"]) <= 256 for step in tests))
        parts = sum(len(step["items"]) for step in tests)
        self.assertLessEqual(parts, MAX_HOST_PARTS)
        # The per-step limit holds as well, where one test has many boards.
        self.assertTrue(all(len(step["items"]) <= 64 for step in tests))
        # A planned list is bounded the same way.
        planned = host_steps([], run_status="running", invocations=[
            {"invocation_id": f"{index:03d}-P{index}", "test_name": f"P{index}",
             "estimated_s": 1.0}
            for index in range(MAX_TEST_STEPS + 50)
        ])
        self.assertEqual(
            MAX_TEST_STEPS,
            sum(step["kind"] == TEST_STEP for step in planned),
        )

    def test_malformed_records_are_data_not_a_failure(self):
        steps = host_steps(
            ["not a record", {"event_type": "test_started"}, None,
             {"event_type": "build_finished", "status": "passed"}],
            build=True, run_status="running",
        )
        self.assertEqual({BUILD_STEP, PREFLIGHT_STEP},
                         {step["kind"] for step in steps})
        self.assertEqual([], host_steps(None, run_status="running")[0]["items"])

    def test_rows_hold_one_cell_per_host_that_runs_the_step(self):
        linux = host_steps(
            [record("run-1", 1, "preflight_started")],
            build=True, flash_build=True, run_status="running",
            invocations=[
                {"invocation_id": f"001-{SHARED_TEST}", "test_name": SHARED_TEST,
                 "estimated_s": 0.5},
                {"invocation_id": f"002-{LINUX_ONLY_TEST}",
                 "test_name": LINUX_ONLY_TEST, "estimated_s": 60.0},
            ],
        )
        mac = host_steps(
            [], run_status="running",
            invocations=[{"invocation_id": f"001-{SHARED_TEST}",
                          "test_name": SHARED_TEST, "estimated_s": 0.8}],
        )
        rows = merge_steps({LINUX: linux, MAC: mac}, [LINUX, MAC])
        self.assertEqual(
            [BUILD_STEP, FLASH_STEP, PREFLIGHT_STEP, SHARED_TEST, LINUX_ONLY_TEST],
            [row["key"] for row in rows],
        )
        by_key = {row["key"]: row for row in rows}
        # The Mac did not build or flash, and does not run the Linux-only
        # test: no cell, so no pill.
        self.assertEqual([LINUX], list(by_key[BUILD_STEP]["hosts"]))
        self.assertEqual([LINUX], list(by_key[LINUX_ONLY_TEST]["hosts"]))
        self.assertEqual([LINUX, MAC], list(by_key[SHARED_TEST]["hosts"]))
        self.assertEqual("running", by_key[PREFLIGHT_STEP]["status"])
        # One unit per host per step, and the sum of every cell's estimate.
        self.assertEqual({"planned": 7, "completed": 0, "estimated_s": None},
                         group_progress(rows))

    def test_progress_counts_a_step_nobody_will_reach_as_done(self):
        finished = host_steps(
            [record("run-1", 1, "preflight_started"),
             record("run-1", 2, "preflight_finished", "failed", reason="no board")],
            run_status="failed",
            invocations=[{"invocation_id": f"001-{SHARED_TEST}",
                          "test_name": SHARED_TEST, "estimated_s": 0.5}],
        )
        rows = merge_steps({LINUX: finished})
        progress = group_progress(rows)
        self.assertEqual((2, 2), (progress["planned"], progress["completed"]))
        self.assertIsNone(progress["estimated_s"])


class GroupRecordTests(unittest.TestCase):
    """The stored group record, and the group of one a lone run is shown as."""

    def test_a_record_survives_a_round_trip(self):
        group = new_group(
            [LINUX, MAC],
            {"ref": "main", "commit": COMMIT, "plan": "fast", "suite": "fast",
             "tests": [], "build": True, "flash_build": True,
             "with_firmware": False, "with_python": False},
        )
        group.set_member_run(LINUX, "run-1")
        group.set_member_error(MAC, "target Host_Mac is offline")
        again = group_from_record(json.loads(json.dumps(group.record())))
        self.assertEqual(group.record(), again.record())
        self.assertEqual(["run-1"], again.run_ids())
        self.assertTrue(again.group_id.startswith("grp-"))
        # A host that has a run keeps it; its member carries no error.
        self.assertFalse(again.set_member_error(LINUX, "too late"))
        self.assertIsNone(again.member(LINUX)["error"])

    def test_an_unreadable_record_is_refused(self):
        for broken in (
            "not an object", {}, {"group_id": "no spaces here either"},
            {"group_id": "grp-1", "hosts": "Host_Linux"},
            {"group_id": "grp-1", "members": [{"run_id": "run-1"}]},
            {"group_id": "grp-1", "tests": "Host version"},
            # The members must be the hosts asked for, in that order.
            {"group_id": "grp-1", "hosts": [LINUX, MAC],
             "members": [{"host": LINUX, "run_id": None, "error": None}]},
            {"group_id": "grp-1", "hosts": [LINUX, MAC],
             "members": [{"host": MAC}, {"host": LINUX}]},
            # Every other field is checked too: a record this side would
            # read and then trip over is not readable.
            {"group_id": "grp-1", "created_at": 17},
            # One entry per host, or a host's run could never be recorded.
            {"group_id": "grp-1", "hosts": [LINUX, LINUX],
             "members": [{"host": LINUX}, {"host": LINUX}]},
            # One run belongs to one host.
            {"group_id": "grp-1", "hosts": [LINUX, MAC],
             "members": [{"host": LINUX, "run_id": "run-1"},
                         {"host": MAC, "run_id": "run-1"}]},
            {"group_id": "grp-1", "build": "yes"},
            {"group_id": "grp-1", "plan": ["fast"]},
            {"group_id": "grp-1", "hosts": [LINUX],
             "members": [{"host": LINUX, "run_id": 7}]},
        ):
            with self.subTest(broken), self.assertRaises(ValueError):
                group_from_record(broken)

    def test_a_run_of_its_own_is_a_group_of_one(self):
        run = {
            "run_id": "20260101T000000Z-abcdefabcdef", "target": MAC,
            "status": "passed", "ref": "main", "commit": COMMIT, "plan": "fast",
            "suite": "fast", "tests": [], "build": False, "flash_build": False,
            "with_firmware": False, "with_python": False,
            "created_at": "2026-01-01T00:00:00.000+00:00",
            "finished_at": "2026-01-01T00:10:00.000+00:00",
            "cancel_requested": False,
        }
        group = implicit_group(run)
        self.assertEqual(run["run_id"], group.group_id)
        self.assertTrue(group.implicit)
        view = group_view(group, {run["run_id"]: run})
        self.assertEqual(
            ("passed", [MAC], 1, 1, True, run["finished_at"]),
            (view["status"], view["hosts"], view["hosts_asked"],
             view["hosts_started"], view["complete"], view["finished_at"]),
        )

    def test_a_host_still_being_started_is_not_an_error_yet(self):
        """Until every request is answered, a host without a run is starting."""
        group = new_group([LINUX, MAC], {"plan": "fast"})
        group.set_member_run(LINUX, "run-1")
        view = group_view(group, {"run-1": {"run_id": "run-1", "status": "running"}})
        self.assertEqual("running", view["status"])
        self.assertEqual(["running", "starting"],
                         [member["status"] for member in view["members"]])
        self.assertIsNone(view["members"][1]["error"])
        # Once the requests have been answered, a host with no run is an
        # error of the group and says why.
        group.dispatched_at = "2026-01-01T00:00:01.000+00:00"
        view = group_view(group, {"run-1": {"run_id": "run-1", "status": "passed"}})
        self.assertEqual("error", view["status"])
        self.assertEqual("its run was never started", view["members"][1]["error"])

    def test_a_member_whose_run_is_gone_is_an_error_of_the_group(self):
        group = new_group([LINUX], {"plan": "fast"})
        group.set_member_run(LINUX, "run-1")
        view = group_view(group, {})
        self.assertEqual("error", view["status"])
        self.assertFalse(view["complete"])
        self.assertIn("no longer stored", view["members"][0]["error"])


# ----- the controller, with two scripted runners ---------------------------


class RunGroupTestCase(ControllerTestCase):
    """A controller with a scripted Linux runner and a scripted Mac runner."""

    async def runners(self, hosts=(LINUX, MAC)):
        """Both scripted runners, registered, closed when the test ends."""
        made = []
        for host in hosts:
            runner = ScriptedRunner(
                self.port, host,
                "macos" if host == MAC else "linux",
                serial=("b" if host == MAC else "a") * 32,
            )
            await runner.__aenter__()
            self.addAsyncCleanup(runner.__aexit__)
            await self.registered(runner, host)
            made.append(runner)
        return made

    async def start_group(self, hosts, **body):
        """POST /api/run-groups, returning the pending request task."""
        request = {
            "hosts": list(hosts), "ref": "main", "plan": "fast", "suite": "fast",
            **body,
        }
        return asyncio.create_task(self.http("POST", "/api/run-groups", request))

    async def accepted_group(self, runners, run_ids=None, **body):
        """Start a group on every runner given and accept each child run.

        Every runner's request is read before any is answered, so a
        controller that started the children one after another would never
        get past the first runner.
        """
        ids = dict(run_ids or {
            runner.host: f"run-{index}" for index, runner in enumerate(runners, 1)
        })
        post = await self.start_group([runner.host for runner in runners], **body)
        requests = {}
        for runner in runners:
            requests[runner.host] = await runner.recv()
        # Answered in reverse, so neither child waited for the other.
        for runner in reversed(runners):
            await runner.send(
                "run_accepted", request_id=requests[runner.host]["request_id"],
                run_id=ids[runner.host],
            )
        status, payload, _ = await post
        self.assertEqual(201, status, payload)
        return payload["group"], requests, ids

    async def group(self, group_id):
        status, payload, _ = await self.http(
            "GET", f"/api/run-groups/{group_id}"
        )
        self.assertEqual(200, status, payload)
        return payload

    async def feed(self, runner, run_id, events):
        """Send a run's records as the runner would, in order."""
        for event in events:
            await runner.send("event", run_id=run_id, event=event)
        await wait_until(
            lambda: self.controller.runs[run_id].last_event_sequence
            == events[-1]["sequence"]
        )

    async def finish(self, runner, run_id, status="passed", exit_code=0):
        request_id = self.controller.runs[run_id].request_id
        await runner.send(
            "run_finished", request_id=request_id, run_id=run_id,
            status=status, exit_code=exit_code, run_directory=f"/runs/{run_id}",
        )
        await wait_until(lambda: not self.controller.runs[run_id].active)


class GroupStartTests(RunGroupTestCase):

    async def test_a_group_is_judged_by_its_hosts_together(self):
        # The release suite's required step names the Linux host its peers
        # are on. The Mac alone could not measure it, so that group is
        # refused (#285); the bench together measures it once, which is what
        # a run group of the release plan is for (#277).
        linux, mac = await self.runners()
        status, payload, _ = await (
            await self.start_group([MAC], plan="release", suite="release")
        )
        self.assertEqual(422, status, payload)
        for part in ("Recover AFH map", "required on Host_Linux"):
            self.assertIn(part, payload["detail"])
        group, requests, _ids = await self.accepted_group(
            [linux, mac], plan="release", suite="release"
        )
        self.assertEqual([LINUX, MAC], group["hosts"])
        self.assertEqual({"release"}, {m["suite"] for m in requests.values()})

    async def test_a_group_is_told_about_the_suite_before_its_hosts(self):
        # The suite is judged before the hosts are asked, so a group that
        # could not have measured it is told that rather than told its
        # hosts are not here; a suite they could measure gets the per-host
        # answer, as before.
        status, payload, _ = await (
            await self.start_group(["Host_Nowhere"], plan="release", suite="release")
        )
        self.assertEqual(422, status, payload)
        self.assertIn("Recover AFH map", payload["detail"])
        status, payload, _ = await (await self.start_group(["Host_Nowhere"]))
        self.assertEqual(404, status, payload)

    async def test_one_run_per_host_started_together_on_one_commit(self):
        linux, mac = await self.runners()
        group, requests, ids = await self.accepted_group(
            [linux, mac], build=True
        )
        # Every host got the same commit: the ref is resolved once, so a
        # branch that moves while the children start cannot split the group.
        self.assertEqual({COMMIT}, {m["commit"] for m in requests.values()})
        self.assertEqual({"main"}, {m["ref"] for m in requests.values()})
        self.assertEqual({True}, {m["build"] for m in requests.values()})
        self.assertEqual([LINUX, MAC], group["hosts"])
        self.assertEqual(
            [(LINUX, "run-1"), (MAC, "run-2")],
            [(member["host"], member["run_id"]) for member in group["members"]],
        )
        self.assertEqual(("running", 2, 2, True),
                         (group["status"], group["hosts_asked"],
                          group["hosts_started"], group["complete"]))
        # Each child is an ordinary run of its own host, and knows its group.
        for host, run_id in (("Host_Linux", "run-1"), ("Host_Mac", "run-2")):
            run = self.controller.runs[run_id]
            self.assertEqual((host, group["group_id"]), (run.target, run.group_id))
            stored = json.loads((self.results / run_id / "run.json").read_text())
            self.assertEqual(group["group_id"], stored["group_id"])
        # The group record is stored beside the run directories.
        path = self.results / GROUPS_DIR / f"{group['group_id']}.json"
        self.assertEqual(group["group_id"], json.loads(path.read_text())["group_id"])

    async def test_the_group_passes_only_once_every_host_has_passed(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await self.finish(linux, "run-1", "passed")
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual(("running", None), (view["status"], view["finished_at"]))
        await self.finish(mac, "run-2", "passed")
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual("passed", view["status"])
        self.assertEqual(
            self.controller.runs["run-2"].finished_at, view["finished_at"]
        )

    async def test_one_host_failing_fails_the_group(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await self.finish(linux, "run-1", "passed")
        await self.finish(mac, "run-2", "failed", exit_code=1)
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual("failed", view["status"])
        self.assertEqual(
            ["passed", "failed"], [m["status"] for m in view["members"]]
        )

    async def test_a_group_that_started_only_some_hosts_says_so(self):
        """A host that is away keeps its reason; the others still run."""
        (linux,) = await self.runners([LINUX])
        post = await self.start_group([LINUX, MAC])
        message = await linux.recv()
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id="run-1"
        )
        status, payload, _ = await post
        self.assertEqual(201, status, payload)
        group = payload["group"]
        # Still running, because the host that did start is running; what is
        # already known is that the group is not complete.
        self.assertEqual(("running", 2, 1, False),
                         (group["status"], group["hosts_asked"],
                          group["hosts_started"], group["complete"]))
        linux_member, mac_member = group["members"]
        self.assertEqual(("run-1", None), (linux_member["run_id"], linux_member["error"]))
        self.assertIn("unknown target", mac_member["error"])
        self.assertEqual("error", mac_member["status"])
        # The run that did start is a run like any other, and a passing run
        # does not make the group pass.
        await self.finish(linux, "run-1", "passed")
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual("error", view["status"])
        self.assertEqual(["passed", "error"], [m["status"] for m in view["members"]])

    async def test_a_group_no_host_started_is_not_a_group(self):
        status, payload, _ = await self.http(
            "POST", "/api/run-groups",
            {"hosts": [LINUX, MAC], "ref": "main", "plan": "fast", "suite": "fast"},
        )
        self.assertEqual(404, status, payload)
        self.assertIn("no run started", payload["detail"])
        self.assertIn(LINUX, payload["detail"])
        self.assertIn(MAC, payload["detail"])
        self.assertEqual({}, self.controller.groups)
        self.assertFalse((self.results / GROUPS_DIR).exists())
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertEqual([], payload["groups"])

    async def test_a_group_whose_requests_time_out_keeps_a_late_child(self):
        """A request that timed out may still be accepted, so the group stays."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        status, payload, _ = await post
        self.assertEqual(504, status, payload)
        self.assertIn("no run started", payload["detail"])
        self.assertIn("did not answer in time", payload["detail"])
        # The answer names the group it kept, so a caller can follow it.
        self.assertIn("is kept while a request may still be accepted",
                      payload["detail"])
        # Stored, because both requests are still pending. The group is not
        # finished while a child of it may still arrive.
        (group_id,) = list(self.controller.groups)
        self.assertTrue(
            (self.results / GROUPS_DIR / f"{group_id}.json").is_file()
        )
        view = (await self.group(group_id))["group"]
        self.assertEqual(("running", 0, False, None),
                         (view["status"], view["hosts_started"], view["complete"],
                          view["finished_at"]))
        self.assertEqual(["starting", "starting"],
                         [member["status"] for member in view["members"]])
        # The Mac's runner answers late: its run is still one of the group's.
        await mac.send(
            "run_accepted", request_id=requests[MAC]["request_id"], run_id="run-2"
        )
        await wait_until(lambda: "run-2" in self.controller.runs)
        self.assertEqual(group_id, self.controller.runs["run-2"].group_id)
        view = (await self.group(group_id))["group"]
        self.assertEqual(("running", 1, False),
                         (view["status"], view["hosts_started"], view["complete"]))
        self.assertEqual(["starting", "running"],
                         [member["status"] for member in view["members"]])
        # The history list says the same as the group's own view.
        status, payload, _ = await self.http("GET", "/api/run-groups")
        (listed,) = payload["groups"]
        self.assertEqual(
            (view["status"], view["finished_at"],
             [member["status"] for member in view["members"]]),
            (listed["status"], listed["finished_at"],
             [member["status"] for member in listed["members"]]),
        )

    async def test_a_group_no_child_can_join_any_more_is_forgotten(self):
        """A group kept for a pending request goes when that request does."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        self.controller.late_reply_grace_s = 0.1
        post = await self.start_group([LINUX, MAC])
        for runner in (linux, mac):
            await runner.recv()
        self.assertEqual(504, (await post)[0])
        (group_id,) = list(self.controller.groups)
        path = self.results / GROUPS_DIR / f"{group_id}.json"
        self.assertTrue(path.is_file())
        # Once the last request has expired, no child can arrive: a group
        # with none is not a group.
        await wait_until(lambda: not self.controller.groups)
        self.assertFalse(path.exists())
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertEqual([], payload["groups"])
        self.assertTrue(
            any("started no run; forgotten" in line for line in self.logs), self.logs
        )

    async def test_a_request_that_expires_says_so_on_the_group(self):
        """The last word on a host whose runner never answered."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        self.controller.late_reply_grace_s = 0.2
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        await linux.send(
            "run_accepted", request_id=requests[LINUX]["request_id"], run_id="run-1"
        )
        self.assertEqual(201, (await post)[0])
        group_id = self.controller.runs["run-1"].group_id
        # The Mac's request is still outstanding, so that host is starting.
        self.assertEqual(
            "starting",
            self.controller.view_group(
                self.controller.groups[group_id]
            )["members"][1]["status"],
        )
        await wait_until(
            lambda: self.controller.groups[group_id].member(MAC)["error"]
        )
        view = (await self.group(group_id))["group"]
        self.assertIn("did not answer the start request",
                      view["members"][1]["error"])
        self.assertEqual(("running", 1, False),
                         (view["status"], view["hosts_started"], view["complete"]))

    async def test_a_refused_group_leaves_no_record_behind(self):
        """Every host refuses, including one whose refusal wrote the record."""
        linux, mac = await self.runners()
        post = await self.start_group([LINUX, MAC])
        for runner in (linux, mac):
            message = await runner.recv()
            await runner.send(
                "run_rejected", request_id=message["request_id"],
                reason="builds disabled: no libbtbb source",
            )
        status, payload, _ = await post
        self.assertEqual(409, status, payload)
        self.assertIn("builds disabled", payload["detail"])
        self.assertEqual({}, self.controller.groups)
        self.assertEqual([], list((self.results / GROUPS_DIR).glob("*.json")))
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertEqual([], payload["groups"])

    async def test_a_group_whose_hosts_are_all_starting_can_be_cancelled(self):
        """Nothing to cancel yet, but what arrives later is cancelled."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        self.assertEqual(504, (await post)[0])
        (group_id,) = list(self.controller.groups)
        status, payload, _ = await self.http(
            "POST", f"/api/run-groups/{group_id}/cancel", {"reason": "not now"},
        )
        self.assertEqual(202, status, payload)
        self.assertTrue(payload["group"]["cancel_requested"])
        self.assertEqual(
            [(LINUX, None, False), (MAC, None, False)],
            [(item["host"], item["run_id"], item["canceled"])
             for item in payload["canceled"]],
        )
        self.assertIn("canceled as it arrives", payload["canceled"][0]["error"])
        # A runner that accepts now has its run cancelled at once.
        await mac.send(
            "run_accepted", request_id=requests[MAC]["request_id"], run_id="run-2"
        )
        late = await mac.recv()
        self.assertEqual(("cancel", "run-2", "not now"),
                         (late["type"], late["run_id"], late["reason"]))

    async def test_a_child_accepted_after_the_cancel_is_cancelled_too(self):
        """A host that answered late must not run on by itself."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        await linux.send(
            "run_accepted", request_id=requests[LINUX]["request_id"], run_id="run-1"
        )
        self.assertEqual(201, (await post)[0])
        group_id = self.controller.runs["run-1"].group_id
        cancel = asyncio.create_task(self.http(
            "POST", f"/api/run-groups/{group_id}/cancel", {"reason": "bench needed"},
        ))
        asked = await linux.recv()
        await linux.send("cancel_accepted", run_id=asked["run_id"])
        self.assertEqual(202, (await cancel)[0])
        # The Mac's runner accepts its run after the group was cancelled.
        await mac.send(
            "run_accepted", request_id=requests[MAC]["request_id"], run_id="run-2"
        )
        late = await mac.recv()
        self.assertEqual(("cancel", "run-2", "bench needed"),
                         (late["type"], late["run_id"], late["reason"]))
        await mac.send("cancel_accepted", run_id="run-2")
        await wait_until(lambda: self.controller.runs["run-2"].cancel_requested)

    async def test_a_late_refusal_is_recorded_on_the_group(self):
        """The runner's own reason, not "its run was never started"."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        self.assertEqual(504, (await post)[0])
        await linux.send(
            "run_accepted", request_id=requests[LINUX]["request_id"], run_id="run-1"
        )
        await mac.send(
            "run_rejected", request_id=requests[MAC]["request_id"],
            reason="builds disabled: no libbtbb source",
        )
        (group_id,) = list(self.controller.groups)
        await wait_until(
            lambda: self.controller.groups[group_id].member(MAC)["error"]
        )
        view = (await self.group(group_id))["group"]
        self.assertIn(
            "builds disabled", view["members"][1]["error"]
        )
        # Kept, because the Linux host did start; the reason is on disk too.
        stored = json.loads(
            (self.results / GROUPS_DIR / f"{group_id}.json").read_text()
        )
        self.assertIn("builds disabled", stored["members"][1]["error"])

    async def test_a_fault_in_the_controller_is_not_reported_as_a_refusal(self):
        await self.runners([LINUX])
        with patch.object(
            Controller, "_request_run", side_effect=RuntimeError("/private/path broke")
        ):
            with self.assertRaises(RuntimeError):
                await self.controller.start_group(
                    [LINUX], ref="main", plan="fast", suite="fast", tests=[],
                    build=False,
                )
        # Nothing stored, and the log carries the detail rather than the
        # answer or the group record.
        self.assertEqual({}, self.controller.groups)
        self.assertTrue(
            any("/private/path broke" in line for line in self.logs), self.logs
        )

    async def test_a_busy_host_and_an_offline_one_each_give_their_reason(self):
        linux, mac = await self.runners()
        await self.accepted_group([linux], {LINUX: "run-1"})
        # Host_Linux is busy with run-1; the Mac's runner has gone.
        await mac.ws.close()
        await wait_until(lambda: not self.controller.targets[MAC].connected)
        status, payload, _ = await self.http(
            "POST", "/api/run-groups",
            {"hosts": [LINUX, MAC], "ref": "main", "plan": "fast", "suite": "fast"},
        )
        self.assertEqual(409, status, payload)
        self.assertIn("busy: run run-1 is active", payload["detail"])
        self.assertIn(f"target {MAC} is offline", payload["detail"])

    async def test_a_request_that_cannot_be_carried_out_is_refused(self):
        await self.runners([LINUX])
        cases = {
            "no host": ({"hosts": []}, "at least one target"),
            "one host twice": ({"hosts": [LINUX, LINUX]}, "not name a target twice"),
            "not a host name": ({"hosts": ["not a host"]}, "registry host name"),
            "too many hosts": (
                {"hosts": [f"Host_{index}" for index in range(MAX_GROUP_HOSTS + 1)]},
                "at most",
            ),
            "firmware without build": (
                {"hosts": [LINUX], "with_firmware": True},
                "with_firmware requires build",
            ),
            # The bootloader modes that compare need a build, and the one
            # that writes needs the boards flashed with it first (#298).
            "bootloader check without build": (
                {"hosts": [LINUX], "bootloader": "check"},
                "bootloader check requires build",
            ),
            "bootloader update without flashing": (
                {"hosts": [LINUX], "build": True, "flash_build": False,
                 "bootloader": "update"},
                "bootloader update requires flash_build",
            ),
            "a bootloader mode nobody has": (
                {"hosts": [LINUX], "bootloader": "install"},
                "bootloader must be one of",
            ),
            "a ref the repository does not list": (
                {"hosts": [LINUX], "ref": "no-such-branch"}, "is not a branch or tag",
            ),
        }
        for name, (body, expected) in cases.items():
            with self.subTest(name):
                status, payload, _ = await self.http(
                    "POST", "/api/run-groups",
                    {"ref": "main", "plan": "fast", "suite": "fast", **body},
                )
                self.assertEqual(422, status, payload)
                self.assertIn(expected, json.dumps(payload["detail"]))
        self.assertEqual({}, self.controller.groups)

    async def test_history_lists_groups_and_lone_runs_the_same_way(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await self.finish(linux, "run-1", "passed")
        await self.finish(mac, "run-2", "passed")
        # A run started on one target of its own: a group of one.
        post, message = await self.start_run(linux, LINUX)
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id="run-3"
        )
        await post
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertEqual(200, status, payload)
        groups = payload["groups"]
        self.assertEqual(2, len(groups))
        lone, both = groups
        self.assertEqual(("run-3", True, [LINUX], "running"),
                         (lone["group_id"], lone["implicit"], lone["hosts"],
                          lone["status"]))
        self.assertEqual((group["group_id"], False, "passed"),
                         (both["group_id"], both["implicit"], both["status"]))
        # Filtered by host: the Mac was in the group but not in the lone run.
        status, payload, _ = await self.http("GET", f"/api/run-groups?target={MAC}")
        self.assertEqual([group["group_id"]],
                         [view["group_id"] for view in payload["groups"]])
        # A lone run is reachable as its own group, and its run is not.
        self.assertEqual(
            "run-3", (await self.group("run-3"))["group"]["group_id"]
        )
        self.assertEqual(404, (await self.http("GET", "/api/run-groups/run-1"))[0])
        self.assertEqual(404, (await self.http("GET", "/api/run-groups/grp-nothing"))[0])

    async def test_a_stored_group_is_read_again_at_startup(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await self.finish(linux, "run-1", "passed")
        await self.finish(mac, "run-2", "failed", exit_code=1)
        await self.server.stop()
        await self.start_controller()
        self.assertEqual([group["group_id"]], list(self.controller.groups))
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual("failed", view["status"])
        self.assertEqual(["run-1", "run-2"],
                         [member["run_id"] for member in view["members"]])

    async def test_a_group_saved_mid_dispatch_is_not_starting_for_ever(self):
        """Nobody is asking for its children once the process is gone."""
        group = new_group([LINUX, MAC], {"plan": "fast", "suite": "fast"})
        group.set_member_run(LINUX, "run-1")
        directory = self.results / GROUPS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        # As _dispatch saves it when the first child is accepted, before the
        # other host's request has been answered.
        self.assertIsNone(group.record()["dispatched_at"])
        (directory / f"{group.group_id}.json").write_text(
            json.dumps(group.record())
        )
        await self.server.stop()
        await self.start_controller()
        view = self.controller.view_group(self.controller.groups[group.group_id])
        self.assertEqual("error", view["status"])
        self.assertEqual(["error", "error"],
                         [member["status"] for member in view["members"]])
        self.assertIsNotNone(view["finished_at"])

    async def test_a_child_its_group_does_not_list_is_a_group_of_one(self):
        """Only one of the two records was written before the stop."""
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        group_id = group["group_id"]
        # As a crash between the two writes leaves it: the run knows its
        # group, the group does not know the run.
        stored = self.results / GROUPS_DIR / f"{group_id}.json"
        record_data = json.loads(stored.read_text())
        record_data["members"][1]["run_id"] = None
        stored.write_text(json.dumps(record_data))
        await self.server.stop()
        await self.start_controller()
        self.assertEqual(group_id, self.controller.runs["run-2"].group_id)
        # Its updates and events belong to the group it is shown as, not to
        # the one its record names.
        self.assertEqual("run-2", self.controller._group_id_of("run-2"))
        # Listed as its own group of one, and reachable as one.
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertIn("run-2", [view["group_id"] for view in payload["groups"]])
        detail = await self.group("run-2")
        self.assertEqual((True, [MAC]),
                         (detail["group"]["implicit"], detail["group"]["hosts"]))
        # And the group it was started by says that host never started.
        view = (await self.group(group_id))["group"]
        self.assertEqual("its run was never started", view["members"][1]["error"])

    async def test_a_group_whose_record_cannot_be_written_still_starts(self):
        """The children are running, so the answer says so."""
        linux, mac = await self.runners()
        with patch.object(
            Controller, "_persist_group", side_effect=OSError("read-only")
        ):
            group, _requests, _ids = await self.accepted_group([linux, mac])
        self.assertEqual(("running", 2), (group["status"], group["hosts_started"]))
        self.assertEqual(["run-1", "run-2"],
                         [member["run_id"] for member in group["members"]])
        self.assertTrue(
            any("could not write group" in line for line in self.logs), self.logs
        )
        # A restart finds each child as a group of one, since the group was
        # never written.
        await self.server.stop()
        await self.start_controller()
        self.assertEqual({}, self.controller.groups)
        status, payload, _ = await self.http("GET", "/api/run-groups")
        self.assertEqual(
            {"run-1", "run-2"}, {view["group_id"] for view in payload["groups"]}
        )
        self.assertTrue(all(view["implicit"] for view in payload["groups"]))

    async def test_a_group_record_that_cannot_be_written_is_only_a_log_line(self):
        """A runner session must not fall over on a write to a group record."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        await linux.send(
            "run_accepted", request_id=requests[LINUX]["request_id"], run_id="run-1"
        )
        self.assertEqual(201, (await post)[0])
        with patch.object(
            Controller, "_persist_group", side_effect=OSError("read-only")
        ):
            await mac.send(
                "run_rejected", request_id=requests[MAC]["request_id"],
                reason="builds disabled",
            )
            await wait_until(lambda: any(
                "could not record" in line for line in self.logs
            ))
        # The connection and the run are untouched, and the group still has
        # the host that did start.
        self.assertTrue(self.controller.targets[MAC].connected)
        await mac.heartbeat()
        await wait_until(lambda: self.controller.targets[MAC].online)
        view = (await self.group(self.controller.runs["run-1"].group_id))["group"]
        self.assertEqual(["running", "error"],
                         [member["status"] for member in view["members"]])

    async def test_the_watchdog_outlives_a_results_directory_it_cannot_write(self):
        linux, _mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        self.controller.late_reply_grace_s = 0.1
        post = await self.start_group([LINUX])
        await linux.recv()
        self.assertEqual(504, (await post)[0])
        with patch.object(
            Controller, "_persist_group", side_effect=OSError("read-only")
        ):
            # The request expires in the watchdog, which writes the reason on
            # the group: the write fails, the log says so, and the watchdog
            # carries on.
            await wait_until(lambda: any(
                "could not record" in line for line in self.logs
            ))
            self.assertFalse(self.server._monitor_task.done())
            # Still watching: the target goes offline when its heartbeats stop.
            await wait_until(lambda: not self.controller.targets[LINUX].online)

    async def test_an_unreadable_group_record_is_logged_and_left(self):
        directory = self.results / GROUPS_DIR
        directory.mkdir(parents=True)
        (directory / "grp-broken.json").write_text("{not json")
        (directory / "grp-other.json").write_text(json.dumps({"group_id": "grp-x"}))
        (directory / "notes.txt").write_text("nothing to do with groups")
        await self.server.stop()
        await self.start_controller()
        self.assertEqual({}, self.controller.groups)
        self.assertEqual(
            2, sum("skipping" in line for line in self.logs), self.logs
        )

    async def test_the_group_directory_is_not_a_run_id(self):
        """A runner cannot store a run where the group records live."""
        (linux,) = await self.runners([LINUX])
        post, message = await self.start_run(linux, LINUX)
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id=GROUPS_DIR
        )
        status, payload, _ = await post
        self.assertEqual(504, status, payload)
        self.assertNotIn(GROUPS_DIR, self.controller.runs)
        self.assertTrue(
            any("is reserved" in line for line in self.logs), self.logs
        )


class GroupStepTests(RunGroupTestCase):
    """The per-host step results a pill is drawn from."""

    async def test_each_step_has_one_cell_per_host_that_runs_it(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group(
            [linux, mac], build=True
        )
        await self.feed(linux, "run-1", [
            record("run-1", 1, "run_started"),
            record("run-1", 2, "build_started", with_firmware=True, with_python=False),
            record("run-1", 3, "build_finished", "passed"),
            record("run-1", 4, "build_flash_started"),
            record("run-1", 5, "build_flash_finished", "passed"),
            manifest("run-1", 6, LINUX, [
                (SHARED_TEST, {"hosts": "each"}),
                (LINUX_ONLY_TEST, {"platforms": ["linux"]}),
            ]),
            record("run-1", 7, "preflight_started"),
            record("run-1", 8, "preflight_finished", "passed"),
            *test_records("run-1", LINUX, [(SHARED_TEST, "passed")]),
        ])
        await self.feed(mac, "run-2", [
            record("run-2", 1, "run_started"),
            record("run-2", 2, "build_started", with_firmware=True, with_python=False),
            record("run-2", 3, "build_finished", "passed"),
            record("run-2", 4, "build_flash_started"),
            record("run-2", 5, "build_flash_finished", "passed"),
            # The Mac does not plan the Linux-only test (the planner left it
            # out there), so its manifest has one invocation.
            manifest("run-2", 6, MAC, [(SHARED_TEST, {"hosts": "each"})]),
            record("run-2", 7, "preflight_started"),
            record("run-2", 8, "preflight_finished", "passed"),
            *test_records("run-2", MAC, [(SHARED_TEST, "failed")]),
        ])
        detail = await self.group(group["group_id"])
        rows = {row["key"]: row for row in detail["steps"]}
        self.assertEqual(
            [BUILD_STEP, FLASH_STEP, PREFLIGHT_STEP, SHARED_TEST, LINUX_ONLY_TEST],
            [row["key"] for row in detail["steps"]],
        )
        self.assertEqual([LINUX, MAC], list(rows[BUILD_STEP]["hosts"]))
        # Only the Linux host is tested on the Linux-only test, so only it
        # has a cell there; it has not been reached yet.
        self.assertEqual([LINUX], list(rows[LINUX_ONLY_TEST]["hosts"]))
        self.assertEqual("pending", rows[LINUX_ONLY_TEST]["hosts"][LINUX]["status"])
        self.assertEqual(
            {LINUX: "passed", MAC: "failed"},
            {host: cell["status"] for host, cell in rows[SHARED_TEST]["hosts"].items()},
        )
        self.assertEqual("failed", rows[SHARED_TEST]["status"])
        self.assertEqual(
            f"{SHARED_TEST} did not pass",
            rows[SHARED_TEST]["hosts"][MAC]["reason"],
        )
        # Each cell carries what a progress bar needs: when it started, how
        # long it took, and the invocations behind it.
        cell = rows[SHARED_TEST]["hosts"][LINUX]
        self.assertEqual(1.0, cell["elapsed_s"])
        self.assertEqual([f"001-{SHARED_TEST}"], [item["id"] for item in cell["items"]])
        # Nine cells: both hosts build, flash and preflight and run the
        # shared test, and only the Linux host has the Linux-only one left.
        self.assertEqual(
            {"planned": 9, "completed": 8}, {
                key: detail["progress"][key] for key in ("planned", "completed")
            },
        )
        # Each member keeps its own failure summary and planned invocations,
        # as /api/runs/{run_id} reports them for a run on its own.
        mac_member = detail["members"][1]
        self.assertEqual([SHARED_TEST],
                         [item["test_name"] for item in mac_member["invocations"]])
        self.assertEqual([SHARED_TEST], [item["step"] for item in mac_member["failures"]])

    async def test_a_group_whose_runs_have_no_records_yet_lists_what_was_asked(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group(
            [linux, mac], build=True, flash_build=False
        )
        detail = await self.group(group["group_id"])
        # No records yet: the steps the run was asked for, both pending, and
        # no flash step because the run was told not to flash.
        self.assertEqual([BUILD_STEP, PREFLIGHT_STEP],
                         [row["key"] for row in detail["steps"]])
        for row in detail["steps"]:
            self.assertEqual({LINUX: "pending", MAC: "pending"},
                             {host: cell["status"] for host, cell in row["hosts"].items()})
        self.assertEqual({"planned": 4, "completed": 0, "estimated_s": None},
                         detail["progress"])

    async def test_a_member_that_never_started_has_no_steps(self):
        (linux,) = await self.runners([LINUX])
        post = await self.start_group([LINUX, MAC])
        message = await linux.recv()
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id="run-1"
        )
        group = (await post)[1]["group"]
        detail = await self.group(group["group_id"])
        self.assertEqual([LINUX], list(detail["steps"][0]["hosts"]))
        mac_member = detail["members"][1]
        self.assertEqual((None, [], None, []),
                         (mac_member["run"], mac_member["failures"],
                          mac_member["invocations"], mac_member["stages"]))


class GroupCancelTests(RunGroupTestCase):

    async def test_cancel_stops_every_child(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        post = asyncio.create_task(self.http(
            "POST", f"/api/run-groups/{group['group_id']}/cancel",
            {"reason": "the maintainer asked for the bench"},
        ))
        for runner in (linux, mac):
            message = await runner.recv()
            self.assertEqual("cancel", message["type"])
            self.assertEqual("the maintainer asked for the bench", message["reason"])
            await runner.send("cancel_accepted", run_id=message["run_id"])
        status, payload, _ = await post
        self.assertEqual(202, status, payload)
        self.assertEqual(
            [(LINUX, "run-1", True), (MAC, "run-2", True)],
            [(item["host"], item["run_id"], item["canceled"])
             for item in payload["canceled"]],
        )
        self.assertTrue(payload["group"]["cancel_requested"])
        self.assertEqual("the maintainer asked for the bench",
                         payload["group"]["cancel_reason"])
        self.assertTrue(all(
            self.controller.runs[run_id].cancel_requested
            for run_id in ("run-1", "run-2")
        ))
        # The children end as cancelled runs, and so does the group.
        await self.finish(linux, "run-1", "canceled")
        await self.finish(mac, "run-2", "canceled")
        view = (await self.group(group["group_id"]))["group"]
        self.assertEqual("canceled", view["status"])
        # Recorded, so a controller that restarts still knows it was asked.
        stored = json.loads(
            (self.results / GROUPS_DIR / f"{group['group_id']}.json").read_text()
        )
        self.assertTrue(stored["cancel_requested"])

    async def test_a_cancel_that_reaches_one_host_cancels_that_one(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await mac.ws.close()
        await wait_until(lambda: not self.controller.targets[MAC].connected)
        post = asyncio.create_task(self.http(
            "POST", f"/api/run-groups/{group['group_id']}/cancel", {"reason": "stop"},
        ))
        message = await linux.recv()
        await linux.send("cancel_accepted", run_id=message["run_id"])
        status, payload, _ = await post
        self.assertEqual(202, status, payload)
        linux_outcome, mac_outcome = payload["canceled"]
        self.assertEqual((True, None), (linux_outcome["canceled"], linux_outcome["error"]))
        self.assertEqual(False, mac_outcome["canceled"])
        self.assertIn("offline", mac_outcome["error"])
        self.assertTrue(payload["group"]["cancel_requested"])

    async def test_a_cancel_no_host_could_take_is_a_refusal(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        for runner in (linux, mac):
            await runner.ws.close()
        await wait_until(lambda: not any(
            target.connected for target in self.controller.targets.values()
        ))
        status, payload, _ = await self.http(
            "POST", f"/api/run-groups/{group['group_id']}/cancel", {"reason": "stop"},
        )
        self.assertEqual(409, status, payload)
        self.assertIn("could not cancel any run", payload["detail"])
        self.assertFalse(self.controller.groups[group["group_id"]].cancel_requested)

    async def test_a_group_with_nothing_running_cannot_be_cancelled(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        await self.finish(linux, "run-1", "passed")
        await self.finish(mac, "run-2", "passed")
        status, payload, _ = await self.http(
            "POST", f"/api/run-groups/{group['group_id']}/cancel", {"reason": ""},
        )
        self.assertEqual(409, status, payload)
        self.assertIn("no active run", payload["detail"])
        self.assertEqual(
            404,
            (await self.http(
                "POST", "/api/run-groups/grp-nothing/cancel", {"reason": ""}
            ))[0],
        )

    async def test_a_lone_run_is_cancelled_through_its_group_of_one(self):
        (linux,) = await self.runners([LINUX])
        post, message = await self.start_run(linux, LINUX)
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id="run-1"
        )
        await post
        cancel = asyncio.create_task(self.http(
            "POST", "/api/run-groups/run-1/cancel", {"reason": "stop"},
        ))
        asked = await linux.recv()
        await linux.send("cancel_accepted", run_id=asked["run_id"])
        status, payload, _ = await cancel
        self.assertEqual(202, status, payload)
        self.assertTrue(payload["group"]["cancel_requested"])
        self.assertTrue(self.controller.runs["run-1"].cancel_requested)


class GroupLiveTests(RunGroupTestCase):

    async def live(self, query=""):
        from websockets.asyncio.client import connect

        ws = await connect(f"ws://127.0.0.1:{self.port}/ws/live{query}", proxy=None)
        self.addAsyncCleanup(ws.close)
        return ws

    async def received(self, ws, wanted, limit=40):
        """The next message of one of ``wanted`` types."""
        for _ in range(limit):
            message = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
            if message["type"] in wanted:
                return message
        raise AssertionError(f"no {wanted} message arrived")

    async def test_a_group_stream_carries_every_member_run_and_event(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        ws = await self.live(f"?group_id={group['group_id']}")
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
        self.assertEqual("snapshot", snapshot["type"])
        self.assertEqual(group["group_id"], snapshot["group"]["group_id"])
        self.assertEqual(["run-1", "run-2"],
                         [run["run_id"] for run in snapshot["runs"]])
        # Both members' events arrive on the one stream, each naming its run.
        await linux.send("event", run_id="run-1",
                         event=record("run-1", 1, "run_started"))
        await mac.send("event", run_id="run-2",
                       event=record("run-2", 1, "run_started"))
        seen = set()
        while seen != {"run-1", "run-2"}:
            message = await self.received(ws, {"event"})
            seen.add(message["run_id"])
        # A child ending updates the group on the same stream.
        await self.finish(linux, "run-1", "failed", exit_code=1)
        message = await self.received(ws, {"group"})
        self.assertEqual("running", message["group"]["status"])
        await self.finish(mac, "run-2", "passed")
        while True:
            message = await self.received(ws, {"group"})
            if message["group"]["status"] != "running":
                break
        self.assertEqual("failed", message["group"]["status"])

    async def test_a_browser_following_one_run_gets_only_that_run(self):
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        ws = await self.live("?run_id=run-1")
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
        # The group the run is a host of comes with the snapshot, so a client
        # following one host knows what it is part of.
        self.assertEqual(group["group_id"], snapshot["group"]["group_id"])
        await mac.send("event", run_id="run-2",
                       event=record("run-2", 1, "run_started"))
        await linux.send("event", run_id="run-1",
                         event=record("run-1", 1, "run_started"))
        message = await self.received(ws, {"event", "group"})
        self.assertEqual(("event", "run-1"), (message["type"], message["run_id"]))

    async def test_a_group_that_is_forgotten_says_so_on_the_stream(self):
        """A browser must not keep a group the history no longer lists."""
        linux, mac = await self.runners()
        self.controller.reply_timeout_s = 0.3
        self.controller.late_reply_grace_s = 0.1
        ws = await self.live()
        await asyncio.wait_for(ws.recv(), TIMEOUT_S)  # the snapshot
        post = await self.start_group([LINUX, MAC])
        requests = {runner.host: await runner.recv() for runner in (linux, mac)}
        self.assertEqual(504, (await post)[0])
        await mac.send(
            "run_rejected", request_id=requests[MAC]["request_id"],
            reason="builds disabled",
        )
        message = await self.received(ws, {"group"})
        group_id = message["group"]["group_id"]
        while True:
            message = await self.received(ws, {"group"}, limit=200)
            if message.get("removed"):
                break
        self.assertEqual(group_id, message["group"]["group_id"])
        self.assertEqual({}, self.controller.groups)

    async def test_a_child_its_group_does_not_list_streams_as_its_own_group(self):
        """The stream follows the same rule as the API and the history."""
        linux, mac = await self.runners()
        group, _requests, _ids = await self.accepted_group([linux, mac])
        stored = self.results / GROUPS_DIR / f"{group['group_id']}.json"
        record_data = json.loads(stored.read_text())
        record_data["members"][1]["run_id"] = None
        stored.write_text(json.dumps(record_data))
        await self.server.stop()
        await self.start_controller()
        linux, mac = await self.runners()
        await wait_until(lambda: self.controller.targets[MAC].connected)
        ws = await self.live("?group_id=run-2")
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
        self.assertEqual(["run-2"], [run["run_id"] for run in snapshot["runs"]])
        await mac.send("event", run_id="run-2",
                       event=record("run-2", 1, "run_started"))
        message = await self.received(ws, {"event"})
        self.assertEqual("run-2", message["run_id"])

    async def test_a_lone_run_streams_as_its_own_group(self):
        (linux,) = await self.runners([LINUX])
        post, message = await self.start_run(linux, LINUX)
        await linux.send(
            "run_accepted", request_id=message["request_id"], run_id="run-1"
        )
        await post
        ws = await self.live("?group_id=run-1")
        snapshot = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
        self.assertEqual(("run-1", True),
                         (snapshot["group"]["group_id"], snapshot["group"]["implicit"]))
        await linux.send("event", run_id="run-1",
                         event=record("run-1", 1, "run_started"))
        self.assertEqual("run-1", (await self.received(ws, {"event"}))["run_id"])

    async def test_a_stream_of_something_that_does_not_exist_is_closed(self):
        await self.runners([LINUX])
        for query, code in (("?group_id=grp-nothing", UNKNOWN_GROUP),
                            ("?run_id=run-1", UNKNOWN_RUN)):
            with self.subTest(query), self.assertRaises(ConnectionClosed) as raised:
                ws = await self.live(query)
                while True:
                    await asyncio.wait_for(ws.recv(), TIMEOUT_S)
            self.assertEqual(code, raised.exception.rcvd.code)

    async def test_a_stream_follows_a_run_or_a_group_but_not_both(self):
        """Refused at the handshake, like any other request the guard stops."""
        for query in ("?run_id=run-1&group_id=grp-1", f"?group_id={'g' * 70}"):
            with self.subTest(query), self.assertRaises(InvalidStatus) as raised:
                await self.live(query)
            self.assertEqual(403, raised.exception.response.status_code)


class GroupEndpointTests(ControllerTestCase):

    async def test_the_summary_lists_the_group_endpoints(self):
        status, payload, _ = await self.http("GET", "/")
        self.assertEqual(200, status)
        for endpoint in (
            "POST /api/run-groups", "GET /api/run-groups",
            "GET /api/run-groups/{group_id}",
            "POST /api/run-groups/{group_id}/cancel",
        ):
            self.assertIn(endpoint, payload["endpoints"])
        self.assertIn("group_id", payload["endpoints"]["WS /ws/live"])

    async def test_the_group_endpoints_are_behind_the_same_guard(self):
        """Starting or cancelling a group is a state change like any other."""
        body = {"hosts": ["Host_A"], "ref": "main", "plan": "fast", "suite": "fast"}
        for path, sent, headers in (
            ("/api/run-groups", json.dumps(body), {"Content-Type": "text/plain"}),
            ("/api/run-groups", body, {"Origin": "http://attacker.example"}),
            ("/api/run-groups/grp-1/cancel", body, {"Origin": "http://attacker.example"}),
            ("/api/run-groups/grp-1/cancel", "{}", {"Content-Type": "text/plain"}),
        ):
            with self.subTest(path=path, headers=headers):
                status, payload, _ = await self.http("POST", path, sent, headers)
                self.assertEqual(403, status, payload)
        # Reading them is refused from another origin too.
        status, payload, _ = await self.http(
            "GET", "/api/run-groups", headers={"Origin": "http://attacker.example"}
        )
        self.assertEqual(403, status, payload)

    async def test_the_group_request_takes_the_run_fields_and_nothing_else(self):
        status, payload, _ = await self.http(
            "POST", "/api/run-groups",
            {"hosts": ["Host_A"], "ref": "main", "plan": "fast", "suite": "fast",
             "targets": ["Host_B"]},
        )
        self.assertEqual(422, status, payload)
        self.assertIn("targets", json.dumps(payload["detail"]))


if __name__ == "__main__":
    unittest.main()
