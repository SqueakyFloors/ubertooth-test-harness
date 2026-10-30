#!/usr/bin/env python3
"""Run the unit test suite with one process per test module.

    scripts/test.py [-j N] [--durations N] [-v] [MODULE ...]

Each module under tests/ runs in a process of its own, N at a time (default:
one per core), and the results are combined into one summary with the usual
exit status: 0 when everything passed or skipped, 1 otherwise. A module may be
named as ``tests.test_gui``, ``test_gui`` or ``tests/test_gui.py``; with none,
every ``tests/test_*.py`` runs.

The suite also still runs the plain way, which is what to reach for when a
single module or test is what matters:

    python -m unittest discover -s tests
    python -m unittest tests.test_gui -v

Separate processes are what makes the modules safe to run at once: each child
imports ``tests``, which gives that process a board lease directory of its
own, and no module shares a port, a lock file or the current directory with
another. A module that waits on a real timeout bounds the whole run, so the
longest modules start first: by their wall time in the last run, cached in
tests/.test-durations.json, and by file size until that file exists.

``--durations N`` prints the N slowest tests of the whole run, as
``python -m unittest --durations N`` does for a serial run. A module's output
is shown when it failed, or with ``-v`` for every module, since a child's
output only arrives when it ends. A module that hangs is stopped after
TIMEOUT_S and reported as an error, so one hung test cannot hold a run.

Ctrl-C, or a SIGTERM to the runner, stops every child and the commands they
started: each child leads a process group of its own, which neither signal
reaches by itself.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
# module -> wall seconds in the last full run, so the next run can start the
# longest modules first. Losing the file only costs a worse order.
CACHE = TESTS / ".test-durations.json"
# Long enough for the slowest module on a small, busy runner; a module that
# reaches it has hung, and is stopped so the rest of the run still reports.
TIMEOUT_S = 900.0
# Between SIGTERM and SIGKILL for a child that is being stopped.
STOP_WAIT_S = 2.0
SEPARATOR1 = "=" * 70
SEPARATOR2 = "-" * 70


# --------------------------------------------------------------- the worker
# One child process, running one module.

class _TimingResult(unittest.TextTestResult):
    """A result that also records how long each test took (--durations)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings: list[tuple[str, float]] = []
        self._started = 0.0

    def startTest(self, test):
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        self.timings.append((test.id(), time.perf_counter() - self._started))


def _worker(module: str, verbosity: int, result_path: str) -> int:
    """Run one module's tests, write the result as JSON, return its status."""
    # python scripts/test.py puts scripts/ first on the path, where the tests
    # are not and scripts/test.py would shadow the standard library's test
    # package; the repository root is what `python -m unittest` uses.
    sys.path[0] = str(ROOT)
    tests = unittest.defaultTestLoader.loadTestsFromName(module)
    result = unittest.TextTestRunner(
        stream=sys.stderr, verbosity=verbosity, resultclass=_TimingResult).run(tests)
    report = {
        "module": module,
        "tests": result.testsRun,
        "failures": [[str(test), text] for test, text in result.failures],
        "errors": [[str(test), text] for test, text in result.errors],
        "skipped": [[str(test), reason] for test, reason in result.skipped],
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [str(test) for test in result.unexpectedSuccesses],
        "timings": result.timings,
    }
    with open(result_path, "w") as file:
        json.dump(report, file)
    return 0 if result.wasSuccessful() else 1


# --------------------------------------------------------------- the parent

# The children now running, so an interrupt can stop them: they lead process
# groups of their own and do not get the terminal's interrupt themselves.
# _stopping closes the run to new children, so none is started, and left
# running, after the interrupt.
_lock = threading.Lock()
_running: set = set()
_stopping = False


