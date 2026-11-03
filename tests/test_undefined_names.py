"""No module may reference a global name it never imported or defined.

Python resolves a global name when the line runs, so a missing import in a
branch the unit suite never takes is invisible until something executes it.
Twice now that branch has been one a fake does not reach: `output_of` was
missing from `catalog.py` (#194) and from `sequencer.py` (#303), and the
second one surfaced as a `NameError` in the middle of a bootloader read on
the bench, after the build and the flash had already run.

This checks the whole package for that one class of fault, so the next one
costs a test run instead of a hardware session.
"""

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories are walked for `*.py`. The lab scripts are Python with a
# shebang and no extension, so pyflakes skips them unless each is named:
# they are driven only through subprocesses, which is exactly the code this
# gate is for.
TARGETS = (
    "ubertooth_harness",
    "tests",
    "scripts",
    "bootstrap.py",
    "scripts/lab/hold",
    "scripts/lab/hub-port",
    "scripts/lab/peer",
)


class UndefinedNameTests(unittest.TestCase):
    def test_no_module_uses_a_name_it_never_defined(self):
        if importlib.util.find_spec("pyflakes") is None:  # pragma: no cover
            self.skipTest("pyflakes is not installed")
        present = [str(ROOT / t) for t in TARGETS if (ROOT / t).exists()]
        completed = subprocess.run(
            [sys.executable, "-m", "pyflakes", *present],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        # pyflakes exits 0 with nothing to say and 1 when it has findings.
        # Anything else, or any stderr, means it did not actually check what
        # it was given: a file it could not parse is reported there and then
        # never searched for undefined names, which would pass this test
        # while checking nothing.
        self.assertEqual("", completed.stderr, "pyflakes could not check the tree")
        self.assertIn(
            completed.returncode, (0, 1), f"pyflakes exited {completed.returncode}"
        )
        # Only undefined names. This is a correctness gate, and the tree has
        # other pyflakes reports today (an unused import, and the duplicate
        # definitions in report.py and leases.py filed as #308), so asserting
        # on all of them would fail for reasons that are not this fault.
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
