"""Typed resource registry for hosts and attached lab hardware."""

import posixpath
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

import yaml

from ubertooth_harness.rf import (
    Placement,
    PlacementError,
    PlacementRequirement,
    describe as describe_placement,
    placement as parse_placement,
)


_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

DEBUG_PROGRAMMER = "Debug programmer"

# Resource types with no radio, which may not record an RF placement.
PLACELESS_TYPES = ("Host", "USB power controller", DEBUG_PROGRAMMER)


class RegistryError(ValueError):
    """Resource configuration or selection is invalid."""


class MissingResource(RegistryError):
    """This registry does not have a host or device a plan asked for.

    The one refusal that says nothing about the plan: the same plan is
    compiled against every bench's registry, and a bench that has no ESP32
    peer, or is not the Linux host a step names, simply cannot run that step.
    Run planning records it as not applicable (planner.py); explicit
    validation (``validate-plan --hardware``) still refuses it, because there
    the registry is named on purpose.

    A name this registry does have, used wrongly (an Ubertooth asked for as a
    host, an alias the plan names for a role it does not declare), and a name
    that differs from a registered one only in case, are mistakes in the
    plan: they stay plain ``RegistryError`` and fail everywhere.
    """


class MissingRequiredHardware(RegistryError):
    """Hardware a step must have to mean anything, which this bench has not got.

    Absence again, as ``MissingResource`` is, but of the very thing the step
    exists to exercise: a second Ubertooth to alternate flashes across, or a
    switchable port to power another board off. Leaving such a step out
    quietly would read as a pass for a gate that was never measured, so run
    planning records the step as blocked (``hostneeds.Blocked``), runs
    everything unrelated, and fails the run. ``validate-plan --hardware``
    refuses it outright, as it does every other absence.

    A plan step may say the same of the hardware it binds, with
    ``required hardware: true``. There the absence is usually the registry's
    own (``MissingResource``: the host, or a device for a role), and the
    planner records it as blocked rather than not applicable. A host that
    can offer only one device for two roles is the planner's own finding,
    since no single lookup failed: it writes that reason itself, and raises
    this error for it only under ``strict_hardware``.
    """


STARTECH_DRIVER = "startech managed USB hub"
# Port 4 carries the hub's management controller; only 1-3 are DUT ports.
STARTECH_PORTS = (1, 2, 3)
STARTECH_DEVICE_PREFIX = "/dev/serial/by-id/"


def is_stable_serial_device(path) -> bool:
    """True for a /dev/serial/by-id/<name> path (stable across re-plugging)."""
    return (
        isinstance(path, str)
        and posixpath.dirname(path) == STARTECH_DEVICE_PREFIX.rstrip("/")
        and bool(posixpath.basename(path))
        and ".." not in path.split("/")
    )


NRF52840_DK = "nRF52840 DK"
# Roles the DK role firmware (firmware/nrf52840dk) can take. One image holds
# all three and `role <name>` switches between them.
DK_ROLES = ("advertiser", "central", "peripheral")
_DK_IDENTITY_KEYS = ("jlink serial", "BLE address", "serial device")
# The canonical spelling of a J-Link serial, and the only one a registry
# takes: the padded digits `nrfutil device list` prints and udev's by-id
# name carries (``leases.JLINK_SERIAL_DIGITS``). A DK answers to two
# spellings of one serial, and its external lock file is named after the
# serial, so two spellings are two files and a lab wrapper that chose the
# other one would meet nothing (#324). One spelling here is what gives the
# device one lock file; nrfutil's zero-stripped form is still a name
# `scripts/lab/hold` resolves, and the lease key is built from it either
# way, so nothing but the registry entry changes.
# The width is the spelling and nothing more: how many digits a probe's
# serial itself has is ``nrfdk.jlink_serial``'s rule, which is unchanged
# and is what judges the number the DK is driven by.
_JLINK_SERIAL = re.compile(r"^[0-9]{12}$")
# A 48-bit device address, colon separated: a DK's BLE address and an
# ESP32 peer's BD_ADDR are written the same way.
_ADDRESS_48 = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


ESP32_CLASSIC_PEER = "ESP32 Classic peer"
# Roles the peer firmware (firmware/esp32-classic-peer) can take. One image
# holds both and `role <name>` switches between them, so a board may declare
# both; the initiator pages, so it is the piconet central.
PEER_ROLES = ("acceptor", "initiator")
# LAP, UAP and name are notes: the lab registry records what the board's
# status reports, for a person reading it. The code derives the LAP and UAP
# from the address, and classic_cases.py refuses a note that disagrees.
_PEER_IDENTITY_KEYS = ("bridge serial", "BD_ADDR", "LAP", "UAP", "name",
                       "serial device")
# The bridge's serial as its /dev/serial/by-id name carries it. udev takes
# that from the USB iSerial string as the device reports it, and a CP210x
# serial can be reprogrammed to any string, so the case is the board's to
# choose: only the characters a by-id name can hold are required here. It is
# used verbatim, in the device path and in the lease key, so what is
# registered is what is looked for. How long it may be is one rule in one
# place, about the lock files it names (_refuse_a_long_serial).
_BRIDGE_SERIAL = re.compile(r"^[0-9A-Za-z_.:+-]+$")


def _validate_peer(resource) -> None:
    """An ESP32 Classic peer: bridge serial, BD_ADDR, and its by-id path."""
    alias = resource.alias
    unknown = sorted(set(resource.identity) - set(_PEER_IDENTITY_KEYS))
    if unknown:
        raise RegistryError(
            f"{ESP32_CLASSIC_PEER} {alias} identity has unknown key(s) "
            f"{', '.join(unknown)}; use {', '.join(_PEER_IDENTITY_KEYS)}"
        )
    if not _BRIDGE_SERIAL.fullmatch(resource.identity.get("bridge serial", "")):
        raise RegistryError(
            f"{ESP32_CLASSIC_PEER} {alias} requires a 'bridge serial' identity: "
            "the serial as its /dev/serial/by-id name holds it (letters, "
            "digits, and _.:+- only)"
        )
    # After the shape, as a programmer's is: how long a name may be is the
    # rule about the lock files, not about what a serial looks like.
    _refuse_a_long_serial(resource, "bridge serial", "esp32-")
    # Both boards' addresses are needed: the initiator calls the acceptor's,
    # and every case checks the tool's output against the central's LAP and UAP.
    if not _ADDRESS_48.fullmatch(resource.identity.get("BD_ADDR", "")):
        raise RegistryError(
            f"{ESP32_CLASSIC_PEER} {alias} requires a 'BD_ADDR' identity "
            "(the address its status reports), like C8:85:41:2A:1B:2A"
        )
    device = resource.identity.get("serial device")
    if device is not None and not is_stable_serial_device(device):
        raise RegistryError(
            f"{ESP32_CLASSIC_PEER} {alias} serial device must be a "
            "/dev/serial/by-id/ path: /dev/ttyUSB<n> is assignment order and "
            "the two boards swap"
        )
    roles = set(resource.roles)
    if not roles or not roles <= set(PEER_ROLES):
        raise RegistryError(
            f"{ESP32_CLASSIC_PEER} {alias} roles must be one or more of "
            f"{', '.join(PEER_ROLES)}"
        )


