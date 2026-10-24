import hashlib
import io
import json
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


class LeaseCliTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.lease_dir = self.root / "leases"
        self.hardware = self.root / "hardware.yaml"
        self.hardware.write_text(HARDWARE)

    def tearDown(self):
        self._dir.cleanup()

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
        self.assertEqual([False, True], [s["held"] for s in json.loads(raw)])

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

    def test_wait_for_boards_rejects_negative_minutes(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["lease", "--hardware", str(self.hardware), "--wait-for-boards", "-5",
                  "UT_0", "--", "true"])

    def test_lease_returns_the_command_exit_code(self):
        code, _out, _err = self._main(
            "lease", "--hardware", str(self.hardware), "--lease-dir", str(self.lease_dir),
            "UT_0", "--", sys.executable, "-c", "raise SystemExit(3)",
        )
        self.assertEqual(3, code)


if __name__ == "__main__":
    unittest.main()
