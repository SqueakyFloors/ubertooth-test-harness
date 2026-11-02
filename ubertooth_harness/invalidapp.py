"""Commands for the Reset into invalid application test.

Run as ``python <this file> COMMAND ...`` from a transition step. Every
command prints short summary lines (never image bytes) and exits 0 when the
check holds, 1 when it does not, and 2 for a usage error.

What the test is about. The bootloader decides on every power-up whether
there is an application it can start, from the two words at the start of the
application region (firmware/bootloader/app_image.h). An image whose words
are not startable -- erased flash, which is what an interrupted download
leaves -- must leave the bootloader running, so the next image can be written
over USB. Before that check existed, such a board faulted on every power-up
and only came back if someone bridged the entry pin at the bench.

So this test writes an image the bootloader must refuse, cuts the board's USB
power, turns it back on, and requires the board to come up as a DFU
bootloader rather than as its application. Then it writes the run's own image
back and checks the revision.

The commands:

- ``make`` writes the unstartable image beside the run's test image and says
  what makes it unstartable and what still makes it a valid Ubertooth DFU
  file, so the record shows the host had no reason to refuse it.
- ``cycle`` power-cycles the board's registered port and checks what comes
  back. It restores the port's power on every exit, including a failed check,
  an error from the controller or the bus, and SIGTERM or SIGINT.
- ``port`` reads the port and switches it back on if it is off. It is the
  case's first cleanup step, so a run killed outright (SIGKILL, which no
  process can handle) still leaves the port powered.
- ``recover`` returns the board to its application with the test image: it
  writes the image to the single attached DFU device, waits for the
  application, and reads the flash back. It does nothing when the board
  already runs the test image, so the same command serves as the case's step
  and as its cleanup.

``cycle`` and ``port`` need the resource registry (the board's ``power:``
mapping says which controller and port, never the command line) and
``--allow-power-changes``, and the run holds the host lock for the whole
test: nothing else may write firmware or switch power beside it.

Identifying the board. A board in its DFU bootloader reports no serial, so
``-U`` cannot reach it and USB enumeration cannot name it. It is identified
by elimination, as the DFU images test does it: exactly one serial-less
Ubertooth device attached, and every other registered board enumerated by
its own serial. Anything else refuses before a port is switched.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import signal
import sys
import time
from typing import Callable, Sequence

if __package__ in (None, ""):
    # Run as a script: make the package importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ubertooth_harness import dfuimage  # noqa: E402
from ubertooth_harness.enumeration import (  # noqa: E402
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_REMOVAL_DEADLINE_S,
    DEFAULT_RETURN_DEADLINE_S,
    DEFAULT_SETTLE_S,
    DFU_BOOTLOADER_PRODUCT,
    EnumerationError,
    UbertoothEnumerator,
    is_unidentified,
    local_enumerator,
    wait_for_enumeration,
)
from ubertooth_harness.power import (  # noqa: E402
    PowerController,
    PowerControllerError,
    controller_from_resource,
    preserved_power_state,
)
from ubertooth_harness.resources import (  # noqa: E402
    FileResourceRegistry,
    RegistryError,
)
from ubertooth_harness.topology import PowerCycleTarget, plan_power_cycle  # noqa: E402

# The image the hardware case writes: both vector words erased, the state an
# interrupted download leaves at the start of the application region.
UNSTARTABLE_KIND = "erased-vectors"

# Enumeration deadlines. The defaults are the measured ones (enumeration.py):
# a board leaves USB within about 0.2 s of power-off and comes back within
# about 0.6 s of power-on, and a state must hold for the settle time to
# count. A board that refuses to start its application re-enumerates as its
# bootloader, which is the same USB attach.
REMOVAL_DEADLINE_S = DEFAULT_REMOVAL_DEADLINE_S
RETURN_DEADLINE_S = DEFAULT_RETURN_DEADLINE_S
SETTLE_S = DEFAULT_SETTLE_S
POLL_INTERVAL_S = DEFAULT_POLL_INTERVAL_S


class InvalidAppError(RuntimeError):
    """A precondition of the test does not hold, so nothing was changed."""


class PowerCycleInterrupted(BaseException):
    """A signal arrived during the power cycle; the port is restored first.

    A BaseException, like KeyboardInterrupt: an ``except Exception`` inside
    the controller or enumeration code must not swallow it and carry on with
    the port switched off.
    """


@dataclass(frozen=True)
class BoardState:
    """What the bus must show while the board is outside its application.

    A board in its DFU bootloader reports no serial, so it is counted, not
    named: ``bootloaders`` is how many serial-less Ubertooth devices must be
    enumerated -- one while the board sits in its bootloader, none while its
    port is off. Every guard board must be enumerated by its own serial
    throughout, which is also what makes the one serial-less device this
    board rather than somebody else's.
    """

    selected: str
    guards: frozenset[str]
    bootloaders: int

    def problems(self, seen: frozenset[str]) -> list[str]:
        problems = []
        if self.selected in seen:
            problems.append(
                f"the selected board {self.selected} is enumerated by its "
                "serial, so it is running an application"
            )
        missing = sorted(self.guards - seen)
        if missing:
            problems.append("guard board(s) gone: " + ", ".join(missing))
        extra = sorted(
            {serial for serial in seen if not is_unidentified(serial)}
            - self.guards
            - {self.selected}
        )
        if extra:
            problems.append("unregistered Ubertooth enumerated: " + ", ".join(extra))
        found = sorted(serial for serial in seen if is_unidentified(serial))
        other = [
            serial for serial in found
            if not serial.endswith(f":{DFU_BOOTLOADER_PRODUCT}")
        ]
        if other:
            problems.append(
                "serial-less Ubertooth that is not a DFU bootloader: "
                + ", ".join(other)
            )
        elif len(found) != self.bootloaders:
            problems.append(
                f"{len(found)} DFU bootloader device(s) enumerated, expected "
                f"{self.bootloaders}"
                + (f" ({', '.join(found)})" if found else "")
            )
        return problems


def _registry_target(
    hardware: str, board_alias: str, controller_alias: str | None
) -> tuple[FileResourceRegistry, PowerCycleTarget]:
    """The registry and the port the board under test is registered on."""
    registry = FileResourceRegistry.load(hardware)
    board = registry.get(board_alias)
    if board.attached_to is None:
        raise RegistryError(f"resource {board_alias} is not attached to a host")
    return registry, plan_power_cycle(
        registry, board.attached_to, board_alias, controller_alias
    )


def _controller(
    registry: FileResourceRegistry,
    target: PowerCycleTarget,
    *,
    allow_power_changes: bool,
    controller_factory=controller_from_resource,
) -> PowerController:
    if not allow_power_changes:
        # The same opt-in run-suite requires of power isolation. Without it
        # this command reads nothing and switches nothing, so running it by
        # hand cannot take a board off the bus by accident.
        raise InvalidAppError(
            "switching port power needs --allow-power-changes (run-suite "
            "passes it on from its own option)"
        )
    return controller_factory(
        registry.get(target.controller_alias), allow_real_changes=True
    )


def ensure_power_on(
    controller: PowerController,
    port: int,
    log: Callable[[str], None],
    attempts: int = 2,
    *,
    switched_off: bool = True,
) -> str:
    """Leave the port powered, whatever state it is in; "" or why it is not.

    Reads first and switches only a port that is off, so the ordinary case
    (the cycle restored it already) changes nothing. Nothing is raised out of
    here, not even a signal: this runs on the way out of a command that may
    already be failing, and the one thing its caller must always learn is
    whether the port ended up powered. A reconnect is part of a retry, so an
    I/O error that closed the controller's link does not cost the port.
    """
    reason = ""
    for attempt in range(1, attempts + 1):
        try:
            controller.connect()
            if controller.snapshot(port).power_enabled:
                log(f"Port {port} power: on")
                return ""
            log(f"Port {port} power: off; switching it back on")
            controller.set_power_enabled(port, True)
            if not controller.snapshot(port).power_enabled:
                raise PowerControllerError(f"port {port} did not come back on")
            log(f"Port {port} power: on")
            return ""
        # BaseException as well: a signal arriving here must not end the
        # command with the port switched off. The handler that raised it has
        # already put the default back, so a second one kills the process and
        # the case's cleanup step restores the port.
        except BaseException as error:  # noqa: BLE001 - reported, never raised
            reason = f"port {port} is not powered: {error}"
            if attempt < attempts:
                log(f"Port {port}: {error}; trying once more")
    if switched_off:
        log(f"PORT {port} STILL OFF: {reason}. The board is off the bus until "
            "someone switches it on by hand (README, \"USB power controllers\")")
    else:
        # Nothing was switched off here, so the port is as some earlier
        # command left it; what is missing is the confirmation.
        log(f"PORT {port} NOT CONFIRMED POWERED: {reason}. Nothing was "
            "switched off here; read the port with ubertooth-harness power")
    return reason


def _close_quietly(controller: PowerController) -> None:
    try:
        controller.close()
    except Exception:  # noqa: BLE001 - the port state is what matters here
        pass


def _diagnose(enumerator: UbertoothEnumerator, state: BoardState) -> str:
    """What the bus shows now, in the words the next person needs.

    Read after the wait for the bootloader ran out, so the record says which
    of the two bad outcomes it was: the board started the image it should
    have refused, or it is not on the bus at all.
    """
    try:
        seen = enumerator.serials()
    except (EnumerationError, OSError) as error:
        return f"the bus could not be read: {error}"
    if state.selected in seen:
        return (
            "the board started the image the bootloader should have refused: "
            f"it is enumerated as {state.selected}. Its flash holds an "
            "unstartable image, so write a good one back before using it"
        )
    if not any(is_unidentified(serial) for serial in seen):
        return (
            "the board is not on the bus at all, in its application or its "
            "bootloader. A board that faults on every power-up needs the P4 "
            "entry pin bridged by hand at the bench"
        )
    return "the bus shows: " + ", ".join(sorted(seen))


def power_cycle(
    target: PowerCycleTarget,
    controller: PowerController,
    enumerator: UbertoothEnumerator,
    state: BoardState,
    *,
    log: Callable[[str], None] = print,
    removal_deadline_s: float = REMOVAL_DEADLINE_S,
    return_deadline_s: float = RETURN_DEADLINE_S,
    settle_s: float = SETTLE_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Cut the board's port power, restore it, and check what comes back.

    Before anything is switched, the bus must already show the board outside
    its application: one DFU bootloader device, no selected serial, every
    guard in place. Then the port goes off until the bootloader device is
    gone and has stayed gone for the settle time, the port is restored, and
    the bootloader device must come back -- a cold boot in which the
    bootloader refused to start the image in flash.

    The port's power is restored on every exit from this function. The
    restoration itself is checked by reading the port back, and a port that
    cannot be restored is the failure the command reports, whatever else
    happened.
    """
    timing = {
        "poll_interval_s": poll_interval_s, "clock": clock, "sleep": sleep,
    }
    off = BoardState(state.selected, state.guards, 0)
    failure = ""
    unpowered = ""
    switched_off = False
    try:
        controller.connect()
        before = controller.snapshot(target.port)
        if not before.power_enabled:
            raise InvalidAppError(
                f"{target.controller_alias} port {target.port} is already off, "
                "so this board's state is not known; power it on and rerun"
            )
        try:
            # Bounded, not a single scan: the reset that ubertooth-dfu made
            # after writing the image may still be re-enumerating the
            # bootloader when this step starts.
            wait_for_enumeration(
                enumerator, state, return_deadline_s, settle_s=settle_s,
                **timing
            )
        except EnumerationError as error:
            raise InvalidAppError(
                "the board is not sitting in its DFU bootloader before the "
                f"power cycle: {error}"
            ) from error
        log(f"Before the cycle: {_describe(state)}")
        with preserved_power_state(controller, (target.port,)):
            log(f"Switching {target.controller_alias} port {target.port} off")
            # Before the call, not after: a write that failed on its
            # read-back may still have cut the port, and the restoration
            # below has to be reported as what it is either way.
            switched_off = True
            controller.set_power_enabled(target.port, False)
            waited = wait_for_enumeration(
                enumerator, off, removal_deadline_s, settle_s=settle_s, **timing
            )
            log(f"Board off the bus after {waited:.1f}s, and still gone "
                f"{settle_s:g}s later")
        # preserved_power_state restored the port here, reading it back.
        log(f"Port {target.port} power restored")
        try:
            waited = wait_for_enumeration(
                enumerator, state, return_deadline_s, settle_s=settle_s, **timing
            )
        except EnumerationError as error:
            raise InvalidAppError(
                f"{error}; {_diagnose(enumerator, state)}"
            ) from error
        log(f"Board back on the bus after {waited:.1f}s as a DFU bootloader "
            "device, not as its application")
    except InvalidAppError as error:
        failure = str(error)
    except (PowerControllerError, EnumerationError, OSError) as error:
        failure = f"power cycle failed: {error}"
    except PowerCycleInterrupted as error:
        failure = f"power cycle interrupted: {error}"
    finally:
        # Whatever happened above, including a signal: the port must be
        # powered when this returns, and the command must say so.
        unpowered = ensure_power_on(controller, target.port, log,
                                    switched_off=switched_off)
        _close_quietly(controller)
    if unpowered and switched_off:
        # A port this command switched off and could not switch back on is
        # the worst outcome there is, so it is what the command reports.
        log(f"Power cycle: {unpowered}"
            + (f"; the check itself failed too: {failure}" if failure else ""))
        return 1
    if failure or unpowered:
        failure = failure or unpowered
        log(f"Power cycle: {failure}")
        return 1
    log("Power cycle: the bootloader refused the unstartable image, as it must")
    return 0