BLUETOOTH_ADAPTER = "Bluetooth adapter"
# A host Bluetooth controller BlueZ drives. The two tools the fork builds only
# with libbluetooth, ubertooth-scan and ubertooth-follow, name it with -b: the
# first runs its inquiry, the second reads its clock and the channel map of a
# link it makes. An Ubertooth cannot stand in for either, which is why the
# adapter is a resource of its own.
_ADAPTER_IDENTITY_KEYS = ("hci device", "BD_ADDR", "description")
# hciN, as BlueZ numbers its controllers and as -b takes it. The number is the
# order the kernel attached them, not a property of the adapter, so a case
# checks the address the device reports against the one registered here
# (bluez_cases.py): an adapter that came back as another hciN then fails with
# what it found rather than driving whatever took the name.
_HCI_DEVICE = re.compile(r"^hci[0-9]{1,3}$")


def _validate_adapter(resource) -> None:
    """A host Bluetooth adapter: its HCI device name and its own address."""
    alias = resource.alias
    unknown = sorted(set(resource.identity) - set(_ADAPTER_IDENTITY_KEYS))
    if unknown:
        raise RegistryError(
            f"{BLUETOOTH_ADAPTER} {alias} identity has unknown key(s) "
            f"{', '.join(unknown)}; use {', '.join(_ADAPTER_IDENTITY_KEYS)}"
        )
    if not _HCI_DEVICE.fullmatch(resource.identity.get("hci device", "")):
        raise RegistryError(
            f"{BLUETOOTH_ADAPTER} {alias} requires an 'hci device' identity "
            "like hci0 (the name BlueZ gave it, which -b takes)"
        )
    if not _ADDRESS_48.fullmatch(resource.identity.get("BD_ADDR", "")):
        raise RegistryError(
            f"{BLUETOOTH_ADAPTER} {alias} requires a 'BD_ADDR' identity (the "
            "address hciconfig reports for it), like 00:1A:7D:DA:71:13"
        )
    if resource.roles:
        # Nothing binds an adapter by role: one host has one controller for
        # these tools, and a declared role would only keep the test from
        # finding it.
        raise RegistryError(
            f"{BLUETOOTH_ADAPTER} {alias} takes no roles; it is bound by type"
        )


# A debug programmer: a USB serial adapter, SWD probe, or anything else wired
# to one device's debug or in-system programming header and left there. It is
# not a radio and no test binds it; it exists in the registry so that it and
# the device it is wired to are held together (see pair_members).
PROGRAMS_FIELD = "programs"
_PROGRAMMER_IDENTITY_KEYS = ("serial", "serial device")
# The programmer's own serial, as its USB descriptor reports it. Taken
# verbatim, as a peer's bridge serial is: the same string names its lease
# file, so one spelling is one device. Only the characters a file name can
# hold are allowed; how long it may be is one rule in one place, about the
# lock files it names (_refuse_a_long_serial).
_PROGRAMMER_SERIAL = re.compile(r"^[0-9A-Za-z_.:+-]+$")


# The files in a lease directory that are not a device's, by the name a
# bench lock would spell, and what each one is in a refusal.
_RESERVED_LEASE_NAMES = MappingProxyType({
    "host-power": "this host's power/firmware lock",
    "takeover": "the lease directory's own takeover lock",
})


def _bench_lock_names(resource) -> tuple[tuple[str, str], ...]:
    """The plain lock names a bench script may use for this device.

    (compared, as registered) for each, lowercased for the comparison
    because one serial written two ways is still one device. One flat space,
    shared by every device: such a name is the bench's own lock file, which
    every command that leases the device takes beside the harness's own
    lease key (``leases.board_for_resource``), so a second device with the
    same serial would be held through the first one's file.
    An Ubertooth's is its serial (its lease key too), a DK's its J-Link
    serial as nrfutil and sysfs each print it, a peer's its bridge serial,
    and a programmer's the serial its USB descriptor reports.
    """
    identity = resource.identity
    values = {
        "Ubertooth": ("serial",),
        NRF52840_DK: ("jlink serial",),
        ESP32_CLASSIC_PEER: ("bridge serial",),
        DEBUG_PROGRAMMER: ("serial",),
    }.get(resource.type, ())
    names = []
    for key in values:
        written = str(identity.get(key, "") or "").strip()
        serial = written.lower()
        if not serial:
            continue
        names.append((serial, written))
        if resource.type == NRF52840_DK and serial.isdigit():
            # A DK's J-Link serial is the one serial a device answers to in
            # two spellings: nrfutil prints it without the leading zeros
            # sysfs pads it with, and `scripts/lab/hold` takes the name a
            # bench note would use. Only the padded one names a lock file
            # now (#324), and the stripped one is still the name the lease
            # key is built from and a name `_bench.resolve` accepts, so
            # neither may belong to a second device. An all-digit serial of
            # another type names only one file, so it reserves only itself.
            # (A stripped all-digit serial of any type still *names* a
            # device to `scripts/lab/_bench.resolve`; two devices that share
            # one are refused when a command resolves the name, which says
            # so and names them both.)
            from ubertooth_harness.leases import canonical_jlink_serial

            for spelling in (canonical_jlink_serial(serial), serial.lstrip("0")):
                if spelling != serial:
                    names.append((spelling, written))
    return tuple(item for item in names if item[0])


