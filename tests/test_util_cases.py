"""Ubertooth util modes and Multi-board selection against fakes of the fork."""

import random
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ubertooth_harness import ledblink, util_cases
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.stateful import StateCase
from tests.test_guards import A, B, C, guard_checks, guarded_invocation, run_guarded
from tests.test_transitions import (
    SERIAL,
    FakeUbertooth,
    cases_for,
    patch_host_api,
    run_test,
    steps,
)


def setUpModule():
    patch_host_api()


def failed_cases(events):
    """Failed test-phase case IDs (guard checks excluded)."""
    return sorted(
        e.details["case_id"]
        for e in events
        if e.event_type == "check_finished" and e.status == "failed"
        and e.details.get("phase") == "test" and "-guard-" not in e.details["case_id"]
    )


def ends(case_ids, *names):
    """The names that some failed case ID ends with."""
    return sorted(n for n in names if any(c.endswith(n) for c in case_ids))


# ---------------------------------------------------------------------------
# Fake: board-side modes, LED sampling, -m


class ModeUbertooth(FakeUbertooth):
    """FakeUbertooth plus -I, -q, -m, and the LED sampler (fork main 3b22c95).

    -q takes 20-120 (-N dBm, bare -q is 85) and prints "Entering LED specan
    mode (threshold -N dBm)"; any mode stop runs the idle reset, including a
    stop by the next mode; -m with no result prints "no range test result
    yet". Knobs model the regressions (and earlier fork builds) the cases
    must catch:

    - ``specan_stop_resets``: stopping -q runs the idle reset; False is
      SqueakyFloors/ubertooth#191: the LEDs stay as the mode left them,
      channel and squelch kept.
    - ``replace_resets``: a mode's start runs the idle reset when it ends a
      prior mode; False skips it.
    - ``range_text``: what -m prints with no result ("invalid range test
      result" before fork 7180f74, #190).
    - ``blink``: identify toggles the LEDs; False leaves them steady.
    - ``stop_ends_mode``: -S ends identify/LED specan; False keeps it running.
    - ``mode_resets_channel``: starting a mode resets the channel at once.
    - ``range_values``: -m prints result values although no test ran.
    - ``read_stalls``: a predicate on the LED read attempt number (from 0)
      that makes that ``-d`` read fail with -9 (#213).
    - ``before_fcadf34``: the mode lines on stdout, and -I's two lines
      ("Flashing LEDs ...", then the blink note without the selector), as
      before fork fcadf34 (#326) moved them to stderr.
    """

    def __init__(self, *args, specan_stop_resets=True, replace_resets=True,
                 range_text="no range test result yet", blink=True,
                 stop_ends_mode=True, mode_resets_channel=False, range_values=False,
                 read_stalls=lambda attempt: False, before_fcadf34=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.before_fcadf34 = before_fcadf34
        self.specan_stop_resets = specan_stop_resets
        self.replace_resets = replace_resets
        self.range_text = range_text
        self.blink = blink
        self.stop_ends_mode = stop_ends_mode
        self.mode_resets_channel = mode_resets_channel
        self.range_values = range_values
        self.read_stalls = read_stalls
        self.read_attempts = 0
        self.blink_samplers = []
        self.threshold = None
        self.mode_options = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[:3] == ("sh", "-c", util_cases.LED_SAMPLER):
            self.commands.append(command)
            assert command[3:5] == ("ubertooth-util", self.serial), command
            code, stdout, stderr = self._sampler(int(command[5]))
            return self._result(command, code, stdout, stderr, on_output)
        if command[:2] == util_cases.led_blink_sampler_argv(self.serial)[:2]:
            self.commands.append(command)
            assert command == util_cases.led_blink_sampler_argv(self.serial), command
            code, stdout, stderr = self._sampler(util_cases.LED_BLINK_MAX_SAMPLES,
                                                 until_blink=True)
            self.blink_samplers.append(len(util_cases.led_samples(stdout)))
            return self._result(command, code, stdout, stderr, on_output)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)

    def _read(self):
        """One -d: (ok, report). A stalled read reports only USR and RX."""
        attempt = self.read_attempts
        self.read_attempts += 1
        report = self._sample()
        if self.read_stalls(attempt):
            return False, "".join(report.splitlines(keepends=True)[:2])
        return True, report

    def _sampler(self, count, until_blink=False):
        """The LED_SAMPLER shell loop: one retry per failed read. With
        ``until_blink`` (ledblink.py) it stops once a whole-on and a
        whole-off sample have both appeared; the fake takes no time, so the
        deadline never ends it first."""
        stdout, stderr = "", ""
        for index in range(count):
            samples = util_cases.led_samples(stdout)
            if until_blink and util_cases.ALL_ON in samples and util_cases.ALL_OFF in samples:
                break
            ok, report = self._read()
            if not ok:
                stderr += (f"TX LED read failed (-9)\n"
                           f"LED sample {index}: -d exited 1, retrying\n")
                ok, report = self._read()
                if not ok:
                    return 1, stdout, stderr + "TX LED read failed (-9)\n"
            stdout += report
        return 0, stdout, stderr

    def _sample(self):
        index = self.read_attempts
        if self.mode == "identify":
            on = index % 2 == 0 if self.blink else True
            usr = rx = tx = "1" if on else "0"
        elif self.mode == "led-specan":
            usr = rx = tx = "1" if self._lit() else "0"
        else:
            usr, rx, tx = self.state["usr"], self.state["rx"], self.state["tx"]
        return f"USR LED status: {usr}\nRX LED status : {rx}\nTX LED status : {tx}\n"

    def _stop_mode(self):
        if self.mode == "led-specan" and not self.specan_stop_resets:
            lit = "1" if self._lit() else "0"
            self.state.update(usr=lit, rx=lit, tx=lit)
            self.mode = None
            return
        super()._stop_mode()

    def _lit(self):
        raw = 54 - self.threshold
        # Ambient noise reads about -41 raw (-95 dBm); nothing reads -66 raw
        # (-120 dBm) or below.
        return raw < -41

    def _util(self, command, options, on_output):
        modes = [o for o in options if o == "-I" or o.startswith("-q")]
        if "-S" in options and not self.stop_ends_mode and self.mode in (
            "identify", "led-specan"
        ):
            return self._result(command, 0, "", f"Stopping ubertooth device {self.serial}\n",
                                on_output)
        if "-m" in options:
            if self.range_values:
                text = "request PA level : 7\nrequest number   : 3\n"
            else:
                text = self.range_text + "\n"
            rest = tuple(o for o in options if o != "-m")
            result = super()._util(command, rest, on_output=None) if rest else None
            stdout = text + (result.stdout if result else "")
            return self._result(command, 0, stdout, "", on_output)
        if not modes:
            return super()._util(command, options, on_output)
        mode = modes[0]
        if mode != "-I" and len(mode) > 2 and not 20 <= int(mode[2:]) <= 120:
            return self._result(
                command, 1, "", f"error: RSSI threshold must be 20-120 (got {mode[2:]})\n",
                on_output)
        rest = tuple(o for o in options if o not in modes)
        prefix = super()._util(command, rest, on_output=None).stdout if rest else ""
        self.mode_options.append(mode)
        if self.mode is not None and not self.replace_resets:
            self.mode = None
        self._stop_mode()  # each mode first leaves any prior mode
        if self.mode_resets_channel:
            self.state["channel"] = "2441"
        if mode == "-I":
            self.mode = "identify"
            if self.before_fcadf34:
                out = (f"Flashing LEDs on ubertooth device {self.serial}\n"
                       "The board is blinking; stop it with ubertooth-util -S or any "
                       "mode option\n")
            else:
                out = (f"note: the board is blinking; stop it with ubertooth-util "
                       f"-U {self.serial} -S or any mode option\n")
        else:
            self.threshold = int(mode[2:]) if len(mode) > 2 else 85
            self.mode = "led-specan"
            out = f"Entering LED specan mode (threshold -{self.threshold} dBm)\n"
        if self.before_fcadf34:
            return self._result(command, 0, prefix + out, "", on_output)
        return self._result(command, 0, prefix, out, on_output)


