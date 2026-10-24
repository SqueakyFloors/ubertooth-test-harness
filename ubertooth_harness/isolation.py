"""Build run-time power isolation from the hardware registry.

The registry decides which kind of USB power switch each board sits behind
(the `driver` of its `USB power controller` resource), so one run can use
StarTech and Acroname controllers side by side. Serials for enumeration checks
also come from the registry, never from hand-built maps.
"""

import re
import sys
from typing import Callable, Mapping

from ubertooth_harness.enumeration import (
    EnumerationError,
    is_unidentified,
    SysfsUbertoothEnumerator,
    UbertoothEnumerator,
)
from ubertooth_harness.power import (
    SIMULATED_DRIVER,
    PowerController,
    PowerControllerError,
    controller_from_resource,
)
from ubertooth_harness.resources import ResourceRegistry
from ubertooth_harness.topology import IsolationVerification, exclusive_remedy


_UBERTOOTH_SERIAL = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)


class IsolationSetupError(RuntimeError):
    """Isolation cannot be set up for this run (fails before any switching)."""


def default_enumerator(platform: str, local_platform: str | None = None) -> UbertoothEnumerator:
    local = sys.platform if local_platform is None else local_platform
    if platform == "linux":
        if not local.startswith("linux"):
            raise IsolationSetupError(
                f"host is registered as linux but this machine is '{local}'; "
                "run exclusive-Ubertooth tests on the registered host"
            )
        return SysfsUbertoothEnumerator()
    raise IsolationSetupError(
        f"no USB enumerator for platform '{platform}'; exclusive-Ubertooth "
        "tests can only run on Linux hosts for now"
    )


