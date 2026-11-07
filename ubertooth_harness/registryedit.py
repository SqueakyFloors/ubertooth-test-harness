"""Safe, surgical edits to one host's hardware registry file.

The runner owns its own `hardware.yaml`. The GUI can ask it to register or
unregister an Ubertooth, and this module is how that file changes. Two
properties matter more here than convenience.

**The rest of the file is left exactly as it was.** A bench registry holds
comments explaining the wiring, other hosts, hubs, DKs, peers, and RF
placements. Rewriting it through a YAML dumper would reformat all of that and
drop every comment, so only the lines of the one board being added or removed
are touched. The edit is then checked by reading the result back: unless the
new text parses to exactly the mapping the edit intended, nothing is kept.

**The file is never left in a state no run could use.** The new text is
validated by the real registry loader before it replaces anything, the
previous contents are kept beside it as a backup, and a loader failure after
the replace restores them.

Only Ubertooths are edited. Hubs, DKs, ESP32 peers, Bluetooth adapters and
programmers stay file-edited: each needs identity fields nothing can detect
read-only, and a half-filled entry is worse than none.
"""

import dataclasses
import os
from pathlib import Path
import re

import yaml

from ubertooth_harness.power import controller_ports
from ubertooth_harness.resources import (
    FileResourceRegistry,
    PROGRAMS_FIELD,
    RegistryError,
)


UBERTOOTH_TYPE = "Ubertooth"
POWER_CONTROLLER_TYPE = "USB power controller"
# Kept beside the registry, replaced on every edit, and restored when the
# edited file turns out not to load.
BACKUP_SUFFIX = ".backup"
ADD = "add"
REMOVE = "remove"
ACTIONS = (ADD, REMOVE)

# The same alias shape the registry itself accepts (resources._ALIAS_RE),
# written out here because an alias is checked before any file is read.
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# An Ubertooth serial as the board reports it: the 128-bit identifier is 32
# hex digits, and nothing else is accepted, so a serial copied from the wrong
# column cannot be registered. A board in its DFU bootloader reports none at
# all and cannot be registered until it is back in its application.
SERIAL_RE = re.compile(r"^[0-9a-f]{8,64}$")
# How many boards one registry may hold. Far past any bench, and a bound on
# what a loop of add requests could grow the file to.
MAX_BOARDS = 64
# Indentation of a resource under `resources:` when the file has no resource
# to copy it from.
DEFAULT_INDENT = 2


class RegistryEditError(ValueError):
    """The edit was refused; ``status`` is the HTTP status it answers with.

    400 the request itself cannot be acted on, 404 a name this registry has
    not got, 409 the registry already says otherwise, 503 the file could not
    be read or written.
    """

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclasses.dataclass(frozen=True)
class BoardEdit:
    """One board to register or unregister on one host."""

    action: str
    alias: str
    # Add only.
    serial: str = ""
    power_controller: str | None = None
    power_port: int | None = None

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise RegistryEditError(
                f"action must be {' or '.join(ACTIONS)}, not {self.action!r}"
            )
        if not ALIAS_RE.fullmatch(self.alias or ""):
            raise RegistryEditError(
                f"{self.alias!r} is not a resource alias: a letter, then "
                "letters, digits, underscores or hyphens, up to 64 characters"
            )
        if self.action == REMOVE:
            if self.serial or self.power_controller or self.power_port is not None:
                raise RegistryEditError(
                    "unregistering a board takes its alias only"
                )
            return
        if not SERIAL_RE.fullmatch(self.serial or ""):
            raise RegistryEditError(
                "a board is registered by the serial it reports: 8 to 64 "
                "lowercase hex digits. A board sitting in its DFU bootloader "
                "reports none and cannot be registered until it is back in "
                "its application firmware"
            )
        if (self.power_controller is None) != (self.power_port is None):
            raise RegistryEditError(
                "a power mapping needs both a controller and a port, or "
                "neither: a board on no switchable port is registered "
                "without one"
            )
        if self.power_controller is not None:
            if not ALIAS_RE.fullmatch(self.power_controller):
                raise RegistryEditError(
                    f"{self.power_controller!r} is not a resource alias"
                )
            if (
                not isinstance(self.power_port, int)
                or isinstance(self.power_port, bool)
                or not 0 <= self.power_port <= 255
            ):
                raise RegistryEditError("a power port is a small whole number")

    def describe(self) -> str:
        """The edit in one line, for a log. Names no path and no full serial."""
        if self.action == REMOVE:
            return f"unregistered Ubertooth {self.alias}"
        where = (
            f" on {self.power_controller} port {self.power_port}"
            if self.power_controller else " on no switchable port"
        )
        return f"registered Ubertooth {self.alias} ({short_serial(self.serial)}){where}"


