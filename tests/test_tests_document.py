"""`docs/tests.md` against the catalog: one section per test, and every
executable a test really runs named in that section's **Commands** block.

No hardware, no network, nothing built: the document is read from the
repository, and the commands are the ones the catalog compiles for the
example bench, which is the same code a run builds its cases with. So this
is not a second catalog -- there is nothing here to keep in step with
``catalog.py`` -- it is the catalog asked what it would run and the document
asked whether it says so (#347).

It is checked both ways round. Forward: every executable a test's cases run
is named in its **Commands** block, unless that command is one the shared
transition block states. Backward: every executable a **Commands** block
names is one that test runs, so a command form nobody sends cannot sit there
looking authoritative. Only the fenced blocks are read, never the prose
around them: a tool mentioned in a sentence is a cross-reference, not a
command this test sends.

The shared allowance is per command, not per test and not per case: a
command is the shared block's only when its step is one the transitions
helpers built AND the command is the one that step should send
(``is_shared_command``). So a section's own setter must be documented even
where the shared restore passes the same option with another value, which
the option names alone cannot tell apart; and so must a read a section
makes for a check of its own, even though the shared block states the same
command, because the section is what says why it reads it there. The one
command recognized by its form alone is the idle stop, whose step ID is its
caller's to choose.

All three shared blocks are compared with the code that builds their
commands, in both directions: the transition block against what
``is_shared_command`` classifies as shared, which is derived from the
settings themselves, and preflight's reads and the guard reads against
``preflight.py`` and ``transitions.GUARD_READBACK``. The last two are no
allowance at all, because no case sends one of those commands; the
transition block is the allowance, and comparing it is what makes its
content mean something.

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
  and the document says so too, in the prose beside the block. Only one of
  those statements is compared with anything: ``<peer options>``, which
  five sections share, is defined in "How to read a section" and checked
  against the declarations
  (test_the_shared_placeholder_paragraph_names_every_option_it_covers).
  The ``-<mode>`` and ``<refusal>`` legends are prose, so deleting one
  fails nothing;
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
_collapsed("Refused command lines", "ubertooth-btle", "<refusal>", "-a", "-f", "-h")
_collapsed("Refused command lines", "ubertooth-rx", "<refusal>", "-h", "-l", "-u", "-z")
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
    # Every nRF52840 DK in the example bench leaves `serial device` out, so
    # the driver derives the VCOM0 path from the J-Link serial. A registry
    # may name one, and then every `nrfdk.py` command, preflight's included,
    # carries `--port <path>`. That is a conditional form like `ensure`'s
    # `--hex`, and a registry without it would never produce it, so this
    # copy names one: the document has to state the fuller command.
    ports = [0]

    def _port(match):
        # One path per DK: two resources naming one path would be two
        # resources for one device.
        ports[0] += 1
        return (f"{match.group(1)}jlink serial: {match.group(2)}\n"
                f"{match.group(1)}serial device: "
                f"/dev/serial/by-id/dk-vcom0-{ports[0]}")

    text = re.sub(r"(?m)^(\s*)jlink serial: (.*)$", _port, text)
    assert ports[0] >= 2, (
        "the example registry must register at least two nRF52840 DKs for "
        f"this check to reach the connection tests, found {ports[0]}"
    )
    for index, placeholder in enumerate(
        dict.fromkeys(re.findall(r"REPLACE_WITH_[A-Z_]+", text))
    ):
        # Letters as well as digits, so YAML reads it as a string and
        # selection by serial accepts it.
        text = text.replace(placeholder, f"abcdef{index:02x}" * 4)
    path = Path(directory) / "hardware.yaml"
    path.write_text(text, encoding="utf-8")
    registry = FileResourceRegistry.load(path)
    # The registry is what says the injection worked: a DK that already
    # named a port would have left a duplicate YAML key, which the loader
    # resolves silently to one of the two.
    paths = [resource.identity.get("serial device")
             for resource in registry.resources.values()
             if resource.type == "nRF52840 DK"]
    assert len(paths) == ports[0] and len(set(paths)) == ports[0], (
        f"each DK needs a serial device of its own, got {paths}"
    )
    return registry


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


# What the shared transition block states, derived from the transitions
# helpers that build those commands rather than written out again: per
# setting, the query `Setting.read_original` and `Setting.verify` send and
# the set options `Setting.restore` sends; the idle stop (`_stop`); the
# serial read that confirms the board (`_identity`); the firmware watchdog
# record; and the CRC pair. So a setting added to the harness, or one whose
# query option changes, moves this with it.
#
# A step ID alone is not enough to recognize one, because the shared squelch
# restore and a squelch setter a section is about are the same command but
# for the value, which no option name shows. So the ID says which shared
# command the step should be and the command is then held to it: a
# `read-original-channel` step that sends `-c2426` is a setter whatever it
# is called.
UTIL, BTLE = "ubertooth-util", "ubertooth-btle"
_SHARED_SETTINGS = (
    transitions.CHANNEL, transitions.CHANNEL_INDEX, transitions.PA_LEVEL,
    transitions.SQUELCH, transitions.USR_LED, transitions.LEDS,
)


def _set_option_names(setting) -> frozenset:
    """The option names a setting's restore passes, `-d` and `-l` for the
    LED triple and one option for the rest."""
    value = setting.after_mode_stop or (
        setting.candidates[0] if setting.candidates else "0")
    return frozenset(
        (UTIL, word[:2]) for word in setting.set_options(value)
        if word.startswith("-")
    )


# A shared command is either a bare query, where the words after the
# selector must be exactly those options with no value (so `-z` is the
# shared squelch read and `-z-60` is not), or a write, where each word
# carries its value attached and only the option names are compared,
# because the value is whatever the run recorded.
QUERY, WRITE = "query", "write"


def _shared_commands() -> dict:
    """What each shared step ID family must send: ``(kind, options)``."""
    found = {
        "stop": (QUERY, ((UTIL, "-S"),)),
        "cleanup-stop": (QUERY, ((UTIL, "-S"),)),
        "restored-device-identity": (QUERY, ((UTIL, "-s"),)),
        "read-original-watchdog": (QUERY, ((UTIL, "-w"),)),
        "read-original-crc": (QUERY, ((BTLE, "-k"),)),
        "verify-restored-crc": (QUERY, ((BTLE, "-k"),)),
        "restore-crc": (WRITE, frozenset({(BTLE, "-k")})),
    }
    for setting in _SHARED_SETTINGS:
        query = (QUERY, ((UTIL, setting.query),))
        found[f"read-original-{setting.key}"] = query
        found[f"verify-restored-{setting.key}"] = query
        found[f"restore-{setting.key}"] = (WRITE, _set_option_names(setting))
    return found


SHARED_COMMANDS = _shared_commands()
# The read-only commands among them. Each is placed by its step ID like any
# other, with one exception: `_stop`'s step ID is its caller's to choose, so
# the idle stop turns up as `establish-idle`, `mode-stop` and `idle-stop`
# and is the one command `is_shared_command` recognizes by its form alone.
SHARED_QUERIES = frozenset(
    options[0] for kind, options in SHARED_COMMANDS.values() if kind == QUERY
)
# Every form the shared block therefore has to state, and no other:
# (program, option, QUERY or WRITE). The kind is part of it because an
# option name alone cannot tell a read from a write, and the block states
# both for most settings: `-C` reads the channel in MHz and `-C<index>`
# restores it by index, so a comparison per name let the read stand in for
# the write and the channel-index restore went unstated.
SHARED_FORMS = frozenset(
    {(prog, option, QUERY) for prog, option in SHARED_QUERIES}
    | {(prog, option, WRITE)
       for kind, options in SHARED_COMMANDS.values() if kind == WRITE
       for prog, option in options}
)
# The BLE access address is read and restored the same way, and its step
# IDs are `read-original-access-address` / `restore-access-address`. It is
# deliberately absent: the shared block states no `ubertooth-btle -a`
# command, so calling those steps shared would excuse an option the reader
# of that block never sees. `BLE settings` documents its own `-a` forms.

# Every shared command is one of these two tools naming one board by its
# full serial. Both halves are load-bearing. Without the program, a step ID
# ending in `-stop` excused whatever it ran, and `advertiser-stop`, which
# runs `nrfdk.py stop`, is one. Without the serial, `ubertooth-util -U all
# -s` read as the shared per-board read, and the selector is what
# `Multi-board selection` is about.
SHARED_TOOLS = frozenset({UTIL, BTLE})
SERIAL = re.compile(r"[0-9a-fA-F]{32}")


def shared_expectation(step_id: str, roles=()):
    """``(kind, options)`` a step with this ID must send to be the shared
    command, or None when the ID names no shared command.

    A test that drives two boards works the role into the ID, before it
    (`sender-restored-device-identity`) or inside it
    (`read-original-sender-squelch`), so the ID is read whole and again
    with the role taken out. ``roles`` is what the invocation binds, so
    only a word that is one of ITS roles may be taken out: stripping any
    word let a section's own step pass under a borrowed name, and
    `read-original-debug-channel` is the one a reviewer found.
    """
    if not step_id:
        return None
    candidates = [step_id]
    words = {role.lower().replace(" ", "-") for role in roles}
    for role in words:
        if step_id.startswith(f"{role}-"):
            candidates.append(step_id[len(role) + 1:])
    for candidate in tuple(candidates):
        for verb in ("read-original-", "restore-", "verify-restored-"):
            if not candidate.startswith(verb):
                continue
            setting = candidate[len(verb):]
            for role in words:
                if setting.startswith(f"{role}-"):
                    candidates.append(verb + setting[len(role) + 1:])
    for candidate in candidates:
        if candidate in SHARED_COMMANDS:
            return SHARED_COMMANDS[candidate]
    return None


def is_shared_command(step_id: str, argv, roles=()) -> bool:
    """Is this command one the shared transition block states?

    Only a step of a state-changing case can be: the shared block states
    what a transition case sends, and the transitions helpers that build
    those commands build steps. A ``CommandCase`` has no step, so it is its
    section's own however close its command comes, which is what keeps
    `Ubertooth identity` documenting the reads it is about.
    """
    words = list(argv)
    if not step_id or not words:
        return False
    tool = program(words[0])
    if tool not in SHARED_TOOLS:
        return False
    if len(words) < 4 or words[1] != "-U" or not SERIAL.fullmatch(words[2]):
        return False
    expectation = shared_expectation(step_id, roles)
    if expectation is not None:
        # The ID says which shared command this is, so the command has to
        # be it.
        kind, options = expectation
        rest = words[3:]
        if kind == QUERY:
            # Exactly those bare options: a value makes it a write, and a
            # write is not what a read step sends.
            return tuple((tool, word) for word in rest) == options
        return (_options(argv) == set(options) | {(tool, "-U")}
                and all(len(word) > 2 and word.startswith("-")
                        for word in rest))
    # Otherwise only the idle stop, which is the one shared command whose
    # step ID its caller chooses (`_stop`), so it turns up as
    # `establish-idle`, `mode-stop` and `idle-stop`. Every other shared
    # command comes from a helper that names its own step, so the ID can be
    # required: a read a section makes for a check of its own is that
    # section's to document, even though the shared block states the same
    # command (review round 15).
    return len(words) == 4 and (tool, words[3]) == (UTIL, "-S")


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
    # The programs and the options a test runs from a command the shared
    # transition block does NOT state, which are its section's to document.
    # The allowance is per command, by the step ID the transitions helpers
    # give it, so an option this test also sends from a command of its own
    # is not excused by the shared block having a command that passes the
    # same option with another value.
    own: dict[str, set[tuple[str, str]]] = {}
    own_programs: dict[str, set[str]] = {}
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
            for step_id, argv in _argvs(case):
                ran = set(filter(None, (program(w) for w in _words(argv))))
                found.update(ran)
                passed = _options(argv)
                options.setdefault(invocation.test_name, set()).update(passed)
                if not is_shared_command(step_id, argv,
                                         invocation.resources):
                    own.setdefault(invocation.test_name, set()).update(passed)
                    own_programs.setdefault(
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
            # These are plain command cases, so every command is the test's
            # own: `own` and `own_programs` have to be filled here too, or
            # the two forward checks compare an empty set and a section can
            # say anything (review round 13).
            for step_id, argv in _argvs(case):
                ran = set(filter(None, (program(w) for w in _words(argv))))
                found.update(ran)
                passed = _options(argv)
                options.setdefault(name, set()).update(passed)
                if not is_shared_command(step_id, argv):
                    own.setdefault(name, set()).update(passed)
                    own_programs.setdefault(name, set()).update(ran)
    return used, options, own, own_programs, skipped


def _argvs(case):
    """``(step ID, argv)`` for every command a case sends.

    A ``CommandCase`` is one command and no step, so its ID is empty, and
    ``is_shared_command`` takes nothing without a step as shared: its
    options are its section's to document.
    """
    if isinstance(case, CommandCase):
        return [("", tuple(case.argv))]
    if isinstance(case, FlashCase):
        return []
    assert isinstance(case, StateCase)
    out = []
    for step in tuple(case.steps) + tuple(case.cleanup):
        try:
            out.append((step.step_id, step.resolve_argv(_context(case))))
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
    the harness makes before any test and ``guard`` the reads a guarded test
    makes of every other board; neither is an allowance a section may draw
    on, because they are not the shared cleanup. Treating preflight as one
    excused `ubertooth-util -v`, `-V`, `-H` and `-B` for every test at once.
    Each is compared with the code that builds it instead, in both
    directions.
    """
    def after(heading):
        text = body.split(heading, 1)
        assert len(text) == 2, heading
        # Up to the next bold paragraph, so one block's fence cannot be
        # read as another's.
        return fences(re.split(r"\n\*\*", text[1])[0])

    found = {"preflight": after("**Mandatory preflight.**"),
             "transition": after("**Transition cases.**"),
             "guard": after("**Guard Ubertooths.**")}
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
            # A colon too: `# -c: restore the channel` would otherwise
            # read as `-c:`, a form of `-c` carrying a value, which is one
            # way a comment could satisfy the per-form comparison of the
            # transition block for a line somebody had deleted.
            yield word.strip(",;`.:)([]")


