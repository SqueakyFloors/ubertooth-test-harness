"""A runnable plan names only implemented tests (#107)."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cli import main
from ubertooth_harness.plan import PlanError, check_runnable, load_plan, unimplemented_tests
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / "testplans"

# BLE soak has no implementation (catalog entry only).
_PLAN = """plan: Planned
{runnable}suites:
  quick:
    - Ubertooth count:
        hosts: each
    - BLE soak:
        duration: 1m
        sample every: 5s
"""


def _write(directory, runnable=""):
    path = Path(directory) / "plan.yaml"
    path.write_text(_PLAN.format(runnable=runnable), encoding="utf-8")
    return path


def _validate(path):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["validate-plan", str(path)])
    return code, out.getvalue(), err.getvalue()


class RunnablePlanTests(unittest.TestCase):

    def test_a_runnable_plan_with_an_unimplemented_test_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = load_plan(_write(directory))
            self.assertTrue(plan.runnable)
            self.assertEqual(("BLE soak",), unimplemented_tests(plan))
            with self.assertRaisesRegex(PlanError, "no implementation: BLE soak"):
                check_runnable(plan)
            code, out, err = _validate(_write(directory))
        self.assertEqual(2, code)
        self.assertIn("  - BLE soak", out)  # the listing still prints
        self.assertIn("names tests with no implementation: BLE soak", err)
        self.assertIn("runnable: false", err)

    def test_a_plan_marked_not_runnable_may_name_planned_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write(directory, "runnable: false\n")
            plan = load_plan(path)
            self.assertFalse(plan.runnable)
            check_runnable(plan)
            code, out, _err = _validate(path)
        self.assertEqual(0, code)
        self.assertIn("plan: Planned (not runnable: planned tests)", out)

    def test_runnable_must_be_a_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PlanError, "runnable must be true or false"):
                load_plan(_write(directory, "runnable: no-thanks\n"))

    def test_every_committed_runnable_plan_names_only_implemented_tests(self):
        catalog = builtin_catalog()
        not_runnable = []
        for path in sorted(PLANS.glob("*.yaml")):
            plan = load_plan(path, catalog)
            with self.subTest(path.name):
                check_runnable(plan, catalog)
            if not plan.runnable:
                not_runnable.append(path.name)
                # A not-runnable plan exists for planned tests only.
                self.assertTrue(unimplemented_tests(plan, catalog), path.name)
        self.assertEqual(["backlog.yaml", "mac-polling.yaml"], not_runnable)

    def test_every_committed_plan_validates(self):
        for path in sorted(PLANS.glob("*.yaml")):
            with self.subTest(path.name):
                code, out, err = _validate(path)
                self.assertEqual(0, code, err)
                self.assertIn("plan: ", out)

    def test_every_committed_suite_compiles_against_the_example_bench(self):
        """Run planning leaves out hardware a registry has not got, so this
        is where a plan naming a host or role the bench should provide is
        caught (#265): every suite compiles strictly against
        `hardware.example.yaml`, which documents the whole bench.

        `range-weak-link` is the exception: it asks for a board behind a
        20 dB attenuator, and the example registry carries that `rf:` block
        commented out, as the optional setup it is.
        """
        catalog = builtin_catalog()
        registry = FileResourceRegistry.load(ROOT / "hardware.example.yaml")
        compiled = 0
        for path in sorted(PLANS.glob("*.yaml")):
            plan = load_plan(path, catalog)
            for suite in plan.suites:
                if suite == "range-weak-link":
                    continue
                with self.subTest(f"{path.name}:{suite}"):
                    PlanCompiler(catalog, registry).compile_suite(
                        plan, suite, strict_hardware=True
                    )
                    compiled += 1
        self.assertGreaterEqual(compiled, 16)

    def test_the_fast_plan_runs_spectrum_rssi_sanity_and_not_ble_monitor(self):
        names = [step.test_name for step in load_plan(PLANS / "fast.yaml").steps("fast")]
        self.assertIn("Spectrum RSSI sanity", names)
        self.assertNotIn("BLE monitor", names)
        backlog = [step.test_name for step in load_plan(PLANS / "backlog.yaml").steps("backlog")]
        self.assertIn("BLE monitor", backlog)


if __name__ == "__main__":
    unittest.main()
