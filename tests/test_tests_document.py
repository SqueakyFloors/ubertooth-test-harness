"""`docs/tests.md` against the catalog: one section per test, and every
executable a test really runs named in its section.

No hardware, no network, nothing built: the document is read from the
repository, and the commands are the ones the catalog compiles for the
example bench, which is the same code a run builds its cases with. So this
is not a second catalog -- there is nothing here to keep in step with
``catalog.py`` -- it is the catalog asked what it would run and the document
asked whether it says so (#347).

What it cannot see: a few steps build their command from values an earlier
step recorded, and a ``FlashCase`` is argv the flash runner assembles rather
than a step. Those are skipped, and ``UNCOLLECTED`` names the tests they
leave uncovered, so a test that stops being covered at all is a failure here
rather than a silence.
"""

from pathlib import Path
import re
import tempfile
import unittest

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cases import CommandCase, FlashCase
from ubertooth_harness.hostneeds import HOST_TOOLS
from ubertooth_harness.plan import load_plan
from ubertooth_harness.planner import PlanCompiler
from ubertooth_harness.resources import FileResourceRegistry
from ubertooth_harness.stateful import StateCase
from ubertooth_harness import transitions

REPOSITORY = Path(__file__).resolve().parents[1]
DOCUMENT = REPOSITORY / "docs" / "tests.md"
PLANS = REPOSITORY / "testplans"
REGISTRY = REPOSITORY / "hardware.example.yaml"

# The bold field each section carries, in order.
FIELDS = ("What it does", "Theory of operation", "Hardware and arguments",
          "Commands", "Expected outcome", "Known limits")
# The section that documents the commands every transition case shares (the
# idle stop, the reads it records, the restores, the guard reads, and
# preflight), so no section below repeats them.
SHARED = "What every test shares"
# The last section, which is not a test: what the harness has decided not to
# test, so a missing catalog entry is not mistaken for an oversight.
OUT_OF_SCOPE = "Out of scope: Uberducky and E-GO devices"
# Tests whose commands this check cannot collect, with why.
UNCOLLECTED = {
    # One FlashCase, whose ubertooth-dfu and ubertooth-util commands the
    # flash runner assembles per write rather than as steps (flash.py).
    "Flash reliability": "its case is a FlashCase, not argv",
}
# Shell and file utilities a case runs around the tool under test; naming
# them in a section would say nothing about the fork's CLI.
UTILITIES = frozenset({"env", "sh", "bash", "chmod", "ln", "mkdir", "python",
                       "python3", "timeout"})


def _registry(directory):
    """The example bench, with its placeholder serials filled in.

    The placeholders are not 32 hex digits, which selection by serial
    refuses, and this check is about which executables run rather than which
    board they name.
    """
    text = REGISTRY.read_text(encoding="utf-8")
    # The hub's is a 32-bit BrainStem serial, not a board serial.
    text = text.replace("REPLACE_WITH_ACRONAME_SERIAL", "12345678")
    for index, placeholder in enumerate(
        dict.fromkeys(re.findall(r"REPLACE_WITH_[A-Z_]+", text))
    ):
        # Letters as well as digits, so YAML reads it as a string and
        # selection by serial accepts it.
        text = text.replace(placeholder, f"abcdef{index:02x}" * 4)
    path = Path(directory) / "hardware.yaml"
    path.write_text(text, encoding="utf-8")
    return FileResourceRegistry.load(path)


def _context():
    """A stand-in for what an earlier step recorded.

    The settings' own values, so a restore step that parses one (the LED
    triple, the channel in MHz) resolves; anything else becomes a visible
    placeholder.
    """
    settings = (transitions.CHANNEL, transitions.PA_LEVEL, transitions.SQUELCH,
                transitions.USR_LED, transitions.LEDS, transitions.CHANNEL_INDEX)
    recorded = {
        setting.original: (setting.after_mode_stop
                           or (setting.candidates[0] if setting.candidates else "0"))
        for setting in settings
    }
    recorded["original crc"] = "0"

    class Recorded(dict):
        def __missing__(self, key):
            return recorded.get(key, f"<{key}>")

    return Recorded()


def program(token: str) -> str:
    """The program a command-line word names, or "" for anything else."""
    name = token.rsplit("/", 1)[-1]
    if name.startswith("ubertooth-") and not name.endswith((".dfu", ".bin")):
        return name
    if name in HOST_TOOLS or name == "lpc21isp":
        return name
    if name.endswith(".py") and ("/ubertooth_harness/" in token
                                 or name == "btle-extcap.py"):
        return name
    return ""


