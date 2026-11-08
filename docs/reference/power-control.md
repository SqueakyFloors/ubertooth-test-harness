# USB power control reference

Reference for the power-controller drivers, armed changes, enumeration-verified
isolation, and manual recovery. Overview: [DESIGN.md](../../DESIGN.md).

## USB power controllers

The registry models the ordered [Acroname USBHub3+](https://acroname.com/store/s79-usbhub-3p) as a typed `USB power
controller` with a 32-bit serial identity, explicit driver, capabilities, and a
unique downstream port for each powered resource. The generic controller
contract snapshots power, data, VBUS voltage, and VBUS current. A simulated
driver supports development and restoration tests without hardware.

The Acroname driver (`acroname BrainStem USBHub3+`) is validated on the Linux
lab host with a USBHub3+ (model 19, firmware `0x28000000`) and BrainStem
2.12.5. BrainStem is Acroname's proprietary Python API (Acroname BrainStem SDK
license; its wheel bundles the native library for Linux x86_64/aarch64,
macOS, and Windows), so it is not in [`requirements.txt`](../../requirements.txt). Install it into the
harness environment only on a host with a hub:

```sh
python3 bootstrap.py --with-acroname
```

That installs [`requirements-acroname.txt`](../../requirements-acroname.txt) in pip's hash-checking mode
(`brainstem==2.12.5` and its `cffi`/`pycparser` dependencies, each pinned with
its files' sha256). Without it the driver fails closed with `BrainStem Python
API is not installed`. The driver imports BrainStem lazily, connects over USB
to the one configured controller serial (a hub with another serial is not
found), reads the serial and model back, and addresses downstream ports 0-7
through the hub's `usb` entity: `getPortState` gives power (bit 0) and USB 2.0
and 3.0 data (bits 1 and 3, reported and switched as a pair; a port with only
one on reads as data off, and isolation, which switches only VBUS, leaves it
as found), `getPortVoltage`
and `getPortCurrent` give VBUS in microvolts and microamps. Every BrainStem
error code fails the call, every write is read back (a stale readback fails),
and ports outside 0-7 are refused before the hub is asked. Real power/data
changes are refused unless armed (`run-suite --allow-power-changes`). Ports
are restored to their original power and data state after success, failure,
or cancellation. The hub keeps answering on its control link while every
downstream port is off.

Measured on the lab hub: a board leaves USB enumeration 0.06-0.22 s after its
port is switched off; VBUS is below 0.1 V about 0.55 s after switch-off with a
board attached; a board re-enumerates 0.37-0.59 s after power-on and answers
`ubertooth-util -v` about 0.02 s later. The isolation defaults (5 s removal,
10 s return, 1 s settle; [`enumeration.py`](../../ubertooth_harness/enumeration.py)) leave ample headroom.

`ubertooth-harness power --hardware hardware.yaml <controller>` reads every
port of a registered controller (power, data, VBUS, current, and the board
mapped to it) without changing anything; `--json` gives the same as JSON. It
takes no lease or host lock, so while a run holds the hub it may fail to
connect (nothing is switched either way):

```text
Hub_Linux1 (acroname BrainStem USBHub3+), firmware 0x28000000
port 0: power on  data on  VBUS  5.11 V   81.9 mA  UT_Linux2
port 1: power on  data on  VBUS  5.11 V   85.8 mA  UT_Linux3
...
port 4: power on  data on  VBUS  5.12 V    0.0 mA
```

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
It needs [`pyserial`](https://pyserial.readthedocs.io/) (pinned in [`requirements.txt`](../../requirements.txt)), imported lazily. Standard
hub power control ([`uhubctl`](https://github.com/mvp/uhubctl)) only changes the reported state on this hub and
does not cut power, so it must not be used.

Isolation is confirmed from USB enumeration instead (read-only sysfs scan in
[`ubertooth_harness/enumeration.py`](../../ubertooth_harness/enumeration.py)): the selected board must become the only
Ubertooth enumerated, and isolated boards that were enumerated beforehand must
come back after restoration. See [DESIGN.md, "Power-controller boundary"](../../DESIGN.md#7-execution-and-safety-invariants), for
the exact rules and error precedence.

Plans requiring `exclusive Ubertooth: true` compile deterministic power-off
actions for every other registered Ubertooth on the host. An extra board on no
power controller blocks the step and fails the run (see
[Hardware a step is about](../guide/plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out));
a mapping that is there and wrong, including a controller without per-port
VBUS switching, is refused instead.
`run-suite` applies those actions only with `--allow-power-changes`; without
it, the run fails before anything executes. A host with only the selected
board registered switches nothing and needs no flag (see the read-only check
below). Each controller is built from its registry `driver` (`startech
managed USB hub` or Acroname BrainStem), so one run can mix switch types.
Both the StarTech and the Acroname paths are validated on hardware (the
Acroname one with `testplans/exclusive.yaml`, three other boards switched off
per invocation, and a cancellation that restored every port,
[#2](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/2)). The simulated driver is for unit tests only and is
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
and 4 set) and port 3 must come back, send `SPpass    0D000000`. Acroname
(with BrainStem installed), holding the board's lease and the host lock so no
run switches power or flashes meanwhile; replace `SERIAL` with the hub's
registered serial (for example `0x1234ABCD`) and `PORT` with the port number
from `isolation_applied`:

```sh
python -m ubertooth_harness lease --hardware hardware.yaml --host-lock UT_Linux1 -- \
  .venv/bin/python -c 'import brainstem; h = brainstem.stem.USBHub3p();
assert h.discoverAndConnect(brainstem.link.Spec.USB, SERIAL) == 0;
assert h.usb.setPowerEnable(PORT) == 0; h.disconnect()'
```

then check it with `ubertooth-harness power`. A run with power isolation
(StarTech or Acroname) holds the [host lock](../guide/leases.md#sharing-boards-leases) for the
whole run, so no other harness command switches power or writes firmware on
that host meanwhile.

[`testplans/exclusive.yaml`](../../testplans/exclusive.yaml) runs the `Exclusive Ubertooth topology` test, which
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

[`testplans/power.yaml`](../../testplans/power.yaml) runs the `Reset into invalid
application` test, which uses a hub port the other way round: instead of
switching other boards off, it power-cycles the board under test. It writes an
image the bootloader must refuse to start (a valid Ubertooth DFU file whose
two vector words are erased), cuts the board's registered port, turns it back
on, and requires the board to come up as a DFU bootloader device rather than
running the image; then it writes the run's own image back and checks the
revision. The port comes from the board's `power:` mapping, and the plan names
the controller it must be on, so a bench whose wiring moved fails planning
rather than switching whatever is on that port.

**The board's bootloader is the precondition.** The verdict is about the
bootloader already in the board's flash, and `ubertooth-dfu` writes the
application region only: a bootloader is installed over a programmer on the
board's in-system programming header. On a board whose bootloader came from
this tree the test is a test; on a board with the factory bootloader it is a
way to brick that board, which then needs its P4 entry pin bridged. So the
plan names the board the registry pairs with a `Debug programmer` — the board
that has a recovery path — and the run holds the programmer with it.
`--bootloader check` does cover this board, so run it with that option: the
read now reaches every Ubertooth a test of the suite selects, this one
included
([#327](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/327)).
Because the board's application image is written by the test rather than
required by the run, it may be carrying firmware older than the request when
preflight reads it; that answer is recorded as `not read` with the reason and
the run goes on, where any other board that will not answer fails. A board
that does answer is compared like the rest, and `check` fails the run on a
stale bootloader there. `--bootloader update` still does not reach it — the
write covers the boards whose firmware preflight requires, which this one is
not — so the reason sends you to the programmer, and the stage says it had no
board to install on. Reading `-B` before and after by hand is still worth
doing, since preflight reads it before the test and nothing reads it after.

**Run it by hand, with someone who can reach the bench.** It is destructive:
if the bootloader does start the unstartable image, the board faults on every
power-up and needs its P4 entry pin bridged by hand, which no harness command
can do. That is why it is in a plan of its own and not in `release.yaml` or
any other unattended plan. The case's cleanup powers the port on and writes
the run's image back whatever happened, including when the cycle command
itself is killed, so an ordinary failure still ends with a usable board. A
run killed outright (SIGKILL) is the exception: it leaves whatever it was in
the middle of — the port off, or the board in its DFU bootloader with the
unstartable image in flash — and both are put right by hand, as after a
killed power-isolation run (above). The next run cannot do it for you:
preflight reads the board's serial before any case runs, and a board in DFU
reports none.

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/power.yaml --suite power \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --build --with-firmware --output runs \
  --bootloader check \
  --allow-power-changes --allow-destructive
```

`--allow-destructive` is what makes this a decision somebody takes: without
it `run-suite` refuses the test before anything is built.
`--allow-power-changes` cannot stand for it, because a runner service is
deployed with that option so an exclusive-Ubertooth plan can run, and then
passes it to every run it dispatches. `--allow-destructive` it never passes
on, so no dispatched run, and no click in the GUI, can start a test that
could leave a board needing the bench.

It writes the board's firmware, so it must be the last test on that board, and
the run holds the [host lock](../guide/leases.md#sharing-boards-leases) throughout. Both options
are needed for `--dry-run` as well, since the refusal comes before it: a dry
run that exits 0 means the suite can run as asked.

[`testplans/mac.yaml`](../../testplans/mac.yaml) is the macOS plan, for a Mac
host (`Host_Mac` in the registry). The firmware and radio are the same on every
host, so it leaves out what the Linux plans already prove about a board pair
(the range test and following) and the command-line refusal matrix, which the
fork's macOS CI runs. It covers what macOS changes on the host side: the build
(run it with `--build --with-firmware --with-python`), the USB stack (`-N`,
`-r` and the board coming back, util mode start and stop, DFU detach and 20
alternating flashes; the listing and `-U` by index come from `Multi-board
selection`, which skips itself on a host with one registered board), and
streaming (timed and signal-stopped captures to files and standard output,
specan, and the extcap FIFO), plus one smoke pass of the radio-setting, CRC,
BLE settings and debug register tests. `ubertooth-scan` and `ubertooth-follow`
need BlueZ and are not in it. `Flash reliability` alternates the run's build
with the KGV release image, so the run also needs `--artifact "KGV=<kgv.dfu>"`:

```
.venv/bin/python -m ubertooth_harness run-suite --plan testplans/mac.yaml \
  --suite mac --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --build --with-firmware --with-python \
  --artifact "KGV=/path/to/kgv.dfu" --output runs
```

It runs once a Mac runner exists.

[`testplans/range.yaml`](../../testplans/range.yaml) runs the `Two-device range test`
between the host's default board (sender) and `UT_Linux1` (responder). The
test transmits: both boards send on 2.4 GHz, the sender at every PA level up
to 0 dBm. Run it only where that is allowed, for example with the boards in a
shielded box or on an RF-isolated bench. Both boards need firmware built from the commit under
test; a `--build` run flashes both with its own build before preflight, as
for any device test (see [Flashing the boards with the run's
build](../guide/builds-and-firmware.md#flashing-the-boards-with-the-runs-build)):

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/range.yaml --suite range \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --libbtbb-source /path/to/libbtbb \
  --libbtbb-ref d356e9da7fb03969371aa14d603a8aad2f0ef198 \
  --build --output runs
```

The plan's second suite, `range-weak-link`, runs the same test with the
weak-link receiver answering, choosing both boards by RF placement rather than
by alias: a responder recorded at exactly the 20 dB the bench measured, with
its antenna attached, and an unattenuated sender. Its one recorded run, on an
older fork, stopped at the `-n` step: the `waiting for the repeater...` line a
repeater slower than the first progress interval makes `-n` print went to
standard output then
([ubertooth#1026](https://github.com/SqueakyFloors/ubertooth/issues/1026),
fixed in `30c4103f`). Nothing has run the suite since, so it has still never
judged a result. [docs/tests.md](../tests.md#two-device-range-test) has the
measurement and the selection.

On current fork main the `range` suite passes: the sender gets a clean round trip,
`-n` reports its request and reply PA levels and copy numbers, and `-m` reads
the same result back (fixed by
[ubertooth#601](https://github.com/SqueakyFloors/ubertooth/pull/601); before
it the reply read back as zeros, [ubertooth#483](https://github.com/SqueakyFloors/ubertooth/issues/483),
and earlier still no reply came at all, [ubertooth#26](https://github.com/SqueakyFloors/ubertooth/issues/26)).
On the lab bench (two boards a few centimetres apart, PA 0) 9 of 10 runs pass
with PA level 0 and copy numbers 0-6; about one run in ten the firmware takes
a CRC-OK packet that is not a copy of its request and `-n` reports `invalid
range test: mismatch on byte 0` and exits 1, so the test fails there, before
`-m` is read ([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)).
Since fork `0684c11` ([ubertooth#445](https://github.com/SqueakyFloors/ubertooth/pull/445)) both boards keep answering USB while the
modes run and `-S` stops the repeater, so the run cleans up and needs no power
cycle. Before `0684c11` the repeater answered no USB request once started, the
sender stopped answering too, and both boards needed a power cycle.

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
surface`, `Ubertooth count`, and grouped `Ubertooth identity` command cases.
The host surface runs `-h` and `-V` on every C host tool the build installs,
retaining each tool's result: the eleven always-built executables, and the
BlueZ-only `ubertooth-scan` and `ubertooth-follow`, whose two cases are not
applicable (left out before the run, see
[Host requirements](../guide/plans-and-resources.md#host-requirements)) where
they are not built. Both questions come from one list of tools, so a tool the
fork adds is asked both with the build rather than when somebody edits a
second list; `-V` used to be asked of four of the thirteen
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).
`Ubertooth count` checks that `ubertooth-util -N` equals the
registered Ubertooths on the host (the selected board plus its guards), so an
unregistered or missing board fails it. Because it uses guards, the fast and
release plans need every registered board attached and readable, like the
transition tests. The identity test runs isolated firmware, version (`-V`), serial, part, and
board queries plus the `-vspb` composition of the four that compose (`-V`
ends the command, so it is only ever asked on its own). `Debug register read`
constructs a serial-selected `ubertooth-debug` command from the normalized plan
register. Each case retains its own command, output, log, and result while the
parent YAML item reports one aggregate outcome. Remaining named tests are
catalog-validated and resource-planned but gain their hardcoded implementations
in subsequent slices.

`Host CLI surface`, `Raw receive replay`, `Refused command lines`,
`Failed capture files`, `Util mode refusals`, `DFU image checks`, and
`Python tools` need no Ubertooth, so `testplans/host.yaml` runs on a host with none registered or attached; a
registry holding just the `Host` entry is enough:

```sh
python -m ubertooth_harness run-suite --plan testplans/host.yaml --suite host \
  --hardware host-only.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --libbtbb-source /path/to/libbtbb \
  --libbtbb-ref d356e9da7fb03969371aa14d603a8aad2f0ef198 \
  --build --output runs
```

`Raw receive replay` writes its own synthetic `ubertooth-rx -i` input with
`ubertooth_harness/rxreplay.py` (spec sync words, HEC, whitening, and a DH1
CRC, so libbtbb derives a known UAP); no over-the-air capture is used or
stored.

`Refused command lines` runs every tool as `env UBERTOOTH_TEST_NO_DEVICES=1
<tool> ...`, so a command that got past its argument checks finds no board
instead of opening one (libubertooth, fork `b6d8ede`). Its first step reads
the bytes of the libubertooth beside the tool on `PATH` (never running a tool)
and skips the case when that library lacks the variable's name (only the built
library can tell). The `ubertooth-follow` and `ubertooth-scan` cases need
BlueZ, so they are not applicable on macOS. Each file-option case seeds files
with known contents in its own capture directory, refuses one command naming a
new file and one naming a seeded file, then checks that nothing was created,
truncated, or left behind (`ubertooth_harness/refusal.py`). The stray-word
cases on `ubertooth-scan` and `ubertooth-follow` name no file and seed none:
each refuses one command and checks that its capture directory stays empty.

`Util mode refusals` runs `ubertooth-util` the same way, with the same skip,
for the mode option the harness never runs on a board: `-i` (ISP) leaves the
application firmware. Each combination must be refused, exit 1, with the fork's
message before any board lookup (no switch note), so a regression that accepted
one would find no board instead of stranding it. `-f` (DFU), removed in fork
`db5649b`, must be refused as an unknown option. The
cleanup step runs `ubertooth-util -V`, whose host version line needs no
board.

`DFU image checks` runs `ubertooth-dfu -d <image> -r` and `-s` the same way,
with the same skip. `-d` checks the whole image before it opens a device, so
each bad image must be refused before the board lookup (no switch note, no DFU
stage log), with one `error:` line naming the file, what is wrong with it, and
that the firmware will not be updated, and must leave its directory, the image
included, exactly as it was; a good image must get past the check to the
lookup, which finds no board. The host-only plan has no firmware build (CI has
no ARM toolchain), so the cases use a synthetic image (`dfuimage.py synth`:
filler carrying a firmware info block that names the revision
`harness-synthetic`, which `-d` reads out of the file, and the suffix the
harness computes) and derive the bad images from it as `DFU images` does
from the test build. `sign-test-build` signs the test build's binary and
compares the result with the build's `.dfu`; it needs a firmware build
(`--build --with-firmware`, or `--artifact "test build=PATH"`) and is not
applicable without one.

`Python tools` runs `ubertooth-specan-ui -h` with no display (`DISPLAY`,
`WAYLAND_DISPLAY`, and `QT_QPA_PLATFORM` removed) from a
`--build --with-python` prefix, and `btle-extcap.py` (which CMake never
installs) exactly as it is in the resolved Ubertooth commit, read with
`git show`. specan-ui imports numpy, qtpy, and a Qt binding before it parses
`-h`. Its case needs the Python tools build, so it is not applicable without
`--build --with-python`, and is skipped at run time, with the reason, when
that interpreter lacks one of them. The btle-extcap cases need nothing from the
host beyond `python3` and the tools every build has. `BLE extcap interfaces` runs
`btle-extcap.py --extcap-interfaces`, which opens every attached board through
`ubertooth-util -U all -s`, so it leases and guards them like `Ubertooth count`.
Both run through `ubertooth_harness/pytools.py`. `BLE extcap capture` runs
`btle-extcap.py --capture` through `ubertooth_harness/extcapcapture.py`, which
does what Wireshark does: it creates the FIFO, reads it, and stops the script
with SIGTERM after `duration` (never SIGKILL). The script's stdout, where
`ubertooth-btle` prints decoded packets, is discarded and only record counts
are logged, so no captured traffic reaches the run record.

`BLE extcap in tshark` adds the one thing those cannot show, that a real
Wireshark agrees. `ubertooth_harness/tsharkcapture.py` installs the plugin in
a Wireshark configuration of its own (a temporary `HOME`, so the user's own
configuration is untouched), and real
`tshark` then calls it as that Wireshark version does and decodes the records
with its own dissector. The installed program records every call it is given,
which is what shows that it, and not another extcap program the host has
globally, is what tshark captured with. The row it must print is known in
advance, because an
nRF52840 DK advertises from its registered address. Its cases declare that
they need `tshark` installed, so a host without it runs none of them (not
applicable, and `--dry-run` says so) instead of flashing the DK to find out,
and the version that read the records is in the run manifest
(`environment.host_tools`). Only counts and the advertiser's own address are
printed, never another device's address.

