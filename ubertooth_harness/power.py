"""Deterministic USB power-controller boundary with a no-hardware simulator."""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Mapping, Protocol, Sequence

from ubertooth_harness.resources import (
    STARTECH_DEVICE_PREFIX,
    STARTECH_DRIVER,
    STARTECH_PORTS,
    Resource,
    is_stable_serial_device,
)


USBHUB3P_PORTS = tuple(range(8))
ACRONAME_DRIVER = "acroname BrainStem USBHub3+"
SIMULATED_DRIVER = "simulated USB power controller"

# StarTech managed hubs (e.g. 5G4AINDRM-USB-A-HUB) gate port power through a
# built-in FTDI serial management controller. Standard USB hub power control
# (uhubctl) only changes the reported state on these hubs and does not cut VBUS.
# STARTECH_DRIVER, STARTECH_PORTS, and the by-id path check live in resources
# (shared with registry validation without an import cycle).
# Downstream port 4 carries the hub's own management controller. Its power bit
# reads 0 at power-up while the controller keeps answering, so it may not gate
# the controller at all; it is refused as a precaution so a test can never cut
# the control link. Only ports 1-3 are switchable DUT ports.
STARTECH_CONTROL_PORT = 4
# Management-controller firmware validated on lab hardware (sf-control-server-103).
STARTECH_FIRMWARE_VERSIONS = ("CENTOS000104v02",)
# Only the factory-default password is supported; a hub with a changed
# password fails closed with "rejected by controller".
STARTECH_DEFAULT_PASSWORD = "pass"



class PowerControllerError(RuntimeError):
    """Power-controller identity, read, write, or restoration failed."""


@dataclass(frozen=True)
class PortState:
    port: int
    power_enabled: bool
    data_enabled: bool
    # None when the controller cannot measure VBUS (e.g. StarTech managed hub)
    vbus_microvolts: int | None
    vbus_microamps: int | None


class PowerController(Protocol):
    serial_number: int | None

    def connect(self) -> None:
        """Connect to and verify one deterministic controller."""

    def snapshot(self, port: int) -> PortState:
        """Read one port's power, data, voltage, and current state."""

    def set_power_enabled(self, port: int, enabled: bool) -> None:
        """Set VBUS power and verify readback."""

    def set_data_enabled(self, port: int, enabled: bool) -> None:
        """Set USB data and verify readback."""

    def close(self) -> None:
        """Disconnect from the controller."""


def _validate_serial(serial_number: int) -> int:
    if (
        not isinstance(serial_number, int)
        or isinstance(serial_number, bool)
        or not 0 <= serial_number <= 0xFFFFFFFF
    ):
        raise PowerControllerError(
            "controller serial number must be a 32-bit integer"
        )
    return serial_number


def _validate_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or port not in USBHUB3P_PORTS:
        raise PowerControllerError("USBHub3+ downstream port must be 0-7")
    return port


class SimulatedPowerController:
    """Stateful USBHub3+ model used only for tests and explicit simulation."""

    def __init__(
        self,
        serial_number: int,
        states: Mapping[int, PortState] | None = None,
        *,
        ignore_writes: bool = False,
    ):
        self.serial_number = _validate_serial(serial_number)
        defaults = {
            port: PortState(port, True, True, 5_000_000, 0)
            for port in USBHUB3P_PORTS
        }
        for port, state in dict(states or {}).items():
            _validate_port(port)
            if state.port != port:
                raise PowerControllerError("simulated port state index mismatch")
            defaults[port] = state
        self._states = defaults
        self._connected = False
        self._ignore_writes = ignore_writes

    def connect(self) -> None:
        self._connected = True

    def _require_connected(self) -> None:
        if not self._connected:
            raise PowerControllerError("power controller is not connected")

    def snapshot(self, port: int) -> PortState:
        self._require_connected()
        return self._states[_validate_port(port)]

    def set_power_enabled(self, port: int, enabled: bool) -> None:
        self._require_connected()
        port = _validate_port(port)
        requested = bool(enabled)
        state = self._states[port]
        if not self._ignore_writes:
            self._states[port] = PortState(
                port,
                requested,
                state.data_enabled,
                5_000_000 if requested else 0,
                state.vbus_microamps if requested else 0,
            )
        if self.snapshot(port).power_enabled != requested:
            raise PowerControllerError(
                f"power readback mismatch on port {port}"
            )

    def set_data_enabled(self, port: int, enabled: bool) -> None:
        self._require_connected()
        port = _validate_port(port)
        requested = bool(enabled)
        state = self._states[port]
        if not self._ignore_writes:
            self._states[port] = PortState(
                port,
                state.power_enabled,
                requested,
                state.vbus_microvolts,
                state.vbus_microamps,
            )
        if self.snapshot(port).data_enabled != requested:
            raise PowerControllerError(
                f"data readback mismatch on port {port}"
            )

    def close(self) -> None:
        self._connected = False

    def physical_power(self, port: int) -> bool:
        """Simulated VBUS state, readable without a connection.

        Test hook: lets fake USB enumerators follow physical port power.
        """
        return self._states[_validate_port(port)].power_enabled