def short_serial(serial: str) -> str:
    """A serial as a log should name it: enough to recognize, not all of it."""
    text = str(serial or "")
    return f"{text[:4]}…" if len(text) > 4 else text


def suggested_alias(host: str, taken) -> str:
    """``UT_<host>N``: the next free alias for a board on this host.

    ``Host_Linux`` suggests ``UT_Linux0``, which is what a bench registry
    already writes by hand; a host alias that says nothing beyond its own
    name keeps it whole.
    """
    stem = re.sub(r"^Host[_-]?", "", str(host or ""), count=1) or str(host or "")
    stem = re.sub(r"[^A-Za-z0-9]", "", stem) or "Board"
    taken = {str(name).casefold() for name in taken}
    for index in range(MAX_BOARDS + 1):
        candidate = f"UT_{stem}{index}"
        if ALIAS_RE.fullmatch(candidate) and candidate.casefold() not in taken:
            return candidate
    return ""


# ----- reading the file ----------------------------------------------------


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        # The reason, never the path: a refusal travels to a browser.
        raise RegistryEditError(
            f"this target's hardware registry could not be read: "
            f"{type(error).__name__}", 503,
        ) from error


def _parse(text: str) -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise RegistryEditError(
            f"this target's hardware registry is not valid YAML: {error}", 503
        ) from error
    if not isinstance(raw, dict) or not isinstance(raw.get("resources"), dict):
        raise RegistryEditError(
            "this target's hardware registry has no resources mapping to edit",
            503,
        )
    return raw


@dataclasses.dataclass(frozen=True)
class _Block:
    """Where one resource's lines are in the file, 0-based and inclusive.

    ``last`` is its last line of content. ``trailing`` is how many blank
    lines follow it, which the block owns: a resource is separated from the
    next by them, so removing one takes them with it and adding one puts one
    back. Comments are never owned, which is why ``trailing`` stops at the
    first one.
    """

    alias: str
    first: int
    last: int
    trailing: int = 0

    @property
    def end(self) -> int:
        """The last line removing this resource takes."""
        return self.last + self.trailing


def _blocks(text: str) -> tuple[tuple[_Block, ...], int, int]:
    """Each resource's lines, the ``resources:`` line, and the indentation.

    Taken from the parser's own marks, so an alias is found where YAML says
    it is rather than by matching text. A block's content ends at its last
    line that is neither blank nor a comment, so a comment between two
    resources belongs to the file and not to either board: removing a board
    leaves it, and adding one goes before it.
    """
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        raise RegistryEditError(
            f"this target's hardware registry is not valid YAML: {error}", 503
        ) from error
    lines = text.splitlines()
    for key, value in getattr(root, "value", ()):
        if getattr(key, "value", None) != "resources":
            continue
        pairs = list(getattr(value, "value", ()))
        indent = pairs[0][0].start_mark.column if pairs else DEFAULT_INDENT
        blocks = []
        for index, (alias_node, _) in enumerate(pairs):
            first = alias_node.start_mark.line
            if index + 1 < len(pairs):
                last = pairs[index + 1][0].start_mark.line - 1
            else:
                last = min(value.end_mark.line, len(lines) - 1)
            while last > first and _is_filler(lines[last]):
                last -= 1
            trailing = 0
            while (
                last + trailing + 1 < len(lines)
                and not lines[last + trailing + 1].strip()
            ):
                trailing += 1
            blocks.append(_Block(str(alias_node.value), first, last, trailing))
        return tuple(blocks), key.start_mark.line, indent
    raise RegistryEditError(
        "this target's hardware registry has no resources mapping to edit", 503
    )


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


# ----- composing the edit --------------------------------------------------


def _ubertooths(resources) -> dict:
    return {
        alias: value for alias, value in resources.items()
        if isinstance(value, dict) and value.get("type") == UBERTOOTH_TYPE
    }


def _check_host(resources, host: str) -> None:
    entry = resources.get(host)
    if not isinstance(entry, dict) or entry.get("type") != "Host":
        raise RegistryEditError(
            f"{host!r} is not a Host in this target's registry", 404
        )


