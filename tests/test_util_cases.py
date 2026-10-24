"""Ubertooth util modes and Multi-board selection against fakes of the fork."""

import re
import unittest

from ubertooth_harness import util_cases
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
    """FakeUbertooth plus -I, -q, -m, and the LED sampler (fork main b7437da).

    Knobs model each fork behavior and the regressions the cases must catch:

    - ``int8_threshold``: the firmware's int8 LED-specan threshold
      (SqueakyFloors/ubertooth#189): -q183..-q225 and bare -q light nothing.
    - ``range_text``: what -m prints with no result ("invalid range test
      result" on main, #190).
    - ``blink``: identify toggles the LEDs; False leaves them steady.
    - ``stop_ends_mode``: -S ends identify/LED specan; False keeps it running.
    - ``mode_resets_channel``: starting a mode resets the channel at once.
    - ``range_values``: -m prints result values although no test ran.
    - ``specan_stop_resets``: stopping -q runs the idle reset; False on main
      (#191): the LEDs stay as the mode left them, channel and squelch kept.
    """

    def __init__(self, *args, int8_threshold=True, range_text="invalid range test result",
                 blink=True, stop_ends_mode=True, mode_resets_channel=False,
                 range_values=False, specan_stop_resets=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.int8_threshold = int8_threshold
        self.range_text = range_text
        self.blink = blink
        self.stop_ends_mode = stop_ends_mode
        self.mode_resets_channel = mode_resets_channel
        self.range_values = range_values
        self.specan_stop_resets = specan_stop_resets
        self.threshold = None
        self.mode_options = []

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[:3] == ("sh", "-c", util_cases.LED_SAMPLER):
            self.commands.append(command)
            assert command[3:5] == ("ubertooth-util", self.serial), command
            count = int(command[5])
            return self._result(command, 0, self._samples(count), "", on_output)
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)

    def _samples(self, count):
        lines = []
        for index in range(count):
            if self.mode == "identify":
                on = index % 2 == 0 if self.blink else True
                usr = rx = tx = "1" if on else "0"
            elif self.mode == "led-specan":
                usr = rx = tx = "1" if self._lit() else "0"
            else:
                usr, rx, tx = self.state["usr"], self.state["rx"], self.state["tx"]
            lines.append(f"USR LED status: {usr}\nRX LED status : {rx}\nTX LED status : {tx}\n")
        return "".join(lines)

    def _stop_mode(self):
        if self.mode == "led-specan" and not self.specan_stop_resets:
            lit = "1" if self._lit() else "0"
            self.state.update(usr=lit, rx=lit, tx=lit)
            self.mode = None
            return
        super()._stop_mode()

    def _lit(self):
        raw = 54 - self.threshold
        if self.int8_threshold:
            raw = (raw + 128) % 256 - 128
        # Ambient noise reads about -41 raw (-95 dBm); -128 is never read.
        return raw < -41

    def _util(self, command, options, on_output):
        modes = [o for o in options if o == "-I" or o.startswith("-q")]
        if "-S" in options and not self.stop_ends_mode and self.mode in (
            "identify", "led-specan"
        ):
            return self._result(command, 0, f"Stopping ubertooth device {self.serial}\n",
                                "", on_output)
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
        rest = tuple(o for o in options if o not in modes)
        prefix = super()._util(command, rest, on_output=None).stdout if rest else ""
        self.mode_options.append(mode)
        self._stop_mode()  # each mode first leaves any prior mode
        if self.mode_resets_channel:
            self.state["channel"] = "2441"
        if mode == "-I":
            self.mode = "identify"
            out = (f"Flashing LEDs on ubertooth device {self.serial}\n"
                   "The board is blinking; stop it with ubertooth-util -S or any "
                   "mode option\n")
        else:
            self.threshold = int(mode[2:]) if len(mode) > 2 else 225
            self.mode = "led-specan"
            out = f"Entering LED specan mode (RSSI {self.threshold})\n"
        return self._result(command, 0, prefix + out, "", on_output)


STRICT_MODE_CASES = (
    "led-specan-stop-runs-idle-reset",
    "led-specan-default-lights-leds",
    "led-specan-225-lights-leds",
    "range-result-none-says-no-result",
)
FIXED = {
    "int8_threshold": False, "specan_stop_resets": True,
    "range_text": "no range test result (none since start)",
}


