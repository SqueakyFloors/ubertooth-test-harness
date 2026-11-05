"""`docs/tests.md` against the catalog: one section per test, and every
executable a test really runs named in that section's **Commands** block.

No hardware, no network, nothing built: the document is read from the
repository, and the commands are the ones the catalog compiles for the
example bench, which is the same code a run builds its cases with. So this
is not a second catalog -- there is nothing here to keep in step with
``catalog.py`` -- it is the catalog asked what it would run and the document
asked whether it says so (#347).

It is checked both ways round. Forward: every executable a test's cases run
is named in its **Commands** block, or in the shared block that states the
commands every transition case sends. Backward: every executable a
**Commands** block names is one that test runs, so a command form nobody
sends cannot sit there looking authoritative. Only the fenced blocks are
read, never the prose around them: a tool mentioned in a sentence is a
cross-reference, not a command this test sends.

What it cannot see:

- a ``FlashCase``, which is argv the flash runner assembles per write rather
  than a step. ``UNCOLLECTED`` names the tests that leaves uncovered, so one
  that stops being covered at all fails here rather than passing quietly;
- a tool a driver finds for itself rather than taking on its command line
  (``pytools.py`` runs ``ubertooth-specan-ui`` and ``btle-extcap.py``,
  ``extcapcapture.py`` runs ``ubertooth-btle``, ``nrfdk.py`` runs
  ``nrfutil``). ``BY_A_DRIVER`` lists those, so the backward check does not
  call them invented;
- a step whose command is built from a value an earlier step recorded, where
  the stand-in context below cannot produce a usable value.

A tool inside a shell string *is* seen: ``_words`` splits the argument of
``sh -c``/``bash -c``, which is how ``Exclusive Ubertooth topology`` runs
``ubertooth-btle -f``.
"""

from pathlib import Path
import re
import shlex
import tempfile
import unittest

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.cases import CommandCase, FlashCase
from ubertooth_harness.hostneeds import HOST_TOOLS
from ubertooth_harness.plan import PlanError, load_plan
from ubertooth_harness.planner import PlanCompiler, PlanningError
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
# Programs a driver runs itself, so they are in no argv the catalog builds,
# per test: the backward check must not call these invented, and it must not
# excuse them anywhere else. The forward check cannot ask for them at all.
BY_A_DRIVER = {
    # pytools.py runs the installed specan-ui and the btle-extcap.py of the
    # resolved commit; extcapcapture.py and tsharkcapture.py run
    # ubertooth-btle behind the extcap script.
    "Python tools": {"ubertooth-specan-ui", "btle-extcap.py"},
    "BLE extcap interfaces": {"btle-extcap.py"},
    "BLE extcap capture": {"btle-extcap.py", "ubertooth-btle"},
    "BLE extcap in tshark": {"btle-extcap.py", "ubertooth-btle", "tshark",
                             "nrfutil"},
    # nrfdk.py flashes with nrfutil.
    "Capture known BLE advertiser": {"nrfutil"},
    "Capture known BLE connection": {"nrfutil"},
    "Follow a BLE connection across its end": {"nrfutil"},
    # btbrconsole.py drives the prompt of ubertooth-btbr.
    "Classic btbr console": {"ubertooth-btbr"},
    # The flash runner assembles these per write (UNCOLLECTED).
    "Flash reliability": {"ubertooth-dfu", "ubertooth-util"},
    # Preflight's own read, and the bootloader write over a programmer.
    "Bootloader identity": {"ubertooth-util", "lpc21isp"},
}
# Wrappers a case puts in front of the tool under test. They are not what a
# section is about, and `env` or `timeout` in front of every no-board
# command would say nothing, so `program` does not name them. The file
# utilities a case uses to set its directory up (ln, chmod, mkdir) are NOT
# here: they change what the tool then finds, so a section names them.
WRAPPERS = frozenset({"env", "sh", "bash", "timeout", "python", "python3"})
# A word a Commands block writes that looks like a program and is not: a
# placeholder in angle brackets.
NOT_A_PROGRAM = re.compile(r"^<.*>$")
# Option words a Commands block writes that stand for a set of real options
# rather than naming one: `-<mode>`, `-<query>`, `-r|-q|-d`, `--variant`'s
# `<full|partial>` value. A word with a placeholder in it is one of those.
OPTION = re.compile(r"^-{1,2}[A-Za-z][A-Za-z0-9-]*$")


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


# The harness's own driver modules, by file name: a Commands block writes
# one bare (`esp32peer.py stream ...`) and an argv carries its full path.
# Read from the package, so a driver added later is recognized.
HARNESS_MODULES = frozenset(
    path.name for path in (REPOSITORY / "ubertooth_harness").glob("*.py")
) | {"btle-extcap.py"}