class IsolationRuntime:
    """Controllers and verification for one host, built from the registry."""

    def __init__(
        self,
        registry: ResourceRegistry,
        host: str,
        *,
        allow_changes: bool,
        enumerator: UbertoothEnumerator | None = None,
        controller_factory: Callable[..., PowerController] = controller_from_resource,
        verification_options: Mapping | None = None,
    ):
        self.registry = registry
        self.host = host
        self.allow_changes = bool(allow_changes)
        self._enumerator = enumerator
        # Simulated controllers are only allowed with an injected (test) enumerator.
        self._enumerator_injected = enumerator is not None
        self._controller_factory = controller_factory
        self._verification_options = dict(verification_options or {})
        self._controllers: dict[str, PowerController] = {}

    @property
    def enumerator(self) -> UbertoothEnumerator:
        if self._enumerator is None:
            self._enumerator = default_enumerator(self.registry.get(self.host).platform)
        return self._enumerator

    def check_ready(self, actions, selected_alias: str | None = None) -> None:
        """Fail before any switching if these actions cannot be applied.

        Checks the opt-in, the platform enumerator, and every registry serial;
        connects to (and closes) each controller to read the ports it will
        switch; then scans USB once. The selected board must be enumerated, no
        unregistered Ubertooth may be attached, and an isolated board whose
        port is already off and that is not enumerated is refused (a killed
        earlier run may have left it off). No port is changed.

        With no actions (an exclusive invocation whose host has no other
        registered board), the check is read-only: no opt-in is needed and no
        controller is built; one USB scan must show the selected board and
        nothing else, identified or unidentified.
        """
        if not actions:
            if selected_alias is not None:
                self._check_exclusive_ready(selected_alias)
            return
        if not self.allow_changes:
            raise IsolationSetupError(
                "exclusive-Ubertooth test requires real power changes; rerun "
                "with --allow-power-changes"
            )
        enumerator = self.enumerator
        selected = self._serial(selected_alias) if selected_alias is not None else None
        isolated = {action.resource_alias: self._serial(action.resource_alias)
                    for action in actions}
        # Read the ports live on every call, so a port turned off between
        # invocations is caught too.
        ports = {}
        for action in actions:
            ports.setdefault(action.controller_alias, []).append(action.port)
        port_states: dict[tuple[str, int], bool] = {}
        for alias in sorted(ports):
            controller = self.controller(alias)
            try:
                controller.connect()
                for port in ports[alias]:
                    port_states[(alias, port)] = controller.snapshot(port).power_enabled
            except Exception as error:  # noqa: BLE001 - any driver error fails closed here
                raise IsolationSetupError(f"power controller {alias}: {error}") from error
            finally:
                try:
                    controller.close()
                except Exception:  # noqa: BLE001 - probe only; next use reconnects
                    pass
        try:
            seen = enumerator.serials()
        except (EnumerationError, OSError) as error:
            raise IsolationSetupError(f"USB enumeration: {error}") from error
        if selected is not None and selected not in seen:
            raise IsolationSetupError(
                f"selected Ubertooth {selected_alias} ({selected}) is not enumerated"
            )
        known = set(isolated.values()) | ({selected} if selected else set())
        unknown = sorted(
            serial for serial in seen if not is_unidentified(serial) and serial not in known
        )
        if unknown:
            raise IsolationSetupError(
                "unregistered Ubertooth attached: " + ", ".join(unknown)
                + "; " + exclusive_remedy("")
            )
        for action in actions:
            if (not port_states[(action.controller_alias, action.port)]
                    and isolated[action.resource_alias] not in seen):
                raise IsolationSetupError(
                    f"{action.resource_alias} is already powered off on "
                    f"{action.controller_alias} port {action.port} and not "
                    "enumerated; a killed earlier run may have left it off. "
                    "Power it back on (see README, manual recovery) and rerun"
                )

    def _check_exclusive_ready(self, selected_alias: str) -> None:
        # The exclusivity rule lives in IsolationVerification.exclusive_problems.
        verification = self.verification(selected_alias, ())  # fails closed off Linux
        try:
            seen = verification.enumerator.serials()
        except (EnumerationError, OSError) as error:
            raise IsolationSetupError(f"USB enumeration: {error}") from error
        problems = "; ".join(verification.exclusive_problems(seen))
        if problems:
            raise IsolationSetupError(
                f"exclusive Ubertooth {selected_alias} ({verification.selected}) "
                f"is not the only Ubertooth enumerated ({problems}); "
                + exclusive_remedy(problems)
            )

    def controller(self, alias: str) -> PowerController:
        if alias not in self._controllers:
            resource = self.registry.get(alias)
            if resource.attached_to != self.host:
                raise IsolationSetupError(
                    f"power controller {alias} is not attached to {self.host}"
                )
            if resource.driver == SIMULATED_DRIVER and not self._enumerator_injected:
                raise IsolationSetupError(
                    f"power controller {alias} is simulated and cannot remove a "
                    "real board from USB enumeration"
                )
            try:
                self._controllers[alias] = self._controller_factory(
                    resource, allow_real_changes=self.allow_changes
                )
            except PowerControllerError as error:
                raise IsolationSetupError(f"power controller {alias}: {error}") from error
        return self._controllers[alias]

    def _serial(self, alias: str) -> str:
        serial = self.registry.get(alias).identity.get("serial", "")
        if not isinstance(serial, str) or not _UBERTOOTH_SERIAL.fullmatch(serial):
            raise IsolationSetupError(
                f"{alias} has no registered 32-hex-digit Ubertooth serial"
            )
        return serial.lower()  # sysfs and libubertooth report lowercase

    def verification(self, selected_alias: str, actions) -> IsolationVerification:
        try:
            return IsolationVerification(
                enumerator=self.enumerator,
                selected_serial=self._serial(selected_alias),
                isolated_serials={
                    action.resource_alias: self._serial(action.resource_alias)
                    for action in actions
                },
                **self._verification_options,
            )
        except EnumerationError as error:
            raise IsolationSetupError(str(error)) from error

    def close(self) -> None:
        for controller in self._controllers.values():
            try:
                controller.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup at run end
                pass