class UtilModeCaseTests(unittest.TestCase):

    def test_smoke_and_comprehensive_case_sets(self):
        _args, smoke = cases_for("Ubertooth util modes", "smoke")
        _args, full = cases_for("Ubertooth util modes", "comprehensive")
        self.assertEqual(
            ["identify-blinks-until-stop", "led-specan-182-lights-leds",
             "range-result-none-after-reset"],
            [case.case_id for case in smoke],
        )
        self.assertEqual(11, len(full))
        self.assertTrue(all(isinstance(case, StateCase) for case in full))
        ids = {case.case_id for case in full}
        self.assertTrue(set(STRICT_MODE_CASES) <= ids)

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
                    if argv[0] == "sh":
                        self.assertEqual(util_cases.led_sampler_argv(SERIAL), argv)
                        continue
                    self.assertEqual(["-U", SERIAL], list(argv[1:3]))
                    flags = {item[:2] for item in argv[3:]}
                    self.assertFalse(flags & {"-t", "-n", "-e", "-f", "-i"})

    def test_led_checks(self):
        on = "USR LED status: 1\nRX LED status : 1\nTX LED status : 1\n"
        off = on.replace("1", "0")
        mixed = "USR LED status: 1\nRX LED status : 0\nTX LED status : 1\n"
        n = util_cases.LED_SAMPLES
        self.assertEqual("", util_cases.blink_check((on + off) * (n // 2)))
        self.assertEqual("", util_cases.blink_check(on + mixed * (n - 2) + off))
        self.assertIn("did not blink", util_cases.blink_check(on * n))
        self.assertIn("did not blink", util_cases.blink_check(mixed * n))
        self.assertIn("expected 12 LED samples, got 2", util_cases.blink_check(on + off))
        lit = util_cases.steady_check(util_cases.ALL_ON, "all on")
        self.assertEqual("", lit(on * n))
        self.assertIn("not all on", lit(on * (n - 1) + mixed))

    def test_smoke_passes_on_fork_main(self):
        device = ModeUbertooth(crc="1")
        summary, events = run_test("Ubertooth util modes", device)
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(["-I", "-q182"], device.mode_options)
        # Cleanup restored the fake's originals.
        self.assertEqual("2441", device.state["channel"])
        self.assertEqual("-90", device.state["squelch"])
        self.assertEqual(("1", "0", "0"), tuple(device.state[k] for k in ("usr", "rx", "tx")))
        self.assertEqual("1", device.state["crc"])
        self.assertIsNone(device.mode)

    def test_comprehensive_fails_only_the_strict_cases_on_fork_main(self):
        device = ModeUbertooth(crc="1")
        summary, events = run_test("Ubertooth util modes", device, coverage="comprehensive")
        self.assertFalse(summary.passed)
        self.assertEqual(sorted(STRICT_MODE_CASES), ends(failed_cases(events), *STRICT_MODE_CASES))
        self.assertEqual(len(STRICT_MODE_CASES), len(failed_cases(events)))
        self.assertIsNone(device.mode)

    def test_comprehensive_passes_once_the_fork_issues_are_fixed(self):
        device = ModeUbertooth(crc="1", **FIXED)
        summary, _events = run_test("Ubertooth util modes", device, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(
            ["-I", "-q182", "-q1", "-q182", "-q", "-q225", "-q182", "-I", "-I",
             "-q182", "-I"],
            device.mode_options,
        )

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
        self.assertEqual(2, len(failed_cases(events)))  # -I and -q182

    def test_led_specan_that_lights_nothing_fails(self):
        # A threshold that never trips (the #189 wrap applied to every value).
        device = ModeUbertooth()
        device._lit = lambda: False
        self.assert_smoke_fails(device, "led-specan-182-lights-leds", "LEDs not all on")

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


# ---------------------------------------------------------------------------
# Fake: several boards on one host, with -U all, no -U, and -U<index>


LABELS = {"-s": "Serial No: ", "-v": "Firmware version: ", "-p": "Part ID: ",
          "-b": "Board ID Number: ", "-V": ""}
IDENTITY = set(LABELS)


class MultiBench:
    """Boards in enumeration order, routed like libubertooth (fork main).

    - ``past_end``: what -U<index> past the last board opens: "first" (fork
      main, #188: the first USB device, here board 0) or None (refused).
    - ``unregistered``: extra serials on the bus that the registry lacks.
    - ``index_map``: board opened for each -U<index> (default identity).
    - ``listing_drops_last``: the listing omits the last board.
    """

    def __init__(self, serials, *, past_end="first", unregistered=(), index_map=None,
                 listing_drops_last=False):
        self.order = list(serials) + list(unregistered)
        self.boards = {s: FakeUbertooth(serial=s, crc="1") for s in self.order}
        self.past_end = past_end
        self.index_map = index_map
        self.listing_drops_last = listing_drops_last
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
        order = ["-b", "-v", "-V", "-p", "-s"]  # ubertooth-util report order
        requested = [q for q in order if q in queries]
        listed = self.order[:-1] if self.listing_drops_last else self.order
        lines = []
        for index, serial in enumerate(listed):
            for query in requested:
                for line in self._report(serial, query):
                    if len(requested) == 1 and LABELS[query]:
                        line = line.removeprefix(LABELS[query])
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
                    return self._result(command, 1, "", f"error: no Ubertooth at index {index}\n")
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
        return self._result(
            command, 1, "",
            "multiple Ubertooth devices found! Use '-U' to specify device number\n"
            + devices + "could not open Ubertooth device\n",
        )


STRICT_MULTI_CASES = ("index-past-end-refused",)


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
        check = util_cases.serial_listing_check((A, B))
        self.assertEqual("", check(f"0: {B}\n1: {A}\n"))
        self.assertIn("not exactly", check(f"0: {A}\n"))
        self.assertIn("unregistered", check(f"0: {A}\n1: {B}\n2: {C}\n"))
        self.assertIn("not 0..1 in order", check(f"1: {A}\n0: {B}\n"))
        self.assertIn("not '<index>: <value>'", check(f"Serial No: {A}\n"))
        self.assertIn("not exactly", check(f"0: {A}\n1: {A}\n"))

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

    def test_comprehensive_fails_only_the_strict_case_on_fork_main(self):
        summary, events, _report = multi_run(MultiBench((A, B)), "comprehensive")
        self.assertFalse(summary.passed)
        failed = failed_cases(events)
        self.assertEqual(1, len(failed), failed)
        self.assertTrue(failed[0].endswith("index-past-end-refused"))
        self.assertIn("command exited 0", summary.failure_reason)

    def test_comprehensive_passes_once_index_past_end_is_refused(self):
        summary, _events, _report = multi_run(MultiBench((A, B), past_end=None),
                                              "comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)

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