def _describe(state: BoardState) -> str:
    guards = ", ".join(sorted(state.guards)) or "none registered"
    return (f"{state.bootloaders} DFU bootloader device, no "
            f"{state.selected}, guard board(s) {guards}")


def make_unstartable_image(
    source: Path, output: Path, kind: str = UNSTARTABLE_KIND,
    *, log: Callable[[str], None] = print,
) -> int:
    """Write the unstartable image and say what the board should make of it.

    The run's own test image is the source, so the file differs from the
    image the board already runs in the two words the bootloader reads and
    nothing else. Both halves are then checked and printed: the host has no
    reason to refuse the file (valid suffix, firmware info block present),
    and the bootloader must refuse to start it.
    """
    image = source.read_bytes()
    if dfuimage.suffix_problems(image):
        log("Source image is not a valid DFU file: "
            + "; ".join(dfuimage.suffix_problems(image)))
        return 1
    startable = dfuimage.startable_problems(image)
    if startable:
        # The board runs this image, so it had better be startable; if it is
        # not, the source is not what the run thinks it is.
        log("Source image would not start either: " + "; ".join(startable))
        return 1
    data = dfuimage.make_unstartable(kind, image)
    output.write_bytes(data)
    problems = dfuimage.suffix_problems(data)
    info = dfuimage.fw_info(data)
    if problems or info is None:
        log("Unstartable image is not a valid DFU file: "
            + "; ".join(problems or ["no firmware info block"]))
        return 1
    reasons = dfuimage.startable_problems(data)
    if not reasons:
        log("Unstartable image would still start; nothing to test")
        return 1
    stack_pointer, reset_vector = dfuimage.app_vectors(data)
    log(f"Image ({kind}): {len(data)} bytes, DFU suffix valid, firmware info "
        f"{info.image} {info.revision}, so ubertooth-dfu will write it")
    log(f"Vectors: stack pointer 0x{stack_pointer:08x}, reset vector "
        f"0x{reset_vector:08x}")
    log("Unstartable: " + "; ".join(reasons))
    return 0