def program(token: str) -> str:
    """The program a command-line word names, or "" for anything else."""
    name = token.rsplit("/", 1)[-1]
    if NOT_A_PROGRAM.match(name):
        return ""
    if name.startswith("ubertooth-") and not name.endswith((".dfu", ".bin")):
        return name
    if name in HOST_TOOLS or name in ("lpc21isp", "nrfutil"):
        return name
    if name in HARNESS_MODULES:
        return name
    if name in ("ln", "chmod", "mkdir"):
        return name
    return ""


def _words(argv) -> list[str]:
    """Every word of a command, including the words inside a shell string.

    ``sh -c '<script>'`` and ``bash -c '<script>'`` carry the tool they run
    inside one argument, so the script is split too.
    """
    words = []
    previous = ""
    for word in argv:
        words.append(word)
        if previous == "-c" and word:
            try:
                words.extend(shlex.split(word))
            except ValueError:
                words.extend(word.split())
        previous = word
    return words


def _options(argv) -> set[tuple[str, str]]:
    """(program, option) for every option a command passes to a program.

    The program is whichever one the option follows, so a driver's own
    options and the options of the tool it wraps are kept apart. An attached
    value is dropped (`-c2426` is `-c`, `--variant=x` is `--variant`), since
    only the option names are compared.
    """
    found = set()
    current = ""
    for word in _words(argv):
        name = program(word)
        if name:
            current = name
            continue
        if not current or not word.startswith("-") or word == "-":
            continue
        word = word.split("=", 1)[0]
        if word.startswith("--"):
            found.add((current, word))
            continue
        # A short option carries its value attached: `-c2426`, `-U<serial>`,
        # `-A37`. The option is its first letter.
        found.add((current, word[:2]))
    return found


def programs_by_test():
    """Every program each test's cases run, from the catalog's own cases."""
    catalog = builtin_catalog()
    used: dict[str, set[str]] = {}
    options: dict[str, set[tuple[str, str]]] = {}
    with tempfile.TemporaryDirectory() as directory:
        registry = _registry(directory)
        compiler = PlanCompiler(catalog, registry)
        invocations = []
        skipped = []
        for path in sorted(PLANS.glob("*.yaml")):
            plan = load_plan(path, catalog)
            for suite in plan.suites:
                try:
                    invocations.extend(compiler.compile_suite(plan, suite).invocations)
                except (PlanningError, PlanError) as error:
                    # A suite the example bench cannot compile: the weak-link
                    # suites, which ask for an attenuated board that registry
                    # carries commented out. Their tests are in another
                    # plan's suite, which is where they are collected from.
                    skipped.append((f"{path.name}:{suite}", str(error)))
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
                found.update(filter(None, (program(w) for w in _words(argv))))
                options.setdefault(invocation.test_name, set()).update(
                    _options(argv))
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
                found.update(filter(None, (program(w) for w in _words(argv))))
                options.setdefault(name, set()).update(_options(argv))
    used = {name: found - WRAPPERS for name, found in used.items()}
    return used, options, skipped


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
    """Each `## ` section of the document, in order, with its body.

    A duplicate heading is kept as its own entry, so the structural check
    below sees it rather than one quietly replacing the other.
    """
    found: list[tuple[str, str]] = []
    name = None
    body: list[str] = []
    for line in DOCUMENT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if name is not None:
                found.append((name, "\n".join(body)))
            name, body = line[3:].strip(), []
        elif name is not None:
            body.append(line)
    if name is not None:
        found.append((name, "\n".join(body)))
    return found


def fences(text: str) -> list[str]:
    """The lines inside every fenced block of some text."""
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
        elif inside:
            lines.append(line)
    return lines


def commands_of(body: str) -> list[str]:
    """The lines of every fenced block in the **Commands** field of a body."""
    field = body.split("**Commands.**", 1)
    if len(field) == 1:
        return []
    return fences(field[1].split("**Expected outcome.**", 1)[0])


def _fence_words(lines):
    """The words of a Commands block, with the comments taken off."""
    for line in lines:
        text = line.split("#", 1)[0]
        for word in text.replace("|", " ").split():
            yield word.strip(",;`.)(")


def programs_named(lines) -> set[str]:
    """The programs the lines of a Commands block name."""
    named = {program(word) for word in _fence_words(lines)}
    return {name for name in named if name} - WRAPPERS


def options_named(lines) -> set[tuple[str, str]]:
    """(program, option) for every option a Commands block names.

    A word with a placeholder in it stands for a set of real options
    (`-<mode>`, `-<query>`, `-<setter or mode>`) and is passed over: which
    options it covers is what the prose beside the block says.
    """
    found = set()
    current = ""
    for word in _fence_words(lines):
        name = program(word)
        if name:
            current = name
            continue
        if not current or "<" in word or not OPTION.match(word):
            continue
        found.add((current, word if word.startswith("--") else word[:2]))
    return {(prog, opt) for prog, opt in found if prog not in WRAPPERS}


class DocumentStructureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.order = sections()
        cls.sections = dict(cls.order)

    def test_the_test_sections_are_the_catalog_names_in_catalog_order(self):
        # A test added without a section, a section left behind by a removed
        # test, a heading that drifted from its catalog name, and a heading
        # written twice all fail here. The two leading sections explain the
        # document; the last one records what is deliberately not tested.
        headings = [name for name, _body in self.order]
        self.assertEqual(sorted(set(headings)), sorted(headings),
                         "a heading is written twice")
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
                field = self.sections[name].split("**Commands.**", 1)[1]
                self.assertTrue(
                    field.lstrip().startswith("None"),
                    f"{name}: the Commands field must open with None",
                )


class DocumentedCommandTests(unittest.TestCase):
    """The Commands blocks against the commands the catalog really builds."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.sections = dict(sections())
        cls.used, cls.options, cls.skipped = programs_by_test()
        cls.named = {
            name: programs_named(commands_of(body))
            for name, body in cls.sections.items()
        }
        cls.named_options = {
            name: options_named(commands_of(body))
            for name, body in cls.sections.items()
        }

    def test_only_the_weak_link_suites_are_left_out_of_the_comparison(self):
        # Those ask for an attenuated board the example registry carries
        # commented out. Any other planning failure would quietly shrink
        # what is compared, so it is a failure here -- every suite that was
        # left out is named, `weak-link.yaml`'s included.
        expected = [
            "ble.yaml:ble-weak-link", "classic.yaml:classic-weak-link",
            "range.yaml:range-weak-link",
        ] + [f"weak-link.yaml:{suite}" for suite in
             load_plan(PLANS / "weak-link.yaml", self.catalog).suites]
        self.assertEqual(
            sorted(expected), sorted(suite for suite, _reason in self.skipped),
            [reason for _suite, reason in self.skipped],
        )

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

    def test_every_executable_a_test_runs_is_in_its_commands_block(self):
        # The shared section has no **Commands** field of its own: its
        # fenced blocks are the preflight reads and the commands every
        # transition case sends, and only those blocks count here, never the
        # prose around them.
        shared = programs_named(fences(self.sections[SHARED]))
        self.assertEqual({"ubertooth-util", "ubertooth-btle"}, shared)
        for name, used in sorted(self.used.items()):
            with self.subTest(name):
                undocumented = sorted(
                    item for item in used
                    if item not in self.named[name] and item not in shared
                )
                self.assertEqual(
                    [], undocumented,
                    f"{name} runs these and its Commands block does not name them",
                )

    def test_every_executable_a_commands_block_names_is_one_that_test_runs(self):
        # The other way round: a command form nobody sends must not sit in
        # the field a reader trusts. A tool a driver finds for itself is in
        # no argv the catalog builds, so those are allowed for the tests
        # whose driver runs them, and only those.
        for name in self.catalog.names:
            used = self.used.get(name, set()) | BY_A_DRIVER.get(name, set())
            with self.subTest(name):
                invented = sorted(
                    item for item in self.named[name] if item not in used
                )
                self.assertEqual(
                    [], invented,
                    f"{name}'s Commands block names these and it runs none of them",
                )

    def test_every_option_a_commands_block_names_is_one_that_test_passes(self):
        """An option a test does not send must not sit in its Commands block.

        This is where a wrong option name is caught, which naming the
        executable cannot do: a block that wrote `--sco-type` for
        `esp32peer.py audio`, which takes `--type`, reads as authoritative
        and is not. What it does not compare is values, argument order, or
        whether a form the block collapses covers every case: a word with a
        placeholder in it stands for a set of options and is passed over.
        """
        for name in self.catalog.names:
            passed = self.options.get(name, set())
            # A tool a driver runs itself takes options no argv here
            # carries, so its options are passed over -- that tool's, not
            # the whole test's.
            by_a_driver = BY_A_DRIVER.get(name, set())
            with self.subTest(name):
                invented = sorted(
                    f"{prog} {option}"
                    for prog, option in self.named_options[name]
                    if (prog, option) not in passed and prog not in by_a_driver
                )
                self.assertEqual(
                    [], invented,
                    f"{name}'s Commands block names options it never passes",
                )

    def test_the_program_reader_finds_the_tool_a_command_runs(self):
        # Including one inside a shell string, which is how `Exclusive
        # Ubertooth topology` runs ubertooth-btle. An image path and a
        # placeholder are not programs.
        self.assertEqual("ubertooth-rx", program("ubertooth-rx"))
        self.assertEqual("esp32peer.py", program("/x/ubertooth_harness/esp32peer.py"))
        self.assertEqual("tshark", program("tshark"))
        self.assertEqual("", program("/x/build/bluetooth_rxtx.dfu"))
        self.assertEqual("", program("-U"))
        self.assertEqual("", program("/usr/bin/env"))
        self.assertEqual("", program("<serial>"))
        self.assertEqual(
            ["ubertooth-btle"],
            [p for p in (program(w) for w in
                         _words(("bash", "-c", "timeout 5 ubertooth-btle -f | tail")))
             if p],
        )


if __name__ == "__main__":
    unittest.main()
