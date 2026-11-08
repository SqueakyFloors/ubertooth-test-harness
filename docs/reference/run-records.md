# Run records: run.jsonl, report.md, and output files

What a run writes, how checks read it, and how a finished run is re-checked.

## Run output: run.jsonl and report.md

A test that fails does not end the run when the device is back in a known
state: every failed case was verified restored and every guard read matched.
Later tests still run, the run fails, and its summary names the first failed
test and how many others failed. The run stops after a failed test only when
that state is not known: a case was not verified restored, a guard changed or
could not be read, a flash failed, power isolation failed, or the run was
canceled. A failed test's `test_finished` says which with `stops_run`. A
later cancellation does not hide an earlier failure: the run still fails.

A test run is one `run-suite` run of a plan suite. It writes exactly two files
into `<output>/<run-id>/` (plus `build/` with `--build`, `captures/` when
a capture case ran, and `output/` when a command printed a lot):

- `run.jsonl`: every record, appended and fsynced as it happens. The first
  records are `run_started`, then the comparisons with the fork at the
  commit under test -- `libbtbb_pin` ([Building the resolved
  revision](../guide/builds-and-firmware.md#building-the-resolved-revision)) on a run that builds or names a
  libbtbb source, and `dfu_part_table`, the flash sizes a DFU read-back is
  checked against ([DFU images](../guide/builds-and-firmware.md#dfu-images)), on every run; neither fails a
  run -- and `manifest` (sources, plan and registry hashes,
  artifacts, invocations, Python/OS environment, and build toolchain), then
  build commands, preflight, and each test's events, including every command
  and its output as it arrives. One `watchdog` record per board per read
  ([Firmware watchdog](../guide/transitions-guards-watchdog.md#firmware-watchdog)) carries that board's record and
  what changed, and `watchdog_summary` at the end of the run carries the
  per-board self-reset count and any fork issue to file. `run_finished` holds
  the summary, whose `watchdog_resets` is the run's total and whose
  `duration_s` is how long the run took, in seconds and unrounded, with
  `duration_hms` beside it: the same length of time in words
  (`"duration_s": 4234.535980591027, "duration_hms": "1h 10m 34.536s"`).
  Automation reads the number, which nothing rounds or replaces.
- `report.md`: compiled from `run.jsonl` when the run ends, including failed or
  canceled runs. It lists the setup (commits, host and platform, toolchain,
  Python, device serials and firmware, RF placement, artifact hashes) and a
  results table, then a Watchdog section when any board was read, an Output
  files section when the run saved any (see [Checks read the output
  files](#checks-read-the-output-files)), then the full transcript. A board
  that reset itself puts its warning in the test's Reason column too, since
  the test itself passed.

Where a board the run used records an RF placement
([RF placement](../guide/plans-and-resources.md#rf-placement-where-a-board-sits-on-the-bench)), the manifest
carries it per invocation as `resource_placements`, each `test_started` record
repeats it as `rf`, and the report shows it in a setup row and beside the
resources of every result.

Each transcript line starts with the time elapsed since the run started
(`HH:MM:SS.mmm`); the report header gives the run's UTC start time, and
run.jsonl keeps absolute UTC times (`2026-10-24T00:18:21.535`, no offset
suffix). Each line then carries a tag: `[C]` command sent (shell-quoted), `[O]`
stdout, `[E]` stderr, `[X]` the execution result (tool, exit code, duration,
timeout or cancellation), `[S]` state-change step, `[I]` power isolation, `[M]`
harness record, and, for a line of a known target's serial shell, `[<alias> >]` sent
to it and `[<alias> <]` answered by it (see [Known
targets](run-records.md#known-targets)). Section markers are indented to line up with the
tagged text:
`+++ name +++` opens a step and `--- name EXIT: status ---` closes it; `===`
lines open and close the run, build, preflight, and each test:

```text
00:00:10.438     === PREFLIGHT ===
00:00:10.439     +++ Host version +++
00:00:10.439 [C] ubertooth-util -V
00:00:10.465 [X] ubertooth-util exit 0 in 0.025s; [O] Host tools version: 2026-11-DEV1+22 (libbtbb 2020-12-R1+79)
00:00:10.465     --- Host version EXIT: passed ---
00:00:10.466     === PREFLIGHT EXIT: passed ===
00:00:10.466     === TEST 002-Host CLI surface: Host CLI surface on Host_Mac ===
...
00:00:12.785     === RUN passed ===
```

A command that printed one line is two lines, with the response on the `[X]`
line. Longer output stamps each `[O]`/`[E]` line when it arrived, in arrival
order, before the `[X]` line. The live terminal stream on stderr uses the same
`[C]`/`[O]`/`[E]`/`[X]` command tags, with banners and verdicts of its own
(see [Reading a run as it goes](#reading-a-run-as-it-goes)).

`[X]` is the *execution* result and never a verdict: a command the harness
expects to exit nonzero passes, and one that exits 0 can still fail the
checks its output is read for, so a pass or a failure is only ever stated on
a line of its own (see [Reading a run as it goes](#reading-a-run-as-it-goes)).

Every response is kept, but run.jsonl holds at most the first 200 output
records or 64 KiB, and the last 50 records or 16 KiB, of each build,
preflight, test or flash command's output (bytes as written to run.jsonl, JSON
escapes included, so a 4-byte character counts 12). Past the first limits,
each of the command's streams goes to a file of its own in the run
directory as it arrives, from its first byte:
`output/<command id>.stdout.log` and `output/<command id>.stderr.log`. A file
holds what the command wrote on that stream, with nothing of the harness's
own added, so a reader sees what the tool printed, including a known target's
shell lines as the driver marked them (`[<alias> >]` and `[<alias> <]`). The
text is as the harness decoded it (UTF-8, any byte that is not valid UTF-8
replaced, as the transcript has always shown it), and the SHA-256 is the hash
of the file. A stream a command never wrote to leaves no file.
A flash command's files also name its test, for example
`output/003-Flash-reliability-flash-0001.stdout.log`, because flash command
IDs repeat in every flash test. The live stream then gets no more output records until the
command ends; then a `command_output_omitted` record names the files
(`output_files`, each with its path, stream, size, line count, and SHA-256),
summarizes what run.jsonl left out (`output_omitted`:
stdout and stderr line and character counts and the ten most common line
shapes, digit runs replaced by `N`, with their counts) and gives in `received`
when the left-out output ended; it is followed by the last
records (each with its arrival time in `received`, starting at a whole line),
so the GUI failure summary (for a failed test or build) and report.md show how
the command ended; report.md shows every output record run.jsonl holds. A
command recorded before these limits existed left no file and no
`command_output_omitted` record, so run.jsonl holds all of its output;
rendering report.md for such a run caps it the same way instead, to the first
200 records or 64 KiB and the last 50 records or 16 KiB, counted in characters
rendered rather than the bytes a record takes in run.jsonl, with one `[M]`
line for the gap (`<n> more characters of output are in run.jsonl`).
`command_finished` repeats `output_files` and `output_omitted`. The newest
output, such as the harness's own timeout, cancellation or `OUTPUT SINK ERROR`
line, is always among the last records. report.md shows the gap as `[M]`
lines between the first and last lines:

```text
00:07:00.120 [E] rx block timestamp 283640 * 100 nanoseconds
00:07:02.940 [M] full output in output/016-Raw-receive-dump-full-receive.stderr.log; left out of run.jsonl here: 6893 stderr lines (309933 characters)
00:07:02.940 [M]   6893 x [E] rx block timestamp N * N nanoseconds
00:07:02.944 [E] rx block timestamp 290481 * 100 nanoseconds
00:07:02.951 [X] ubertooth-dump exit 124 in 3.012s, stopped at its planned deadline
```

This keeps the per-block progress lines `ubertooth-dump` prints on stderr
(about 2500 a second), the per-block progress of each `ubertooth-dfu` in a
flash reliability run, compiler output, and long packet listings out of
run.jsonl: on a release run that cut run.jsonl from 48 MB to about 20 MB, and the dump stderr records
from 15 MB to 1 MB. Through
the runner service the `output/` files stay in the runner's run directory;
only run.jsonl records reach the controller, which asks that runner for a
page when the GUI opens one (`GET /api/runs/{run_id}/file` below).

### Reading a run as it goes

The live stream on stderr is written for somebody watching with `tail -F`, so
what a reader looks for is never a tag among the output
([#342](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/342)).
Each test opens with a banner, each of its cases with a lighter one inside it,
a verdict is stated in words of its own, and each state step says which phase
it belongs to and lines its values up:

```text
########################################################################
TEST: Radio setting transitions
########################################################################
[M] 001-Radio setting transitions on Host_Linux; Ubertooth=UT_0, guard Ubertooth 1=UT_1

------------------------------------------------------------------------
CASE: Guard Ubertooths baseline
------------------------------------------------------------------------

[C] ubertooth-util -U bbbb...bbbb -C
[O] Current frequency: 2441 MHz (Bluetooth channel 39)
[X] ubertooth-util exit 0 in 0.010s
[BASELINE PASS] guard Ubertooth 1 channel
    recorded: 2441
...
[STEP PASS] Verify channel after the mode stops
    expected: 2441
    observed: 2441
...
[CLEANUP PASS] Restore original channel
[STATE PASS] guard Ubertooth 1 channel unchanged
    expected: 2441
    observed: 2441
[PASS] Guard Ubertooths unchanged after channel-mode-stop-restore
[PASS] TEST Radio setting transitions
```

- `BASELINE` is the first read of a board or setting the case must leave
  alone, `STEP` one of the case's own actions, `CLEANUP` a restoration, and
  `STATE` a later read that has to find the baseline unchanged. The words are
  the labels of the stage each step is recorded under, so the record and the
  terminal say the same thing.
- A verdict is `PASS`, `FAIL`, `SKIP`, `CANCELED`, `N/A` (the host or the
  bench cannot run it, see [Host
  requirements](../guide/plans-and-resources.md#host-requirements)), or
  `BLOCKED` (see [Hardware a step is
  about](../guide/plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out)).
  A failed case, step, or test says why on the same line, once, since the
  recorded reason opens with the name that line has already said; the whole
  reason is in its own record either way.
- `[X]` stays the execution result and never a verdict, and output is shown
  as it arrives, packet lines included, within the per-command cap above:
  the captures the fast plan runs are brief, and the transcript is the
  evidence.

The run then closes with a block that identifies itself, so a tailed run says
what was tested without opening `report.md`: when it started in UTC, the ref
and exact commit of each source, the version line the host tools printed
(with the libbtbb they load), each device by registry alias and serial with
the firmware it was verified on, the totals, and the verdict.

```text
========================================================================
RUN SUMMARY
========================================================================
  run                20261105T091233Z-abcdef123456
  started (UTC)      2026-11-05T09:12:33.004 (ran 2m 18.996s)
  plan / suite       fast / fast
  host               Host_Linux (Linux 24.04 6.8.0 x86_64)
  Ubertooth          main = 9903b4251f... (git-9903b42)
  libbtbb            fork-pin at 7e1cdf0336... (git-7e1cdf0)
  host tools         Host tools version: libubertooth 1.07 (git-9903b42), libbtbb 1.3 (git-7e1cdf0)
  libbtbb in tools   git-7e1cdf0, the libbtbb this run resolved (--libbtbb-ref) (verified)
  device UT_0        Ubertooth, serial aaaa...aaaa: firmware git-9903b42 (verified)
  preflight          6/6 checks passed
  tests              12/12 passed
  also               1 skipped case
  report             runs/20261105T091233Z-abcdef123456/report.md
========================================================================
VERDICT: PASS
========================================================================
```

It is compiled from the run's own records, the ones `report.md` is compiled
from, so the two cannot disagree, and it goes to stderr with the rest of the
live stream: stdout still holds the summary JSON alone, so a caller parsing
that is unaffected. A run that stopped before its tests ran gets the same
block, from whatever it had recorded by then: a run that failed in its build
still names its sources and its verdict.

#### One way of saying a length of time

Every finished duration a person reads is written the same way: the largest
unit it reaches first, each smaller one after it, and seconds to the
millisecond — `0.004s`, `34.536s`, `10m 34.536s`, `1h 10m 34.536s`,
`2d 03h 10m 34.536s`. One formatter
([`ubertooth_harness/durations.py`](../../ubertooth_harness/durations.py))
produces all of them, so the terminal summary above, `report.md`, the GUI and
its history, the [completion email](../guide/notifications.md), and a
[release's evidence](release-evidence.md) cannot drift apart. The measured
number is kept beside the words wherever one is reported — `duration_s` with
`duration_hms`, `elapsed_s` with `elapsed_hms` — so a machine reads the
seconds and nothing it reads was rounded for a reader.

Two durations of the same run are measured on different spans, so read the
one you mean. `run_finished.duration_s` is the suite's own: from the moment
the sequencer began, which is after a `--build` run has already built its
sources, to the moment it wrote that record. The controller's `duration_s`
for a run or group ([`GET /api/runs/{run_id}`, `GET
/api/run-groups`](../guide/services.md#controller)) is the wall clock of the
whole job, from when the run was asked for to when it finished, so it
includes resolving the ref, the clone and build, flashing, and compiling the
report. A run that builds therefore reads longer in the GUI than in its own
record, by the length of its build.

`report.md`'s transcript says the same words on its own `[S]` line
(`[S] STATE FAIL: guard Ubertooth 1 channel unchanged (expected 2441,
observed 2426): expected channel=2441, observed 2426`), where one record is
one timestamped line, so a reader moving between the live stream and the
report reads one vocabulary.

### Checks read the output files

A check sees each command's complete output however long the command ran. It
reads the stream from the file a block at a time, so the harness's memory
does not grow with the stream: an hours-long capture and a two-line answer
cost the same. `CommandResult.stdout` and `.stderr` keep only the first and
last 128 KiB of each stream for the live view, with a line between them
saying how much is missing, so a check must not read them directly. A command
whose output stayed inside the run.jsonl limits has no file, and the same
checks read its complete in-memory copy.

Every check that reads output is recorded in an `output_checks` record: its
kind (an expected pattern, a forbidden one, an observed setting, the
command's own `SKIP:` line, or a check written as code), the pattern, the
files it read, whether it matched, and what it matched. The files a case's
tools wrote (captures, `-d` dumps, and any target device's shell traffic in
`<name>.serial.log`) are recorded the same way, in an `output_files` record
with each file's size and SHA-256.

Each file the run saved is listed in report.md's **Output files** section,
grouped by test (and under `build` or `preflight` for a file those phases
saved): its path, stream, size, line count, SHA-256, and the checks that read
it with their results. A check counts against the run when it recorded a
reason, when an expected pattern or an observed setting was not found, or
when a forbidden pattern was.


### Re-running a finished run's output checks

```sh
python -m ubertooth_harness validate runs/<run-id>
```

`validate` reads the run directory and nothing else: no hardware, no
network, and it writes nothing. It measures every recorded file again and
runs every recorded pattern check again over the same bytes, then reports

- a file that is missing, a different size, or a different SHA-256, so the
  run directory is no longer the one the run produced;
- a check that now reaches a different verdict, which is what to look at
  after a check has been fixed, or when auditing a result;
- the checks it cannot run again. A check written as code
  (`Step.output_check`, for example the spectrum sweep's sample checks) is
  one of those: the record holds its verdict, not its logic.

It exits 0 when nothing differs and 1 when something does. A run recorded
before output checks existed has nothing to re-run, which it says and which
is not a failure.

Run IDs look like `20261022T203045Z-a1b2c3d4e5f6`; `--run-id` supplies a
path-safe ID. Records use event schema version 1 (pre-release; no stable
consumers yet) and carry the run ID, UTC timestamp, sequence, status, and
bounded JSON details. The same records feed the live terminal stream on stderr
(see [Reading a run as it goes](#reading-a-run-as-it-goes); stdout holds only
the final summary JSON) and the web GUI; a live display
failure never interrupts hardware work.

SIGINT/SIGTERM (and SIGHUP) request cooperative cancellation. A firmware write
in progress is allowed to finish and be verified before the next one is
stopped; other commands are terminated as a process group.

Commands carry no credentials today. Before adding one that does, redact argv
and output where events are created; masking only in a display is not enough
because run.jsonl and report.md are persisted.

### Known targets

Some tests run against a device the harness drives itself, so the run knows
exactly what was on the air: an nRF52840 DK in one of its roles, or an ESP32
Classic peer. The rest listen to whatever traffic is there. A run records
which, once per test, as a `known_target` record, and `report.md` and the web
GUI both show what it says:

- the results table has a **Known target** column naming the board and its
  role, or `none`; it is empty for a test the host could not run, and for a
  run recorded before the harness kept this record;
- a **Known targets** section gives the whole line per test, for example
  `Run against known target: nRF52840 DK advertiser DK_Linux0
  (J-Link 000123456789, /dev/serial/by-id/usb-SEGGER_J-Link_000123456789-if00),
  requires firmware utdk-1.4.0, BLE address C0:00:00:00:00:01`, and a test
  with no known target says `No known target: the harness drove no device for
  this test`, which the GUI shortens to `no known target`;
- the transcript carries the same line as an `[M]` record where the test
  starts.

The serial port path is the one registered as `serial device`, or the stable
`/dev/serial/by-id` path derived from the probe's or bridge's serial on a
Linux host. The serial, port, and address are what the registry records, and
the test's own ready case checks them against the board. The firmware version
is the one that device's driver requires, not one read from the board when
the record is written: the board's own answer is in the transcript, because
every line of the conversation with its shell is recorded too
(`[<alias> >]` out, `[<alias> <]` back).

### Reports for several runs

```sh
python -m ubertooth_harness report runs/<mac-run-id> runs/<linux-run-id> \
  --title "Ubertooth 2026-11-R1 validation" --output validation.md
```

`report` writes one page covering several runs, for example the macOS and
Linux runs of a release. It summarizes each run's setup and results; with
`--redact-serials` it carries each run's transcript as well, so the option
means the whole report, masked. Without the option the page stays a summary,
because each run's own `report.md` already keeps its full transcript with its
serials in it.

Serial numbers are shown; `--redact-serials` replaces them with
`<serial>`: an Ubertooth's serial, a DK's J-Link serial, and an ESP32 peer's
bridge serial. A numeric serial is masked in each width the run writes it, as
registered and as the number the tools take, so a DK's J-Link serial does not
survive as the `--jlink 1050210121` the harness passes. A width short enough
to read as an ordinary number or word (the `123` of a serial registered as
`000123`, or a two-character bridge serial) is masked only where it stands
alone: a short number between characters that are not digits, a short word
between characters that are neither letters nor digits. So it cannot mask
part of a count or of a word, and it is still masked in a `by-id` path,
where `_`, `-` and `.` stand around it. What it leaves is a serial that
short written straight against a letter or a digit, as the macOS probe name
writes one (`/dev/cu.usbmodemab1`). A serial of one or two characters is
masked wherever it stands alone, so it takes a short word or an option
letter with it (`-a 0` reads `-<serial> 0`): the registry allows such a
serial and nothing tells it from an ordinary word, and a registered serial
is masked rather than published. A
serial port path keeps its shape with the serial inside it masked
(`/dev/serial/by-id/usb-SEGGER_J-Link_<serial>-if00`), whether the registry
held that path or the harness found it. Case is ignored throughout, since a
tool prints a serial in the case it likes. Any standalone 32-hex-digit token is
masked too, since that is the shape of an Ubertooth serial. Where output was
cut inside a serial, which either output cap can do, the piece the cut left
behind is masked as well: a run of 8 hex digits or more that a serial the run
names holds, or, for a board the run never names, a run of 24 to 31 digits
carrying both letters and digits, which is a serial and little else. Left to
read are a whole git SHA or digest, a narrower piece, and a long run of
digits or of letters alone (a counter, a bit string, a field of zeros) that
no serial the run names holds. The cost of the width rule is that a SHA an
output cap cut that short reads as `<serial>` too.

The lines an output cap left out of run.jsonl are counted by shape in the
`[M]` note (`3 x [E] <shape>`), and a shape is not the line: every run of
digits in it reads `N`, so a line that named a board would arrive with the
serial's digits gone and its hex letters and their positions intact, which
is enough to pick one board out of a small bench. So a shape a serial's own
shape appears in is masked as well, whole or as the 200-character shape
limit cut it.

The rule reads the shapes in that note and nothing else on the page, since
the note is the only place a report publishes one. In a shape, masked is the
whole shape of a serial the run names, at every occurrence and wherever it
sits, including written against a word of its own as a serial reaches a
line, and a piece of such a shape that carries an `N` and four or more of
the characters the serial itself held, since a piece is all the rule has to
go on there. Every other shape is left to read.

The costs are the width rule's, and fall inside that note. A short value
whose own shape a known serial's shape holds reads `<serial>` too, so a
`[M]` line can show two masks where one device was named. A piece a cut left
with no `N` in it is a run of the serial's own characters, left to read like
any piece of a serial narrower than 8 hex digits. And a serial with one
character of its own (31 digits and a letter) keeps its shape, because
masking `Na` would take that letter out of every other shape in the note.

What the option does not mask is anything that is not a serial the run
names, and a transcript holds more than those: the decimal serial of a
device the run never named, the registered Bluetooth addresses and LAPs of
the devices the harness drove, the addresses of whatever third-party devices
were on the air, and the paths the commands ran with, which carry a user
name and a home directory where the run built its tools. Read a report
before publishing it, as you would a run's own `report.md`.

Redaction is the `report` page only: a run's own `report.md` is always
written with its serials.

