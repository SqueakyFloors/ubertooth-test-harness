"""Plan physical isolation for tests requiring one visible Ubertooth."""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
import time
from types import MappingProxyType
from typing import Callable, Iterator, Mapping

from ubertooth_harness.enumeration import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_REMOVAL_DEADLINE_S,
    DEFAULT_RETURN_DEADLINE_S,
    DEFAULT_SETTLE_S,
    EnumerationAborted,
    EnumerationError,
    EnumerationExpectation,
    UNIDENTIFIED_PREFIX,
    UbertoothEnumerator,
    is_unidentified,
    stable_serials,
    wait_for_enumeration,
)
from ubertooth_harness.power import (
    ACRONAME_DRIVER,
    PowerController,
    RESTORE_FAILURE_PREFIX,
    PowerControllerError,
    USBHUB3P_PORTS,
    restore_port_states,
    snapshot_ports,
)
from ubertooth_harness.resources import (
    STARTECH_DRIVER,
    STARTECH_PORTS,
    RegistryError,
    ResourceRegistry,
)


@dataclass(frozen=True)
class PowerIsolationAction:
    resource_alias: str
    controller_alias: str
    port: int
    power_enabled: bool = False

    def to_dict(self) -> dict:
        return {
            "resource": self.resource_alias,
            "controller": self.controller_alias,
            "port": self.port,
            "power_enabled": self.power_enabled,
        }


def plan_exclusive_ubertooth(
    registry: ResourceRegistry,
    host: str,
    selected_alias: str,
) -> tuple[PowerIsolationAction, ...]:
    selected = registry.get(selected_alias)
    if selected.type != "Ubertooth" or selected.attached_to != host:
        raise RegistryError(
            f"selected resource {selected_alias} is not an Ubertooth on {host}"
        )
    extras = sorted(
        (
            resource
            for resource in registry.resources.values()
            if resource.type == "Ubertooth"
            and resource.attached_to == host
            and resource.alias != selected_alias
        ),
        key=lambda resource: resource.alias,
    )
    actions = []
    for resource in extras:
        controller_alias = resource.power.get("controller")
        port = resource.power.get("port")
        if controller_alias is None or port is None:
            raise RegistryError(
                f"extra Ubertooth {resource.alias} has no power mapping"
            )
        controller = registry.get(controller_alias)
        if "per-port VBUS switching" not in controller.capabilities:
            raise RegistryError(
                f"controller {controller_alias} lacks per-port VBUS switching"
            )
        if controller.driver == ACRONAME_DRIVER and port not in USBHUB3P_PORTS:
            raise RegistryError(
                f"controller {controller_alias} port must be 0-7"
            )
        if controller.driver == STARTECH_DRIVER and port not in STARTECH_PORTS:
            raise RegistryError(
                f"controller {controller_alias} port must be 1-3"
            )
        actions.append(
            PowerIsolationAction(
                resource.alias,
                controller_alias,
                port,
                False,
            )
        )
    return tuple(actions)


