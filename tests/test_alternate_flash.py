"""Planning for 'alternate Ubertooths': which boards a flash test writes in turn."""

import tempfile
import unittest
from pathlib import Path

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError, alternated_roles
from ubertooth_harness.preflight import PreflightError, PreflightPolicy
from ubertooth_harness.resources import FileResourceRegistry

A, B, C = "a" * 32, "b" * 32, "c" * 32
KGV = "e" * 64


def hardware(serials):
    boards = "".join(
        f"""  UT_{index}:
    type: Ubertooth
    attached to: Host_Linux
    identity: {{serial: {serial}}}
"""
        for index, serial in enumerate(serials)
    )
    return f"""resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [test target]
    defaults: {{Ubertooth: UT_0}}
{boards}"""


def plan(flashes=4, ubertooth="default", extra="", hosts="[Host_Linux]"):
    return f"""plan: alternate
suites:
  slow:
    - Flash reliability:
        hosts: {hosts}
        platforms: [linux]
        Ubertooth: {ubertooth}
        flashes: {flashes}
        image sequence: [test build, KGV]
        KGV sha256: {KGV}
        KGV revision: 2020-12-R1
        alternate Ubertooths: true
{extra}"""


class AlternatePlanningTests(unittest.TestCase):

    def compile(self, plan_text, serials=(A, B)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hardware.yaml").write_text(hardware(serials))
            (root / "plan.yaml").write_text(plan_text)
            registry = FileResourceRegistry.load(root / "hardware.yaml")
            invocations = PlanCompiler(builtin_catalog(), registry).compile(
                load_plan(root / "plan.yaml"), "slow"
            )
            return invocations, registry

    def test_other_boards_are_bound_as_alternated_resources_in_order(self):
        (invocation,), _ = self.compile(plan(6), serials=(A, B, C))

        self.assertEqual("UT_0", invocation.resources["Ubertooth"])
        roles = alternated_roles(invocation.resources)
        self.assertEqual(("alternated Ubertooth 2", "alternated Ubertooth 3"), roles)
        self.assertEqual(["UT_1", "UT_2"], [invocation.resources[r] for r in roles])
        self.assertEqual(B, invocation.resource_identities["alternated Ubertooth 2"]["serial"])

    def test_hosts_each_and_ubertooth_each_are_refused_without_a_registry(self):
        from ubertooth_harness.plan import PlanError

        for text, message in ((plan(hosts="each"), r"hosts: \[\.\.\.\], not each"),
                              (plan(ubertooth="each"), "not each")):
            with self.subTest(message), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "plan.yaml"
                path.write_text(text)
                with self.assertRaisesRegex(PlanError, message):
                    load_plan(path)

    def test_flash_request_alternates_boards_and_budgets_every_query(self):
        from ubertooth_harness.cases import FlashCase
        from types import SimpleNamespace
        from ubertooth_harness.sequencer import NamedTestSequencer

        (invocation,), registry = self.compile(plan(6), serials=(A, B, C))
        sequencer = NamedTestSequencer(object(), registry=registry)

        class Manifest:
            artifacts = (
                SimpleNamespace(name="test build", path="/t.dfu", sha256="1" * 64),
                SimpleNamespace(name="KGV", path="/k.dfu", sha256=KGV),
            )

            @staticmethod
            def source(_name):
                class Source:
                    expected_revision = "git-test"
                return Source

        case = FlashCase("flash-reliability", "Flash reliability",
                         invocation.arguments, A)
        request = sequencer._flash_request(case, invocation, Manifest)
        self.assertEqual((A, B, C), request.targets)
        self.assertEqual((), request.guard_serials)
        # 3 queries per flash (target + 2 others) and once for the baselines.
        self.assertEqual(5.0 * 3 + 6 * (25.0 + 5.0 * 3), request.total_timeout_s)

    def test_a_single_board_is_refused(self):
        with self.assertRaisesRegex(PlanningError, "at least two registered"):
            self.compile(plan(2), serials=(A,))

    def test_short_serials_are_refused(self):
        with self.assertRaisesRegex(PlanningError, "UT_1 needs a full 32-hex-digit serial"):
            self.compile(plan(), serials=(A, "c32000f5"))

    def test_uneven_flash_count_is_refused_at_planning(self):
        with self.assertRaisesRegex(PlanningError, "cannot alternate evenly across 3"):
            self.compile(plan(100), serials=(A, B, C))

    def test_later_test_on_an_alternated_board_is_refused(self):
        later = """    - Firmware version:
        hosts: each
        Ubertooth: UT_1
"""
        invocations, registry = self.compile(plan(extra=later))
        with self.assertRaisesRegex(PreflightError, "would run on UT_1"):
            PreflightPolicy(registry).check_order(invocations)

    def test_preflight_checks_every_alternated_board_serial_only(self):
        invocations, registry = self.compile(plan())
        established = PreflightPolicy(registry)._firmware_established_by_suite(invocations)
        self.assertEqual({"UT_0", "UT_1"}, established)


if __name__ == "__main__":
    unittest.main()
