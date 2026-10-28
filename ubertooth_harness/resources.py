"""Typed resource registry for hosts and attached lab hardware."""

import posixpath
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

import yaml


_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class RegistryError(ValueError):
    """Resource configuration or selection is invalid."""


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
# Roles the DK role firmware (firmware/nrf52840dk) can take; only the
# advertiser is implemented so far, the connection pair is planned (#87).
DK_ROLES = ("advertiser", "central", "peripheral")
_DK_IDENTITY_KEYS = ("jlink serial", "BLE address", "serial device")
_JLINK_SERIAL = re.compile(r"^[0-9]{6,12}$")
_BLE_ADDRESS = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


def _validate_dk(resource) -> None:
    """An nRF52840 DK: J-Link serial, optional BLE address and VCOM0 path."""
    alias = resource.alias
    unknown = sorted(set(resource.identity) - set(_DK_IDENTITY_KEYS))
    if unknown:
        raise RegistryError(
            f"nRF52840 DK {alias} identity has unknown key(s) {', '.join(unknown)}; "
            f"use {', '.join(_DK_IDENTITY_KEYS)}"
        )
    if not _JLINK_SERIAL.fullmatch(resource.identity.get("jlink serial", "")):
        raise RegistryError(
            f"nRF52840 DK {alias} requires a 'jlink serial' identity (6-12 digits)"
        )
    address = resource.identity.get("BLE address")
    if address is not None and not _BLE_ADDRESS.fullmatch(address):
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

    def resolve_hardware(
        self, host: str, resource_type: str, selector: str, role: str | None = None
    ) -> tuple[str, ...]:
        """Resolve default, each, or explicit hardware deterministically."""


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
            )
        return cls(resources)

    def _validate(self) -> None:
        identities = {}
        for resource in self.resources.values():
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
        for resource in self.resources.values():
            if resource.type == NRF52840_DK:
                _validate_dk(resource)
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

    def get(self, alias: str) -> Resource:
        try:
            return self.resources[alias]
        except KeyError as error:
            raise RegistryError(f"unknown resource '{alias}'") from error

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
            if self.get(alias).type != "Host":
                raise RegistryError(f"resource {alias} is not a Host")
        return aliases

    def resolve_hardware(
        self, host: str, resource_type: str, selector: str, role: str | None = None
    ) -> tuple[str, ...]:
        host_resource = self.get(host)
        if host_resource.type != "Host":
            raise RegistryError(f"resource {host} is not a Host")
        eligible = tuple(
            sorted(
                resource.alias
                for resource in self.resources.values()
                if resource.type == resource_type and resource.attached_to == host
            )
        )
        if selector == "default":
            # A test that binds several resources of one type (the connection
            # pair's central and peripheral) needs a default per role, so the
            # host may name one by role as well as by type.
            alias = host_resource.defaults.get(role) if role else None
            if alias is None:
                alias = host_resource.defaults.get(resource_type)
            if alias is None:
                raise RegistryError(
                    f"host {host} has no default {resource_type}"
                    + (f" for the {role} role" if role else "")
                )
            if alias not in eligible:
                raise RegistryError(
                    f"default {alias} is not a {resource_type} attached to {host}"
                )
            self._declares_role(self.get(alias), role)
            return (alias,)
        if selector == "each":
            if not eligible:
                raise RegistryError(f"host {host} has no {resource_type} resources")
            return eligible
        if selector in ("any", "random"):
            raise RegistryError(f"ambiguous selector '{selector}' is unsupported")
        resource = self.get(selector)
        if resource.type != resource_type or resource.attached_to != host:
            raise RegistryError(
                f"resource {selector} is not a {resource_type} attached to {host}"
            )
        self._declares_role(resource, role)
        return (selector,)

    @staticmethod
    def _declares_role(resource, role: str | None) -> None:
        """A resource that declares roles may only be bound to one of them.

        A resource with no roles (an Ubertooth) is bound by type alone, so
        nothing is checked; a DK registered as the peripheral must not be
        handed to a test that wants an advertiser, which would otherwise show
        up only as a capture that holds nothing.
        """
        if role is None or not resource.roles or role in resource.roles:
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