def _check_power(resources, host: str, edit: BoardEdit) -> None:
    if edit.power_controller is None:
        return
    entry = resources.get(edit.power_controller)
    if not isinstance(entry, dict) or entry.get("type") != POWER_CONTROLLER_TYPE:
        raise RegistryEditError(
            f"{edit.power_controller!r} is not a {POWER_CONTROLLER_TYPE} in "
            "this target's registry", 404,
        )
    if entry.get("attached to") != host:
        raise RegistryEditError(
            f"{edit.power_controller} is not attached to {host}", 400
        )
    for alias, value in resources.items():
        power = value.get("power") if isinstance(value, dict) else None
        if not isinstance(power, dict):
            continue
        if (
            power.get("controller") == edit.power_controller
            and power.get("port") == edit.power_port
        ):
            raise RegistryEditError(
                f"{alias} is already registered on {edit.power_controller} "
                f"port {edit.power_port}", 409,
            )


def intended_resources(raw, host: str, edit: BoardEdit) -> dict:
    """The resources mapping this edit means to produce.

    Every refusal that can be decided from the file alone is decided here,
    before anything is written, and says what the registry already holds
    rather than leaving the loader to report a shape it does not expect.
    """
    resources = dict(raw["resources"])
    if edit.action == REMOVE:
        entry = resources.get(edit.alias)
        if not isinstance(entry, dict):
            raise RegistryEditError(
                f"{edit.alias!r} is not registered on this target", 404
            )
        if entry.get("type") != UBERTOOTH_TYPE:
            raise RegistryEditError(
                f"{edit.alias} is a {entry.get('type')}, and only an "
                f"{UBERTOOTH_TYPE} is managed here; edit the registry file "
                "for anything else", 409,
            )
        for alias, value in resources.items():
            if not isinstance(value, dict):
                continue
            if value.get(PROGRAMS_FIELD) == edit.alias:
                raise RegistryEditError(
                    f"{alias} is wired to {edit.alias} as its debug "
                    "programmer; unwire it in the registry file first", 409,
                )
            defaults = value.get("defaults")
            if isinstance(defaults, dict) and edit.alias in defaults.values():
                raise RegistryEditError(
                    f"{alias} names {edit.alias} as a default; change that in "
                    "the registry file first", 409,
                )
        del resources[edit.alias]
        return resources

    _check_host(resources, host)
    if edit.alias in resources:
        raise RegistryEditError(
            f"{edit.alias} is already registered on this target "
            f"(a {resources[edit.alias].get('type')})", 409,
        )
    for alias, value in _ubertooths(resources).items():
        identity = value.get("identity")
        registered = (
            str(identity.get("serial", "")).strip().lower()
            if isinstance(identity, dict) else ""
        )
        if registered == edit.serial:
            raise RegistryEditError(
                f"that board is already registered as {alias}", 409
            )
    if len(_ubertooths(resources)) >= MAX_BOARDS:
        raise RegistryEditError(
            f"this registry already holds {MAX_BOARDS} Ubertooths", 409
        )
    _check_power(resources, host, edit)
    entry = {
        "type": UBERTOOTH_TYPE,
        "attached to": host,
        "identity": {"serial": edit.serial},
    }
    if edit.power_controller is not None:
        entry["power"] = {
            "controller": edit.power_controller, "port": edit.power_port
        }
    resources[edit.alias] = entry
    return resources


def _rendered(edit: BoardEdit, host: str, indent: int) -> list[str]:
    """The lines one added board takes, at the file's own indentation."""
    pad = " " * indent
    inner = " " * (indent + DEFAULT_INDENT)
    deeper = " " * (indent + 2 * DEFAULT_INDENT)
    lines = [
        f"{pad}{edit.alias}:",
        f"{inner}type: {UBERTOOTH_TYPE}",
        f"{inner}attached to: {host}",
        f"{inner}identity:",
        # Always quoted: an all-digit serial would otherwise be read as a
        # number, and a registry is compared as text.
        f'{deeper}serial: "{edit.serial}"',
    ]
    if edit.power_controller is not None:
        lines += [
            f"{inner}power:",
            f"{deeper}controller: {edit.power_controller}",
            f"{deeper}port: {edit.power_port}",
        ]
    return lines


