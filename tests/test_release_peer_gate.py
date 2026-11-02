"""The release gate holds a test for the ESP32 Classic peers (#250).

`Recover AFH map` is release coverage, and the peers it needs are on one
bench host, so its entry in `testplans/release.yaml` names that host. What
makes that honest rather than a plan written for somebody else's bench is
`required hardware: true`: a registry without the peers, or without the host
they are on, compiles the plan and runs every other release test, records
this one as blocked, and finishes the run failed. It is never left out
quietly, because a release gate that reads as a pass for something nobody
measured is worse than one that fails.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.test_cli import make_source
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cli import main
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.resources import (
    FileResourceRegistry,
    MissingResource,
)

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PLAN = ROOT / "testplans" / "release.yaml"
AFH = "Recover AFH map"

# The bench: the Linux host the release plan names, its Ubertooth, and the
# two ESP32 Classic peers the AFH test holds a piconet with.
BENCH = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Linux0
      ESP32 Classic peer: ESP32_Linux0
      acceptor: ESP32_Linux0
      initiator: ESP32_Linux1
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ESP32_Linux0:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "0123456789abcdef0123456789abcdef"
      BD_ADDR: "C8:00:00:00:00:01"
  ESP32_Linux1:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "fedcba9876543210fedcba9876543210"
      BD_ADDR: "C8:00:00:00:00:02"
"""

# The same host with no peer registered: everything else in the plan runs.
NO_PEERS = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults: {Ubertooth: UT_Linux0}
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
"""

# One peer declaring both roles, and named as the default for both, as the
# example registry names each of its two. Both roles resolve, to the same
# board, so the test has no second device to hold the link with.
ONE_PEER_BOTH_ROLES = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Linux0
      acceptor: ESP32_Linux0
      initiator: ESP32_Linux0
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ESP32_Linux0:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: "0123456789abcdef0123456789abcdef"
      BD_ADDR: "C8:00:00:00:00:01"
"""

# One peer, registered for one role only: the initiator has nowhere to go.
ONE_PEER = """
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Linux0
      acceptor: ESP32_Linux0
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity: {serial: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  ESP32_Linux0:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor]
    identity:
      bridge serial: "0123456789abcdef0123456789abcdef"
      BD_ADDR: "C8:00:00:00:00:01"
"""

# A bench that is not the one the step names at all.
MAC_ONLY = """
resources:
  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: "cccccccccccccccccccccccccccccccc"}
"""


# Added to a registry so a dry run can be made on the machine running the
# tests, whichever platform that is.
MAC_HOST = """  Host_Mac:
    type: Host
    platform: macos
    roles: [test target]
    defaults: {Ubertooth: UT_Mac0}
  UT_Mac0:
    type: Ubertooth
    attached to: Host_Mac
    identity: {serial: "cccccccccccccccccccccccccccccccc"}
"""


def registry(directory, text):
    path = Path(directory) / "hardware.yaml"
    path.write_text(text, encoding="utf-8")
    return FileResourceRegistry.load(path)


