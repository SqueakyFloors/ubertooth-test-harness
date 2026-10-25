"""Target runner against an in-process stub controller on 127.0.0.1.

Runs execute the real run-suite path in a child process with no hardware: the
plan selects only a Host, and a fake ``ubertooth-util`` on PATH answers the
host-version checks.
"""

import asyncio
import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from websockets.asyncio.server import serve

from ubertooth_harness.cli import main
from ubertooth_harness.protocol import (
    PROTOCOL_VERSION,
    ProtocolError,
    decode_controller_message,
    decode_runner_message,
    encode_controller_message,
    encode_runner_message,
)
from ubertooth_harness.runner_service import (
    RunnerConfig,
    RunnerConfigError,
    RunnerService,
    validate_controller_url,
)

TIMEOUT_S = 60


class StubEnumerator:
    def __init__(self, serials=("0000000000000000000000000000abcd",)):
        self._serials = frozenset(serials)
        self.calls = 0

    def serials(self):
        self.calls += 1
        return self._serials


class StubController:
    """Loopback WebSocket controller that records every runner message."""

    def __init__(self, *, drop_first_after_register=False):
        self.messages: asyncio.Queue = asyncio.Queue()
        self.connections = []
        self.drop_first_after_register = drop_first_after_register
        self.server = None

    async def __aenter__(self):
        self.server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/runner"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, connection):
        self.connections.append(connection)
        try:
            async for text in connection:
                message = decode_runner_message(text)
                await self.messages.put(message)
                if (
                    self.drop_first_after_register
                    and len(self.connections) == 1
                    and message["type"] == "register"
                ):
                    await connection.close()
                    return
        except Exception:
            pass

    async def send(self, text):
        await self.connections[-1].send(text)

    async def next(self, message_type=None, *, skip=("heartbeat",)):
        while True:
            message = await asyncio.wait_for(self.messages.get(), TIMEOUT_S)
            if message_type is None and message["type"] not in skip:
                return message
            if message["type"] == message_type:
                return message
            if message["type"] not in skip and message_type is not None:
                raise AssertionError(f"expected {message_type}, got {message}")


def make_source(root: Path):
    source = root / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("source\n")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test",
         "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        check=True,
    )
    short = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return source, short


