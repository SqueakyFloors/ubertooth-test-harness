"""Command-line interface for the local harness slice."""

import argparse
import contextlib
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from ubertooth_harness.build import (
    BuildError,
    Builder,
    homebrew_include_dir,
    plan_build,
    require_tools,
    resolve_python,
)
from ubertooth_harness.cancellation import CancellationToken

from ubertooth_harness.environment import (
    HarnessEnvironmentError,
    require_harness_environment,
)
from ubertooth_harness.events import (
    TerminalEventSink,
    open_run_publisher,
    create_run_directory,
    generate_run_id,
    validate_run_id,
)
from ubertooth_harness.executor import SubprocessCommandExecutor
from ubertooth_harness.isolation import IsolationRuntime, IsolationSetupError
from ubertooth_harness.flash import HarnessError
from ubertooth_harness import leases as board_leases
from ubertooth_harness import libbtbbpin
from ubertooth_harness.manifest import (
    FileIdentity,
    GitRevisionResolver,
    ManifestError,
    build_run_manifest,
)
from ubertooth_harness.hostneeds import planned_capabilities, split_cases
from ubertooth_harness.plan import PlanError, check_runnable, image_label, load_plan
from ubertooth_harness import pytools
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.planner import PlanCompiler, PlanningError
from ubertooth_harness.preflight import PreflightError, PreflightPolicy
from ubertooth_harness.resources import FileResourceRegistry, RegistryError
from ubertooth_harness.report import render_runs, write_report
from ubertooth_harness.sequencer import NamedTestSequencer, run_started_details
from ubertooth_harness.stateful import StateCaseError
from ubertooth_harness.transitions import guard_boards, ubertooth_roles


_DEFAULT_PLAN = Path(__file__).resolve().parents[1] / "testplans" / "slow.yaml"


def _detected_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise HarnessError(f"unsupported host platform: {sys.platform}")


def _add_build_options(parser: argparse.ArgumentParser, *, optional: bool) -> None:
    if optional:
        parser.add_argument(
            "--build",
            action="store_true",
            help="build the resolved commits into the run directory and test "
            "those tools (requires --libbtbb-source/--libbtbb-ref)",
        )
    parser.add_argument(
        "--with-firmware",
        action="store_true",
        help="also build bluetooth_rxtx firmware (needs arm-none-eabi-gcc); "
        "the image is recorded, not flashed",
    )
    parser.add_argument(
        "--with-python",
        nargs="?",
        const="python3",
        metavar="PYTHON",
        help="also build the Python tools (ENABLE_PYTHON=ON) for PYTHON "
        "(default python3 on PATH), recorded in the manifest; "
        "ubertooth-specan-ui also needs numpy, qtpy and a Qt binding there",
    )


def _minutes(value: str) -> float:
    minutes = float(value)
    if not 0 <= minutes <= 24 * 60:
        raise argparse.ArgumentTypeError("must be between 0 and 1440 minutes")
    return minutes


def _add_lease_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--owner",
        help="who holds the boards (agent, issue); default "
        f"${board_leases.OWNER_ENV} or the user name",
    )
    parser.add_argument(
        "--wait-for-boards", type=_minutes, default=0.0, metavar="MINUTES",
        help="wait up to MINUTES for leased boards instead of failing at once",
    )
    # Tests and unusual hosts only; the default is shared by every agent.
    parser.add_argument("--lease-dir", type=Path, help=argparse.SUPPRESS)
    _add_external_lock_option(parser)


def _add_external_lock_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--external-board-lock", metavar="TEMPLATE",
        help="also hold each board's own lock file from another tool on this "
        "host: an absolute path with {serial} (the full 32-hex serial), e.g. "
        "'/var/lock/board-{serial}.lock'; default "
        f"${board_leases.EXTERNAL_LOCK_ENV}; '' turns it off",
    )


def _external_template(args: argparse.Namespace) -> str | None:
    return board_leases.external_lock_template(args.external_board_lock)