class ReleaseGateTests(unittest.TestCase):
    """The committed release plan against four registries."""

    def setUp(self):
        self.catalog = builtin_catalog()
        self.plan = load_plan(RELEASE_PLAN, self.catalog)

    def compile(self, hardware, strict=False):
        with tempfile.TemporaryDirectory() as directory:
            return PlanCompiler(
                self.catalog, registry(directory, hardware)
            ).compile_suite(self.plan, "release", strict_hardware=strict)

    def test_the_plan_names_the_afh_test_as_required_hardware(self):
        steps = {step.test_name: step for step in self.plan.steps("release")}
        self.assertIn(AFH, steps)
        arguments = steps[AFH].arguments
        self.assertIs(True, arguments["required hardware"])
        self.assertEqual(("Host_Linux",), arguments["hosts"])
        # Smoke, two captures of two minutes: the comprehensive profile's
        # third case stays in the classic plan.
        self.assertEqual("smoke", arguments["coverage"])
        self.assertEqual("2m", arguments["duration"])

    def test_the_bench_with_the_peers_runs_it(self):
        compiled = self.compile(BENCH)

        (invocation,) = [
            item for item in compiled.invocations if item.test_name == AFH
        ]
        self.assertEqual("Host_Linux", invocation.host)
        self.assertEqual(
            {"Ubertooth": "UT_Linux0", "acceptor": "ESP32_Linux0",
             "initiator": "ESP32_Linux1"},
            {role: alias for role, alias in invocation.resources.items()
             if not role.startswith("guard ")},
        )
        self.assertEqual((), compiled.blocked)
        self.assertEqual((), compiled.blocked_for_host("Host_Linux"))
        self.assertEqual(
            [], [item for item in compiled.for_host("Host_Linux")
                 if item.test_name == AFH]
        )

    def test_another_host_of_the_same_bench_neither_runs_nor_fails_on_it(self):
        """The step names the host the peers are on, and that host is here,
        so a second host of the bench has nothing to say about it: its own
        run does not carry the test, and does not fail on it. A run group of
        the release plan across the bench runs the gate once."""
        compiled = self.compile(BENCH + MAC_HOST)

        self.assertEqual((), compiled.blocked)
        self.assertEqual((), compiled.blocked_for_host("Host_Mac"))
        self.assertEqual(
            ["Host_Linux"],
            [item.host for item in compiled.invocations
             if item.test_name == AFH],
        )
        self.assertEqual(
            [], [item for item in compiled.for_host("Host_Mac")
                 if item.test_name == AFH]
        )

    def test_the_same_bench_without_its_peers_is_blocked_and_the_rest_runs(self):
        compiled = self.compile(NO_PEERS)

        (blocked,) = compiled.blocked
        self.assertEqual(("Host_Linux", AFH), (blocked.host, blocked.test_name))
        self.assertEqual(
            "host Host_Linux has no default ESP32 Classic peer for the "
            "acceptor role",
            blocked.reason,
        )
        self.assertEqual(
            f"{AFH}: blocked on Host_Linux: host Host_Linux has no default "
            "ESP32 Classic peer for the acceptor role",
            blocked.describe(),
        )
        # The run this host would make carries it, and the rest of the gate
        # still compiles.
        self.assertEqual((blocked,), compiled.blocked_for_host("Host_Linux"))
        self.assertNotIn(AFH, [item.test_name for item in compiled.invocations])
        self.assertGreater(len(compiled.invocations), 20)
        # Never also recorded as not applicable: the blocked record is what
        # the run fails on, and two records would show the test twice.
        self.assertEqual(
            [], [item for item in compiled.for_host("Host_Linux")
                 if item.test_name == AFH]
        )

    def test_a_host_with_one_peer_cannot_fill_the_second_role_either(self):
        compiled = self.compile(ONE_PEER)

        (blocked,) = compiled.blocked
        self.assertEqual(("Host_Linux", AFH), (blocked.host, blocked.test_name))
        self.assertEqual(
            "host Host_Linux has no default ESP32 Classic peer for the "
            "initiator role",
            blocked.reason,
        )

    def test_one_peer_taking_both_roles_is_blocked_not_a_plan_error(self):
        """A bench with one peer registered for both roles resolves both
        roles to it, and the test needs two devices. That is hardware this
        bench has not got, not a plan to correct, so the gate is blocked and
        the rest of the suite still compiles: raising here would stop every
        other release test from running."""
        compiled = self.compile(ONE_PEER_BOTH_ROLES)

        (blocked,) = compiled.blocked
        self.assertEqual(("Host_Linux", AFH), (blocked.host, blocked.test_name))
        self.assertEqual(
            "no device of its own for each role: Ubertooth from UT_Linux0, "
            "acceptor from ESP32_Linux0, initiator from ESP32_Linux0",
            blocked.reason,
        )
        self.assertNotIn(AFH, [item.test_name for item in compiled.invocations])
        self.assertGreater(len(compiled.invocations), 20)

    def test_a_registry_without_that_host_blocks_it_in_the_runs_it_has(self):
        """No run of `Host_Linux` will ever happen here, so the record goes
        to the runs that do: a gate nobody could measure must not pass."""
        compiled = self.compile(MAC_ONLY)

        self.assertEqual(("Host_Linux",), compiled.absent_hosts)
        (blocked,) = compiled.blocked
        self.assertEqual(("Host_Linux", AFH), (blocked.host, blocked.test_name))
        self.assertEqual(
            "Host_Linux is not a host in this registry", blocked.reason
        )
        self.assertEqual((blocked,), compiled.blocked_for_host("Host_Mac"))
        self.assertEqual(
            [], [item for item in compiled.for_host("Host_Mac")
                 if item.test_name == AFH]
        )
        self.assertTrue(
            all(item.host == "Host_Mac" for item in compiled.invocations)
        )
        self.assertGreater(len(compiled.invocations), 20)

    def test_strict_validation_refuses_every_absence(self):
        """`validate-plan --hardware` names the bench on purpose, so a
        missing peer there is a bench that is not set up as the plan says.
        Every absence a run would record is a refusal there, including the
        one peer that cannot fill two roles."""
        with self.assertRaisesRegex(
            PlanningError, "no default ESP32 Classic peer for the acceptor role"
        ):
            self.compile(NO_PEERS, strict=True)
        with self.assertRaisesRegex(
            PlanningError, "this registry has no host 'Host_Linux'"
        ):
            self.compile(MAC_ONLY, strict=True)
        with self.assertRaisesRegex(
            PlanningError,
            f"{AFH}: Host_Linux has no device of its own for each role: "
            ".*acceptor from ESP32_Linux0, initiator from ESP32_Linux0",
        ):
            self.compile(ONE_PEER_BOTH_ROLES, strict=True)
        # The bench that has them passes, as CI checks it does.
        self.compile(BENCH, strict=True)

    def test_validate_plan_against_the_example_bench_accepts_the_entry(self):
        """The example registry documents the whole bench, and CI validates
        the release plan against it, so the entry has to compile there."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([
                "validate-plan", str(RELEASE_PLAN),
                "--hardware", str(ROOT / "hardware.example.yaml"),
                "--suite", "release",
            ])
        self.assertEqual(0, code, err.getvalue())
        printed = out.getvalue()
        self.assertIn(f"{AFH}: Host_Linux [", printed)
        self.assertIn("acceptor=ESP32_Linux0", printed)
        self.assertIn("initiator=ESP32_Linux1", printed)

    def test_validate_plan_without_the_peers_refuses_the_whole_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.yaml"
            path.write_text(NO_PEERS, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main([
                    "validate-plan", str(RELEASE_PLAN),
                    "--hardware", str(path), "--suite", "release",
                ])
        self.assertEqual(2, code)
        self.assertIn("no default ESP32 Classic peer", err.getvalue())


class ReleaseDryRunTests(unittest.TestCase):
    """`run-suite --dry-run` of the release suite, on the machine running
    the tests: it names the blocked gate and exits 1, because a dry run that
    exits 0 says this host can run the suite.

    A dry run is made for the local platform's host, so which registry shows
    the absence depends on it: a Linux host is the one the step names and has
    no peers; a macOS host is not that host at all, and carries the record
    because no run of `Host_Linux` will ever happen on that bench.
    """

    def dry_run(self, hardware):
        host = "Host_Mac" if sys.platform == "darwin" else "Host_Linux"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _short = make_source(root)
            (root / "hardware.yaml").write_text(hardware, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main([
                    "run-suite", "--dry-run", "--plan", str(RELEASE_PLAN),
                    "--suite", "release",
                    "--hardware", str(root / "hardware.yaml"),
                    "--host", host, "--ubertooth-source", str(source),
                    "--ubertooth-ref", "HEAD", "--output", str(root / "runs"),
                    "--lease-dir", str(root / "leases"),
                ])
            return code, out.getvalue(), err.getvalue(), host

    def test_a_bench_without_the_peers_fails_the_dry_run(self):
        absent_host = sys.platform == "darwin"
        code, text, errors, host = self.dry_run(
            MAC_ONLY if absent_host else NO_PEERS
        )

        self.assertEqual(1, code, errors)
        self.assertIn(
            f"{AFH}: blocked on "
            + ("Host_Linux: Host_Linux is not a host in this registry"
               if absent_host else
               "Host_Linux: host Host_Linux has no default ESP32 Classic "
               "peer for the acceptor role"),
            text,
        )
        self.assertIn("1 test(s) blocked", text)
        self.assertIn(f"error: 1 required test(s) cannot run on {host}", errors)
        # The rest of the gate is still planned: the one absence does not
        # take the suite away.
        self.assertIn("Host CLI surface", text)

    def test_the_bench_that_has_them_passes_the_dry_run(self):
        code, text, errors, _host = self.dry_run(BENCH + MAC_HOST)

        self.assertEqual(0, code, errors)
        self.assertNotIn("blocked", text)
        if sys.platform != "darwin":
            # The gate's own invocation, with both peers bound.
            self.assertIn(AFH, text)
            self.assertIn("acceptor=ESP32_Linux0", text)


class RequiredHardwareArgumentTests(unittest.TestCase):
    """`required hardware:` itself: where it may be written, and what it
    changes. Without it, absence is left out as not applicable (#265)."""

    def setUp(self):
        self.catalog = builtin_catalog()

    def plan(self, directory, required):
        text = (
            "plan: gate\nsuites:\n  release:\n"
            "    - Host CLI surface:\n        hosts: each\n"
            f"    - {AFH}:\n"
            "        hosts: [Host_Linux]\n"
            + (f"        required hardware: {required}\n" if required else "")
            + "        Ubertooth: default\n"
            "        acceptor: default\n"
            "        initiator: default\n"
            "        coverage: smoke\n"
            "        duration: 2m\n"
        )
        path = Path(directory) / "plan.yaml"
        path.write_text(text, encoding="utf-8")
        return load_plan(path, self.catalog)

    def compile(self, required, hardware=NO_PEERS):
        with tempfile.TemporaryDirectory() as directory:
            return PlanCompiler(
                self.catalog, registry(directory, hardware)
            ).compile_suite(self.plan(directory, required), "release")

    def test_without_the_argument_absence_is_still_left_out(self):
        compiled = self.compile(None)

        self.assertEqual((), compiled.blocked)
        (left_out,) = compiled.not_applicable
        self.assertEqual(("Host_Linux", AFH), (left_out.host, left_out.test_name))
        self.assertEqual(
            [AFH], [item.test_name for item in compiled.for_host("Host_Linux")]
        )

    def test_written_false_it_is_still_left_out(self):
        compiled = self.compile("false")

        self.assertEqual((), compiled.blocked)
        self.assertEqual(1, len(compiled.not_applicable))

    def test_written_true_it_is_blocked(self):
        compiled = self.compile("true")

        self.assertEqual((), compiled.not_applicable)
        (blocked,) = compiled.blocked
        self.assertEqual(AFH, blocked.test_name)

    def test_without_it_one_device_in_two_roles_still_fails_planning(self):
        """Unchanged for a step that does not claim its hardware: a host
        that cannot give each role its own device is a plan to correct, so
        the refusal names what each role could have taken."""
        with self.assertRaisesRegex(
            PlanningError,
            "no valid distinct resource assignment: Host_Linux: .*"
            "acceptor from ESP32_Linux0, initiator from ESP32_Linux0",
        ):
            self.compile(None, ONE_PEER_BOTH_ROLES)

    def test_it_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Exception, "must be true or false"):
                self.plan(directory, "yes please")

    def test_a_test_that_binds_no_hardware_does_not_take_it(self):
        """There is no hardware for such a step to be without, so the
        argument would mean nothing; `rf:` is refused the same way."""
        definition = self.catalog.get("Host CLI surface")
        self.assertNotIn("required hardware", definition.arguments)
        self.assertNotIn("rf", definition.arguments)
        with self.assertRaisesRegex(Exception, "unknown argument"):
            definition.normalize_arguments({"required hardware": True})

    def test_every_test_that_binds_hardware_takes_it(self):
        for name in self.catalog.names:
            definition = self.catalog.get(name)
            with self.subTest(name):
                self.assertEqual(
                    bool(definition.hardware_roles),
                    "required hardware" in definition.arguments,
                )

    def test_two_steps_of_the_same_required_test_give_one_record(self):
        """One record per test and host, as the live run view shows it."""
        text = (
            "plan: gate\nsuites:\n  release:\n"
            "    - Host CLI surface:\n        hosts: each\n"
            + 2 * (
                f"    - {AFH}:\n"
                "        hosts: [Host_Linux]\n"
                "        required hardware: true\n"
                "        Ubertooth: default\n"
                "        acceptor: default\n"
                "        initiator: default\n"
                "        coverage: smoke\n"
                "        duration: 2m\n"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.yaml"
            path.write_text(text, encoding="utf-8")
            compiled = PlanCompiler(
                self.catalog, registry(directory, NO_PEERS)
            ).compile_suite(load_plan(path, self.catalog), "release")

        self.assertEqual(
            [("Host_Linux", AFH)],
            [(item.host, item.test_name) for item in compiled.blocked],
        )

    def test_a_host_that_ran_the_test_is_not_failed_by_an_absent_one(self):
        """A required step may name two hosts and find one of them here.
        That host's run measured the gate, so an absent host's record is not
        what it should fail on; `for_host` keeps a host's own record the
        same way. The run of a host that has neither still carries it."""
        text = (
            "plan: gate\nsuites:\n  release:\n"
            "    - Host CLI surface:\n        hosts: each\n"
            f"    - {AFH}:\n"
            "        hosts: [Host_Linux, Host_Lab]\n"
            "        required hardware: true\n"
            "        Ubertooth: default\n"
            "        acceptor: default\n"
            "        initiator: default\n"
            "        coverage: smoke\n"
            "        duration: 2m\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.yaml"
            path.write_text(text, encoding="utf-8")
            compiled = PlanCompiler(
                self.catalog, registry(directory, BENCH + MAC_HOST)
            ).compile_suite(load_plan(path, self.catalog), "release")

        # `Host_Lab` is not here, and `Host_Linux` ran the step.
        self.assertEqual(("Host_Lab",), compiled.absent_hosts)
        self.assertEqual(
            ["Host_Linux"],
            [item.host for item in compiled.invocations
             if item.test_name == AFH],
        )
        self.assertEqual((), compiled.blocked_for_host("Host_Linux"))
        # The host that neither ran it nor has a reason of its own does
        # carry it: no run of `Host_Lab` will ever happen here.
        (blocked,) = compiled.blocked_for_host("Host_Mac")
        self.assertEqual(("Host_Lab", AFH), (blocked.host, blocked.test_name))

    def test_a_suite_with_only_a_blocked_required_step_is_refused(self):
        """Nothing to run means no run to fail, so planning refuses it and
        names the step (#269)."""
        text = (
            f"plan: gate\nsuites:\n  release:\n    - {AFH}:\n"
            "        hosts: [Host_Linux]\n"
            "        required hardware: true\n"
            "        Ubertooth: default\n"
            "        acceptor: default\n"
            "        initiator: default\n"
            "        coverage: smoke\n"
            "        duration: 2m\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.yaml"
            path.write_text(text, encoding="utf-8")
            compiler = PlanCompiler(
                self.catalog, registry(directory, NO_PEERS)
            )
            with self.assertRaisesRegex(
                PlanningError, "produced no runnable invocations"
            ) as caught:
                compiler.compile_suite(load_plan(path, self.catalog), "release")
        self.assertIn("blocked on Host_Linux", str(caught.exception))

    def test_the_registry_refusal_is_the_one_absence_reports(self):
        """Sanity: the reason a blocked record carries is the registry's own
        wording, not one the planner invents."""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MissingResource) as caught:
                registry(directory, NO_PEERS).resolve_hardware(
                    "Host_Linux", "ESP32 Classic peer", "default", "acceptor"
                )
        self.assertEqual(
            "host Host_Linux has no default ESP32 Classic peer for the "
            "acceptor role",
            str(caught.exception),
        )


if __name__ == "__main__":
    unittest.main()