def programs_by_test():
    """Every program each test's cases run, from the catalog's own cases."""
    catalog = builtin_catalog()
    used: dict[str, set[str]] = {}
    with tempfile.TemporaryDirectory() as directory:
        registry = _registry(directory)
        compiler = PlanCompiler(catalog, registry)
        invocations = []
        for path in sorted(PLANS.glob("*.yaml")):
            plan = load_plan(path, catalog)
            for suite in plan.suites:
                try:
                    invocations.extend(compiler.compile_suite(plan, suite).invocations)
                except Exception:  # noqa: BLE001 - the weak-link suites
                    # A suite the example bench cannot compile (it carries no
                    # attenuated board); its tests are in another plan's
                    # suite, which is where they are collected from.
                    continue
    for invocation in invocations:
        definition = catalog.get(invocation.test_name)
        if not definition.implemented:
            continue
        cases = definition.command_cases(
            invocation.arguments, invocation.resources,
            invocation.resource_identities,
        )
        found = used.setdefault(invocation.test_name, set())
        for case in cases:
            for argv in _argvs(case):
                found.update(filter(None, (program(word) for word in argv)))
    # A test no committed plan names (`Host version` and `Help smoke`, whose
    # question every run asks in preflight instead) still has commands: ask
    # it for them with nothing but a host.
    for name in catalog.names:
        definition = catalog.get(name)
        if name in used or not definition.implemented or definition.hardware_roles:
            continue
        arguments = definition.normalize_arguments({"hosts": ["Host_Linux"]})
        found = used.setdefault(name, set())
        for case in definition.command_cases(arguments, {}, {}):
            for argv in _argvs(case):
                found.update(filter(None, (program(word) for word in argv)))
    return {name: found - UTILITIES for name, found in used.items()}


def _argvs(case):
    if isinstance(case, CommandCase):
        return [tuple(case.argv)]
    if isinstance(case, FlashCase):
        return []
    assert isinstance(case, StateCase)
    out = []
    for step in tuple(case.steps) + tuple(case.cleanup):
        try:
            out.append(step.resolve_argv(_context()))
        except Exception:  # noqa: BLE001
            # A command built from a value only a run records.
            continue
    return out


def sections():
    """Each `## ` section of the document, in order, with its body."""
    found: dict[str, str] = {}
    name = None
    body: list[str] = []
    for line in DOCUMENT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if name is not None:
                found[name] = "\n".join(body)
            name, body = line[3:].strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        found[name] = "\n".join(body)
    return found


class DocumentStructureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.sections = sections()

    def test_the_test_sections_are_the_catalog_names_in_catalog_order(self):
        # A test added without a section, a section left behind by a removed
        # test, and a heading that drifted from its catalog name all fail
        # here. The two leading sections explain the document; the last one
        # records what is deliberately not tested.
        headings = list(self.sections)
        self.assertEqual(["How to read a section", SHARED], headings[:2])
        self.assertEqual(OUT_OF_SCOPE, headings[-1])
        self.assertEqual(list(self.catalog.names), headings[2:-1])

    def test_every_section_carries_every_field_once_and_in_order(self):
        for name in self.catalog.names:
            with self.subTest(name):
                body = self.sections[name]
                for field in FIELDS:
                    self.assertEqual(
                        1, body.count(f"**{field}.**"),
                        f"{name} must carry one **{field}.** paragraph",
                    )
                self.assertEqual(
                    sorted(FIELDS, key=lambda f: body.index(f"**{f}.**")),
                    list(FIELDS),
                    f"{name}: the fields are out of order",
                )

    def test_a_test_with_no_implementation_says_it_sends_no_commands(self):
        for name in self.catalog.names:
            if self.catalog.get(name).implemented:
                continue
            with self.subTest(name):
                commands = self.sections[name].split("**Commands.**", 1)[1]
                commands = commands.split("**Expected outcome.**", 1)[0]
                self.assertIn("None", commands)


class DocumentedCommandTests(unittest.TestCase):
    """Every executable a test runs is named where a reader looks for it."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.sections = sections()
        cls.used = programs_by_test()

    def test_every_implemented_test_has_commands_to_check(self):
        # Otherwise the check below passes by having nothing to compare: a
        # test that drops out of every plan, or whose cases stop compiling
        # against the example bench, is a failure here.
        missing = sorted(
            name for name in self.catalog.names
            if self.catalog.get(name).implemented
            and not self.used.get(name)
            and name not in UNCOLLECTED
        )
        self.assertEqual([], missing)
        self.assertEqual(sorted(UNCOLLECTED), sorted(
            name for name in UNCOLLECTED if not self.used.get(name)
        ))

    def test_every_executable_a_test_runs_is_named_in_its_section(self):
        shared = self.sections[SHARED]
        for name, used in sorted(self.used.items()):
            body = self.sections[name]
            with self.subTest(name):
                undocumented = sorted(
                    item for item in used
                    if item not in body and item not in shared
                )
                self.assertEqual(
                    [], undocumented,
                    f"{name} runs these and its section does not name them",
                )

    def test_the_program_reader_takes_the_tool_out_of_a_wrapped_command(self):
        # The check is only as good as this: a driver's own argv carries the
        # tool it wraps, and an image path is not a program.
        self.assertEqual("ubertooth-rx", program("ubertooth-rx"))
        self.assertEqual("esp32peer.py", program("/x/ubertooth_harness/esp32peer.py"))
        self.assertEqual("tshark", program("tshark"))
        self.assertEqual("", program("/x/build/bluetooth_rxtx.dfu"))
        self.assertEqual("", program("-U"))
        self.assertEqual("", program("/usr/bin/env"))


if __name__ == "__main__":
    unittest.main()
