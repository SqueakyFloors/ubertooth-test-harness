"""The libbtbb pin is one commit of one fork, written the same way everywhere.

`scripts/ci.sh` holds the pin the harness builds by default; README.md repeats
it in its examples. They must agree, and neither may fall back to a moving ref
such as the `master` branch, or a run would test a different libbtbb from
the one the Ubertooth fork ships with. Mirrors the "Check the libbtbb pin"
step in the fork's own CI.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[1]
CI_SH = REPO / "scripts" / "ci.sh"
README = REPO / "README.md"
FORK_URL = "https://github.com/SqueakyFloors/libbtbb.git"
FORK_PIN = "fork-pin"


def _pin() -> str:
    match = re.search(
        r"^LIBBTBB_PIN=([0-9a-f]{40})$", CI_SH.read_text(encoding="utf-8"), re.M
    )
    if match is None:
        raise AssertionError(
            "scripts/ci.sh must set LIBBTBB_PIN to a full commit hash"
        )
    return match.group(1)


class LibbtbbPinTests(unittest.TestCase):

    def test_ci_clones_the_fork_and_defaults_to_the_pin(self):
        text = CI_SH.read_text(encoding="utf-8")

        self.assertIn(f"LIBBTBB_URL={FORK_URL}", text)
        self.assertIn('git clone --quiet "$LIBBTBB_URL"', text)
        self.assertNotIn("greatscottgadgets/libbtbb", text)
        # Every --libbtbb-ref the script passes defaults to the pin, and the
        # LIBBTBB_REF override still wins over it. A new build invocation is
        # welcome; one that reaches for another ref is not.
        passed = re.findall(r"--libbtbb-ref (\S+)", text)
        self.assertTrue(passed, "scripts/ci.sh passes no --libbtbb-ref")
        self.assertEqual(
            ['"${LIBBTBB_REF:-$LIBBTBB_PIN}"'] * len(passed), passed,
            "every --libbtbb-ref in scripts/ci.sh must default to LIBBTBB_PIN",
        )

    def test_readme_examples_name_the_same_pin(self):
        text = README.read_text(encoding="utf-8")
        # Stop at a backtick (the option table) or a `]` (an optional-argument
        # example), so only the ref itself is compared. `REF` is the
        # placeholder in option tables; `fork-pin` is the value that resolves
        # per run from the Ubertooth commit under test, not a commit.
        refs = set(re.findall(r"--libbtbb-ref ([^\s`\]]+)", text))

        self.assertEqual({_pin()}, refs - {"REF", FORK_PIN})


if __name__ == "__main__":
    unittest.main()