def programs_named(lines) -> set[str]:
    """The programs the lines of a Commands block name."""
    return {name for name in (program(word) for word in _fence_words(lines))
            if name}


def forms_named(lines) -> set[tuple[str, str, str]]:
    """(program, option, QUERY or WRITE) for every form a block states.

    A word that is the option alone is a read; one that carries anything
    after the option name, a value or a placeholder for one, is a write. So
    `-C` and `-C<index>` are two forms of one option, which is what the
    shared transition block states for most of its settings. The selector
    is left out: `-U <serial>` names the board for both.
    """
    found = set()
    for program_name, option, word in _option_words(lines):
        if option == "-U":
            continue
        rest = word[len(option):]
        found.add((program_name, option,
                   WRITE if rest and not rest.startswith("=") else QUERY))
    return found


def options_named(lines) -> set[tuple[str, str]]:
    """(program, option) for every option a Commands block names.

    A word with a placeholder in it stands for a set of real options
    (`-<mode>`, `-<query>`, `-<setter or mode>`) and is passed over: which
    options it covers is what the prose beside the block says.
    """
    return {(program_name, option)
            for program_name, option, _word in _option_words(lines)}


def _commands(lines):
    """The commands a fenced block states, one list of words each.

    A line that is a continuation (the line before ended in a backslash) or
    only a comment belongs to the command above it. Comment text is cut
    here, unlike everywhere else in this module: a comment states further
    FORMS of the command beside it, which is the right thing to read when
    comparing option sets and the wrong thing when comparing one command.
    """
    found: list[list[str]] = []
    continued = False
    for line in lines:
        text = line.split("#", 1)[0]
        starts = (not continued and line.strip()
                  and not line.strip().startswith("#"))
        continued = text.rstrip().endswith("\\")
        words = text.replace("\\", " ").split()
        if starts or not found:
            found.append([])
        found[-1].extend(word.strip(",;`.:)([]") for word in words)
    return [words for words in found if words]


