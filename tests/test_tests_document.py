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

Options are compared both ways too, per program, which is what catches a
block that names the right tool with the wrong options. What that does not
compare:

- option *values*, and argument order;
- a *combination*: a line that puts options this test does pass to this
  program on one command nobody sends passes both directions, since each
  pair is in the set;
- a helper's positional subcommand (``refusal.py probe`` written as
  ``refusal.py sniff`` passes);
- a cluster of short options, where only the first is read
  (``ubertooth-util -vspb`` is ``-v``, so the ``-s -p -b`` of it are
  compared in neither direction);
- whether a form a block collapses behind a placeholder covers every case
  it stands for. ``COLLAPSED`` says which options a placeholder stands for,
  and the document names them too: the prose beside the block, or, for
  ``<peer options>``, which several sections share, the placeholder list in
  "How to read a section";
- a program ``program`` does not know, in either direction: a
  ``ubertooth-*`` name is caught by its prefix, but a misspelled ``tshrk``
  is not a program to it, and the setup utilities it knows are the three the
  cases use today (``ln``, ``chmod``, ``mkdir``), so a case that starts
  running ``cp`` or ``rm`` would not have to be documented until that list
  grows.

What it cannot see at all:

- a ``FlashCase``, which is argv the flash runner assembles per write rather
  than a step. ``UNCOLLECTED`` names the tests that leaves uncovered, so one
  that stops being covered at all fails here rather than passing quietly;
  that test's fence and `Bootloader identity`'s are compared with the code
  that builds their commands instead
  (``test_the_two_fences_no_case_argv_covers_match_the_code``);
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
from ubertooth_harness.stateful import StateCase, artifact_key
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
# The section that says how to read the others, and so where a placeholder
# several sections share is defined.
HOW_TO_READ = "How to read a section"
# The one such placeholder: the options every `esp32peer.py` session carries.
PEER_PLACEHOLDER = "<peer options>"
# The last section, which is not a test: what the harness has decided not to
# test, so a missing catalog entry is not mistaken for an oversight.
OUT_OF_SCOPE = "Out of scope: Uberducky and E-GO devices"
# Tests whose commands this check cannot collect, with why.
UNCOLLECTED = {
    # One FlashCase, whose ubertooth-dfu and ubertooth-util commands the
    # flash runner assembles per write rather than as steps (flash.py).
    "Flash reliability": "its case is a FlashCase, not argv",
}
# A program a test's Commands block names that no argv of that test carries,
# because the command is assembled somewhere the catalog's cases cannot show
# -- the flash runner per write, the bootloader write over a programmer. The
# backward check must not call those invented. An entry for a program the
# block does not name excuses nothing, and an entry for a program the test's
# own commands run would turn off a comparison that could have been made;
# test_every_driver_exemption_is_necessary refuses both.
#
# The tools a driver finds for itself (pytools.py's specan-ui and
# btle-extcap.py, extcapcapture.py's and tsharkcapture.py's ubertooth-btle,
# nrfdk.py's nrfutil, btbrconsole.py's ubertooth-btbr) need no entry: those
# blocks name the driver, which is the command the harness sends, and say in
# prose what it runs.
BY_A_DRIVER = {
    # The flash runner assembles these per write (UNCOLLECTED).
    "Flash reliability": {"ubertooth-dfu", "ubertooth-util"},
    # Preflight's own read, and the bootloader write over a programmer.
    "Bootloader identity": {"ubertooth-util", "lpc21isp"},
}
# A word a Commands block writes that looks like a program and is not: a
# placeholder in angle brackets.
NOT_A_PROGRAM = re.compile(r"^<.*>$")
# Wrappers and pipeline commands a case puts around the tool under test.
# `program` does not name them, because `env` or `timeout` in front of every
# no-board command would say nothing about the fork's CLI -- but the option
# readers stop attributing options at one, so `timeout --foreground -k 2 -s
# INT` and `tail -n 20` are not read as options of the tool beside them.
WRAPPERS = frozenset({"env", "sh", "bash", "timeout", "python", "python3",
                      "tail", "cat", "tr", "date", "printf", "echo", "sleep",
                      "exit", "head", "true", "false"})