@dataclass(frozen=True)
class _LockName:
    """One claim on a name a device is held under.

    ``alias`` is the device that has it, None for a name the lease
    directory keeps for itself; ``kind`` is ``serial``, ``lease`` or
    ``reserved``, which decide how a collision reads; ``spelling`` is the
    name as it was written, which is not always the name compared (a serial
    is compared without regard to case, and a DK has two spellings of one).
    """

    alias: str | None
    kind: str
    describes: str
    spelling: str


def _refuse_a_long_serial(resource, field: str, prefix: str) -> None:
    """A serial every lock file name built from it still fits.

    A lease key carries its type's prefix, so a serial that is a file name
    on its own may still build one that is not. Refused here, where the
    field it names is in front of whoever wrote it, rather than left to the
    command that leases the device (``leases.resource_key``, the backstop).
    """
    from ubertooth_harness.leases import max_serial_chars

    limit = max_serial_chars(prefix)
    serial = str(resource.identity.get(field, "") or "").strip()
    if len(serial) > limit:
        raise RegistryError(
            f"{resource.type} {resource.alias} '{field}' may be at most {limit} "
            f"characters, so that its lease file ('{prefix}<{field}>') has a "
            f"name a file can carry; registered {len(serial)}"
        )


def _lock_names(resource) -> dict[str, _LockName]:
    """Every name this device is held under, by the name as compared.

    Its plain serials, which the bench's own lock files are named after,
    and its lease key, which names its file in the lease directory and the
    bench's own file where there is no second name. Serials first, so a
    collision between two of them is reported as the serial a person wrote
    rather than as the key built from it.
    """
    names: dict[str, _LockName] = {}
    describes = f"{resource.alias} ({resource.type})"
    for name, written in _bench_lock_names(resource):
        names.setdefault(
            name, _LockName(resource.alias, "serial", describes, written)
        )
    if resource.type in _leased_types():
        # Only a device the harness leases has a lease name at all.
        # `resource_key` answers for any resource it is handed, falling
        # back to a board's serial, so a hub or a host would otherwise
        # claim a name no command ever locks for it.
        key = _lease_key_of(resource)
        if key is not None:
            names.setdefault(
                key.lower(), _LockName(resource.alias, "lease", describes, key)
            )
    return names


def _lease_key_of(resource) -> str | None:
    """The file this device's lease takes, or None when it has no key yet.

    A registry may record a device before the field its lease key comes
    from: leasing it is refused where it is asked for, naming the field to
    fill in, and there is no key to compare here.
    """
    from ubertooth_harness.leases import LeaseError, resource_key

    try:
        return resource_key(resource)
    except LeaseError:
        return None


def _leased_types() -> tuple[str, ...]:
    """The device types the harness leases, which a pair member must be.

    Imported here rather than at the top of the module: ``leases`` needs
    nothing of the registry (it works on resources it is handed), and one
    direction of dependency is what keeps it that way.
    """
    from ubertooth_harness.leases import LEASED_TYPES

    return tuple(LEASED_TYPES)


def _validate_programmer(resource) -> None:
    """A debug programmer: its own serial, and the one device it programs."""
    alias = resource.alias
    unknown = sorted(set(resource.identity) - set(_PROGRAMMER_IDENTITY_KEYS))
    if unknown:
        raise RegistryError(
            f"{DEBUG_PROGRAMMER} {alias} identity has unknown key(s) "
            f"{', '.join(unknown)}; use {', '.join(_PROGRAMMER_IDENTITY_KEYS)}"
        )
    if not _PROGRAMMER_SERIAL.fullmatch(resource.identity.get("serial", "")):
        raise RegistryError(
            f"{DEBUG_PROGRAMMER} {alias} requires a 'serial' identity: the serial "
            "its USB descriptor reports, which is also what its lease file is "
            "named after (letters, digits, and _.:+- only)"
        )
    # After the shape, so a serial that is both is reported as the shape it
    # is: the length a name may be is the rule about the files, not about
    # what a serial looks like.
    _refuse_a_long_serial(resource, "serial", "programmer-")
    device = resource.identity.get("serial device")
    if device is not None and not is_stable_serial_device(device):
        raise RegistryError(
            f"{DEBUG_PROGRAMMER} {alias} serial device must be a "
            "/dev/serial/by-id/ path: /dev/ttyUSB<n> is assignment order, so it "
            "names another adapter after a re-plug"
        )
    if resource.programs is None:
        raise RegistryError(
            f"{DEBUG_PROGRAMMER} {alias} requires '{PROGRAMS_FIELD}: <alias>', the "
            "device it is wired to; the two are held together, and a programmer "
            "wired to nothing is a registry half filled in"
        )


def _dk_serial_refusal(alias: str, serial: str) -> str:
    """Why this 'jlink serial' is not the spelling a DK is locked under.

    A serial written short is the commoner mistake and the one worth a
    migration: it is the same probe, so the refusal says what to write
    instead and what it costs a bench that locks the short name today.
    """
    from ubertooth_harness.leases import JLINK_SERIAL_DIGITS, canonical_jlink_serial

    wanted = (
        f"nRF52840 DK {alias} requires a 'jlink serial' of exactly "
        f"{JLINK_SERIAL_DIGITS} digits: the spelling `nrfutil device list` "
        "prints and the /dev/serial/by-id name carries, which is also what "
        f"this DK's external lock file is named after; registered '{serial}'"
    )
    padded = canonical_jlink_serial(serial)
    if padded == serial or not padded:
        return wanted
    return (
        f"{wanted}. That is the same probe written short, so write it as "
        f'"{padded}" (quoted, so the leading zeros survive). Nothing else '
        f"changes: its lease file stays 'nrf52840dk-{int(serial)}', and "
        "a bench note or `scripts/lab/hold` still takes either spelling as a "
        f"name. A bench whose own scripts lock 'board-{serial}.lock' renames "
        f"that file to 'board-{padded}.lock'"
    )