class AcronameUSBHub3pController:
    """Lazy BrainStem adapter; real state changes are unarmed by default."""

    def __init__(
        self,
        serial_number: int,
        *,
        allow_changes: bool = False,
        module_factory=None,
        no_error: int | None = None,
        usb_transport=None,
    ):
        self.serial_number = _validate_serial(serial_number)
        self.allow_changes = bool(allow_changes)
        self._module_factory = module_factory
        self._no_error = no_error
        self._usb_transport = usb_transport
        self._hub = None

    def _load_api(self) -> None:
        if self._module_factory is not None:
            if self._no_error is None or self._usb_transport is None:
                raise PowerControllerError(
                    "injected BrainStem API requires transport and no-error value"
                )
            return
        try:
            import brainstem
            from brainstem.link import Spec
            from brainstem.result import Result
        except ImportError as error:
            raise PowerControllerError(
                "BrainStem Python API is not installed"
            ) from error
        self._module_factory = brainstem.stem.USBHub3p
        self._no_error = Result.NO_ERROR
        self._usb_transport = Spec.USB

    def connect(self) -> None:
        if self._hub is not None:
            return
        self._load_api()
        hub = self._module_factory()
        result = hub.discoverAndConnect(
            self._usb_transport, self.serial_number
        )
        if result != self._no_error:
            raise PowerControllerError(
                f"unable to connect to USBHub3+ serial "
                f"0x{self.serial_number:08x}: error {result}"
            )
        identity = hub.system.getSerialNumber()
        try:
            observed = self._result_value(identity, "controller serial")
        except PowerControllerError:
            hub.disconnect()
            raise
        if observed != self.serial_number:
            hub.disconnect()
            raise PowerControllerError(
                "USBHub3+ controller identity mismatch: "
                f"expected 0x{self.serial_number:08x}, "
                f"observed 0x{observed:08x}"
            )
        self._hub = hub

    def _require_hub(self):
        if self._hub is None:
            raise PowerControllerError("power controller is not connected")
        return self._hub

    def _result_value(self, result, operation: str):
        error = getattr(result, "error", None)
        if error != self._no_error:
            raise PowerControllerError(f"{operation} failed with error {error}")
        return result.value

    def snapshot(self, port: int) -> PortState:
        entity = self._require_hub().port[_validate_port(port)]
        return PortState(
            port,
            bool(self._result_value(entity.getPowerEnabled(), "read power")),
            bool(self._result_value(entity.getDataEnabled(), "read data")),
            int(self._result_value(entity.getVbusVoltage(), "read VBUS voltage")),
            int(self._result_value(entity.getVbusCurrent(), "read VBUS current")),
        )

    def _require_changes_armed(self) -> None:
        if not self.allow_changes:
            raise PowerControllerError("real power-controller changes are not armed")

    def set_power_enabled(self, port: int, enabled: bool) -> None:
        self._require_changes_armed()
        entity = self._require_hub().port[_validate_port(port)]
        requested = bool(enabled)
        result = entity.setPowerEnabled(requested)
        if result != self._no_error:
            raise PowerControllerError(
                f"set power on port {port} failed with error {result}"
            )
        observed = bool(
            self._result_value(entity.getPowerEnabled(), "power readback")
        )
        if observed != requested:
            raise PowerControllerError(
                f"power readback mismatch on port {port}"
            )

    def set_data_enabled(self, port: int, enabled: bool) -> None:
        self._require_changes_armed()
        entity = self._require_hub().port[_validate_port(port)]
        requested = bool(enabled)
        result = entity.setDataEnabled(requested)
        if result != self._no_error:
            raise PowerControllerError(
                f"set data on port {port} failed with error {result}"
            )
        observed = bool(
            self._result_value(entity.getDataEnabled(), "data readback")
        )
        if observed != requested:
            raise PowerControllerError(
                f"data readback mismatch on port {port}"
            )

    def close(self) -> None:
        if self._hub is not None:
            self._hub.disconnect()
            self._hub = None


def _encode_startech_mask(mask: int) -> str:
    # The controller exchanges the 32-bit port mask as 8 hex digits, least
    # significant byte first (bit 0 = downstream port 1).
    return mask.to_bytes(4, "little").hex().upper()