class RunnerFixture:
    """Temporary source, plan, hardware registry, and fake host tool."""

    def __init__(self, root: Path):
        self.root = root
        self.source, short = make_source(root)
        platform = "macos" if sys.platform == "darwin" else "linux"
        self.plans = root / "plans"
        self.plans.mkdir()
        (self.plans / "local.yaml").write_text(
            "plan: local\nsuites:\n  fast:\n    - Host version:\n        hosts: each\n"
        )
        self.hardware = root / "hardware.yaml"
        self.hardware.write_text(
            f"resources:\n  Host_Local:\n    type: Host\n    platform: {platform}\n"
            "    roles: [test target]\n"
        )
        self.bin = root / "bin"
        self.bin.mkdir()
        tool = self.bin / "ubertooth-util"
        # FAKE_SLEEP keeps the command running so a cancel can arrive.
        tool.write_text(
            "#!/bin/sh\n"
            'if [ -n "$FAKE_SLEEP" ]; then sleep "$FAKE_SLEEP"; fi\n'
            f'echo "libubertooth 1.1 (git-{short}), libbtbb 1.0 (git-btbb)"\n'
        )
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.output = root / "runs"

    def config(self, url, **overrides):
        import os

        environment = {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        environment.update(overrides.pop("environment", {}))
        values = dict(
            controller_url=url,
            hardware=self.hardware,
            host="Host_Local",
            ubertooth_source=self.source,
            output=self.output,
            plans_dir=self.plans,
            heartbeat_interval_s=0.2,
            initial_backoff_s=0.05,
            max_backoff_s=0.2,
            child_environment=environment,
        )
        values.update(overrides)
        return RunnerConfig(**values)


def run_request(request_id="r1", **overrides):
    fields = dict(
        request_id=request_id, ref="HEAD", commit=None, plan="local",
        suite="fast", tests=[], build=False,
    )
    fields.update(overrides)
    return encode_controller_message("run", **fields)


class ProtocolTests(unittest.TestCase):

    def test_valid_messages_round_trip(self):
        message = decode_controller_message(run_request())
        self.assertEqual("run", message["type"])
        self.assertEqual(PROTOCOL_VERSION, message["version"])
        cancel = decode_controller_message(
            encode_controller_message("cancel", run_id="abc", reason="")
        )
        self.assertEqual("abc", cancel["run_id"])

    def test_strict_rejection(self):
        good = json.loads(run_request())
        cases = {
            "not json": "not json",
            "array": "[]",
            "unknown version": json.dumps({**good, "version": 99}),
            "previous version": json.dumps({**good, "version": 1}),
            "string version": json.dumps({**good, "version": "1"}),
            "missing version": json.dumps({k: v for k, v in good.items() if k != "version"}),
            "unknown type": json.dumps({**good, "type": "deploy"}),
            "runner type": json.dumps(
                {"version": PROTOCOL_VERSION, "type": "protocol_error", "reason": "x"}
            ),
            "missing commit": json.dumps({k: v for k, v in good.items() if k != "commit"}),
            "short commit": json.dumps({**good, "commit": "0123456789ab"}),
            "upper-case commit": json.dumps({**good, "commit": "A" * 40}),
            "commit ref name": json.dumps({**good, "commit": "main"}),
            "numeric commit": json.dumps({**good, "commit": 1}),
            "extra field": json.dumps({**good, "priority": 1}),
            "missing field": json.dumps({k: v for k, v in good.items() if k != "suite"}),
            "option ref": json.dumps({**good, "ref": "--upload-pack=x"}),
            "spaced ref": json.dumps({**good, "ref": "a b"}),
            "plan path": json.dumps({**good, "plan": "../etc/passwd"}),
            "bool tests": json.dumps({**good, "tests": True}),
            "string build": json.dumps({**good, "build": "yes"}),
            "nan": run_request().replace('"build": false', '"build": NaN'),
            "large": json.dumps({**good, "ref": "a" * 70000}),
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ProtocolError):
                decode_controller_message(text)
        with self.assertRaises(ProtocolError):
            decode_controller_message(b"{}")

    def test_encoder_applies_the_schema(self):
        with self.assertRaises(ProtocolError):
            encode_runner_message("protocol_error", reason="x", extra=1)
        with self.assertRaises(ProtocolError):
            encode_runner_message("heartbeat", active_run_id=None)


class ControllerUrlTests(unittest.TestCase):

    def test_loopback_urls_are_accepted(self):
        self.assertEqual("127.0.0.1", validate_controller_url("ws://127.0.0.1:9/x").address)
        self.assertEqual("::1", validate_controller_url("ws://[::1]:9/").address)
        self.assertEqual(443, validate_controller_url("wss://127.0.0.1/").port)
        resolved = validate_controller_url(
            "ws://localhost:9/",
            resolve=lambda *a, **k: [(None, None, None, "", ("127.0.0.1", 9))],
        )
        self.assertEqual("127.0.0.1", resolved.address)

    def test_non_loopback_urls_are_refused(self):
        for url in (
            "ws://10.0.0.1:9/", "ws://192.168.1.2/", "ws://0.0.0.0:9/",
            "ws://[::]:9/", "ws://example.com/", "ws://localhost.example.com/",
            "ws://127.0.0.1.nip.io/", "ws://[::ffff:10.0.0.1]/",
            "http://127.0.0.1/", "ws://user:secret@127.0.0.1/", "ws:///x",
            "ws://127.0.0.1:99999/",
        ):
            with self.subTest(url), self.assertRaises(RunnerConfigError):
                validate_controller_url(url)

    def test_localhost_resolving_elsewhere_is_refused(self):
        with self.assertRaisesRegex(RunnerConfigError, "non-loopback"):
            validate_controller_url(
                "ws://localhost/",
                resolve=lambda *a, **k: [
                    (None, None, None, "", ("127.0.0.1", 80)),
                    (None, None, None, "", ("10.1.1.1", 80)),
                ],
            )

    def test_cli_refuses_remote_controller_before_connecting(self):
        stderr = io.StringIO()
        with (
            patch("socket.socket.connect", side_effect=AssertionError("connected")),
            contextlib.redirect_stderr(stderr),
        ):
            result = main([
                "runner", "--controller", "ws://203.0.113.5:8765/runner",
                "--hardware", "hardware.yaml", "--host", "Host_Local",
                "--ubertooth-source", ".", "--output", "runs",
            ])
        self.assertEqual(2, result)
        self.assertIn("is not loopback", stderr.getvalue())


class RunnerServiceTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Synchronous git and file setup stays out of the (debug-mode) event loop.
        self._directory = tempfile.TemporaryDirectory()
        self.fixture = RunnerFixture(Path(self._directory.name))
        self.logs = []

    def tearDown(self):
        self._directory.cleanup()

    @contextlib.asynccontextmanager
    async def running(self, controller, **overrides):
        enumerator = overrides.pop("enumerator", StubEnumerator())
        service = RunnerService(
            self.fixture.config(controller.url, **overrides),
            enumerator=enumerator, log=self.logs.append,
        )
        task = asyncio.create_task(service.run_forever())
        try:
            yield service
        finally:
            service.request_stop()
            await asyncio.wait_for(task, TIMEOUT_S)

    async def test_register_and_heartbeat(self):
        enumerator = StubEnumerator()
        async with StubController() as controller, self.running(
            controller, enumerator=enumerator
        ):
            register = await controller.next("register")
            self.assertEqual("Host_Local", register["host"])
            self.assertEqual(["0000000000000000000000000000abcd"], register["ubertooth_serials"])
            self.assertIsNone(register["enumeration_error"])
            self.assertIsNone(register["active_run_id"])
            self.assertIn("system", register["os"])
            self.assertEqual(1, enumerator.calls)
            for _ in range(2):
                beat = await controller.next("heartbeat", skip=())
                self.assertIsNone(beat["active_run_id"])

    async def test_enumeration_failure_is_reported_not_fatal(self):
        class Broken:
            def serials(self):
                raise OSError("no sysfs")

        async with StubController() as controller, self.running(
            controller, enumerator=Broken()
        ):
            register = await controller.next("register")
            self.assertIsNone(register["ubertooth_serials"])
            self.assertIn("no sysfs", register["enumeration_error"])

    async def test_run_streams_run_jsonl_unchanged_and_in_order(self):
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            await controller.send(run_request())
            accepted = await controller.next("run_accepted")
            run_id = accepted["run_id"]
            events = []
            while True:
                message = await controller.next()
                if message["type"] != "event":
                    break
                self.assertEqual(run_id, message["run_id"])
                events.append(message["event"])
            self.assertEqual("run_finished", message["type"], message)
            self.assertEqual("passed", message["status"])
            self.assertEqual(0, message["exit_code"])
            run_dir = self.fixture.output.resolve() / run_id
            self.assertEqual(str(run_dir), message["run_directory"])
            on_disk = [
                json.loads(line)
                for line in (run_dir / "run.jsonl").read_text().splitlines()
            ]
            self.assertEqual(on_disk, events)
            self.assertEqual(list(range(1, len(events) + 1)), [e["sequence"] for e in events])
            self.assertEqual("run_started", events[0]["event_type"])
            self.assertEqual("run_finished", events[-1]["event_type"])
            self.assertTrue((run_dir / "report.md").is_file())

    async def test_busy_rejection_and_cancel(self):
        async with StubController() as controller, self.running(
            controller, environment={"FAKE_SLEEP": "30"}
        ):
            await controller.next("register")
            await controller.send(run_request("first"))
            run_id = (await controller.next("run_accepted"))["run_id"]
            await controller.send(run_request("second"))
            rejected = await controller.next("run_rejected", skip=("heartbeat", "event"))
            self.assertEqual("second", rejected["request_id"])
            self.assertIn("busy", rejected["reason"])
            # Wait until the slow preflight command is running, then cancel.
            while True:
                message = await controller.next("event")
                if message["event"]["event_type"] == "command_started":
                    break
            await controller.send(
                encode_controller_message("cancel", run_id="other", reason="")
            )
            self.assertEqual(
                "other",
                (await controller.next("cancel_rejected", skip=("heartbeat", "event")))["run_id"],
            )
            await controller.send(
                encode_controller_message("cancel", run_id=run_id, reason="operator")
            )
            await controller.next("cancel_accepted", skip=("heartbeat", "event"))
            finished = await controller.next("run_finished", skip=("heartbeat", "event"))
            self.assertEqual(run_id, finished["run_id"])
            self.assertEqual("canceled", finished["status"])
            # Idle again: a new run is accepted.
            await controller.send(run_request("third", tests=["Host version"]))
            accepted = await controller.next("run_accepted", skip=("heartbeat",))
            self.assertEqual("third", accepted["request_id"])
            # Stopping the runner cancels the active run (see running()).

    async def test_the_sent_commit_is_run_not_the_ref_name(self):
        head = subprocess.run(
            ["git", "-C", str(self.fixture.source), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            # The name does not resolve in this checkout; the commit does.
            await controller.send(run_request(ref="listed-elsewhere", commit=head))
            run_id = (await controller.next("run_accepted"))["run_id"]
            finished = await controller.next("run_finished", skip=("heartbeat", "event"))
            self.assertEqual("passed", finished["status"], finished)
            run_dir = self.fixture.output.resolve() / run_id
            started = json.loads((run_dir / "run.jsonl").read_text().splitlines()[0])
            source = started["details"]["sources"][0]
            self.assertEqual((head, head), (source["requested_ref"], source["commit"]))
        self.assertTrue(any(f"listed-elsewhere = {head}" in line for line in self.logs))

    async def test_a_commit_missing_from_the_checkout_is_a_run_error(self):
        missing = "f" * 40
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            # HEAD resolves here, but the controller listed another commit.
            await controller.send(run_request(commit=missing))
            await controller.next("run_accepted")
            error = await controller.next("run_error", skip=("heartbeat", "event"))
            self.assertIn(missing, error["reason"])

    async def test_run_refused_before_its_directory_is_a_run_error(self):
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            await controller.send(run_request(ref="no-such-ref"))
            run_id = (await controller.next("run_accepted"))["run_id"]
            error = await controller.next("run_error", skip=("heartbeat", "event"))
            self.assertEqual(run_id, error["run_id"])
            self.assertEqual(2, error["exit_code"])
            self.assertIn("no-such-ref", error["reason"])

    async def test_unknown_plan_is_rejected(self):
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            await controller.send(run_request(plan="missing"))
            rejected = await controller.next("run_rejected")
            self.assertIn("unknown plan", rejected["reason"])

    async def test_malformed_messages_are_rejected_and_not_acted_on(self):
        good = json.loads(run_request())
        async with StubController() as controller, self.running(controller):
            await controller.next("register")
            for text in (
                "{not json",
                json.dumps({**good, "version": 99}),
                json.dumps({**good, "type": "flash"}),
                json.dumps({**good, "extra": True}),
            ):
                await controller.send(text)
                error = await controller.next("protocol_error")
                self.assertTrue(error["reason"])
            # Still connected and idle: a valid request is accepted.
            await controller.send(run_request())
            await controller.next("run_accepted")
            await controller.next("run_finished", skip=("heartbeat", "event"))
        self.assertFalse(
            [p for p in self.fixture.output.iterdir()][1:],
            "only the one valid request may create a run",
        )

    async def test_redirects_are_not_followed(self):
        from http import HTTPStatus

        def redirect(connection, request):
            response = connection.respond(HTTPStatus.FOUND, "")
            response.headers["Location"] = "ws://192.0.2.1:9/elsewhere"
            return response

        server = await serve(lambda c: None, "127.0.0.1", 0, process_request=redirect)
        port = server.sockets[0].getsockname()[1]
        try:
            service = RunnerService(
                self.fixture.config(f"ws://127.0.0.1:{port}/"),
                enumerator=StubEnumerator(), log=self.logs.append,
            )
            with patch(
                "socket.getaddrinfo", side_effect=AssertionError("resolved a redirect")
            ):
                task = asyncio.create_task(service.run_forever())
                await asyncio.sleep(0.5)
                service.request_stop()
                await asyncio.wait_for(task, TIMEOUT_S)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(0, service.connections)
        self.assertTrue(any("cannot follow redirect" in line for line in self.logs), self.logs)

    async def test_reconnects_and_reregisters(self):
        async with StubController(drop_first_after_register=True) as controller, \
                self.running(controller) as service:
            await controller.next("register")
            await controller.next("register")
            self.assertGreaterEqual(service.connections, 2)
            self.assertTrue(any("reconnecting" in line for line in self.logs))

    async def test_reconnect_with_backoff_when_controller_is_absent(self):
        async with StubController() as controller:
            url = controller.url
        # Nothing listens now; the runner retries with bounded backoff.
        with contextlib.ExitStack():
            service = RunnerService(
                self.fixture.config(url), enumerator=StubEnumerator(),
                log=self.logs.append,
            )
            task = asyncio.create_task(service.run_forever())
            await asyncio.sleep(0.8)
            service.request_stop()
            await asyncio.wait_for(task, TIMEOUT_S)
        delays = [line for line in self.logs if line.startswith("reconnecting in")]
        self.assertGreaterEqual(len(delays), 3)
        self.assertIn("reconnecting in 0.05s", delays[0])
        self.assertTrue(all(float(d.split()[-1][:-1]) <= 0.2 for d in delays))

    async def test_events_are_replayed_after_a_reconnect_during_a_run(self):
        async with StubController() as controller, self.running(
            controller, environment={"FAKE_SLEEP": "2"}
        ):
            await controller.next("register")
            await controller.send(run_request())
            run_id = (await controller.next("run_accepted"))["run_id"]
            first = await controller.next("event")
            await controller.connections[-1].close()
            register = await controller.next("register", skip=("heartbeat", "event"))
            self.assertEqual(run_id, register["active_run_id"])
            replayed = await controller.next("event")
            self.assertEqual(first["event"], replayed["event"])
            finished = await controller.next("run_finished", skip=("heartbeat", "event"))
            self.assertEqual("passed", finished["status"])


if __name__ == "__main__":
    unittest.main()
