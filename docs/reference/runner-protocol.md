# Target runner protocol (controller <-> runner), version 7

Wire reference. Architecture: [DESIGN.md](../../DESIGN.md).

Minimal, versioned. Transport: one WebSocket per target runner; the target
runner dials out to the controller and opens no listening socket. Until remote
runner authentication is designed ([Open items](../../DESIGN.md#open-items)),
the runner refuses any controller URL whose host is not loopback (`127.0.0.1`,
`::1`, or `localhost` resolving only to loopback), with no override. It
connects to the checked address itself, ignores proxies, and follows no HTTP
redirects.

Implemented protocol, version 7 (`ubertooth_harness/protocol.py`; version 2
added the run's `commit`, version 3 its `flash_build`, version 4 the
`run_file` request and its answers, version 5 the run's `with_firmware` and
`with_python`, version 6 its `bootloader`, and version 7 its `with_btbr`).
Every message is one
JSON text frame `{"version": 7, "type": ..., ...fields}`. Each
type has an exact field set; the receiver rejects any other version, unknown
type, missing or extra field, wrongly typed value, or combination of fields
it could not act on (`with_firmware`, `with_btbr` or `with_python` without
`build`), and does not act on it
(the runner answers `protocol_error {reason}`). Inbound messages to the runner
are limited to 64 KiB; the controller accepts runner messages up to 4 MiB,
since one `event` carries one run.jsonl record and the manifest record alone
may reach 1 MiB.

Controller → target runner:

| Type | Fields |
|------|--------|
| `run` | `request_id`, `ref` (git ref name, for the record; no whitespace, no leading `-`), `commit` (the full commit the controller listed for `ref`, which the runner runs; `null` only when the controller could not list refs, and then the runner resolves `ref` in its own Ubertooth checkout), `plan` (name of `<plans dir>/<plan>.yaml`), `suite`, `tests` (exact test names; empty = whole suite), `build` (bool), `flash_build` (bool: with `build`, flash the run's boards with that build's firmware before preflight; false passes `--no-flash-build`; ignored without `build`, and the controller sends and records it false then), `with_firmware`, `with_btbr` and `with_python` (bool: the run's build also builds and records the application firmware image, the alternate btbr image, and the Python tools, passing `--with-firmware`, `--with-btbr` and `--with-python`; `run-suite` takes none of them without `--build`, so any one true without `build` is rejected as a message the receiver could not act on. The btbr image is its own field because a firmware build makes the application image without it, so a test that needs the alternate one has to say so directly), `bootloader` (`record`, `check` or `update`: what the run does about its boards' bootloaders, passed as `--bootloader`; `check` and `update` compare against a bootloader built from the run's own source, which `run-suite` refuses without `--build`, so either one without `build` is rejected the same way) |
| `cancel` | `run_id`, `reason` |
| `run_file` | `request_id`, `run_id`, `path` (a path that run's own run.jsonl records), `offset` and `limit` in bytes (`limit` at most 256 KiB, since the page travels in one message) |

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
| `run_file_page` | `request_id`, `run_id`, `page`: what the run recorded about the file (`path`, `stream`, `bytes`, `lines`, `sha256`) and where this page sits in it (`file_bytes`, `offset`, `length`, `eof`, `page_bytes`, `text`) |
| `run_file_refused` | `request_id`, `run_id`, `status` (400, 404 or 503, which the controller's route answers with), `reason` (names the run ID or the path only) |
| `protocol_error` | `reason` |

Semantics:
- `register` is the first message on every connection. Ubertooth serials come
  from a read-only enumeration: the sysfs scan power isolation uses on Linux,
  and `ioreg -p IOUSB -l -w0 -a` on macOS. Neither opens a device, so
  registration cannot stop a running board. Power isolation itself stays
  Linux-only, since it is the switchable hubs that are validated there.
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
- The runner's `--libbtbb-ref` goes to every run as given. The deployed
  default, `fork-pin`, is resolved per run from the Ubertooth commit the run
  was asked for (its own `install.sh` `LIBBTBB_REF`), so each commit is built
  against the libbtbb it asks for and a pin bump on the fork needs no
  redeploy. The resolved commit is what the manifest records, named
  `fork-pin`. Every run resolves it, build or not, since the libbtbb a run
  records is checked against the tools' version line; a commit with no pin,
  or one that names something other than a commit, fails the run with that
  reason.
- Whether a runner can build (it has a libbtbb source and ref) is not in
  `register`: a new field would need a protocol version bump. The runner
  logs it at startup and refuses a `run` with `build` set that it cannot
  honour with `run_rejected`, which the controller returns as 409 and the GUI
  shows, so no run starts only to fail.
- `cancel` sends the child SIGINT, which run-suite maps to its cooperative
  `CancellationToken` (including power-isolation restoration).
- `run_file` is how the GUI opens a saved output file of a run a runner
  carried out: those files stay in the runner's own run directory and only
  run.jsonl reaches the controller. The runner serves a page under the
  confinement the controller's own route uses (`runfiles`, over
  `outputfiles.confined`): only a path that run's own run.jsonl records, only
  inside the run directory, no symbolic link followed, not even for the run
  directory itself, and the page bounded. Every request is answered, with a
  page or a refusal, and a refusal names the run ID or the path and never a
  path on the target. The runner answers four at once and refuses further
  ones as busy (503); it reads off the event loop and in its own task, so
  reading a page of a soak's log does not hold up the next message on the
  connection, which may be a cancel. The controller asks only for
  a file that run's own run.jsonl records and this host does not hold, holds
  at most eight requests per target, answers 503 while the runner is not
  connected, and 504 if it does not answer within the reply timeout.
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
between them; that command's streams go to `output/<command id>.stdout.log`
and `.stderr.log` in the run directory as they arrive, the only other
per-command files. Stdout and stderr are drained concurrently before process
completion, timeout, or cancellation, so a CLI or GUI displays the command as
it happens without deadlocking a noisy child.

Output is checked from those files, not from memory. The in-memory copy of
each stream in `CommandResult` is bounded (`executor.MEMORY_WINDOW_CHARS`),
which is what keeps a mode that streams for hours from growing the harness;
a check reads the file a block at a time instead
(`outputfiles.CommandOutput`). A command whose output stayed inside the
run.jsonl limits has no file and the same checks read its complete in-memory
copy, so a check behaves the same either way.

Checks are recorded, so they can be run again without hardware. Each command
gets one `output_checks` record naming the files its checks read and, per
check, its kind, its pattern, whether it matched, and what it matched; a case
gets an `output_files` record for the captures and target serial logs it
wrote, each with its size and SHA-256. `ubertooth-harness validate <run dir>`
(`validate.py`) measures every recorded file again and re-runs every recorded
pattern over the same bytes, reporting a file that changed, a check that now
reaches a different verdict, and the checks it cannot repeat. A check written
as code (`Step.output_check`) is one of those: its record holds its verdict,
not its logic, which is the price of letting a case express a count or a
shape that no pattern can.

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

