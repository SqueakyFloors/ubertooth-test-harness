# Ubertooth Test Harness — Design

A networked, GUI-driven test orchestrator for the Ubertooth fork. Runs on a lab
server, deploys/builds a chosen git ref on one or more target machines, flashes
Ubertooth firmware, and runs a configurable test plan (short "fast" suite and
long "reliability" suite) against each target — with live per-target progress,
checkoff, and a countdown/ETA in a web GUI.

> Status: local flash reliability, versioned events, immutable named-run manifests,
> mandatory revision/serial preflight, deterministic resource/power-isolation
> compilation, simulated port restoration, grouped and stateful command cases,
> per-run builds, and flash reliability as a plan test are implemented. Each run
> writes one run.jsonl and one report.md. The persistent target runner
> (protocol v2, outbound-only to a loopback controller) is implemented and
> tested against a stub controller. The controller (target registry, run
> dispatch, results store, REST and live WebSocket API; `127.0.0.1` only) is
> implemented and tested against scripted runners and the real runner, and it
> serves the web GUI (targets, run setup, live checklist, history). A
> single-node deploy script installs the controller and a runner as systemd
> user services on one Linux host (Ansible multi-node deploy remains). MAC
> polling soak execution and radio fixtures remain design-only. Multi-node, firmware-flash, and radio behaviour require the lab
> rack; see the
> [core Ubertooth lab plan](https://github.com/SqueakyFloors/ubertooth/blob/main/TEST_LAB.md).

---

## 1. Components

```
   laptop ──Ansible──> lab server ───────────────> targets (U24, macs, ...)
                        │  controller               │  target runner (persistent)
                        │  - web GUI (FastAPI)       │  - build git ref
                        │  - REST + WebSocket        │  - flash Ubertooth (dfu)
                        │  - GitHub ref list         │  - run test plan
                        │  - results store           │  - stream status/heartbeat
                        └── test plans (YAML)        └── local Ubertooth(s)+BT devices
```

- **Controller** (Linux lab server, Python/[FastAPI](https://fastapi.tiangolo.com/)): serves the web GUI, lists
  refs, dispatches runs to target runners, aggregates live status, and stores results.
  A loopback-only controller may also run on macOS for local UI development;
  Linux remains the deployed controller target.
  Implemented (`ubertooth_harness/controller.py`, `python -m ubertooth_harness
  controller --port N`): it binds `127.0.0.1` only, with no host option or
  environment override, and has no authentication until remote auth is
  designed ([Open items](#8-decisions-and-open-items)); browsers and remote
  runners reach it through SSH tunnels (`ssh -L` / `ssh -R`). Before routing,
  every HTTP request and WebSocket handshake must come from a loopback client
  with `Host` `127.0.0.1:PORT` or `localhost:PORT` and an `Origin` that is
  absent or that same origin (runner connections: absent); `Sec-Fetch-Site`
  must be same-origin or none, and `POST` bodies must be JSON. This blocks DNS
  rebinding and cross-site requests from a local browser. REST:
  `/api/refs` (git ls-remote of the Ubertooth repository), `/api/plans`,
  `/api/targets`, `/api/targets/{host}/runs` (start), `/api/runs`,
  `/api/runs/{id}` (status and events), `/api/runs/{id}/report.md`,
  `/api/runs/{id}/cancel`. `/ws/live` fans out target status changes, run
  updates, and (with `?run_id=`) one run's events to browsers, best effort: a
  browser that falls behind is disconnected, never the runner. `/ws/runner`
  speaks protocol v2. Each run is a flat directory
  `<results dir>/<run_id>/` (`run.json`, `run.jsonl`, `report.md`), with the
  results directory outside the repository by default
  (`$XDG_DATA_HOME/ubertooth-harness/controller/runs`). `GET /` returns a
  JSON summary; the GUI is at `/ui/` ([section 4](#4-web-gui-controller)).
- **Target runner** (each macOS/Linux target, Python): registers with the controller, checks out/
  builds the requested ref, flashes firmware, executes the test plan locally,
  streams step-by-step status back.
- **Transport:**
  - **[Ansible](https://docs.ansible.com/)** for one-shot provisioning: install the target runner, deploy code,
    (optionally) flash firmware. Driven from the laptop (→ server) and the
    server (→ targets).
  - **Target runner WebSocket/HTTP** for the live run phase: trigger a plan, stream
    checkoff + heartbeats + samples back to the controller.
- **GitHub:** controller reads refs (branches and tags, via `git ls-remote`
  against the configured repository, default the public fork) to populate the
  ref dropdown; target runners check out the chosen ref. Read-only, no token
  for a public repository. Releases are their tags.

Why both Ansible and an target runner: Ansible is ideal for idempotent deploy/flash
fan-out; a persistent target runner is needed for real-time checkoff and long-running
soak polling that the GUI displays live.

---

## 2. Target runner protocol (controller ↔ target runner)

Minimal, versioned. Transport: one WebSocket per target runner; the target
runner dials out to the controller and opens no listening socket. Until remote
runner authentication is designed ([Open items](#8-decisions-and-open-items)),
the runner refuses any controller URL whose host is not loopback (`127.0.0.1`,
`::1`, or `localhost` resolving only to loopback), with no override. It
connects to the checked address itself, ignores proxies, and follows no HTTP
redirects.

Implemented protocol, version 2 (`ubertooth_harness/protocol.py`; version 2
added the run's `commit`). Every message is one JSON text frame
`{"version": 2, "type": ..., ...fields}`. Each
type has an exact field set; the receiver rejects any other version, unknown
type, missing or extra field, or wrongly typed value, and does not act on it
(the runner answers `protocol_error {reason}`). Inbound messages to the runner
are limited to 64 KiB; the controller accepts runner messages up to 4 MiB,
since one `event` carries one run.jsonl record and the manifest record alone
may reach 1 MiB.

Controller → target runner:

| Type | Fields |
|------|--------|
| `run` | `request_id`, `ref` (git ref name, for the record; no whitespace, no leading `-`), `commit` (the full commit the controller listed for `ref`, which the runner runs; `null` only when the controller could not list refs, and then the runner resolves `ref` in its own Ubertooth checkout), `plan` (name of `<plans dir>/<plan>.yaml`), `suite`, `tests` (exact test names; empty = whole suite), `build` (bool) |
| `cancel` | `run_id`, `reason` |

Target runner → controller:

| Type | Fields |
|------|--------|
| `register` | `host` (registry alias), `hostname`, `os` (system, release, architecture, distribution or macOS version), `python`, `ubertooth_serials` (list, or `null` when enumeration is unavailable), `enumeration_error`, `heartbeat_interval_s`, `active_run_id` |
| `heartbeat` | `active_run_id`, `elapsed_s`, `last_event_sequence`, `last_event_type` |
| `run_accepted` | `request_id`, `run_id` |
| `run_rejected` | `request_id`, `reason` (busy, cannot build: the runner has no libbtbb and `build` is set, unknown plan, cannot start) |
| `event` | `run_id`, `event` (one run.jsonl record, unchanged, with its own `schema_version`) |
| `run_finished` | `request_id`, `run_id`, `status` (from the run's `run_finished` record), `exit_code`, `run_directory` |
| `run_error` | `request_id`, `run_id`, `exit_code`, `reason` (run ended without a `run_finished` record) |
| `cancel_accepted` | `run_id` |
| `cancel_rejected` | `run_id`, `reason` |
| `protocol_error` | `reason` |

Semantics:
- `register` is the first message on every connection. Ubertooth serials come
  from the read-only sysfs enumeration used by power isolation (Linux); no
  device is opened, so registration cannot stop a running board.
- One run at a time. The runner executes `run-suite` as a child process in
  its harness environment, with a runner-assigned run ID and the runner's own
  hardware registry, host, source checkouts, output root, and
  `--allow-power-changes` setting (never set by the controller). It tails the
  child's run.jsonl and forwards each complete record as it is written, in
  sequence order; run.jsonl and report.md remain the source of truth.
- A commit (or ref) missing from the runner's checkout, because it was
  pushed after the checkout was last fetched, is fetched once from the
  checkout's remote within 60 s and resolved again (manifest.py
  `GitRevisionResolver`, for libbtbb too); the fetch never moves the working
  tree, `HEAD` or a local branch, and a failure is a `run_error` naming the
  remote and why.
- Whether a runner can build (it has a libbtbb source and ref) is not in
  `register`: a new field would need a protocol version bump. The runner
  logs it at startup and refuses a `run` with `build` set that it cannot
  honour with `run_rejected`, which the controller returns as 409 and the GUI
  shows, so no run starts only to fail.
- `cancel` sends the child SIGINT, which run-suite maps to its cooperative
  `CancellationToken` (including power-isolation restoration).
- The run continues if the controller disconnects. The runner reconnects with
  bounded exponential backoff (0.5 s to 30 s), registers with
  `active_run_id`, and replays that run's records from the start of
  run.jsonl; delivery is at-least-once and the controller deduplicates by
  `(run_id, sequence)`. A finished run is reported (`run_finished` or
  `run_error`) once, on the first connection after it ends; until then the
  runner is busy.
- Controller side: the first message must be a valid `register` or the
  connection is closed (1008). Later malformed or out-of-place messages are
  counted on the target and ignored; the protocol has no controller → runner error
  message. A target is offline after three heartbeat intervals without a
  valid message; only a message that decodes as the current protocol resets the timer,
  and malformed messages do not. Events are stored once per `(run_id, sequence)`, including across
  controller restarts; a run the controller did not start (for example one
  that outlived a controller restart) is adopted when it is the target's
  `active_run_id`. A stored `running` run of a host that registers without
  naming it as `active_run_id` (the runner restarted, or reports another run)
  becomes `lost` with a reason, is persisted, and is published live; runs
  loaded at controller startup stay `running` until their target registers.
  A registration replaces an existing connection for the same host
  (half-open reconnects);
  the controller logs a warning when the replaced connection was still online.
  The replaced connection is closed (1008 `replaced`), and every `run` or
  `cancel` request still waiting for its answer on it fails at once with 503
  `runner reconnected`: no run is created and no cancel is recorded, a late
  `run_accepted` on the old connection creates no run, and asking again
  sends the request on the new connection. The target's error counters
  (`protocol_errors`, `last_protocol_error`, `last_runner_protocol_error`)
  carry over to the new registration. A message read on the replaced
  connection after the replacement is never acted on or counted: the
  controller logs a warning, closes that connection, and stops reading it. A
  message whose handling had already begun (for example a `run_finished`
  writing its report) still completes on the shared run.
  Any local process can register any host until runner authentication exists.
- Live fan-out to browsers (`/ws/live`) is best effort and never slows a
  runner: each browser has a queue of `LIVE_QUEUE_LIMIT` (1000) updates. A
  browser that falls that far behind (whether it follows one run or none) has
  its backlog dropped and is closed with 1013 `live stream overflowed`; it
  reconnects for a fresh snapshot. Runners and other browsers are unaffected.
- Reports: the controller compiles each run's report.md from its own copy of
  run.jsonl, and writes it before the run's terminal status (finished, error,
  or `lost`) is stored, served, or published, so a client that sees the
  status can fetch the report. A run with no records yet (for example a
  `run_error` before the first event), or whose records cannot be compiled,
  gets a short report that names the status and reason. If report.md cannot
  be written at all, the terminal status is still stored and published, with
  `report` false and the failure in `report_error`. Records that arrive after
  a run ended rebuild its report.md.

Planned, not yet in the protocol: `deploy {ref}` (build host + firmware ahead
of a run; today `run.build` builds inside the run directory), `flash {image,
device}` (flashing currently runs as the `Flash reliability` plan test),
attached BT emitters in `register`, and soak samples (RSS, throughput,
device-still-enumerated) in `heartbeat`. Adding them bumps the protocol
version.

All step/heartbeat events are timestamped and persisted for the results view.
The implemented local contract assigns a path-safe run ID and schema-versioned,
strictly ordered events (`run_started`, operation start/result, and one terminal
`run_finished`) in one append-only, fsynced `run.jsonl` per run. After
`run_started` (and any build commands) comes one `manifest` record, then
`preflight_started`, per-check command/output events, and `preflight_finished`
before any `test_started`. `report.md` is compiled from run.jsonl when the run
ends. Every external process (build, preflight, test, and flash commands alike)
additionally emits `command_started`, bounded incremental `command_output`
chunks tagged `stdout`/`stderr`, and `command_finished`. run.jsonl keeps only
the first and last output records of a command that prints a lot (200 records
or 64 KiB, then 50 records or 16 KiB), with a `command_output_omitted` record
between them; that command's full output goes to `output/<command id>.log` in
the run directory, the only other per-command file. Stdout and stderr are drained concurrently before process
completion, timeout, or cancellation, so a CLI or GUI displays the command as
it happens without deadlocking a noisy child.

A durable fsynced JSONL sink is always present; composite sinks allow the same
event to feed a future live controller/GUI stream without coupling presentation
to execution. Live display delivery is best-effort and cannot interrupt a
firmware write. Event details are JSON-only and bounded. Commands that may carry
credentials MUST add field-aware argv/output redaction before execution support
is enabled; masking only in the GUI is insufficient because run.jsonl and
report.md are persisted.

Cancellation is cooperative. Target runners check it before each state-changing action
and after commands. An in-progress firmware write is allowed to finish and be
verified before cancellation prevents the next write; non-flash long-running
processes are terminated as process groups. A failure already observed remains
a failure even if cancellation arrives concurrently.

### Device identity: pin by serial, not `-U` index

The `ubertooth-util -U<n>` flag selects a device by its **USB enumeration
index**, which reflects bus/port and plug order and is **not stable** across
reboots or re-plugging, and is **not** the serial number. On a multi-Ubertooth
station (e.g. the two-device range test) an index can silently point at the
wrong board after a reboot.

Therefore the target runner MUST identify devices by **serial number**, not index:
- On register, enumerate attached Ubertooths and record each serial (the
  no-`-U` multi-device listing prints one `Serial No (<index>): <serial>`
  line per board; `ubertooth-util -U<n> -s` reads one serial).
- Test steps and the device cards reference devices by **serial**; the target runner
  resolves serial→current `-U` index at run time, re-resolving each session.
- For the range test, the plan names the two devices by serial so "repeater"
  and "sender" roles are deterministic regardless of enumeration order.

The fork's `-U` also accepts a serial string, but libubertooth treats an
all-digit value as an index and matches any other value as the *end* of a
serial (first matching board wins). The flash runner therefore refuses
digit-only selectors and any selector that is the ending of another, and the
registry should hold full 32-hex-digit serials. With `alternate Ubertooths`,
the planner binds every other registered board on the host as an
`alternated Ubertooth N` resource of the flash invocation (requiring a
`hosts: [...]` list, full serials, at least two boards, a single selected board rather than `each`, and
a flash count that divides evenly), so preflight, the firmware-order check,
the manifest, and the report treat each written board as a resource. After
every flash each other board must still report the revision it had when last
verified.

---

## 3. Resource registry and planning

Hosts and attached hardware are one typed resource graph. Every controller or
test computer is registered as a `Host`; controller placement is a deployment
role, not a test property. Stable aliases use human-friendly prefixes such as
`Host_Linux`, `Host_Mac`, `UT_Linux0`, `DK_Linux0`, and `Hub_Linux0`, but code
MUST use the explicit `type` and `attached to` fields rather than parsing names.

Each host may define one preferred resource per hardware type:

```yaml
resources:
  Host_Linux:
    type: Host
    platform: linux
    roles: [controller, test target]
    defaults:
      Ubertooth: UT_Linux0

  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: 05f0...
    power:
      controller: Hub_Linux0
      port: 1
```

The file-backed CLI implementation and future controller implementation share a
`ResourceRegistry` interface. Validation requires unique aliases/identities,
valid host attachments, type-correct defaults, and valid power relationships.
Actual identities live in ignored `hardware.yaml`; [`hardware.example.yaml`](hardware.example.yaml) is
the committed template.

### Power-controller boundary

USB power controllers are typed resources with a stable controller serial,
driver, capabilities, and host attachment. Each controlled resource maps to one
unique `(controller, downstream port)` pair. For [Acroname USBHub3+](https://acroname.com/store/s79-usbhub-3p), only ports
0–7 are switchable and the controller must advertise per-port VBUS switching
and power readback.

The controller protocol reads an immutable port snapshot containing power/data
enable state and VBUS voltage/current. The simulated implementation supports
planning and restoration tests. The optional [BrainStem](https://acroname.com/software/brainstem-development-kit) adapter uses the
verified v2.12 Python boundary: deterministic
`USBHub3p.discoverAndConnect(Spec.USB, serial)`, dependent `system.getSerialNumber()` identity readback, Result error/value
checks for getters, and integer error/readback checks for setters. It imports
BrainStem lazily and real writes are unarmed by default; no dependency is added
until the physical hub and distributable are validated.

The StarTech managed hub adapter speaks the hub's management-controller serial
protocol (CR-terminated ASCII at 9600 baud: `?Q` firmware version, `GP` port
mask, `SP<password><mask>` set). Its identity is the controller's
`/dev/serial/by-id` path; connection verifies the firmware against a validated
allowlist, and every write must match both the `SP` reply mask and a fresh `GP`
readback of the full mask. The serial link is opened exclusively with read and
write timeouts; any I/O error, timeout, or malformed reply closes it so a late
reply cannot be taken as a later answer. Ports 1-3 are switchable. Port 4 holds
the management controller; its power bit reads 0 at power-up while the
controller keeps answering, so it is refused as a precaution rather than
because it is known to gate the controller. Snapshots carry `None` for VBUS
voltage/current and report data as always enabled; disabling data is rejected.
[`uhubctl`](https://github.com/mvp/uhubctl) per-port power switching is not usable on this hub because it does
not gate VBUS. Restoration attempts every snapshotted port, reconnecting and
retrying each once, so one transient I/O error does not leave ports isolated.
The harness cannot measure VBUS on this hub.

Enumeration-verified isolation: `applied_power_isolation` takes an `IsolationVerification` (sysfs
enumerator, selected serial, isolated alias-to-serial map; serials are
normalized and the map frozen). Before switching, it checks that the isolated
aliases match the actions exactly and waits for a stable enumeration (bounded
by the return deadline). That enumeration must include the selected board, no
identified Ubertooth outside the selected and isolated serials, and no more
unidentified devices than isolated boards missing a serial. Only power-off
actions, one per resource, are accepted. So an unknown board, or a wrong
registry serial whose real board reports a serial, is rejected before
switching. Other mismatches, such as a swapped pair of isolated serials or a
mapped board in DFU, are caught by the power-off confirmation instead. After
power-off, the selected board must be the only Ubertooth enumerated, held for a
settle window (default 1 s, configurable, counted within the removal deadline
and required to be shorter than both deadlines). This keeps a board that is
merely re-enumerating from being mistaken for an isolated one. An isolated
board still present, an unknown extra board, or an unidentified device fails
the run. Boards in the DFU bootloader report no serial number, so they appear
as `unidentified:<device>:<product>`. After restoration, every isolated board
enumerated beforehand must reappear; this return check requires presence only,
with no settle window. Error precedence: a switching or restoration failure, or
an interrupt such as Ctrl-C, wins and skips the return check. Otherwise an
isolation or test-body error wins and a return failure is attached to it as a
note. If everything else succeeded, a return failure is raised. Default
deadlines: 5 s removal and 10 s return. Measured on the lab host: a
board leaves USB about 0.1-0.3 s after power-off and re-enumerates about
0.7-0.8 s after power-on (about 1.2 s including restoration).

An exclusive-Ubertooth test compiles one immutable power-off action for every
other registered Ubertooth on the same host. Missing mappings, duplicate ports,
wrong-host controllers, unsupported ports, or missing VBUS capabilities fail
before execution. `run-suite` wires this through `IsolationRuntime`
([`ubertooth_harness/isolation.py`](ubertooth_harness/isolation.py)): it builds one controller per referenced
resource from the registry `driver` field (simulated controllers are allowed
only with an injected test enumerator), takes 32-hex-digit serials from registry
identities, picks the platform enumerator (sysfs on Linux, and only when the
harness is running on Linux; other platforms fail closed), and refuses real
changes unless `--allow-power-changes` is given. Before the run directory is
created, readiness checks the flag and serials, connects to and closes each
controller to read the live power state of the ports it will switch (no
writes; re-read for every invocation),
and scans USB once. The scan must contain the selected serial and no
unregistered identified Ubertooth, and a port that already reads off while its
registered board is not enumerated is refused as leftover isolation from a
killed run. The sequencer
builds each invocation's cases, then wraps them in `applied_power_isolation`
and records `isolation_started`, `isolation_applied`, `isolation_confirmed`
(the selected serial, which confirmation required to be the only one
enumerated; it is not a second scan),
`isolation_restored`, `isolation_returned`, and `isolation_finished`.
`isolation_restored` is recorded whenever switching unwound cleanly, including
on an interrupt. Any exception closes the record with a failed
`isolation_finished`; enumeration, setup, and controller errors become a
"power isolation" case failure, and, for isolated invocations only, any other
`Exception` becomes an "unexpected" case failure so the run still writes
`test_finished`, `run_finished`, and `report.md` (other tests keep the
existing propagate-and-abort behavior). `isolation_applied` records the
snapshotted original state of every switched port for manual recovery. Every controller is restored even if
another fails, and all restore failures are reported together by controller
alias and port. The cancellation token is checked before isolation starts and again after
the baseline wait (after controllers connect and snapshot, just before the
first write), so a cancel in that window raises `IsolationCancelled`, records
`isolation_finished` as canceled, and changes no port. Once boards are off,
the remaining waits (confirmation, return) are bounded by their deadlines.
Controller `close()` errors after restoration are ignored, since they cannot
undo a restore. `run-suite` turns SIGINT, SIGTERM, and SIGHUP into
cancellation. Restore runs in-process, so a killed harness can leave boards
off; [README](README.md#usb-power-controllers) describes manual recovery. An
exclusive invocation with no power actions (no other registered board on the
host) is still checked, read-only ([#27](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/27)). The planner marks every exclusive
invocation `exclusive_ubertooth`, and the manifest records that flag.
`run-suite` builds the isolation runtime for these invocations without
requiring `--allow-power-changes` and, before the run directory exists, takes
the selected serial from the registry and scans USB once. The enumerator
choice, and so the Linux-only fail-closed rule, is unchanged, and no
controller is built or connected. Around the invocation the sequencer records
`isolation_started` (empty `actions` and `controllers`, `read_only: true`),
then waits, bounded by the return deadline, for the enumeration to be exactly
the selected serial and hold for the settle time. Any other identified serial,
any unidentified device, or a missing selected board still present at the
deadline closes the record with a failed `isolation_finished`
and fails the test as a "power isolation" case failure before any case runs.
The cancellation token is checked before every scan of that wait, so a cancel
records `isolation_finished` as canceled. On success the sequencer records
`isolation_confirmed` (the enumerated set) before the cases and a passed
`isolation_finished` after them.
Controllers are closed when the run ends. Active firmware
writes must never overlap isolation changes, so a run that isolates power
(StarTech or Acroname), checks an exclusive Ubertooth, or writes firmware
(`Flash reliability`, `DFU images`) holds the per-host lock
`<lease dir>/host-power.lock` (an `flock` lease, like a board's, recorded in
the manifest) from before preflight until it ends ([#4](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/4)). A second
such command on the host fails at once and names the holder. The host lock is
taken first, then the board leases in serial order, all or nothing and
without blocking while any lock is held, so lock order cannot deadlock (see
README, "The host lock").

The exclusive-topology btle case runs `timeout --foreground -k 2 -s INT`, so
[`timeout`](https://www.gnu.org/software/coreutils/manual/html_node/timeout-invocation.html) and btle stay in the command's own process group (the executor
starts each command in a new session and signals that group on cancellation
and deadline), and `timeout` escalates to SIGKILL if btle ignores SIGINT; exit 124
proves only that btle kept running on the one board.

### 3a. Discovery and preflight

A **target runner** is the ordinary harness process running on a macOS/Linux
test host, not an AI agent. It enumerates local hardware and reports stable
identities and capabilities. Discovery never silently registers or reassigns an
alias. Reconciliation classifies each resource as present, missing, unknown, or
conflicting.

Validation and preparation have distinct phases:

1. Static load: test name exists and every direct argument matches that test's
   catalog contract.
2. Pre-execution compilation: registered hosts/resources/defaults exist and
   bindings satisfy type, identity, count, capability, distinctness, same-host,
   and power-control constraints.
3. Source resolution: a requested branch, tag, or commit resolves once to a
   full immutable commit without changing the source checkout. Dirty or
   untracked source is rejected.
4. Preparation: with `--build`, clone the resolved commits into the run
   directory, build out of tree, and install into a per-run prefix that every
   command then uses first; record the toolchain and SHA-256 identities of the
   built artifacts. Without `--build`, already-installed artifacts are used.
5. Mandatory preflight: verify installed host revision and every selected
   Ubertooth firmware revision, compile identity, and registered serial before
   the first suite test starts.

The `manifest` record is written to run.jsonl before preflight and snapshots requested
refs, resolved commits, plan/registry/artifact hashes, target host, normalized
arguments, and immutable role-to-resource identities. It also records the
interpreter environment: Python version and executable, pip and dependency
versions, OS, and architecture. Hardware commands run only in the
bootstrapped harness virtual environment ([`bootstrap.py`](bootstrap.py)). Branch or tag movement
after resolution cannot change an active run.

Core preflight is generated by the sequencer policy from compiled invocations;
it is not optional YAML. Checks are reusable test-like modules and appear in
the normal event/report stream, but run before `test_started`. They are
deduplicated once per host or physical Ubertooth. A failed or canceled check
prevents all suite execution. Optional rack-specific preflight plans may add
checks but cannot remove the core policy.

The planner records an immutable invocation containing normalized arguments,
target host, and every role-to-resource/identity binding; the GUI displays this
expansion before run.

Selectors are deterministic:

- `default`: the host's configured preferred resource of the required type.
- `each`: one invocation for every eligible resource, sorted by alias.
- explicit alias: exactly that registered resource.

Ambiguous `any` and random selection are not initially supported. If random
selection is later needed, it requires an explicit recorded seed.

### 3b. Named test plan schema

Plans select registered tests; they do not contain commands, expected output,
implementation descriptions, or embedded shell. Every suite item is a mapping
with exactly one human-readable test name and its arguments directly below it:

```yaml
plan: Ubertooth default
suites:
  fast:
    - Host CLI surface:
        hosts: each

    - Ubertooth identity:
        hosts: [Host_Linux, Host_Mac]
        Ubertooth: default

  slow:
    - Flash reliability:
        hosts: [Host_Linux, Host_Mac]
        platforms: [linux, macos]
        Ubertooth: default
        flashes: 100
```

Every test accepts `platforms:` (`linux`, `macos`, no duplicates), and a step
that leaves it out runs on every platform. `hosts:` chooses the registered
hosts a step may run on; `platforms:` narrows those to the ones that can run
it, as a test-wide need (`hostneeds.Needs`), so the planner compiles the step
only for hosts whose platform it lists and records the rest as not applicable.
A step no named host can run fails planning. `Flash reliability`, `Exclusive
Ubertooth topology`, and `MAC polling soak` must state their platforms and
keep the argument required; the flash reliability compatibility view
(`plan.flash_steps`) is built per listed platform.

Host version, firmware version, compile identity, and serial identity are
mandatory generated preflight checks rather than optional suite items. Their
catalog/check implementations remain reusable for direct diagnostics.

There is no `test:`, `args:`, `cmd`, `check`, or underscore-only public test
identifier. Each explicitly imported implementation declares its public name,
description, argument schema/defaults, supported platforms, hardware roles and
constraints, exact commands, timeouts, and result validation. The catalog is
built only from those implementations; YAML never dynamically imports or
executes a name.

The sequencer resolves each exact name, rejects unknown tests/arguments, asks
the planner to expand resource assignments, and invokes tests in YAML order.
One schedulable test may own multiple named command cases. Each case emits
`check_started`, exact command/output events, `check_finished`, and its own raw
log. Read-only groups may continue collecting case failures before the parent
test reports its aggregate result; state-changing groups stop and clean up on
the first failure. CLI and GUI output show the human-readable test name followed
by the exact live command/output stream. Repeated tests receive stable
sequence-based invocation IDs such as `003-BLE capture`.

State-changing cases are ordered transition steps plus mandatory cleanup
([`ubertooth_harness/stateful.py`](ubertooth_harness/stateful.py)). Steps may record an observed setting or
compare it with a value derived from earlier observations. Cleanup always runs
with a fresh cancellation token and hard timeouts; its failure is a case
failure, and restoration that depends on a value never recorded is skipped only
when no mutating step has run. A case whose precondition does not hold (an
unrestorable original) is reported `skipped` before any change. Plans choose a
bounded `coverage` profile (`smoke` or `comprehensive`); case IDs are fixed,
descriptive literals and generation is deterministic.

Transition and composition tests also bind every other registered Ubertooth
on the host as a `guard Ubertooth N` resource (planner `_guarded`, mirroring
`alternated Ubertooth N`). The sequencer records each guard's firmware
revision and API, firmware compile info, channel, PA level, squelch, LEDs, and
CRC with query-only commands before the first case, re-reads them after every
state-changing case and, if read-only cases ran since the last check, once
after the test, and fails and stops the test on any difference (even with
`continue_on_case_failure`). Differences are counted as `failed_guard_checks`,
apart from `failed_cases`, and the reason names the guard to check and
restore by hand; guards are never restored automatically. The CRC read uses
`ubertooth-btle`, whose `ubertooth_check_api` stops firmware older than the
host API, so it is refused (failing the check) unless the same check's `-v`
read showed an API no older than the host tools' (`HOST_TOOLS_API_VERSION`,
the fork's `UBERTOOTH_API_VERSION` 0x0107, since no host tool reports it).
The reads cannot see a leaked `-S`, `-r`, or mode start on a guard idle at
boot defaults; modulation, access address, and re-enumeration are not read.
Guard-only boards get a serial preflight. Guarded runs need every registered
Ubertooth on the host present, idle, and readable, and full serials for every
selector; `run-suite` refuses short serials, and `validate-plan --hardware`
only warns.

### 3c. Dedicated MAC polling workload

The MAC polling reliability plan is intentionally one named soak rather than a
collection of generic BLE checks. Its implementation reproduces a
production-style MAC polling workload that runs on the lab host:

- one persistent `ubertooth-btle -f` process;
- immediate extraction of every `AdvA` MAC, independent of log throttling;
- five in-memory set buckets rotated every two seconds;
- a MAC is found only when present in the intersection of all buckets;
- one-second target-MAC queries;
- ten seconds without any received MAC marks the listener unhealthy;
- empty buckets persisting for fifteen seconds trigger restart handling, with
  the production restart suppression, wait, and timeout-extension intervals.

The plan records all workload timing and failure thresholds directly so a run
manifest explains exactly which workload behavior was reproduced. It binds a
Linux host, one registered Ubertooth, and a deterministic registered BLE
advertiser. The advertiser's expected BLE address comes from its immutable
resource identity rather than free-form plan text. `exclusive Ubertooth: true`
is mandatory: the planner records power-off actions for every other registered
board, and execution must fail if any discovered extra is unknown or cannot be
physically isolated.

Preparation snapshots and powers down extra boards, verifies one expected
no-selector serial remains, then runs the production command exactly as
`ubertooth-btle -f` without `-U`. Cleanup restores and verifies every original
port state even after failure or cancellation. Merely unbinding or
deauthorizing a powered USB device does not satisfy this topology.

The soak passes only after bucket warm-up if the target remains continuously
findable within the configured miss allowance, the capture process stays
healthy, and restart count remains within the plan limit. Reports preserve
query outcomes, bucket rotations, process health, exact commands, resource
identities, and cleanup results.

The harness models the workload's bucket and query semantics directly in the
target runner. It MUST NOT start the original application's own web server or
introduce a network listener; local development remains listener-free until the
networking and authentication design is approved.

### 3d. Firmware flash + rollback (verified on the lab host)

Flashing is state-changing and the one step that can leave a device unusable,
so the harness treats it carefully.

**Known-good version (KGV) = a pinned artifact, not a version string.** Two
different binaries can both report `2020-12-R1` (observed: a local rebuild,
carrying its own builder and build host in the version string, vs. the
authentic upstream `mikeryan@steel` release, which is a published release string and
stays). The KGV must be a specific
`.dfu` file pinned by sha256. Current KGV: the upstream
2020-12-R1 `bluetooth_rxtx.dfu`, sha256 `e3fcb486…bb11`.

**Safe remote-flash protocol (default for unattended remote flashing):**
1. Flash the new image **without `-r`** so the device **stays in DFU** after
   write (never let an unverified image boot unattended).
2. Verify the device still enumerates as `1d50:6003` (bootloader/DFU).
3. Then either reset into the new image, or — if anything looks wrong —
   immediately reflash the KGV from the same DFU state.

**MANDATORY: verify the flashed firmware version before running any test.**
After every flash, the target runner MUST read `ubertooth-util -v` (and/or `-V`) and
confirm it matches the intended build (git hash / release) BEFORE executing any
test step. Do not run tests against unverified firmware. This session proved the
failure mode: flashes can silently not take (stale firmware keeps running — see
the DFU-entry bug, now fixed in fork [#29](https://github.com/SqueakyFloors/ubertooth/issues/29)), and tests run against stale firmware
produce invalid, misleading results. The harness treats a post-flash version
mismatch as a hard error and aborts the run for that target. The same applies to
every manual test in fork [TEST_PLAN](https://github.com/SqueakyFloors/ubertooth/blob/main/TEST_PLAN.md) / [TEST_LAB](https://github.com/SqueakyFloors/ubertooth/blob/main/TEST_LAB.md): confirm firmware version first.

**Long-suite flash reliability acceptance:** macOS and Linux are independent
required targets, not one combined sample. Each platform MUST complete exactly
100 sequential firmware writes. Alternate the test image and pinned KGV so the
expected revision changes on every write; reflashing an already-installed image
does not prove that a write occurred. After every reset, verify the exact
intended revision and abort immediately on a command failure, missing device, or
version mismatch.

A platform passes only with 100/100 verified flashes, zero manual recoveries,
zero USB power cycles, and zero changes to an unselected device. Preserve each
flash's duration, transition-stage output, expected/actual revision, and exit
status. A controllable hub may recover hardware for diagnosis after a failure,
but that run remains failed. Flash devices sequentially by serial; simultaneous
multi-device flashing is a separate stress test and does not satisfy this gate.

**Verified cycle (2026-10-20):** flashed our `test/lab-bringup` build
(`-v` → `git-2b806aa`, `-V` UTC stamp), rolled back to KGV (`-v` → `2020-12-R1`,
device healthy), and ran a deliberate **bad-app** test (first 8 KB zeroed): wrote
it without reset, confirmed still in DFU, recovered to KGV. Device healthy
throughout.

**Residual risk (needs on-site test):** we did NOT let the device **reset into a
dead app** to see whether the bootloader auto-re-enters DFU. If it does not, a
bad image that is allowed to boot would require a **physical DFU trigger** (boot
pins / reset-hold) that cannot be done remotely. Characterize this at the rack
before relying on reset-into-new-image for unattended runs.

**Version flags (how the harness identifies builds):**
- `ubertooth-util -H` / `--host-version` — host tools (libubertooth) version +
  git hash; no device needed.
- `ubertooth-util -v` — firmware revision (from device).
- `ubertooth-util -V` — firmware compile info (from device).

---

## 4. Web GUI (controller)

Implemented: `/ui/` on the controller, vanilla JavaScript in
`ubertooth_harness/web/` (no build step, nothing from another origin), driven
by the REST API and `/ws/live`.

- **Targets view:** one card per target — host, hostname, OS/version, Python,
  Ubertooth serial(s), online/offline, last seen, active run. Attached
  emitters are not shown yet (no emitter registry).
- **Run setup:** git-ref dropdown (default branch, branches, tags from
  `GET /api/refs`, which runs `git ls-remote` against the configured
  repository and caches for 60 s) + plan and suite selectors
  (`GET /api/plans`) + one target + build toggle → "Run". The controller
  refuses a ref that is not in the list or a full commit ID (422). The run
  request carries the commit listed for the ref (the GUI sends the commit it
  showed; a ref that has moved since is refused with 409 and the page lists
  the refs again), and the runner runs exactly that commit, recording the
  ref name and the commit in the manifest and report. A branch and a tag of
  one name are separate entries, labelled, each with its own commit. When the list cannot be fetched it passes the ref
  on unchecked and logs a warning. One target per run; a multiselect is a later step.
- **Live run:** the suite rendered as a checklist that ticks in real time
  from the run's events (✓ passed / ✗ failed / … running / ○ pending, always
  with text, in an `aria-live` region), the stage, elapsed time, Cancel, and a
  bounded log tail (last 200 lines). The **countdown** is the sum of the
  tests' estimates minus elapsed time, shown only when every test has one. The
  catalog holds an estimate for every implemented test and coverage profile
  (fixed seconds plus a multiple of the duration argument, calibrated from
  run records; a test without an implementation is estimated at its
  `duration` argument), and build/preflight are excluded; a suite with any
  unestimated test shows elapsed time and says so. Estimates are per
  invocation; once the run's manifest record arrives, the checklist is its
  planned invocations on the target (`invocations` in `GET /api/runs/{id}`),
  so `Ubertooth: each` counts every selected board.
- **History:** every run with ref, plan/suite, target, status,
  started/finished, a `report.md` link, and the failure summary from
  `GET /api/runs/{id}` (per failed phase or test: reason and last output
  lines; then the run's reason). Soak graphs remain.

Security: the GUI is served by the loopback-only controller behind the same
Host/Origin/Sec-Fetch guard as the API, `POST`s stay JSON-only, every
response carries a strict CSP (`default-src 'self'`, no inline script,
`connect-src 'self' ws://<host>`, `frame-ancestors 'none'`) plus `nosniff`,
`X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`, controller and
runner strings are rendered with `textContent` only, and static files are
served from memory by exact name.

---

## 5. Deploy flow (Ansible)

Now (single node): [`scripts/deploy.sh`](scripts/deploy.sh), run on the lab
host as the service user from its clone, bootstraps `.venv` and installs the
controller and one runner as systemd **user** units
(`ubertooth-harness-controller.service`, `ubertooth-harness-runner.service`;
the runner is ordered after the controller, both `Restart=on-failure`). The
controller binds `127.0.0.1`; the runner dials it over loopback. It is
idempotent (a unit and its service change only when the content, which records
the checkout commit, changes), never runs sudo, and prints the one-time
`sudo loginctl enable-linger <user>` when linger is off, since user services
otherwise stop at logout. By default it also gives the runner a libbtbb for
Build runs: a clone of the libbtbb fork under the harness data directory
(cloned once, fetched on later deploys) at the `LIBBTBB_REF` pin in the
Ubertooth checkout's `origin/main` `install.sh` (else the harness's
`LIBBTBB_PIN`); `--no-libbtbb` opts out. `scripts/install-desktop-shortcut.sh` adds a GUI
launcher for the desktop user, and `scripts/harness-open` opens a
loopback-to-loopback SSH tunnel (`-J`, or `--two-hop` through an
owner-only Unix socket on the jump host, never a TCP port there) and the
browser from a laptop. See README "Deploy on the lab host". Linux only; launchd is not done.

Later (multi-node), Ansible:

- **Laptop → lab server:** install controller (FastAPI app, service unit),
  Python deps, config. `ansible-playbook deploy-controller.yml`.
- **Server → targets:** install target runner + deps + build prerequisites + ARM
  toolchain, register target runner with controller. `deploy-target-runners.yml`.
- Idempotent; re-runnable to update code on any node.

---

## 6. Build order

1. **This design doc** (here).
2. **Test-plan schema + local target runner** — flash slice, durable versioned
   events, run identity, real-time command/output streaming, cancellation,
   named catalog/resource planning, immutable source/artifact manifests,
   mandatory revision/serial preflight, initial grouped identity commands, and
   simulated fail-closed USB power-isolation planning/restoration implemented;
   automated build/install, armed physical power control, and generic
   command/soak execution remain.
3. **Target runner service** — register/heartbeat/run over the wire; test against a
   stub controller. Implemented (protocol v2, loopback controllers only;
   v2 added the listed commit to `run` and does not interoperate with v1).
4. **Controller + web GUI** — targets, ref dropdown, live checkoff, countdown.
   Implemented: controller (registry, dispatch, results store, REST and live
   WebSocket API, `127.0.0.1` only) and the GUI (targets, run setup, live
   checklist with a countdown when estimates exist, history with failure
   summaries) and catalog duration estimates. Multi-target runs remain.
5. **Deploy** — single node implemented: `scripts/deploy.sh` (systemd user
   units for controller + runner on one Linux host), desktop launcher, and
   `scripts/harness-open` for laptop access over SSH. Ansible
   (laptop→server, server→targets), launchd, and the firmware-flash step
   remain.
6. **eero beacon emitter step** — once its control interface is known.
7. **Harden** — long-soak sampling, history/graphs, failure diagnostics.

Single-node first (one U24 running controller + target runner + one Ubertooth), then
add nodes once the end-to-end flow works.

---

## 7. What can be built/tested here vs. needs the rack

- **Here (mocks/fakes):** schema, target runner runner logic, controller/API, GUI,
  event streaming, Ansible syntax, ETA math, soak-sample parsing.
- **Rack only:** real multi-node transport, firmware flashing, and all radio
  behaviour (BLE/BR capture, RSSI, AFH) validated against real traffic.

### Two test tiers (don't duplicate coverage)
Deterministic, pure-C firmware logic is covered by **host unit tests**
(`make -C firmware test`, run in CI) — e.g. the RSSI rounding math (fork [#15](https://github.com/SqueakyFloors/ubertooth/issues/15)/[#16](https://github.com/SqueakyFloors/ubertooth/issues/16)).
Those don't need the rack and run in seconds. The hardware harness should test
the **end-to-end / signal-dependent** behaviour that unit tests can't: that a
tool runs on real hardware and that its *output is plausible against real RF*
(e.g. the `specan-rssi-sane` fast-suite step checks live RSSI is in a sane dBm
range with a negative mean). Rule of thumb: if a check can be made deterministic
without a radio, put it in `make test`; the harness verifies it holds on real
hardware/traffic.

---

## 8. Decisions and open items

Resolved:
- **Target runner connection direction:** target runner **dials the controller** (outbound),
  so targets on DHCP/behind NAT work and the live status stream stays open.
- **Local development exposure:** controller/GUI binds to `127.0.0.1`; remote
  development uses an authenticated SSH tunnel.
- **Single-node lab deploy:** systemd user services of a non-root service user
  (`scripts/deploy.sh`), still `127.0.0.1` only; people use the lab host's own
  desktop browser, a loopback-to-loopback SSH tunnel (`scripts/harness-open`),
  or screen sharing. Linger is enabled once by an administrator, never by the
  script.
- **Results store:** flat JSON files per run, **plus a printed summary of
  failures to act on** (per failed step: target, git ref, step id, reason, and
  a log snippet) emitted at run end and shown in the GUI history.
- **eero beacon trigger:** the eero emitter step asks the staged eero, over
  its control network, to start advertising its BLE setup beacon, then sniffs
  it with the Ubertooth. This replaces physically resetting the eero into
  setup mode. The device-side interface, endpoint and credentials are kept out
  of this repository and supplied by the lab configuration.

Open:
- **Remote networking and auth:** before binding beyond loopback, select and
  implement approved HTTPS browser authentication, target runner authentication, and
  action-level authorization. Fail closed until configured. A self-managed
  shared token is not an approved final design. Define reconnect, delivery
  acknowledgement, bounded offline spooling, and cancellation semantics in the
  same networking specification.
- **Firmware flashing (rack checklist, not code):** flashing via the target runner
  (`ubertooth-dfu` on the target) depends on the device re-enumerating
  `6002`->`6003`->`6002` unattended. This is historically flaky (upstream
  [#538](https://github.com/greatscottgadgets/ubertooth/issues/538)/[#71](https://github.com/greatscottgadgets/ubertooth/pull/71)). **Confirm per node that an unattended reflash works** before the
  harness relies on reflashing between runs.
- **Additional emitters / Ubertooth features to exercise:** TBD together
  (AFH map, BR/EDR paging, spectrum, etc.) as hardware is added.
- **eero control-plane access:** obtain the staged eero's beacon-trigger
  endpoint and credentials so the emitter step can be implemented for real.