# What a placeholder in a Commands block stands for, per test: the options a
# collapsed form covers, so the forward check knows the block does document
# them. Written down here rather than assumed, so a block cannot hide an
# option behind a placeholder nobody declared.
COLLAPSED: dict[str, dict[tuple[str, str], set[tuple[str, str]]]] = {}
# Steps whose command the stand-in context cannot build, filled in as the
# cases are read and asserted below, so a step that starts dropping out of
# the comparison is a failure rather than a silence.
DROPPED: list[str] = []


def _collapsed(test, program_name, placeholder, *options):
    """Declare that ``placeholder``, on a line of ``test``'s Commands block
    that runs ``program_name``, stands for ``options``.

    The placeholder is part of the declaration so it can be checked: an
    entry has to point at a form the block really writes, not at any
    bracketed word the block happens to contain
    (test_every_collapsed_declaration_is_load_bearing).
    """
    COLLAPSED.setdefault(test, {}).setdefault(
        (program_name, placeholder), set()
    ).update((program_name, option) for option in options)


def collapsed_options(test) -> set[tuple[str, str]]:
    """Every (program, option) a test's placeholders stand for."""
    return set().union(*COLLAPSED.get(test, {}).values()) if test in COLLAPSED else set()


# An option word, in argv or in a Commands block: a dash and a letter, so a
# negative value (`-60`, `-120`), the `--` separator and a bare `-` are not
# read as options. A trailing placeholder is allowed, since that is how a
# block writes an option with a value (`-c<MHz>`, `-A<channel>`); a word
# whose option NAME is a placeholder (`-<mode>`, `-<query>`) is not an
# option here, and `COLLAPSED` says what it stands for.
OPTION = re.compile(r"^(?P<name>-{1,2}[A-Za-z][A-Za-z0-9-]*)")


# Only what a placeholder has to stand for: an option a block names, in a
# command or in a comment beside one, is not declared here, because an entry
# for one would excuse it if somebody took it out of the block
# (test_every_collapsed_declaration_is_load_bearing).
#
# `-<mode>`: the modes Util mode refusals pairs with `-i`.
_collapsed("Util mode refusals", "ubertooth-util", "-<mode>",
           "-I", "-e", "-n", "-t")
# `<refusal>`: the file option, the later value, the repeat or the stray
# word each refusal case writes after the tool.
_collapsed("Refused command lines", "ubertooth-btle", "<refusal>", "-A", "-a", "-f", "-h")
_collapsed("Refused command lines", "ubertooth-rx", "<refusal>", "-c", "-h", "-l", "-u", "-z")
_collapsed("Refused command lines", "ubertooth-dump", "<refusal>", "-U", "-h")
_collapsed("Refused command lines", "ubertooth-specan", "<refusal>", "-h", "-l", "-u")
_collapsed("Refused command lines", "ubertooth-dfu", "<refusal>", "-d", "-h")
_collapsed("Refused command lines", "ubertooth-follow", "<refusal>", "-e")
# The two DFU sections write every `dfuimage.py` form out, so nothing here
# stands for one of its options either.
# `<peer options>`: the ports, addresses, aliases and stream settings every
# peer session carries, per test, since each one holds the link differently.
# The document names all eleven in "How to read a section" and writes them
# out with values under `Capture known Classic device`.
_PEER = ("--acceptor", "--acceptor-address", "--acceptor-alias", "--initiator",
         "--initiator-address", "--initiator-alias", "--min-seconds",
         "--min-writes", "--payload", "--period", "--timeout")
for _test in ("Follow Classic Bluetooth piconet", "Classic capture record fields",
              "Recover AFH map", "Bluetooth inquiry scan",
              "Follow Classic piconet over HCI"):
    _collapsed(_test, "esp32peer.py", PEER_PLACEHOLDER, *_PEER)