def _decode_startech_mask(text: str) -> int:
    if len(text) != 8:
        raise PowerControllerError(f"malformed StarTech port mask '{text}'")
    try:
        return int.from_bytes(bytes.fromhex(text), "little")
    except ValueError as error:
        raise PowerControllerError(
            f"malformed StarTech port mask '{text}'"
        ) from error


def _open_serial(path: str):
    try:
        import serial
    except ImportError as error:
        raise PowerControllerError("pyserial is not installed") from error
    try:
        # exclusive: refuse to share the controller with another harness run
        # or a manual terminal session, which could interleave port writes.
        return serial.Serial(
            path, baudrate=9600, timeout=1, write_timeout=1, exclusive=True
        )
    except (OSError, ValueError, serial.SerialException) as error:
        raise PowerControllerError(
            f"unable to open StarTech controller {path}: {error}"
        ) from error


class StarTechManagedHubController:
    """StarTech managed hub over its serial management controller.

    Identity is the stable serial-device path of the hub's controller (for
    example /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0).
    Real state changes are unarmed by default. The hub cannot measure VBUS or
    switch data separately, so snapshots report data as always enabled and
    voltage/current as unknown (None); callers that need proof of power-off
    must confirm the device disappears from USB enumeration.
    """

    serial_number = None

    def __init__(
        self,
        device_path: str,
        *,
        password: str = STARTECH_DEFAULT_PASSWORD,
        allow_changes: bool = False,
        serial_factory=None,
        firmware_versions: Sequence[str] = STARTECH_FIRMWARE_VERSIONS,
    ):
        if not is_stable_serial_device(device_path):
            raise PowerControllerError(
                "StarTech controller identity must be a stable "
                f"{STARTECH_DEVICE_PREFIX} path"
            )
        if (
            not isinstance(password, str)
            or not 0 < len(password) <= 8
            or not password.isascii()
        ):
            raise PowerControllerError(
                "StarTech password must be 1-8 ASCII characters"
            )
        self.device_path = device_path
        self.allow_changes = bool(allow_changes)
        self.firmware_version = None
        self._password = password.ljust(8)
        self._serial_factory = serial_factory or _open_serial
        self._firmware_versions = tuple(firmware_versions)
        self._link = None

    def connect(self) -> None:
        if self._link is not None:
            return
        link = self._serial_factory(self.device_path)
        self._link = link
        try:
            version = self._command("?Q")
            if version not in self._firmware_versions:
                raise PowerControllerError(
                    f"unvalidated StarTech controller firmware '{version}' "
                    f"at {self.device_path}"
                )
            self._read_mask()
        except Exception:
            self.close()
            raise
        self.firmware_version = version

    def _require_link(self):
        if self._link is None:
            raise PowerControllerError("power controller is not connected")
        return self._link

    def _command(self, command: str) -> str:
        """Send one request and return its reply line.

        Any I/O error, timeout, or malformed reply closes the link, so a late
        reply can never be mistaken for the answer to a later request.
        """
        link = self._require_link()
        try:
            link.reset_input_buffer()  # drop stale bytes before a request
            link.write(command.encode("ascii") + b"\r")
            raw = link.read_until()
        except Exception as error:  # pyserial raises SerialException/OSError
            self.close()
            raise PowerControllerError(
                f"StarTech controller I/O failed on '{command[:2]}': {error}"
            ) from error
        if not raw.endswith(b"\r\n"):
            self.close()
            raise PowerControllerError(
                f"StarTech controller did not answer '{command[:2]}'"
            )
        try:
            return raw[:-2].decode("ascii")
        except UnicodeDecodeError as error:
            self.close()
            raise PowerControllerError(
                f"non-ASCII reply to '{command[:2]}'"
            ) from error

    def _parse_mask(self, text: str) -> int:
        try:
            return _decode_startech_mask(text)
        except PowerControllerError:
            self.close()
            raise

    def _read_mask(self) -> int:
        return self._parse_mask(self._command("GP"))

    @staticmethod
    def _validate_port(port: int) -> int:
        if not isinstance(port, int) or isinstance(port, bool) or port not in STARTECH_PORTS:
            if port == STARTECH_CONTROL_PORT:
                raise PowerControllerError(
                    "StarTech port 4 carries the management controller and "
                    "cannot be switched"
                )
            raise PowerControllerError("StarTech downstream port must be 1-3")
        return port

    def snapshot(self, port: int) -> PortState:
        port = self._validate_port(port)
        powered = bool(self._read_mask() & (1 << (port - 1)))
        return PortState(port, powered, True, None, None)

    def set_power_enabled(self, port: int, enabled: bool) -> None:
        if not self.allow_changes:
            raise PowerControllerError("real power-controller changes are not armed")
        port = self._validate_port(port)
        requested_on = bool(enabled)
        current = self._read_mask()
        bit = 1 << (port - 1)
        requested = (current | bit) if requested_on else (current & ~bit)
        if requested != current:
            reply = self._command(
                "SP" + self._password + _encode_startech_mask(requested)
            )
            if not reply.startswith("G"):
                self.close()
                raise PowerControllerError(
                    f"set power on port {port} rejected by controller"
                )
            if self._parse_mask(reply[1:]) != requested:
                self.close()
                raise PowerControllerError(
                    f"power readback mismatch on port {port}"
                )
        # Full-mask readback also catches any other port changing unexpectedly.
        if self._read_mask() != requested:
            raise PowerControllerError(f"power readback mismatch on port {port}")

    def set_data_enabled(self, port: int, enabled: bool) -> None:
        self._validate_port(port)
        if not enabled:
            raise PowerControllerError(
                "StarTech managed hub cannot switch USB data separately"
            )

    def close(self) -> None:
        self.firmware_version = None
        if self._link is not None:
            try:
                self._link.close()
            finally:
                self._link = None


