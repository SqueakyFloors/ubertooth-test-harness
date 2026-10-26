"""Controller service on 127.0.0.1 with an ephemeral port and no hardware.

Most tests drive the controller with a scripted protocol-v1 runner; one runs
the real target runner end to end with the Host-only plan the runner tests
use.
"""

import asyncio
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ubertooth_harness.cli import main
from ubertooth_harness.controller import (
    BIND_ADDRESS,
    Controller,
    ControllerConfigError,
    ControllerServer,
    bind_loopback,
    check_request,
    default_results_dir,
    refuse_bind_overrides,
)
from ubertooth_harness.gui import GitRefSource
from ubertooth_harness.protocol import (
    decode_controller_message,
    encode_runner_message,
)
from ubertooth_harness.report import render_run
from ubertooth_harness.runner_service import RunnerService

try:
    from test_runner_service import RunnerFixture, StubEnumerator
except ImportError:  # run as tests.test_controller
    from tests.test_runner_service import RunnerFixture, StubEnumerator

TIMEOUT_S = 30
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# What `git ls-remote` prints for the refs these tests start runs with.
LS_REMOTE = (
    f"{COMMIT}\tHEAD\n{COMMIT}\trefs/heads/main\n{COMMIT}\trefs/tags/v1.2\n"
).encode()


def fake_ref_source(output=LS_REMOTE, code=0, calls=None):
    """A GitRefSource whose git ls-remote is scripted (no subprocess)."""
    async def git(argv, timeout_s):
        if calls is not None:
            calls.append(list(argv))
        return code, output, b"fatal: repository not found\n" if code else b""
    return GitRefSource("https://example.invalid/ubertooth.git", git=git)


