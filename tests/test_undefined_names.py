"""No module may reference a global name it never imported or defined.

Python resolves a global name when the line runs, so a missing import in a
branch the unit suite never takes is invisible until something executes it.
Twice now that branch has been one a real board reaches and a fake does not:
`output_of` was missing from `catalog.py` (#194) and from `sequencer.py`
(#303), and the second one surfaced as a `NameError` in the middle of a
bootloader read on the bench, after the build and the flash had already run.

This checks the whole package for that one class of fault, so the next one
costs a test run instead of a hardware session.
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("ubertooth_harness", "tests", "scripts", "bootstrap.py")


class UndefinedNameTests(unittest.TestCase):
    def test_no_module_uses_a_name_it_never_defined(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:  # pragma: no cover - requirements.txt installs it
            self.skipTest("pyflakes is not installed")
        present = [str(ROOT / t) for t in TARGETS if (ROOT / t).exists()]
        completed = subprocess.run(
            [sys.executable, "-m", "pyflakes", *present],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        # Only undefined names: pyflakes' other reports (an unused import, a
        # redefinition) are style, and this is a correctness gate that must
        # not start failing for them.
        undefined = [
            line
            for line in completed.stdout.splitlines()
            if "undefined name" in line
        ]
        self.assertEqual(
            [],
            undefined,
            "a module references a name it never imported or defined:\n"
            + "\n".join(undefined),
        )


if __name__ == "__main__":
    unittest.main()