def snapshot_ports(
    controller: PowerController, ports: Sequence[int]
) -> tuple[PortState, ...]:
    for port in ports:
        if not isinstance(port, int) or isinstance(port, bool):
            raise PowerControllerError("controller port must be an integer")
    # Each controller validates its own port range in snapshot().
    unique = tuple(dict.fromkeys(ports))
    return tuple(controller.snapshot(port) for port in unique)


def _restore_port(controller: PowerController, expected: PortState) -> None:
    # Reconnect if an earlier I/O failure closed the link.
    controller.connect()
    current = controller.snapshot(expected.port)
    if expected.power_enabled and not current.power_enabled:
        controller.set_power_enabled(expected.port, True)
        current = controller.snapshot(expected.port)
    if current.data_enabled != expected.data_enabled:
        controller.set_data_enabled(expected.port, expected.data_enabled)
        current = controller.snapshot(expected.port)
    if current.power_enabled != expected.power_enabled:
        controller.set_power_enabled(expected.port, expected.power_enabled)
    restored = controller.snapshot(expected.port)
    if (
        restored.power_enabled != expected.power_enabled
        or restored.data_enabled != expected.data_enabled
    ):
        raise PowerControllerError(
            f"port {expected.port} restoration readback mismatch"
        )


RESTORE_FAILURE_PREFIX = "power-state restoration failed: "


def restore_port_states(
    controller: PowerController, snapshots: Sequence[PortState]
) -> None:
    """Restore every snapshotted port, attempting each one even if others fail.

    Each port is retried once (reconnecting first) so a single transient I/O
    error neither skips the remaining ports nor leaves that port isolated.
    This may reconnect a closed controller; the caller must close() it after.
    """
    failures = []
    for expected in reversed(tuple(snapshots)):
        for attempt in (1, 2):
            try:
                _restore_port(controller, expected)
                break
            except Exception as error:  # noqa: BLE001 - one port must not stop the rest
                if attempt == 2:
                    failures.append(f"port {expected.port}: {error}")
    if failures:
        raise PowerControllerError(RESTORE_FAILURE_PREFIX + "; ".join(failures))


@contextmanager
def preserved_power_state(
    controller: PowerController, ports: Sequence[int]
) -> Iterator[tuple[PortState, ...]]:
    snapshots = snapshot_ports(controller, ports)
    try:
        yield snapshots
    finally:
        restore_port_states(controller, snapshots)


def parse_controller_serial(value: str) -> int:
    if not isinstance(value, str) or not value:
        raise PowerControllerError("controller identity requires serial")
    try:
        return _validate_serial(int(value, 0))
    except ValueError as error:
        raise PowerControllerError(
            "controller serial must be decimal or 0x-prefixed hexadecimal"
        ) from error


def controller_from_resource(
    resource: Resource,
    *,
    allow_real_changes: bool = False,
    module_factory=None,
    no_error: int | None = None,
    usb_transport=None,
    serial_factory=None,
) -> PowerController:
    if resource.type != "USB power controller":
        raise PowerControllerError(
            f"resource {resource.alias} is not a USB power controller"
        )
    if resource.driver == STARTECH_DRIVER:
        return StarTechManagedHubController(
            resource.identity.get("serial device", ""),
            allow_changes=allow_real_changes,
            serial_factory=serial_factory,
        )
    serial = parse_controller_serial(resource.identity.get("serial", ""))
    if resource.driver == ACRONAME_DRIVER:
        return AcronameUSBHub3pController(
            serial,
            allow_changes=allow_real_changes,
            module_factory=module_factory,
            no_error=no_error,
            usb_transport=usb_transport,
        )
    if resource.driver == SIMULATED_DRIVER:
        return SimulatedPowerController(serial)
    raise PowerControllerError(
        f"unsupported power-controller driver '{resource.driver}'"
    )
