# Ubertooth Test Harness

Private standalone test-harness application for
[`SqueakyFloors/ubertooth`](https://github.com/SqueakyFloors/ubertooth) and its
companion [libbtbb](https://github.com/SqueakyFloors/libbtbb) builds. The application is described in
[DESIGN.md](DESIGN.md). The first implemented slice is a local, sequential
firmware-flash reliability runner. The persistent [target runner](#target-runner-service)
dials a loopback controller; the harness opens no listening socket.

## Environment

The harness runs only in its own Python 3.12 virtual environment, never in a
system, user, or application (for example GLaDOS) interpreter. Create or update
it with one idempotent command, on macOS or Linux:

```sh
python3 bootstrap.py          # creates or updates .venv
. .venv/bin/activate          # or call .venv/bin/python explicitly
```

[`bootstrap.py`](bootstrap.py) uses only the standard library. It finds Python 3.12 (or use
`--python PATH`), fails with install instructions if 3.12 or its `venv`
support is missing (Ubuntu: `python3.12-venv`), creates `.venv` with a
harness marker file, and installs the pinned [`requirements.txt`](requirements.txt) with the new
environment's own interpreter, pip's virtualenv guard on, and the user site
disabled; then it runs `pip check`. Rerunning reuses the environment. It
refuses to modify an existing directory that lacks the marker, and
`--recreate` rebuilds only a marked harness environment. `--venv PATH` selects
another location.

`run-suite` and `build` refuse to start outside a bootstrapped harness
environment. Every run manifest (schema version 4)
records the Python version, implementation, executable and prefix, pip and
dependency versions, OS and release, and CPU architecture under
`environment`. Commands below assume the environment is active.

## Validate plans

```sh
python -m ubertooth_harness validate-plan testplans/slow.yaml
```

## Named tests and registered resources

Plans contain human-readable test names with direct arguments—never commands or
expected output:

```yaml
fast:
  - Host CLI surface:
      hosts: each

  - Ubertooth identity:
      hosts: [Host_Linux, Host_Mac]
      Ubertooth: default
```

The built-in catalog owns descriptions, accepted/default arguments, hardware
roles, exact argv, timeouts, and result checks. Every plan item must contain
exactly one known test name; unknown names, legacy `cmd/check` entries, unknown
arguments, and invalid values fail during load.

`hardware.yaml` is the ignored local resource registry; copy
[`hardware.example.yaml`](hardware.example.yaml) to start. Register every macOS/Linux computer as a
`Host`, then attach typed resources such as `UT_Linux0`, `DK_Linux0`, and
`Hub_Linux0`. Aliases are for humans—the explicit type, host attachment, and
stable serial/probe identity are authoritative. A host's `defaults` selects its
preferred Ubertooth so repeated reliability tests stay on one device.

Selection is deterministic: `default`, `each`, or an explicit resource alias.
Ambiguous `any` and random selection are rejected. Tests with multiple hardware
roles require explicit aliases; a future explicit `all pairs` policy will be
separate. Target-runner discovery is
reconciled as present/missing/unknown before execution and never silently
reassigns an alias.

### USB power controllers

The registry models the ordered [Acroname USBHub3+](https://acroname.com/store/s79-usbhub-3p) as a typed `USB power
controller` with a 32-bit serial identity, explicit driver, capabilities, and a
unique downstream port for each powered resource. The generic controller
contract snapshots power, data, VBUS voltage, and VBUS current. A simulated
driver supports development and restoration tests without hardware.

The [BrainStem](https://acroname.com/software/brainstem-development-kit) adapter is lazy and optional: no package dependency is installed
until the arriving hub and exact distributable version are validated. It
connects to one configured controller serial, reads the serial back, checks
every API error and write readback, and refuses all real power/data changes
unless armed (`run-suite --allow-power-changes`). Ports are restored
to their original state after success or failure.

The StarTech managed hub driver (`startech managed USB hub`) is validated on
the Linux lab host. It talks to the hub's built-in serial management controller
by its stable `/dev/serial/by-id` path, refuses unvalidated controller
firmware, checks every write against the controller's port-mask readback (the
full port mask), and is also unarmed by default. It opens the controller
exclusively, and any I/O error, timeout, or malformed reply closes the link.
Only ports 1-3 are switchable; port 4 carries the management controller and is
refused as a precaution. Only the factory-default password is supported. The
hub cannot measure VBUS or switch data separately, so snapshots report
voltage/current as unknown, and a port reading "off" is not proof of power-off.
It needs [`pyserial`](https://pyserial.readthedocs.io/) (pinned in [`requirements.txt`](requirements.txt)), imported lazily. Standard
hub power control ([`uhubctl`](https://github.com/mvp/uhubctl)) only changes the reported state on this hub and
does not cut power, so it must not be used.

Isolation is confirmed from USB enumeration instead (read-only sysfs scan in
[`ubertooth_harness/enumeration.py`](ubertooth_harness/enumeration.py)): the selected board must become the only
Ubertooth enumerated, and isolated boards that were enumerated beforehand must
come back after restoration. See [DESIGN.md, "Power-controller boundary"](DESIGN.md#power-controller-boundary), for
the exact rules and error precedence.

Plans requiring `exclusive Ubertooth: true` compile deterministic power-off
actions for every other registered Ubertooth on the host. Compilation fails if
an extra board lacks a valid controller mapping or per-port VBUS capability.
`run-suite` applies those actions only with `--allow-power-changes`; without
it, the run fails before anything executes. A host with only the selected
board registered switches nothing and needs no flag (see the read-only check
below). Each controller is built from its registry `driver` (`startech
managed USB hub` or Acroname BrainStem), so one run can mix switch types.
Only the StarTech path has been validated on hardware; the Acroname path is unit-tested with simulated controllers until its
hardware validation ([#8](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/8)). The simulated driver is for unit tests only and is
refused unless a test injects its own enumerator. Serials come from the
registry identity (32 hex digits; placeholders are rejected). Enumeration
verification is Linux-only; other platforms fail closed. Before the run
directory is created, the harness checks the flag and serials, connects to and
closes each controller to read the ports it will switch (no port is changed),
and scans USB once: the selected board must be enumerated, no unregistered
Ubertooth may be attached, and a registered board whose port is already off and
that is not enumerated is refused, since a killed earlier run may have left it
off. Each isolated invocation emits `isolation_started`, `isolation_applied`
(with the original state of every switched port), `isolation_confirmed`,
`isolation_restored`, `isolation_returned`, and `isolation_finished` events.
Any isolation failure fails that test and, like any test failure, stops the
run. A restoration failure names every controller and port that could not be
restored. SIGINT, SIGTERM, and SIGHUP (for example, a dropped SSH session)
cancel the run and restore every port. A cancel that arrives before the other
boards are switched (including during the pre-switch enumeration wait) skips
the test without changing power; once boards are off, cancellation restores
them, waiting at most about 15 s for removal and return. An exclusive test on a
host with no other registered board compiles no power actions and gets a
read-only enumeration check instead ([#27](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/27)): no controller is built or
connected and `--allow-power-changes` is not needed. Readiness is a single
USB scan before the run directory is created, with no settle tolerance: it
must show exactly the selected serial. Then, before the test's cases run, the
harness waits (up to the return deadline) for exactly the selected serial to
hold for the settle time; that tolerance applies to this pre-case wait only,
so a board that briefly re-enumerates or drops out does not fail the test. The
test fails before any case runs if, at the deadline, the enumeration still
shows any of these: an extra board (registered on another host or not
registered at all), an unidentified device such as a board in DFU, or no
selected board. The refusal names what was enumerated and how to clear it. A
cancel during this wait is seen at the next poll and the test is recorded as
canceled. The check records `isolation_started` (no actions,
`read_only: true`), `isolation_confirmed`, and `isolation_finished`, and the
run manifest marks the invocation `exclusive_ubertooth`. Like the rest of
enumeration it is Linux-only and fails closed elsewhere.

Restoration runs in-process, so if the harness itself is killed (SIGKILL, OOM,
host power loss) while boards are isolated, power the ports in the last
`isolation_applied` event back to their recorded original state by hand before
the next run (an exclusive run that would switch that port refuses to start
while the board is still off). StarTech: send `GP` to read the 8-hex-digit mask (least-significant byte
first, bit 0 = port 1), OR in the bits for those ports, and send `SP` + the
password padded to 8 characters + the new mask, each CR-terminated. For
example, with the factory password `pass`, if `GP` returns `09000000` (ports 1
and 4 set) and port 3 must come back, send `SPpass    0D000000`. Acroname:
enable the port's power through BrainStem. Exclusive host/hub ownership across concurrent harness runs is not
yet enforced ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)).

[`testplans/exclusive.yaml`](testplans/exclusive.yaml) runs the `Exclusive Ubertooth topology` test, which
checks with no `-U` selector that the selected board answers `ubertooth-util
-s`/`-v` and that `ubertooth-btle -f` keeps running until its deadline (this
proves the tool opened the only board, not that it captured packets). The
capture is capped at 10 minutes, since every other board is off meanwhile:

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/exclusive.yaml --suite exclusive \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --output runs --allow-power-changes
```

Validate syntax/catalog only:

```sh
python -m ubertooth_harness validate-plan testplans/fast.yaml
```

Compile against registered resources before execution:

```sh
python -m ubertooth_harness validate-plan testplans/fast.yaml \
  --hardware hardware.yaml --suite fast
```

Run currently implemented catalog-owned tests for one local host:

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/fast.yaml --suite fast \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref feature/my-change \
  --test "Ubertooth identity" --output runs
```

The generic sequencer executes `Host version`, `Help smoke`, grouped `Host CLI
surface`, and grouped `Ubertooth identity` command cases. The host surface runs
help/parser checks for all eleven always-built executables and retains each
tool's result; Linux-only BlueZ tools will be added with platform-aware catalog
support. The identity test runs isolated firmware, compile, serial, part, and
board queries plus the compatible `-vVspb` composition. `Debug register read`
constructs a serial-selected `ubertooth-debug` command from the normalized plan
register. Each case retains its own command, output, log, and result while the
parent YAML item reports one aggregate outcome. Remaining named tests are
catalog-validated and resource-planned but gain their hardcoded implementations
in subsequent slices.

## Option compositions and stateful transitions

Stateful host CLIs need more than isolated option checks. Plans must select a
`coverage` profile for each transition test; the Python implementations own
every command, expected state, and cleanup step. [`testplans/fast.yaml`](testplans/fast.yaml) runs
`smoke`; [`testplans/release.yaml`](testplans/release.yaml) runs `comprehensive` plus the BLE mode
conflicts, before release preparation:

```sh
python -m ubertooth_harness validate-plan testplans/release.yaml \
  --hardware hardware.yaml --suite release
python -m ubertooth_harness run-suite \
  --plan testplans/release.yaml --suite release \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main --output runs
```

| Test | `smoke` (fast plan) | `comprehensive` (release plan) |
|------|---------------------|--------------------------------|
| `Ubertooth util compositions` | `-v -V` and every read-only report in one command | adds every other compatible read-only pair and repeated `-U` (last wins) |
| `Radio setting transitions` | channel, PA level, squelch across an idle stop and an active-mode stop | adds USR LED; repeated `-c`, `-C`, `-a`, `-z`, `-l`, `-d` (last wins); mixed `-c`/`-C` in both orders; rejected `-a9` with a later `-v` must still exit nonzero; full reset `-r` returns boot defaults |
| `BLE CRC transitions` | CRC enabled during `-n` and `-f` on channel 37 | both CRC values x both modes x channels 37/38/39; repeated `-v` (last wins) |
| `BLE mode conflicts` | no `coverage` argument; release plan | every combination of `ubertooth-btle -f -n -p` must be rejected before sniffing |
| `Ubertooth util conflicts` | no `coverage` argument; requires `allow RF transmission: true`; in no plan yet | every pair of `-q -n -e -t -x -I` must be rejected before a mode starts |

Every test case is built before the manifest is written or any command runs,
so an implementation error fails the run before it touches hardware. Every
state-changing case first stops the device so originals are read from
idle (if the board was left in an active mode, that stop has already reset
channel, squelch, and LEDs, so those post-stop values are the originals),
records every readable setting it or its modes can change, and always runs
cleanup: return to idle, restore each recorded original, verify each
restoration, and confirm the registered serial.

The setting cases check the firmware stop contract. A stop from idle changes
nothing. Stopping an active mode (the cases use `-x`) resets channel to
2441 MHz, squelch to -120, and LEDs to off; PA level is kept. CRC cases preset
the opposite value first, so a mode that loses its CRC setting cannot pass by
matching a leftover value; the CRC re-read after the mode is the check.

Not checked or restored, because no option the cases use can read it: the
modulation (the firmware stop returns it to basic rate), the BR/EDR target
address and LE advertising channel (cleared by a mode stop), and the BLE
access address (cleared by `-r`).

LEDs are recorded and restored as a USR/RX/TX triple. `-l` sets USR only and
`-d` sets all three, so a state with RX different from TX can be read but not
written back. Any case whose original for a setting it may change cannot be
written back (RX different from TX, or PA level 0) is reported `skipped`
before anything changes; if a value turns out unrestorable after a change,
cleanup fails. Failure reasons name the output pattern that did not match. A continuous mode that
reaches its deadline reports `command_finished` status `stopped`.

`ubertooth-util` cannot set PA level 0 (`-a0` is a query). On hardware a
powered-on or reset board reports PA level 7 (checked on current main and the
2020-12-R1 firmware), and the full-reset case checks that boot default. A board
reads 0 only if something set the front end that way. Cases that can change
PA record it (the PA setting cases, the full-reset case, and the conflict pairs
containing range test `-n` or repeater `-e`); one that reads 0 is reported
`skipped` before anything changes, and cleanup reports PA restoration
as skipped rather than failed. `test_finished` reports `failed_cases`,
`failed_guard_checks` (see [Guard Ubertooths](#guard-ubertooths)), and `skipped_cases`; the run summary (`run_finished` in run.jsonl, and
report.md) reports the run's `skipped_cases`, so a
skip is never hidden by an overall pass. Event schema version 1 is pre-release
and has no stable consumers yet; field changes before the controller lands do
not bump it.

A state-changing case stops at its first failed step. The parent test moves on
to the next case only when that case's cleanup verified the device restored;
otherwise the test stops. Cleanup runs with its own hard-timeout commands even
after cancellation or an internal error such as a failed event write, during
the transition or during cleanup itself (remaining restores run without
events, then the error is raised), a cleanup failure fails the case, and a passing cleanup
never clears an earlier failure. A case is reported `canceled` only when
cancellation was its sole failure. Restore steps that depend on a value never
recorded are reported as skipped when nothing was changed and as failed
otherwise. Every step that runs, is skipped, or is canceled before it starts
emits a `step_finished` event with its reason, expected and observed values,
and argv (`null` for a step that did not run). Steps after the first failure or
skip emit nothing. Aggregate reasons are bounded; each
case's full reason is in its own `check_finished` event.

`Ubertooth util conflicts` fails against the fork's `ubertooth-util`, which
runs whichever mode it checks first
([SqueakyFloors/ubertooth#82](https://github.com/SqueakyFloors/ubertooth/issues/82)).
It stays in the catalog and joins [`testplans/release.yaml`](testplans/release.yaml) once that is fixed.
The full-reset case can fail on macOS when re-enumeration takes longer than
the fixed 2 s wait inside `ubertooth-util -r`
([SqueakyFloors/ubertooth#94](https://github.com/SqueakyFloors/ubertooth/issues/94)).
Repeated `ubertooth-btle` options (capture files are first-wins) are tracked in
issue [#14](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/14).
`ubertooth-util -i` and `-f` are excluded because an accepted conflict could
leave the board outside the application firmware. Against current firmware,
pairs that start range test, repeater, or TX test transmit on 2.4 GHz until
cleanup's stop, so the test requires `allow RF transmission: true`; PA level
and channel are recorded and restored. Pairs with `-I` never exit on their own
(identify handles only SIGINT) and run to the 5 s timeout before cleanup.

### Guard Ubertooths

Every test in the [table above](#option-compositions-and-stateful-transitions)
binds each other registered Ubertooth on the host as a `guard Ubertooth 1`,
`2`, ... resource of the same invocation, so the
[Results table, transcript](#run-output-runjsonl-and-reportmd), and manifest
name the guards. The design is in
[DESIGN.md, "Named test plan schema"](DESIGN.md#3b-named-test-plan-schema). A host with one
registered Ubertooth has no guards. Preflight checks each guard-only board's
serial (`ubertooth-util -U <serial> -s`) and that its firmware API (`-v`) is
no older than the host tools' API; a guard below it is refused before any
test. Its firmware revision is not required to match the resolved one. Guards do not count toward the
firmware-order check (a firmware-writing test must be the last test on its
board), so a guarded test may follow a flash on another board.

A guarded test therefore needs every registered Ubertooth on its host to be
attached, idle, and readable, each registered with its full 32-hex-digit
serial (libubertooth matches `-U` against the end of a serial). `run-suite`
refuses shorter serials for the selected host before any build or device
access. Planning stays permissive: `validate-plan --hardware` prints a
`warning:` line for each such invocation but still exits 0.

Before the first case the test records a baseline for each guard with
read-only queries only: `ubertooth-util -U <serial>` with `-v` (firmware
revision and API), `-V` (firmware compile info, whose build timestamp also
catches a reflash of the same revision), `-C` (channel), `-a` (PA level), `-z`
(squelch), and `-d` (LEDs), then `ubertooth-btle -U <serial> -v` (CRC).
`ubertooth-util` never checks the firmware API, but `ubertooth-btle` does, and
it sends a stop to firmware whose API is older than the host tools'
(`ubertooth_check_api` in the fork's
[`host/libubertooth/src/ubertooth.c`](https://github.com/SqueakyFloors/ubertooth/blob/main/host/libubertooth/src/ubertooth.c)).
So the CRC read runs only when the same check's `-v` read showed an API no
older than the host tools' (no host tool reports it, so the harness reads
`UBERTOOTH_API_VERSION` from the resolved Ubertooth source and refuses guarded
tests if it cannot). Otherwise the read is refused without running and the
baseline fails: the guard firmware API is too old to read CRC safely. The harness
never sets, stops, or resets a guard. One stop it cannot prevent: the fork's
`ubertooth_connect_serial` sends a stop when it cannot claim the board's USB
interface, so a guard held open by another process may receive one from any
read.

The same reads run after every state-changing case (`Guard Ubertooths
unchanged after <case>`) and, if read-only cases ran since the last check,
once after the test; each value must equal the guard's own baseline. Any
difference fails the test and stops it, even in a test that otherwise
continues past failed cases. Every read runs even after the first difference,
so the record shows each one. Without a baseline (a read failed) no case runs.
Guard checks after a case run with their own hard timeouts even after
cancellation, as cleanup does. Each guard read is a `step_finished` event with
stage `guard` and its expected and observed values, and each command is
recorded like any other. A guard that differs is never restored: the failure
reason names it (role, alias, and serial) so the operator can check and
restore it by hand. `test_finished` counts failed guard checks in
`failed_guard_checks`, not in `failed_cases`; the Results table shows them
beside the case count (for example `0 + 1 guard check`).

The check sees only what it reads. It catches a leaked firmware change or
reflash and any change to channel, PA level, squelch, LEDs, or CRC. On a guard
idle at boot defaults it cannot see a leaked `-S` or `-r`, or a mode start
(`-x`, `-t`, range test, repeater, `ubertooth-btle -n`/`-f`) that leaves those
values unchanged, so a guard could be left transmitting without the test
failing. Modulation, access address, and USB re-enumeration are not read.

## Building the resolved revision

By default a run tests the Ubertooth tools already installed, and preflight
refuses to start when they do not match the requested commit. With `--build`
the run builds the exact commits it tests instead:

```sh
python -m ubertooth_harness run-suite --plan testplans/fast.yaml --suite fast \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref feature/my-change \
  --libbtbb-source /path/to/libbtbb --libbtbb-ref master \
  --build [--with-firmware] --output runs
```

The resolved commits are cloned into `<run>/build/src` (the source checkouts
are never modified), built out of tree, and installed into `<run>/build/prefix`.
Every test and preflight command then runs with that prefix first on `PATH`, so
the run tests what it built. Host tools use `-DREQUIRE_ALL=ON` on Linux (BlueZ
exists only there), with Python tools and udev rules off. `--with-firmware`
also builds `bluetooth_rxtx.dfu` with the built `ubertooth-dfu`; the image is
hashed into the manifest, not flashed. The built `ubertooth-util -H` must
report both resolved revisions, without a modified-source `*`.

The manifest records the toolchain (CMake, C compiler, pkg-config, libusb,
BlueZ on Linux, and the ARM compiler when building firmware), the CMake flags,
every build command with its exit code, and hashes of every built tool,
library, and firmware image. Every host tool the catalog runs must be in the
prefix, so a missing one cannot fall through to an installed copy. The build
commands are recorded in run.jsonl and appear under `=== BUILD ===` in the
report. A missing tool or library stops
the run first, naming what to install. Ctrl-C cancels the build, and the
whole build has a one-hour deadline on top of per-command timeouts.

`python -m ubertooth_harness build` does the same build without running tests.

Install the build prerequisites and udev rules once per machine, for example
with the fork's [`install.sh`](https://github.com/SqueakyFloors/ubertooth/blob/main/install.sh). The harness never runs it or installs anything
system-wide.

## Source identity, manifest, and mandatory preflight

Every named-suite run requires `--ubertooth-source` and `--ubertooth-ref`. The
ref may name a branch, tag, or commit; the CLI resolves it without switching the
checkout and records the full immutable commit plus the repository and short
build revision. Dirty or untracked source content is rejected. Supply
`--libbtbb-source` and `--libbtbb-ref` together when validating an exact
companion build. Repeat `--artifact NAME=PATH` to hash build products into the
manifest.

Before preflight, the `manifest` record in run.jsonl captures the source revisions,
SHA-256 identities of the plan, hardware registry, supplied artifacts, and
immutable resource-bound invocations. The sequencer then generates mandatory
preflight from the selected resources; it does not depend on optional test-plan
entries:

- `Host version` once per selected host, matched to the resolved source commit;
- firmware version, compile identity, and registered serial once per selected
  Ubertooth, always selected by serial.

Checks are deduplicated by host/resource and emit normal check/command events.
Any mismatch prevents the first `test_started` event. This local slice verifies
artifacts that are already installed. Building and installing the resolved ref
on local/remote targets remains a later preparation phase; preflight cannot be
skipped to compensate for a missing installation.

## Sharing boards: leases

Several agents can share one host's Ubertooths when they run as the same user
account (leases live in that user's home; separate accounts do not exclude
each other, see [#12](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/12)). `run-suite` leases
every board it touches before preflight and holds the leases until the run
ends: the selected board, guard and alternated boards, power-isolated boards,
and, for `Flash reliability` and exclusive tests, every Ubertooth on the host
(they read or enumerate all of them). A lease is an exclusive `flock` on
`~/.ubertooth-harness/leases/<serial>.lock` (override with
`UBERTOOTH_HARNESS_LEASE_DIR`); the kernel drops it when the process exits, so
leases never go stale. Boards are taken all or nothing, so two runs cannot
deadlock. A leased board needs its full 32-hex-digit serial in the hardware
registry: libubertooth matches `-U` against the end of a serial, so a short
serial would select the same board under a different lock.

If a board is held, the run fails before any build or device command and names
the holder. `--wait-for-boards MINUTES` waits instead; `--owner LABEL` (or
`UBERTOOTH_HARNESS_OWNER`) names you in other agents' errors. The run report
lists the leases.

```sh
ubertooth-harness boards --hardware hardware.yaml --host Host_Mac  # free / held by whom
ubertooth-harness lease --hardware hardware.yaml --owner "agent #14" UT_Mac0 -- \
    ubertooth-util -U <serial> -v                          # manual use, leased
```

`lease` holds the named boards for exactly as long as the command runs; the
command inherits the locks, so they stay held even if the harness process is
killed first. Use it for every manual Ubertooth command on a shared host.

## GLaDOS workload soak

[`testplans/glados.yaml`](testplans/glados.yaml) contains one dedicated `GLaDOS MAC polling soak` item.
It reproduces the production workload shape traced from GLaDOS: a persistent
`ubertooth-btle -f` capture, five in-memory MAC buckets rotated every two
seconds, one-second lookup cadence, ten-second listener-silence detection, and
the existing empty-bucket/restart timing. The plan selects a registered Linux
host, its preferred Ubertooth, and a deterministic nRF52840 DK advertiser whose
BLE address is recorded in the resource registry. It also requires
`exclusive Ubertooth: true`: every other registered board on that host must be
physically power-controlled, leaving exactly one enumerated device before the
implementation runs the production command verbatim as `ubertooth-btle -f`
without `-U`.

The committed plan currently defines and resource-compiles this future soak;
its manifest includes every required power-off action, but its executable
implementation remains pending. Once implemented, `run-suite` [power isolation](#usb-power-controllers)
(above) will power down extras (or, with only one registered board, check
read-only that no other Ubertooth is attached), verify exactly one expected
no-selector serial, run the soak, and restore/read back all original states on
success, failure, or cancellation. Unknown or unpowerable extra Ubertooths
fail closed. The harness will model bucket rotation and MAC queries directly
in-process; it will not start GLaDOS or add a network listener.

Validate its catalog and resource bindings with:

```sh
python -m ubertooth_harness validate-plan testplans/glados.yaml \
  --hardware hardware.yaml --suite glados
```

## Run output: run.jsonl and report.md

A test run is one `run-suite` run of a plan suite. It writes exactly two files
into `<output>/<run-id>/` (plus `build/` with `--build`):

- `run.jsonl`: every record, appended and fsynced as it happens. The first
  records are `run_started` and `manifest` (sources, plan and registry hashes,
  artifacts, invocations, Python/OS environment, and build toolchain), then
  build commands, preflight, and each test's events, including every command
  and its output as it arrives. `run_finished` holds the summary.
- `report.md`: compiled from `run.jsonl` when the run ends, including failed or
  canceled runs. It lists the setup (commits, host and platform, toolchain,
  Python, device serials and firmware, artifact hashes) and a results table,
  then the full transcript.

Each transcript line starts with the time elapsed since the run started
(`HH:MM:SS.mmm`); the report header gives the run's UTC start time, and
run.jsonl keeps absolute UTC times (`2026-10-24T00:18:21.535`, no offset
suffix). Each line then carries a tag: `[C]` command sent (shell-quoted), `[O]`
stdout, `[E]` stderr, `[R]` command result (tool, exit code, duration, timeout
or cancellation), `[S]` state-change step, `[I]` power isolation, `[M]` harness
record. Section markers are indented to line up with the tagged text:
`+++ name +++` opens a step and `--- name EXIT: status ---` closes it; `===`
lines open and close the run, build, preflight, and each test:

```text
00:00:10.438     === PREFLIGHT ===
00:00:10.439     +++ Host version +++
00:00:10.439 [C] ubertooth-util -H
00:00:10.465 [R] ubertooth-util exit 0 in 0.025s; [O] libubertooth 1.1 (git-be97109), libbtbb 1.0 (git-f0fe176)
00:00:10.465     --- Host version EXIT: passed ---
00:00:10.466     === PREFLIGHT EXIT: passed ===
00:00:10.466     === TEST 002-Host CLI surface: Host CLI surface on Host_Mac ===
...
00:00:12.785     === RUN passed ===
```

A command that printed one line is two lines, with the response on the `[R]`
line. Longer output stamps each `[O]`/`[E]` line when it arrived, in arrival
order, before the `[R]` line. Very long output is capped in the report; the
rest stays in run.jsonl. The live terminal stream on stderr uses the same
`[C]`/`[O]`/`[E]`/`[R]` command tags.

Run IDs look like `20261022T203045Z-a1b2c3d4e5f6`; `--run-id` supplies a
path-safe ID. Records use event schema version 1 (pre-release; no stable
consumers yet) and carry the run ID, UTC timestamp, sequence, status, and
bounded JSON details. The same records feed the live terminal stream on stderr
(stdout holds only the final summary JSON) and a future GUI; a live display
failure never interrupts hardware work.

SIGINT/SIGTERM (and SIGHUP) request cooperative cancellation. A firmware write
in progress is allowed to finish and be verified before the next one is
stopped; other commands are terminated as a process group.

Commands carry no credentials today. Before adding one that does, redact argv
and output where events are created; masking only in a display is not enough
because run.jsonl and report.md are persisted.

### Reports for several runs

```sh
python -m ubertooth_harness report runs/<mac-run-id> runs/<linux-run-id> \
  --title "Ubertooth 2026-11-R1 validation" --output validation.md
```

`report` writes one page summarizing each run's setup and results, for example
macOS and Linux runs for a release; each run's own report.md keeps its full
transcript. Serial numbers are shown; `--redact-serials` replaces them with
`<serial>`.

## Flash reliability

`Flash reliability` is an ordinary plan test (see [`testplans/slow.yaml`](testplans/slow.yaml)):

```yaml
- Flash reliability:
    hosts: each
    platforms: [macos, linux]
    Ubertooth: default
    flashes: 100
    image sequence: [test build, KGV]
    KGV sha256: e3fcb486c6e0ce7cc991bf6439f6dbc0156ed1229c2b95ebfd02198f9675bb11
    KGV revision: 2020-12-R1
```

Supply the images as artifacts named after the plan's image labels, for example
`--artifact "test build=/path/to/bluetooth_rxtx.dfu" --artifact "KGV=/path/to/kgv.dfu"`
with `--test "Flash reliability"`. With `--build --with-firmware` the freshly
built `bluetooth_rxtx.dfu` is the test build. The test build must report the
resolved Ubertooth revision; the KGV must match the plan's sha256 and revision.

The test:

- checks every image's SHA-256 (against the manifest and the pinned KGV) before
  the run starts and again before each write;
- alternates the images so every write changes the expected revision;
- flashes with `ubertooth-dfu` by serial, one write at a time;
- reads `ubertooth-util -v` after every flash and fails on any mismatch;
- reads every other registered Ubertooth on the host before the run and after
  every flash, and fails if one changes or disappears;
- applies a 25 s per-flash and a whole-test deadline and stops at the first failure.

Add `alternate Ubertooths: true` to rotate flashes across every registered
Ubertooth on the host. The planner binds the other boards (in alias order) as
`alternated Ubertooth 2`, `3`, ... resources of the same invocation, so
preflight checks each one's serial, the manifest records them, and the
"last test on its device" rule covers every board written. Flash 1 goes to
the selected board, flash 2 to the next, and so on. Each board alternates
between the test build and KGV, starting with the first image it is not running, so
boards that start on different images get different images in the same round,
and a board's final image depends on whether its own flash count is even.
`flashes` is the total across boards and must divide evenly by the board count
(for example, 100 flashes on two boards gives each 50). The step must list its
hosts (`hosts: [Host_Linux]`, not `each`), because planning covers every
selected host and one host that cannot alternate would stop the run
everywhere; `validate-plan` reports this without a registry. Planning refuses `Ubertooth: each` with `alternate Ubertooths` (it
would rotate once per board), a host with only one registered Ubertooth, and
any board without a full 32-hex-digit serial. Every registered Ubertooth on
the host is written, including boards the plan does not otherwise use. After every flash the target is read back and every
other board must still report the revision it had when last verified. The
report names the board for each flash and the per-board count, and
`flash_run_finished` carries `serials`, `verified_flashes_by_serial`, and
`final_revisions`: the last revision read from each board, `""` when a failed
write or unreadable output left it unknown, and absent for a board never
queried. `final_revision` stays the last verified revision of the selected
board.

Device selectors are serials: libubertooth treats an all-digit `-U` value as a
USB index and matches anything else as the end of a serial (first board wins),
so the flash test refuses digit-only selectors and any serial that is the
ending of another. Register full 32-hex-digit serials.

Because the test writes the firmware it verifies, preflight checks only its
Ubertooth's serial, not its firmware revision. It must be the last test on that
Ubertooth in the suite, so no later test runs on firmware the run did not verify.

## Target runner service

`python -m ubertooth_harness runner` is the persistent target runner. It dials
the controller (outbound only; it opens no listening socket), registers, sends
heartbeats, and runs the suites the controller requests through the same
`run-suite` path, one run at a time:

```sh
python -m ubertooth_harness runner \
  --controller ws://127.0.0.1:8765/runner \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth \
  [--libbtbb-source /path/to/libbtbb --libbtbb-ref master] \
  [--plans-dir testplans] [--allow-power-changes] \
  --output runs
```

The controller URL must be `ws://` or `wss://` with a loopback host
(`127.0.0.1`, `::1`, or `localhost` when every address it resolves to is
loopback). Any other host is refused at startup and there is no override:
remote runner authentication is still an open design item (see
[Networking status](#networking-status)). URLs with credentials are refused.
The runner connects to the checked loopback address itself, ignores proxy
settings, and follows no HTTP redirects.

On every connection the runner sends `register` with its registry host, host
name, OS identity, Python version, and attached Ubertooth serials, read from
sysfs the same read-only way power isolation does (no device is opened, so no
stop is sent; on macOS the serials are `null` with an `enumeration_error`).
A `run` request names a git ref (resolved in `--ubertooth-source`), a plan in
`--plans-dir` (`fast` is `testplans/fast.yaml`), a suite, optional exact test
names, and whether to `--build`. The runner starts `run-suite` in this harness
environment with a new run ID, answers `run_accepted`, and forwards every
run.jsonl record unchanged as an `event` message as it is written, then
`run_finished` (the run's own status and exit code) or `run_error` (the run
ended without a `run_finished` record, for example an unresolvable ref). The
run directory (run.jsonl, report.md) stays the source of truth. A second
`run` while one is active gets `run_rejected`. `cancel` sends the run SIGINT,
which run-suite turns into its normal cooperative cancellation, including
restoration of isolated boards. `--allow-power-changes` is a runner flag only;
the controller cannot arm power switching.

If the controller goes away the run continues. The runner reconnects with
exponential backoff (0.5 s doubling to at most 30 s), registers again with
`active_run_id` set, and replays the active run's events from the start of
run.jsonl, so the controller deduplicates by `(run_id, sequence)`. SIGINT,
SIGTERM, or SIGHUP stops the runner after cancelling and waiting for an
active run.

Messages are JSON objects with `"version": 1` and a `type`; each type has an
exact field set, and anything else (unknown version or type, a missing or
extra field, a wrong value) gets `protocol_error` and is not acted on. The
schema is in [`ubertooth_harness/protocol.py`](ubertooth_harness/protocol.py)
and [DESIGN.md section 2](DESIGN.md#2-target-runner-protocol-controller--target-runner).

## Tests

```sh
python -m unittest discover -s tests -v
```

CI runs the suite and validates the long plan with Python 3.12 on Ubuntu 24.04
and the current macOS (`macos-latest`; see `.github/workflows/test.yml`). The
checks themselves are in `scripts/ci.sh`. To run them on an exact commit
without GitHub Actions, locally or on another host over SSH:

```sh
scripts/local-ci.sh                       # HEAD, this host
scripts/local-ci.sh -r <sha> -s "ssh lab-host"
```

The target-runner tests run against an in-process stub controller on
`127.0.0.1` with no hardware. Controller, GUI, provisioning, soak execution,
and radio fixtures remain later slices.

## Repository boundary

This repository owns the controller/GUI, target runners, test plans,
provisioning, private lab adapters, and result formats. Product host/firmware
code and deterministic C tests remain in the Ubertooth repository. Runs consume
explicit Ubertooth/libbtbb repository URLs and refs plus pinned firmware
artifacts; the harness does not import private product implementation details.

The repository is private because its design backlog includes internal lab and
eero integration details. Do not commit credentials or captured device traffic.

## Networking status

No network listener is implemented; the target runner only dials a loopback
controller. Local development remains loopback-only. Before adding the Linux-hosted GUI/controller or remote Mac/Linux target
runners, define and approve the HTTPS, browser authentication, target-runner
authentication, outbound
connection, reconnect, and authorization contract. Do not use the provisional
shared-token text in [`DESIGN.md`](DESIGN.md#8-decisions-and-open-items) as production authorization.

## License

GPLv2; see [COPYING](COPYING).