def _validate_dk(resource) -> None:
    """An nRF52840 DK: J-Link serial, optional BLE address and VCOM0 path."""
    alias = resource.alias
    unknown = sorted(set(resource.identity) - set(_DK_IDENTITY_KEYS))
    if unknown:
        raise RegistryError(
            f"nRF52840 DK {alias} identity has unknown key(s) {', '.join(unknown)}; "
            f"use {', '.join(_DK_IDENTITY_KEYS)}"
        )
    serial = resource.identity.get("jlink serial", "")
    if not _JLINK_SERIAL.fullmatch(serial):
        raise RegistryError(_dk_serial_refusal(alias, serial))
    address = resource.identity.get("BLE address")
    if address is not None and not _ADDRESS_48.fullmatch(address):
        raise RegistryError(
            f"nRF52840 DK {alias} BLE address must look like C0:11:22:33:44:55"
        )
    device = resource.identity.get("serial device")
    if device is not None and not device.startswith("/dev/"):
        raise RegistryError(f"nRF52840 DK {alias} serial device must be a /dev path")
    roles = set(resource.roles)
    if not roles or not roles <= set(DK_ROLES):
        raise RegistryError(
            f"nRF52840 DK {alias} roles must be one or more of {', '.join(DK_ROLES)}"
        )
    # Every role is identified by its address in a capture, and the central
    # connects to the peripheral's, so all three need one.
    if address is None:
        raise RegistryError(
            f"nRF52840 DK {alias} requires a 'BLE address' (the address its "
            "status reports)")


@dataclass(frozen=True)
class Resource:
    alias: str
    type: str
    platform: str | None
    roles: tuple[str, ...]
    defaults: Mapping[str, str]
    attached_to: str | None
    identity: Mapping[str, str]
    capabilities: tuple[str, ...]
    power: Mapping[str, object]
    driver: str | None
    # The unit's RF placement from ``rf:``; None when the registry does not
    # record one, which rf.py reads as an ordinary unit on the bench.
    rf: Placement | None = None
    # The device this one is permanently wired to, from ``programs:``: the
    # alias of a debug programmer's target. None for everything else. The
    # value is whatever the registry held, so validation can report a list
    # (more than one target) as the mistake it is.
    programs: object | None = None


@dataclass(frozen=True)
class Reconciliation:
    present: tuple[str, ...]
    missing: tuple[str, ...]
    unknown: tuple[Mapping, ...]
    conflicting: tuple[str, ...] = ()