def recover(
    board: dfuimage.Board,
    image: Path,
    revision: str,
    readback: Path,
    *,
    log: Callable[[str], None] = print,
) -> int:
    """Leave the board in its application running ``image``.

    Three states are handled, so the same command is the case's recovery step
    and its cleanup. A board that already runs the test image is left alone.
    A board in its application running something else is written with ``-U``.
    A board in its bootloader has no serial, so it is identified by
    elimination and written without a selector.

    One failure is not handled here, as it is not in the DFU images test: a
    read-back that fails and leaves the board in DFU despite ``-r``
    (SqueakyFloors/ubertooth#208, seen about once in twenty). In the case's
    own step that fails the case and the cleanup writes the board back; in
    the cleanup it fails the cleanup, which stops the run on the record
    rather than leaving a board nobody knows the state of.
    """
    seen = board.revision()
    if seen is not None:
        log(f"Board answers in its application: {seen}")
        failure = board.read_back(readback)
        if failure:
            log(f"Recovery: {failure}")
            return 1
        data = image.read_bytes()
        if seen == revision and not dfuimage.readback_problems(
            readback.read_bytes(), data
        ):
            log("Recovered: the board already runs the test image")
            return 0
        log("Flash or revision differs from the test image; writing it")
        failure = board.write(image)
        if failure:
            log(f"Recovery: {failure}")
            return 1
    else:
        log("Board does not answer in its application; looking for its "
            "DFU bootloader")
        failure = board.adopt_dfu_device()
        if failure:
            log(f"Recovery: {failure}")
            return 1
        failure = board.write_unselected(image)
        if failure:
            log(f"Recovery: {failure}")
            return 1
    returned = board.wait_revision()
    if returned != revision:
        log(f"Recovery: board answers {returned}, expected {revision}"
            if returned else
            "Recovery: board did not return to its application")
        return 1
    failure = board.read_back(readback)
    if failure:
        log(f"Recovery: {failure}")
        return 1
    problems = dfuimage.readback_problems(
        readback.read_bytes(), image.read_bytes()
    )
    if problems:
        log("Recovery: flash still differs from the test image: "
            + "; ".join(problems))
        return 1
    log(f"Recovered: application flash holds the test image, firmware {returned}")
    return 0


