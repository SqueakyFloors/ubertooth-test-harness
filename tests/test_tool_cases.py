import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import specandump, tool_cases
from ubertooth_harness.stateful import StateCase
from tests.test_transitions import SERIAL, FakeUbertooth, cases_for, run_test, steps

REGISTERS = {0x00: ("MAIN", 0x8000), 0x01: ("FSCTRL", 0x0010),
             0x02: ("FSDIV", 0x0961), 0x03: ("MDMCTRL", 0x0040)}


class ToolUbertooth(FakeUbertooth):
    """FakeUbertooth plus ubertooth-specan, ubertooth-debug, btle -a/-t/-x,
    and btle -p, as on fork main 3b22c95.

    Flags switch on the fork bugs (fixed on main) the cases catch:
    ``stale_specan`` (#173), ``debug_exit_is_value`` (#164), ``btle_aa_bugs``
    (#171), ``specan_stop_resets=False`` (#191: stopping specan skips the idle
    reset), and ``promisc_stop_resets=False`` (the same for btle -p).
    """

    def __init__(self, *args, stale_specan=False, debug_exit_is_value=False,
                 btle_aa_bugs=False, specan_stop_resets=True, promisc_stop_resets=True,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.stale_specan = stale_specan
        self.debug_exit_is_value = debug_exit_is_value
        self.btle_aa_bugs = btle_aa_bugs
        self.specan_stop_resets = specan_stop_resets
        self.promisc_stop_resets = promisc_stop_resets
        self.state["aa"] = "8e89bed6"
        self.last_specan = None

    def run(self, argv, *, timeout_s, cancellation=None, on_output=None):
        command = tuple(argv)
        if command[1:2] == (tool_cases._SPECAN_CHECKER,):
            out = io.StringIO()
            with redirect_stdout(out):
                code = specandump.main(list(command[2:]))
            return self._result(command, code, out.getvalue(), "", on_output)
        if command[0] == "ubertooth-specan":
            self.commands.append(command)
            return self._specan(command, on_output)
        if command[0] == "ubertooth-debug":
            self.commands.append(command)
            return self._debug(command, on_output)
        options = command[3:]
        if command[0] == "ubertooth-btle" and options == ("-p",):
            self.commands.append(command)
            return self._promisc(command, on_output)
        if command[0] == "ubertooth-btle" and any(o[:2] in ("-a", "-t", "-x") for o in options):
            self.commands.append(command)
            return self._btle_settings(command, options, on_output)
        if command[0] == "ubertooth-util" and "-r" in options:
            self.state["aa"] = "8e89bed6"
        return super().run(argv, timeout_s=timeout_s, cancellation=cancellation,
                           on_output=on_output)

    # ubertooth-specan ---------------------------------------------------
    def _specan(self, command, on_output):
        opts = list(command[3:])
        low = int(opts[opts.index("-l") + 1]) if "-l" in opts else 2402
        high = int(opts[opts.index("-u") + 1]) if "-u" in opts else 2480
        fmt = "-g" if "-g" in opts else "-G" if "-G" in opts else "default"
        sweeps = []
        if self.stale_specan and self.last_specan:
            sweeps.append(self.last_specan)
        sweeps += [(low, high)] * 3
        self.last_specan = (low, high)
        self.mode = "specan"
        self._leave(self.specan_stop_resets)
        if "-d" in opts:
            data = bytearray()
            for lo, hi in sweeps:
                for f in range(lo, hi + 1):
                    data += bytes([f >> 8, f & 0xFF, 0xD3])
            Path(opts[opts.index("-d") + 1]).write_bytes(bytes(data))
            return self._result(command, 124, "", "HARD TIMEOUT\n", on_output, timed_out=True)
        lines = []
        for lo, hi in sweeps:
            for f in range(lo, hi + 1):
                lines.append({"default": f"0.100000, {f}, -45", "-g": f"{f} -45",
                              "-G": f"0.100000 {f} -45"}[fmt])
            if fmt != "default":
                lines.append("")
        stderr = "verbose: debug\n" if "-v" in opts else ""
        return self._result(command, 124, "\n".join(lines) + "\n", stderr + "HARD TIMEOUT\n",
                            on_output, timed_out=True)

    def _leave(self, resets):
        """The tool's ubertooth_stop() on SIGTERM, then the harness -S."""
        if resets:
            self._stop_mode()
        else:
            self.mode = None

    def _promisc(self, command, on_output):
        # bt_promisc_le() -> reset_le(): advertising access address, no CRC check.
        self.mode = "ble"
        self.state["aa"] = "8e89bed6"
        self.state["crc"] = "0"
        self._leave(self.promisc_stop_resets)
        return self._result(command, 124, "", "HARD TIMEOUT\n", on_output, timed_out=True)

    # ubertooth-debug ----------------------------------------------------
    def _debug(self, command, on_output):
        opts = list(command[3:])
        verbose = 1
        for o in opts:
            if o.startswith("-v"):
                verbose = int(o[2:])
                if not 0 <= verbose <= 2:
                    return self._result(command, 1, "", f"error: verbosity must be 0-2 (got {verbose})\n", on_output)
        spec = opts[opts.index("-r") + 1]
        if "-" in spec:
            a, b = (int(x, 16) for x in spec.split("-"))
            regs = list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
        else:
            regs = [int(x, 16) for x in spec.split(",")]
        if any(r > 0xFF for r in regs):
            return self._result(command, 1, "",
                                "ERROR: register address must be > 0x00 and < 0xff\n", on_output)
        out = []
        value = 0
        for r in regs:
            name, value = REGISTERS.get(r, (f"r{r:02x}", 0))
            out.append(f"%{name} = 0x{value:04X}")
            if verbose >= 1:
                out.append("1....... ........ (RW) FIELD")
            if verbose == 2:
                out.append(".00000.. ........ (W0) -")
        code = value & 0xFF if self.debug_exit_is_value else 0
        return self._result(command, code, "\n".join(out) + "\n", "", on_output)

    # ubertooth-btle -a / -t / -x ----------------------------------------
    def _btle_settings(self, command, options, on_output):
        out = []
        get = "-a" in options
        sets = [o[2:] for o in options if o.startswith("-a") and len(o) > 2]
        for o in options:
            if o.startswith("-t"):
                value = o[2:]
                if value == "none":
                    out.append("target cleared")
                else:
                    mac, _, mask = value.partition("/")
                    out.append(f"target set to: {mac}/{mask or 48}")
        for value in sets:
            if not all(c in "0123456789abcdef" for c in value.lower()):
                if not self.btle_aa_bugs:
                    return self._result(
                        command, 1, "",
                        f"error: access address must be 1-8 hex digits (got {value})\n",
                        on_output)
                value = "6fbd5b98"
            self.state["aa"] = value.lower().rjust(8, "0")
            out.append(f"access address set to: {self.state['aa']}")
        if get:
            out.append(f"Access address: {self.state['aa']}")
            if self.btle_aa_bugs:
                return self._result(command, 0, "\n".join(out) + "\n", "", on_output)
        if "-v" in options:
            out.append(self._crc_line(self.state["crc"]).strip())
        return self._result(command, 0, "\n".join(out) + "\n", "", on_output)


def failed_names(events):
    return sorted(e.details["case_name"] for e in events
                  if e.event_type == "check_finished" and e.status == "failed")


class SweepCheckTests(unittest.TestCase):

    def test_accepts_a_full_in_range_sweep(self):
        check = tool_cases.sweep_check("default", 2402, 2404)
        out = "0.1, 2402, -45\n0.1, 2403, -44\n0.1, 2404, -60\nHARD TIMEOUT after 3s\n"
        self.assertEqual("", check(out))

    def test_reports_out_of_range_missing_and_garbage(self):
        check = tool_cases.sweep_check("-g", 2402, 2404)
        self.assertIn("outside", check("2402 -45\n2403 -45\n2404 -45\n2441 -40\n"))
        self.assertIn("no samples for 1", check("2402 -45\n2403 -45\n"))
        self.assertIn("no sample lines", check("Ubertooth stopped\n"))
        self.assertIn("not -g samples", check("2402 -45\n2403 -45\n2404 -45\n" + "x\n" * 6))

    def test_dump_file_with_a_partial_record_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.bin"
            path.write_bytes(bytes([0x09, 0x62, 0xD3, 0x09]))
            out = io.StringIO()
            with redirect_stdout(out):
                code = specandump.main([str(path), "2402", "2402"])
        self.assertEqual(1, code)
        self.assertIn("1 trailing bytes", out.getvalue())

    def test_stderr_lines_do_not_count_as_bad_samples(self):
        from ubertooth_harness.executor import CommandResult
        step = tool_cases._specan_case(SERIAL, "x", "x", (), "default", 2402, 2403, 1.0).steps[-2]
        result = CommandResult(("ubertooth-specan",), 124, "0.1, 2402, -45\n0.1, 2403, -45\n",
                               "debug line\n" * 20, 1.0, True)
        self.assertEqual("", step.evaluate(result, {}).failure)

    def test_dump_file_summary(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.bin"
            path.write_bytes(bytes([0x09, 0x62, 0xD3, 0x09, 0x63, 0xD3, 0x09, 0x89, 0xD3]))
            samples, seen, outside, trailing = specandump.summarize(path, 2402, 2403)
        self.assertEqual((3, {2402, 2403}, 1, 0), (samples, seen, outside, trailing))


class SpectrumAnalyzerTests(unittest.TestCase):

    def test_smoke_is_one_full_band_case_comprehensive_adds_the_rest(self):
        _a, smoke = cases_for_duration("smoke")
        _a, full = cases_for_duration("comprehensive")
        self.assertEqual(["full-band"], [c.case_id for c in smoke])
        self.assertEqual(9, len(full))
        self.assertEqual("stop-resets-board", full[-1].case_id)
        self.assertTrue(all(isinstance(c, StateCase) for c in full))

    def test_passes_on_a_correct_tool(self):
        summary, events = run_spectrum(ToolUbertooth(crc="1"))
        self.assertTrue(summary.passed, summary.failure_reason)
        dump = steps(events, step_id="verify-dump")
        self.assertEqual(["passed"], [e.status for e in dump])

    def test_leftover_samples_from_the_previous_run_fail(self):
        summary, events = run_spectrum(ToolUbertooth(crc="1", stale_specan=True))
        self.assertFalse(summary.passed)
        self.assertIn("2402-2410 MHz after a full-band run", failed_names(events))
        self.assertIn("samples outside 2402-2410 MHz", summary.failure_reason)

    def test_a_specan_stop_without_the_idle_reset_fails(self):
        # SqueakyFloors/ubertooth#191 (fork builds before aa4f620).
        device = ToolUbertooth(crc="1", specan_stop_resets=False)
        summary, events = run_spectrum(device)
        self.assertFalse(summary.passed)
        self.assertEqual(["Stopping ubertooth-specan resets LEDs, channel, and squelch"],
                         failed_names(events))
        self.assertIn("LEDs off after the stop", summary.failure_reason)
        self.assertEqual("2441", device.state["channel"])  # cleanup restored it

    def test_the_stop_reset_case_presets_every_setting(self):
        _a, full = cases_for_duration("comprehensive")
        case = full[-1]
        ids = [s.step_id for s in case.steps]
        self.assertLess(ids.index("verify-leds-set"), ids.index("sweep"))
        self.assertLess(ids.index("sweep"), ids.index("mode-stop"))
        self.assertLess(ids.index("mode-stop"), ids.index("verify-pa-level-after-stop"))
        preset = case.steps[ids.index("set-non-defaults")].resolve_argv(
            {"original channel": "2441"})
        self.assertEqual(("-c2426", "-z-60", "-d1"), preset[3:])
        self.assertTrue(case.steps[ids.index("sweep")].continuous)


class DebugRegistersTests(unittest.TestCase):

    def test_passes_on_a_correct_tool(self):
        summary, _events = run_test("Debug registers", ToolUbertooth(crc="1"),
                                    coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)

    def test_exit_status_equal_to_the_register_value_fails(self):
        summary, events = run_test("Debug registers", ToolUbertooth(crc="1", debug_exit_is_value=True),
                                   coverage="comprehensive")
        self.assertFalse(summary.passed)
        names = failed_names(events)
        self.assertIn("A successful read of 0x01 exits 0", names)
        self.assertNotIn("Verbosity -v0", names)  # 0x00 reads 0x8000: low byte 0
        smoke, _events = run_test("Debug registers", ToolUbertooth(crc="1", debug_exit_is_value=True))
        self.assertTrue(smoke.passed, smoke.failure_reason)  # smoke stays green

    def test_smoke_is_the_register_list(self):
        _a, smoke = cases_for("Debug registers", "smoke")
        self.assertEqual(["list"], [c.case_id for c in smoke])


class BleSettingsTests(unittest.TestCase):

    def test_passes_on_a_correct_tool(self):
        device = ToolUbertooth(crc="1")
        summary, _events = run_test("BLE settings", device, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual("8e89bed6", device.state["aa"])

    def test_known_access_address_bugs_fail_only_their_cases(self):
        device = ToolUbertooth(crc="1", btle_aa_bugs=True)
        summary, events = run_test("BLE settings", device, coverage="comprehensive")
        self.assertFalse(summary.passed)
        self.assertEqual(["-a -v reports both", "Reject -azzzz"], failed_names(events))

    def test_a_promiscuous_stop_without_the_idle_reset_fails(self):
        device = ToolUbertooth(crc="1", promisc_stop_resets=False)
        summary, events = run_test("BLE settings", device, coverage="comprehensive")
        self.assertFalse(summary.passed)
        self.assertEqual(["Stopping ubertooth-btle -p resets LEDs, channel, and squelch"],
                         failed_names(events))
        # Cleanup put back what -p changed.
        self.assertEqual(("8e89bed6", "1"), (device.state["aa"], device.state["crc"]))

    def test_promiscuous_case_is_receive_only_and_restores_the_le_state(self):
        _a, full = cases_for("BLE settings", "comprehensive")
        case = next(c for c in full if c.case_id == "promiscuous-stop-resets-board")
        run = next(s for s in case.steps if s.step_id == "promiscuous")
        self.assertEqual(("ubertooth-btle", "-U", SERIAL, "-p"), run.resolve_argv({}))
        cleanup = [s.step_id for s in case.cleanup]
        for step_id in ("verify-restored-crc", "verify-restored-access-address",
                        "verify-restored-channel", "verify-restored-squelch",
                        "verify-restored-leds"):
            self.assertIn(step_id, cleanup)
        device = ToolUbertooth(crc="1")
        device.state["aa"] = "12345678"
        summary, _events = run_test("BLE settings", device, coverage="comprehensive")
        self.assertTrue(summary.passed, summary.failure_reason)
        self.assertEqual(("12345678", "1"), (device.state["aa"], device.state["crc"]))

    def test_every_command_selects_the_board(self):
        _a, full = cases_for("BLE settings", "comprehensive")
        for case in full:
            for step in case.steps + case.cleanup:
                argv = step.resolve_argv({"original access address": "8e89bed6",
                                          "original crc": "0", "original channel": "2441",
                                          "original pa-level": "7", "original squelch": "-90",
                                          "original leds": "0/0/0"})
                with self.subTest(case.case_id, step=step.step_id):
                    self.assertEqual(SERIAL, argv[argv.index("-U") + 1])


def cases_for_duration(coverage):
    from ubertooth_harness.catalog import builtin_catalog
    definition = builtin_catalog().get("Spectrum analyzer")
    arguments = definition.normalize_arguments(
        {"hosts": ["Host_Linux"], "Ubertooth": "default", "coverage": coverage, "duration": "1s"})
    return arguments, definition.command_cases(
        arguments, {"Ubertooth": "UT_Linux0"}, {"Ubertooth": {"serial": SERIAL}})


def run_spectrum(device):
    from unittest import mock
    import tests.test_transitions as tt
    original = tt.cases_for

    def with_duration(test_name, coverage="smoke"):
        if test_name == "Spectrum analyzer":
            return cases_for_duration(coverage)
        return original(test_name, coverage)

    with mock.patch.object(tt, "cases_for", with_duration):
        return tt.run_test("Spectrum analyzer", device, coverage="comprehensive")


if __name__ == "__main__":
    unittest.main()