@contextlib.contextmanager
def _signals_cancel(token: CancellationToken):
    """SIGINT/SIGTERM cancel the token instead of interrupting the process."""
    previous = {}

    def request(signum, _frame):
        token.cancel(f"received {signal.Signals(signum).name}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request)
    try:
        yield token
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _build(
    sources, work_dir: Path, with_firmware: bool, publisher, with_python=None, *, run_dir: Path
):
    """Build resolved sources into work_dir, recording into the run's publisher
    (long command output goes to run_dir's output/)."""
    platform = _detected_platform()
    require_tools(platform, with_firmware)
    python = resolve_python(with_python) if with_python else None
    by_name = {source.name: source for source in sources}
    probe = SubprocessCommandExecutor()
    plan = plan_build(
        ubertooth=by_name["Ubertooth"],
        libbtbb=by_name["libbtbb"],
        work_dir=work_dir,
        platform=platform,
        with_firmware=with_firmware,
        extra_include_dir=homebrew_include_dir(probe) if platform == "macos" else None,
        python=python,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    with _signals_cancel(CancellationToken()) as token:
        return Builder(
            SubprocessCommandExecutor(plan.environment()),
            publisher,
            run_dir=run_dir,
            cancellation=token,
        ).run(plan)


def _finish_failed_run(publisher, output_dir: Path, reason: str) -> None:
    """Close a run that failed before its tests started, then write report.md."""
    try:
        publisher.emit(
            "run_finished", "failed", {"status": "failed", "failure_reason": reason}
        )
    finally:
        write_report(output_dir)


def _resolve_sources(args: argparse.Namespace):
    if bool(args.libbtbb_source) != bool(args.libbtbb_ref):
        raise ManifestError(
            "--libbtbb-source and --libbtbb-ref must be supplied together"
        )
    resolver = GitRevisionResolver()
    sources = [
        resolver.resolve(
            "Ubertooth", args.ubertooth_source, args.ubertooth_ref,
            getattr(args, "ubertooth_ref_name", None),
        )
    ]
    if args.libbtbb_source:
        sources.append(
            resolver.resolve("libbtbb", args.libbtbb_source, args.libbtbb_ref)
        )
    return tuple(sources)


def _terminal_sink() -> TerminalEventSink:
    """The live stderr stream; quiet about a libbtbb pin check already shown."""
    quiet = {libbtbbpin.EVENT} if os.environ.get(libbtbbpin.SHOWN_ENV) else set()
    return TerminalEventSink(sys.stderr, quiet_events=quiet)


def _record_libbtbb_pin(publisher, sources) -> None:
    """Record how the libbtbb compares with the fork's install.sh pin.

    A libbtbb other than the fork's pin may be deliberate: the record warns
    and the run goes on (#85). The terminal stream prints it on stderr.
    """
    check = libbtbbpin.check_sources(sources)
    if check is not None:
        publisher.emit(libbtbbpin.EVENT, check.status, check.details())


def _build_command(args: argparse.Namespace) -> int:
    sources = _resolve_sources(args)
    if Path(args.output).exists():
        raise HarnessError(f"build output already exists: {args.output}")
    output = Path(args.output)
    output.mkdir(parents=True)
    publisher = open_run_publisher(output, generate_run_id(), _terminal_sink())
    publisher.emit("run_started", "running", run_started_details("build", "build", "", sources))
    _record_libbtbb_pin(publisher, sources)
    try:
        result = _build(
            sources, output, args.with_firmware, publisher, args.with_python, run_dir=output
        )
    except BuildError as error:
        _finish_failed_run(publisher, output, str(error))
        raise
    publisher.emit("run_finished", "passed", {"status": "passed", "failure_reason": ""})
    write_report(output)
    print(json.dumps({
        "sources": [source.to_dict() for source in sources],
        "build": result.to_manifest(),
        "artifacts": [item.to_dict() for item in result.artifacts],
    }, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ubertooth-harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-plan", help="validate named tests")
    validate.add_argument("plan", type=Path, nargs="?", default=_DEFAULT_PLAN)
    validate.add_argument("--hardware", type=Path)
    validate.add_argument("--suite")

    run_suite = subparsers.add_parser(
        "run-suite", help="compile and run named tests for one local host"
    )
    run_suite.add_argument("--plan", type=Path, required=True)
    run_suite.add_argument("--suite", required=True)
    run_suite.add_argument("--hardware", type=Path, required=True)
    run_suite.add_argument("--host", required=True)
    run_suite.add_argument("--ubertooth-source", type=Path, required=True)
    run_suite.add_argument("--ubertooth-ref", required=True)
    run_suite.add_argument(
        "--ubertooth-ref-name", metavar="NAME",
        help="branch or tag name --ubertooth-ref (a commit) was chosen by; "
        "recorded in the manifest and report, not resolved",
    )
    run_suite.add_argument("--libbtbb-source", type=Path)
    run_suite.add_argument("--libbtbb-ref")
    run_suite.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="immutable build artifact to hash into the run manifest; repeatable",
    )
    run_suite.add_argument(
        "--test", action="append", help="exact test name; repeat to select"
    )
    run_suite.add_argument("--run-id")
    run_suite.add_argument(
        "--allow-power-changes",
        action="store_true",
        help="allow real USB power switching for exclusive-Ubertooth tests",
    )
    run_suite.add_argument("--output", type=Path, required=True)
    run_suite.add_argument(
        "--dry-run", action="store_true",
        help="print the planned invocations and cases, and what is not "
        "applicable on this host and why; build, lease and run nothing",
    )
    _add_build_options(run_suite, optional=True)
    _add_lease_options(run_suite)

    boards = subparsers.add_parser(
        "boards",
        help="show which registered Ubertooths, and this host's power/firmware "
        "lock, are free or leased, and by whom",
    )
    boards.add_argument("--hardware", type=Path, required=True)
    boards.add_argument("--host", help="only boards attached to this host")
    boards.add_argument("--lease-dir", type=Path, help=argparse.SUPPRESS)
    _add_external_lock_option(boards)
    boards.add_argument("--json", action="store_true", help="machine-readable output")

    lease = subparsers.add_parser(
        "lease",
        help="hold boards' leases while running one command, e.g. manual ubertooth-util use",
    )
    lease.add_argument("--hardware", type=Path, required=True)
    lease.add_argument("boards", nargs="+", metavar="ALIAS", help="registered Ubertooth aliases")
    lease.add_argument(
        "--host-lock", action="store_true",
        help="also hold this host's power/firmware lock, for a command that "
        "switches USB power or writes firmware (automatic for ubertooth-dfu)",
    )
    _add_lease_options(lease)
    lease.epilog = "usage: ubertooth-harness lease --hardware H ALIAS... -- COMMAND [ARG...]"

    build = subparsers.add_parser(
        "build",
        help="build and install resolved Ubertooth and libbtbb commits into a prefix",
    )
    build.add_argument("--ubertooth-source", type=Path, required=True)
    build.add_argument("--ubertooth-ref", required=True)
    build.add_argument("--libbtbb-source", type=Path, required=True)
    build.add_argument("--libbtbb-ref", required=True)
    build.add_argument("--output", type=Path, required=True,
                       help="new directory for sources, build trees, and the prefix")
    _add_build_options(build, optional=False)

    report = subparsers.add_parser(
        "report", help="render one Markdown report from one or more run directories"
    )
    report.add_argument("runs", type=Path, nargs="+", help="run directories (or run.jsonl files)")
    report.add_argument("--output", type=Path, help="write here instead of stdout")
    report.add_argument("--title", help="report title")
    report.add_argument(
        "--redact-serials", action="store_true",
        help="replace device serial numbers with <serial>",
    )

    runner = subparsers.add_parser(
        "runner",
        help="persistent target runner: dial a loopback controller and run "
        "suites it requests",
    )
    runner.add_argument(
        "--controller", required=True, metavar="URL",
        help="ws:// or wss:// controller URL; the host must be loopback",
    )
    runner.add_argument("--hardware", type=Path, required=True)
    runner.add_argument("--host", required=True)
    runner.add_argument("--ubertooth-source", type=Path, required=True)
    runner.add_argument("--libbtbb-source", type=Path)
    runner.add_argument("--libbtbb-ref")
    runner.add_argument(
        "--plans-dir", type=Path, default=_DEFAULT_PLAN.parent,
        help="directory of <plan>.yaml files a run request may name",
    )
    runner.add_argument("--output", type=Path, required=True)
    runner.add_argument(
        "--allow-power-changes", action="store_true",
        help="pass --allow-power-changes to every run (set here, never by "
        "the controller)",
    )
    runner.add_argument(
        "--heartbeat-interval", type=float, default=10.0, metavar="SECONDS",
    )
    _add_external_lock_option(runner)

    controller = subparsers.add_parser(
        "controller",
        help="controller service on 127.0.0.1: target registry, run dispatch, "
        "results store (remote access: SSH tunnel)",
    )
    controller.add_argument(
        "--port", type=int, default=8765,
        help="TCP port on 127.0.0.1 (0 picks a free port); the address is fixed",
    )
    controller.add_argument(
        "--results-dir", type=Path, metavar="DIR",
        help="run directories (default: $XDG_DATA_HOME/ubertooth-harness/"
        "controller/runs, else ~/.local/share/...)",
    )
    controller.add_argument(
        "--ubertooth-repo", metavar="URL",
        help="Ubertooth repository whose branches and tags (git ls-remote) the "
        "GUI offers and a run may name (default: the public fork, "
        "https://github.com/SqueakyFloors/ubertooth.git)",
    )
    controller.add_argument(
        "--plans-dir", type=Path, default=_DEFAULT_PLAN.parent, metavar="DIR",
        help="directory of <plan>.yaml files the GUI lists",
    )
    controller.add_argument(
        "--host", "--bind", "--address", dest="refused_bind",
        action=_RefuseBind, help=argparse.SUPPRESS,
    )

    return parser


class _RefuseBind(argparse.Action):
    """There is no bind option; say why instead of 'unrecognized argument'."""

    def __init__(self, option_strings, dest, **kwargs):
        kwargs.setdefault("nargs", "?")
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(
            f"{option_string} is not supported: the controller binds 127.0.0.1 "
            "only; use an SSH tunnel for remote access (DESIGN.md section 8)"
        )


def _controller(args: argparse.Namespace) -> int:
    import asyncio

    from ubertooth_harness.controller import (
        Controller,
        default_results_dir,
        refuse_bind_overrides,
        serve,
    )

    from ubertooth_harness.gui import DEFAULT_UBERTOOTH_REPOSITORY, GitRefSource

    refuse_bind_overrides()
    repository = args.ubertooth_repo or DEFAULT_UBERTOOTH_REPOSITORY
    controller = Controller(
        args.results_dir or default_results_dir(),
        ref_source=GitRefSource(repository),
        plans_dir=args.plans_dir,
    )
    controller.log(f"refs from {controller.ref_source.display}")
    controller.log(f"results in {controller.results_dir}")

    def stop(signum, frame):
        raise KeyboardInterrupt

    # The server shuts down gracefully on SIGINT/SIGTERM, then re-raises the
    # signal; end with a clean exit rather than a traceback.
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(serve(controller, args.port))
    except KeyboardInterrupt:
        controller.log("stopped")
    return 0


def _runner(args: argparse.Namespace) -> int:
    import asyncio

    from ubertooth_harness.runner_service import RunnerConfig, RunnerService

    service = RunnerService(RunnerConfig(
        controller_url=args.controller,
        hardware=args.hardware,
        host=args.host,
        ubertooth_source=args.ubertooth_source,
        output=args.output,
        plans_dir=args.plans_dir,
        libbtbb_source=args.libbtbb_source,
        libbtbb_ref=args.libbtbb_ref,
        allow_power_changes=args.allow_power_changes,
        heartbeat_interval_s=args.heartbeat_interval,
        external_board_lock=args.external_board_lock,
    ))
    host = FileResourceRegistry.load(service.config.hardware).get(args.host)
    if host.type != "Host":
        raise HarnessError(f"resource {args.host} is not a Host")

    async def run_service():
        loop = asyncio.get_running_loop()
        # SIGHUP too: a dropped SSH session cancels the run and restores boards.
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(signum, service.request_stop)
        await service.run_forever()

    asyncio.run(run_service())
    return 0


def _validate(
    plan_path: Path,
    hardware_path: Path | None = None,
    suite: str | None = None,
) -> int:
    catalog = builtin_catalog()
    plan = load_plan(plan_path, catalog)
    print(f"plan: {plan.name}" + ("" if plan.runnable else " (not runnable: planned tests)"))
    for suite_name, steps in plan.suites.items():
        print(f"{suite_name}: {len(steps)} named test(s)")
        for step in steps:
            print(f"  - {step.test_name}")
    check_runnable(plan, catalog)
    if hardware_path is not None:
        registry = FileResourceRegistry.load(hardware_path)
        compiler = PlanCompiler(catalog, registry)
        selected_suites = (suite,) if suite else tuple(plan.suites)
        for suite_name in selected_suites:
            compiled = compiler.compile_suite(plan, suite_name)
            invocations = compiled.invocations
            print(f"compiled {suite_name}: {len(invocations)} invocation(s)")
            for item in compiled.not_applicable:
                print(f"  {item.describe()}")
            for invocation in invocations:
                resources = ", ".join(
                    f"{role}={alias}" for role, alias in invocation.resources.items()
                )
                suffix = f" [{resources}]" if resources else ""
                print(
                    f"  {invocation.invocation_id}: {invocation.host}{suffix}"
                )
                for action in invocation.power_isolation:
                    print(
                        "    power: "
                        f"{action.resource_alias}=off via "
                        f"{action.controller_alias} port {action.port}"
                    )
                try:
                    guard_boards(
                        invocation.resource_identities,
                        ubertooth_roles(catalog.get(invocation.test_name).hardware_roles),
                    )
                except StateCaseError as error:
                    # Planning stays permissive; run-suite refuses this host.
                    print(f"    warning: {error}; run-suite refuses this invocation")
                if invocation.exclusive_ubertooth and not invocation.power_isolation:
                    print(
                        "    exclusive: no other registered Ubertooth; "
                        "read-only enumeration check"
                    )
    return 0


def _prepare_run(
    args, plan, invocations, sources, artifacts, run_id, preflight_policy,
    publisher, run_dir, leases=None,
):
    """Build (when requested), then the manifest and preflight checks."""
    built = None
    executor = SubprocessCommandExecutor(_test_environment(sources, None))
    if args.build:
        built = _build(
            sources, run_dir / "build", args.with_firmware, publisher, args.with_python,
            run_dir=run_dir,
        )
        artifacts = artifacts + built.artifacts
        names = {item.name for item in artifacts}
        if built.plan.with_firmware and "test build" not in names:
            # The firmware just built is the plan's test image.
            artifacts += (FileIdentity.from_path("test build", built.plan.firmware_image),)
        # Every test command runs the tools just built, not installed ones.
        executor = SubprocessCommandExecutor(_test_environment(sources, built.plan))
    manifest = build_run_manifest(
        run_id=run_id,
        plan_name=plan.name,
        suite=args.suite,
        host=args.host,
        plan_path=args.plan,
        hardware_path=args.hardware,
        sources=tuple(sources),
        invocations=invocations,
        artifacts=artifacts,
        build=built.to_manifest() if built else None,
        leases=leases.to_manifest() if leases is not None else (),
    )
    preflight_checks = preflight_policy.compile(invocations, manifest)
    return manifest, preflight_checks, executor, built


def _test_environment(sources, plan) -> dict:
    """Environment of the test commands: the build's (prefix first) when the
    run built, and where pytools.py finds the resolved Ubertooth commit."""
    env = plan.environment() if plan is not None else {
        key: value for key, value in os.environ.items()
        if key not in (pytools.PYTHON_ENV, pytools.PYTHON_TOOLS_ENV)
    }
    ubertooth = next(source for source in sources if source.name == "Ubertooth")
    # The build's clone is at the resolved commit; the given checkout holds it.
    env[pytools.SOURCE_ENV] = (
        str(plan.work_dir / "src" / "ubertooth") if plan is not None
        else ubertooth.source_path
    )
    env[pytools.COMMIT_ENV] = ubertooth.commit
    return env


def _parse_artifacts(values: list[str]) -> tuple[FileIdentity, ...]:
    artifacts = []
    names = set()
    for value in values:
        if "=" not in value:
            raise ManifestError("artifact must use NAME=PATH")
        name, raw_path = value.split("=", 1)
        if not name or not raw_path:
            raise ManifestError("artifact must use non-empty NAME=PATH")
        if name in names:
            raise ManifestError(f"duplicate artifact name '{name}'")
        names.add(name)
        artifacts.append(FileIdentity.from_path(name, raw_path))
    return tuple(artifacts)


def _run_suite(args: argparse.Namespace) -> int:
    # A bad template fails before anything else, dry run included.
    external_template = _external_template(args)
    catalog = builtin_catalog()
    plan = load_plan(args.plan, catalog)
    registry = FileResourceRegistry.load(args.hardware)
    host = registry.get(args.host)
    if host.type != "Host":
        raise HarnessError(f"resource {args.host} is not a Host")
    if host.platform != _detected_platform():
        raise HarnessError(
            f"host {args.host} platform {host.platform} does not match local platform"
        )
    compiled = PlanCompiler(catalog, registry).compile_suite(plan, args.suite)
    selected_names = set(args.test or ())
    for name in selected_names:
        catalog.get(name)
    invocations = tuple(
        invocation
        for invocation in compiled.invocations
        if invocation.host == args.host
        and (not selected_names or invocation.test_name in selected_names)
    )
    # Whole tests this host cannot run (hostneeds.py), recorded, not run.
    not_applicable = tuple(
        item for item in compiled.for_host(args.host)
        if not selected_names or item.test_name in selected_names
    )
    if not invocations:
        raise HarnessError(
            f"suite {args.suite} has no selected invocation for {args.host}"
            + "".join(f"; {item.describe()}" for item in not_applicable)
        )
    for invocation in invocations:
        # Guards need unambiguous -U selectors; fail before any build or device access.
        try:
            guard_boards(
                invocation.resource_identities,
                ubertooth_roles(catalog.get(invocation.test_name).hardware_roles),
            )
        except StateCaseError as error:
            raise HarnessError(f"{invocation.invocation_id}: {error}") from error
    unavailable = sorted({
        invocation.test_name
        for invocation in invocations
        if not catalog.get(invocation.test_name).implemented
    })
    if unavailable:
        raise HarnessError(
            "test implementation not available: " + ", ".join(unavailable)
        )
    sources = _resolve_sources(args)
    if args.with_firmware and not args.build:
        raise HarnessError("--with-firmware requires --build")
    if args.with_python and not args.build:
        raise HarnessError("--with-python requires --build")
    if args.build and not args.libbtbb_source:
        raise HarnessError("--build requires --libbtbb-source and --libbtbb-ref")
    if args.dry_run:
        return _dry_run(args, catalog, plan, host, invocations, not_applicable)
    artifacts = _parse_artifacts(args.artifact)
    run_id = validate_run_id(args.run_id) if args.run_id else generate_run_id()
    leases = board_leases.acquire(
        board_leases.boards_for_invocations(invocations, registry),
        board_leases.holder_info(
            run_id=run_id, owner=args.owner,
            purpose=f"run-suite {plan.name} / {args.suite}",
        ),
        directory=args.lease_dir,
        wait_s=args.wait_for_boards * 60,
        # Power isolation and firmware writes hold this host's lock too.
        host_lock=board_leases.needs_host_lock(invocations, catalog),
        external_template=external_template,
    )
    try:
        return _run_leased(
            args, catalog, plan, registry, invocations, sources, artifacts,
            run_id, leases, not_applicable,
        )
    finally:
        leases.release()


def _dry_run(args, catalog, plan, host, invocations, not_applicable) -> int:
    """What run-suite would run on this host and what is not applicable.

    Capabilities come from the platform and the run options (hostneeds.py
    planned_capabilities); nothing is built, leased or run.
    """
    labels = []
    for value in args.artifact:
        name = value.split("=", 1)[0]
        if "=" not in value or not name:
            raise ManifestError("artifact must use NAME=PATH")
        labels.append(image_label(name))
    capabilities = planned_capabilities(
        args.host, host.platform, build=args.build,
        with_python=bool(args.with_python), with_firmware=args.with_firmware,
        artifact_labels=labels,
    )
    print(f"dry run: {plan.name} / {args.suite} on {args.host}; nothing is built or run")
    print(f"host: {capabilities.describe()}")
    runs = omitted = 0
    for invocation in invocations:
        definition = catalog.get(invocation.test_name)
        cases = definition.command_cases(
            invocation.arguments, invocation.resources, invocation.resource_identities
        )
        runnable, left_out = split_cases(cases, capabilities)
        runs += len(runnable)
        omitted += len(left_out)
        resources = ", ".join(f"{role}={alias}" for role, alias in invocation.resources.items())
        state = (f"not applicable ({len(left_out)} cases)" if left_out and not runnable
                 else f"{len(runnable)} case(s)"
                 + (f", {len(left_out)} not applicable" if left_out else ""))
        print(f"  {invocation.invocation_id}: {invocation.test_name}"
              + (f" [{resources}]" if resources else "") + f": {state}")
        for case in runnable:
            print(f"    run: {case.name}")
        for case, reason in left_out:
            print(f"    not applicable on {args.host}: {case.name}: {reason}")
    for item in not_applicable:
        print(f"  {item.describe()}")
    print(f"total: {len(invocations)} test(s), {runs} case(s) to run, {omitted} case(s) "
          f"and {len(not_applicable)} test(s) not applicable on {args.host}")
    return 0


def _run_leased(
    args, catalog, plan, registry, invocations, sources, artifacts, run_id, leases,
    not_applicable=(),
) -> int:
    """run-suite once every board the run touches is leased to this process."""
    preflight_policy = PreflightPolicy(registry, catalog)
    # Checks that need no build run first, so a long build cannot end in a
    # plan-order or power-isolation error.
    preflight_policy.check_order(invocations)
    isolation = None
    isolated_invocations = [
        i for i in invocations if i.power_isolation or i.exclusive_ubertooth
    ]
    if isolated_invocations:
        # Controllers are chosen per resource from its hardware `driver`, so
        # one run can mix StarTech and Acroname switches. Readiness connects
        # to each controller and scans USB once, without changing any port.
        # An exclusive invocation with no power actions only scans USB, so it
        # needs no --allow-power-changes and builds no controller.
        isolation = IsolationRuntime(
            registry, args.host, allow_changes=args.allow_power_changes
        )
        for invocation in isolated_invocations:
            isolation.check_ready(
                invocation.power_isolation, invocation.resources.get("Ubertooth")
            )
    built = None
    publisher = None
    executor = SubprocessCommandExecutor()
    if args.build or args.libbtbb_source:
        # Records before the sequencer's: the build, and the libbtbb pin check.
        run_dir = create_run_directory(args.output, run_id)
        publisher = open_run_publisher(run_dir, run_id, _terminal_sink())
        publisher.emit(
            "run_started", "running",
            run_started_details(plan.name, args.suite, args.host, sources),
        )
        try:
            _record_libbtbb_pin(publisher, sources)
        except BaseException as error:
            _finish_failed_run(publisher, run_dir, str(error) or type(error).__name__)
            raise
    try:
        manifest, preflight_checks, executor, built = _prepare_run(
            args, plan, invocations, sources, artifacts, run_id, preflight_policy,
            publisher, run_dir if args.build else None, leases,
        )
    except BaseException as error:
        if publisher is not None:
            _finish_failed_run(publisher, run_dir, str(error) or type(error).__name__)
        if isolation is not None:
            isolation.close()
        raise
    if publisher is None:
        run_dir = create_run_directory(args.output, run_id)
    cancellation = CancellationToken()
    previous_handlers = {}

    def request_cancellation(signum, _frame):
        cancellation.cancel(f"received {signal.Signals(signum).name}")

    # SIGHUP too: a dropped SSH session must still restore isolated boards.
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_cancellation)
    try:
        summary = NamedTestSequencer(
            executor,
            catalog=catalog,
            event_sink=_terminal_sink(),
            isolation=isolation,
            registry=registry,
        ).run(
            invocations,
            run_dir,
            run_id=run_id,
            manifest=manifest,
            preflight_checks=preflight_checks,
            cancellation=cancellation,
            publisher=publisher,
            not_applicable=not_applicable,
        )
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if isolation is not None:
            isolation.close()
    try:
        print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    except OSError:
        pass  # terminal gone (e.g. SSH dropped); run.jsonl has the result
    return 0 if summary.passed else 1


def _registered_boards(registry, host: str | None = None):
    """(board, None) for each leasable Ubertooth, or (alias, reason) if not."""
    boards = []
    for resource in sorted(registry.resources.values(), key=lambda r: r.alias):
        if resource.type != "Ubertooth":
            continue
        if host and resource.attached_to != host:
            continue
        try:
            key = board_leases.board_key(resource.alias, resource.identity)
        except board_leases.LeaseError as error:
            boards.append((resource.alias, str(error)))
            continue
        boards.append((board_leases.Board(resource.alias, key), None))
    return boards


def _boards_command(args: argparse.Namespace) -> int:
    external_template = _external_template(args)
    registry = FileResourceRegistry.load(args.hardware)
    hosts = [resource.alias for resource in registry.hosts()]
    if args.host:
        if registry.get(args.host).type != "Host":
            raise HarnessError(f"resource {args.host} is not a Host")
    elif len(hosts) == 1:
        args.host = hosts[0]
    else:
        # Leases are host-local; another host's boards would all look free.
        raise HarnessError(
            "boards needs --host: leases are local, so name the host this runs on "
            f"({', '.join(hosts) or 'none registered'})"
        )
    registered = _registered_boards(registry, args.host)
    states = board_leases.board_states(
        [board for board, problem in registered if problem is None], directory=args.lease_dir,
        external_template=external_template,
    )
    states += [
        {"resource": alias, "key": None, "held": False, "holder": {}, "unleasable": problem}
        for alias, problem in registered if problem is not None
    ]
    # The host lock is local to the host this runs on, like the leases.
    states.append(board_leases.host_lock_state(directory=args.lease_dir))
    if args.json:
        print(json.dumps(states, indent=2, sort_keys=True))
        return 0
    for state in states:
        external = state.get("external") or {}
        if state.get("unleasable"):
            print(f"{state['resource']:<12} not leasable: {state['unleasable']}")
        elif state["held"] and state["holder"]:
            holder = state["holder"]
            since = board_leases.held_for(holder)
            detail = board_leases.describe_holder(holder)
            print(f"{state['resource']:<12} held  {detail}" + (f" (for {since})" if since else ""))
        elif external.get("held"):
            detail = board_leases.describe_external(
                Path(external["lock"]), external.get("holder", "")
            )
            print(f"{state['resource']:<12} held  {detail}")
        elif state["held"]:
            print(f"{state['resource']:<12} held  {board_leases.describe_holder({})}")
        else:
            print(f"{state['resource']:<12} free")
        if external.get("error"):
            print(f"{'':<12} warning: cannot read external lock: {external['error']}")
    return 0


def _lease_command(args: argparse.Namespace) -> int:
    command = list(args.command_line)
    if not command:
        raise HarnessError("lease needs a command after --")
    external_template = _external_template(args)
    registry = FileResourceRegistry.load(args.hardware)
    boards = []
    for alias in args.boards:
        resource = registry.get(alias)
        if resource.type != "Ubertooth":
            raise HarnessError(f"resource {alias} is not an Ubertooth")
        boards.append(board_leases.Board(alias, board_leases.board_key(alias, resource.identity)))
    # A manual firmware write (or DFU read-back) must not overlap another
    # command's power changes or writes on this host.
    host_lock = args.host_lock or Path(command[0]).name == "ubertooth-dfu"
    with board_leases.acquire(
        boards,
        board_leases.holder_info(run_id=None, owner=args.owner, purpose="lease: " + " ".join(command)),
        directory=args.lease_dir,
        wait_s=args.wait_for_boards * 60,
        host_lock=host_lock,
        external_template=external_template,
    ) as held:
        # The command inherits the locks, so the boards stay leased until it
        # exits even if this process is killed first.
        fds = [lease.fd for lease in held.leases]
        for fd in fds:
            os.set_inheritable(fd, True)
        return board_leases.exit_status(subprocess.call(command, pass_fds=fds))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    command_line = []
    if argv[:1] == ["lease"] and "--" in argv:
        # Everything after the first -- is the command to run, untouched.
        split = argv.index("--")
        argv, command_line = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    args.command_line = command_line
    try:
        if args.command == "validate-plan":
            return _validate(args.plan, args.hardware, args.suite)
        # Commands that touch hardware must run in the bootstrapped harness
        # environment, never a system or application interpreter.
        if args.command == "run-suite":
            require_harness_environment()
            return _run_suite(args)
        if args.command == "build":
            require_harness_environment()
            return _build_command(args)
        if args.command == "runner":
            require_harness_environment()
            return _runner(args)
        if args.command == "controller":
            require_harness_environment()
            return _controller(args)
        if args.command == "boards":
            return _boards_command(args)
        if args.command == "lease":
            return _lease_command(args)
        if args.command == "report":
            text = render_runs(args.runs, redact_serials=args.redact_serials, title=args.title)
            if args.output:
                args.output.write_text(text, encoding="utf-8")
            else:
                sys.stdout.write(text)
            return 0
        raise HarnessError(f"unknown command {args.command}")
    except (
        BuildError,
        HarnessError,
        HarnessEnvironmentError,
        ManifestError,
        PlanError,
        PlanningError,
        PreflightError,
        RegistryError,
        IsolationSetupError,
        board_leases.LeaseError,
        ValueError,
        OSError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