def edited_text(text: str, host: str, edit: BoardEdit) -> str:
    """``text`` with this one board added or removed, and nothing else moved.

    A resource and the blank lines after it move together, so adding a board
    and removing it again leaves the file exactly as it was, whitespace
    included.
    """
    blocks, resources_line, indent = _blocks(text)
    lines = text.splitlines()
    if edit.action == REMOVE:
        block = next((item for item in blocks if item.alias == edit.alias), None)
        if block is None:
            raise RegistryEditError(
                f"{edit.alias!r} is not registered on this target", 404
            )
        kept = lines[: block.first] + lines[block.end + 1 :]
    else:
        after = blocks[-1].end + 1 if blocks else resources_line + 1
        kept = (
            lines[:after] + _rendered(edit, host, indent) + [""] + lines[after:]
        )
    return "".join(f"{line}\n" for line in kept)


# ----- writing it ----------------------------------------------------------


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: a temporary file beside it, then rename.

    The data is on the disk before the rename, so a registry is never half
    written, whatever happens to the machine between the two.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RegistryEditError(
            f"this target's hardware registry could not be written: "
            f"{type(error).__name__}", 503,
        ) from error


def _validated(text: str, path: Path) -> None:
    """Refuse ``text`` unless the real registry loader accepts it.

    Loaded from a file beside the real one, so a relative path or a
    `/dev/serial/by-id` entry means the same thing it will mean once the text
    is in place, and the registry itself is never the candidate.
    """
    candidate = path.with_name(f".{path.name}.candidate")
    try:
        candidate.write_text(text, encoding="utf-8")
        FileResourceRegistry.load(candidate)
    except OSError as error:
        raise RegistryEditError(
            f"this target's hardware registry could not be written: "
            f"{type(error).__name__}", 503,
        ) from error
    except RegistryError as error:
        # The change would make a registry no run could use (a port the hub
        # has not got, a mapping that collides). Nothing has been written:
        # only the candidate beside it, which goes below.
        raise RegistryEditError(
            f"this target's hardware registry would not accept the change: "
            f"{error}", 409,
        ) from error
    finally:
        candidate.unlink(missing_ok=True)


def apply_edit(path, host: str, edit: BoardEdit) -> FileResourceRegistry:
    """Add or remove one Ubertooth in ``path``; return the registry that
    results.

    Nothing is written until the new text has been read back, compared with
    the mapping the edit intended, and loaded by the real registry loader. A
    copy of the previous contents is kept as ``<name><BACKUP_SUFFIX>`` and is
    restored if the file does not load once it is in place.
    """
    path = Path(path)
    original = _load_text(path)
    raw = _parse(original)
    intended = intended_resources(raw, host, edit)
    text = edited_text(original, host, edit)
    # The surgical edit is text work on a format with significant
    # indentation, so its result is checked rather than trusted: if the file
    # now parses to anything but the intended mapping, the edit is dropped.
    produced = _parse(text)
    if produced.get("resources") != intended or _other_keys(produced) != _other_keys(raw):
        raise RegistryEditError(
            "this target's hardware registry was not edited: the result did "
            "not match what the change intended, so it was left alone", 503,
        )
    _validated(text, path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    _write_atomically(backup, original)
    _write_atomically(path, text)
    try:
        return FileResourceRegistry.load(path)
    except RegistryError as error:
        # It loaded a moment ago from identical text beside it, so this is
        # the filesystem or another writer, not the edit. Put back what was
        # there and say so.
        _write_atomically(path, original)
        raise RegistryEditError(
            f"this target's hardware registry did not load after the change, "
            f"so the previous one was restored: {error}", 503,
        ) from error


def _other_keys(raw) -> dict:
    """Everything in the file but the resources mapping, to compare it."""
    return {key: value for key, value in raw.items() if key != "resources"}


def power_controllers(registry, host: str) -> list[dict]:
    """The switchable hubs on this host, with the ports still free.

    What the GUI offers when a board is registered: a port already mapped to
    another board is left out, because the registry refuses two boards on one
    port and an operator should not have to find that out by being refused.
    """
    used = {
        (
            resource.power.get("controller"), resource.power.get("port")
        )
        for resource in registry.resources.values()
        if resource.power
    }
    controllers = []
    for resource in registry.resources.values():
        if resource.type != POWER_CONTROLLER_TYPE or resource.attached_to != host:
            continue
        controllers.append({
            "alias": resource.alias,
            "driver": resource.driver,
            "ports": [
                port for port in controller_ports(resource)
                if (resource.alias, port) not in used
            ],
        })
    controllers.sort(key=lambda entry: entry["alias"])
    return controllers