def _http(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(BIND_ADDRESS, port, timeout=TIMEOUT_S)
    try:
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        if "json" in (response.getheader("content-type") or ""):
            payload = json.loads(raw)
        else:
            payload = raw.decode("utf-8")
        return response.status, payload, response
    finally:
        connection.close()


def event(run_id, sequence, event_type="test_started", status="running", **details):
    """One run.jsonl record shaped like RunEvent.to_dict()."""
    if event_type == "test_started":
        details.setdefault("invocation_id", f"{sequence:03d}-Host version")
    return {
        "schema_version": 1, "run_id": run_id, "sequence": sequence,
        "timestamp": "2026-01-01T00:00:00Z", "event_type": event_type,
        "status": status, "details": details,
    }


class FakeRunner:
    """Scripted protocol-v1 runner over a real loopback WebSocket."""

    def __init__(self, port):
        self.url = f"ws://127.0.0.1:{port}/ws/runner"
        self.ws = None

    async def __aenter__(self):
        self.ws = await connect(self.url, proxy=None, max_size=None)
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def send(self, message_type, **fields):
        await self.ws.send(encode_runner_message(message_type, **fields))

    async def register(self, host="Host_A", interval=0.2, active=None):
        await self.send(
            "register", host=host, hostname="fake-host",
            os={"system": "Linux", "release": "6.8"}, python="3.12.6",
            ubertooth_serials=["0000000000000000000000000000abcd"],
            enumeration_error=None, heartbeat_interval_s=interval,
            active_run_id=active,
        )

    async def heartbeat(self, active=None):
        await self.send(
            "heartbeat", active_run_id=active, elapsed_s=None,
            last_event_sequence=None, last_event_type=None,
        )

    async def recv(self):
        return decode_controller_message(
            await asyncio.wait_for(self.ws.recv(), TIMEOUT_S)
        )


async def wait_until(predicate, timeout=TIMEOUT_S, interval=0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


class ControllerTestCase(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.results = self.root / "results"
        self.logs = []
        await self.start_controller()

    async def start_controller(self):
        self.controller = Controller(
            self.results, reply_timeout_s=5, monitor_interval_s=0.05,
            log=self.logs.append, ref_source=fake_ref_source(),
        )
        self.server = await ControllerServer(self.controller, 0, quiet=True).start()
        self.port = self.server.port

    async def asyncTearDown(self):
        await self.server.stop()
        self._directory.cleanup()

    async def http(self, method, path, body=None, headers=None):
        return await asyncio.to_thread(_http, self.port, method, path, body, headers)

    async def registered(self, runner, host="Host_A", **kwargs):
        await runner.register(host, **kwargs)
        await wait_until(
            lambda: host in self.controller.targets
            and self.controller.targets[host].connected
            and self.controller._links.get(host) is not None
        )

    async def start_run(self, runner, host="Host_A", **body):
        request = {"ref": "main", "plan": "fast", "suite": "fast", **body}
        post = asyncio.create_task(
            self.http("POST", f"/api/targets/{host}/runs", request)
        )
        message = await runner.recv()
        return post, message

    async def accepted_run(self, runner, run_id="run-1", host="Host_A"):
        post, message = await self.start_run(runner, host)
        await runner.send("run_accepted", request_id=message["request_id"], run_id=run_id)
        status, payload, _ = await post
        self.assertEqual(201, status, payload)
        return message


class BindTests(ControllerTestCase):

    async def test_listener_is_loopback_and_root_is_json(self):
        addresses = {
            sock.getsockname()[0]
            for server in self.server.server.servers for sock in server.sockets
        }
        self.assertEqual({"127.0.0.1"}, addresses)
        status, payload, _ = await self.http("GET", "/")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["protocol_version"])
        self.assertEqual("127.0.0.1", payload["bind"])
        self.assertIn("POST /api/targets/{host}/runs", payload["endpoints"])
        # No CDN-backed docs pages.
        self.assertEqual(404, (await self.http("GET", "/docs"))[0])

    async def test_bind_overrides_are_refused(self):
        with self.assertRaises(ControllerConfigError):
            refuse_bind_overrides({"UVICORN_HOST": "0.0.0.0"})
        refuse_bind_overrides({"HOST": "my-laptop"})  # zsh sets HOST; ignored
        for port in (-1, 65536, "80", True):
            with self.subTest(port), self.assertRaises(ControllerConfigError):
                bind_loopback(port)
        with patch.dict(os.environ, {"UBERTOOTH_CONTROLLER_HOST": "0.0.0.0"}), \
                patch("socket.socket.bind", side_effect=AssertionError("bound")):
            with self.assertRaisesRegex(ControllerConfigError, "no bind override"):
                await ControllerServer(self.controller, 0, quiet=True).start()

    def test_cli_has_no_bind_option(self):
        for argv in (["--host", "0.0.0.0"], ["--bind=::"], ["--address", "10.0.0.1"]):
            stderr = io.StringIO()
            with self.subTest(argv), contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                main(["controller", *argv])
            self.assertEqual(2, raised.exception.code)
            self.assertIn("binds 127.0.0.1 only", stderr.getvalue())

    def test_cli_refuses_environment_override_before_binding(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {"UVICORN_HOST": "0.0.0.0"}), \
                patch("socket.socket.bind", side_effect=AssertionError("bound")), \
                contextlib.redirect_stderr(stderr):
            result = main(["controller", "--port", "0", "--results-dir", str(self.results)])
        self.assertEqual(2, result)
        self.assertIn("no bind override", stderr.getvalue())

    def test_default_results_dir_is_outside_the_repository(self):
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(
            Path("/data/ubertooth-harness/controller/runs"),
            default_results_dir({"XDG_DATA_HOME": "/data"}),
        )
        default = default_results_dir({})
        self.assertNotIn(repository, default.parents)
        self.assertEqual(Path.home() / ".local/share", default.parents[2])


class RequestGuardTests(ControllerTestCase):

    async def test_host_and_origin_are_checked_on_rest(self):
        port = self.port
        same = f"http://127.0.0.1:{port}"
        cases = {
            "rebound name": ({"Host": f"attacker.example:{port}"}, 403),
            "other port": ({"Host": f"127.0.0.1:{port + 1}"}, 403),
            "no port": ({"Host": "127.0.0.1"}, 403),
            "ipv6 host": ({"Host": f"[::1]:{port}"}, 403),
            "foreign origin": ({"Origin": "http://attacker.example"}, 403),
            "null origin": ({"Origin": "null"}, 403),
            "other-port origin": ({"Origin": f"http://127.0.0.1:{port + 1}"}, 403),
            "localhost origin for 127 host": ({"Origin": f"http://localhost:{port}"}, 403),
            "cross-site fetch": ({"Sec-Fetch-Site": "cross-site"}, 403),
            "same origin": ({"Origin": same, "Sec-Fetch-Site": "same-origin"}, 200),
            "localhost": ({"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"}, 200),
            "no origin (curl)": ({}, 200),
        }
        for name, (headers, expected) in cases.items():
            with self.subTest(name):
                status, payload, _ = await self.http("GET", "/api/targets", headers=headers)
                self.assertEqual(expected, status, payload)

    async def test_state_changing_requests_must_be_json(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data; boundary=x"):
            with self.subTest(content_type):
                status, payload, _ = await self.http(
                    "POST", "/api/targets/Host_A/runs",
                    '{"ref": "main", "plan": "fast", "suite": "fast"}',
                    {"Content-Type": content_type},
                )
                self.assertEqual(403, status)
                self.assertIn("application/json", payload["detail"])
        # A cross-origin JSON post is refused before the route runs.
        status, _, _ = await self.http(
            "POST", "/api/targets/Host_A/runs",
            {"ref": "main", "plan": "fast", "suite": "fast"},
            {"Origin": "http://attacker.example"},
        )
        self.assertEqual(403, status)

    async def _ws_status(self, url, headers=None, host=None):
        sock = socket.create_connection((BIND_ADDRESS, self.port))
        try:
            async with connect(
                url.replace("HOST", host or f"127.0.0.1:{self.port}"),
                sock=sock, proxy=None, additional_headers=headers or {},
            ) as ws:
                return 101, ws
        except InvalidStatus as error:
            return error.response.status_code, None

    async def test_host_and_origin_are_checked_on_websockets(self):
        port = self.port
        cases = [
            ("ws://HOST/ws/live", {"Origin": "http://attacker.example"}, None, 403),
            ("ws://HOST/ws/live", {}, f"rebind.example:{port}", 403),
            ("ws://HOST/ws/runner", {"Origin": f"http://127.0.0.1:{port}"}, None, 403),
            ("ws://HOST/ws/runner", {}, f"attacker.example:{port}", 403),
            ("ws://HOST/ws/live", {"Origin": f"http://127.0.0.1:{port}"}, None, 101),
            ("ws://HOST/ws/live", {}, f"localhost:{port}", 101),
        ]
        for url, headers, host, expected in cases:
            with self.subTest(url=url, headers=headers, host=host):
                status, _ = await self._ws_status(url, headers, host)
                self.assertEqual(expected, status)

    def test_non_loopback_client_is_refused(self):
        scope = {
            "type": "http", "method": "GET", "path": "/",
            "client": ("192.0.2.4", 5555),
            "headers": [(b"host", b"127.0.0.1:9")],
        }
        self.assertIn("loopback", check_request(scope, 9))
        scope["client"] = ("127.0.0.1", 5555)
        self.assertIsNone(check_request(scope, 9))
        self.assertIsNotNone(check_request(scope, None))
        scope["headers"] = [(b"host", b"127.0.0.1:9"), (b"host", b"127.0.0.1:9")]
        self.assertIn("exactly one Host", check_request(scope, 9))


class RegistryTests(ControllerTestCase):

    async def test_register_heartbeat_and_offline(self):
        async with connect(
            f"ws://127.0.0.1:{self.port}/ws/live", proxy=None
        ) as live, FakeRunner(self.port) as runner:
            snapshot = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
            self.assertEqual("snapshot", snapshot["type"])
            self.assertEqual([], snapshot["targets"])
            await self.registered(runner)
            status, payload, _ = await self.http("GET", "/api/targets")
            self.assertEqual(200, status)
            (target,) = payload["targets"]
            self.assertEqual("Host_A", target["host"])
            self.assertEqual("online", target["status"])
            self.assertTrue(target["connected"])
            self.assertEqual(["0000000000000000000000000000abcd"], target["ubertooth_serials"])
            self.assertEqual("Linux", target["os"]["system"])
            update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
            self.assertEqual(("target", "online"), (update["type"], update["target"]["status"]))

            # Heartbeats keep it online past three intervals.
            for _ in range(5):
                await runner.heartbeat()
                await asyncio.sleep(0.15)
            self.assertTrue(self.controller.targets["Host_A"].online)
            self.assertIsNotNone(self.controller.targets["Host_A"].last_heartbeat)

            # Missed heartbeats: offline while still connected.
            await wait_until(lambda: not self.controller.targets["Host_A"].online)
            status, payload, _ = await self.http("GET", "/api/targets/Host_A")
            self.assertEqual("offline", payload["target"]["status"])
            self.assertTrue(payload["target"]["connected"])
            update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
            self.assertEqual("offline", update["target"]["status"])
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "main", "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(409, status)
            self.assertIn("offline", payload["detail"])

            # Any valid message brings it back.
            await runner.heartbeat()
            await wait_until(lambda: self.controller.targets["Host_A"].online)
            update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
            self.assertEqual("online", update["target"]["status"])
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)
        self.assertFalse(self.controller.targets["Host_A"].online)
        self.assertEqual(404, (await self.http("GET", "/api/targets/Host_B"))[0])


class RunTests(ControllerTestCase):

    async def test_run_is_dispatched_streamed_and_persisted(self):
        # Recorded only: the controller never reads the runner's directory.
        runner_dir = self.root / "runner-runs" / "run-1"
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            post, message = await self.start_run(
                runner, ref="v1.2", plan="fast", suite="fast",
                tests=["Host version"], build=True,
            )
            self.assertEqual("run", message["type"])
            self.assertEqual(
                ("v1.2", "fast", "fast", ["Host version"], True),
                (message["ref"], message["plan"], message["suite"],
                 message["tests"], message["build"]),
            )
            await runner.send("run_accepted", request_id=message["request_id"], run_id="run-1")
            status, payload, _ = await post
            self.assertEqual(201, status, payload)
            self.assertEqual("run-1", payload["run"]["run_id"])
            self.assertEqual("running", payload["run"]["status"])
            self.assertEqual("run-1", self.controller.targets["Host_A"].active_run_id)

            async with connect(
                f"ws://127.0.0.1:{self.port}/ws/live?run_id=run-1", proxy=None
            ) as live:
                snapshot = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
                self.assertEqual("run-1", snapshot["run"]["run_id"])
                events = [
                    event("run-1", 1, "run_started"),
                    event("run-1", 2, "test_started", name="Host version"),
                    event("run-1", 3, "run_finished", "passed", completed_tests=1,
                          requested_tests=1),
                ]
                for record in events:
                    await runner.send("event", run_id="run-1", event=record)
                await runner.send(
                    "run_finished", request_id=message["request_id"], run_id="run-1",
                    status="passed", exit_code=0, run_directory=str(runner_dir),
                )
                live_events = []
                while True:
                    update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
                    if update["type"] == "event":
                        live_events.append(update["event"])
                    if update["type"] == "run" and update["run"]["status"] != "running":
                        break
                self.assertEqual(events, live_events)
                self.assertEqual("passed", update["run"]["status"])

        directory = self.results / "run-1"
        on_disk = [json.loads(line) for line in (directory / "run.jsonl").read_text().splitlines()]
        self.assertEqual(events, on_disk)
        record = json.loads((directory / "run.json").read_text())
        self.assertEqual("passed", record["status"])
        self.assertEqual(0, record["exit_code"])
        self.assertEqual(str(runner_dir), record["runner_run_directory"])
        self.assertEqual("Host_A", record["target"])
        self.assertEqual("v1.2", record["ref"])
        report = (directory / "report.md").read_text()
        self.assertEqual(render_run(events), report)
        self.assertIn("**passed**: 1/1 tests", report)
        self.assertEqual(
            sorted(["run.json", "run.jsonl", "report.md"]),
            sorted(path.name for path in directory.iterdir()),
        )

        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(200, status)
        self.assertEqual(events, payload["events"])
        self.assertEqual(3, payload["run"]["event_count"])
        status, text, response = await self.http("GET", "/api/runs/run-1/report.md")
        self.assertEqual(200, status)
        self.assertTrue(response.getheader("content-type").startswith("text/markdown"))
        self.assertIn("passed", text)
        status, payload, _ = await self.http("GET", "/api/runs?target=Host_A")
        self.assertEqual(["run-1"], [run["run_id"] for run in payload["runs"]])
        self.assertEqual([], (await self.http("GET", "/api/runs?target=Other"))[1]["runs"])
        self.assertEqual(404, (await self.http("GET", "/api/runs/missing"))[0])
        self.assertEqual(404, (await self.http("GET", "/api/runs/..%2Fetc"))[0])
        self.assertIsNone(self.controller.targets["Host_A"].active_run_id)

    async def test_run_error_is_persisted(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            await runner.send(
                "run_error", request_id=message["request_id"], run_id="run-1",
                exit_code=2, reason="cannot resolve ref",
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "error")
        record = json.loads((self.results / "run-1" / "run.json").read_text())
        self.assertEqual(("error", 2, "cannot resolve ref"),
                         (record["status"], record["exit_code"], record["reason"]))
        # No run.jsonl yet: a short report still names the error (#22).
        self.assertTrue(record["report"])
        self.assertIsNone(record["report_error"])
        status, body, _ = await self.http("GET", "/api/runs/run-1/report.md")
        self.assertEqual(200, status)
        self.assertIn("cannot resolve ref", body)
        self.assertIn("no run records", body)

    async def test_busy_rejection_is_propagated(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            post, message = await self.start_run(runner)
            await runner.send(
                "run_rejected", request_id=message["request_id"],
                reason="busy: run other is active",
            )
            status, payload, _ = await post
            self.assertEqual(409, status)
            self.assertIn("busy", payload["detail"])
        self.assertEqual({}, self.controller.runs)

    async def test_controller_refuses_a_busy_target_without_asking(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active="other-run")
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "main", "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(409, status)
            self.assertIn("busy: run other-run", payload["detail"])
            await runner.heartbeat(active=None)
            await wait_until(
                lambda: self.controller.targets["Host_A"].active_run_id is None
            )
            # One outstanding start request per target.
            post, _ = await self.start_run(runner)
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "main", "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(409, status)
            self.assertIn("already pending", payload["detail"])
            self.controller.reply_timeout_s = 0.1
            self.assertEqual(504, (await post)[0])

    async def test_invalid_run_requests(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            base = {"ref": "main", "plan": "fast", "suite": "fast"}
            for body in (
                {**base, "ref": "--upload-pack=x"},
                {**base, "plan": "../etc/passwd"},
                {**base, "extra": 1},
                {**base, "build": "yes"},
                {"ref": "main"},
            ):
                with self.subTest(body):
                    status, _, _ = await self.http("POST", "/api/targets/Host_A/runs", body)
                    self.assertEqual(422, status)
            status, _, _ = await self.http("POST", "/api/targets/Nope/runs", base)
            self.assertEqual(404, status)

    async def test_no_answer_times_out(self):
        self.controller.reply_timeout_s = 0.2
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            post, _ = await self.start_run(runner)
            self.assertEqual(504, (await post)[0])

    async def test_cancel(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            cancel = asyncio.create_task(
                self.http("POST", "/api/runs/run-1/cancel", {"reason": "operator"})
            )
            message = await runner.recv()
            self.assertEqual(("cancel", "run-1", "operator"),
                             (message["type"], message["run_id"], message["reason"]))
            await runner.send("cancel_accepted", run_id="run-1")
            status, payload, _ = await cancel
            self.assertEqual(202, status, payload)
            self.assertTrue(payload["run"]["cancel_requested"])

            cancel = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            message = await runner.recv()
            self.assertEqual("", message["reason"])
            await runner.send("cancel_rejected", run_id="run-1", reason="no such active run")
            status, payload, _ = await cancel
            self.assertEqual(409, status)
            self.assertIn("no such active run", payload["detail"])

            # A cancel that timed out is sent again, not left waiting.
            self.controller.reply_timeout_s = 0.1
            cancel = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            await runner.recv()
            self.assertEqual(504, (await cancel)[0])
            self.controller.reply_timeout_s = 5
            cancel = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            self.assertEqual("cancel", (await runner.recv())["type"])
            await runner.send("cancel_accepted", run_id="run-1")
            self.assertEqual(202, (await cancel)[0])

            await runner.send(
                "run_finished", request_id=self.controller.runs["run-1"].request_id,
                run_id="run-1", status="canceled", exit_code=1,
                run_directory=str(self.root / "absent"),
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "canceled")
            status, payload, _ = await self.http("POST", "/api/runs/run-1/cancel", {})
            self.assertEqual(409, status)
            self.assertIn("not active", payload["detail"])
            self.assertEqual(404, (await self.http("POST", "/api/runs/zzz/cancel", {}))[0])
        # No records: a short report names the status.
        self.assertTrue(self.controller.runs["run-1"].report)
        self.assertIn("canceled", (self.results / "run-1" / "report.md").read_text())

    async def test_replayed_events_are_deduplicated_across_reconnect_and_restart(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            for sequence in (1, 2):
                await runner.send("event", run_id="run-1", event=event("run-1", sequence))
            await wait_until(lambda: len(self.controller.runs["run-1"].seen) == 2)
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)

        # Reconnect: the runner replays from the start.
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active="run-1")
            for sequence in (1, 2, 3):
                await runner.send("event", run_id="run-1", event=event("run-1", sequence))
            await wait_until(lambda: len(self.controller.runs["run-1"].seen) == 3)
            self.assertEqual(2, self.controller.runs["run-1"].duplicate_events)

        # Controller restart: state reloads from the results directory.
        await self.server.stop()
        await self.start_controller()
        self.assertEqual({1, 2, 3}, self.controller.runs["run-1"].seen)
        self.assertEqual("running", self.controller.runs["run-1"].status)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active="run-1")
            for sequence in (1, 2, 3, 4):
                await runner.send("event", run_id="run-1", event=event("run-1", sequence))
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="passed", exit_code=0, run_directory=str(self.root),
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "passed")
        lines = (self.results / "run-1" / "run.jsonl").read_text().splitlines()
        self.assertEqual([1, 2, 3, 4], [json.loads(line)["sequence"] for line in lines])

    async def test_run_unknown_to_the_controller_is_adopted(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active="orphan-1")
            await runner.send("event", run_id="orphan-1", event=event("orphan-1", 1))
            await runner.send(
                "run_finished", request_id="req-old", run_id="orphan-1",
                status="failed", exit_code=1, run_directory=str(self.root),
            )
            await wait_until(
                lambda: "orphan-1" in self.controller.runs
                and self.controller.runs["orphan-1"].status == "failed"
            )
        status, payload, _ = await self.http("GET", "/api/runs/orphan-1")
        self.assertTrue(payload["run"]["adopted"])
        self.assertEqual("Host_A", payload["run"]["target"])
        self.assertEqual(1, len(payload["events"]))


class ReviewFixTests(ControllerTestCase):

    def run_record(self, run_id="run-1"):
        return json.loads((self.results / run_id / "run.json").read_text())

    async def test_runner_restart_marks_its_running_run_lost(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)
        async with connect(
            f"ws://127.0.0.1:{self.port}/ws/live?run_id=run-1", proxy=None
        ) as live, FakeRunner(self.port) as runner:
            json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))  # snapshot
            # The restarted runner no longer has run-1.
            await self.registered(runner, active=None)
            await wait_until(lambda: self.controller.runs["run-1"].status == "lost")
            while True:
                update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
                if update["type"] == "run":
                    break
            self.assertEqual("lost", update["run"]["status"])
            self.assertIn("no active run", update["run"]["reason"])
        record = self.run_record()
        self.assertEqual("lost", record["status"])
        self.assertIn("no longer has this run", record["reason"])
        self.assertIsNotNone(record["finished_at"])
        self.assertTrue(record["report"])
        self.assertEqual(409, (await self.http("POST", "/api/runs/run-1/cancel", {}))[0])

    async def test_reconnect_with_another_run_marks_the_stored_run_lost(self):
        async with FakeRunner(self.port) as first:
            await self.registered(first)
            await self.accepted_run(first)
            # Half-open reconnect: the new connection replaces a live one and
            # names a different active run.
            async with FakeRunner(self.port) as second:
                await second.register(active="run-2")
                await wait_until(lambda: self.controller.runs["run-1"].status == "lost")
                self.assertIn("active run run-2", self.controller.runs["run-1"].reason)
                self.assertTrue(any(
                    line.startswith("warning:") and "still online" in line
                    for line in self.logs
                ), self.logs)
                await second.send("event", run_id="run-2", event=event("run-2", 1))
                await wait_until(lambda: "run-2" in self.controller.runs)
                self.assertEqual("running", self.controller.runs["run-2"].status)
        self.assertEqual("lost", self.run_record()["status"])

    async def test_controller_restart_marks_a_stored_run_lost_on_register(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
        await self.server.stop()
        await self.start_controller()
        # Until the target registers the stored run stays running.
        self.assertEqual("running", self.controller.runs["run-1"].status)
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, active=None)
            await wait_until(lambda: self.controller.runs["run-1"].status == "lost")
        self.assertEqual("lost", self.run_record()["status"])
        # The terminal status survives another restart. (A register that
        # names the run keeps it running; see the deduplication test.)
        await self.server.stop()
        await self.start_controller()
        self.assertEqual("lost", self.controller.runs["run-1"].status)

    async def test_overlapping_cancels_time_out_independently(self):
        async with FakeRunner(self.port) as runner:
            # Long heartbeat interval: the target stays online without them.
            await self.registered(runner, interval=10)
            await self.accepted_run(runner)
            self.controller.reply_timeout_s = 1.5
            first = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            self.assertEqual("cancel", (await runner.recv())["type"])
            await wait_until(lambda: self.controller._pending_cancels["run-1"].waiters == 1)
            second = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            await wait_until(lambda: self.controller._pending_cancels["run-1"].waiters == 2)
            # The runner never replies: each caller gets its own 504.
            status, payload, _ = await first
            self.assertEqual(504, status, payload)
            status, payload, _ = await second
            self.assertEqual(504, status, payload)

            # The same overlap where the runner answers after the first
            # caller gave up: the second still gets the answer.
            third = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            self.assertEqual("cancel", (await runner.recv())["type"])
            await wait_until(lambda: self.controller._pending_cancels["run-1"].waiters == 1)
            await asyncio.sleep(0.5)
            fourth = asyncio.create_task(self.http("POST", "/api/runs/run-1/cancel", {}))
            await wait_until(lambda: self.controller._pending_cancels["run-1"].waiters == 2)
            self.assertEqual(504, (await third)[0])
            await runner.send("cancel_accepted", run_id="run-1")
            status, payload, _ = await fourth
            self.assertEqual(202, status, payload)
            self.assertTrue(payload["run"]["cancel_requested"])
            # Only one cancel message per overlapping pair reached the runner.
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(runner.ws.recv(), 0.2)

    async def test_malformed_stored_lines_are_skipped(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
        with (self.results / "run-1" / "run.jsonl").open("ab") as stream:
            stream.write(b"{corrupt\n\xff\xfe\n[1, 2]\n")
        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(200, status, payload)
        self.assertEqual([event("run-1", 1, "run_started")], payload["events"])
        self.assertEqual(3, payload["run"]["malformed_lines"])
        self.assertTrue(any("3 malformed" in line for line in self.logs), self.logs)
        await self.server.stop()
        await self.start_controller()
        self.assertEqual({1}, self.controller.runs["run-1"].seen)
        self.assertEqual(3, self.controller.runs["run-1"].malformed_lines)

    async def test_timed_out_run_requests_expire_after_the_grace_period(self):
        self.controller.reply_timeout_s = 0.1
        self.controller.late_reply_grace_s = 0.4
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, interval=10)
            # Within the grace window a late acceptance still creates the run.
            post, message = await self.start_run(runner)
            self.assertEqual(504, (await post)[0])
            self.assertIn(message["request_id"], self.controller._pending_runs)
            await runner.send("run_accepted", request_id=message["request_id"], run_id="run-1")
            await wait_until(lambda: "run-1" in self.controller.runs)
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="passed", exit_code=0, run_directory="/",
            )
            await wait_until(
                lambda: self.controller.targets["Host_A"].active_run_id is None
            )
            # After it, the entry is gone and a late acceptance is ignored.
            post, message = await self.start_run(runner)
            self.assertEqual(504, (await post)[0])
            await wait_until(lambda: self.controller._pending_runs == {})
            errors = self.controller.targets["Host_A"].protocol_errors
            await runner.send("run_accepted", request_id=message["request_id"], run_id="run-2")
            await wait_until(
                lambda: self.controller.targets["Host_A"].protocol_errors == errors + 1
            )
            self.assertNotIn("run-2", self.controller.runs)

    async def test_event_after_run_finished_rebuilds_the_report(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            events = [
                event("run-1", 1, "run_started"),
                event("run-1", 2, "test_started", name="Host version"),
            ]
            for record in events:
                await runner.send("event", run_id="run-1", event=record)
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="failed", exit_code=1, run_directory="/",
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "failed")
            report = self.results / "run-1" / "report.md"
            self.assertEqual(render_run(events), report.read_text())
            late = event("run-1", 3, "run_finished", "passed", completed_tests=1,
                         requested_tests=1)
            await runner.send("event", run_id="run-1", event=late)
            await wait_until(lambda: 3 in self.controller.runs["run-1"].seen)
            await wait_until(lambda: report.read_text() == render_run([*events, late]))
        self.assertTrue(self.run_record()["report"])

    def slow_report_writer(self):
        """Hold every report compile 0.2 s in its worker thread.

        Widens the window in which a status set before the report was
        written could be seen (issue #22); with the fix the window is closed
        whatever its width.
        """
        original = self.controller._write_report

        def slow(*args):
            time.sleep(0.2)
            return original(*args)
        self.controller._write_report = slow

    def assert_report_with_status(self, run_id="run-1"):
        run = self.controller.runs[run_id]
        self.assertTrue(run.report)
        self.assertTrue((self.results / run_id / "report.md").is_file())
        self.assertEqual(run.status, self.run_record(run_id)["status"])
        self.assertTrue(self.run_record(run_id)["report"])

    async def test_terminal_status_is_never_seen_before_its_report(self):
        self.slow_report_writer()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
            async with connect(
                f"ws://127.0.0.1:{self.port}/ws/live?run_id=run-1", proxy=None
            ) as live:
                json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))  # snapshot
                await runner.send(
                    "run_finished", request_id=message["request_id"], run_id="run-1",
                    status="failed", exit_code=1, run_directory="/",
                )
                # Polled at once and often: the first poll that sees the
                # terminal status must also find the report.
                await wait_until(
                    lambda: self.controller.runs["run-1"].status == "failed", interval=0
                )
                self.assert_report_with_status()
                status, _, _ = await self.http("GET", "/api/runs/run-1/report.md")
                self.assertEqual(200, status)
                while True:
                    update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
                    if update["type"] == "run":
                        break
                self.assertEqual(("failed", True),
                                 (update["run"]["status"], update["run"]["report"]))

    async def test_lost_status_is_never_seen_before_its_report(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)
        self.slow_report_writer()
        async with FakeRunner(self.port) as runner:
            await runner.register(active=None)
            await wait_until(
                lambda: self.controller.runs["run-1"].status == "lost", interval=0
            )
            self.assert_report_with_status()
            self.assertEqual(200, (await self.http("GET", "/api/runs/run-1/report.md"))[0])

    async def finish_after_one_event(self, runner, message, kind="run_finished"):
        await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
        await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
        if kind == "run_finished":
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="passed", exit_code=0, run_directory="/",
            )
        else:
            await runner.send(
                "run_error", request_id=message["request_id"], run_id="run-1",
                exit_code=2, reason="build failed",
            )

    async def test_run_error_before_any_record_has_a_report(self):
        self.slow_report_writer()
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            await runner.send(
                "run_error", request_id=message["request_id"], run_id="run-1",
                exit_code=2, reason="cannot resolve ref",
            )
            await wait_until(
                lambda: self.controller.runs["run-1"].status == "error", interval=0
            )
            self.assert_report_with_status()
            self.assertFalse((self.results / "run-1" / "run.jsonl").exists())
            status, body, _ = await self.http("GET", "/api/runs/run-1/report.md")
            self.assertEqual(200, status)
            self.assertIn("| Status | error |", body)
            self.assertIn("cannot resolve ref", body)

    async def test_lost_before_any_record_has_a_report(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)
        self.slow_report_writer()
        async with FakeRunner(self.port) as runner:
            await runner.register(active=None)
            await wait_until(
                lambda: self.controller.runs["run-1"].status == "lost", interval=0
            )
            self.assert_report_with_status()
            status, body, _ = await self.http("GET", "/api/runs/run-1/report.md")
            self.assertEqual(200, status)
            self.assertIn("| Status | lost |", body)
            self.assertIn("no longer has this run", body)

    async def test_removed_run_log_still_gives_a_report(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            await runner.send("event", run_id="run-1", event=event("run-1", 1, "run_started"))
            await wait_until(lambda: self.controller.runs["run-1"].seen == {1})
            (self.results / "run-1" / "run.jsonl").unlink()
            await runner.send(
                "run_error", request_id=message["request_id"], run_id="run-1",
                exit_code=2, reason="build failed",
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "error")
        self.assert_report_with_status()
        record = self.run_record()
        self.assertIn("could not compile report.md", record["report_error"])
        body = (self.results / "run-1" / "report.md").read_text()
        self.assertIn("build failed", body)
        self.assertIn("Report problem", body)

    async def raising_report_compile_ends_the_run(self, kind):
        def broken(output_dir):
            raise OSError(28, "No space left on device")
        with patch("ubertooth_harness.controller.write_report", broken):
            async with FakeRunner(self.port) as runner:
                await self.registered(runner)
                message = await self.accepted_run(runner)
                await self.finish_after_one_event(runner, message, kind)
                await wait_until(lambda: not self.controller.runs["run-1"].active)
                self.assertIsNone(self.controller.targets["Host_A"].active_run_id)
        self.assert_report_with_status()
        self.assertIn("No space left on device", self.run_record()["report_error"])

    async def test_raising_report_compile_still_finishes_the_run(self):
        await self.raising_report_compile_ends_the_run("run_finished")

    async def test_raising_report_compile_still_records_the_run_error(self):
        await self.raising_report_compile_ends_the_run("run_error")

    async def test_failed_report_write_still_marks_the_run_lost(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
        await wait_until(lambda: not self.controller.targets["Host_A"].connected)

        def broken(*args):
            raise OSError(28, "No space left on device")
        self.controller._write_report = broken
        async with FakeRunner(self.port) as runner:
            await runner.register(active=None)
            await wait_until(lambda: self.controller.runs["run-1"].status == "lost")
        record = self.run_record()
        self.assertEqual(("lost", False), (record["status"], record["report"]))
        self.assertIn("No space left on device", record["report_error"])

    async def test_failed_report_write_is_recorded_and_the_status_published(self):
        def broken(*args):
            raise OSError(28, "No space left on device")
        self.controller._write_report = broken
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            async with connect(
                f"ws://127.0.0.1:{self.port}/ws/live?run_id=run-1", proxy=None
            ) as live:
                json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))  # snapshot
                await self.finish_after_one_event(runner, message)
                while True:
                    update = json.loads(await asyncio.wait_for(live.recv(), TIMEOUT_S))
                    if update["type"] == "run":
                        break
        self.assertEqual("passed", update["run"]["status"])
        self.assertFalse(update["run"]["report"])
        self.assertIn("No space left on device", update["run"]["report_error"])
        record = self.run_record()
        self.assertEqual(("passed", False), (record["status"], record["report"]))
        self.assertIn("No space left on device", record["report_error"])
        self.assertTrue(any("No space left on device" in line for line in self.logs))