@dataclass(frozen=True, eq=False)
class IsolationVerification:
    """Confirm power isolation from USB enumeration (opt-in).

    isolated_serials maps each isolated resource alias to its board serial;
    its keys must match the isolation actions exactly. Before switching, the
    enumeration must be stable and include the selected board. After
    power-off, the selected board must be the only Ubertooth enumerated (an
    isolated board still present, an unknown extra board, or an unidentified
    device such as a board in DFU all fail) and that must hold for settle_s.
    After restoration, every isolated board enumerated beforehand must
    reappear.
    """

    enumerator: UbertoothEnumerator
    selected_serial: str
    isolated_serials: Mapping[str, str]
    removal_deadline_s: float = DEFAULT_REMOVAL_DEADLINE_S
    return_deadline_s: float = DEFAULT_RETURN_DEADLINE_S
    settle_s: float = DEFAULT_SETTLE_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    clock: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)

    def __post_init__(self):
        for name in ("removal_deadline_s", "return_deadline_s", "poll_interval_s"):
            if getattr(self, name) <= 0:
                raise EnumerationError(f"{name} must be positive")
        if self.settle_s < 0:
            raise EnumerationError("settle_s must not be negative")
        if self.settle_s >= min(self.removal_deadline_s, self.return_deadline_s):
            raise EnumerationError(
                "settle_s must be shorter than the removal and return deadlines"
            )
        if not isinstance(self.isolated_serials, Mapping):
            raise EnumerationError("isolated_serials must map aliases to serials")
        values = (self.selected_serial, *self.isolated_serials.values())
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise EnumerationError("isolation verification requires serials")
        if any(":" in value for value in values):
            # Reserved for unidentified devices; must never match a registry serial.
            raise EnumerationError("serials must not contain ':'")
        # Normalize and freeze so the validated inputs cannot change later.
        object.__setattr__(self, "selected_serial", self.selected_serial.strip().lower())
        object.__setattr__(self, "isolated_serials", MappingProxyType({
            alias: serial.strip().lower()
            for alias, serial in self.isolated_serials.items()
        }))
        selected = self.selected_serial
        isolated = list(self.isolated_serials.values())
        if len(set(isolated)) != len(isolated):
            raise EnumerationError("isolated serials must be unique")
        if selected in isolated:
            raise EnumerationError("selected serial cannot also be isolated")

    @property
    def selected(self) -> str:
        return self.selected_serial

    @property
    def isolated(self) -> frozenset[str]:
        return frozenset(self.isolated_serials.values())

    def _timing(self) -> dict:
        return {
            "poll_interval_s": self.poll_interval_s,
            "clock": self.clock,
            "sleep": self.sleep,
        }

    def check_actions(self, actions) -> None:
        if any(action.power_enabled for action in actions):
            raise EnumerationError("verified isolation only supports power-off actions")
        aliases = [action.resource_alias for action in actions]
        if len(set(aliases)) != len(aliases):
            raise EnumerationError("duplicate isolation action for a resource")
        aliases = set(aliases)
        if aliases != set(self.isolated_serials):
            raise EnumerationError(
                "isolated serials must match the isolation actions exactly "
                f"(actions: {sorted(aliases)}, serials: {sorted(self.isolated_serials)})"
            )

    def baseline(self) -> frozenset[str]:
        """Stable pre-switch enumeration (bounded by the return deadline).

        Requires the selected board, and rejects any identified Ubertooth that
        is neither selected nor isolated: isolation could never succeed, and a
        wrong registry serial would otherwise power-cycle a board unchecked.
        Unidentified devices are allowed here, since one may be an isolated
        board sitting in its DFU bootloader.
        """
        seen = stable_serials(
            self.enumerator, self.settle_s, self.return_deadline_s, **self._timing()
        )
        if self.selected not in seen:
            raise EnumerationError(
                f"selected Ubertooth {self.selected} is not enumerated"
            )
        unknown = {
            serial for serial in seen if not is_unidentified(serial)
        } - {self.selected} - self.isolated
        if unknown:
            raise EnumerationError(
                "unregistered Ubertooth enumerated before isolation: "
                + ", ".join(sorted(unknown))
            )
        # Each unidentified device can only be an isolated board without a
        # serial (e.g. in DFU); any more and isolation can never succeed.
        unidentified = [serial for serial in seen if is_unidentified(serial)]
        if len(unidentified) > len(self.isolated - seen):
            raise EnumerationError(
                "unidentified Ubertooth enumerated before isolation: "
                + ", ".join(sorted(unidentified))
            )
        return seen

    def exclusive_problems(self, seen) -> list[str]:
        """Why `seen` is not exactly the selected board (empty if it is)."""
        return EnumerationExpectation.of(
            present=(self.selected,), exactly=(self.selected,)
        ).problems(frozenset(seen))

    def confirm_exclusive(self, abort: Callable[[], bool] | None = None) -> frozenset[str]:
        """Read-only check for an exclusive invocation with no power actions.

        Waits, up to the return deadline, for the selected board to be the
        only Ubertooth enumerated, held for settle_s; a board briefly
        re-enumerating or missing is absorbed, while an extra identified or
        unidentified device that stays attached fails at the deadline.
        Nothing is switched. `abort`, if given, is checked before every scan;
        when it returns true, IsolationCancelled is raised.
        """
        if self.isolated_serials:
            raise EnumerationError("exclusive check is only for invocations without power actions")
        try:
            wait_for_enumeration(
                self.enumerator,
                EnumerationExpectation.of(
                    present=(self.selected,), exactly=(self.selected,)
                ),
                self.return_deadline_s,
                settle_s=self.settle_s,
                abort=abort,
                **self._timing(),
            )
        except EnumerationAborted as error:
            raise IsolationCancelled(str(error)) from error
        except EnumerationError as error:
            raise EnumerationError(
                f"selected Ubertooth {self.selected} is not the only Ubertooth "
                f"enumerated: {error}; {exclusive_remedy(str(error))}"
            ) from error
        return frozenset({self.selected})

    def confirm_isolated(self) -> None:
        wait_for_enumeration(
            self.enumerator,
            EnumerationExpectation.of(
                absent=self.isolated, present=(self.selected,), exactly=(self.selected,)
            ),
            self.removal_deadline_s,
            settle_s=self.settle_s,
            **self._timing(),
        )

    def confirm_returned(self, serials) -> None:
        if serials:
            wait_for_enumeration(
                self.enumerator,
                EnumerationExpectation.of(present=serials),
                self.return_deadline_s,
                **self._timing(),
            )