class ResourceRegistry(Protocol):
    resources: Mapping[str, Resource]

    def get(self, alias: str) -> Resource:
        """Return one registered resource."""

    def pair_members(self, alias: str) -> tuple[str, ...]:
        """Every device permanently wired to this one (a debug programmer and
        the device it programs), which is held with it."""

    def describe_pairing(self, alias: str, named: str) -> str:
        """Why holding ``named`` holds ``alias`` too, as a clause."""

    def expand_pairs(
        self, aliases: Sequence[str]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The aliases to hold for these, deduplicated and in a deterministic
        order, and one clause per alias the pairing added."""

    def hardware(self, host: str, resource_type: str) -> tuple[str, ...]:
        """Every resource of this type attached to the host, by alias."""

    def placed(
        self,
        host: str,
        resource_type: str,
        placement: PlacementRequirement,
        role: str | None = None,
    ) -> tuple[str, ...]:
        """Those of them whose RF placement satisfies the requirement, and
        which may be bound to ``role`` when one is given.

        ``resolve_hardware`` answers the same way for ``default``, and for
        ``each`` with a requirement. ``each`` without one is by type alone,
        as it always has been.
        """

    def resolve_hardware(
        self,
        host: str,
        resource_type: str,
        selector: str,
        role: str | None = None,
        placement: PlacementRequirement | None = None,
    ) -> tuple[str, ...]:
        """Resolve default, each, or explicit hardware deterministically,
        restricted to resources whose RF placement satisfies ``placement``."""


class FileResourceRegistry:
    def __init__(self, resources: Mapping[str, Resource]):
        self.resources = MappingProxyType(dict(resources))
        self._validate()

    @classmethod
    def load(cls, path: Path | str) -> "FileResourceRegistry":
        registry_path = Path(path)
        try:
            with registry_path.open("r", encoding="utf-8") as stream:
                raw = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as error:
            raise RegistryError(f"unable to load resource registry: {error}") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("resources"), dict):
            raise RegistryError("registry root must contain a resources mapping")
        resources = {}
        for alias, value in raw["resources"].items():
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                raise RegistryError(f"invalid resource alias '{alias}'")
            if not isinstance(value, dict):
                raise RegistryError(f"resource {alias} must be a mapping")
            resource_type = value.get("type")
            if not isinstance(resource_type, str) or not resource_type:
                raise RegistryError(f"resource {alias} requires a type")
            identity = value.get("identity", {})
            defaults = value.get("defaults", {})
            power = value.get("power", {})
            for field_name, field_value in (
                ("identity", identity), ("defaults", defaults), ("power", power)
            ):
                if not isinstance(field_value, dict):
                    raise RegistryError(f"resource {alias}.{field_name} must be a mapping")
            if "power" in value and not power:
                # No `power:` at all is a board on no controller, which the
                # planner reads as hardware this bench has not got. A written
                # mapping with nothing in it is a registry half-filled in, so
                # it is refused here rather than read as that absence.
                raise RegistryError(
                    f"resource {alias}.power must name a controller and a port"
                )
            programs = value.get(PROGRAMS_FIELD)
            if PROGRAMS_FIELD in value and programs is None:
                # Written with nothing after it. Kept as the empty string, so
                # validation sees a pairing that was meant and left empty
                # rather than a resource with no pairing at all, which is
                # every resource but a programmer.
                programs = ""
            placement = None
            if value.get("rf") is not None:
                try:
                    placement = parse_placement(value["rf"])
                except PlacementError as error:
                    raise RegistryError(f"resource {alias}: {error}") from error
            resources[alias] = Resource(
                alias=alias,
                type=resource_type,
                platform=value.get("platform"),
                roles=tuple(value.get("roles", ())),
                defaults=MappingProxyType(dict(defaults)),
                attached_to=value.get("attached to"),
                identity=MappingProxyType({str(k): str(v) for k, v in identity.items()}),
                capabilities=tuple(value.get("capabilities", ())),
                power=MappingProxyType(dict(power)),
                driver=value.get("driver"),
                rf=placement,
                programs=programs,
            )
        return cls(resources)

    def _validate(self) -> None:
        identities = {}
        for resource in self.resources.values():
            if resource.rf is not None and resource.type in PLACELESS_TYPES:
                # A placement describes a radio on the bench; a computer and
                # a hub have none, and no test selects either by placement.
                raise RegistryError(
                    f"resource {resource.alias} is a {resource.type} and cannot "
                    "have an rf placement"
                )
            if resource.type == "Host":
                if resource.platform not in ("linux", "macos"):
                    raise RegistryError(
                        f"host {resource.alias} platform must be linux or macos"
                    )
                if resource.attached_to is not None:
                    raise RegistryError(f"host {resource.alias} cannot be attached")
            else:
                if resource.attached_to not in self.resources:
                    raise RegistryError(
                        f"resource {resource.alias} has unknown attached host"
                    )
                host = self.resources[resource.attached_to]
                if host.type != "Host":
                    raise RegistryError(
                        f"resource {resource.alias} must attach to a Host"
                    )
            if resource.identity:
                key = (resource.type, tuple(sorted(resource.identity.items())))
                if key in identities:
                    raise RegistryError(
                        f"duplicate identity for {identities[key]} and {resource.alias}"
                    )
                identities[key] = resource.alias

        for host in self.hosts():
            for key, alias in host.defaults.items():
                target = self.get(alias)
                # A default is named either by resource type or by the role a
                # test binds it to; a role default must be a resource that
                # declares that role.
                if target.type != key and key not in target.roles:
                    raise RegistryError(
                        f"default {alias} is neither a {key} nor declares the "
                        f"{key} role"
                    )
                if target.attached_to != host.alias:
                    raise RegistryError(
                        f"default {alias} is not attached to {host.alias}"
                    )
        adapter_devices: dict[tuple, str] = {}
        adapter_addresses: dict[tuple, str] = {}
        for resource in self.resources.values():
            if resource.type == NRF52840_DK:
                _validate_dk(resource)
            if resource.type == ESP32_CLASSIC_PEER:
                _validate_peer(resource)
            if resource.type == BLUETOOTH_ADAPTER:
                _validate_adapter(resource)
                # One hciN names one controller on one host. Two adapters
                # registered under the same name would take two leases and
                # drive the same device; the duplicate-identity check above
                # does not catch it, because it compares whole identities and
                # the two addresses differ.
                key = (resource.attached_to, resource.identity["hci device"])
                if key in adapter_devices:
                    raise RegistryError(
                        f"{BLUETOOTH_ADAPTER} {adapter_devices[key]} and "
                        f"{resource.alias} are both "
                        f"{resource.identity['hci device']} on "
                        f"{resource.attached_to}"
                    )
                adapter_devices[key] = resource.alias
                # And one address names one radio. The reverse of the check
                # above: a bench that registered the same adapter again
                # after it came back under another hciN has two aliases for
                # one controller, whose identities differ in the name, so
                # the duplicate-identity check does not catch it either.
                # Both would then share a lease, since the lease key is
                # built from the address, while the cases drove two devices.
                address = (resource.attached_to,
                           str(resource.identity["BD_ADDR"]).upper())
                if address in adapter_addresses:
                    raise RegistryError(
                        f"{BLUETOOTH_ADAPTER} {adapter_addresses[address]} "
                        f"and {resource.alias} are both registered at "
                        f"{resource.identity['BD_ADDR']} on "
                        f"{resource.attached_to}"
                    )
                adapter_addresses[address] = resource.alias
            if resource.type == DEBUG_PROGRAMMER:
                _validate_programmer(resource)
        self._validate_lock_names()
        self._validate_pairs()
        used_power_ports = {}
        for resource in self.resources.values():
            if (
                resource.type == "USB power controller"
                and resource.driver == STARTECH_DRIVER
            ):
                if not is_stable_serial_device(
                    resource.identity.get("serial device")
                ):
                    raise RegistryError(
                        f"controller {resource.alias} requires a "
                        "/dev/serial/by-id/ serial device identity"
                    )
                if "per-port VBUS switching" not in resource.capabilities:
                    raise RegistryError(
                        f"controller {resource.alias} lacks required capabilities"
                    )
        for resource in self.resources.values():
            if resource.power:
                controller_alias = resource.power.get("controller")
                # Checked before the lookup: a YAML value of any other type
                # (a list, a mapping) is not even hashable, and must be
                # reported as the registry mistake it is.
                if not isinstance(controller_alias, str):
                    raise RegistryError(
                        f"resource {resource.alias} power controller must be "
                        "a registered alias"
                    )
                if controller_alias not in self.resources:
                    raise RegistryError(
                        f"resource {resource.alias} has unknown power controller"
                    )
                controller = self.resources[controller_alias]
                if controller.type != "USB power controller":
                    raise RegistryError(
                        f"resource {resource.alias} power controller has wrong type"
                    )
                if controller.attached_to != resource.attached_to:
                    raise RegistryError(
                        f"resource {resource.alias} power controller is on another host"
                    )
                port = resource.power.get("port")
                if not isinstance(port, int) or isinstance(port, bool) or port < 0:
                    raise RegistryError(
                        f"resource {resource.alias} power port must be a nonnegative integer"
                    )
                if controller.driver == "acroname BrainStem USBHub3+":
                    if port > 7:
                        raise RegistryError(
                            f"resource {resource.alias} power port must be 0-7"
                        )
                    if "serial" not in controller.identity:
                        raise RegistryError(
                            f"controller {controller_alias} requires serial identity"
                        )
                    required = {"per-port VBUS switching", "power readback"}
                    if not required.issubset(controller.capabilities):
                        raise RegistryError(
                            f"controller {controller_alias} lacks required capabilities"
                        )
                if controller.driver == STARTECH_DRIVER:
                    if port not in STARTECH_PORTS:
                        raise RegistryError(
                            f"resource {resource.alias} power port must be 1-3 "
                            "(port 4 carries the hub management controller)"
                        )
                key = (controller_alias, port)
                if key in used_power_ports:
                    raise RegistryError(
                        "duplicate power port mapping for "
                        f"{used_power_ports[key]} and {resource.alias}"
                    )
                used_power_ports[key] = resource.alias

    def _validate_lock_names(self) -> None:
        """One file, one device, over every name a device is held under.

        Two kinds of name, one flat space (``_lock_names``). A lease key is
        a file in the lease directory (``leases.resource_key``) and a plain
        serial is what the bench's own lock files are named after
        (``leases.plain_serial``) -- and the bench's files are named after
        whichever of the two a command takes, so a name has to belong to one
        device throughout.

        What that catches, which nothing else did: two resources that take
        one lease file, each read as a device of its own, because a key
        comes from one identity field while the duplicate-identity check
        above compares whole identity mappings (a run could bind both
        boards of a connection to one DK and the lease would not notice,
        #288; a DK makes it plainer, since ``000123456789`` and
        ``123456789`` are two identities and one key). Two devices
        registered with one serial, which would share one of the bench's
        files whatever their types (#290). And a serial that spells another
        device's lease name, which shares a bench file while the lease
        files differ.

        ``host-power`` and ``takeover`` are names the lease directory keeps
        for itself. Nothing can reach either file any more, since every key
        carries its type's prefix or is 32 hex digits; refusing one is a
        guard on the registry, because a device registered under a name of
        the harness's own is a mistake whoever reads that registry next
        should not have to know about.

        Compared without regard to case throughout: one serial written two
        ways is still one device, while the two spellings would name two
        files. That refuses a pair of spellings that could not have
        collided; a registry edit is the cheaper of the two mistakes.
        """
        # Every name a device is held under, in one space. A claim carries
        # the device that has it, whether the name is a serial or a lease
        # key (the two read differently in a refusal), and the spelling to
        # quote. Reserved names are claimed by nobody.
        taken: dict[str, _LockName] = {
            name: _LockName(None, "reserved", what, name)
            for name, what in _RESERVED_LEASE_NAMES.items()
        }
        for resource in sorted(self.resources.values(), key=lambda r: r.alias):
            for name, claim in _lock_names(resource).items():
                held = taken.get(name)
                if held is None:
                    taken[name] = claim
                    continue
                if held.alias == resource.alias:
                    # This device's other spelling of its own serial (a
                    # J-Link serial with and without its leading zeros).
                    continue
                raise RegistryError(self._one_name(name, held, claim))

    def _one_name(self, name: str, held: "_LockName", claim: "_LockName") -> str:
        """Why these two devices cannot both be registered under one name."""
        if held.kind == "reserved":
            has_it = (
                f"is registered with the serial '{claim.spelling}'"
                if claim.kind == "serial"
                else f"would be leased as '{claim.spelling}'"
            )
            return (
                f"{claim.describes} {has_it}, which is the name the lease "
                f"directory keeps for {held.describes}; a device registered under "
                "one of the harness's own file names is a registry mistake, so "
                "give it the serial its own hardware reports"
            )
        if held.kind == "lease" and claim.kind == "lease":
            return (
                f"{held.describes} and {claim.describes} would both be leased as "
                f"'{claim.spelling}'; one lease file is one device, so the two "
                "would be held in turn through it while everything that reads "
                "the registry treated them as two devices"
            )
        if held.kind == "lease" or claim.kind == "lease":
            # One device's serial is the other's lease name. Neither file is
            # shared today: their lease files differ, because every key but a
            # board's carries its type's prefix, and each device's own
            # external file is named after its plain serial alone. Refused
            # anyway, because a registry in which one device's serial is
            # another's lease file reads as a mistake to whoever edits it
            # next, and nothing is lost by saying so.
            return (
                f"the serial of {claim.describes} is the name {held.describes} "
                f"is leased under: '{name}' is both of those names, so a "
                "registry that keeps them apart depends on which name a "
                "command happens to take; give each device the serial its own "
                "hardware reports"
            )
        # Two serials, which is the field a person writes. The name that
        # collides, not either device's spelling of it: a DK is also known
        # by its serial without the leading zeros, so the spelling in the
        # registry may be neither device's own.
        if self.resources[held.alias].type == self.resources[claim.alias].type:
            return (
                f"{self.resources[claim.alias].type}s {held.alias} and "
                f"{claim.alias} are registered with the same serial '{name}'; "
                "one serial is one device, whichever way it is written, and the "
                "two would be held through one of the bench's lock files"
            )
        return (
            f"'{name}' is the serial of both {held.describes} and "
            f"{claim.describes}; the bench's own lock file for a device is named "
            "after its plain serial, so a lock of one would be read as a lock of "
            "the other"
        )

    def _validate_pairs(self) -> None:
        """Permanently wired pairs: one target each, on one host, no loop.

        A pair is held as one device (see ``pair_members``), so a registry
        that describes an impossible pairing has to be refused here rather
        than leave a command holding half of one.
        """
        programmer_of: dict[str, str] = {}
        for resource in sorted(self.resources.values(), key=lambda r: r.alias):
            target = resource.programs
            if target is None:
                continue
            alias = resource.alias
            # The type first: a resource that may not be wired to anything is
            # that mistake whatever it named, including a `programs:` line
            # with nothing after it (load() keeps one as the empty string, so
            # it is not read as a resource with no pairing at all).
            if resource.type != DEBUG_PROGRAMMER:
                raise RegistryError(
                    f"resource {alias} is a {resource.type}, which does not "
                    f"'{PROGRAMS_FIELD}' another device; only a "
                    f"{DEBUG_PROGRAMMER} is wired to one"
                )
            # Checked before any lookup: a YAML list or mapping is not even
            # hashable, and is the mistake of naming more than one target.
            if not isinstance(target, str) or not target:
                raise RegistryError(
                    f"resource {alias} must name the one device it "
                    f"{PROGRAMS_FIELD} by alias"
                )
            if target == alias:
                raise RegistryError(
                    f"{DEBUG_PROGRAMMER} {alias} cannot program itself"
                )
            if target not in self.resources:
                raise RegistryError(
                    f"{DEBUG_PROGRAMMER} {alias} programs unknown device '{target}'"
                )
            if self.resources[target].type not in _leased_types():
                # A pair is held as one, so a target no lock can be named
                # after (a host, a hub) would be accepted here and then
                # refuse every command that named either member, naming a
                # resource the person never asked for.
                raise RegistryError(
                    f"{DEBUG_PROGRAMMER} {alias} programs {target}, which is a "
                    f"{self.resources[target].type}; a pair is two devices the "
                    f"harness leases ({', '.join(_leased_types())})"
                )
            if self.resources[target].attached_to != resource.attached_to:
                raise RegistryError(
                    f"{DEBUG_PROGRAMMER} {alias} and {target} are not attached to "
                    "one host; a pair is held through that host's leases, which "
                    "are local to it"
                )
            if target in programmer_of:
                raise RegistryError(
                    f"{DEBUG_PROGRAMMER}s {programmer_of[target]} and {alias} both "
                    f"program {target}; a device has one programmer"
                )
            programmer_of[target] = alias
        self._refuse_pair_loop()

    def _refuse_pair_loop(self) -> None:
        """Refuse a ring of programmers, each programming the next.

        Every programmer has one target and every target one programmer (the
        checks above), so a loop is a ring: following ``programs`` from one
        of its members comes back to it. Expansion survives a loop anyway
        (``pair_members`` keeps a visited set), but a ring would hold every
        member of itself for one command, which no bench means.
        """
        targets = {
            resource.alias: resource.programs
            for resource in self.resources.values()
            if isinstance(resource.programs, str)
        }
        for start in sorted(targets):
            path, current = [start], targets[start]
            while isinstance(current, str) and current in targets:
                if current in path:
                    raise RegistryError(
                        f"{DEBUG_PROGRAMMER} pairing loops back on itself: "
                        + ", ".join(
                            f"{alias} {PROGRAMS_FIELD} {targets[alias]}"
                            for alias in path
                        )
                    )
                path.append(current)
                current = targets[current]

    def _pair_neighbors(self) -> dict[str, set[str]]:
        """Who is wired to whom, both ways, by alias.

        Undirected: a command that names either member of a pair holds both,
        so the walk below does not care which side is the programmer.
        """
        neighbors: dict[str, set[str]] = {}
        for resource in self.resources.values():
            target = resource.programs
            if not isinstance(target, str) or target not in self.resources:
                continue
            neighbors.setdefault(resource.alias, set()).add(target)
            neighbors.setdefault(target, set()).add(resource.alias)
        return neighbors

    def pair_members(self, alias: str) -> tuple[str, ...]:
        """Every device permanently wired to this one, in alias order.

        A debug programmer and the device it programs are one piece of
        hardware as far as using either is concerned: programming the target
        while another command owns it, or using the target while it is being
        programmed, corrupts a test or leaves the board needing hands on it.
        So every command that takes one takes the other (leases.expand_pairs).

        The whole connected set, so a chain of pairs is covered too, and
        never the alias itself. The walk keeps a visited set, so a registry
        built with a loop in it (validation refuses one, a registry made in
        code need not have passed it) still terminates.
        """
        neighbors = self._pair_neighbors()
        seen, queue = {alias}, [alias]
        while queue:
            for other in sorted(neighbors.get(queue.pop(), ())):
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        return tuple(sorted(seen - {alias}))

    def describe_pairing(self, alias: str, named: str) -> str:
        """Why ``alias`` comes with ``named``, as a clause for a message.

        One wording, so a note from ``hold``, a power change, and a listing
        all explain the pair the same way. Callers add their own tail.
        """
        this = self.resources.get(alias)
        other = self.resources.get(named)
        if this is not None and this.programs == named:
            return f"{alias} is the programmer wired to {named}"
        if other is not None and other.programs == alias:
            return f"{alias} is the device {named} programs"
        # A chain: neither is the other's own pair, but they are connected
        # through one, and holding either holds both.
        return f"{alias} is wired to {named} through a programmer pair"

    def expand_pairs(self, aliases: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The aliases to hold for these, and why each added one is there.

        Every alias given, deduplicated and in the order given, followed by
        each one's pair members in alias order. Naming both members is
        harmless: the result holds each device once. The second tuple is one
        clause per added alias (``describe_pairing``), for the note a command
        prints before it takes the locks.
        """
        held = list(dict.fromkeys(aliases))
        named = set(held)
        added: dict[str, str] = {}
        for alias in held:
            for other in self.pair_members(alias):
                if other not in named:
                    added.setdefault(other, alias)
        extra = sorted(added)
        return (
            tuple(held + extra),
            tuple(self.describe_pairing(other, added[other]) for other in extra),
        )

    def get(self, alias: str) -> Resource:
        try:
            return self.resources[alias]
        except KeyError as error:
            raise RegistryError(f"unknown resource '{alias}'") from error

    def named(self, alias, what: str = "resource") -> Resource:
        """One resource a plan named, telling absence from a misspelling.

        A name this registry does not have is absence (``MissingResource``):
        another bench may well have it. A name that matches a registered one
        but for its case cannot be absence, since aliases are written by
        hand in both files, so it is reported as the typo it is.
        """
        if not isinstance(alias, str):
            raise RegistryError(f"{what} must be named by alias, not {alias!r}")
        if alias in self.resources:
            return self.resources[alias]
        close = sorted(
            name for name in self.resources if name.lower() == alias.lower()
        )
        if close:
            raise RegistryError(
                f"this registry has no {what} '{alias}'; it has "
                f"'{close[0]}', so check the spelling in the plan"
            )
        raise MissingResource(f"this registry has no {what} '{alias}'")

    def hosts(self) -> tuple[Resource, ...]:
        return tuple(
            sorted(
                (resource for resource in self.resources.values() if resource.type == "Host"),
                key=lambda resource: resource.alias,
            )
        )

    def resolve_hosts(self, selector) -> tuple[str, ...]:
        if selector == "each":
            hosts = [host.alias for host in self.hosts() if "test target" in host.roles]
            if not hosts:
                raise RegistryError("registry has no hosts with the test target role")
            return tuple(hosts)
        if isinstance(selector, (tuple, list)) and selector:
            aliases = tuple(selector)
        elif isinstance(selector, str):
            aliases = (selector,)
        else:
            raise RegistryError("hosts must be each or explicit host aliases")
        for alias in aliases:
            if self.named(alias, "host").type != "Host":
                raise RegistryError(f"resource {alias} is not a Host")
        return aliases

    def hardware(self, host: str, resource_type: str) -> tuple[str, ...]:
        """Every resource of this type attached to the host, by alias."""
        return tuple(
            sorted(
                resource.alias
                for resource in self.resources.values()
                if resource.type == resource_type and resource.attached_to == host
            )
        )

    def placed(
        self,
        host: str,
        resource_type: str,
        placement: PlacementRequirement,
        role: str | None = None,
    ) -> tuple[str, ...]:
        """Those of them whose RF placement satisfies the requirement.

        With a role, only those that may also be bound to it, so a caller
        asking whether the host can place a role gets the same answer
        ``resolve_hardware`` will give.
        """
        return tuple(
            alias
            for alias in self.hardware(host, resource_type)
            if placement.matches(self.get(alias).rf)
            and self._takes_role(self.get(alias), role)
        )

    def _placements(self, aliases) -> str:
        """What each of these resources is placed as, for a refusal message."""
        return "; ".join(
            f"{alias} has {describe_placement(self.get(alias).rf)}" for alias in aliases
        ) or "none is registered"

    def resolve_hardware(
        self,
        host: str,
        resource_type: str,
        selector: str,
        role: str | None = None,
        placement: PlacementRequirement | None = None,
    ) -> tuple[str, ...]:
        host_resource = self.get(host)
        if host_resource.type != "Host":
            raise RegistryError(f"resource {host} is not a Host")
        eligible = self.hardware(host, resource_type)
        # A requirement narrows the choice before the selector is read:
        # 'default' takes the host's default only when it satisfies the
        # requirement, and 'each' covers the matching resources alone.
        # Role-filtered, so a resource that could never take the role is not
        # offered as the one that satisfies the placement.
        matching = (
            self.placed(host, resource_type, placement, role) if placement else ()
        )

        def refuse_unplaced():
            # A host where nothing could take the role wherever it sat has
            # not got the hardware at all, which is the more useful answer
            # than one about a placement.
            if role and not any(
                self._takes_role(self.get(alias), role) for alias in eligible
            ):
                refuse_roleless()
            raise RegistryError(
                f"host {host} has no {resource_type} with "
                f"{placement.describe()} ({self._placements(eligible)})"
            )

        def refuse_roleless():
            """The host has units of the type, none of them for this role.

            Absence: the plan left the choice to the harness, so there is
            nothing to correct in it; this bench has no such unit. Every unit
            of the type here declares roles, or one of them would have taken
            the role, so each is named with what it does declare.
            """
            declared = "; ".join(
                f"{alias} declares {', '.join(self.get(alias).roles)}"
                for alias in eligible
            )
            raise MissingResource(
                f"host {host} has no {resource_type} for the {role} role "
                f"({declared})"
            )

        if selector == "default":
            # A test that binds several resources of one type (the connection
            # pair's central and peripheral) needs a default per role, so the
            # host may name one by role as well as by type.
            alias = host_resource.defaults.get(role) if role else None
            if alias is None:
                alias = host_resource.defaults.get(resource_type)
            if alias is None:
                raise MissingResource(
                    f"host {host} has no default {resource_type}"
                    + (f" for the {role} role" if role else "")
                )
            if alias not in eligible:
                raise RegistryError(
                    f"default {alias} is not a {resource_type} attached to {host}"
                )
            if placement and alias not in matching:
                if not matching:
                    refuse_unplaced()
                # The host's default is not placed as this role needs, so the
                # lowest-alias resource that is takes the role instead. The
                # run record and the report name the resource used and the
                # requirement it was chosen for.
                alias = matching[0]
            if not self._takes_role(self.get(alias), role):
                # The host's default cannot take this role, so the
                # lowest-alias unit on the host that can takes it, as it does
                # for a placement the default cannot satisfy. Only a host
                # where nothing can take the role has not got the hardware.
                for_role = tuple(
                    candidate for candidate in (matching or eligible)
                    if self._takes_role(self.get(candidate), role)
                )
                if not for_role:
                    refuse_roleless()
                alias = for_role[0]
            return (alias,)
        if selector == "each":
            if not eligible:
                raise MissingResource(f"host {host} has no {resource_type} resources")
            if not placement:
                return eligible
            if not matching:
                refuse_unplaced()
            return matching
        if selector in ("any", "random"):
            raise RegistryError(f"ambiguous selector '{selector}' is unsupported")
        resource = self.named(selector, resource_type)
        if resource.type != resource_type or resource.attached_to != host:
            raise RegistryError(
                f"resource {selector} is not a {resource_type} attached to {host}"
            )
        # The role first: a resource excluded from `matching` for its role
        # would otherwise be refused for its placement, which is not why.
        self._declares_role(resource, role)
        if placement and selector not in matching:
            # An explicit alias the plan asked for: a placement it cannot
            # satisfy is a mistake in the plan, not a resource to swap out.
            raise RegistryError(
                f"resource {selector} does not have {placement.describe()} "
                f"(it has {describe_placement(resource.rf)})"
            )
        return (selector,)

    @staticmethod
    def _takes_role(resource, role: str | None) -> bool:
        """Whether this resource may be bound to the role.

        A resource with no roles (an Ubertooth) is bound by type alone, so
        anything goes; a DK registered as the peripheral must not be handed
        to a test that wants an advertiser, which would otherwise show up
        only as a capture that holds nothing.
        """
        return role is None or not resource.roles or role in resource.roles

    @classmethod
    def _declares_role(cls, resource, role: str | None) -> None:
        """Refuse a resource that declares roles and not this one."""
        if cls._takes_role(resource, role):
            return
        raise RegistryError(
            f"resource {resource.alias} declares the role(s) "
            f"{', '.join(resource.roles)}, not {role}"
        )

    def reconcile(self, host: str, discovered: Sequence[Mapping]) -> Reconciliation:
        self.get(host)
        expected = {
            resource.alias: resource
            for resource in self.resources.values()
            if resource.attached_to == host and resource.identity
        }
        unmatched = list(discovered)
        present = []
        conflicting = []
        for alias, resource in expected.items():
            for index, item in enumerate(unmatched):
                if dict(item.get("identity", {})) != dict(resource.identity):
                    continue
                if item.get("type") == resource.type:
                    present.append(alias)
                else:
                    conflicting.append(alias)
                unmatched.pop(index)
                break
        missing = sorted(set(expected) - set(present) - set(conflicting))
        return Reconciliation(
            present=tuple(sorted(present)),
            missing=tuple(missing),
            unknown=tuple(MappingProxyType(dict(item)) for item in unmatched),
            conflicting=tuple(sorted(conflicting)),
        )