# ---------------------------------------------------------------------------
# Step timeouts: the worst case of each command if every tool call and every
# wait takes as long as it is allowed to, plus a quarter and 15 s for
# interpreter start-up and file reads. A step never kills a command of its
# own accord mid-flash or mid-cycle.

def cycle_worst_s() -> float:
    # Two controller reads and a write before the cycle, the removal wait,
    # the restoration (a read, a write, a read), the return wait, and the
    # power check on the way out. The controller calls are milliseconds on
    # both drivers; the waits are what this costs.
    return REMOVAL_DEADLINE_S + RETURN_DEADLINE_S + 5.0


def recover_worst_s(guards: int) -> float:
    return (dfuimage.REVISION_TIMEOUT_S
            + dfuimage.leave_dfu_worst_s(guards)
            + dfuimage.DFU_WRITE_TIMEOUT_S
            + dfuimage.DFU_READ_TIMEOUT_S * 2
            + dfuimage.wait_worst_s())


def step_timeout_s(command: str, guards: int = 0) -> float:
    if command == "cycle":
        worst = cycle_worst_s()
    elif command == "recover":
        worst = recover_worst_s(guards)
    elif command == "port":
        worst = 10.0
    else:
        raise ValueError(f"'{command}' is not a timed device command")
    return worst * 1.25 + 15.0