# The two DK sections that once collapsed their role commands behind `...`
# write every one of them out, so nothing here stands for a `nrfdk.py`
# option.


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


def _context(case=None):
    """A stand-in for what an earlier step, and the run, recorded.

    The settings' own values, so a restore step that parses one (the LED
    triple, the channel in MHz) resolves; anything else becomes a visible
    placeholder.

    A case's optional artifacts are entries of the mapping itself rather
    than placeholders the lookup invents, because a step asks for one with
    ``context.get``, which never reaches ``__missing__``. Without them a
    conditional form would be collected as the command a run WITHOUT that
    artifact sends, so ``nrfdk.py ensure``'s ``--hex <image>`` would never
    be collected and the block could leave it out and still pass.
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

    present = Recorded()
    for label in getattr(case, "artifacts", ()) + getattr(
        case, "optional_artifacts", ()
    ):
        present[artifact_key(label)] = f"<{label}>"
    return present


# The harness's own driver modules, by file name: a Commands block writes
# one bare (`esp32peer.py stream ...`) and an argv carries its full path.
# Read from the package, so a driver added later is recognized.
HARNESS_MODULES = frozenset(
    path.name for path in (REPOSITORY / "ubertooth_harness").glob("*.py")
) | {"btle-extcap.py"}


def program(token: str) -> str:
    """The program a command-line word names, or "" for anything else.

    A wrapper is deliberately not a program here: ``env``, ``sh``, ``bash``,
    ``timeout`` and the interpreter are how a case runs the tool under test,
    and naming them in a section would say nothing about the fork's CLI. The
    file utilities a case sets its directory up with are programs, because
    what they make is what the tool then finds.
    """
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
    previous = shell = ""
    for word in argv:
        words.append(word)
        if word.rsplit("/", 1)[-1] in ("sh", "bash"):
            shell = word
        if shell and previous == "-c" and word:
            try:
                words.extend(shlex.split(word))
            except ValueError:
                words.extend(word.split())
            shell = ""
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
    previous = ""
    for word in _words(argv):
        if (word in ("|", ";", "&&", "||", "&")
                or word.rsplit("/", 1)[-1] in WRAPPERS):
            # A shell string's next command, or a wrapper: whatever follows
            # belongs to it, not to the tool before it (`... ubertooth-btle
            # -f | tail -n 20`, whose -n is tail's).
            current = previous = ""
            continue
        name = program(word)
        # A program name is the next command at the front of one -- after a
        # wrapper or a pipe -- and after `--`, which is how a driver gives
        # the command it wraps (`esp32peer.py stream ... -- ubertooth-rx
        # ...`). Anywhere else it is an argument of the command already
        # running: `btbrconsole.py ... --peer-reset esp32peer.py` as an
        # option's value, `ledblink.py ubertooth-util <serial>
        # --max-samples ...` as a positional. The options after it are that
        # command's, not the name's.
        if name and (not current or previous == "--"):
            current = name
            previous = word
            continue
        previous = word
        match = OPTION.match(word)
        if not current or not match:
            continue
        name = match.group("name").split("=", 1)[0]
        # A short option carries its value attached: `-c2426`, `-U<serial>`,
        # `-A37`. The option is its first letter.
        found.add((current, name if name.startswith("--") else name[:2]))
    return found


def programs_by_test():
    """Every program each test's cases run, from the catalog's own cases."""
    catalog = builtin_catalog()
    used: dict[str, set[str]] = {}
    options: dict[str, set[tuple[str, str]]] = {}
    # The programs and the options a test runs from a state-changing case,
    # which are the only ones the shared transition commands can stand in
    # for: an allowance that followed the test would excuse a program or an
    # option a case of another kind sends.
    stateful: dict[str, set[tuple[str, str]]] = {}
    stateful_programs: dict[str, set[str]] = {}
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
                ran = set(filter(None, (program(w) for w in _words(argv))))
                found.update(ran)
                passed = _options(argv)
                options.setdefault(invocation.test_name, set()).update(passed)
                if isinstance(case, StateCase):
                    stateful.setdefault(invocation.test_name, set()).update(
                        passed)
                    stateful_programs.setdefault(
                        invocation.test_name, set()).update(ran)
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
    return used, options, stateful, stateful_programs, skipped


def _argvs(case):
    if isinstance(case, CommandCase):
        return [tuple(case.argv)]
    if isinstance(case, FlashCase):
        return []
    assert isinstance(case, StateCase)
    out = []
    for step in tuple(case.steps) + tuple(case.cleanup):
        try:
            out.append(step.resolve_argv(_context(case)))
        except (ValueError, LookupError) as error:
            # A command built from a value only a run records, which the
            # stand-in context cannot produce: `Two-device range test`'s two
            # LED restores, which parse a per-board triple. Counted, so a
            # step that starts dropping out is visible.
            DROPPED.append(f"{case.case_id}/{step.step_id}: {error}")
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


def shared_fences(body: str) -> dict[str, list[str]]:
    """The two fenced blocks of "What every test shares", by what they are.

    ``transition`` holds the commands every state-changing case sends, which
    is what a section may leave out. ``preflight`` holds the run-level reads
    the harness makes before any test, which a section may NOT leave out:
    they are a different thing from the shared cleanup, and treating them as
    an allowance excused `ubertooth-util -v`, `-V`, `-H` and `-B` for every
    test at once.
    """
    def after(heading):
        text = body.split(heading, 1)
        assert len(text) == 2, heading
        # Up to the next bold paragraph, so one block's fence cannot be
        # read as another's.
        return fences(re.split(r"\n\*\*", text[1])[0])

    found = {"preflight": after("**Mandatory preflight.**"),
             "transition": after("**Transition cases.**")}
    for name, lines in found.items():
        assert lines, f"no fenced block for {name}"
    return found


def commands_of(body: str) -> list[str]:
    """The lines of every fenced block in the **Commands** field of a body."""
    field = body.split("**Commands.**", 1)
    if len(field) == 1:
        return []
    return fences(field[1].split("**Expected outcome.**", 1)[0])


def _fence_words(lines):
    """Every word of a Commands block, a comment's words included.

    A comment in a fence says which other forms the line stands for ("# and
    -A40, -k 2"), so it is command text and is held to the same standard: a
    program or an option written there is compared like any other. Reading
    it is what catches an option named only in a comment, which was 84
    tokens across 18 sections when the comments were skipped.
    """
    for line in lines:
        for word in line.replace("|", " ").replace("#", " ").split():
            yield word.strip(",;`.)([]")


def programs_named(lines) -> set[str]:
    """The programs the lines of a Commands block name."""
    return {name for name in (program(word) for word in _fence_words(lines))
            if name}


def options_named(lines) -> set[tuple[str, str]]:
    """(program, option) for every option a Commands block names.

    A word with a placeholder in it stands for a set of real options
    (`-<mode>`, `-<query>`, `-<setter or mode>`) and is passed over: which
    options it covers is what the prose beside the block says.
    """
    found = set()
    current = ""
    continued = False
    for line in lines:
        # A line of its own starts a command; a line continued from the one
        # before (that one ended in a backslash) carries its program on.
        if not continued:
            current = ""
        continued = line.rstrip().endswith("\\")
        previous = ""
        for word in _fence_words([line]):
            if word.rsplit("/", 1)[-1] in WRAPPERS:
                current = previous = ""
                continue
            name = program(word)
            # The same rule as `_options`: a program name is the next
            # command at the front of one and after `--`, and anywhere else
            # it is an argument of the command already running.
            if name and (not current or previous == "--"):
                current, previous = name, word
                continue
            previous = word
            match = OPTION.match(word)
            if not current or not match:
                continue
            name = match.group("name").split("=", 1)[0]
            found.add((current, name if name.startswith("--") else name[:2]))
    return found


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
        (cls.used, cls.options, cls.stateful, cls.stateful_programs,
         cls.skipped) = programs_by_test()
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

    def test_only_the_known_steps_drop_out_of_the_comparison(self):
        # A step whose command the stand-in context cannot build is left out
        # of both option directions, so which ones they are is asserted
        # rather than counted silently.
        self.assertEqual(
            ["round-trip/restore-responder-leds",
             "round-trip/restore-sender-leds"],
            sorted(item.split(":")[0] for item in set(DROPPED)),
            DROPPED,
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
        shared = programs_named(
            shared_fences(self.sections[SHARED])["transition"]
        )
        self.assertEqual({"ubertooth-util", "ubertooth-btle"}, shared)
        for name, used in sorted(self.used.items()):
            # The allowance follows the case, as it does for options: a
            # program this test runs from a case of another kind has to be
            # named, even in a test that has state-changing cases too.
            allowed = shared & self.stateful_programs.get(name, set())
            with self.subTest(name):
                undocumented = sorted(
                    item for item in used
                    if item not in self.named[name] and item not in allowed
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

    def test_every_driver_exemption_is_necessary(self):
        # BY_A_DRIVER excuses a program from both directions for one test,
        # which is only honest where no argv of that test carries it: an
        # entry for a program the test does run would turn the comparison
        # off for something the checks could have made.
        for name, programs in sorted(BY_A_DRIVER.items()):
            with self.subTest(name):
                self.assertEqual(
                    [], sorted(programs & self.used.get(name, set())),
                    f"{name} exempts a program its own commands run",
                )
                # And it has to be used: an entry for a program the block
                # does not name excuses nothing and reads as if it did.
                self.assertEqual(
                    [], sorted(programs - self.named[name]),
                    f"{name} exempts a program its block does not name",
                )

    def test_the_shared_placeholder_paragraph_names_every_option_it_covers(self):
        """`<peer options>` is the one placeholder several sections share, so
        what it stands for is stated once, in "How to read a section", rather
        than in each of them. That paragraph is the only statement of those
        options a reader of the document gets, so it is compared with the
        ``COLLAPSED`` declarations that excuse them: an option added to or
        taken out of the peer session fails here until the paragraph says so.
        """
        declared = {option for entries in COLLAPSED.values()
                    for (prog, placeholder), options in entries.items()
                    if placeholder == PEER_PLACEHOLDER
                    for _prog, option in options}
        self.assertTrue(declared, "no test collapses the peer session options")
        paragraph = next(
            (part for part in self.sections[HOW_TO_READ].split("\n\n")
             if PEER_PLACEHOLDER in part),
            "",
        )
        self.assertTrue(
            paragraph,
            f'"{HOW_TO_READ}" must have a paragraph defining {PEER_PLACEHOLDER}',
        )
        named = {word.strip(",.`") for word in paragraph.split()
                 if word.strip(",.`").startswith("--")}
        self.assertEqual(
            sorted(declared), sorted(named),
            f"the {PEER_PLACEHOLDER} paragraph and what the blocks collapse "
            "behind it have drifted apart",
        )

    def test_every_collapsed_declaration_is_load_bearing(self):
        """COLLAPSED says what a placeholder in a block stands for, which
        excuses those options from the forward check, so every entry has to
        earn its place three ways: the test really passes that option, its
        block names that program, and the block does not already name the
        option literally. The last one matters most: an entry for an option
        the block spells out would excuse it if somebody deleted it from the
        block later, which is the drift this check is for. And a block that
        collapses anything has to write a placeholder, or there is nothing
        for the entry to describe.
        """
        shared = options_named(
            shared_fences(self.sections[SHARED])["transition"]
        )
        for name, placeholders in sorted(COLLAPSED.items()):
            passed = self.options.get(name, set())
            documented = self.named_options[name] | (
                shared & self.stateful.get(name, set()))
            lines = commands_of(self.sections[name])
            for (program_name, placeholder), declared in sorted(
                placeholders.items()
            ):
                with self.subTest(name, program=program_name,
                                  placeholder=placeholder):
                    self.assertEqual(
                        [], sorted(f"{prog} {option}"
                                   for prog, option in declared
                                   if (prog, option) not in passed),
                        f"{name} declares collapsed options it never passes",
                    )
                    self.assertEqual(
                        [], sorted(f"{prog} {option}"
                                   for prog, option in declared
                                   if (prog, option) in documented),
                        f"{name} declares collapsed options already documented",
                    )
                    self.assertLessEqual(
                        {prog for prog, _option in declared}, self.named[name],
                        f"{name} declares a collapsed form for a program its "
                        "block does not name",
                    )
                    # The declaration points at a form the block writes:
                    # a line that runs THIS program and carries THIS
                    # placeholder, so one tool's `<refusal>` line cannot
                    # stand in for another tool's declaration.
                    self.assertTrue(
                        [line for line in lines
                         if placeholder in line
                         and program_name in programs_named([line])],
                        f"{name}: no line runs {program_name} with "
                        f"{placeholder!r} on it",
                    )

    def test_every_section_names_at_least_one_command(self):
        # Otherwise the forward checks pass on an empty block: for a test
        # whose only programs are the shared ones, the allowance above would
        # satisfy it whatever the block held (including nothing).
        for name in self.catalog.names:
            if not self.catalog.get(name).implemented:
                continue
            with self.subTest(name):
                self.assertTrue(
                    self.named[name],
                    f"{name}'s Commands block names no program",
                )

    def test_every_option_a_test_passes_is_in_its_commands_block(self):
        """And the other direction for options: an option a case passes must
        be named, so a case that gains one is documented or fails here.

        This is the drift the issue is about: a case gaining `-e` or `-q` on
        a tool its block already names. The shared fences stand in for the
        commands every transition case sends, and a form the block collapses
        covers the options its prose names, which is why `COLLAPSED` exists:
        each entry is a placeholder the block writes and the options it
        stands for, so the stand-in is written down rather than assumed.
        """
        shared = options_named(
            shared_fences(self.sections[SHARED])["transition"]
        )
        for name in self.catalog.names:
            passed = self.options.get(name, set())
            # The shared transition commands stand in for what a
            # state-changing case sends, so the allowance follows the case
            # rather than the test: an option this test passes from a case
            # of another kind has to be named, even in a test that has
            # state-changing cases too.
            allowed = shared & self.stateful.get(name, set())
            documented = (self.named_options[name] | allowed
                          | collapsed_options(name))
            by_a_driver = BY_A_DRIVER.get(name, set())
            with self.subTest(name):
                undocumented = sorted(
                    f"{prog} {option}"
                    for prog, option in passed
                    if (prog, option) not in documented
                    and prog not in by_a_driver
                )
                self.assertEqual(
                    [], undocumented,
                    f"{name} passes these and its Commands block does not name them",
                )

    def test_the_shared_block_names_only_commands_some_test_sends(self):
        # The transition fence stands in for ~40 sections, so it carries the
        # most weight and needs the same backward check: a program or an
        # option written there that no test sends would excuse itself
        # everywhere. The preflight fence is not here: no case sends one of
        # its commands, so it is compared with preflight.py instead, in both
        # directions (test_the_preflight_fence_matches_the_code...).
        every_program = set().union(*self.used.values())
        every_option = set().union(*self.options.values())
        lines = shared_fences(self.sections[SHARED])["transition"]
        self.assertEqual([], sorted(programs_named(lines) - every_program))
        self.assertEqual(
            [],
            sorted(f"{prog} {option}"
                   for prog, option in options_named(lines)
                   if (prog, option) not in every_option),
            "the shared block names an option no test passes",
        )

    def test_the_preflight_fence_matches_the_code_that_builds_preflight(self):
        """The preflight block stands in for every run's pre-test commands,
        and no catalog case sends one of them, so the checks above cannot
        reach it: the backward check alone would let a read added to or
        removed from `preflight.py` pass unremarked.

        So it is compared with the code both ways, like the two fences
        below: every program and option preflight builds must be in the
        block, and every one the block names must be one preflight builds.
        """
        from ubertooth_harness import bootloader, preflight

        serial, revision = "abcdef00" * 4, "<revision>"
        argvs = [preflight.HOST_VERSION_ARGV,
                 preflight.HOST_VERSION_FALLBACK_ARGV,
                 preflight.guard_api_check(serial)[1],
                 bootloader.identity_argv(serial)]
        argvs += [argv for _name, argv, _patterns
                  in preflight.device_checks(serial, revision)]
        # The two device drivers, whose commands are built from a registered
        # device's own identity.
        with tempfile.TemporaryDirectory() as directory:
            registry = _registry(directory)
            for alias, build in (("DK_Linux0", preflight._dk_check),
                                 ("ESP32_Linux0", preflight._peer_check)):
                resource = registry.get(alias)
                argvs.append(build(1, "Host_Linux", resource, revision).argv)

        built = set()
        programs = set()
        for argv in argvs:
            built |= _options(argv)
            programs |= set(filter(None, (program(w) for w in _words(argv))))
        lines = shared_fences(self.sections[SHARED])["preflight"]
        named = options_named(lines)
        self.assertEqual(
            [], sorted(f"{prog} {option}" for prog, option in built - named),
            "preflight sends these and the preflight block does not name them",
        )
        self.assertEqual(
            [], sorted(f"{prog} {option}" for prog, option in named - built),
            "the preflight block names an option preflight does not send",
        )
        self.assertEqual([], sorted(programs - programs_named(lines)))
        self.assertEqual([], sorted(programs_named(lines) - programs))

    def test_the_two_fences_no_case_argv_covers_match_the_code(self):
        """`Bootloader identity` and `Flash reliability` document commands no
        plan step builds, so the checks above cannot reach them: one is a
        preflight read and a programmer write, the other is argv the flash
        runner assembles per write. Their fences are compared with the code
        that builds those commands instead, which is the whole reason they
        are allowed in ``BY_A_DRIVER`` and ``UNCOLLECTED``.

        Both ways round, per fence: every option that code passes must be in
        the fence, and every option the fence names must be one that code
        passes. The programmer write is compared word for word as well, so a
        wrong baud or crystal fails and not only a wrong option.
        """
        from ubertooth_harness import bootloader, flash, watchdog

        serial, image = "<serial>", "<image>"
        built = {
            "Bootloader identity": (
                bootloader.identity_argv(serial),
                bootloader.program_argv("<bootloader.hex>", "<serial device>"),
                bootloader.application_argv(serial),
            ),
            # flash.py: the write, the revision read of every board, and the
            # watchdog read between writes (watchdog.py).
            "Flash reliability": (
                flash.flash_argv(serial, image),
                flash.revision_argv(serial),
                watchdog.record_argv(serial),
            ),
        }
        for name, commands in built.items():
            lines = commands_of(self.sections[name])
            named = options_named(lines)
            passes = set()
            for argv in commands:
                passes |= _options(tuple(str(word) for word in argv))
            with self.subTest(name):
                self.assertEqual([], sorted(f"{p} {o}" for p, o in passes
                                            if (p, o) not in named),
                                 f"{name}'s block does not name these")
                self.assertEqual([], sorted(f"{p} {o}" for p, o in named
                                            if (p, o) not in passes),
                                 f"{name}'s block names these and nothing runs them")
        fence = " ".join(commands_of(self.sections["Bootloader identity"]))
        for word in bootloader.program_argv("<bootloader.hex>", "<serial device>")[1:]:
            if not str(word).startswith("<"):
                self.assertIn(str(word), fence)

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
