"""Unit tests. Board leases go to a private temporary directory, never ~/."""

import atexit
import os
import shutil
import tempfile

_LEASES = tempfile.mkdtemp(prefix="harness-test-leases-")
os.environ["UBERTOOTH_HARNESS_LEASE_DIR"] = _LEASES
atexit.register(shutil.rmtree, _LEASES, ignore_errors=True)
