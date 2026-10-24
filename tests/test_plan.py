import tempfile
import unittest
from pathlib import Path

import yaml

from ubertooth_harness.plan import PlanError, load_plan


class PlanLoadingTests(unittest.TestCase):

    def test_flash_compatibility_view_matches_named_slow_plan(self):
        path = Path(__file__).resolve().parents[1] / "testplans" / "slow.yaml"
        raw = yaml.safe_load(path.read_text())
        arguments = next(
            step["Flash reliability"]
            for step in raw["suites"]["slow"]
            if "Flash reliability" in step
        )

        plan = load_plan(path)

        self.assertEqual(raw["plan"], plan.name)
        self.assertEqual(
            tuple(sorted(arguments["platforms"])),
            tuple(sorted(step.platform for step in plan.flash_steps)),
        )
        for step in plan.flash_steps:
            self.assertEqual(arguments["flashes"], step.flashes)
            self.assertEqual(
                tuple(label.lower().replace(" ", "_") for label in arguments["image sequence"]),
                step.image_sequence,
            )
            self.assertEqual(arguments["KGV sha256"], step.kgv_sha256)
            self.assertEqual(arguments["flashes"], step.required_verified_flashes)

    def test_rejects_duplicate_flash_platform(self):
        text = """plan: duplicate
suites:
  slow:
    - Flash reliability:
        hosts: each
        platforms: [macos, macos]
        Ubertooth: default
        flashes: 2
        image sequence: [test build, KGV]
        KGV sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        KGV revision: 2020-12-R1
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.yaml"
            path.write_text(text)

            with self.assertRaisesRegex(PlanError, "duplicates"):
                load_plan(path)

    def test_rejects_missing_required_flash_field(self):
        text = """plan: missing
suites:
  slow:
    - Flash reliability:
        hosts: each
        platforms: [linux]
        Ubertooth: default
        image sequence: [test build, KGV]
        KGV sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        KGV revision: 2020-12-R1
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.yaml"
            path.write_text(text)

            with self.assertRaisesRegex(PlanError, "flashes"):
                load_plan(path)

    def test_rejects_unknown_test_name(self):
        text = """plan: unknown
suites:
  fast:
    - Execute arbitrary command: {}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.yaml"
            path.write_text(text)

            with self.assertRaisesRegex(PlanError, "unknown test"):
                load_plan(path)

    def test_rejects_unsafe_yaml_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.yaml"
            path.write_text("!!python/object/apply:os.system ['echo unsafe']")

            with self.assertRaises(PlanError):
                load_plan(path)


if __name__ == "__main__":
    unittest.main()
