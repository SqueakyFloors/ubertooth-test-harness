# Ubertooth test harness — architecture

This is the design of the harness: what it is for, how it is put together, and
which rules every part has to keep. It is meant to be read end to end in one
sitting. The detail each component needs to be worked on lives in the
[reference documents](#reference-documents) at the end; this file does not
repeat them.

## 1. Purpose

Qualify one fixed commit of [the Ubertooth
fork](https://github.com/SqueakyFloors/ubertooth), built against the exact
libbtbb commit that commit pins, on real hardware.

Goals, in the order they decide arguments:

1. **A result says what it measured.** Every run records the commits, the
   built artifacts, the host, the boards and their identities, the commands,
   and the output the checks read.
2. **No run can be fooled by stale state.** Firmware, host tools, and the
   libbtbb the tools load are read back before the first test.
3. **Nothing is left for somebody to find.** Device settings, USB power, and
   firmware are restored on success, failure, and cancellation.
4. **The bench is shared.** Several runs and by-hand commands coexist through
   leases rather than by convention.
5. **A plan is portable.** One plan compiles against any bench's registry, and
   hardware a bench has not got is recorded rather than guessed around.

## 2. Boundaries

- The harness tests the fork; it does not contain product code. Host tools and
  firmware come from the resolved commits, built per run.
- Deterministic C logic belongs in the fork's own unit tests. The harness
  measures what needs a radio, a USB bus, or a real tool invocation
  ([section 11](#11-two-test-tiers)).
- The only listening socket is the controller, on `127.0.0.1`. Runners dial
  out; people reach the GUI through an SSH tunnel. There is no authentication
  yet, so there is no remote bind ([Open items](#open-items)).
- This repository is public. Bench identities (host names, serials, ports,
  paths, credentials) stay in the ignored local registry and in private lab
  configuration.

## 3. Components

```
   laptop ──SSH tunnel──> lab host                       test hosts
                          controller                     target runner
                          - web GUI (FastAPI)            - builds a commit
                          - REST + /ws/live              - flashes firmware
                          - run dispatch + store         - runs the suite
                          - git ls-remote ref list       - streams records
                          127.0.0.1 only            <────  dials out (ws)
```

- **Controller** (`ubertooth_harness/controller.py`): the registry of targets,
  run dispatch, the results store, the REST and live-WebSocket API, and the
  web GUI. Loopback only, with no host option and no override.
- **Target runner** (`ubertooth_harness/runner_service.py`): the ordinary
  harness process on a macOS or Linux test host. It registers with the
  controller, runs one suite at a time as a `run-suite` child, and forwards
  that run's records as they are written. It opens no listening socket and
  refuses a controller URL that is not loopback.
- **`run-suite`** (`ubertooth_harness/cli.py` and the modules under it): the
  whole of a run. Everything the GUI shows is derived from what a run writes,
  so the CLI is complete on its own and the services are optional.
- **Plans** (`testplans/*.yaml`) name tests from the built-in catalog
  (`ubertooth_harness/catalog.py`). They carry no commands.
- **Registry** (`hardware.yaml`, ignored; template in
  [`hardware.example.yaml`](hardware.example.yaml)): the typed resource graph
  of one bench.

## 4. One run, end to end

```
resolve refs ──> lease boards ──> build ──> flash ──> preflight ──> tests ──> restore
    │               │               │         │          │            │         │
 immutable      host lock +     per-run    read -v    identity     cases +   cleanup
 commits        board leases    prefix     back       readback     checks    verified
```

1. **Resolve.** The requested branch, tag, or commit resolves once to a full
   commit without touching the source checkout; dirty or untracked source is
   refused. libbtbb resolves the same way, `fork-pin` meaning the
   `LIBBTBB_REF` that Ubertooth commit sets in its own `install.sh`.
2. **Compile.** The plan's suite is compiled against this registry into
   immutable invocations: normalized arguments, the host, and every
   role-to-resource binding with its identity.
3. **Lock.** The host lock first, then the boards' leases in serial order, all
   or nothing, so lock order cannot deadlock
   ([leases](docs/guide/leases.md)).
4. **Build.** With `--build`, the resolved commits are cloned into the run
   directory, built out of tree, and installed into a per-run prefix that
   every later command finds first. Toolchain and artifact SHA-256 identities
   are recorded.
5. **Flash.** Unless `--no-flash-build`, every board whose firmware the run
   requires is written with the build's image, one board at a time, and `-v`
   is read back until it reports the build's revision. The first failure stops
   the run before preflight.
6. **Preflight.** Mandatory, generated from the compiled invocations rather
   than from YAML: installed host revision, the libbtbb those tools are
   actually linked against, each selected board's firmware revision, compile
   identity, registered serial, and the bootloader under that firmware. A
   failed or canceled check prevents every test.
7. **Tests.** The sequencer runs each invocation's cases. A case owns its
   commands, its checks, and its cleanup; cleanup runs with its own hard
   timeouts even after cancellation or an internal error, and the parent test
   only continues once cleanup has verified the device restored.
8. **Finish.** One terminal record, then `report.md` compiled from
   `run.jsonl`. Boards, ports, and leases are released whatever happened.

## 5. Resources and the bench

Hosts and attached hardware are one typed graph. Every computer is a `Host`;
being the controller is a deployment role, not a test property. Aliases such
as `UT_Linux0` are for people. Code uses the explicit `type` and
`attached to` fields and the stable identity (a serial, a probe path).

- A host may name one preferred resource per type (`defaults:`), so repeated
  tests stay on one device.
- Selection is deterministic: `default`, `each`, or an explicit alias.
  Ambiguous `any` and random selection are refused; a seeded random policy
  would have to record its seed.
- A resource may be recorded as permanently wired to another (a
  `Debug programmer` `programs` one board). The pair is leased together,
  whichever member a command names.
- A board may record its **RF placement** (`rf:`: antenna, attenuation,
  distance). Nothing measures it; it describes the bench. A test asks for a
  placement per hardware role with `rf:`, or with the `weak link:` shorthand,
  and a figure is read against the attenuation it was taken at.
- **USB power controllers** are typed resources too, with one unique
  `(controller, port)` pair per powered board. Real power and data changes
  are refused unless the run is armed with `--allow-power-changes`, and
  isolation is confirmed from USB enumeration rather than trusted from the
  hub.

What a bench has not got is a first-class outcome rather than an error:

| Outcome | When | Effect |
|---|---|---|
| not applicable | a platform the step does not list, a board placed differently from what the test needs, a host or device this registry has not got | the step is left out and the reason recorded |
| blocked | hardware the step is *about* is absent: `alternate Ubertooths` with one board, `exclusive Ubertooth` with an unmapped board, or a step that says `required hardware: true` | recorded before anything runs; every other step still runs and the run finishes failed |
| planning error | a mistake in the plan or the registry: an unknown alias, a wrong type, an impossible placement, a `power:` mapping that is wrong | nothing runs |

A gate must not pass unmeasured, which is why "blocked" exists: a release plan
can name one bench's peer hardware honestly, and a bench without it fails
rather than quietly skipping.

## 6. Plans and the catalog

A plan selects named tests and gives them arguments. It contains no commands,
no expected output, and no shell:

```yaml
plan: Ubertooth default
suites:
  fast:
    - Host CLI surface:
        hosts: each
    - Ubertooth identity:
        hosts: [Host_Linux, Host_Mac]
        Ubertooth: default
```

The catalog owns each test's description, accepted and default arguments,
hardware roles, exact argv, timeouts, coverage profiles, time estimates, and
result checks. Unknown names, unknown arguments, and invalid values fail when
the plan loads, with no registry involved. Validation is layered: static load,
then compilation against the registry, then source resolution, then
preparation, then preflight.

Three arguments every test takes are worth knowing: `platforms:` (which of its
`hosts:` can run it), `max_watchdog_resets:` (makes the firmware's self-reset
count a measure of that test), and `coverage:` where a test has profiles.

## 7. Execution and safety invariants

These are the rules that must hold for a result to mean anything.

- **Identity before measurement.** No test runs against firmware, tools, or a
  libbtbb that has not been read back. A post-flash version mismatch is a hard
  failure, never a retry: a write that silently did not take would otherwise
  produce confident nonsense.
- **One run at a time per host, and sequential hardware.** Boards are operated
  one at a time by serial unless a test is explicitly about concurrency.
- **Hard deadlines.** Every command has a timeout; so does a run. Each command
  starts in its own session so a deadline or a cancellation signals the whole
  process group.
- **Cancellation is cooperative and never leaves a half-written board.** It is
  checked before each state-changing action and after each command; an
  in-progress firmware write finishes and is verified before the next one is
  prevented. A failure already observed stays a failure even if cancellation
  arrives at the same moment.
- **Restoration is part of the test.** Device settings, hub ports, and
  firmware are restored and read back. A restoration failure is reported with
  the controller and port named. Restoration runs in-process, so a killed
  harness (SIGKILL, OOM, power loss) can leave a port off or a board in DFU,
  and that is put right by hand before the next run; the next run refuses to
  start rather than paper over it.
- **A recovery never converts a failure into a pass.** A power cycle may
  gather diagnostics after a failed reliability run; the run stays failed.
- **Transmit is opt-in.** A test whose commands can put the radio on the air
  carries `allow RF transmission: true`, and a destructive test additionally
  needs `--allow-destructive`, which the runner service never passes on, so no
  dispatched run or GUI click can start one.
- **Secrets never reach a record.** `run.jsonl` and `report.md` are persisted,
  so any command that could carry a credential needs field-aware redaction
  before execution, not masking in the GUI.

## 8. Persistence

One flat directory per run, outside the repository by default.

- `run.jsonl`: append-only, fsynced, schema-versioned, strictly ordered
  records — `run_started`, the build commands, one `manifest`, preflight, each
  test's cases and steps with their commands and bounded output, and exactly
  one terminal record. It is the source of truth.
- `output/`: each noisy command's full stdout and stderr, plus the captures
  and device logs a case wrote, each with its size and SHA-256. Checks read
  these files a block at a time, so a mode that streams for hours does not
  grow the harness.
- `report.md`: compiled from `run.jsonl` when the run ends, and rebuilt if
  later records arrive.
- The `manifest` record snapshots requested refs, resolved commits, plan and
  registry and artifact hashes, the host, normalized arguments, role-to-resource
  identities, RF placements and what each role required, and the interpreter
  environment. Branch movement after resolution cannot change a live run.
- Checks are recorded, not just their verdicts: `ubertooth-harness validate
  <run dir>` measures every recorded file again and re-runs every recorded
  pattern over the same bytes. A check written as code records its verdict
  only, which is the price of counting things no pattern can.

Details: [run records](docs/reference/run-records.md).

## 9. Services and the GUI

The controller stores runs, fans out live updates, and serves the GUI at
`/ui/`: targets, run setup (ref, plan, suite, the hosts to run on, build and
flash options), a live checklist with one pill per host on every row and a
countdown where estimates exist, history
with failure summaries, and the output files a run saved. A run on several
hosts at once is a **run group**, carried on one stream, and a run of one
host is a group of one, so the GUI has one thing to draw.

Two properties matter for the design:

- **Live display never slows a run.** A browser that falls behind is
  disconnected; the runner never is. Delivery to the GUI is best effort and
  cannot interrupt a firmware write.
- **A run outlives its viewers.** The runner keeps going if the controller
  disconnects, reconnects with backoff, and replays that run's records;
  delivery is at-least-once and the controller deduplicates by
  `(run_id, sequence)`.

Estimates are the catalog's, improved by what a host has actually taken:
durations are learned per host for tests, builds, preflight, and flashes.

Details: [controller, runner, and GUI](docs/guide/services.md),
[runner protocol](docs/reference/runner-protocol.md).

## 10. Deployment

[`scripts/deploy.sh`](scripts/deploy.sh), run on the lab host as the service
user from its clone, bootstraps `.venv` and installs the controller and one
runner as systemd **user** units. It is idempotent, never runs sudo, and
prints the one-time `loginctl enable-linger` an administrator applies. The
same script deploys a Mac test host, where there is no controller: two launchd
LaunchAgents, the runner and the loopback-to-loopback SSH tunnel it dials
through. `scripts/harness-open` opens that tunnel and the browser from a
laptop. Multi-node Ansible fan-out remains.

Details: [deploy](docs/guide/deploy.md).

## 11. Two test tiers

Do not duplicate coverage. Deterministic, pure-C firmware logic belongs in the
fork's host unit tests, which run in seconds without hardware. The harness
measures the end-to-end and signal-dependent behavior those cannot: that a
tool runs on real hardware, against known traffic from fixtures the harness
owns, and that its output is plausible against real RF. If a check can be made
deterministic without a radio, it belongs in the fork.

The Ubertooth is a receiver in most tests, so known traffic comes from
registered fixtures (nRF52840 DKs for LE, ESP32 peers for Classic). It is not
receive-only as a platform: the `Two-device range test` transmits from both
boards, and the firmware's range-test, repeater, and TX modes exist, which is
also why a test that could start one carries an explicit transmit guard.

## 12. Extension points

- **A new test**: a catalog entry plus its case builder, a section in
  [docs/tests.md](docs/tests.md), and an entry in
  `ubertooth_harness/test_info.yaml`. Plans gain a name, not a command.
- **A new device type**: a typed resource with a stable identity, one driver
  module, and one firmware image that holds every role, so no test reflashes a
  radio to change what it does.
- **A new power controller**: the controller contract (snapshot power, data,
  VBUS voltage and current; armed writes; read back every write), chosen from
  the registry `driver` field.
- **A new protocol message**: a version bump; no version interoperates with
  another.

## Open items

Resolved decisions worth keeping: the runner dials out, so targets behind NAT
work; the controller is loopback only and reached over SSH; results are flat
files per run plus a printed summary of failures to act on.

Open:

- **Remote networking and authentication.** Before binding beyond loopback,
  select and implement approved HTTPS browser authentication, runner
  authentication, and action-level authorization, failing closed until
  configured, and define reconnect, delivery acknowledgement, bounded offline
  spooling, and cancellation in the same specification. A self-managed shared
  token is not an approved final design, and any local process can register
  any host until this exists.
- **Unattended reflash per node.** Flashing depends on the board
  re-enumerating `6002`→`6003`→`6002` without hands; confirm it per node
  before relying on reflashing between runs.
- **Soak execution.** The one modeled workload
  ([soak workload](docs/reference/soak-workload.md)) is designed and
  compiled, not implemented. No soak is a release gate.
- **Multi-node provisioning** (Ansible) and the remaining radio fixtures.

## Reference documents

| Document | What is in it |
|---|---|
| [README](README.md) | entry point: quick start, environment, running a suite |
| [docs/tests.md](docs/tests.md) | every catalog test: method, pass criteria, what a pass does not prove |
| [docs/release-testing.md](docs/release-testing.md) | the release test plan: cases, schedule, exit criteria |
| [docs/hardware.md](docs/hardware.md) | the bench: devices, firmware, protocols, setup, limits |
| [docs/hardware-utilization.md](docs/hardware-utilization.md) | which fixture each test uses, and the conflicts between them |
| [docs/guide/plans-and-resources.md](docs/guide/plans-and-resources.md) | plan schema, selectors, absence outcomes, RF placement |
| [docs/guide/builds-and-firmware.md](docs/guide/builds-and-firmware.md) | builds, libbtbb, flashing, bootloaders, DFU images |
| [docs/guide/leases.md](docs/guide/leases.md) | leases, the host lock, external locks, device pairs |
| [docs/guide/transitions-guards-watchdog.md](docs/guide/transitions-guards-watchdog.md) | coverage profiles, guard boards, the firmware watchdog |
| [docs/guide/test-devices.md](docs/guide/test-devices.md) | how the harness drives the DKs, the ESP32 peers, and a host adapter |
| [docs/guide/services.md](docs/guide/services.md) | controller, runner service, web GUI, run groups |
| [docs/guide/deploy.md](docs/guide/deploy.md) | deploying the services on the lab host and a Mac |
| [docs/reference/runner-protocol.md](docs/reference/runner-protocol.md) | the controller↔runner wire protocol, version 7 |
| [docs/reference/run-records.md](docs/reference/run-records.md) | `run.jsonl`, `report.md`, output files, re-checking a run |
| [docs/reference/power-control.md](docs/reference/power-control.md) | power drivers, armed changes, isolation, manual recovery |
| [docs/reference/soak-workload.md](docs/reference/soak-workload.md) | the modeled MAC polling workload |
| [COVERAGE.md](COVERAGE.md) | every host CLI option and which test covers it |
