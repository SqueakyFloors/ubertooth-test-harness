"""The per-host power/firmware lock (leases.HOST_LOCK, issue #4)."""

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ubertooth_harness import cli, leases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.flash import HarnessError
from ubertooth_harness.topology import PowerIsolationAction
from tests.test_leases import A, B, HARDWARE, PLATFORM, holder

# A child process that holds the host lock (and any boards given) until killed.
_HOLDER = textwrap.dedent("""\
    import sys, time
    from ubertooth_harness import leases
    keys = sys.argv[3:]
    leases.acquire(
        [leases.Board("UT_%d" % i, key) for i, key in enumerate(keys)],
        leases.holder_info(run_id="run-held", owner=sys.argv[2],
                           purpose="run-suite flash plan / slow"),
        directory=sys.argv[1], host_lock=True,
    )
    print("held", flush=True)
    time.sleep(60)
    """)


def _host_lock_held(directory):
    return leases.host_lock_state(directory=directory)["held"]


class HostLockTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
        self._dir.cleanup()

    def _hold_in_child(self, owner, *keys):
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(self.dir), owner, *keys],
            stdout=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        self.assertEqual("held", child.stdout.readline().strip())
        return child

    def test_a_second_process_fails_fast_and_the_error_names_the_holder(self):
        child = self._hold_in_child("flasher")
        started = time.monotonic()
        with self.assertRaises(leases.LeaseError) as caught:
            leases.acquire([B], holder("agent-2"), directory=self.dir, host_lock=True)
        # Fails after the short probe grace, never waits for the holder.
        self.assertLess(time.monotonic() - started, 5)
        message = str(caught.exception)
        self.assertIn("power/firmware lock is in use", message)
        self.assertIn("flasher", message)
        self.assertIn(f"pid {child.pid}", message)
        self.assertIn("run-suite flash plan / slow", message)
        self.assertIn("host-power.lock", message)
        # The failed attempt took no board.
        self.assertFalse(leases.board_states([B], directory=self.dir)[0]["held"])

    def test_kill_9_leaves_no_stale_host_lock(self):
        child = self._hold_in_child("doomed", A.key)
        state = leases.host_lock_state(directory=self.dir)
        self.assertTrue(state["held"])
        self.assertEqual("doomed", state["holder"]["owner"])
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
        # The lock file still records the dead holder, but the kernel dropped
        # the flock, so the lock is free.
        self.assertEqual("doomed", leases.read_holder(self.dir / "host-power.lock")["owner"])
        self.assertFalse(_host_lock_held(self.dir))
        leases.acquire([A], holder("agent-2"), directory=self.dir, host_lock=True).release()

    def test_release_frees_the_host_lock_and_clears_its_record(self):
        held = leases.acquire([], holder("agent-1"), directory=self.dir, host_lock=True)
        self.assertEqual([leases.HOST_LOCK], [lease.board for lease in held.leases])
        self.assertTrue(_host_lock_held(self.dir))
        held.release()
        self.assertFalse(_host_lock_held(self.dir))
        self.assertEqual({}, leases.read_holder(self.dir / "host-power.lock"))

    def test_context_manager_releases_on_error_and_interrupt(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):
            with self.subTest(type(error).__name__), self.assertRaises(type(error)):
                with leases.acquire([A], holder("agent-1"), directory=self.dir, host_lock=True):
                    raise error
            self.assertFalse(_host_lock_held(self.dir))

    def test_the_host_lock_is_taken_before_any_board(self):
        order = []
        real = leases._try_lock

        def recording(path, mode):
            order.append(Path(path).name)
            return real(path, mode)

        with mock.patch.object(leases, "_try_lock", recording):
            leases.acquire([B, A], holder("agent-1"), directory=self.dir, host_lock=True).release()
        self.assertEqual(["host-power.lock", f"{A.key}.lock", f"{B.key}.lock"], order)

    def test_a_busy_board_releases_the_host_lock_it_took(self):
        # A manual `lease` holds board A without the host lock; a run that
        # needs the host lock and A must not keep the host lock while A is busy.
        self._hold_in_child_board_only(A.key)
        with self.assertRaises(leases.LeaseError) as caught:
            leases.acquire([A], holder("agent-2"), directory=self.dir, host_lock=True)
        self.assertIn("UT_0 is in use", str(caught.exception))
        self.assertFalse(_host_lock_held(self.dir))
        leases.acquire([B], holder("agent-3"), directory=self.dir, host_lock=True).release()

    def _hold_in_child_board_only(self, key):
        code = (
            "import sys, time\n"
            "from ubertooth_harness import leases\n"
            f"leases.acquire([leases.Board('UT_0', '{key}')], "
            "leases.holder_info(run_id=None, owner='manual', purpose='lease: x'), "
            "directory=sys.argv[1])\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.dir)], stdout=subprocess.PIPE, text=True,
        )
        self.children.append(child)
        self.assertEqual("held", child.stdout.readline().strip())

    def test_contenders_with_and_without_the_host_lock_never_deadlock(self):
        # flock is per open file, so threads contend exactly as processes do.
        errors = []

        def contend(boards, host_lock):
            try:
                for _ in range(15):
                    with leases.acquire(
                        boards, holder("agent"), directory=self.dir,
                        wait_s=30, host_lock=host_lock,
                    ):
                        time.sleep(0.005)
            except Exception as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [
            threading.Thread(target=contend, args=([A, B], True)),
            threading.Thread(target=contend, args=([B, A], True)),
            threading.Thread(target=contend, args=([B], False)),
            threading.Thread(target=contend, args=([A], False)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse([t for t in threads if t.is_alive()], "deadlocked")
        self.assertEqual([], errors)
        self.assertFalse(_host_lock_held(self.dir))

    def test_runs_that_switch_power_or_write_firmware_need_the_host_lock(self):
        catalog = builtin_catalog()

        def invocation(name="Radio setting transitions", **fields):
            values = dict(test_name=name, power_isolation=(), exclusive_ubertooth=False)
            values.update(fields)
            return SimpleNamespace(**values)

        self.assertFalse(leases.needs_host_lock([invocation()], catalog))
        for label, item in {
            "power isolation": invocation(
                power_isolation=(PowerIsolationAction("UT_1", "Hub", 1),)),
            "exclusive USB scan": invocation(exclusive_ubertooth=True),
            "flash": invocation("Flash reliability"),
            "DFU write": invocation("DFU images"),
        }.items():
            with self.subTest(label):
                self.assertTrue(leases.needs_host_lock([invocation(), item], catalog))

    def test_every_test_that_establishes_firmware_writes_it(self):
        catalog = builtin_catalog()
        establishing = [n for n in catalog.names if catalog.get(n).establishes_firmware]
        self.assertIn("Flash reliability", establishing)
        self.assertIn("DFU images", establishing)
        for name in establishing:
            with self.subTest(name):
                self.assertTrue(catalog.get(name).writes_firmware)


class HostLockCliTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.lease_dir = self.root / "leases"
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(HARDWARE)
        self.output = self.root / "runs"

    def tearDown(self):
        self._dir.cleanup()

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _flash_run_args(self, run_id="r1"):
        from tests.test_cli import make_source

        source, _short = make_source(self.root)
        kgv = self.root / "kgv.dfu"
        kgv.write_bytes(b"kgv")
        test = self.root / "test.dfu"
        test.write_bytes(b"test")
        plan = self.root / "plan.yaml"
        plan.write_text(textwrap.dedent(f"""\
            plan: host lock cli
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
        return [
            "run-suite", "--plan", str(plan), "--suite", "slow",
            "--hardware", str(self.hardware), "--host", "Host_Local",
            "--ubertooth-source", str(source), "--ubertooth-ref", "HEAD",
            "--artifact", f"test build={test}", "--artifact", f"KGV={kgv}",
            "--run-id", run_id, "--output", str(self.output),
            "--lease-dir", str(self.lease_dir), "--owner", "flash-agent",
        ]

    def test_run_suite_refuses_while_another_command_holds_the_host_lock(self):
        args = self._flash_run_args("blocked")
        with leases.acquire([], holder("isolating-agent"), directory=self.lease_dir,
                            host_lock=True):
            code, _out, err = self._main(*args)
        self.assertEqual(2, code)
        self.assertIn("power/firmware lock is in use", err)
        self.assertIn("isolating-agent", err)
        self.assertIn(f"pid {os.getpid()}", err)
        self.assertFalse((self.output / "blocked").exists())
        # Its boards were not kept either.
        self.assertFalse(leases.board_states([A], directory=self.lease_dir)[0]["held"])

    def _run_with(self, behaviour):
        seen = {}

        def fake_run_leased(*args, **_kwargs):
            state = leases.host_lock_state(directory=self.lease_dir)
            seen["held"] = state["held"]
            seen["owner"] = state["holder"].get("owner")
            seen["manifest"] = args[8].to_manifest()
            return behaviour()

        with mock.patch.object(cli, "_run_leased", fake_run_leased):
            result = self._main(*self._flash_run_args())
        return seen, result

    def test_run_suite_holds_the_host_lock_and_releases_it_on_success(self):
        seen, (code, _out, _err) = self._run_with(lambda: 0)
        self.assertEqual(0, code)
        self.assertTrue(seen["held"])
        self.assertEqual("flash-agent", seen["owner"])
        self.assertIn("host lock", [item["resource"] for item in seen["manifest"]])
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])

    def test_run_suite_releases_the_host_lock_on_failure(self):
        def fail():
            raise HarnessError("power isolation failed")

        seen, (code, _out, err) = self._run_with(fail)
        self.assertEqual(2, code)
        self.assertIn("power isolation failed", err)
        self.assertTrue(seen["held"])
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])

    def test_run_suite_releases_the_host_lock_on_interrupt(self):
        def interrupt():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self._run_with(interrupt)
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])
        self.assertEqual({}, leases.read_holder(self.lease_dir / "host-power.lock"))

    def test_sigint_to_a_running_run_suite_releases_the_host_lock(self):
        # The child stays alive after the cancel, so a free lock proves the
        # harness released it, not that the kernel did at exit.
        code = textwrap.dedent("""\
            import json, sys, time
            from unittest import mock
            from ubertooth_harness import cli

            def run_leased(*_args, **_kwargs):
                print("held", flush=True)
                time.sleep(60)

            with mock.patch.object(cli, "_run_leased", run_leased):
                try:
                    cli.main(json.loads(sys.argv[1]))
                except KeyboardInterrupt:
                    print("cancelled", flush=True)
            time.sleep(60)
            """)
        child = subprocess.Popen(
            [sys.executable, "-c", code, json.dumps(self._flash_run_args())],
            stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1],
        )
        try:
            self.assertEqual("held", child.stdout.readline().strip())
            self.assertTrue(leases.host_lock_state(directory=self.lease_dir)["held"])
            child.send_signal(signal.SIGINT)
            self.assertEqual("cancelled", child.stdout.readline().strip())
            self.assertIsNone(child.poll())
            self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])
        finally:
            child.kill()
            child.wait()
            child.stdout.close()

    def test_boards_shows_the_host_lock_holder(self):
        with leases.acquire([], holder("agent-9"), directory=self.lease_dir, host_lock=True):
            code, out, _err = self._main(
                "boards", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            )
        self.assertEqual(0, code)
        line = out.splitlines()[-1]
        self.assertRegex(line, r"^host lock +held  agent-9, run run-agent-9, test, ")
        self.assertIn(f"pid {os.getpid()}", line)

    def _probe(self):
        return (
            "import json, sys; from ubertooth_harness import leases; "
            "s = leases.host_lock_state(directory=sys.argv[1]); "
            "print(json.dumps(s['holder'].get('owner')) if s['held'] else 'free')"
        )

    def _lease(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "ubertooth_harness", "lease",
             "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
             "--owner", "manual", *argv],
            capture_output=True, text=True, timeout=60,
            cwd=Path(__file__).resolve().parents[1],
        )

    def test_lease_takes_the_host_lock_only_when_asked_or_for_ubertooth_dfu(self):
        plain = self._lease("UT_0", "--", sys.executable, "-c", self._probe(), str(self.lease_dir))
        self.assertEqual(0, plain.returncode, plain.stderr)
        self.assertEqual("free", plain.stdout.strip())

        asked = self._lease("--host-lock", "UT_0", "--",
                            sys.executable, "-c", self._probe(), str(self.lease_dir))
        self.assertEqual(0, asked.returncode, asked.stderr)
        self.assertEqual('"manual"', asked.stdout.strip())

        dfu = self.root / "bin" / "ubertooth-dfu"
        dfu.parent.mkdir()
        dfu.write_text(f"#!/bin/sh\nexec '{sys.executable}' -c \"$1\" \"$2\"\n")
        dfu.chmod(0o755)
        auto = self._lease("UT_0", "--", str(dfu), self._probe(), str(self.lease_dir))
        self.assertEqual(0, auto.returncode, auto.stderr)
        self.assertEqual('"manual"', auto.stdout.strip())
        self.assertFalse(leases.host_lock_state(directory=self.lease_dir)["held"])


if __name__ == "__main__":
    unittest.main()