class Outcome:
    """What one child reported, or what its absence says."""

    def __init__(self, module: str, seconds: float):
        self.module = module
        self.seconds = seconds
        self.tests = 0
        self.failures: list[list[str]] = []
        self.errors: list[list[str]] = []
        self.skipped: list[list[str]] = []
        self.expected_failures = 0
        self.unexpected_successes: list[str] = []
        self.timings: list[list] = []
        self.output = ""

    @property
    def problems(self) -> int:
        return (len(self.failures) + len(self.errors)
                + len(self.unexpected_successes))

    def status(self) -> str:
        return "FAIL" if self.problems else "ok"


def _cores() -> int:
    """The cores this process may use (a CI container may be given fewer)."""
    if hasattr(os, "sched_getaffinity"):  # Linux
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def _modules(names: list[str]) -> list[str]:
    """The modules to run, as dotted names, from the command line or tests/."""
    if not names:
        # test*.py directly under tests/, which is what `unittest discover`
        # finds there; tests/test_suite_runner.py checks the two agree.
        return sorted(f"tests.{path.stem}" for path in TESTS.glob("test*.py"))
    chosen = []
    for name in names:
        # tests/test_gui.py and tests/test_gui are paths; tests.test_gui and
        # test_gui are module names.
        stem = (Path(name).stem if name.endswith(".py") or "/" in name
                else name.rsplit(".", 1)[-1])
        if not (TESTS / f"{stem}.py").exists():
            raise SystemExit(f"error: no test module tests/{stem}.py")
        chosen.append(f"tests.{stem}")
    return chosen


def _longest_first(modules: list[str]) -> list[str]:
    """Longest module first, by the last run's times and then by file size:
    the longest module bounds a parallel run, so it must not start last."""
    try:
        with open(CACHE) as file:
            known = json.load(file)
        if not isinstance(known, dict):
            known = {}
    except (OSError, ValueError):
        known = {}

    def weight(module: str) -> tuple:
        path = TESTS / f"{module.rsplit('.', 1)[-1]}.py"
        seconds = known.get(module)
        size = path.stat().st_size if path.exists() else 0
        # Modules with a known time come first, longest first; the rest follow
        # biggest first, since a bigger module is usually the slower one.
        return (0 if isinstance(seconds, (int, float)) else 1,
                -(seconds if isinstance(seconds, (int, float)) else 0), -size)

    return sorted(modules, key=weight)


def _save_durations(outcomes: list[Outcome]) -> None:
    """Keep each module's wall time for the next run's order."""
    try:
        # .gitignore covers the name and these: a run killed between the
        # write and the rename must not leave the tree dirty.
        with tempfile.NamedTemporaryFile(
                "w", dir=TESTS, prefix=".test-durations-", suffix=".tmp",
                delete=False) as file:
            json.dump({o.module: round(o.seconds, 3) for o in outcomes}, file,
                      indent=1, sort_keys=True)
            temporary = file.name
        os.replace(temporary, CACHE)
    except OSError:
        pass  # A read-only tree only costs the ordering.


def _signal_group(child, number: int) -> None:
    """Signal a child and everything it started; it leads its own group."""
    try:
        os.killpg(child.pid, number)
    except (OSError, ProcessLookupError):
        pass


def _stop(children) -> None:
    """Stop these children's process groups, SIGTERM then SIGKILL.

    The group, not the child alone, so a command the tests started and did
    not stop goes too: one left running holds a lock or a port the rest of
    the run needs, and its end of the child's output pipe. A second
    interrupt does not get out of here with children still running.
    """
    children = list(children)
    for child in children:
        _signal_group(child, signal.SIGTERM)
    try:
        time.sleep(STOP_WAIT_S)
    except KeyboardInterrupt:
        pass  # Interrupted again: do not wait, kill now.
    while True:
        try:
            for child in children:
                _signal_group(child, signal.SIGKILL)
            return
        except KeyboardInterrupt:
            continue


def _interrupt() -> None:
    """Close the run to new children and stop the ones running."""
    global _stopping
    with _lock:
        _stopping = True
        children = list(_running)
    _stop(children)


def _interrupted(number, _frame):
    """A signal aimed at the runner has to take the children with it: they
    are in sessions of their own, so a job cancel or a kill never reaches
    them. Raising here goes through the one interrupt path below."""
    raise KeyboardInterrupt(f"signal {number}")