# ---------------------------------------------------------------------------
# Command line

def _install_signal_handlers() -> None:
    """Turn a termination signal into an exception, once.

    run-suite ends a command it is cancelling with SIGTERM, and a person
    presses Ctrl-C, so the ordinary ways this command is stopped have to
    unwind through its restoration rather than stop the process where it
    stands. Each handler puts the default back before raising, so a second
    signal still kills the process; the case's ``port`` cleanup step is what
    restores the port after that, and after a SIGKILL, which nothing can
    handle.
    """
    def stop(signum, _frame):
        signal.signal(signum, signal.SIG_DFL)
        raise PowerCycleInterrupted(signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invalidapp.py")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make", help="write the unstartable image")
    make.add_argument("--image", required=True, help="the run's test image")
    make.add_argument("--output", required=True)
    make.add_argument("--kind", default=UNSTARTABLE_KIND,
                      choices=dfuimage.UNSTARTABLE_KINDS)
    for name in ("cycle", "port"):
        power = sub.add_parser(name)
        power.add_argument("--hardware", required=True,
                           help="the resource registry")
        power.add_argument("--board", required=True,
                           help="registry alias of the board under test")
        power.add_argument("--controller",
                           help="registry alias of its USB power controller, "
                                "as the plan names it")
        power.add_argument("--allow-power-changes", action="store_true")
        if name == "cycle":
            power.add_argument(
                "--serial", required=True,
                help="the board's serial, which must NOT be enumerated")
            power.add_argument(
                "--guard", action="append", default=[],
                help="another registered board, which must be")
    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("--serial", required=True)
    recover_parser.add_argument("--guard", action="append", default=[])
    recover_parser.add_argument("--image", required=True)
    recover_parser.add_argument("--revision", required=True)
    recover_parser.add_argument("--readback", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    board_factory=dfuimage.Board,
    enumerator_factory=local_enumerator,
    controller_factory=controller_from_resource,
    install_handlers: bool = True,
    timing: dict | None = None,
) -> int:
    """``timing`` overrides the enumeration deadlines (tests only)."""
    try:
        args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as error:
        return 2 if error.code else 0
    try:
        return _main(args, board_factory, enumerator_factory, controller_factory,
                     install_handlers, timing or {})
    except (InvalidAppError, RegistryError, EnumerationError) as error:
        print(f"{args.command}: {error}")
        return 1
    except OSError as error:
        print(f"{args.command}: {error}")
        return 1


def _main(args, board_factory, enumerator_factory, controller_factory,
          install_handlers, timing) -> int:
    if args.command == "make":
        return make_unstartable_image(
            Path(args.image), Path(args.output), args.kind
        )
    if args.command == "recover":
        board = board_factory(args.serial.lower(),
                              [guard.lower() for guard in args.guard])
        return recover(board, Path(args.image), args.revision,
                       Path(args.readback))
    registry, target = _registry_target(args.hardware, args.board, args.controller)
    controller = _controller(
        registry, target,
        allow_power_changes=args.allow_power_changes,
        controller_factory=controller_factory,
    )
    if args.command == "port":
        try:
            unpowered = ensure_power_on(controller, target.port, print)
        finally:
            _close_quietly(controller)
        if unpowered:
            print(f"Port: {unpowered}")
            return 1
        print(f"Port: {target.controller_alias} port {target.port} is powered")
        return 0
    if install_handlers:
        _install_signal_handlers()
    state = BoardState(
        args.serial.lower(),
        frozenset(guard.lower() for guard in args.guard),
        1,
    )
    return power_cycle(target, controller, enumerator_factory(), state, **timing)


if __name__ == "__main__":
    sys.exit(main())