def command_forms(lines) -> set[tuple[str, tuple[str, ...]]]:
    """(program, the option names and subcommand it is given) per command.

    Values are dropped, so `-U <serial> -v` and `--jlink <serial>` compare
    as `-U -v` and `--jlink`; a word that is not an option is kept only
    where a driver takes it as a subcommand, directly after the program.
    Two lines that differ only in their comment therefore compare as one
    command, which is what the preflight block's two `-v` reads are.
    """
    found = set()
    for words in _commands(lines):
        # The program is the first word that names one, so the interpreter
        # in front of a harness driver's path is passed over exactly as a
        # reader passes over it in the block.
        start = next((index for index, word in enumerate(words)
                      if program(word)), None)
        if start is None:
            continue
        kept = []
        for offset, word in enumerate(words[start + 1:]):
            match = OPTION.match(word)
            if match:
                option = match.group("name").split("=", 1)[0]
                kept.append(option if option.startswith("--") else option[:2])
            elif offset == 0:
                kept.append(word)
        found.add((program(words[start]), tuple(kept)))
    return found


def _option_words(lines):
    """(program, option name, the whole word) for each option a block names."""
    found = []
    current = ""
    continued = False
    for line in lines:
        # A line of its own starts a command; a line continued from the one
        # before (that one ended in a backslash) carries its program on,
        # and so does a line that is only a comment, which is how a block
        # says which other forms the line above stands for ("# and -A40").
        # Resetting there counted an option on such a line in neither
        # direction, which is where a fence states its extra forms.
        if not continued and line.strip() and not line.strip().startswith("#"):
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
            found.append((current,
                          name if name.startswith("--") else name[:2],
                          word))
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
        (cls.used, cls.options, cls.own, cls.own_programs,
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
            "ble.yaml:ble-weak-link", "ble.yaml:connection-weak-link",
            "classic.yaml:classic-weak-link",
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
        # fenced blocks are the preflight reads, the guard reads, and the
        # commands every transition case sends, and only those blocks count
        # here, never the prose around them. The transition block is the
        # only one a section may leave out, and only for the commands it
        # really states: `own_programs` is what this test runs from a
        # command of its own.
        self.assertEqual(
            {"ubertooth-util", "ubertooth-btle"},
            programs_named(shared_fences(self.sections[SHARED])["transition"]),
        )
        for name in sorted(self.used):
            with self.subTest(name):
                undocumented = sorted(
                    item for item in self.own_programs.get(name, set())
                    if item not in self.named[name]
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
        named = {word for word in (item.strip(",;`.:)([]") for item in
                                   paragraph.split())
                 if word.startswith("--")}
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
            documented = self.named_options[name]
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
        for name in self.catalog.names:
            # The shared transition block stands in for the commands it
            # states and nothing else, so what is compared here is what the
            # test passes from a command of its own. An option it also
            # sends from a shared command is still its own to document: the
            # shared squelch restore and a squelch setter a section is
            # about are both `ubertooth-util -z<dBm>`, and only the value
            # tells them apart.
            passed = self.own.get(name, set())
            documented = self.named_options[name] | collapsed_options(name)
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

    def test_the_transition_fence_matches_the_commands_it_stands_in_for(self):
        """The transition block is the one allowance the forward checks
        grant, standing in for about forty sections, so it needs the same
        comparison with code the other two shared blocks have.

        What it has to state is exactly what `is_shared_command` classifies
        as shared, and that is derived from the transitions helpers
        themselves (``SHARED_OPTIONS``). Without this the block's content
        constrained nothing: its lines could be deleted, or an option
        letter changed, with every other check still passing, because the
        classification never read the block.
        """
        lines = shared_fences(self.sections[SHARED])["transition"]
        named = forms_named(lines)

        def described(forms):
            return sorted(f"{prog} {option}"
                          f"{'<value>' if kind == WRITE else ''}"
                          for prog, option, kind in forms)

        self.assertEqual(
            [], described(SHARED_FORMS - named),
            "the transition block leaves out a command it stands in for",
        )
        self.assertEqual(
            [], described(named - SHARED_FORMS),
            "the transition block states a form it does not stand in for",
        )
        self.assertEqual(SHARED_TOOLS, programs_named(lines))

    def test_the_guard_fence_matches_the_code_that_builds_the_guard_reads(self):
        """The guard reads are the other shared command set no case argv
        carries: `transitions.guard_steps` builds them outside
        ``command_cases``, so the checks above cannot reach them either.

        They are a table in the code, so the block is compared with that
        table: a read added to or removed from ``GUARD_READBACK`` fails here
        until the block says so, and a read the block invents fails too.
        """
        built = {("ubertooth-util", option)
                 for _key, option, *_rest in transitions.GUARD_READBACK}
        # The CRC read, which is the one made with the other tool and comes
        # last (_guard_crc_step), and the selector every read carries,
        # since a guard is always named by serial.
        built |= {("ubertooth-btle", "-k"), ("ubertooth-btle", "-U"),
                  ("ubertooth-util", "-U")}
        lines = shared_fences(self.sections[SHARED])["guard"]
        named = options_named(lines)
        self.assertEqual(
            [], sorted(f"{prog} {option}" for prog, option in built - named),
            "a guard read is missing from the guard block",
        )
        self.assertEqual(
            [], sorted(f"{prog} {option}" for prog, option in named - built),
            "the guard block names a read the guard check does not make",
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

        built = command_forms(
            [" ".join(argv).replace("#", "") for argv in argvs]
        )
        programs = set()
        for argv in argvs:
            programs |= set(filter(None, (program(w) for w in _words(argv))))
        lines = shared_fences(self.sections[SHARED])["preflight"]
        named = command_forms(lines)

        def described(forms):
            return sorted(f"{prog} {' '.join(words)}" for prog, words in forms)

        # Per command, not per option: the distinguishing part of a
        # preflight read is its selector rather than a value, so comparing
        # option names let one line of the block stand in for another and
        # four of its seven lines could be deleted unremarked.
        self.assertEqual(
            [], described(built - named),
            "preflight sends these and the preflight block does not state them",
        )
        self.assertEqual(
            [], described(named - built),
            "the preflight block states a command preflight does not send",
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

    def test_what_the_shared_transition_block_stands_in_for(self):
        """`is_shared_command` decides which commands a section may leave
        out, so it is the loophole to watch: every widening of it excuses a
        real command form somewhere. These are the cases reviewers found.
        """
        serial = "abcdef00" * 4
        shared = [
            # The idle stop, under each of the names the cases give it.
            ("stop", ("ubertooth-util", "-U", serial, "-S")),
            ("establish-idle", ("ubertooth-util", "-U", serial, "-S")),
            ("mode-stop", ("ubertooth-util", "-U", serial, "-S")),
            # A read, and the restore pair, which only the step ID tells
            # from a setter.
            ("read-original-squelch", ("ubertooth-util", "-U", serial, "-z")),
            ("restore-squelch", ("ubertooth-util", "-U", serial, "-z-120")),
            # A two-board test works the role into the ID, and the role
            # has to be one the invocation binds.
            ("sender-read-original-crc", ("ubertooth-btle", "-U", serial, "-k")),
            ("responder-restored-device-identity",
             ("ubertooth-util", "-U", serial, "-s")),
            # A write, whose value is whatever the run recorded, so only
            # the option names are compared.
            ("restore-crc", ("ubertooth-btle", "-U", serial, "-k0")),
            ("restore-leds", ("ubertooth-util", "-U", serial, "-d1", "-l1")),
            # The USR LED query, which is `-l`: the set of queries comes
            # from the settings, so it is not a letter anyone can forget.
            ("read-original-usr-led", ("ubertooth-util", "-U", serial, "-l")),
        ]
        own = [
            # A setter is the section's, however close to the restore.
            ("own-setter", ("ubertooth-util", "-U", serial, "-z-60")),
            # A step named after a read that writes instead, and one named
            # after a restore that writes the wrong setting: the ID says
            # which shared command it should be, and the command is held to
            # it.
            ("read-original-channel", ("ubertooth-util", "-U", serial, "-c2426")),
            ("read-original-sender-squelch",
             ("ubertooth-util", "-U", serial, "-z-60")),
            ("restore-channel", ("ubertooth-util", "-U", serial, "-C2426")),
            ("restore-leds", ("ubertooth-util", "-U", serial, "-d1")),
            # A stop that stops nothing: `_stop`'s step ID is its caller's
            # to choose, so the ID cannot be taken on its own.
            ("x-stop", ("ubertooth-util", "-U", serial, "-x")),
            # The access address pair, deliberately not shared.
            ("restore-access-address",
             ("ubertooth-btle", "-U", serial, "-a8e89bed6")),
            # Another program, whatever its step is called: `nrfdk.py stop`
            # is one of these.
            ("advertiser-stop", ("nrfdk.py", "stop", "--jlink", "1")),
            ("peer-stop", ("esp32peer.py", "reset", "--device", "/dev/x")),
            ("flash-cleanup-stop",
             ("ubertooth-dfu", "-U", serial, "-d", "image.dfu", "-r")),
            # Another selector: `-U all` and an index are what `Multi-board
            # selection` is about.
            ("list-all", ("ubertooth-util", "-U", "all", "-s")),
            ("by-index", ("ubertooth-util", "-U", "0", "-z")),
            # More on the line than the shared read.
            ("read-back", ("ubertooth-util", "-U", serial, "-s", "-C")),
            # A word that is not one of this invocation's roles, which is
            # how a section's own step could take a shared name:
            # `Debug registers` binds no `debug` role.
            ("read-original-debug-channel",
             ("ubertooth-util", "-U", serial, "-C")),
            # A bare read a section makes for a check of its own: the
            # shared block states the same command, but the section is
            # what says why it reads it there.
            ("verify-unchanged-channel", ("ubertooth-util", "-U", serial, "-C")),
            ("mode-stopped-squelch", ("ubertooth-util", "-U", serial, "-z")),
            ("idle-after-capture", ("ubertooth-util", "-U", serial, "-w")),
            # A command case, which has no step at all.
            ("", ("ubertooth-util", "-U", serial, "-S")),
        ]
        # The roles a two-board invocation binds, which is what may be
        # taken out of a step ID.
        roles = ("sender", "responder")
        for step_id, argv in shared:
            with self.subTest(step_id, argv=argv):
                self.assertTrue(is_shared_command(step_id, argv, roles))
        for step_id, argv in own:
            with self.subTest(step_id, argv=argv):
                self.assertFalse(is_shared_command(step_id, argv, roles))


if __name__ == "__main__":
    unittest.main()
