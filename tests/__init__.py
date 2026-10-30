"""Unit tests. Board leases go to a private temporary directory, never ~/."""

import atexit
import os
import shutil
import tempfile

_LEASES = tempfile.mkdtemp(prefix="harness-test-leases-")
os.environ["UBERTOOTH_HARNESS_LEASE_DIR"] = _LEASES
# A host's external board lock template must not leak into the tests.
os.environ.pop("UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK", None)
atexit.register(shutil.rmtree, _LEASES, ignore_errors=True)