class MalformedRunnerTests(ControllerTestCase):

    async def _closed_with(self, first):
        async with FakeRunner(self.port) as runner:
            await runner.ws.send(first)
            with self.assertRaises(ConnectionClosed) as raised:
                await asyncio.wait_for(runner.ws.recv(), TIMEOUT_S)
        return raised.exception.rcvd.code

    async def test_first_message_must_be_a_valid_register(self):
        heartbeat = encode_runner_message(
            "heartbeat", active_run_id=None, elapsed_s=None,
            last_event_sequence=None, last_event_type=None,
        )
        for first in ("{bad json", heartbeat, b"\x00binary",
                      json.dumps({"version": 2, "type": "register"})):
            with self.subTest(first=first):
                self.assertEqual(1008, await self._closed_with(first))
        self.assertEqual({}, self.controller.targets)

    async def test_malformed_messages_after_register_are_ignored(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            good = json.loads(encode_runner_message("protocol_error", reason="x"))
            bad = [
                "{not json",
                json.dumps({**good, "version": 99}),
                json.dumps({**good, "type": "deploy"}),
                json.dumps({**good, "extra": 1}),
                json.dumps({"version": 1, "type": "event", "run_id": "x", "event": {}}),
                b"binary",
                encode_runner_message("run_accepted", request_id="nope", run_id="run-x"),
                encode_runner_message("event", run_id="run-x", event=event("run-x", 1)),
                encode_runner_message("event", run_id="run-x", event=event("run-y", 1)),
                encode_runner_message(
                    "run_finished", request_id="r", run_id="run-x", status="passed",
                    exit_code=0, run_directory="/",
                ),
                encode_runner_message("cancel_accepted", run_id="run-x"),
                encode_runner_message(
                    "register", host="Host_B", hostname="h", os={}, python="3",
                    ubertooth_serials=None, enumeration_error=None,
                    heartbeat_interval_s=1, active_run_id=None,
                ),
            ]
            for text in bad:
                await runner.ws.send(text)
            await wait_until(
                lambda: self.controller.targets["Host_A"].protocol_errors == len(bad)
            )
            self.assertEqual({}, self.controller.runs)
            self.assertNotIn("Host_B", self.controller.targets)
            self.assertEqual([], list(self.results.iterdir()))
            # Still connected and usable.
            await self.accepted_run(runner)
        status, payload, _ = await self.http("GET", "/api/targets/Host_A")
        self.assertEqual(len(bad), payload["target"]["protocol_errors"])
        self.assertTrue(payload["target"]["last_protocol_error"])


class EndToEndTests(ControllerTestCase):
    """The real target runner against the real controller, no hardware."""

    def setUp(self):
        # Synchronous git and file setup stays out of the event loop.
        self._fixture_directory = tempfile.TemporaryDirectory()
        self.fixture = RunnerFixture(Path(self._fixture_directory.name))

    def tearDown(self):
        self._fixture_directory.cleanup()

    async def test_real_runner_run_through_the_controller(self):
        service = RunnerService(
            self.fixture.config(f"ws://127.0.0.1:{self.port}/ws/runner"),
            enumerator=StubEnumerator(), log=self.logs.append,
        )
        task = asyncio.create_task(service.run_forever())
        try:
            await wait_until(lambda: "Host_Local" in self.controller.targets)
            status, payload, _ = await self.http("GET", "/api/targets/Host_Local")
            self.assertEqual("online", payload["target"]["status"])
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_Local/runs",
                {"ref": "HEAD", "plan": "local", "suite": "fast"},
            )
            self.assertEqual(201, status, payload)
            run_id = payload["run"]["run_id"]
            await wait_until(lambda: self.controller.runs[run_id].status != "running", 60)
            status, payload, _ = await self.http("GET", f"/api/runs/{run_id}")
            self.assertEqual("passed", payload["run"]["status"], payload["run"])
            self.assertEqual(0, payload["run"]["exit_code"])
            runner_dir = self.fixture.output.resolve() / run_id
            on_runner = [
                json.loads(line)
                for line in (runner_dir / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual(on_runner, payload["events"])
            self.assertEqual("run_finished", payload["events"][-1]["event_type"])
            status, text, _ = await self.http("GET", f"/api/runs/{run_id}/report.md")
            self.assertEqual(200, status)
            self.assertEqual((runner_dir / "report.md").read_text(), text)
            self.assertIsNone(self.controller.targets["Host_Local"].active_run_id)
        finally:
            service.request_stop()
            await asyncio.wait_for(task, TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
