# Option compositions, guards, and the firmware watchdog

What the stateful transition tests do, which boards a test guards, and how the
firmware watchdog record is read. What each test checks is in
[docs/tests.md](../tests.md) and in the catalog the GUI reports.

Stateful host CLIs need more than isolated option checks. Plans must select a
`coverage` profile for each transition test; the Python implementations own
every command, expected state, and cleanup step. [`testplans/fast.yaml`](../../testplans/fast.yaml) runs
`smoke`; [`testplans/release.yaml`](../../testplans/release.yaml) runs `comprehensive` plus the BLE mode
conflicts, before release preparation:

```sh
python -m ubertooth_harness validate-plan testplans/release.yaml \
  --hardware hardware.yaml --suite release
python -m ubertooth_harness run-suite \
  --plan testplans/release.yaml --suite release \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main --output runs
```

All but one test in that plan runs on any target. The exception is
`Recover AFH map`, which needs the two ESP32 Classic peers and so names the
Linux host they are on; it carries `required hardware: true`, so a bench
without them runs the rest of the plan and the run fails on it rather than
passing with that gate unmeasured (see
[Hardware a step is about](plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out)).
It runs the `smoke` profile there, about 520 s of the plan's roughly 1175 s.
A target that is not the host it names plans about 655 s of the suite, which
is what run setup shows for that target, with this step named as one it does
not run — and it holds Run there, because such a run could not measure the
gate whatever that bench has (see [Run setup](services.md#web-gui)). Run this plan on
the host the step names, or as a run group across the bench, which measures
it once.

That step leases both ESP32 Classic bridges for the length of the run, so the
peer suites run before or after the release plan, not beside it.

The other tests that need a peer device are not in the plan: run the `ble`,
`connection`, `classic`, and `btbr` suites as well, wherever those devices
are registered. The `btbr` suite is the only coverage of `ubertooth-btbr` and
of the `btbr` firmware image, and it needs `--build --with-firmware
--with-python` (the test names the alternate image itself, so that run builds
it too; `--with-btbr` asks for it outright). The `classic` suite repeats the gate's two AFH captures;
`classic-comprehensive` runs them with the third, `-t` detection case as
well, so it is the one to run after the gate.


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

Capture cases write to `captures/<invocation>/<case>/` in the run directory
and check the file header with `ubertooth_harness/capturefile.py`, run by path:
`ubertooth-btle` `-r` PCAPNG and `-q` PCAP must have link type 256
(`LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR`), `-c` PCAP link type 192
(`LINKTYPE_PPI`). `ubertooth-rx` `-r` PCAPNG and `-q` PCAP must have link type
255 (`LINKTYPE_BLUETOOTH_BREDR_BB`). `ubertooth-dump -d` and `ubertooth-rx -d`
files are checked with `ubertooth_harness/rxdump.py` (record size, packet
type, channel index, host times), which prints only a record count. Only the
header is read; nothing from the packets is logged.
Capture files can hold nearby devices' traffic, so they stay in the run
directory and must never be committed. The receive tools' own output (the
`ubertooth-rx` packet lines with nearby LAPs) is logged like any other
command output.

Not checked or restored, because no option the cases use can read it: the
modulation (the firmware stop returns it to basic rate), the BR/EDR target
address and LE advertising channel (cleared by a mode stop), the BLE
follow target, the E-GO capture length and access code (`Raw receive`
sets `ubertooth-ego -l1`/`-l48`/`-a`; a later `-l`/`-a` or `-r` replaces them),
and the board's AFH channel map, which no `ubertooth-util` option reads or
writes. No case leaves one set: `ubertooth-follow` clears the map without
`-a`, and the bench measured that it clears it with `-a` too against this
peer, because the peer reports AFH off and the tool takes its disabled
branch.
The BLE access address (reset to 8e89bed6 by `-r`) is read with
`ubertooth-btle -a`: `BLE settings` records and restores it; other cases leave
it alone.

LEDs are recorded and restored as a USR/RX/TX triple. `-l` sets USR only and
`-d` sets all three, so a state with RX different from TX can be read but not
written back. Any case whose original for a setting it may change cannot be
written back (RX different from TX) is reported `skipped`
before anything changes; if a value turns out unrestorable after a change,
cleanup fails. Failure reasons name the output pattern that did not match. A continuous mode that
reaches its deadline reports `command_finished` status `stopped`.

`ubertooth-util -a0` (or `-a 0`) sets PA level 0, so every PA level can be
written back. That needs fork main 4ed4a1d or later; on older builds `-a0`
was a query and the PA-0 cases and the full-reset case fail. On hardware a
powered-on or reset board reports PA level 7 (checked on current main and the
2020-12-R1 firmware); the full-reset case sets PA 0 before `-r` and checks
that boot default, so a board found at PA 0 is covered too.

Option values follow the fork's one argument rule (8b58f75 for
`ubertooth-util`, 0b63b01 for the other tools): a value is attached or the next
word, a next word is taken only when it looks like a value (so `-c -b` is two
queries), and any leftover word is refused before a device is opened. A set and
a read of the same `ubertooth-util` value in one command both run, the set
first (`-c2426 -c` reports 2426; fork 5b45b3e,
[SqueakyFloors/ubertooth#228](https://github.com/SqueakyFloors/ubertooth/issues/228)). `test_finished` reports `failed_cases`,
`failed_guard_checks` (see [Guard Ubertooths](#guard-ubertooths)), `skipped_cases`, and `not_applicable_cases`
(see [Host requirements](plans-and-resources.md#host-requirements)); the run summary (`run_finished`
in run.jsonl, and report.md) reports the run's `skipped_cases`,
`not_applicable_cases`, `not_applicable_tests`, and `blocked_tests`, so none
of them is hidden by an
overall pass. Event schema version 1 is pre-release
and has no stable consumers yet; field changes before the controller lands do
not bump it.

A state-changing case stops at its first failed step, unless the case declares
a group of steps to run again (`StateCase.retry_steps` and `attempts`): a
failure inside that group starts the group over, up to that many attempts, and
only the last attempt's failure ends the case. It is for a case whose outcome
depends on what arrives over the air, where the devices keep making the
traffic and another try costs only the capture window; the steps in the group
have to be repeatable and leave the board as the first attempt found it. From
the second attempt on, each step's `step_finished` event carries its `attempt`
and the saved output of its command is kept separately, and the case's
`check_finished` event carries the `attempts` it took. A cancellation, and a
failure anywhere else in the case, end it as they always did.

The parent test moves on
to the next case only when that case's cleanup verified the device restored;
otherwise the test stops. Cleanup runs with its own hard-timeout commands even
after cancellation or an internal error such as a failed event write, during
the transition or during cleanup itself (remaining restores run without
events, then the error is raised), a cleanup failure fails the case, and a passing cleanup
never clears an earlier failure. A case is reported `canceled` only when
cancellation was its sole failure. Restore steps that depend on a value never
recorded are reported as skipped when nothing was changed and as failed
otherwise. A step whose *comparison* alone depends on such a value is
different: a cleanup step may compare two answers to one question, read
either side of something that must not have changed it, where the first
reading can only be taken after a mutating step (an alternate firmware image
written, so that a bootloader read under it means anything). The step asks to
compare with that recorded reading rather than with a value of its own, so a
case that stopped, was canceled, or was skipped before the reading was taken
still runs the step and is judged by its own checks, and the cleanup counts
as verified. A case that merely failed therefore goes on to the next one
instead of reporting a board nobody restored; a cancellation still ends the
test, as it always has, but the record says the board was put back rather
than left. The step's
`step_finished` event names the reading it had nothing to compare with
(`uncompared`), and the report and the live console line say so beside what
it recorded. Where both
readings are there the comparison is made and a difference fails the cleanup.
Nothing is invented in either case, and a case that asks to compare with a
reading no earlier step of it takes is refused where it is built, so a
misspelled name cannot quietly stop comparing. The tolerance is given to a
case's own steps alone: a step no case owns (the guard reads) fails on the
absence instead.
Every step that runs, is skipped, or is canceled before it starts
emits a `step_finished` event with its reason, expected and observed values,
and argv (`null` for a step that did not run). Steps after the first failure or
skip emit nothing. Aggregate reasons are bounded; each
case's full reason is in its own `check_finished` event.

`Ubertooth util conflicts` keeps its `allow RF transmission: true` guard:
fork main refuses every pair before a mode starts
([SqueakyFloors/ubertooth#82](https://github.com/SqueakyFloors/ubertooth/issues/82),
fixed by #134), but a regression that accepted a pair would start range test,
repeater, or TX test and transmit. It is in [`testplans/release.yaml`](../../testplans/release.yaml)
since a passing hardware run at fork 3b22c95.
The full-reset case can fail on macOS when re-enumeration takes longer than
the fixed 2 s wait inside `ubertooth-util -r`
([SqueakyFloors/ubertooth#94](https://github.com/SqueakyFloors/ubertooth/issues/94)).
Repeated `ubertooth-btle` options (an option given a value twice is refused;
`-q` and `-c` share one capture handle, so the first file given wins) are
tracked in issue
[#14](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/14).
`ubertooth-util -i` and `-f` are excluded because an accepted conflict could
leave the board outside the application firmware. Against current firmware,
pairs that start range test, repeater, or TX test transmit on 2.4 GHz until
cleanup's stop, so the test requires `allow RF transmission: true`; PA level
and channel are recorded and restored. Pairs with `-I` never exit on their own
(identify handles only SIGINT) and run to the 5 s timeout before cleanup.

### Guard Ubertooths

Every test in the catalog ([docs/tests.md](../tests.md))
except `Raw receive replay`, `Refused command lines`, `Failed capture files`,
`Util mode refusals`, `DFU image checks`, and `Python tools` (which open no
Ubertooth), and
`Ubertooth count` and `BLE extcap interfaces`,
binds each other registered Ubertooth on the host as a `guard Ubertooth 1`,
`2`, ... resource of the same invocation, so the
[Results table, transcript](../reference/run-records.md#run-output-runjsonl-and-reportmd), and manifest
name the guards. The design is in
[DESIGN.md, "Named test plan schema"](plans-and-resources.md). A host with one
registered Ubertooth has no guards. Preflight checks each guard-only board's
serial (`ubertooth-util -U <serial> -s`) and that its firmware API (`-v`) is
the host tools' own; a guard carrying any other API is refused before any
test, because `ubertooth-btle` may stop such a board rather than read it
(below). Its firmware revision is not required to match the resolved one. Guards do not count toward the
firmware-order check (a firmware-writing test must be the last test on its
board), so a guarded test may follow a flash on another board.

A guarded test therefore needs every registered Ubertooth on its host to be
attached, idle, and readable, each registered with its full 32-hex-digit
serial (libubertooth matches `-U` case-insensitively against the start of a
serial). `run-suite`
refuses shorter serials for the selected host before any build or device
access. Planning stays permissive: `validate-plan --hardware` prints a
`warning:` line for each such invocation but still exits 0.

Before the first case the test records a baseline for each guard with
read-only queries only: `ubertooth-util -U <serial>` with `-v` (firmware
image, revision and API), `-V` (the version answer, whose `Firmware build:`
line carries a build timestamp that also catches a reflash of the same
revision), `-s` (the serial the board itself reports), `-B` (the bootloader
under that firmware), `-C` (channel), `-a` (PA level), `-z`
(squelch), and `-d` (LEDs), then `ubertooth-btle -U <serial> -k` (CRC).
None of the `ubertooth-util` options these reads use checks the firmware API,
but `ubertooth-btle` does, and
it may refuse any API that is not the one the tools were built for — newer as
well as older — stopping that board as it goes; whether it refuses or warns
and reads on is how those tools were built and whether
`UBERTOOTH_ALLOW_API_MISMATCH` is set; the run does not set that variable
and cannot tell how the installed tools were built, so it cannot know which it
would get
(`ubertooth_check_api` in the
fork's
[`host/libubertooth/src/ubertooth.c`](https://github.com/SqueakyFloors/ubertooth/blob/main/host/libubertooth/src/ubertooth.c)).
So the CRC read runs only when the same check's `ubertooth-util -v` read showed
that same API (no host tool reports it, so the harness reads
`UBERTOOTH_API_VERSION` from the resolved Ubertooth source and refuses guarded
tests if it cannot). Otherwise the read is refused without running and the
baseline fails, naming the API the guard reported and the one the tools need.

Every read goes to every guard whatever firmware image it carries. A board
refuses a request its image has not got by stalling it, and `ubertooth-btle`
refuses a board carrying another of this tree's images (`btbr`) as it opens
it; either answer is recorded as the reading and compared before and after
like any value, so a board that starts answering a read it refused, or
refuses one it answered, fails the check. The LED read is the exception: `-d`
asks for all three LEDs in one command, so a stall there fails the read
rather than being recorded. A guard running `bluetooth_rxtx`, or the
receive-only `bluetooth_rx` build of it, must answer the radio settings that
build implements, and a guard that answered none of `-v`, `-V`, `-s`, or `-B`
is refused by name before any case: nothing about it could be compared
afterwards. [docs/tests.md, "Guard Ubertooths"](../tests.md) has the whole
rule.

The harness
never sets, stops, or resets a guard: every command a guard is sent is a
query, and the one that could write to it — `ubertooth-btle`, whose API check
may stop a board — is refused rather than sent whenever that board's API is
not the one this check read. A board another process holds open is not
disturbed either: libubertooth reports the claim failure and closes the board
without a stop, so that it does not interrupt whatever has it.

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
reflash, a board answering as another serial, a changed bootloader, and any
change to channel, PA level, squelch, LEDs, or CRC. On a guard
idle at boot defaults it cannot see a leaked `-S` or `-r`, or a mode start
(`-x`, `-t`, range test, repeater, `ubertooth-btle -n`/`-f`) that leaves those
values unchanged, so a guard could be left transmitting without the test
failing. Modulation, access address, and USB re-enumeration are not read.

### Firmware watchdog

The fork's firmware resets a board that stops servicing USB or whose mode
stops looping, and keeps the reason in a corner of RAM a reset does not clear.
`ubertooth-util -U <serial> -w` reads that record. Every run reads it on each
Ubertooth it binds, guards included: after preflight (the baseline), between
cases once three minutes have passed since the last read, between the writes
of a flash soak on the same three minutes, after each test, and once more at
the end of the run. The reads are read-only and never run beside another
command.

A self-reset is the watchdog working, so the run **reports** it. A read whose
unrequested reset count has risen since the baseline, or that recorded a new
processor fault, becomes a warning on that board and on the test; the test
still passes. The warning names the mode and the wait the firmware
recorded, for example "in BLE follow mode, running a control request".

The count, not the boot cause, is the signal. A reset the host asked for
(`-r`, or `ubertooth-dfu` entering the bootloader) is a watchdog reset as far
as the microcontroller is concerned, so `Last boot cause: watchdog` turns up
on a healthy board; the firmware leaves those out of the count. A boot cause
that gained `watchdog` with no change in the count is therefore recorded as a
note, not a warning.

Both counters are since power-on, so cutting power to a board starts them
again; so does flashing firmware that does not recognize the record it finds.
A count below the baseline's, or a boot cause that gained `power-on` or
`brown-out`, is read as that restart: the board is re-baselined where it
restarted, with a note naming the causes, and only resets after it are
counted. Both counts stop at 255; a board already there is noted at the
baseline, because a reset can then no longer change the count. The radio-wait
counts are since boot rather than since power-on, so every reboot starts them
again.

A reset is attributed to the test after which it was first read. A board no
test binds is read at the baseline and at the end of the run, so a reset on it
belongs to the run rather than to a test.

A read during a test covers only the boards that test binds: another test's
board may be switched off by this one's power isolation, and its resets are
not this test's to count. The baseline and the run-end read cover every board.

The reads between cases need a test with more than one case. `Flash
reliability` compiles to a single case however many flashes it runs, so its
reads happen between its writes instead: the flash runner gives the monitor a
turn after each write and its read-backs, where no other command is running,
and the time a read takes is added back to the test's deadline so a check the
harness asked for cannot be what ends a soak. A test that is one case and
writes no firmware is still read at its start and its end only.

One limit worth knowing: the firmware records one processor fault, the last,
so a board that faults the same way twice is reported once, because the record
cannot tell a repeat from the fault it already held.

Two things fail:

- A board that does not answer on USB. It fails its test and stops the run,
  because nothing can use it again; one that does not answer at the baseline
  stops the run before any test runs. A read that brings back no record is
  tried three times, about two and a half seconds apart and each read bounded
  at ten seconds, because that is how a board looks for a moment after the
  watchdog has reset it: it has re-enumerated and every request to it is
  refused (`libusb -9`) until the host notices. A record that did print is the
  read, whatever became of the rest of the command, with a note saying what
  else it said: a board that printed its record answered. While a run is being
  cancelled a board is read once and one that does not answer is recorded
  without failing, since stopping the run is what was asked for and the board
  may have gone as part of it.
- A test whose plan sets `max_watchdog_resets: <n>` and whose boards reset
  themselves more than `n` times. Nothing else makes the count pass or fail,
  and there is no limit by default. This is for stability runs (the release
  soak, [ubertooth#703](https://github.com/SqueakyFloors/ubertooth/issues/703)).
  Every test accepts the argument. The boards are still in a known state, so
  later tests run.

Capture transfer errors and radio waits that gave up are recorded as notes
with no warning: each transfer error costs the packet that was arriving, and a
radio wait that gave up means what that mode captured may be incomplete.

Firmware without the request (2020-12-R1, which a flash test writes on
purpose) refuses it, and keeps refusing it. That is how it is told from a
board the host has not yet noticed re-enumerating, which answers inside the
window: the last read of the window decides, and a board still refusing the
request there reads as "watchdog not reported", in the tool's own words, which
is neither a warning nor a failure. Such a board stops being waited for as
soon as it refuses the request again, and gets the window back the moment
something else is wrong with it, such as the read right after a test
reflashes it.

A commit that has the watchdog record and not the radio waits prints the
record and refuses the second read; the record still counts, with the waits
read as not reported. Host tools older than the ones that added the last two
lines of the report print the rest without them.

For each board that reset itself, the run's record and `report.md` carry a
ready-to-file fork issue (board, firmware and host commits, mode, wait, test,
time, and the `-w` output) and the open fork issues a `gh issue list` search
for the same mode and wait found, so the reset can be commented on an existing
issue instead. A search that cannot run (no `gh`, not signed in, no network)
says so, and the issue is still offered.