def exclusive_remedy(problems: str) -> str:
    """How to clear a refused exclusive check, given its problem text."""
    remedy = (
        "unplug the other Ubertooth, or register it on this host with a power "
        "mapping so it can be switched off"
    )
    if UNIDENTIFIED_PREFIX in problems:
        remedy += (
            "; an unidentified entry has no serial and may be a board in its "
            "DFU bootloader"
        )
    if "not enumerated:" in problems:
        remedy += "; check the selected board is plugged in and powered"
    return remedy


class IsolationCancelled(RuntimeError):
    """The run was cancelled before any port was switched."""


@contextmanager
def applied_power_isolation(
    actions: tuple[PowerIsolationAction, ...],
    controller_provider: Callable[[str], PowerController],
    verification: IsolationVerification | None = None,
    observer: Callable[..., None] | None = None,
    abort: Callable[[], bool] | None = None,
) -> Iterator[Mapping[str, PowerController]]:
    """Apply compiled power-off actions, then restore every port on exit.

    With verification, isolation is confirmed by USB enumeration before the
    body runs and the isolated boards must re-enumerate after restoration.
    Error precedence: a failure while switching or restoring wins and skips
    the return check (restored power cannot be trusted), as does an interrupt
    such as KeyboardInterrupt; otherwise an error from isolation
    confirmation or the body wins and a return failure is attached to it as a
    note; if everything else succeeded, a return failure is raised.

    `observer`, if given, is called with "applied" (and the original state of
    every switched port) once every action is switched, "restored" once every
    port is restored, and "returned" once the isolated boards re-enumerate
    (verification only). `abort`, if given, is checked after the baseline
    wait; when it returns true, IsolationCancelled is raised before any port
    is switched.
    """
    notify = observer or (lambda _step, _details=None: None)
    returning = frozenset()
    if verification is not None:
        verification.check_actions(actions)
        returning = verification.baseline() & verification.isolated
    if abort is not None and abort():
        raise IsolationCancelled("canceled before switching")
    inner_error = None
    original = []
    try:
        with _switched(actions, controller_provider, original, abort) as controllers:
            try:
                notify("applied", {"original": list(original)})
                if verification is not None:
                    verification.confirm_isolated()
                yield controllers
            except BaseException as error:
                inner_error = error
                raise
    except BaseException as error:
        if error is not inner_error:
            # Switching or restoration failed: propagate without waiting for
            # boards to return (restored power cannot be trusted).
            raise
        notify("restored")
        if verification is None or not isinstance(error, Exception):
            # Interrupted (Ctrl-C, exit): do not wait for boards to return.
            raise
        try:
            verification.confirm_returned(returning)
        except Exception as return_error:  # the body/isolation error must win
            error.add_note(f"after restoration: {return_error!r}")
        else:
            notify("returned")
        raise
    notify("restored")
    if verification is not None:
        # A board that does not come back means restoration cannot be
        # trusted, even though the body succeeded.
        verification.confirm_returned(returning)
        notify("returned")


@contextmanager
def _switched(
    actions: tuple[PowerIsolationAction, ...],
    controller_provider: Callable[[str], PowerController],
    original: list | None = None,
    abort: Callable[[], bool] | None = None,
) -> Iterator[Mapping[str, PowerController]]:
    """Switch the actions; on exit restore every controller, then close it.

    Every controller is restored even if another fails, and all failures are
    reported together, each naming its controller alias. Closing is
    best-effort: a close error cannot undo a restore that succeeded.
    `original`, if given, receives the snapshotted state of every port.
    """
    grouped = {}
    for action in actions:
        grouped.setdefault(action.controller_alias, []).append(action)
    controllers = {}
    failures = []

    def raise_restore_failures():
        if failures:
            raise PowerControllerError(RESTORE_FAILURE_PREFIX + "; ".join(failures))

    with ExitStack() as stack:
        stack.callback(raise_restore_failures)  # runs last
        for alias in sorted(grouped):
            controller = controller_provider(alias)
            controller.connect()
            stack.callback(_close_quietly, controller)
            ports = tuple(action.port for action in grouped[alias])
            snapshots = snapshot_ports(controller, ports)
            if original is not None:
                original.extend(
                    {"controller": alias, "port": snap.port,
                     "power_enabled": snap.power_enabled}
                    for snap in snapshots
                )

            def restore(alias=alias, controller=controller, snapshots=snapshots):
                try:
                    restore_port_states(controller, snapshots)
                except PowerControllerError as error:
                    failures.append(f"{alias} {str(error).removeprefix(RESTORE_FAILURE_PREFIX)}")

            stack.callback(restore)
            controllers[alias] = controller
        if abort is not None and abort():
            raise IsolationCancelled("canceled before switching")
        for action in actions:
            controllers[action.controller_alias].set_power_enabled(
                action.port, action.power_enabled
            )
        yield MappingProxyType(controllers)


def _close_quietly(controller: PowerController) -> None:
    try:
        controller.close()
    except Exception:  # noqa: BLE001 - the ports are already restored
        pass
