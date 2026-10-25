"""The libbtbb pin is one commit of one fork, written the same way everywhere.

`scripts/ci.sh` holds the pin the harness builds by default; README.md repeats
it in its examples. They must agree, and neither may fall back to a moving ref
such as `master`, or a run would test a different libbtbb from the one the
Ubertooth fork ships with. Mirrors the "Check the libbtbb pin" step in the
fork's own CI.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO = Path(__file__).resolve().parents[1]
CI_SH = REPO / "scripts" / "ci.sh"
README = REPO / "README.md"
FORK_URL = "https://github.com/SqueakyFloors/libbtbb.git"


def _pin() -> str:
    match = re.search(
        r"^LIBBTBB_PIN=([0-9a-f]{40})$", CI_SH.read_text(encoding="utf-8"), re.M
    )
    assert match, "scripts/ci.sh must set LIBBTBB_PIN to a full commit hash"
    return match.group(1)


class LibbtbbPinTests(unittest.TestCase):

    def test_ci_clones_the_fork_and_defaults_to_the_pin(self):
        text = CI_SH.read_text(encoding="utf-8")

        self.assertIn(f"LIBBTBB_URL={FORK_URL}", text)
        self.assertIn('git clone --quiet "$LIBBTBB_URL"', text)
        self.assertNotIn("greatscottgadgets/libbtbb", text)
        # Both run_build invocations default the ref to the pin, and the
        # LIBBTBB_REF override still wins.
        self.assertEqual(2, text.count('--libbtbb-ref "${LIBBTBB_REF:-$LIBBTBB_PIN}"'))
        self.assertNotIn("${LIBBTBB_REF:-master}", text)

    def test_readme_examples_name_the_same_pin(self):
        text = README.read_text(encoding="utf-8")
        # Stop at a backtick (the option table) or a `]` (an optional-argument
        # example), so only the ref itself is compared.
        refs = set(re.findall(r"--libbtbb-ref ([^\s`\]]+)", text))

        self.assertEqual({_pin()}, refs - {"REF"})


if __name__ == "__main__":
    unittest.main()