SMOKE_MODE_CASES = ["identify-blinks-until-stop", "led-specan-120-lights-leds",
                    "range-result-none-after-reset"]
Q_CASES = ("led-specan-120-lights-leds", "led-specan-20-runs-until-stop",
           "led-specan-default-runs-until-stop", "identify-replaces-led-specan",
           "led-specan-replaces-identify")


class UtilModeCaseTests(unittest.TestCase):

    def test_smoke_and_comprehensive_case_sets(self):
        _args, smoke = cases_for("Ubertooth util modes", "smoke")
        _args, full = cases_for("Ubertooth util modes", "comprehensive")
        self.assertEqual(SMOKE_MODE_CASES, [case.case_id for case in smoke])
        self.assertEqual(8, len(full))
        self.assertTrue(all(isinstance(case, StateCase) for case in full))
        self.assertTrue(set(Q_CASES) <= {case.case_id for case in full})

    def test_every_command_selects_the_board_and_none_transmits(self):
        _args, full = cases_for("Ubertooth util modes", "comprehensive")
        context = {
            "original channel": "2441", "original pa-level": "7",
            "original squelch": "-120", "original leds": "0/0/0", "original crc": "1",
        }
        for case in full:
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv(context)
                with self.subTest(case=case.case_id, step=step.step_id):
                    if argv[0] in ("sh", sys.executable):
                        self.assertIn(argv, (util_cases.led_sampler_argv(SERIAL),
                                             util_cases.led_blink_sampler_argv(SERIAL)))
                        continue
                    self.assertEqual(["-U", SERIAL], list(argv[1:3]))
                    flags = {item[:2] for item in argv[3:]}
                    self.assertFalse(flags & {"-t", "-n", "-e", "-f", "-i"})

    def test_only_the_blink_checks_sample_until_both_phases(self):
        _args, full = cases_for("Ubertooth util modes", "comprehensive")
        blink = util_cases.led_blink_sampler_argv(SERIAL)
        used = {}
        for case in full:
            for step in case.steps:
                if step.resolve_argv({"original channel": "2441"})[0] in ("sh", sys.executable):
                    used[(case.case_id, step.step_id)] = (
                        step.resolve_argv({}) == blink)
                    self.assertGreater(step.timeout_s, util_cases.LED_BLINK_DEADLINE_S
                                       + ledblink.GRACE_S + 5)
        self.assertEqual({
            ("identify-blinks-until-stop", "sample-mode"),
            ("identify-replaces-led-specan", "sample-mode"),
            ("led-specan-replaces-identify", "sample-prior-mode"),
            ("identify-after-serial-query", "sample-mode"),
        }, {key for key, is_blink in used.items() if is_blink})

    def test_the_blink_sampler_stops_at_both_phases(self):
        device = ModeUbertooth(crc="1")
        summary, _events = run_test("Ubertooth util modes", device)
        self.assertTrue(summary.passed, summary.failure_reason)
        # The fake alternates every read: on, off, done.
        self.assertEqual([2], device.blink_samplers)

    def test_every_q_threshold_is_in_the_accepted_range(self):
        _args, full = cases_for("Ubertooth util modes", "comprehensive")
        for case in full:
            for step in case.steps:
                for item in step.resolve_argv({"original channel": "2441"}):
                    if re.fullmatch(r"-q\d+", item):
                        self.assertTrue(20 <= int(item[2:]) <= 120, item)

    def test_led_checks(self):
        on = "USR LED status: 1\nRX LED status : 1\nTX LED status : 1\n"
        off = on.replace("1", "0")
        mixed = "USR LED status: 1\nRX LED status : 0\nTX LED status : 1\n"
        n = util_cases.LED_SAMPLES
        self.assertEqual("", util_cases.blink_check((on + off) * (n // 2)))
        self.assertEqual("", util_cases.blink_check(on + mixed * (n - 2) + off))
        self.assertIn("did not blink", util_cases.blink_check(on * n))
        self.assertIn("did not blink", util_cases.blink_check(mixed * n))
        # The blink check takes however many samples the sampler needed.
        self.assertEqual("", util_cases.blink_check(on + off))
        self.assertIn("did not blink in 3 samples", util_cases.blink_check(on * 3))
        self.assertIn("did not blink in 1 sample (", util_cases.blink_check(on))
        self.assertIn("no LED samples", util_cases.blink_check(""))
        lit = util_cases.steady_check(util_cases.ALL_ON, "all on")
        self.assertEqual("", lit(on * n))
        self.assertIn("not all on", lit(on * (n - 1) + mixed))

    def test_smoke_passes_on_fork_main(self):
        device = ModeUbertooth(crc="1")
        summary, events = run_test("Ubertooth util modes", device)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(["-I", "-q120"], device.mode_options)
        # Cleanup restored the fake's originals.
        self.assertEqual("2441", device.state["channel"])
        self.assertEqual("-90", device.state["squelch"])
        self.assertEqual(("1", "0", "0"), tuple(device.state[k] for k in ("usr", "rx", "tx")))
        self.assertEqual("1", device.state["crc"])
        self.assertIsNone(device.mode)

    def test_mode_lines_on_stdout_before_fcadf34_fail(self):
        # Fork #326: the mode lines leave stdout, and -I says the blink once.
        summary, events = run_test("Ubertooth util modes", ModeUbertooth(
            crc="1", before_fcadf34=True))
        self.assertFalse(summary.passed)
        failed = {e.details["case_id"].rsplit("-Ubertooth util modes-", 1)[-1]
                  for e in events
                  if e.event_type == "check_finished" and e.status == "failed"}
        self.assertEqual({"identify-blinks-until-stop", "led-specan-120-lights-leds"}, failed)
        self.assertIn("progress and diagnostics go to stderr", summary.failure_reason)

    def test_comprehensive_passes_on_fork_main(self):
        device = ModeUbertooth(crc="1")
        summary, events = run_test("Ubertooth util modes", device, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual([], failed_cases(events))
        self.assertEqual(
            ["-I", "-q120", "-q20", "-q", "-q120", "-I", "-I", "-q120", "-I"],
            device.mode_options,
        )
        self.assertIsNone(device.mode)

    def test_a_q_stop_without_the_idle_reset_fails_every_q_case(self):
        # SqueakyFloors/ubertooth#191 (fork builds before aa4f620).
        device = ModeUbertooth(crc="1", specan_stop_resets=False)
        summary, events = run_test("Ubertooth util modes", device, coverage="comprehensive")
        self.assertFalse(summary.passed)
        # -q20 lights nothing, so its stop leaves the LEDs off; the squelch
        # and channel checks still catch it.
        self.assertEqual(sorted(Q_CASES), ends(failed_cases(events), *Q_CASES))
        self.assertEqual(len(Q_CASES), len(failed_cases(events)))

    def test_led_specan_that_lights_nothing_fails(self):
        # A threshold that never trips (as the #189 int8 wrap did for -q183
        # and up on fork builds before aa4f620).
        device = ModeUbertooth()
        device._lit = lambda: False
        self.assert_smoke_fails(device, "led-specan-120-lights-leds", "LEDs not all on")

    def assert_smoke_fails(self, device, case, reason):
        summary, events = run_test("Ubertooth util modes", device)
        self.assertFalse(summary.passed)
        failed = failed_cases(events)
        self.assertEqual(1, len(failed), failed)
        self.assertTrue(failed[0].endswith(case), failed)
        self.assertIn(reason, summary.failure_reason)
        self.assertIsNone(device.mode)  # cleanup stopped the mode
        return events

    def test_identify_that_does_not_blink_fails(self):
        self.assert_smoke_fails(ModeUbertooth(blink=False), "identify-blinks-until-stop",
                                "LEDs did not blink")

    def test_a_mode_that_survives_stop_fails(self):
        device = ModeUbertooth(stop_ends_mode=False)
        summary, events = run_test("Ubertooth util modes", device)
        self.assertFalse(summary.passed)
        self.assertIn("LEDs not off in every sample", summary.failure_reason)
        # The first failing case stops the test: its cleanup -S cannot end
        # the mode, so the device is not verified restored.
        self.assertTrue(failed_cases(events)[0].endswith("identify-blinks-until-stop"))

    def test_a_mode_that_moves_the_channel_fails(self):
        device = ModeUbertooth(mode_resets_channel=True)
        summary, events = run_test("Ubertooth util modes", device)
        self.assertFalse(summary.passed)
        self.assertIn("Channel kept while the mode runs", summary.failure_reason)
        self.assertEqual(2, len(failed_cases(events)))  # -I and -q120

    def test_a_replacement_that_skips_the_reset_fails(self):
        device = ModeUbertooth(crc="1", replace_resets=False)
        summary, events = run_test("Ubertooth util modes", device, coverage="comprehensive")
        self.assertFalse(summary.passed)
        replaced = ("identify-replaces-led-specan", "led-specan-replaces-identify")
        self.assertEqual(sorted(replaced), ends(failed_cases(events), *replaced))
        self.assertEqual(2, len(failed_cases(events)))
        self.assertIn("Channel reset by ending the prior mode", summary.failure_reason)

    def test_old_range_wording_fails(self):
        # "invalid range test result" (fork builds before 7180f74, #190).
        self.assert_smoke_fails(ModeUbertooth(range_text="invalid range test result"),
                                "range-result-none-after-reset",
                                "Range test result with no range test run")

    def test_range_result_values_without_a_test_fail(self):
        self.assert_smoke_fails(ModeUbertooth(range_values=True),
                                "range-result-none-after-reset",
                                "Range test result with no range test run")

    def test_range_result_case_resets_first(self):
        _args, smoke = cases_for("Ubertooth util modes", "smoke")
        case = next(c for c in smoke if c.case_id == "range-result-none-after-reset")
        ids = [step.step_id for step in case.steps]
        self.assertLess(ids.index("reset"), ids.index("range-result"))
        device = ModeUbertooth(crc="1")
        _summary, events = run_test("Ubertooth util modes", device)
        reset = steps(events, step_id="reset")
        self.assertEqual("passed", reset[0].status)

    # LED read stalls (SqueakyFloors/ubertooth#213)

    def test_a_single_stalled_read_is_retried_and_recorded(self):
        device = ModeUbertooth(crc="1", read_stalls=lambda attempt: attempt == 15)
        summary, events = run_test("Ubertooth util modes", device)
        self.assertTrue(summary.passed, summary.failure_reason)
        retried = [e for e in events
                   if e.event_type == "command_output" and e.details["stream"] == "stderr"
                   and re.search(util_cases.LED_RETRY_LINE, e.details["text"])]
        self.assertEqual(1, len(retried), "the retry is not in the run record")

    def test_frequent_single_stalls_still_pass(self):
        # One read in three fails, never two in a row: every retry succeeds.
        device = ModeUbertooth(crc="1", read_stalls=lambda attempt: attempt % 3 == 0)
        summary, _events = run_test("Ubertooth util modes", device)
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_a_read_that_keeps_failing_fails_the_case(self):
        device = ModeUbertooth(read_stalls=lambda attempt: attempt in (3, 4))
        self.assert_smoke_fails(device, "identify-blinks-until-stop", "command exited 1")

    def test_reads_that_always_fail_fail_the_case(self):
        device = ModeUbertooth(read_stalls=lambda attempt: True)
        summary, events = run_test("Ubertooth util modes", device)
        self.assertFalse(summary.passed)
        self.assertIn("command exited 1", summary.failure_reason)


class LedSamplerShellTests(unittest.TestCase):
    """The real LED_SAMPLER under sh, with a stub ubertooth-util."""

    def run_sampler(self, outcomes, count=3):
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "n"
            state.write_text("0")
            stub = Path(root) / "util"
            # outcomes[i]: exit code of the i-th -d call (0 prints a report).
            stub.write_text(
                "#!/bin/sh\n"
                f"n=$(cat {state}); echo $((n + 1)) > {state}\n"
                f"set -- {' '.join(map(str, outcomes))}\n"
                'shift "$n"; code=${1:-0}\n'
                'echo "USR LED status: 1"; echo "RX LED status : 1"\n'
                'if [ "$code" -ne 0 ]; then echo "TX LED read failed" >&2; exit "$code"; fi\n'
                'echo "TX LED status : 1"\n'
            )
            stub.chmod(0o755)
            result = subprocess.run(
                ["sh", "-c", util_cases.LED_SAMPLER, str(stub), SERIAL, str(count), "0"],
                capture_output=True, text=True, timeout=10,
            )
        return result

    def test_clean_reads(self):
        result = self.run_sampler([0, 0, 0])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1/1/1"] * 3, util_cases.led_samples(result.stdout))
        self.assertEqual("", result.stderr)

    def test_one_failed_read_is_retried_its_partial_output_dropped(self):
        result = self.run_sampler([0, 1, 0, 0])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1/1/1"] * 3, util_cases.led_samples(result.stdout))
        self.assertEqual(9, result.stdout.count("LED status"))
        self.assertRegex(result.stderr, r"LED sample 1: -d exited 1, retrying")

    def test_a_failed_retry_ends_the_sampler(self):
        result = self.run_sampler([0, 1, 1, 0, 0])
        self.assertEqual(1, result.returncode)
        self.assertEqual(["1/1/1"], util_cases.led_samples(result.stdout))


class LedBlinkSamplerTests(unittest.TestCase):
    """ledblink.py: in-process with a fake clock, and as a command with a
    stub ubertooth-util."""

    def board(self, half_period_s=0.167, read_s=0.284, offset_s=0.0):
        """A fake clock and an identify board: whole on for the first half of
        each period, whole off for the second. A read takes ``read_s`` and
        samples the LEDs at its end."""
        now = [0.0]
        reads = []

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        def read(argv, timeout_s):
            if timeout_s < read_s:
                now[0] += max(timeout_s, 0.0)
                return None, ""
            now[0] += read_s
            reads.append(now[0])
            on = int((now[0] + offset_s) // half_period_s) % 2 == 0
            bit = "1" if on else "0"
            return 0, (f"USR LED status: {bit}\nRX LED status : {bit}\n"
                       f"TX LED status : {bit}\n")
        return clock, sleep, read, reads, now

    def run_fake(self, *, jitter_s, seed=0, offset_s=0.0, deadline_s=10.0,
                 max_samples=60, **board):
        import io
        clock, sleep, read, reads, now = self.board(offset_s=offset_s, **board)
        out, err = io.StringIO(), io.StringIO()
        code = ledblink.sample(
            "ubertooth-util", SERIAL, max_samples=max_samples, deadline_s=deadline_s,
            interval_s=0.05, jitter_s=jitter_s, rng=random.Random(seed),
            clock=clock, sleep=sleep, read=read, out=out, err=err)
        return code, out.getvalue(), err.getvalue(), now[0]

    def test_a_fixed_pause_aliases_to_one_phase(self):
        # A 0.05 s pause plus a read whose sample period is the blink period
        # (0.334 s): every sample lands on one phase. This is the stub board
        # the jittered sampler must handle.
        code, out, err, _end = self.run_fake(jitter_s=0.0, read_s=0.284,
                                             half_period_s=0.167, offset_s=0.01)
        self.assertEqual(0, code)
        self.assertIn("did not blink", util_cases.blink_check(out))
        self.assertIn("stopped at the deadline", err)

    def test_jitter_reaches_both_phases_on_the_aliasing_board(self):
        for seed in range(300):
            for offset in (0.01, 0.1, 0.2, 0.3):
                with self.subTest(seed=seed, offset=offset):
                    code, out, err, end = self.run_fake(
                        jitter_s=ledblink.BLINK_PERIOD_S, seed=seed, offset_s=offset,
                        read_s=0.284, half_period_s=0.167)
                    self.assertEqual("", util_cases.blink_check(out), err)
                    self.assertIn("stopped at both phases", err)
                    self.assertLess(end, 10.0)

    def test_the_deadline_covers_a_read_in_flight(self):
        # Reads of 3 s (three 1 s control timeouts): a read that would end
        # past the deadline is stopped at it, not waited for.
        code, out, err, end = self.run_fake(jitter_s=0.0, read_s=3.0, deadline_s=10.0)
        self.assertEqual(0, code)
        self.assertLessEqual(end, 10.0)
        self.assertIn("stopped at the deadline (a read was stopped)", err)

    def test_one_sample_is_singular(self):
        code, _out, err, _end = self.run_fake(jitter_s=0.0, max_samples=1)
        self.assertIn("LED blink sampling: 1 sample in ", err)
        self.assertIn("stopped at the sample bound", err)

    # As a command, with a stub ubertooth-util.

    def run_sampler(self, reports, *, count=20, deadline=10, delay="0"):
        """reports[i]: the i-th -d call's LEDs ("1/1/1", "0/1/0"), or an
        exit code (int) after a partial report; past the end, the last one.
        The stub records each SIGTERM it gets in a file."""
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "n"
            state.write_text("0")
            terms = Path(root) / "terms"
            stub = Path(root) / "util"
            words = " ".join(str(r) for r in reports)
            stub.write_text(
                "#!/bin/sh\n"
                f"sleep {delay} >/dev/null 2>&1 & s=$!\n"
                f"trap 'echo term >> {terms}; kill $s; exit 143' TERM\n"
                "wait $s\n"
                f"n=$(cat {state}); echo $((n + 1)) > {state}\n"
                f"set -- {words}\n"
                'while [ "$n" -gt 0 ] && [ "$#" -gt 1 ]; do shift; n=$((n - 1)); done\n'
                'case "$1" in */*) ;; *) echo "USR LED status: 1"; exit "$1";; esac\n'
                'IFS=/; set -- $1\n'
                'echo "USR LED status: $1"; echo "RX LED status : $2"; '
                'echo "TX LED status : $3"\n'
            )
            stub.chmod(0o755)
            start = time.monotonic()
            result = subprocess.run(
                [sys.executable, ledblink.__file__, str(stub), SERIAL,
                 "--max-samples", str(count), "--interval", "0", "--jitter", "0.01",
                 "--deadline", str(deadline)],
                capture_output=True, text=True, timeout=30,
            )
            result.elapsed = time.monotonic() - start
            result.calls = int(state.read_text())
            result.terms = terms.read_text().count("term") if terms.exists() else 0
        return result

    def test_stops_once_both_phases_appeared(self):
        result = self.run_sampler(["1/1/1", "1/0/1", "1/1/1", "0/0/0", "1/1/1"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1/1/1", "1/0/1", "1/1/1", "0/0/0"],
                         util_cases.led_samples(result.stdout))
        self.assertEqual("", util_cases.blink_check(result.stdout))
        self.assertRegex(result.stderr, r"LED blink sampling: seed \d+")
        self.assertIn("4 samples in ", result.stderr)

    def test_mixed_samples_are_neither_phase(self):
        result = self.run_sampler(["1/1/0", "0/0/1", "0/1/1"], count=6)
        self.assertEqual(6, len(util_cases.led_samples(result.stdout)))
        self.assertIn("did not blink in 6 samples", util_cases.blink_check(result.stdout))

    def test_a_steady_board_ends_at_the_sample_bound(self):
        result = self.run_sampler(["1/1/1"], count=7)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1/1/1"] * 7, util_cases.led_samples(result.stdout))
        self.assertIn("stopped at the sample bound", result.stderr)

    def test_a_read_in_flight_is_stopped_at_the_deadline(self):
        # One read that would take 20 s: SIGTERM at the deadline, so the
        # sampler ends within the deadline plus the grace. Three seconds, not
        # one: the stub has to reach its own trap before the signal arrives,
        # and on a busy machine starting it took longer than that (its
        # SIGTERM went unrecorded in a run under the parallel suite, #184).
        deadline = 3
        result = self.run_sampler(["0/0/0"], count=50, deadline=deadline,
                                  delay="20")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, result.terms)
        self.assertLess(result.elapsed, deadline + ledblink.GRACE_S + 1)
        self.assertIn("stopped at the deadline (a read was stopped)", result.stderr)
        self.assertEqual([], util_cases.led_samples(result.stdout))

    def test_one_failed_read_is_retried(self):
        result = self.run_sampler(["1/1/1", 1, "0/0/0"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["1/1/1", "0/0/0"], util_cases.led_samples(result.stdout))
        self.assertRegex(result.stderr, util_cases.LED_RETRY_LINE)
        self.assertIn("LED sample 1: -d exited 1, retrying", result.stderr)

    def test_a_failed_retry_ends_the_sampler(self):
        result = self.run_sampler(["1/1/1", 1, 1, "0/0/0"])
        self.assertEqual(1, result.returncode)
        self.assertEqual(["1/1/1"], util_cases.led_samples(result.stdout))
        self.assertIn("stopped at a failed retry (exit 1)", result.stderr)


# ---------------------------------------------------------------------------
# Fake: several boards on one host, with -U all, no -U, and -U<index>


# The label and report order come from the module under test, so the fake
# cannot drift into a protocol the checks no longer expect.
LABELS = dict(util_cases.LISTING_LABELS)
REPORT_ORDER = [query for query, _label in util_cases.LISTING_LABELS]
IDENTITY = set(LABELS)


class MultiBench:
    """Boards in enumeration order, routed like libubertooth (fork main).

    - ``past_end``: what -U<index> past the last board opens: None (refused
      with "no Ubertooth at index", fork main since 0c7e9c0) or "first"
      (#188, earlier builds: the first USB device, here board 0).
    - ``unregistered``: extra serials on the bus that the registry lacks.
    - ``index_map``: board opened for each -U<index> (default identity).
    - ``listing_drops_last``: the listing omits the last board.
    - ``unreadable``: indices the listing cannot open, which get
      ``unavailable, <reason>`` where each query's value would be.
    """

    UNAVAILABLE = "unavailable, in use by another program"

    def __init__(self, serials, *, past_end=None, unregistered=(), index_map=None,
                 listing_drops_last=False, unreadable=()):
        self.order = list(serials) + list(unregistered)
        self.boards = {s: FakeUbertooth(serial=s, crc="1") for s in self.order}
        self.past_end = past_end
        self.index_map = index_map
        self.listing_drops_last = listing_drops_last
        self.unreadable = set(unreadable)
        self.commands = []

    @staticmethod
    def _result(command, code, stdout, stderr=""):
        return CommandResult(command, code, stdout, stderr, 0.01, False)

    def _expand(self, options):
        expanded = []
        for option in options:
            if option.startswith("-") and len(option) > 2 and set(option[1:]) <= set("vVspb"):
                expanded += [f"-{c}" for c in option[1:]]
            else:
                expanded.append(option)
        return expanded

    def _report(self, serial, query):
        command = ("ubertooth-util", "-U", serial, query)
        return self.boards[serial].run(command, timeout_s=5).stdout.splitlines()

    def _listing(self, queries):
        requested = [q for q in REPORT_ORDER if q in queries]
        listed = self.order[:-1] if self.listing_drops_last else self.order
        lines = []
        for index, serial in enumerate(listed):
            if index in self.unreadable:
                # One line per query, so a reader filtering for one of them
                # still sees this board.
                for query in requested:
                    label = LABELS[query]
                    prefix = f"{label} ({index})" if label else f"{index}"
                    lines.append(f"{prefix}: {self.UNAVAILABLE}")
                continue
            for query in requested:
                for line in self._report(serial, query):
                    # The report's label moves into the listing's, which
                    # carries the index. Compile info has no label of its
                    # own, and neither does a line that does not start with
                    # one, so those keep the bare index prefix.
                    label = LABELS[query]
                    if label and line.startswith(f"{label}: "):
                        value = line.removeprefix(f"{label}: ")
                        lines.append(f"{label} ({index}): {value}")
                    else:
                        lines.append(f"{index}: {line}")
        return "".join(line + "\n" for line in lines)

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        self.commands.append(command)
        if command[0] != "ubertooth-util":
            return self.boards[command[2]].run(argv, timeout_s=timeout_s,
                                               cancellation=cancellation,
                                               on_output=on_output)
        options = list(command[1:])
        if options[:2] == ["-U", "all"]:
            queries = self._expand(options[2:])
            if not queries or set(queries) - IDENTITY:
                return self._result(command, 1, "", "error: -U all only supports\n")
            return self._result(command, 0, self._listing(queries))
        if options and re.fullmatch(r"-U\d+", options[0]):
            index = int(options[0][2:])
            if self.index_map is not None:
                index = self.index_map[index]
            if index >= len(self.order):
                if self.past_end is None:
                    return self._result(
                        command, 1, "",
                        f"error: no Ubertooth at index {index} ({len(self.order)} attached)\n")
                index = 0
            serial = self.order[index]
            rest = tuple(options[1:])
            if not rest:
                return self._result(command, 0, "")
            return self.boards[serial].run(("ubertooth-util", "-U", serial) + rest,
                                           timeout_s=timeout_s)
        if "-U" in options:
            return self.boards[command[2]].run(argv, timeout_s=timeout_s,
                                               cancellation=cancellation,
                                               on_output=on_output)
        if options == ["-N"]:
            return self._result(command, 0, f"{len(self.order)}\n")
        queries = self._expand(options)
        if queries and not set(queries) - IDENTITY:
            return self._result(command, 0, self._listing(queries))
        devices = "".join(
            f"  Device {i}: Serial No: {s}\n" for i, s in enumerate(self.order)
        )
        # Neither refusal is followed by another line: the retired
        # "could not open Ubertooth device" was the only one, and it named no
        # cause of its own (fork 753269b).
        return self._result(
            command, 1, "",
            "multiple Ubertooth devices found! Use '-U' to specify device number\n"
            + devices,
        )


def multi_run(bench, coverage="smoke", guards=(B,)):
    return run_guarded(guarded_invocation("Multi-board selection", coverage, guards), bench)


class MultiBoardCaseTests(unittest.TestCase):

    def test_a_single_board_host_skips_instead_of_failing_the_run(self):
        definition = builtin_catalog().get("Multi-board selection")
        arguments = definition.normalize_arguments(
            {"hosts": ["Host_Linux"], "Ubertooth": "default", "coverage": "smoke"}
        )
        cases = definition.command_cases(arguments, {"Ubertooth": "UT_0"},
                                         {"Ubertooth": {"serial": A}})
        self.assertEqual(["needs-two-boards"], [c.case_id for c in cases])
        summary, _events, _commands = multi_run(MultiBench([A]), guards=())
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(1, summary.skipped_cases)

    def test_case_sets_and_index_steps(self):
        invocation = guarded_invocation("Multi-board selection", "smoke", (B, C))
        definition = builtin_catalog().get("Multi-board selection")
        smoke = definition.command_cases(invocation.arguments, invocation.resources,
                                         invocation.resource_identities)
        self.assertEqual(
            ["all-lists-every-registered-serial",
             "no-selector-lists-every-registered-serial", "index-selects-listed-board"],
            [case.case_id for case in smoke],
        )
        index = smoke[2]
        argv = [step.resolve_argv({}) for step in index.steps if step.step_id.startswith("select")]
        self.assertEqual([("ubertooth-util", f"-U{i}", "-s") for i in range(3)], argv)
        full_invocation = guarded_invocation("Multi-board selection", "comprehensive", (B,))
        full = definition.command_cases(full_invocation.arguments,
                                        full_invocation.resources,
                                        full_invocation.resource_identities)
        self.assertEqual(7, len(full))
        past = next(c for c in full if c.case_id == "index-past-end-refused")
        # No option: a buggy open must not send a request to another device.
        self.assertEqual(("ubertooth-util", "-U2"), past.steps[0].resolve_argv({}))

    def test_listing_checks(self):
        def listed(*serials):
            return "".join(f"Serial No ({i}): {s}\n"
                           for i, s in enumerate(serials))

        check = util_cases.serial_listing_check((A, B))
        self.assertEqual("", check(listed(B, A)))
        self.assertIn("not exactly", check(listed(A)))
        self.assertIn("unregistered", check(listed(A, B, C)))
        self.assertIn("not 0..1 in order",
                      check(f"Serial No (1): {A}\nSerial No (0): {B}\n"))
        self.assertIn("is not", check(f"Serial No: {A}\n"))
        self.assertIn("not exactly", check(listed(A, A)))
        # The old unlabelled listing is a regression, not an accepted form:
        # only a query the tool has no label for keeps the bare prefix.
        self.assertIn("not 'Serial No'", check(f"0: {B}\n1: {A}\n"))
        self.assertIn("not 'Serial No'",
                      check(f"Part ID (0): {B}\nPart ID (1): {A}\n"))
        # A board the tool could not read is named, not counted as a serial,
        # whether the board never opened or the query itself failed on it.
        self.assertIn(
            "board 1 could not be opened",
            check(f"Serial No (0): {A}\n"
                  "Serial No (1): unavailable, in use by another program\n"),
        )
        self.assertIn(
            "board 1 query read failed",
            check(f"Serial No (0): {A}\nSerial No (1): read failed\n"),
        )
        # The unlabelled -V form names the query in the value instead.
        identity = util_cases.identity_listing_check((A,))
        self.assertIn("board 0 query read failed",
                      identity("0: compile info read failed\n"))
        # An unlabelled board ID line is a regression, even though -V's is not.
        block = (f"0: 1\n"
                 f"Firmware version (0): bluetooth_rxtx git-x (API:1.37)\n"
                 f"0: bluetooth_rxtx git-x (API:1.37)\n"
                 f"Part ID (0): 1234abcd\n"
                 f"Serial No (0): {A}\n")
        self.assertIn("not 'Board ID Number'", identity(block))

    def test_smoke_passes_on_fork_main_and_guards_hold(self):
        bench = MultiBench((A, B))
        summary, events, _report = multi_run(bench)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertTrue(guard_checks(events))
        self.assertTrue(all(e.status == "passed" for e in guard_checks(events)))
        self.assertIn(("ubertooth-util", "-U1", "-s"), bench.commands)

    def test_three_boards_with_a_reversed_listing_still_pass(self):
        # Enumeration order is not registry order.
        bench = MultiBench((C, A, B))
        summary, _events, _report = multi_run(bench, guards=(B, C))
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_comprehensive_passes_on_fork_main(self):
        summary, _events, _report = multi_run(MultiBench((A, B)), "comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_an_index_past_the_end_that_opens_a_board_fails(self):
        # SqueakyFloors/ubertooth#188 (fork builds before 0c7e9c0).
        summary, events, _report = multi_run(MultiBench((A, B), past_end="first"),
                                             "comprehensive")
        self.assertFalse(summary.passed)
        failed = failed_cases(events)
        self.assertEqual(1, len(failed), failed)
        self.assertTrue(failed[0].endswith("index-past-end-refused"))
        self.assertIn("command exited 0", summary.failure_reason)

    def assert_smoke_fails(self, bench, cases, reason):
        summary, events, _report = multi_run(bench)
        self.assertFalse(summary.passed)
        self.assertEqual(sorted(cases), ends(failed_cases(events), *cases))
        self.assertIn(reason, summary.failure_reason)

    def test_an_unregistered_board_fails_the_listings(self):
        self.assert_smoke_fails(
            MultiBench((A, B), unregistered=(C,)),
            ("all-lists-every-registered-serial",
             "no-selector-lists-every-registered-serial", "index-selects-listed-board"),
            "unregistered",
        )

    def test_a_listing_that_misses_a_board_fails(self):
        self.assert_smoke_fails(
            MultiBench((A, B), listing_drops_last=True),
            ("all-lists-every-registered-serial",
             "no-selector-lists-every-registered-serial", "index-selects-listed-board"),
            "missing",
        )

    def test_an_index_that_opens_another_board_fails(self):
        self.assert_smoke_fails(
            MultiBench((A, B), index_map={0: 1, 1: 0}),
            ("index-selects-listed-board",),
            "expected serial selected by -U0",
        )

    def test_a_board_the_listing_cannot_open_fails_and_names_it(self):
        # The run has to say which board went unread, not report its stand-in
        # value as a serial.
        self.assert_smoke_fails(
            MultiBench((A, B), unreadable={1}),
            ("all-lists-every-registered-serial",
             "no-selector-lists-every-registered-serial", "index-selects-listed-board"),
            "board 1 could not be opened",
        )


class PlanTests(unittest.TestCase):

    def test_plans_select_the_profile_of_each_suite(self):
        from pathlib import Path
        from ubertooth_harness.plan import load_plan

        root = Path(__file__).resolve().parents[1] / "testplans"
        for filename, suite, profile in (("fast.yaml", "fast", "smoke"),
                                         ("release.yaml", "release", "comprehensive")):
            selected = {
                step.test_name: step.arguments["coverage"]
                for step in load_plan(root / filename).steps(suite)
                if step.test_name in ("Ubertooth util modes", "Multi-board selection")
            }
            with self.subTest(filename):
                self.assertEqual(
                    {"Ubertooth util modes": profile, "Multi-board selection": profile},
                    selected,
                )


if __name__ == "__main__":
    unittest.main()