def _register(child) -> bool:
    """Track a running child; False when the run is already stopping."""
    with _lock:
        if _stopping:
            return False
        _running.add(child)
        return True


def _decode(output) -> str:
    """The partial output of a child that timed out, bytes or text."""
    if not output:
        return ""
    return output if isinstance(output, str) else output.decode(errors="replace")


def _not_run(module: str) -> Outcome:
    outcome = Outcome(module, 0.0)
    outcome.errors.append([module, "the run was interrupted before this "
                                   "module ran\n"])
    return outcome


def _start(module: str, verbosity: int, timeout: float = TIMEOUT_S) -> Outcome:
    """Run one module in a child process and collect what it reported."""
    if _stopping:
        return _not_run(module)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="harness-test-") as directory:
        result_path = os.path.join(directory, "result.json")
        argv = [sys.executable, str(Path(__file__).resolve()),
                "--worker", module, "--worker-result", result_path,
                "--worker-verbosity", str(verbosity)]
        # Each child leads its own process group, so a module that hangs can
        # be stopped along with the commands it started: one of those left
        # running would hold a lock or a port the rest of the run needs.
        child = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
        if not _register(child):
            # The interrupt landed between the check above and this child.
            _stop([child])
            return _not_run(module)
        timed_out = False
        try:
            output, errors = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            # A hung test would otherwise hold the whole run with nothing to
            # read, since a child's output only arrives when it ends.
            timed_out = True
            output, errors = _decode(expired.stdout), _decode(expired.stderr)
            _stop([child])
            try:
                # Bounded: a process that left the group still holds the
                # write end of the pipe, and this is the hang the timeout
                # exists to end. What the child printed before it hung is
                # the only evidence of where, so it is kept either way.
                output, errors = child.communicate(timeout=STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                errors += "(something still holds the output pipe open)\n"
        finally:
            _running.discard(child)
        outcome = Outcome(module, time.monotonic() - started)
        outcome.output = output + errors
        if timed_out:
            outcome.errors.append([
                module, f"the tests did not finish within {timeout:g}s and were "
                        f"stopped\n{outcome.output or '(no output)'}\n"])
            return outcome
        try:
            with open(result_path) as file:
                report = json.load(file)
        except (OSError, ValueError):
            # The child died without a result: a crash, an import that called
            # exit(), a signal. Its output is the only evidence, so show it.
            outcome.errors.append([
                module,
                f"the test process exited {child.returncode} without a result\n"
                + (outcome.output or "(no output)\n")])
            return outcome
    outcome.tests = report["tests"]
    outcome.failures = report["failures"]
    outcome.errors = report["errors"]
    outcome.skipped = report["skipped"]
    outcome.expected_failures = report["expected_failures"]
    outcome.unexpected_successes = report["unexpected_successes"]
    outcome.timings = report["timings"]
    if child.returncode not in (0, 1) or (child.returncode and not outcome.problems):
        outcome.errors.append([
            module, f"the test process exited {child.returncode}\n"
                    + (outcome.output or "(no output)\n")])
    return outcome


def _report(outcomes: list[Outcome], seconds: float, jobs: int, durations: int,
            verbose: bool) -> bool:
    """Print the combined result the way unittest does; True when it passed."""
    out = sys.stderr
    for outcome in outcomes:
        if outcome.output and (verbose or outcome.problems):
            out.write(f"\n{SEPARATOR2}\n{outcome.module} output:\n{outcome.output}")
    for outcome in outcomes:
        for kind, items in (("FAIL", outcome.failures), ("ERROR", outcome.errors)):
            for name, text in items:
                out.write(f"\n{SEPARATOR1}\n{kind}: {name}\n{SEPARATOR2}\n{text}\n")
        for name in outcome.unexpected_successes:
            out.write(f"\n{SEPARATOR1}\nUNEXPECTED SUCCESS: {name}\n{SEPARATOR2}\n")

    if durations:
        timings = sorted((timing for outcome in outcomes
                          for timing in outcome.timings),
                         key=lambda timing: timing[1], reverse=True)[:durations]
        out.write(f"\nSlowest test durations\n{SEPARATOR2}\n")
        for name, took in timings:
            out.write(f"{took:.3f}s{'':4}{name}\n")
        out.write(f"\nSlowest modules\n{SEPARATOR2}\n")
        for outcome in sorted(outcomes, key=lambda o: o.seconds, reverse=True)[:durations]:
            out.write(f"{outcome.seconds:.3f}s{'':4}{outcome.module}\n")

    tests = sum(outcome.tests for outcome in outcomes)
    out.write(f"\n{SEPARATOR2}\nRan {tests} test{'s' * (tests != 1)} in "
              f"{seconds:.3f}s ({len(outcomes)} modules, {jobs} at a time)\n\n")
    counts = {
        "failures": sum(len(outcome.failures) for outcome in outcomes),
        "errors": sum(len(outcome.errors) for outcome in outcomes),
        "skipped": sum(len(outcome.skipped) for outcome in outcomes),
        "expected failures": sum(outcome.expected_failures for outcome in outcomes),
        "unexpected successes": sum(
            len(outcome.unexpected_successes) for outcome in outcomes),
    }
    passed = not (counts["failures"] or counts["errors"]
                  or counts["unexpected successes"])
    detail = ", ".join(f"{name}={count}" for name, count in counts.items() if count)
    out.write(("OK" if passed else "FAILED")
              + (f" ({detail})" if detail else "") + "\n")
    return passed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/test.py", description=__doc__.splitlines()[0])
    parser.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                        help="modules to run at a time (0, the default: one "
                             "per core)")
    parser.add_argument("--durations", type=int, default=0, metavar="N",
                        help="print the N slowest tests and modules")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="name each test, and show every module's output")
    parser.add_argument("modules", nargs="*", metavar="MODULE")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--worker-result", help=argparse.SUPPRESS)
    parser.add_argument("--worker-verbosity", type=int, default=1,
                        help=argparse.SUPPRESS)
    options = parser.parse_args(argv)
    if options.worker:
        return _worker(options.worker, options.worker_verbosity,
                       options.worker_result)

    modules = _longest_first(_modules(options.modules))
    if not modules:
        raise SystemExit("error: no test modules found in tests/")
    jobs = options.jobs or min(len(modules), _cores())
    if jobs < 1:
        raise SystemExit(f"error: --jobs must be 1 or more, not {jobs}")
    verbosity = 2 if options.verbose else 1
    for number in (signal.SIGTERM, signal.SIGHUP):
        # Not where it is already ignored: under nohup, a closed terminal
        # must not end the run.
        if signal.getsignal(number) is not signal.SIG_IGN:
            signal.signal(number, _interrupted)
    print(f"running {len(modules)} test modules, {jobs} at a time",
          file=sys.stderr, flush=True)

    started = time.monotonic()
    outcomes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        running = {pool.submit(_start, module, verbosity): module
                   for module in modules}
        try:
            for future in concurrent.futures.as_completed(running):
                outcome = future.result()
                outcomes.append(outcome)
                print(f"{outcome.status():>4} {outcome.seconds:6.1f}s "
                      f"{outcome.tests:4d} tests  {outcome.module}",
                      file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            # The children lead their own sessions, so neither the terminal's
            # interrupt nor a signal sent to this process reaches them; they
            # are stopped outright, or leaving this block would wait for
            # every one of them.
            for future in running:
                future.cancel()
            print("\ninterrupted", file=sys.stderr, flush=True)
            _interrupt()
            return 1
    seconds = time.monotonic() - started

    outcomes.sort(key=lambda outcome: outcome.module)
    if not options.modules:
        _save_durations(outcomes)
    return 0 if _report(outcomes, seconds, jobs, options.durations,
                        options.verbose) else 1


if __name__ == "__main__":
    sys.exit(main())
