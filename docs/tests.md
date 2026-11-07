# What each test does

One section per test in the harness catalog
([`ubertooth_harness/catalog.py`](../ubertooth_harness/catalog.py)), in catalog
order. Each heading is the exact catalog name, which is also the name a plan
writes, so a test without a section here is easy to spot. The last section,
`Out of scope: ...`, is the exception: it records what the harness has decided
not to test, so a missing catalog entry is not mistaken for an oversight.

Each section says what the test does, how it produces a known answer, what
hardware and arguments it needs, the commands it sends, what passes and fails,
and what it cannot tell you. Plan membership is deliberately absent: any test
can go in any plan, so which plans use a test belongs in
[`testplans/`](../testplans), not here.

Companions: [`COVERAGE.md`](../COVERAGE.md) is the same ground organized by CLI
option, and tells you which option still has no test;
[`hardware.md`](hardware.md) describes the devices these tests bind, and the
firmware they run; [`DESIGN.md`](../DESIGN.md) describes the harness itself.

[`ubertooth_harness/test_info.yaml`](../ubertooth_harness/test_info.yaml) is
the short form of this file, one entry per test under the same name, with the
same five prose parts as its fields (the commands below are this file's
alone). It is what the GUI shows beside a test name, so
a reader does not have to open this document to learn why a test exists, and
`/api/plans` serves it. The two are kept in step together: a change to what a
test checks updates both, in the same change (AGENTS.md).

## How to read a section

**What it does** is the one- or two-sentence summary.

**Theory of operation** is why the check is a real signal rather than a
tautology: what makes the answer knowable in advance.

**Hardware and arguments** lists the roles the test binds and the arguments
worth setting. It leaves out the three arguments every test takes (below).

**Commands** is every command form the test sends, naming the executable. A
command that is the same on every bench is written out; everything a run
substitutes is a named placeholder in angle brackets: `<serial>` is a board's
full 32-hex-digit serial, `<case dir>` the per-case directory under the run's
`captures/`, `<duration>` the plan's `duration:` argument in whole seconds,
and anything else is named where it appears. One form stands for every case
built from it, so `ubertooth-btle -U <serial> -n -A<channel> -q <case dir>/le.pcap`
is one line however many channels the profile runs. A harness driver the test
runs is written as the module it is, `esp32peer.py stream ... -- <tool ...>`,
with the tool it wraps; the interpreter in front of it is the harness's own
environment and is left out.

`<peer options>` is the one placeholder several sections share. It is the
eleven options an `esp32peer.py stream` session carries to bring the pair's
link up and stream across it: `--acceptor`, `--acceptor-address`, `--acceptor-alias`, `--initiator`,
`--initiator-address`, `--initiator-alias`, `--period`, `--payload`,
`--min-seconds`, `--min-writes`, and `--timeout`. "Capture known Classic
device" writes them out with values; every later section collapses them,
because what differs between those tests is the tool inside the session and
not the session. `esp32peer.py audio` holds the link without streaming
across it, so its session carries the eight that bring the link up and name
the boards and none of the three stream settings (`--payload`, `--period`,
`--min-writes`); the placeholder stands for that session's own eight where
it is written on an `audio` line.

The commands every transition case shares (the
idle stop, the reads it records, the restores, and the guard reads) are in
"What every test shares" below rather than repeated in each section.
`tests/test_tests_document.py` checks that every executable a test's cases
really run is named in its section or there.

**Expected outcome** says what passes, what fails, what is only reported, and
how many cases the test generates. Case counts are for one invocation with one
guard board attached; a test whose cases scale with the number of boards says
so.

**Known limits** is what a pass does not prove, and the open issues that
touch the test.

## What every test shares

**Arguments every test accepts.** `hosts:` (`each`, or a list of registered
host aliases) selects which hosts the step runs on. `platforms:` (`linux`,
`macos`) selects which of those can run it; a step without it runs on every
platform, and a host whose platform the step does not list is left out before
the run as not applicable. `max_watchdog_resets:` turns the firmware's
self-reset counter into a pass/fail measure for that step; without it a
self-reset is reported and judged by a person, not failed
([`watchdog.py`](../ubertooth_harness/watchdog.py)). `Flash reliability`,
`Exclusive Ubertooth topology`, `MAC polling soak`, and `Reset into invalid
application` must state their platforms. Every test that binds hardware also
accepts `rf:`, one placement requirement per role, and
`required hardware: true`, which says the hardware that step names is what
the step measures: a registry without it blocks the step and fails the run
rather than leaving it out ("Hardware a step is about", below).

**Hardware the bench has not got.** A step whose `hosts:` name a host this
registry does not have, or whose hardware roles no selected host can bind (no
ESP32 Classic peer, no DK, no default for the role, none declaring the role),
is left out before the run as not applicable, the same way a platform
mismatch is, unless the step carries `required hardware: true`, where it is
blocked instead. So a plan every host runs may still hold a step for hardware
only one bench has. What a host that is here cannot run is still a plan
error: a platform it does not have, and a board placed differently from what
the test needs, fail planning when every host the step names is here and none
can run it (with a host this registry has not got among them, the step is
left out instead).
`validate-plan --hardware <registry>` refuses the absence too, since there
the bench is named on purpose (guide/plans-and-resources.md, "Hardware a registry has not got").

**Hardware a step is about.** Three absences are not left out quietly,
because the missing hardware is the subject of the test: `alternate
Ubertooths` on a host with one registered Ubertooth, `exclusive Ubertooth`
where another registered board is on no power controller, and a step that
says so itself with `required hardware: true` for the hardware it binds (a
host with no peer registered, one peer where the test needs two, or a
registry without that host at all). `testplans/release.yaml` uses the last
of those for `Recover AFH map`. Such a step is recorded as
**blocked** before the run; every unrelated step still compiles and runs, and
the run finishes failed, so a gate nobody measured never reads as a pass.
`run-suite --dry-run` lists the blocked steps and exits 1. A suite whose only
step is blocked is still refused at planning, naming it, since there is
nothing left to run. A `power:` mapping
that is present but wrong (an unknown controller, a port the controller has
not got, a controller that cannot switch one) is a registry to correct, so it
is refused rather than recorded: when the registry loads where it can be
checked there, when the plan compiles where planning can tell, and for a
`driver` no controller is written for, when the run builds its controller,
before any port is switched.

**RF placement.** Every test with a hardware role also accepts `rf:`, one
requirement per role, which says what the role's placement on the bench must
be: `antenna`, `min_attenuation_db`, and `max_attenuation_db`
([`rf.py`](../ubertooth_harness/rf.py)). The placement itself is recorded by
hand in the registry and never measured, so this is how a plan asks for a
weak or distant link, or keeps a test off an attenuated board. A test whose
cases need an ordinary link declares that as its own default, so a plan does
not have to; the plan's `rf:` overrides the default role by role. The sections
below name the default where a test has one.

**Weak link.** `weak link:` is the shorthand for the usual case, one role at
an attenuation in dB: `weak link: {Ubertooth: 20}` asks for a board at exactly
20 dB with its antenna attached, and for an ordinary board for every other
role of the test, which is what makes the attenuation of a run's figures a
single number in the plan
([`weaklink.py`](../ubertooth_harness/weaklink.py)). It names exactly one
role, because a figure from a run where two boards were attenuated would
belong to neither attenuation; a plan that really wants two placements has
`rf:`, and a test takes one argument or the other, never both. Exactly that
attenuation, not at least: a figure measured behind 20 dB says nothing about a
bench with 10 dB or 40 dB in the path, so a bench carrying a different
attenuator is told what the check needs rather than run against numbers taken
elsewhere. The attenuation a run used is not read back from the argument: the
planner records the placement of each board it bound, and the report quotes
that beside the figures.

A **runnable** plan may only ask for a link a figure has been taken over:
either no attenuation at all, or exactly one attenuation the bench has been
measured at, today 20 dB and 40 dB (`weaklink.MEASURED_ATTENUATIONS_DB`).
A figure can also be refused for a third reason: the bench has been taken to
it and it settled nothing, which `weaklink.CHARACTERIZED_UNUSABLE_DB` records
with why, so the refusal quotes the reason rather than calling the figure
unmeasured to somebody about to fit an attenuator a previous session already
tried. 50 dB is the one such point today. A test's
floors were set over whatever link it last ran, so running it behind an
uncharacterized attenuator would judge it against figures taken elsewhere. A
bare `min_attenuation_db`, which only `rf:` can write, is refused there as
well, since which board it binds depends on what the bench holds.
`validate-plan` applies this to a whole plan and `run-suite` to the suite it
is about to run, so a suite is refused before a board is touched.
The weak-link entries a run has characterized live in the plan they belong
to, each carrying the figures it was judged against: `ble-weak-link` in
[`ble.yaml`](../testplans/ble.yaml), `classic-weak-link` in
[`classic.yaml`](../testplans/classic.yaml), and `range-weak-link` in
[`range.yaml`](../testplans/range.yaml). None of them changed a threshold:
each is the committed test pointed at a weak-link receiver.
[`testplans/weak-link.yaml`](../testplans/weak-link.yaml) holds what is still
waiting, split into what needs a second nRF52840 DK and what needs a
measurement or an implementation.

**What attenuating a receiver is for.** On an ordinary bench every board is a
few tens of centimeters from every transmitter, so each one hears a signal far
stronger than anything in use: packets arrive clean, error flags never fire,
and a receiver reading 20 dB too hot can still pass every check. Putting one
receiver behind an inline attenuator makes its link marginal but still
decodable, which is the only regime where several of these tests measure
anything. What it exercises is the behavior under a weaker signal:
access-code error tolerance (`ubertooth-rx -e`), the HEC and CRC flags in a
capture record, UAP and clock recovery from fewer good packets, whether a
follow keeps up, how sharp an AFH map recovery is, and whether the board stays
reliable when it is not being overloaded.

What it is **not** is RF calibration or a receiver-sensitivity
certification. The attenuation is a number a person wrote in the registry,
the path loss at a meter is not measured at all, and nothing here is
traceable to a dBm figure; the attenuator is a way to move a test into a
different regime, not an instrument. A figure from such a run says what the
tool did over *that* link on *this* bench, which is why a plan pins one
attenuation rather than a range and why `measured()` refuses an attenuation
nobody has characterized.

The points, and what each buys:

| link | what it is for | what is measured there |
|---|---|---|
| clear | the baseline every test already runs over | everything, with the margin a short bench link gives |
| 20 dB | marginal but comfortable: the point most weak-link entries use | the range test's reply level, a DK advertiser capture, a targeted Classic follow, exact AFH map recovery |
| 40 dB | 20 dB more attenuation, still with margin: the attenuated-device sweep read it about 14 dB below the 20 dB board from the Classic peer (`weaklink.py`) | a DK advertiser capture, Classic decode with the clock acquired, `-e 2` honored; AFH recovery starts losing edge channels |
| 30 dB | asked for, uncharacterized | nothing yet: it is the point that would turn 20 against 40 into a curve (`weaklink.PLANNED_ATTENUATIONS_DB`) |
| 50 dB | past the end of the useful range | measured and found unusable: 62 records across six windows with no clock recovered in any, so it removes packets instead of damaging them. It is in `weaklink.CHARACTERIZED_UNUSABLE_DB` rather than the measured list, and a plan that asks for it is refused with that reason |

So more attenuation is not automatically better, and a point can be
characterized and still be unusable. `weaklink.py` holds both lists —
what the bench has measured, per test and per role, and what it has found
unusable — and a runnable plan may only ask for the first; read it rather
than assuming a figure further down the scale is available. The clearest
case of that is the error flags of `Classic capture record fields`, which a
weaker link looked like the way to earn and was not: the unattenuated board
had the highest rate of HEC failures of four receivers, 20 dB failed none,
and 50 dB recovered no clock at all.

The limits of all of this are worth saying plainly. Only the attenuation is
pinned, never the distance, because a placement requirement has no distance
key: a board at 20 dB much further away is still selected and may behave
differently. The figures belong to a source as well as a link — the
attenuated-device sweep separated the two receivers by about 14 dB with a
Classic peer and by 1 dB with a DK — so a figure taken with one transmitter
does not carry to another. And a weak-link entry is the committed test
pointed at the attenuated board: none of them changed a threshold, so what a
run there adds is which board received, not a tighter bound.

**Coverage profiles.** A test with a `coverage:` argument takes `smoke` or
`comprehensive`. Smoke keeps one case per behavior; comprehensive adds the
combinations, the edge values, and the cases that pin specific fork fixes, so
a comprehensive run fails on a build from before those fixes. Both are the
same code paths; only the case list differs.

**Durations.** A `duration:` argument is how long each bounded run inside the
test lasts. Two shapes appear. A mode that runs until it is told to stop is
started and stopped by the harness at the deadline, so reaching the deadline is
the expected outcome and an early exit fails the case. A tool that ends itself
after `-t N` must exit at about N seconds: more than 5 s late counts as hung,
and more than 0.5 s early counts as a premature exit. Separately, the catalog
carries a calibrated wall-time estimate per test and profile, used for the
GUI countdown; it is never a timeout.

**Transition cases.** Most board tests are built from state-changing cases:
an ordered list of exact commands, then a mandatory cleanup. A case reads the
settings it is about to disturb, changes them, checks what it expected to
change, and the cleanup writes the originals back and reads them to confirm.
Cleanup runs after a failure or a cancellation too, and a cleanup failure
always fails the case. The settings read and restored this way are the channel
(`-C`/`-c`), the PA level (`-a`), the squelch (`-z`), all three LEDs (`-d`,
`-l`), and, for BLE cases, the CRC verify flag (`ubertooth-btle -k`) and the
access address (`-a`).

These are the commands such a case sends, and no section below repeats
them. Which of them a given case sends follows what it disturbs: the
settings it records and restores, the CRC flag if it is a BLE case, and the
watchdog record when a read falls due between cases. `<serial>` is the
selected board's full serial:

```text
ubertooth-util -U <serial> -S                 # return to idle, first and last
ubertooth-util -U <serial> -C                 # read the channel, in MHz
ubertooth-util -U <serial> -a                 # read the PA level
ubertooth-util -U <serial> -z                 # read the squelch
ubertooth-util -U <serial> -d                 # read all three LEDs
ubertooth-util -U <serial> -l                 # read the USR LED alone
ubertooth-util -U <serial> -s                 # confirm the registered serial
ubertooth-btle  -U <serial> -k                # read the CRC verify flag
ubertooth-util -U <serial> -c<MHz>            # restore the channel
ubertooth-util -U <serial> -C<index>          # or by channel index
ubertooth-util -U <serial> -a<level>          # restore the PA level
ubertooth-util -U <serial> -z<dBm>            # restore the squelch
ubertooth-util -U <serial> -d<state> -l<state>  # restore the LEDs
ubertooth-btle  -U <serial> -k<0|1>           # restore the CRC verify flag
ubertooth-util -U <serial> -w                 # read the firmware watchdog record
```

A case that sets a value writes it as `-c2426`, attached, and a case that is
about the next-word form says so where it does.

**Mandatory preflight.** Before the first test, every run runs checks the
harness generates from the resources it bound, not from the plan. They are
reported under their own names, and no catalog test carries them:

```text
ubertooth-util -V                              # "Host version", once per host
ubertooth-util -H                              # the same, on a fork before the one
                                               # -V answer (ubertooth#813)
ubertooth-util -U <serial> -v                  # "Firmware version", per board
ubertooth-util -U <serial> -V                  # "Firmware compile info", per board
ubertooth-util -U <serial> -s                  # "Device serial", per board
ubertooth-util -U <serial> -v                  # "Guard firmware API", guard-only board
ubertooth-util -U <serial> -B                  # "Bootloader identity", per board
nrfdk.py identify --jlink <jlink serial> [--port <serial device>]
                                               # "nRF52840 DK probe", per DK
esp32peer.py identify --device <serial device> --address <BD_ADDR>
                                               # "ESP32 Classic peer", per peer
```

Each device driver is given the port the registry names for that device, and
derives it from the probe's or bridge's serial where the registry names none.
`nrfdk.py` takes it as `--port`, which is therefore in its command only where
a registry names one; `esp32peer.py` takes it as `--device`, which is in
every one of its commands either way.

The host version line must name the resolved Ubertooth commit and the libbtbb
the tools are linked against; `-v` and `-V` must report the resolved firmware
revision, and `-s` the serial the registry holds. A board whose firmware a
test establishes itself gets the serial read only, a guard-only board gets the
serial read and the API read, and the bootloader read is governed by
`--bootloader` ("Bootloader identity", below). `Host version` and `Ubertooth
identity` are the catalog tests that ask the same questions as plan steps; the
five standalone catalog names that used to shadow the per-board reads
(`Firmware version`, `Firmware compile info`, `Device serial`, `Part ID`,
`Board ID`) were removed, because each was a case of `Ubertooth identity` with
the same command and the same check
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).

**Guard Ubertooths.** A guarded test binds every other registered Ubertooth on
the host as a `guard Ubertooth N` resource and reads each one before the test
and after every case, with queries only and one command per read:

```text
ubertooth-util -U <serial> -v                 # the firmware image and revision
ubertooth-util -U <serial> -s                 # the serial the board reports
ubertooth-util -U <serial> -B                 # the bootloader under it
ubertooth-util -U <serial> -C                 # the channel, in MHz
ubertooth-util -U <serial> -a                 # the PA level
ubertooth-util -U <serial> -z                 # the squelch
ubertooth-util -U <serial> -d                 # all three LEDs
ubertooth-btle  -U <serial> -k                # the CRC verify flag, last
```

A ninth read asked `-V` for the image's build string, whose build date caught
a reflash of the same revision; fork #1233 took that string out of the
answer, so nothing reports it and the read has gone
([#358](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/358)).

Every read must match the baseline taken before the
first case; one that differs fails the test. This is what catches a regression
that reaches a board the command did not select. The `ubertooth-btle -k` read
is refused, which fails the check, unless that board's firmware API is the
host tools' own: `ubertooth-btle`'s own API check may refuse any other, newer
as well as older, and stop the board as it goes — whether it does is how the
host tools were built and whether `UBERTOOTH_ALLOW_API_MISMATCH` is set,
which the run does not set and cannot predict, so it does not send the
read. Preflight refuses
a guard-only board whose API differs, so that is caught before any test runs.

Every guard gets every read, whatever firmware image it carries, and the CRC
read is the one that may not reach the board: `ubertooth-btle` is built for
one firmware API and may refuse any other, stopping the board as it goes, so
that read is refused unless the API matched, and the refusal fails the check
rather than counting as a reading.

Two answers count as a reading this host could not make: a request the
board's image has not got, which it refuses by stalling, and a board carrying
another of this tree's images, which `ubertooth-btle` refuses as it opens it.
Either is recorded as the reading (`unsupported`) and compared with the rest,
so a board that starts answering a read it refused before, or refuses one it
answered, fails the check. The alternate `btbr` image, for instance, has no
channel or squelch request and no tool will read its CRC setting, and it is
still guarded by its identity, image and revision, bootloader, LEDs,
and, on an Ubertooth One, its transmit power level. The LED read is the
exception to the tolerance: `-d` asks for all three LEDs in one command, and
every image that answers these requests at all answers those three, so a
stall there fails the read rather than being recorded — otherwise a board that
answered two of the three would leave all three unguarded. Two things the
readings themselves have to satisfy:
a guard running `bluetooth_rxtx`, or the receive-only `bluetooth_rx` build of
it, must answer the radio settings that build implements, since a board that
cannot is not holding them; and any guarded test is turned away when a guard
answered none of `-v`, `-s`, or `-B`, because nothing about that board
could be compared afterwards — a wedged board would read that way too, though
the strict LED read fails it first.

A guard is only ever read: every command it is sent is a query, it is never
reflashed to make it readable, and the one command that could write to it —
`ubertooth-btle`, which may stop a board whose firmware API is not the host
tools' own — is refused rather than sent.

**Files.** Files a case writes (captures, dumps, images, FIFOs) go in a
per-case directory under the run's `captures/`. Checker scripts read them and
print summaries only, so no capture bytes reach the run record.

**No-board tests.** The tests that need no Ubertooth run each tool as
`env UBERTOOTH_TEST_NO_DEVICES=1 <tool> ...`, so a command that got past its
own argument checks finds no board instead of opening one. A refusal has to
come before that, so the switch's "no board is selected" note is a forbidden
pattern in every refusal case. These tests also work on a host with boards
attached, and they are what CI can run against the tools it builds.

**Tests with no implementation.** Several catalog entries are names,
descriptions, and argument contracts with no cases yet. `run-suite` refuses a
suite that names one, and `validate-plan` refuses a plan that does, unless the
plan declares `runnable: false`. Their sections say so, and say "not
established" where the intended method is not written down anywhere.

**Case failures.** A test marked to continue on case failure runs its
remaining cases after one fails. The run itself continues past a failed test
as long as the board is in a known state: every failed case verified restored
and every guard read matching. It stops when that is not true.

---

## Host version

**What it does.** Runs `ubertooth-util -V` and requires the host tools version
line.

**Theory of operation.** `ubertooth_print_host_version()` prints the host
tools' own version as the first line of every tool's `-V` answer. The check is
anchored to that label, so the firmware line printed under it cannot stand in
for it. The same line is what preflight compares against the resolved host
sources, which is how a run proves the tools under test are the ones the
manifest names.

A build names the release it came from, with libbtbb's beside it
([ubertooth#1230](https://github.com/SqueakyFloors/ubertooth/issues/1230),
`ubertooth#1232`):

```text
Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)
```

That is the one layout accepted. Before that change a build named the two
library interface versions, each with the commit it was built from, and
printed the libbtbb it loaded on a line of its own; the harness held a run of
such a commit to that older line until
[#358](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/358),
where the maintainer decided that current-release validation requires the
release-name answer and keeps no requirement that output carry a commit hash
or a build stamp. A run of a commit from before the change is now refused in
preflight, by name, rather than checked against output the release under test
does not print.

**Hardware and arguments.** No board role. `hosts:` only. The command runs
with a 20 s timeout.

**Commands.**

```text
ubertooth-util -V
```

**Expected outcome.** One case. The line above must appear.
Exit 0 and exit 1 both pass: the command goes on to read every attached board
and exits 1 if any cannot be read, which may be a board this run does not use.

**Known limits.** It checks the shape of the line, not that the versions are
the ones you wanted; preflight does that. A board in use by another program
makes the command exit 1 without failing this test.

## Help smoke

**What it does.** Runs `ubertooth-util -h`.

**Theory of operation.** A binary whose shared libraries do not resolve, or
whose option parser aborts, cannot print its usage text and exit 0. This is
the cheapest proof that the build under test produced a runnable tool.

**Hardware and arguments.** No board role. `hosts:` only.

**Commands.**

```text
ubertooth-util -h
```

**Expected outcome.** One case: exit 0. No output pattern is required.

**Known limits.** It proves almost nothing beyond "the binary runs".
`Host CLI surface` is the same idea applied to every tool, with the tool name
required in the output, so this entry adds no coverage to a suite that holds
that one. It is kept as the cheapest step a plan can hold, half a second for
one command with no board and no pattern, which is what the five removed
identity entries were not: each of those had no implementation and no plan
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).

## Host CLI surface

**What it does.** Runs `-h` and `-V` on every C host tool the fork builds.
(A `--with-python` build installs `ubertooth-specan-ui` and `ubertooth-btbr`
as well; those are `Python tools`' and `Classic btbr console`'s.)

**Theory of operation.** Each tool's usage text names the tool, so requiring
the tool's own name in the output distinguishes a working parser from a
binary that printed something else. `-h` exits 0 on every tool in this fork,
which was not always so: `ubertooth-btle` and then `ubertooth-dfu`,
`ubertooth-scan`, and `ubertooth-follow` used to exit 1. Pinning exit 0 per
tool keeps that fixed.

`-V` is the other command every tool answers. Each one calls the same
libubertooth function, `ubertooth_print_versions()`, and ends the command
there, so the host tools version line is the right answer from all thirteen
and a tool that stopped printing it is a regression in that tool alone. Four
of the thirteen used to be asked. Read at Ubertooth `2026-11-DEV1`, every one
of the thirteen sources carries `-V` in its `getopt` string and a `case 'V'`
that sets `do_version`, and the eleven tools a macOS build produces answer
it identically there: the host version line, the libbtbb path, and
`Firmware version: no Ubertooth attached` with exit 0 on a host with no board
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).
Both cases of a tool come from one list, the C host tools the build
installs, so a tool the fork adds is asked both questions with the build
rather than when somebody remembers a second list.

**Hardware and arguments.** No board role. `hosts:` only. `-V` reads whatever
boards are attached, which may include boards this run does not use, so a
board another program holds makes the command exit 1 without failing the
case. The two BlueZ-only tools declare that they need BlueZ for both of their
cases, so on a host without it (every macOS host) they are left out before
the run as not applicable, rather than failing or being skipped at run time.
The `-V` cases run with a 20 s timeout.

**Commands.** Two per tool, one list of tools, for `ubertooth-util`,
`-debug`, `-specan`, `-rx`,
`-tx`, `-dump`, `-btle`, `-dfu`, `-ego`, `-afh` and `-ducky`, and for
`ubertooth-scan` and `-follow` where BlueZ is present:

```text
ubertooth-util   -h
ubertooth-debug  -h
ubertooth-specan -h
ubertooth-rx     -h
ubertooth-tx     -h
ubertooth-dump   -h
ubertooth-btle   -h
ubertooth-dfu    -h
ubertooth-ego    -h
ubertooth-afh    -h
ubertooth-ducky  -h
ubertooth-scan   -h          # BlueZ builds
ubertooth-follow -h          # BlueZ builds
ubertooth-util   -V
ubertooth-debug  -V
ubertooth-specan -V
ubertooth-rx     -V
ubertooth-tx     -V
ubertooth-dump   -V
ubertooth-btle   -V
ubertooth-dfu    -V
ubertooth-ego    -V
ubertooth-afh    -V
ubertooth-ducky  -V
ubertooth-scan   -V          # BlueZ builds
ubertooth-follow -V          # BlueZ builds
```

**Expected outcome.** 26 cases on a Linux host, 22 on macOS: every `-h` exits
0 with its own tool's name in the output, and every `-V` prints
`Host tools version: libubertooth <v> (<id>), libbtbb <v> (<id>)`, with exit
0 or 1. A case failure does not stop the others.

**Known limits.** It checks that help works and that the version line is
present, not what any option does, and not that the versions are the ones you
wanted: `Host version` and preflight compare them with the resolved sources.
The firmware lines `-V` prints under that line are `Ubertooth identity`'s, not
checked here, because this test binds no board and cannot know which board
answered.

## Ubertooth count

**What it does.** Requires that `ubertooth-util -N` prints exactly the number
of registered Ubertooths on the host.

**Theory of operation.** The harness knows how many boards it bound: the
selected one plus every other registered board, bound as guards. So the
expected count is known before the command runs, which makes a wrong count a
real finding rather than an observation. A count that is too low means a board
did not enumerate; too high means an unregistered board is attached, which
would also break the no-selector and `-U all` behavior other tests rely on.

**Hardware and arguments.** One `Ubertooth`. Guarded.

**Commands.**

```text
ubertooth-util -N
```

No `-U`: the count is of every attached board, so naming one would defeat it.

**Expected outcome.** One case: the output is the expected number on a line of
its own.

**Known limits.** It depends on the registry being right. An unregistered
board attached to the host fails this test, correctly, but the message says
only that the count differed.

## Bootloader identity

**What it does.** Nothing as a plan entry: a catalog entry for the bootloader
read, which mandatory preflight does instead — for the boards whose
firmware a run requires and then verifies, which leaves out a guard-only
board and a board whose test establishes its own firmware.

**Theory of operation.** A board's bootloader is the first 16 KiB of its
flash, and `ubertooth-dfu` runs from there, so an ordinary firmware write
cannot replace it: a board keeps the bootloader it came with until somebody
installs another one over a programmer on its in-system programming header.
`ubertooth-util -B` is what reports which one that is, in one line:

```text
Bootloader: 2.00 2026-11-DEV1+22, CRC32:0x9d2fe1b4
Bootloader: 2.00 git-1a2b3c4, CRC32:0x9d2fe1b4
Bootloader: no version block (factory or earlier, treated as 1.x), CRC32:0x5f0a73c1
```

The first two forms come from a 48-byte version block the fork's bootloader
carries at a fixed offset; the last is every board as shipped. The CRC-32 is
of the whole region, worked out on the board, and is on the line either way.

The build string in the block is the version the tree reported when the
bootloader was built: the release it came from since
[ubertooth#1230](https://github.com/SqueakyFloors/ubertooth/issues/1230)
(`ubertooth#1232`), and a commit hash before that, which is the second form and is what
a board flashed from an older commit carries. The harness works out which one
a build at the pin reports the way the makefile works it out (`release.py`)
and compares that: a release name for equality, since there is no shorter form
of one to accept, and a hash as an abbreviation of the commit, since two
checkouts need not abbreviate to the same length.

Preflight reads it for every board whose firmware the run requires, after the
firmware checks, since firmware older than the read refuses it. What the run
then requires is `--bootloader`:

- `record`, the default, asks only that the board answered, and records what
  it said. A bootloader write is physical and cannot be undone over USB, so
  nothing about one is implicit in a bench or release run.
- `check` compares the answer with a bootloader built from the run's own
  Ubertooth source and fails the run when a board is on an older or different
  one. All three of the version, the build string, and the region hash must
  agree: two different 2.00 images are two different bootloaders, and the hash
  is what tells them apart.
- `update` installs that bootloader on a stale board whose registry pairs it
  with a debug programmer, then reads `-B` again and requires exactly the
  identity that was written, and the application still reporting the run's
  revision. A stale board with no programmer fails and the reason names the
  missing capability.

"Current" is not the resolved commit's bootloader but the newest commit,
reachable from it, that touched the sources the bootloader image is built
from. The build string in the block is the whole repository's revision at
build time, so an artifact pinned to the resolved commit would differ on every
unrelated commit and ask for a physical write to every board on most days, for
no change in behavior. The harness reads the bootloader's own makefile and
refuses a pin whose paths no longer cover a source the image is built from.

It is the one identity read left in the catalog as a name with no cases, and
it stays because no case could do it: a case cannot tell whether the commit
under test has `-B` at all, and against one that has not it would fail a
board that is perfectly set up. The five other identity names
(`Firmware version`, `Firmware compile info`, `Device serial`, `Part ID`,
`Board ID`) had no such reason — each was a case of `Ubertooth identity` with
the same command and the same check — so they were removed rather than
implemented twice
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).

**Hardware and arguments.** One `Ubertooth` per check, selected by full
serial. `update` also needs a `Debug programmer` paired with the board, with a
`serial device`, plus `lpc21isp` and `arm-none-eabi-gcc` on the host; see
"Installing a bootloader through it" in [`hardware.md`](hardware.md).

**Commands.** None as a plan entry. As preflight, one read per board, and for
`update` the write and the reads around it:

```text
ubertooth-util -U <serial> -B                     # read for every setting
lpc21isp -control <bootloader.hex> <serial device> 230400 4000
ubertooth-util -U <serial> -v                     # the application came back
```

**Expected outcome.** No implementation as a plan entry; only a
`runnable: false` plan may name it. As a preflight check: one `-B` per
selected board whose firmware the run requires, recorded in `run.jsonl` as a
`bootloader_identity` record and
shown in the report's Bootloader section and in the GUI's board list.
`record` passes on any answer, including a factory bootloader; `check` and
`update` pass only on the identity the artifact names.

**Known limits.** A run against a fork commit from before `-B` existed has no
check at all, and records that it had none. The region hash is worked out from
the artifact, which is the image padded with the 0xff of erased flash and the
vector checksum filled in; a programmer that wrote the region any other way
would be caught by the read-back rather than predicted, which is what the
first hardware test of the update path confirms
([#298](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/298)).
There is no one factory hash to compare against: shipped boards each report a
different one, so a board's own hash is worth recording and another board's
says nothing about it.

## Ubertooth identity

**What it does.** Reads the identity reports from one board, each on its own
command line, and then the four that compose in one command.

**Theory of operation.** The registry already knows the board's serial, so the
`-s` read has a known right answer and proves the command reached the board the
plan selected rather than whichever board USB offered first. The other reads
are checked against the shape the tools print. The composed `-vspb` case then
requires all four reports from one command: a parser that drops a report when
several are asked for passes the single-option cases and fails this one. `-V`
is not in that composition. It is the version command: since fork `f898fe5`
it prints the host tools' version and the board's firmware version,
then ends the command, so its case asks for the whole of that answer on its
own.

**Hardware and arguments.** One `Ubertooth`, selected by full serial. Not
guarded (every command is read-only and selects by serial).

**Commands.**

```text
ubertooth-util -U <serial> -v        # firmware image, revision and API
ubertooth-util -U <serial> -V        # the whole version answer
ubertooth-util -U <serial> -s        # the registered serial
ubertooth-util -U <serial> -p        # microcontroller part ID
ubertooth-util -U <serial> -b        # Ubertooth board ID
ubertooth-util -U <serial> -vspb     # the four that compose, in one command
```

Those five reads are the whole of the harness's identity readback: the
catalog named each of them as a test of its own once, with the same command
and the same check, and the names were removed rather than run twice
([#347](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/347)).

**Expected outcome.** Six cases: firmware version, the whole `-V` answer (its
lines in the fork's order: the host version, then the firmware version),
device serial, part ID, board ID, and the four-query
readback composition. A case failure does not stop the others. The `-V` case
keeps its `firmware-compile-info` id and its `Firmware compile info` name,
although it now covers more than that line: the id is what a run's records
and the learned durations are kept under, and the name is the one preflight's
per-board `-V` check already uses for the same read.

**Known limits.** These are readbacks, so a pass says the board answers and
identifies itself, not that any radio function works.

## Debug register read

**What it does.** Reads one CC2400 register with
`ubertooth-debug -U <serial> -r 0x<reg>`.

**Theory of operation.** Reading a radio register goes through the board's own
register-read path, so an answer in the documented form
(`%NAME = 0xVVVV`, or `%rNN` for a register with no name) shows that path
works end to end. The register number is a plan argument, so one entry covers
any register.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
`register:` takes an integer or `0x00`-`0xff` and defaults to 0 (MAIN).

**Commands.**

```text
ubertooth-debug -U <serial> -r 0x<register>
```

**Expected outcome.** One case: exit 0 with one `%<name> = 0x<four hex>` line.

**Known limits.** It does not check the value, only that a register read
answers in the documented form. `Debug registers` covers lists, ranges,
verbosity, and the exit status.

## Ubertooth util compositions

**What it does.** Runs read-only `ubertooth-util` queries together in one
command and requires every report asked for.

**Theory of operation.** Each query has a distinct output line, so a command
that asks for two must print both. A parser bug that lets a later option
replace an earlier one, or that returns after the first report, shows up as a
missing line. `-l` and `-d` both print a USR LED line, so that pair requires
two of them. `-V` is left out of the compositions and gets two cases of its
own that go the other way: since fork `f898fe5` it is the version command and
ends the command, so `-V -s` and `-s -V` must each print the whole version
answer (its lines, in order, with the libbtbb path line among them on a
commit that prints one) and no serial line. The fork sets the flags
while it reads the command line and acts only once the whole line has parsed,
so both orders must behave alike. Asking for `-V` beside the others would
hide every query behind it.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

**Commands.** Every command is `ubertooth-util -U <serial>` with the queries
under test:

```text
ubertooth-util -U <serial> -V -s                     # and -s -V: the version answer, no serial line
ubertooth-util -U <serial> -v -s -p -b -a -C -z -l -d  # every composable report at once
ubertooth-util -U <serial> -<query> -<query>          # each compatible pair, comprehensive
ubertooth-util -U 99 -U <serial> -s                   # a repeated -U, refused
```

The nine composable queries are `-v`, `-s`, `-p`, `-b`, `-a`, `-C`, `-z`,
`-l` and `-d`; `-V` ends the command, so it is only ever paired with one
other option to show that the other one prints nothing.

**Expected outcome.** Smoke runs 3 cases: `-V -s` and `-s -V`, each
answering the version alone, and all nine composable read-only queries in one
command. Comprehensive runs 40: the same three, every compatible pair of the
nine queries, and a refusal case, since two `-U` options name two boards for
one command and are refused (`error: option '-U' given more than once`,
exit 1) rather than the last one winning. A case failure does not stop the
others.

**Known limits.** Read-only only: nothing here writes a setting, so it says
nothing about set-and-read ordering, which `Radio setting transitions` covers.

## Argument validation

**What it does.** Gives each tool an argument its own parser refuses, and
requires that the board is unchanged afterward.

**Theory of operation.** A refusal is only safe if it happens before anything
is written. So each case reads the CRC flag, channel, squelch, LEDs, and PA
level first, runs the refused command, then reads all five back and requires
the recorded values. The command must exit 1 with the tool's own error text,
and must print none of the mode-start lines, so an accepted argument that
started a mode is caught even where the exit status would not show it. The
arguments and messages are taken from the fork's own sources, so the expected
text is the tool's, not the harness's guess.

Each command names the board it is for with `-U <serial>`, except the two
`-U all` cases, which bring that selector themselves: no option may be given
a value twice, so a second `-U` would be refused for the repeat and the case
could never reach the behavior it is named for. Those two are refused before
any board is opened, so leaving the selector out takes nothing away.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded, which is what catches the two `-U all` cases if a regression let them
reach every attached board. `coverage:`.

**Commands.** One command per rejected argument, each selected by serial. One
line per option here stands for the values the two profiles give it:

```text
ubertooth-util   -U <serial> -c2401        # and -c 2481, -c2481, -c2440x, -cabc
ubertooth-util   -U <serial> -C79          # and -C-1, -Cabc
ubertooth-util   -U <serial> -a8           # and -a 8, -aa
ubertooth-util   -U <serial> -z128         # and -z -129, -z-129, -zabc
ubertooth-util   -U <serial> -q19          # and -q0, -q121, -q183, -qabc
ubertooth-util   -U <serial> -l2           # and -lx, -d2, -dx
ubertooth-util   -U <serial> -z -I         # and -z -q, -z -x
ubertooth-util   -U all -a5               # and -U all -x: a setter and a
                                          # mode, with no serial selector
ubertooth-btle   -U <serial> -x33
ubertooth-btle   -U <serial> -A36          # and -A40
ubertooth-btle   -U <serial> -k 2          # and -k stray
ubertooth-btle   -U <serial> -a zzzz       # and -a -
ubertooth-btle   -U <serial> -tnot-a-mac
ubertooth-ego    -U <serial> -l0           # and -l49
ubertooth-ducky  -U <serial> -A36          # and -A40
ubertooth-ducky  -U <serial> -anot-an-address
ubertooth-rx     -U <serial> -u 12         # -u without -l
ubertooth-rx     -U <serial> -z -l 123456  # -z with -l
ubertooth-specan -U <serial> -l 2401
ubertooth-specan -U <serial> -u 2481       # and -l 2480 -u 2402
```

and a stray word on every built tool, which is the fork's one argument rule:

```text
ubertooth-afh    -U <serial> stray
ubertooth-btle   -U <serial> stray
ubertooth-debug  -U <serial> -r 0x00 stray
ubertooth-dfu    -U <serial> stray
ubertooth-ducky  -U <serial> stray
ubertooth-dump   -U <serial> stray
ubertooth-ego    -U <serial> stray
ubertooth-rx     -U <serial> stray
ubertooth-specan -U <serial> -l 2402 stray
ubertooth-tx     -U <serial> stray
ubertooth-util   -U <serial> -a 7x         # a next word that is not a value
ubertooth-util   -U <serial> -c2470 stray  # and -x stray
ubertooth-util   -U <serial> -c2470 -      # a bare dash, and -z -
ubertooth-util   -U <serial> -v -- -c2470  # a word after --
ubertooth-btle   -U <serial> -             # a bare dash
ubertooth-util   -U <serial> -v            # the firmware, read after a refusal
```

Every case then reads the board back with the shared setting reads, so a
refusal that changed something is caught.

**Expected outcome.** Smoke runs 11 cases, one per tool family: a frequency
below the band, a bad BLE error allowance, a bad E-GO length, a bad ducky
channel index, `ubertooth-rx -u` without `-l`, a specan lower bound below the
band, and a stray word on `afh`, `debug`, `dfu`, `dump`, and `tx`.
Comprehensive runs 64, covering out-of-range and non-numeric values for every
`ubertooth-util` setting, the `-U all` restriction to read-only queries, `-z`
with a mode, the BLE and ducky address and channel checks, the specan range
rules, and the one-argument rule in its forms: a stray word, a bare `-`, a
word after `--`, and a next word that is not a value for the option before it.
A case failure does not stop the others.

**Known limits.** Commands that would transmit if a regression accepted them
(`btle -s`, `-i`, `-I`, `ducky -q`, `-b`, `util -t`, `-n`, `-e`) are left out
on purpose, so the refusals of those are not covered here. `ubertooth-follow`
and `ubertooth-scan` stray words live in `Refused command lines` instead,
because those tools are built only with BlueZ and their cases need no board.
A pass shows the five readable settings are unchanged, not that nothing else
on the board moved.

## Radio setting transitions

**What it does.** Sets a radio setting, reads it back, runs a mode and stops
it, and requires the firmware's documented behavior across the stop.

**Theory of operation.** The firmware keeps settings between idle commands but
runs an idle reset when an active mode stops: channel back to 2441 MHz,
squelch back to -120, LEDs off, PA level kept. So each setting has a known
answer on both sides of a mode stop. A case reads the original, picks a
different value, sets it, confirms it, stops from idle (the value must
survive), turns every LED on, enters xmas mode, stops it, and then requires
either the value it set (PA level, which only RX and TX mode entry writes) or
the reset value. Cleanup restores the originals. A value the tools cannot
write back, such as an LED state with RX and TX differing, makes the case skip
before anything changes rather than fail.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

**Commands.** `ubertooth-util -U <serial>` throughout, with the mode starts
and the CRC read that a case needs:

```text
ubertooth-util -U <serial> -c2426          # and -c 2426, the next-word form
ubertooth-util -U <serial> -C24            # and -C 24: the channel as an index
ubertooth-util -U <serial> -a3             # and -a 6, -a0, -a 0: the PA level
ubertooth-util -U <serial> -z-60           # the squelch, in dBm
ubertooth-util -U <serial> -l1             # the USR LED alone
ubertooth-util -U <serial> -d1             # all three LEDs
ubertooth-util -U <serial> -c2426 -a0 -z-60 -d1   # several setters in one command
ubertooth-util -U <serial> -c 2426 -a 6 -z -60 -d 1 -l 0   # the same as next words
ubertooth-util -U <serial> -c2426 -c       # a set and a read of one value
ubertooth-util -U <serial> -c2450 -c2426   # a repeated setter, refused
ubertooth-util -U <serial> -C48 -c2426     # the channel given twice, refused
ubertooth-util -U <serial> -a9 -v          # out of range before the report
ubertooth-util -U <serial> -x              # an active mode, for the stop
ubertooth-util -U <serial> -r              # the full reset, to the boot defaults
```

**Expected outcome.** Smoke runs 5 cases: channel, PA level, and squelch
across a mode stop, `-c <MHz>` with the value as the next word, and `-a0`
setting PA level 0. Comprehensive runs 28, adding the USR LED, repeated-option
refusals for each setting (`error: option '-c' given more than once`, exit 1,
with the setting unchanged), the refusal of `-c` together with `-C` (one
setting written two ways), an invalid PA level, a full `-r` reset back to boot
defaults including PA level 7, every next-word form, several next-word values
in one command, and the set-and-read pairs (`-c2426 -c`, `-c -c2426`, and so
on) where both must run and the read must report the new value.

**Known limits.** Only settings `ubertooth-util` can both read and write are
covered. The BR/EDR target address and the LE advertising channel are cleared
by a mode stop but are not readable through these options, so no case records
or restores them. The mode used is xmas, so this says nothing about what a
receive mode does to the same settings; `Spectrum analyzer` and the receive
tests cover their own stops.

## BLE CRC transitions

**What it does.** Presets the opposite CRC verify setting, runs a
`ubertooth-btle` sniffing mode with `-k0` or `-k1`, stops it, and requires the
mode's setting to be the one in effect.

**Theory of operation.** `ubertooth-btle -k` reads the CRC setting without
starting a mode, and `-n`/`-f` apply `-k[01]` after the mode starts. Presetting
the opposite value first is what makes the read afterward meaningful: it can
only report the mode's value if the mode really applied it. The capture cases
add a second known answer: each capture option writes a file with a fixed
header format and link type, so the file itself says whether the option did
what it names.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`. Each mode runs for 4 s and is stopped by the harness.

**Commands.** The CRC is set and read with `ubertooth-btle`, applied by a
bounded sniffing mode, and each capture file is walked by `capturefile.py`:

```text
ubertooth-btle -U <serial> -k<0|1>                     # and -k 0, -k 1 as next words
ubertooth-btle -U <serial> -k0 -k1                     # repeated, refused
ubertooth-btle -U <serial> -n -A<37|38|39> -k<0|1>
ubertooth-btle -U <serial> -f -A<37|38|39> -k<0|1>     # the other mode
ubertooth-btle -U <serial> -n -A37 -k<0|1> -r <case dir>/capture.pcapng
ubertooth-btle -U <serial> -n -A37 -k<0|1> -q <case dir>/capture.pcap
ubertooth-btle -U <serial> -n -A37 -k<0|1> -c <case dir>/capture-ppi.pcap
ubertooth-btle -U <serial> -f -A37 -k<0|1> -r <case dir>/capture.pcapng
ubertooth-btle -U <serial> -f -A37 -k<0|1> -q <case dir>/capture.pcap
ubertooth-btle -U <serial> -f -A37 -k<0|1> -c <case dir>/capture-ppi.pcap
ubertooth-btle -U <serial> -n -A37 -q <case dir>/capture.pcap -c <case dir>/capture-ppi.pcap
ubertooth-btle -U <serial> -n -A37 -r <case dir>/capture.pcapng -q <case dir>/capture.pcap
ubertooth-btle -U <serial> -n -A37 -r <case dir>/capture.pcapng -c <case dir>/capture-ppi.pcap
capturefile.py <case dir>/capture.pcapng               # the header and link type
capturefile.py --absent <case dir>/capture-ppi.pcap    # the file that must not exist
```

**Expected outcome.** Smoke runs 5 cases: CRC enabled during no-follow and
follow mode on advertising channel 37, and one capture case per output format
(`-r` PCAPNG link type 256, `-q` PCAP link type 256, `-c` PCAP link type 192).
Comprehensive runs 31: both modes, both CRC values, and all three advertising
channels; the refusal of a repeated `-k`; `-k 0` and `-k 1` with the value as
the next word; every mode, CRC value, and capture format combination; the
first-option-wins behavior of `-q` and `-c` together, which share one PCAP
handle, so the second file is reported as ignored on stderr and never created;
and `-r` with `-q` or `-c`, which use separate handles, so both files are
written. Every case restores the CRC setting, channel, squelch, and LEDs.

**Known limits.** `-p` (promiscuous) is not a CRC retention mode: it disables
CRC verification inside the firmware on purpose, so it is left out. The
capture checks read the file header and link type, not decoded packets, so a
pass does not mean anything useful was captured; no peer is required, and on a
quiet bench the files may hold nothing.

## BLE settings

**What it does.** Sets and reads back `ubertooth-btle` settings that need no
peer: the access address, the follow target, and the access-address error
allowance.

**Theory of operation.** The access address can be read back
(`Access address: xxxxxxxx`), so a set followed by a read has a known answer,
and the value chosen always differs from the original so the read cannot pass
by accident. `ubertooth-util -r` is documented to reset the access address to
the advertising address `8e89bed6`, which is another known answer. The follow
target cannot be read back, so those cases check only the tool's own
confirmation lines and leave the target cleared.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

**Commands.**

```text
ubertooth-btle -U <serial> -a                       # read the access address
ubertooth-btle -U <serial> -a12345678               # and -a 12345678, the next word
ubertooth-btle -U <serial> -azzzz                   # refused, nothing changed
ubertooth-btle -U <serial> -a -k                    # both reports in one command
ubertooth-btle -U <serial> -t22:44:66:88:aa:cc      # set the follow target
ubertooth-btle -U <serial> -t22:44:66:88:aa:cc/24   # masked
ubertooth-btle -U <serial> -tnone                   # cleared
ubertooth-btle -U <serial> -x0 -k                   # and -x32: the error allowance
ubertooth-btle -U <serial> -p                       # a bounded receive-only run
ubertooth-util -U <serial> -r                       # resets the address to 8e89bed6
ubertooth-util -U <serial> -c2426 -z-60 -d1         # preset away from the values a
                                                    # mode stop resets, so that check
                                                    # cannot pass by default
ubertooth-util -U <serial> -C|-a|-z|-d              # read each back, after the set
                                                    # and after the mode stop: one
                                                    # command each
```

**Expected outcome.** Smoke runs 3 cases: set and read back the access
address, set it with the value as the next word, and set, mask, and clear the
follow target. Comprehensive runs 8, adding that `-r` resets the access
address to `8e89bed6`, that `-x0` and `-x32` are both accepted, that `-a -k`
reports both the address and the CRC setting in one command, that `-azzzz` is
refused with exit 1 and leaves the address alone, and that stopping
`ubertooth-btle -p` runs the idle reset (LEDs off, channel 2441 MHz, squelch
-120, PA kept).

**Known limits.** No peer, so nothing here shows that a follow target or an
access address changes what is received. The follow target is write-only, so
those cases rest on the tool's own output. The promiscuous case receives only;
it never jams.

## Debug registers

**What it does.** Reads CC2400 registers with `ubertooth-debug` in lists,
ranges, and verbosity levels, and requires that nothing on the board changes.

**Theory of operation.** The order of the registers in the output is
predictable from the command, including a reversed range, which must read
downwards. That makes the register names a known answer rather than a
pattern match. The verbosity levels have a structural signature: `-v0` prints
no field lines, `-v1` prints field lines but no reserved fields, and `-v2`
prints the reserved ones too, so each level can be told apart by counting.
The exit-status case reads a register whose value's low byte is non-zero:
a build that exited with the last register value instead of 0 fails it while
passing a read that ends on a register whose low byte is 0.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

**Commands.**

```text
ubertooth-debug -U <serial> -r 0x02,0x01,0x00   # a list, in the order given
ubertooth-debug -U <serial> -r 0x00-0x03        # a range, and 0x03-0x00 reversed
ubertooth-debug -U <serial> -r 0x01             # one register, for the exit status
ubertooth-debug -U <serial> -r 0x00 -v<0|1|2>   # each verbosity
ubertooth-debug -U <serial> -r 0x00 -v3         # refused
ubertooth-debug -U <serial> -r 0x100            # refused
ubertooth-util  -U <serial> -C|-z|-d            # nothing changed: one command each
```

**Expected outcome.** Smoke runs 1 case: the list `0x02,0x01,0x00` must
report FSDIV, FSCTRL, and MAIN in that order. Comprehensive runs 9, adding the
range `0x00-0x03`, the reversed range `0x03-0x00`, the exit-status case, the
three verbosity levels, and refusals of register `0x100` and `-v3`. Every case
reads the channel, squelch, and LEDs afterward and requires them unchanged.

**Known limits.** Register values are not checked, only their shape, order,
and that reads change nothing.

## Ubertooth util modes

**What it does.** Starts the board-side modes `-I` (identify) and `-q`
(LED spectrum analyzer), watches the LEDs, stops the mode with `-S`, and reads
`-m` on a board with no range-test result.

**Theory of operation.** Both modes return immediately and leave the board
doing something visible, so the LEDs are the signal. The identify blink
toggles all three LEDs together about every 167 ms, so sampling `-d` until
both an all-on and an all-off sample appear shows the blink is running;
`-q120` sets the threshold to -120 dBm, the bottom of the radio's RSSI range,
so every reading crosses it and every LED lights steadily. Each mode is
started over deliberately non-default settings, so the idle reset after `-S`
has something to reset, and `-S` must then leave the LEDs off, the channel at
2441 MHz, and the squelch at -120 with the PA level kept. The `-m` case resets
the board first, so it is known to have no range-test result, and the command
must say so rather than print values or fail.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

**Commands.** The modes, the stop, and two LED samplers: `ledblink.py` for
the one case that watches `-I` blink, and a fixed-count shell loop for the
cases whose LEDs should simply be on and stay on.

```text
ubertooth-util -U <serial> -I                  # identify, and -s -I
ubertooth-util -U <serial> -q120               # and -q20 and a bare -q: the LED spectrum
ubertooth-util -U <serial> -r                  # no stale range-test result
ubertooth-util -U <serial> -m                  # the result, read after it
ubertooth-util -U <serial> -v                  # the firmware, after -r
ubertooth-util -U <serial> -c2426 -z-60 -d1    # preset away from the values a mode
                                               # stop resets, so that check cannot
                                               # pass by default
ubertooth-util -U <serial> -C|-a|-z            # read each back, during the mode and
                                               # after the stop: one command each
ledblink.py ubertooth-util <serial> --max-samples 60 --deadline 10 --interval 0.05
sh -c '<read -d <count> times, <interval> apart, retrying one failed read>' \
    ubertooth-util <serial> 12 0.05
```

`ledblink.py` reads `ubertooth-util -d` at jittered intervals until both
phases of the blink appear, at most 60 samples or 10 s, and retries one
failed read. The shell loop is the steady-LED sampler the seven other mode
cases use: it reads `-d` a fixed number of times and retries a read that
failed once.

**Expected outcome.** Smoke runs 3 cases: `-I` blinks until `-S`, `-q120`
lights every LED, and `-m` reports no range-test result after a reset.
Comprehensive runs 8, adding `-q20` and bare `-q` (-85 dBm), which only have
to run until `-S`, each mode replacing the other while it runs, and `-s -I`,
which must report the serial and then blink.

**Known limits.** An LED read can stall while LED specan runs, so the sampler
retries a failed read once and reports the retry on stderr; two failures in a
row fail the case. Three separate control reads make one LED report, so a
sample taken mid-toggle can mix states: the blink check only requires that a
whole-on and a whole-off sample both appear. `-q20` and bare `-q` cannot be
checked by their LEDs at bench noise levels, so those cases only show the mode
starts and stops. The threshold itself cannot be read back.

## Multi-board selection

**What it does.** Checks the listing and index selection `ubertooth-util` does
when more than one board is attached.

**Theory of operation.** The harness knows every registered serial on the
host, so the listing has a known right answer: one line per board, indices
0..n-1 in order, exactly those serials and no others. The index cases then
close the loop: the serial that `-U all -s` lists at an index must be the
serial `-U<index> -s` reads. Each listed line must also carry its report
label, so a regression back to the older unlabeled listing fails rather than
passing the pattern.

**Hardware and arguments.** One `Ubertooth` plus at least one guard board.
Guarded. `coverage:`. On a host with one registered board the test skips
itself at run time rather than failing planning.

**Commands.** Needs two or more registered boards; every command here is
about which board answers, so most carry no `-U`:

```text
ubertooth-util -U all -s          # and -s with no selector: the listing
ubertooth-util -N                 # the count
ubertooth-util -U<index> -s       # each board at the index the listing gave
ubertooth-util -U all -bvps       # and -bvps with no selector: four lines a board
ubertooth-util -U all -V          # and -V with no selector: every board, indexed
ubertooth-util -C                 # refused with several boards and no -U
ubertooth-util -U<past the last>  # refused: no Ubertooth at index
```

**Expected outcome.** With two boards, smoke runs 3 cases and comprehensive 9;
the index case's step count grows with the number of boards. Smoke: `-U all
-s` lists every registered board and only those, `-s` without `-U` does the
same, and `-N` plus one `-U<index> -s` per board agree with the listing.
Comprehensive adds the four identity queries listed per board in index
order, both with `-U all` and with no selector; `-V` reporting every board
itself, also both ways, as the host tools' version (followed by the libbtbb
path on a commit that prints one) and then each board's firmware version and
build under its index; `-C` without `-U` refusing to
pick a board (exit 1 with `error: multiple Ubertooth devices found; select
one with -U <index|serial>` and one `Device <index>: <serial>` line per
board, and no channel read); and an index past the last board refused with
`error: no Ubertooth at index N (N attached)`.

**Known limits.** Every command is read-only, so this says nothing about
selection for a command that writes. The listing order is the USB index order,
which the registry does not know, so the checks require the right set in index
order rather than a particular board at a particular index. The `-V` answer
carries no serial, so which board each of its lines came from is not checked
there; the identity listing does that. Harness issue
[#145](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/145)
is open against the extcap listing fixtures.

## BLE mode conflicts

**What it does.** Gives `ubertooth-btle` more than one major mode and requires
a refusal before sniffing starts.

**Theory of operation.** `-f`, `-n`, and `-p` are alternatives, so any
combination of them is a usage error. The refusal is checked by its own
message and exit 1, and the case reads the CRC flag, channel, squelch, and
LEDs before and after, so a regression that picked one of the modes and
started sniffing is caught by the settings a mode start and stop would have
changed.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. No `coverage:` argument: the case list is fixed.

**Commands.**

```text
ubertooth-btle -U <serial> -f -n      # every pair, and all three
ubertooth-btle -U <serial> -f -p
ubertooth-btle -U <serial> -n -p
ubertooth-btle -U <serial> -f -n -p
```

**Expected outcome.** Four cases: `-f -n`, `-f -p`, `-n -p`, and all three
together. Each must print `error: choose one of -f, -n or -p` and exit 1, with
every setting restored.

**Known limits.** It checks only that nothing started. None of these modes
transmits, so there is no RF risk here, unlike the `ubertooth-util` conflicts.

## Ubertooth util conflicts

**What it does.** Gives `ubertooth-util` two continuous mode options at once
and requires a refusal before any mode starts.

**Theory of operation.** Every pair of the mode options conflicts, so the
expected answer is the same for all of them: `error: options <first> <second>
cannot be combined`, exit 1, and none of the mode-start lines on either
stream. The case also reads back the settings a mode start would have changed,
including every LED, which is a second, independent signal that nothing ran.
Where one of the pair is range test or repeater, the PA level is recorded and
restored too, because those are the only modes that write the PA register.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `allow RF transmission: true` is required: three of the six modes
(range test, repeater, TX test) transmit, so a regression that accepted a pair
would put the board on the air. The explicit allowance is the plan's consent
to that risk, not a statement that the test transmits when it passes.

**Commands.** Every pair of the six continuous modes, in the order the fork
lists them:

```text
ubertooth-util -U <serial> -<mode> -<mode>   # -q -n -e -t -x -I, each pair
```

`-i` is left out on purpose: an accepted pair there would leave the board
outside its application firmware.

**Expected outcome.** 15 cases, one per pair of `-q`, `-n`, `-e`, `-t`, `-x`,
and `-I`. Each must be a usage error with the board left idle and every
setting, including each LED, restored.

**Known limits.** `-i` (ISP) and the removed `-f` (DFU) are never used against
a board here, because an accepted conflict could strand the board outside its
application firmware; `Util mode refusals` covers those with no board
attached. The test pins that the refusal happens, not what any of the modes
would do.

## Two-device range test

**What it does.** Runs the firmware's two-board range test: the repeater on
one board, the range test on the other, and reads the result.

**Theory of operation.** The sender transmits a 22-byte request carrying its
own serial number, 16 copies at each PA level from 0 (-25 dBm) up to 7
(0 dBm), then listens. The repeater returns the first request that arrives
with a good CRC, copying the PA level and copy number it heard into the reply
and adding its own. The sender keeps the first good reply and compares its
first 18 bytes with the request it sent. So the reply is derived from the
request, which is what makes a clean round trip a real check of both
directions rather than of one board talking to itself. `-m` on the sender then
reports the lowest PA level and copy number that got through each way; the
repeater keeps no result of its own, which the test also confirms.

`-n` waits for the round trip and prints that same result block itself, on
standard output, with its progress on standard error. So the test reads the
result twice over: `-n`'s standard output must carry those four lines and
nothing else, and the `-m` read after the test must give the same four
values, which the case records from `-n` and holds the read to. Its step is
given longer than the tool's own deadline, so a test the repeater never
answers is reported by the tool, with its reason, rather than killed by the
harness; a reply that differs from the request ends `-n` the same way, with
the byte it differs at on standard error.

Both boards are reset first, so neither can report a stale result, and both
are set to 2410 MHz, which is neither the boot default nor the value the idle
reset leaves. That way the channel reads after the test show the idle reset
ran rather than just showing what `-r` left behind. The responder's LEDs are
read while it repeats, before the sender starts and again after the test, so a
failure tells a wedged repeater from a wedged sender.

**Hardware and arguments.** Two Ubertooths as `sender` and `responder`, each
named by an explicit alias or chosen by RF placement, and both needing a full
32-hex-digit serial in the registry either way. `host:` must name one host
rather than `each`. `allow RF transmission: true` is required: both boards
transmit, the sender at every PA level up to 0 dBm. `maximum PA level:` (0-7,
default 7) is the highest level the plan accepts for a pass. Guarded.

**Commands.** Two boards: `<sender>` and `<responder>`, each read and reset
first, with the result polled by a shell loop until it appears or the 10 s
deadline passes:

```text
ubertooth-util -U <sender>    -r      # and the responder: no stale result
ubertooth-util -U <sender>    -c2410  # and the responder, onto one channel
ubertooth-util -U <responder> -e      # the repeater
ubertooth-util -U <responder> -v      # it still answers USB
ubertooth-util -U <sender>    -n      # the round trip
sh -c '<read -m until a result or the deadline>' ubertooth-util <sender> 10 0.25
ubertooth-util -U <responder> -m      # the repeater keeps no result
ubertooth-util -U <sender>    -v      # and the responder: the firmware
ubertooth-util -U <responder> -S      # stop the repeater
ubertooth-util -U <responder> -C      # and the sender: the channel it is left on
ubertooth-util -U <responder> -d      # and the sender: the LEDs, during and after
```

**Expected outcome.** One case of 35 steps. It passes when the sender's `-n`
completes the round trip and reports it, and its `-m` reports those same four
values afterwards, with request and reply PA levels at or below
`maximum PA level` and copy numbers 0-15; the repeater answers USB with its RX
LED on both before and after the test and keeps no result; the sender is idle
at 2441 MHz once the test ends by itself; `-S` returns the repeater to idle;
and every setting of both boards is restored. A reply that differs from the
request ends `-n` with the tool's own `invalid range test: mismatch on byte N`
and fails the case there.

**Weak-link responder.** `range.yaml`'s second suite, `range-weak-link`,
chooses both boards by RF placement instead of by alias: the responder behind
an attenuator (`min_attenuation_db: 20`, `max_attenuation_db: 20`,
`antenna: true`) and the sender in the open (`max_attenuation_db: 0`,
`antenna: true`), which also keeps the roles apart
on a bench whose default board is the attenuated one. Because the step names
one host, a bench with no such board fails planning, with the requirement and
each board's placement in the message.

Its `maximum PA level` is the same 3 the ordinary suite sets. That is the
bound this test already carries on this bench rather than a figure taken from
the weak-link runs: a responder a meter away behind 20 dB answered a sender in
the open in five round trips whose ten PA levels, a request and a reply each,
were all 0 or 1, and five with the roles swapped reported one level 2. The
suite does not narrow the bound; what it adds is which board answers, and that
the case runs at all with an attenuated responder.

The suite writes that as `weak link: {responder: 20}`, which pins the
responder to exactly 20 dB with its antenna attached and the sender to an
ordinary board. Exactly, not a range, because the figures below belong to one
attenuation each.

**20 dB against 40 dB, measured 2026-11-02.** One ordinary sender and one
firmware revision on both sides (`bluetooth_rxtx git-31228b05`, API 1.57,
verified on each board inside the hold), six rounds per responder, each
responder about a meter away behind its own attenuator:

| responder | request PA level per round | reply PA level per round |
|---|---|---|
| 20 dB | 0, 0, 0, 0, 0, 0 | 0, 0, 0, 1, 1, 0 |
| 40 dB | 0, 0, 1, 0, 1, 1 | 3, 3, 2, 3, 3, 1 |

All twelve rounds completed with a clean round trip and no `mismatch on
byte`. The figure that moves is the **reply** PA level — the level the
attenuated board has to transmit at to be heard — which reaches 1 behind
20 dB and 3 behind 40 dB. The request level barely moves, because that
direction is an ordinary sender reaching an attenuated receiver rather than
the reverse.

Two consequences. 40 dB is a usable measurement point: it neither loses the
link nor leaves the levels indistinguishable. But reply level 3 is exactly
`maximum PA level`'s default bound here, so a 40 dB entry would sit on the
bound and needs that argument raised rather than inherited. The copy numbers
(`request number`, `reply number`) are not a discriminator at this sample
size: they ranged over 0 to 15 at both attenuations. A bench with a different attenuator fails planning with the
requirement in the message, which is more use than a run judged against
figures taken elsewhere. Distance cannot be pinned, since a placement
requirement has no distance key, so a board at 20 dB much further away is
still selected and may need a higher level.

**Known limits.** On an ordinary bench the boards sit centimeters apart, so a
working link gets through at a low PA level and a pass at level 0 says little
about range. What `maximum PA level` buys is a tighter bound than "it worked at
full power". The weak-link suite sets the same bound and changes which board
answers. An occasional `mismatch on byte 0` is a known fork
behavior
([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)). The
test needs a second board, so it cannot run on a single-board host.

**The `range-weak-link` suite has never judged a result.** Its one recorded
run, at 20 dB and about a meter, failed at the `-n` step: `-n` printed
`waiting for the repeater...` on standard output rather than standard error
([ubertooth#1026](https://github.com/SqueakyFloors/ubertooth/issues/1026)),
where the result block is. That line appears only once the first progress
interval has passed, so a repeater at bench distance answers before it and
the ordinary suite never saw it; one a meter away behind 20 dB did. The fork
fixed the stream in `30c4103f`, and this step now requires standard output to
carry the result block and nothing else, so that regression is still what the
suite would catch. Nothing has run it since the fix, so it stopped before
`-m` was read and the PA levels above come from driving `-e`, `-n`, and `-m`
by hand. It is committed so the fork fix is verified by a suite rather than by
a one-off script.

## Transmit power steps

**What it does.** Nothing yet: a catalog entry for holding one board's
transmitter at each PA level the plan lists while the attenuated board reads
the channel it transmits on, so the RSSI step per level is a figure.

**Theory of operation.** The PA level is the one transmit-side quantity the
firmware sets directly (`FREND.PA_LEVEL`, 0 for about -25 dBm up to 7 for
about 0 dBm), so a receiver at a fixed attenuation should read a signal that
rises with it. Both halves have to be held for that to mean anything: the
level, which the sender sets with `ubertooth-util -a<level>` before entering
its continuous transmit mode (`-t`), and the attenuation, which the registry
records and `weak link:` pins. With neither held, a reading is whatever the
bench was doing.

The receiver's reading would come from `ubertooth-specan` over a narrow range
around the sender's channel, read per frequency rather than as a band mean.
The band mean is what cannot work, and that part is measured: three 10-second
`ubertooth-specan -g` sweeps per board put the attenuated board 1.1 dB from
an ordinary one, with 0.1 dB of run-to-run spread, because most of a sweep is
the receiver's own noise floor
([#217](https://github.com/SqueakyFloors/ubertooth-test-harness/pull/217)). A
peak at a frequency a sender is holding is a different measurement from the
floor across the band.

Why the attenuated board rather than any two boards is the issue's own
reasoning, not a measurement here: attenuation is what keeps the receiver out
of saturation at bench distance, and
[#168](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/168)
records a receiver at PA 7 from an adjacent board wedging until a power
cycle. Whether the levels in fact separate on this board at this attenuation
is one of the things a first run would answer.

**Hardware and arguments.** A `sender` and a `receiver` (both Ubertooth) on
one named `host:`, a required `allow RF transmission: true` because the sender
transmits, `PA levels:` (an ascending list of at least two distinct levels
0-7), a `channel:` in MHz for both boards to hold, and a `duration:`, which
is the whole test as it is everywhere else rather than a time per level — the
catalog's wall-time estimate reads it that way. `weak link: {receiver: 20}`
is how a plan puts the attenuated board on the receiving side.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it. What it would judge is the RSSI difference between consecutive
levels, and nothing has measured that at any attenuation, so the figure is not
written down here or anywhere else.

**Known limits.** Any figure is specific to this bench: a step measured at
20 dB and a meter says nothing about another distance or another attenuator,
which is why the attenuation is pinned rather than bounded. Nothing says yet
whether the levels separate cleanly enough to put a floor on the step, or
whether `-t`'s carrier reads the same way as modulated traffic would. The
continuous transmit mode is also the one mode on this board no test runs
today: `Ubertooth util conflicts` only checks that `-t` is refused when it is
combined with another mode. Tracked as part of
[#168](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/168).

## Receiver sensitivity

**What it does.** Nothing yet: a catalog entry for holding a sender at one
known PA level while the attenuated board reports what it hears.

**Theory of operation.** Not established, and the gap is on the receiving
side. What is settled is why a driven sender is needed at all: the figures the
tools report — `ubertooth-rx`'s `s=`, `n=` and `snr=` beside each packet, and
`ubertooth-specan`'s RSSI per frequency — mean something only when the
transmitter's level is known and held. Against ambient traffic the signal is
whatever happened to be on the air, and how much of a piconet a sweeping
receiver hears varies widely between runs, which is why the Classic tests
carry floors rather than counts.

What is not settled is which reading to take, because the two candidates do
not line up. The per-packet figures come from packet lines, so they need the
sender to put real Bluetooth packets on the air, and the only level-settable
transmitter here is an Ubertooth whose continuous transmit mode (`-t`) sends a
carrier rather than packets. A spectrum reading does work against a carrier,
but it measures the band, not whether a packet decoded, so it is a transmit
power measurement again rather than sensitivity. The ESP32 peers do put real
packets on the air, and their transmit level is not ours to set. Which of
those three the test should use is the open question.

One thing the line format does help with: a packet line says `none` rather
than a figure wherever the board has no reading to give, and the ratio goes
whenever either figure it comes from does, never one of each, so a run can
tell a measured level or floor from an absent one. Both halves of that
matter to a sensitivity reading. The noise column is `none` where the mode
had no background estimate for the channel, and the level column is `none`
where the board's own reading is at the top of the radio's scale, which says
no more than "at least this loud" and used to print as `0 dBm` — about a
fifth of the packets of a close Classic link
([ubertooth#1192](https://github.com/SqueakyFloors/ubertooth/pull/1192)). A
capture record has no room for the distinction and still carries 0 dBm
there, so it is the printed lines that carry it. The harness's packet-line
pattern accepts every combination that rule allows and refuses the ones it
does not, which is what keeps a saturated packet counted as a packet rather
than quietly dropped.

**Hardware and arguments.** A `sender` and a `receiver` (both Ubertooth) on
one named `host:`, a required `allow RF transmission: true`, one `PA level:`
(0-7) for the sender to hold, a `channel:` in MHz for both boards, and a
`duration:`. `weak link: {receiver: 20}` puts the attenuated board on the
receiving side.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it. The figure it would judge — the level at which packets stop arriving,
or a floor on the signal — has not been measured from a held transmitter at
any attenuation.

**Known limits.** One attenuator gives one point, and a sensitivity curve
wants more: 30 dB and 40 dB would be the second and third. A pass would say
nothing about absolute sensitivity in dBm either, since the attenuation and
the path loss at a meter are both known only as the registry records them by
hand. Tracked as part of
[#168](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/168).

## BLE monitor

**What it does.** Nothing yet: a catalog entry for a bounded BLE monitoring
run on one board.

**Theory of operation.** Not established. The known advertiser the useful
version of this would need now exists
([#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86)) and
is used by `Capture known BLE advertiser`.

**Hardware and arguments.** One `Ubertooth` and a required `duration:`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** A monitor run with no known transmitter can only check that
the tool ran, which `BLE CRC transitions` and `BLE extcap capture` already do.

## Spectrum analyzer

**What it does.** Sweeps with `ubertooth-specan` and requires that the samples
stay inside the requested range and cover all of it.

**Theory of operation.** A sweep's range is exactly what the command asked
for, so the set of frequencies that must appear is known before the run: every
frequency from the lower to the upper bound, and nothing outside them. That
makes two faults visible without any signal source: a sweep that reports
frequencies it was not asked for, and one that leaves part of its range out.
Each output format has its own line shape, so changing format changes what a
sample line must look like. Several cases deliberately follow a case with a
different range, which is how a run that began by reporting the previous run's
samples is caught.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:` and a required `duration:`, which is how long each sweep
runs before the harness stops it.

**Commands.** `ubertooth-specan`, with `specandump.py` reading the binary
files it writes:

```text
ubertooth-specan -U <serial>                           # the full band
ubertooth-specan -U <serial> -l <MHz> -u <MHz>         # a narrow, single, top-of-band range
ubertooth-specan -U <serial> -v -l 2402 -u 2420
ubertooth-specan -U <serial> -g -l 2402 -u 2420
ubertooth-specan -U <serial> -G -l 2402 -u 2420        # the 3D form
ubertooth-specan -U <serial> -l 2430 -u 2440 -d <case dir>/specan.bin
python -c 'import os, sys; os.chdir(sys.argv[1]); os.execvp(sys.argv[2], sys.argv[2:])' \
    <case dir> ubertooth-specan -U <serial> -l 2430 -u 2440 -d -x.bin
                                                       # the file named -x.bin,
                                                       # in the case directory
ubertooth-specan -U <serial> -d /dev/full              # Linux only
specandump.py <case dir>/specan.bin 2430 2440
ubertooth-util -U <serial> -c2426 -z-60 -d1            # preset away from the values
                                                       # a mode stop resets, so that
                                                       # check cannot pass by default
ubertooth-util -U <serial> -C|-a|-z|-d                 # read each back, after the
                                                       # set and after the stop:
                                                       # one command each
```

**Expected outcome.** Smoke runs 1 case: a full-band sweep over 2402-2480 MHz
in the default format. Comprehensive runs 11, adding a narrow range right
after the full-band one, a single frequency, the top of the band, the `-g` and
`-G` feedgnuplot formats, `-v` (which may only add stderr), `-d` to a binary
file, a `-d` file whose name starts with `-`, a `-d` to `/dev/full` where the
first failed write must end the run with one error line and exit 1, and a case
that checks the idle reset after the sweep is stopped. A sweep passes when
every line parses in its format, every frequency of the range appears, no
sample is outside it, no progress or diagnostic line is on stdout, and the
RSSI values fit in a signed byte; a handful of lines cut short by the stop
signal are tolerated, more than five are not.

**Known limits.** It checks coverage and format, not signal quality;
`Spectrum RSSI sanity` does that. The `/dev/full` case needs Linux and is left
out elsewhere as not applicable. Nothing here transmits, so the readings are
whatever the bench's ambient traffic happens to be.

## Spectrum RSSI sanity

**What it does.** One full-band sweep, checked for the three properties a live
receiver has and a dead one does not.

**Theory of operation.** The tool prints the CC2400 RSSI register as the
firmware reads it, which sits 54 dB above the input power. A receiver that is
really measuring gives three things with no signal source at all: a noise
floor at every frequency, since the lowest reading of each frequency over a
whole sweep is a quiet sample even on a busy Wi-Fi channel; a negative mean,
since most samples are noise; and readings that vary, since a register that is
not being read gives one value. A stuck or disconnected front end fails at
least one of those.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. A required `duration:`.

**Commands.**

```text
ubertooth-specan -U <serial>
```

One bounded full-band sweep of the plan's `duration`, stopped by the harness;
the readings are checked in the harness rather than by a helper.

**Expected outcome.** One case. It passes when every frequency's lowest
reading is between -115 and -80 dBm, with up to five of the 79 frequencies
allowed above -80 dBm (a continuous emitter nearby); the mean reading is
negative; and at least three distinct readings appear. A floor below -115 dBm
at any frequency fails, as does a sixth busy frequency.

**Known limits.** The bounds are deliberately wide, so this catches a broken
receiver and not a slightly deaf one. Absolute accuracy is not checked: the
offset is the firmware's documented one, not a calibration. A noisy bench can
push more frequencies above -80 dBm and fail the test for reasons that have
nothing to do with the board.

## Classic Bluetooth receive

**What it does.** Runs bounded `ubertooth-rx` and `ubertooth-afh` receive runs
with no peer in range, and checks their exits, output shape, and files.

**Theory of operation.** With no piconet on the bench, what is knowable is the
behavior of the tool rather than the content of the air. `ubertooth-rx -t N`
must stop itself at about N seconds and exit 0, and every packet line it does
print must match the documented format, which is where a change in the record
shape, or a power column out of step with the two beside it, shows up: each
of the level and noise columns is a figure or `none`, and the ratio is a
figure exactly when both of them are. Where the command fixes a
channel, every packet reported must be on that channel; where it caps
access-code errors, no packet may exceed the cap. `ubertooth-afh -t N` has a
known answer in the other direction: with nothing from the named piconet, it
must say so, stop the board, and exit 2 rather than printing an all-zero map
and continuing. Capture and dump options are checked by the file they write:
a BR/EDR PCAPNG or PCAP header with link type 255, or whole 68-byte records.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:` and a required `duration:`, which becomes the `-t`
value. The Ubertooth defaults to needing an ordinary link (antenna attached,
no attenuation), so the planner keeps these cases off an attenuated board.

**Commands.** `ubertooth-rx` and `ubertooth-afh` with no piconet in range,
`<duration>` being the plan's duration in whole seconds, and the files read by
`capturefile.py` and `rxdump.py`:

```text
ubertooth-rx  -U <serial> -t <duration> -z             # the survey
ubertooth-rx  -U <serial> -t <duration>                # no target
ubertooth-rx  -U <serial> -t <duration> -c 39          # one channel
ubertooth-rx  -U <serial> -t <duration> -s            # the default sweep, and -s -c 39
ubertooth-rx  -U <serial> -t <duration> -e <0|4>       # the error allowance
ubertooth-rx  -U <serial> -t <duration> -r <case dir>/rx.pcapng
ubertooth-rx  -U <serial> -t <duration> -q <case dir>/rx.pcap
ubertooth-rx  -U <serial> -t <duration> -d <case dir>/rx.dump
ubertooth-afh -U <serial> -l <LAP> -u <UAP> -t <duration>
ubertooth-afh -U <serial> -l <LAP> -u <UAP> -t <duration> -e 2
ubertooth-afh -U <serial> -l <LAP> -u <UAP> -r         # a map line a second
ubertooth-afh -U <unknown serial> -l <LAP> -u <UAP> -t 1    # exits 1
capturefile.py <case dir>/rx.pcapng
rxdump.py <case dir>/rx.dump full --min-records 0 --types br
```

**Expected outcome.** Smoke runs 3 cases: a survey (`-z -t`) that ends by
itself and lists its results, with the `Survey Results` heading on standard
error and the addresses on standard output (the heading among the addresses
is a failure: the fork splits the two streams so that standard output holds
only what a script parses), a `-r` PCAPNG capture, and the AFH no-packets
case. Comprehensive runs 14, adding the plain sweep, a fixed channel, the
explicit `-s` sweep and `-s -c 39` where the channel wins, `-e 0` and `-e 4`,
a `-q` PCAP capture, a `-d` packet dump, AFH with `-e 2`, AFH `-r` printing
one map line per second, and AFH with a serial no board has, which must exit 1
with a message rather than crash.

**Known limits.** No peer, so a pass does not mean the board can receive. Zero
packet lines is a normal outcome on a quiet bench, so these cases cannot
distinguish a deaf board from a quiet room; survey results, when they appear,
carry a guessed UAP. The AFH cases assert the absence of a piconet, so they
fail if one is actually in range.

A weak-link entry for the access-code error threshold (`-e`) is the one piece
of this test attenuation would be good for, and it is still waiting in
[`weak-link.yaml`](../testplans/weak-link.yaml) rather than running. The
attenuated-device sweep compared what `-e 0`, `-e 2`, and `-e 4` find by hand
and the answer was clean at 20 and 40 dB; what the test cannot do is make that
comparison, because its cases only require that no packet exceeded the limit,
which zero packets satisfy, and this entry binds no peer to produce any. One
measured oddity sits behind that: at 20 dB, `-e 4` collapsed to single-digit
packet counts twice, minutes after the same board returned 2692 packets at
`-e 2`, which is unexplained and fork-side, so no entry asks for `-e 4` at
20 dB. See "What attenuating a receiver is for" above.

## Raw receive

**What it does.** Streams symbols with `ubertooth-dump`, receives with
`ubertooth-ego -r`, and checks the records and modulation.

**Theory of operation.** `ubertooth-dump` delivers a steady stream of
fixed-size records whether or not anything is on the air, so its record count
is a throughput check with a known floor: about 2000 records a second on
hardware, against a required minimum of 50 a second. The format options have
distinct record sizes (68-byte full records, 401-byte ASCII bitstream
records), so the file says which was used. The modulation options are checked
at their source: the firmware programs the CC2400 MDMCTRL register with a
known value for basic rate or LE when the stream starts, and the register
keeps it after the mode stops, so `ubertooth-debug` can read back which
modulation actually took effect. A capture uses one demodulator, so `-c` and
`-l` in one command line are refused rather than ranked; that refusal needs
no board and is in `Refused command lines`.

The stale-packet case uses the fact that `ubertooth-btle -n` and
`ubertooth-dump` read the board over different paths, so the two meet only at
a mode change. Alternating them and requiring that no capture holds anything
from the mode before it is a direct check for leftover buffered packets.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:` and a required `duration:`. The Ubertooth defaults to
needing an ordinary link.

**Commands.** `ubertooth-dump` and `ubertooth-ego`, with `ubertooth-debug`
reading the modulation back, `rxdump.py` checking the records, and
`stalecheck.py` comparing the five rounds of the stale-packet case:

```text
ubertooth-dump  -U <serial> -d <case dir>/dump-full.bin
ubertooth-dump  -U <serial> -b -d <case dir>/dump-bitstream.bin
ubertooth-dump  -U <serial> -c -d <case dir>/dump-classic.bin
ubertooth-dump  -U <serial> -l -d <case dir>/dump-le.bin
ubertooth-debug -U <serial> -r 0x03 -v0                         # MDMCTRL, after each
ubertooth-ego   -U <serial> -r -c 2440
ubertooth-ego   -U <serial> -r -c 2440 -l 1                     # and -l 48
ubertooth-ego   -U <serial> -r -c 2440 -a 630f9ffe
ubertooth-ego   -U <serial> -r -c 2440 -a zz                    # refused
ubertooth-btle  -U <serial> -n -A37
ubertooth-btle  -U <serial> -n -A37 -q <case dir>/stale-<n>.pcap
rxdump.py <case dir>/dump-full.bin full --min-records 150 --types br,keep-alive
stalecheck.py --dumps <case dir>/stale-N.bin ... --captures <case dir>/stale-N.pcap ... --channel 0
```

**Expected outcome.** Smoke runs 3 cases: full records to a file, the `-b`
bitstream to a file, and an E-GO receive run on 2440 MHz. Comprehensive runs
10, adding `-l` LE and `-c` basic-rate modulation, each verified through
MDMCTRL, E-GO capture lengths 1 and 48, an E-GO access
code, the refusal of a bad access code, and the stale-packet case, which
alternates BLE and BR/EDR five times and requires zero packets from the
previous mode in every dump and every capture.

**Known limits.** No peer: the E-GO cases print nothing with no skateboard in
range, so they check that the tool runs and reports nothing malformed. That is
as far as the harness goes with E-GO on purpose, not for want of a fixture
(see `Out of scope: Uberducky and E-GO devices`). The dump cases check record
structure and rate, not that the symbols mean anything.

## Board state between runs

**What it does.** Kills a running capture outright, so the board is left in
the mode that capture started, and then requires the next run on the same
board to be clean anyway: to say what it found, stop it, capture only its own
data, and leave a board that answers every read.

**Theory of operation.** A tool that exits normally, or is stopped with
SIGTERM, sends STOP on the way out, so the board it leaves is idle. A terminal
or an SSH session that disappears does not: the tool dies where it stands and
the board keeps streaming into its endpoint. The test makes that state on
purpose. [`killedcapture.py`](../ubertooth_harness/killedcapture.py) starts the
capture tool as its own child with nothing in between -- no shell, no
`timeout` -- waits until the capture file has grown, and sends that process
SIGKILL, which cannot be caught. What it then requires before the case goes on
is that the wait for the tool returned `-SIGKILL`: it died of the signal where
it stood, rather than ending some other way, and its exit status has been
read, so it is out of the process table rather than sitting there with its
device still open. The tool runs in a session of its own, which is what lets
the module take it and anything it started down together on a failure or when
the harness stops the step; that session also puts it outside the group the
harness signals, so those signals are caught and passed on rather than leaving
a capture streaming, and its own output goes to a file rather than to the
step's streams, which an orphan would hold open. The file the killed tool wrote is hashed at the kill and
checked again at the end of the case, so the evidence is kept rather than
quietly rewritten by the next run.

Both tools used produce data with nothing on the air -- `ubertooth-specan`
about 16 kB a second, `ubertooth-dump` about 2000 records a second -- so the
dump's own contents are the evidence and no peer is needed. The sweep is the
mode left behind and the dump is the capture that has to cope with it, which
is the order that works: see the known limits below. A dump's 68-byte records
carry the firmware's packet type, so a record left from the sweep fails by
type, since the firmware types a sweep's records SPECAN. A record of a mode
that writes basic-rate records would pass that and fails instead by its clock,
which restarts at mode entry
([`rxdump.py`](../ubertooth_harness/rxdump.py), `--max-stale`): no mode in
this test writes those, so that half is the "zero stale records" the issue
asks for rather than the leak this pairing can produce.

The dump states its modulation, LE, before it asks for its mode, and a mode
start puts the modulation back to basic rate. So the radio ending up in LE is
what says a setting stated for this capture survived the mode start on a board
that was left in another mode; it is read afterwards from the CC2400 MDMCTRL
register, which keeps what the last mode programmed.

The warning the second command has to print comes from libubertooth's
`ubertooth_bulk_init()`: it reads the mode the board is in, stops it, and
waits until the board reports that it has left it, because the stop request
only asks and the mode's loop can hand the endpoint another packet in the
meantime. The case requires that warning, requires it on stderr rather than
among the records on stdout, and forbids the second one the same code prints
when the wait runs out. Together those say the board was in a mode, the
capture found it, and the board reported leaving it inside the wait.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. A required `duration:`, which bounds the dump. No RF peer,
no firmware write, and nothing is transmitted.

**Commands.** `killedcapture.py` kills a capture and keeps what it wrote, the
next tool then runs on the board that capture left behind, and
`ubertooth-debug` reads the modulation the mode request did not reset:

```text
killedcapture.py kill   --capture <case dir>/killed-specan.bin \
    --record <case dir>/killed-specan.json \
    -- ubertooth-specan -U <serial> -l 2440 -u 2444 -d <case dir>/killed-specan.bin
killedcapture.py verify --capture <case dir>/killed-specan.bin \
    --record <case dir>/killed-specan.json
ubertooth-util -U <serial> -r                  # back to the boot defaults
ubertooth-util -U <serial> -v                  # the firmware, read once
ubertooth-dump  -U <serial> -l -d <case dir>/after-killed-specan.bin
ubertooth-debug -U <serial> -r 0x03 -v0
rxdump.py <case dir>/after-killed-specan.bin full --min-records 150 \
    --types br,keep-alive --max-stale 0
ubertooth-util -U <serial> -C|-a|-z|-d|-s      # read back after the kill, at the
                                               # boot defaults, and in the cleanup:
                                               # one command each
```

**Expected outcome.** One case: a symbol dump after a killed spectrum sweep.
It resets the board and reads back the documented boot defaults (2441 MHz, PA
level 7, squelch -120, LEDs off), kills the sweep, runs the dump, checks the
dump's records and the modulation the radio ended up in, confirms the killed
capture is byte for byte what the kill left, and then reads firmware version,
serial, channel, PA level, and squelch, each once: a read that needs a second
try fails the case. Cleanup stops the board, resets it,
requires the documented boot defaults again, and then puts back every setting
the case recorded; this is the one test that leaves a board in a mode on
purpose, so it hands the next test a board it has seen boot. A self-reset the
firmware records while this runs is reported by the usual watchdog read
between cases, and the guard boards are checked as in every guarded test.

**Known limits.** Whether the killed tool left a process of its own behind is
not checked, and cannot be after the fact: the tool's pid is also its process
group's id, and once the tool has been reaped that id is free to be given to
the next process that starts a session of its own, which the harness does for
every step, so a question about the group could be answered by somebody else's
process. The host tools are single processes, so there is nothing of theirs to
miss; a tool that did start one would need the process table read by name. A
failure, or the harness stopping the step, does take the whole group down,
which is when it matters and when the group is certainly this run's.

The case rests on the host leaving the board alone when the
killed process's handle closes. On a host or kernel that reset the device
there instead, the board would be idle, the warning the case requires would
never print, and the case would fail for a reason that has nothing to do with
the behavior it is about. Only Linux has been checked (the mode was still
running in 3 tries of 3); check a macOS host before putting this test in
[`mac.yaml`](../testplans/mac.yaml).

The order of the two captures is not free. A board left in a spectrum sweep
answers the mode read libubertooth makes at a capture start, so the warning is
reliable. A board left in a symbol dump does not always: nothing is draining
its endpoint, and the control read can stall, in which case libubertooth
cannot tell what the board is doing and says nothing, which is what the fork
chose for that case. Measured on the lab host: the board was still in `rx
symbols` in 3 of 3 tries after a killed dump, so the mode is there, but one
run of the dump-then-sweep order got the warning and another did not. That
order is therefore not a test, and only the sweep-then-dump order is used. The clock half of the record check sees a leftover
basic-rate record only when its clock is ahead of the new stream's first one,
so a short previous stream can go unseen there; the packet type is what
catches a record of the sweep, whatever its clock. A pass does not separate a
build that waits for the board
to leave the mode from one that warns and starts anyway. The only difference
between them is a packet or so of the previous mode reaching the new capture,
which is what the record and sample checks look for, so a run where nothing
happens to leak cannot tell them apart; the fork pins the wait itself in its
own unit tests. It kills a capture on purpose, which is the state the test is
about, and it is the one test here that does so; a board that comes out of it
wedged is a real finding, not a flake, and on a switchable port a power cycle
recovers it. Nothing here is checked about captured content: the dump is judged
on record structure, packet type, and clock, never on what the symbols mean.

## Interrupting a capture

**What it does.** Sends a real SIGINT to a running capture tool and requires
that one Ctrl-C ends it promptly, leaves a capture file a reader can open,
leaves no process behind and nothing still streaming, and leaves a board that
answers the next command at once.

**Theory of operation.** Ctrl-C is how a capture normally ends, so what it
leaves behind is what an operator meets every time. The fork's handler only
raises a flag; the capture loop reads it on its own thread, ends the stream,
stops the mode, closes the capture files, and exits 0. The handlers go on
before the board is opened, so a signal arriving while one is being found is
not thrown away. Nothing in the harness asserted any of that before this.
Bounded modes are stopped with SIGTERM; the one place a fork tool met a SIGINT
is the no-selector follow case, where `timeout -s INT` is a deadline and the
expected outcome is status 124 with SIGKILL behind it; and the cases that do
check a SIGINT send it to tshark.

The signal has to reach the tool itself, so
[`captureinterrupt.py`](../ubertooth_harness/captureinterrupt.py) starts it
directly, with no shell and no `timeout` wrapper (which would be signalled
instead), sends one SIGINT to that process's own pid, then waits for it and
reaps it. It starts the tool in a process group of its own, which is what lets it say
whether the run left a process behind, and take it down afterwards: the group
goes down on the way out, and a SIGTERM, SIGINT or SIGHUP to the helper —
which is what a canceled run or a step deadline sends — takes it down and
then re-raises, so a tool with a mode started cannot outlive its step. The
verdict is reached before any of that, so a process the run left is a finding
and not something the helper tidied away before looking.

For the two cases that can wait for evidence, the moment is chosen by it, not
by a clock. `ubertooth-dump -d` and `ubertooth-specan -d` stream whether or
not there is traffic, so the capture file growing past a floor is proof that
the mode is running. `ubertooth-dump` flushes each record, so its file grows a
record at a time; `ubertooth-specan` does not, so its file grows a stdio
buffer at a time and the floor is a buffer's worth rather than a few samples.
A capture that never gets there, and a tool that exits on its own before the
signal, both fail the case: these streams do not depend on the air, so neither
is something the bench decides, and no case here can pass by skipping.

**Hardware and arguments.** One `Ubertooth` role, no peer, no firmware write,
no transmission, and every other registered board is read as a guard.
`hosts:`, `coverage:`. No RF placement requirement: none of the cases needs
traffic, so an attenuated board will do.

**Commands.** `captureinterrupt.py` starts the tool with no shell and no
`timeout` wrapper, waits for the capture to grow, sends one SIGINT to that
process, and reaps it; the file is then read again by the checker it belongs
to:

```text
captureinterrupt.py interrupt --grow <case dir>/dump.bin --min-bytes 1360 \
    --settle 1.5 --ready-deadline 20 --exit-deadline 5 --expect-status 0 \
    --teardown-says <pattern> -- ubertooth-dump -U <serial> -d <case dir>/dump.bin
captureinterrupt.py interrupt --grow <case dir>/le.pcapng --min-bytes 28 ... \
    -- ubertooth-btle -U <serial> -n -r <case dir>/le.pcapng -q <case dir>/le.pcap
captureinterrupt.py interrupt --grow <case dir>/specan.bin --min-bytes 4096 ... \
    -- ubertooth-specan -U <serial> -d <case dir>/specan.bin
ubertooth-util -U <serial> -v                        # the board answers at once
rxdump.py      <case dir>/dump.bin full --min-records 20
capturefile.py --packets <case dir>/le.pcapng       # and le.pcap
specandump.py  <case dir>/specan.bin 2402 2480
ubertooth-util -U <serial> -C|-z                     # the 2441 MHz and -120 a
                                                     # mode stop leaves, read
                                                     # before the cleanup's stop
```

**Expected outcome.** Smoke runs 2 cases: `ubertooth-dump -d` interrupted once
it has written 20 whole records, and `ubertooth-btle -n -r -q` interrupted
1.5 s after its PCAPNG appears. Comprehensive runs 3, adding `ubertooth-specan
-d` interrupted once it has written a buffer's worth of samples. Every case
requires: exit 0 within 5 s of the one signal and not death by that signal;
nothing left in the process group the tool ran in; the capture file read again
after the exit and not grown, so no mode is still streaming into it; no
`error:`, `warning:` or progress line on standard output — `note:` is not one
of those, because `ubertooth-btle` prints its undecoded-packet note on the
report stream, which is standard output for a capture written to a file, and
the fork puts it there deliberately; for the two tools that report
unconditionally, their own line on standard error, so a quiet or misdirected
teardown is caught (`rx block timestamp ...` for the dump, `Ubertooth stopped`
for the sweep, while `ubertooth-btle` and `ubertooth-rx` print on their error
and note paths only, so their cases ask for none); the file itself readable
(whole 68-byte records for the dump, whole 3-byte in-band samples for the
sweep, a walkable PCAPNG and PCAP for the two LE writers); the channel and the
squelch back at the 2441 MHz and -120 a mode stop leaves, read before the
cleanup's own stop, so a board still carrying the mode's settings is seen;
`ubertooth-util -v` answered within 5 s; and then the usual cleanup restoring
the channel, the squelch, and the LEDs, with the identity read again. The tool's own two streams are kept beside the capture, as
`<capture>.stdout.log` and `.stderr.log`, so a failed run keeps its
evidence.

**Known limits.** The `ubertooth-btle` case's moment is a settling time, not
evidence: the tool creates both capture files after the board is open and
before the mode requests, and libbtbb sizes a PCAPNG in pages, so the file
reaches its floor as soon as it appears. That case therefore rests on the exit
and the teardown, not on having caught a running mode. For the same two
writers "the capture stopped growing" is weak, because a quiet LE capture
would not grow in the half second after the exit either; and the no-orphan
check cannot fail for any of these tools, since none of them forks, so it is
a guard rather than a measurement. What that case really rests on is the
prompt exit 0, the readable capture files, and the board's state afterwards. A stop that lands inside an *unanswered* poll, which is
where the fork drops the retries still to come
([ubertooth#856](https://github.com/SqueakyFloors/ubertooth/pull/856)), cannot
be produced here: it needs a board that has stopped answering, which is the
board-lost-mid-capture work and its hub power control
([#141](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/141)).
A signal aimed at the board-open window is out of reach from here, so no case
claims it. A lookup finishes in 10 to 20 ms on the bench host, so landing
inside it would take many tries; and on a build from before the handlers moved
ahead of the open ([ubertooth#839](https://github.com/SqueakyFloors/ubertooth/pull/839),
fork `3311ce36`) a signal that landed there killed the tool outright, which is
indistinguishable from one that arrived before the handler was installed on a
build that has the fix. There is no outcome that separates the two, so a case
built on it would either fail a correct build or retry a broken one away. Nothing here checks a second Ctrl-C, because one is meant to be enough.

## Raw receive replay

**What it does.** Decodes a synthetic capture file with `ubertooth-rx -i`,
with no Ubertooth and no USB at all.

**Theory of operation.** This is the one receive test with a known answer for
every packet, because the harness writes the input. The fixture
([`rxreplay.py`](../ubertooth_harness/rxreplay.py)) carries four piconets,
chosen errors, chosen channels, chosen host times, chosen distances from a
CLKN edge, and decoy records, so the expected output is computed rather than
pattern-matched: which packet lines appear, in what order, with which systime
and which clock offset, which ones a filter drops, and how many packets
reach a capture file. That makes it possible to check decode behavior
that no live run can pin down, and to run the whole thing in CI.

The four piconets each carry something the others cannot. The first sends
POLLs and one DH1, so its address part decodes from a payload CRC. The second
sends ID packets only, which have no header at all, so its address part never
decodes. The third's address part is `0x00`, which is an address like any
other and has to start the clock search like any other. The fourth carries
the payload types the bench does not reliably produce: an AUX1, a DV, an EV3,
and a DH3 claiming a payload longer than one capture bank.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`.

**Commands.** No board. `rxreplay.py` writes the synthetic input and checks
what came back, `stdoutdest.py` runs a command with its standard output and
extra descriptors pointed where the case needs them, and `capturefile.py`,
`rxdump.py` and `bredrrecord.py` read the capture files:

```text
rxreplay.py fixture <case dir>/replay.bin --variant <full|empty|truncated> --base-time <epoch>
ubertooth-rx -i <case dir>/replay.bin                      # the bare decode
ubertooth-rx -i <case dir>/replay.bin -l <LAP>             # and -l -u <UAP>
ubertooth-rx -i <case dir>/replay.bin -l <LAP> -u <UAP>
ubertooth-rx -i <case dir>/replay.bin -l <LAP> -u <UAP> -q <case dir>/payloads.pcap
ubertooth-rx -i <case dir>/replay.bin -e 0
ubertooth-rx -i <case dir>/replay.bin -s                   # the default sweep
ubertooth-rx -i <case dir>/replay.bin -z                   # the survey
ubertooth-rx -i <case dir>/replay.bin -r <case dir>/replay.pcapng
ubertooth-rx -i <case dir>/replay.bin -q <case dir>/replay.pcap
ubertooth-rx -i <case dir>/replay.bin -d <case dir>/replay.dump
ubertooth-rx -i <case dir>/replay.bin -u <UAP>              # -u with no -l
ubertooth-rx -i <case dir>/replay.bin -d <case dir>/no-such-dir/x.bin
ubertooth-rx -i <case dir>/a-directory                      # and a missing file
ubertooth-rx -h
python -c 'import os, sys; os.makedirs(sys.argv[1], exist_ok=True)' \
    <case dir>/a-directory                                  # the directory input
stdoutdest.py run --stdout <case dir>/stdout.pcap -- ubertooth-rx -i <case dir>/replay.bin -q -
stdoutdest.py run --fd 3=<case dir>/fd3.pcap -- ubertooth-rx -i <case dir>/replay.bin -q /dev/fd/3
stdoutdest.py seed|link|unlink <case dir> ...               # the destinations
stdoutdest.py link <case dir> --symlink <name>=<target> --hardlink <name>=<target>
stdoutdest.py check <case dir> --sha256 <name>=<digest> --prefix <name>=<digest>:<size>:<n> \
    --also <name> --absent <name> --kept <name> --symlink <name>=<target> \
    --hardlink <name>=<target>
stdoutdest.py run --stdout <case dir>/<name> --append -- <command ...>
rxreplay.py dump-check <case dir>/replay.dump <case dir>/replay.bin
rxreplay.py files <case dir>                                # the directory as it is
rxreplay.py only-fixture <case dir> replay.bin --base-time <epoch>
rxdump.py      <case dir>/replay.dump full --min-records 19 --types br
capturefile.py --packets <case dir>/replay.pcap
bredrrecord.py <case dir>/payloads.pcap --lap <LAP> --reference-lap <LAP> \
    --reference-uap <UAP> --require-types AUX1,DV/3-DH1,HV3/EV3/3-EV3,DH3/3-DH3 \
    --require-transports ACL,SCO,eSCO --max-payload-bytes 34
```

**Expected outcome.** Smoke runs 14 cases (13 where there is no `/proc`): decode every LAP in the fixture,
derive the UAP with `-l`, replay into PCAPNG and into PCAP (link type 255, the
fixture's packet count), a `-d` file that cannot be opened (exit 1, one error
line, no usage text, nothing created or changed anywhere in the directory),
`-s` decoding the same as without it with `-h` listing `-s` as the default,
the payload types read out of a capture, and `-u 00` starting the clock
search.

Every case that reads packet lines requires each line's `clk_offset` to be
the figure the record's own timestamp calls for, and not merely a number.
libubertooth computes it in `cb_rx` from the record's `clk100ns` and the bit
offset of the sync word within the record, in 100 ns ticks within one slot,
so the fixture reproduces that arithmetic and puts each record's access code
one step further past a CLKN edge than the last. A decoder that reported a
constant, or the same offset for every packet, fails
([harness#357](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/357)).
It is not the CLK1-6 slot offset `btbb_piconet_get_clk_offset()` reports,
which the fork reads only on a live follow
([libbtbb#98](https://github.com/SqueakyFloors/libbtbb/pull/98) is still
uncovered).

**Which note each command line prints.** libbtbb reports a clock out of the
address-part search as a CLK6 note only when it already held the UAP, which is
the UAP a caller stated; a UAP it worked out for itself is reported as the UAP
note in the same place. So the two command lines are checked for different
notes. `-l` alone must print the UAP note and then the start of the CLK1-27
search (`<n> initial CLK1-27 candidates`), because the packet whose CRC
settles a discovered UAP settles CLK1-6 with it and the clock search carries
on from that packet; a CLK6 note there is forbidden, since printing one is the
earlier behavior, where the address-part search ran again on every later
packet, reported CLK1-6 over and over, and reached no clock
([libbtbb#137](https://github.com/SqueakyFloors/libbtbb/pull/137), taken on by
the fork in
[ubertooth#1135](https://github.com/SqueakyFloors/ubertooth/pull/1135)).
Comprehensive's correct `-u` is the other half: there the CLK6 note is
required, at offset 0, with the candidates after it. The `-l` case is the one
that guards against a return to the earlier behavior, and it is in smoke, so
it runs with no board in CI; the `-u` case prints the same notes under either
library and could not tell them apart
([harness#341](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/341)).
What the `-l` case can no longer pin is the settled CLK1-6 itself: nothing it
prints names that offset, since the note that carried it is the one this
command line does not print, and the candidate count is matched as `<n>`
rather than as a figure of the library's own. The `-u` case is where offset 0
is still required.

**The payload types, and a payload longer than the bank.** One smoke case
follows the fourth piconet (`-l <LAP> -u <UAP> -q`) and reads the capture it
writes record by record
([`bredrrecord.py`](../ubertooth_harness/bredrrecord.py)), which is how the
payload decoders libbtbb
[#28](https://github.com/SqueakyFloors/libbtbb/pull/28) added are exercised
on packets the bench's own traffic does not produce. Four records, each for
one reason:

- the AUX1 is the one type with a payload header and no CRC after the body
  (Core, Vol 2, Part B, Section 6.5.4.7), so its length field has to be read
  without counting two bytes that are not there, and a record that reports a
  checked CRC on one fails;
- the DV's record holds its data field and not the voice field ahead of it,
  so its length field and its CRC land on the right bytes, and its transport
  is the synchronous one;
- the EV3's length is in no field at all and is found by trying the CRC at
  each, and its transport is the extended synchronous one;
- the DH3 claims a payload no 400-symbol bank can hold, so the decoder must
  report no payload rather than decode one out of whatever followed the
  packet in the bank. A decoder told it had ten banks decoded a payload and a
  CRC verdict out of the buffer's zeros
  ([ubertooth#754](https://github.com/SqueakyFloors/ubertooth/pull/754)).

The case requires one record of each type, the ACL, SCO and eSCO transports
between them, three records carrying a payload, two passing a CRC, and no
record carrying more payload than one bank holds -- 34 bytes, the 400 symbols
of a bank less the 122 the access code and header take, where a payload
carries no error correction.

**A piconet whose address part is `0x00`.** The other new smoke case states
`-u 00` for the third piconet and requires the CLK6 note at offset 0 and the
start of the CLK1-27 search, as for any other address part. A piconet given
`0x00` read it as an absence and never reached the clock search
([libbtbb#135](https://github.com/SqueakyFloors/libbtbb/pull/135), taken on
by the fork in
[ubertooth#1125](https://github.com/SqueakyFloors/ubertooth/pull/1125));
every other case states the first piconet's `0x6b`, which took the same
branch before and after.

Six of the smoke cases are about where a capture goes
([`stdoutdest.py`](../ubertooth_harness/stdoutdest.py) puts the descriptors
where the case needs them, since a harness step otherwise runs with the
harness's own streams). `-q -` writes the capture to the stream the case
redirected, which is then read back as a PCAP of the fixture's packet count,
and the packet report appears on neither stream: the capture owns standard
output, so the report is printed nowhere at all and nothing can land in the
middle of a capture. `/dev/stdout` and `/dev/fd/1` mean the same stream, and so does
`/proc/self/fd/1` on Linux, which is the one case a host without `/proc`
leaves out -- it is in smoke so that CI's Linux build job runs it against the
real tool. `/dev/fd/3` does not: it is whatever the caller opened there, so the capture
goes to that file and the report stays on standard output. `-q` and `-r`
naming one file are refused before either is opened, naming both options, and
a `-d` that names the dump `-i` is replaying is refused as
`error: -d would write over the dump -i is replaying (<name>)`. Each of the
six ends by stating the whole directory, with the input's SHA-256, so a
refusal that truncated or rewrote the file it was reading fails.

Comprehensive runs 32 (31 where there is no `/proc`), adding the survey
listing of every piconet in the fixture
(the four addresses on standard output and the `Survey Results` heading on
standard error, with the address part shown for the three the survey settled
one for -- `00` among them, as an address and not an absence -- and `??` for
the ID-packet piconet),
a LAP filter, a correct and a wrong `-u`, `-e 0` dropping the packet with an
error, a `-d` dump compared byte for byte with the fixture, a filtered PCAP,
an empty file, a file whose last record is cut short (exit 1 after the whole
records), a missing file, the recorded systime on every packet line, a
directory passed as `-i` (exit 1 with the read error), and `-u` without `-l`
refused before any record is decoded.

Comprehensive also adds five more capture-destination cases.
One file under two spellings is refused as
`error: -q and -d cannot both write to one file (<first> and <second>)`, while
`/dev/null` twice is accepted and decodes, since nothing is kept there. The
file is identified and not the name for that pair check too, so a symbolic
link, a hard link, `-` beside the name the shell pointed that stream at, and
`/dev/fd/3` beside the file the case opened there are each refused as one
file under two names, each named as the user wrote it; two captures that both
ask for that stream belong to the one-capture-on-standard-output rule instead
and are refused by it (`error: -q and -d cannot both write to standard
output`). `-r` refuses every
destination that reaches standard output while the command line is read, `-`,
`/dev/stdout`, `/dev/fd/1`, and a link to one of them alike, with
`error: PCAPNG capture to stdout is not supported; use -q -`, because PcapNG
seeks back over what it has written. A symbolic link and a hard link to the
`-i` input are both refused, naming the link the user wrote, with the links
still what they were. And with standard output pointed at the input, `-q -`
is refused with the input untouched while the bare decode, which no option
names and nothing can refuse by then, exits 0 with
`warning: the decode is going to the dump -i is replaying, so the capture is
lost` and leaves the fixture's own records at the start of the file. That
last case reads its own standard output back as a file, so it holds while the
tool's standard output stays buffered until it exits, which it does for the
nineteen packet lines the fixture decodes to; a report long enough to flush
part-way through would be read as a record cut short and the run would exit 1
instead of 0.

**Known limits.** The fixture is synthetic, never an over-the-air capture, so
this covers the decode path and not the receive path. The low-energy
equivalent is [BLE replay](#ble-replay), which replays a synthetic dump
through `ubertooth-btle -R`: that tool reads its own dump, and neither
library has a low-energy file reader for a tool to call.
The capture-destination
cases are `ubertooth-rx`'s; the same checks in `ubertooth-btle` and
`ubertooth-follow` are not covered, and nor is a capture written to a
terminal, which no run has. Five more things the fork's own code calls out go
uncovered: `/dev/stdin` and `/dev/stderr`, which are not standard output but
are still identified as the descriptors they name (so
`-d /dev/stderr 2>>cap.bin` reaches a replay's own dump); the BSD leading-zero
spelling `/dev/fd/01`, which is descriptor 1 there and names nothing on Linux;
`/proc/self/fd/1` as an `-r` destination, which the `-q` cases cover and the
`-r` case does not;
a destination whose last element is a number, which is a file and not a
descriptor unless the descriptor filesystem answers for it; and the backstop
inside `take_stdout`, which no case reaches because the command-line check
refuses such a pair first.

## BLE replay

**What it does.** Replays a synthetic low-energy dump with
`ubertooth-btle -R`, with no Ubertooth, no USB and no connection.

**Theory of operation.** `-d <file>` saves the records the board sent and
`-R <file>` decodes them again, taking the major mode the dump was recorded
with: a record does not say which clock stamped it, and the modes stamp their
packets differently, so the mode is asked for rather than guessed (fork
[ubertooth#1129](https://github.com/SqueakyFloors/ubertooth/pull/1129)). That
makes a replay the one path on which this tool's decode has a known answer
with no radio at all, because the harness writes the input
([`btlereplay.py`](../ubertooth_harness/btlereplay.py)). A low-energy record
carries the packet's own bytes rather than symbols -- the access address, the
PDU header, the payload and the CRC -- so the dump is written field by field
from the Bluetooth Core specification's encoding and the expected report is
computed from the same constants.

Three dumps. The first is a promiscuous capture of one connection: the five
parameter records promiscuous mode sends as it works out which connection it
has found, the `CONNECT_IND` that opened that connection on an advertising
channel, and two data packets on the connection's own access address. The
parameter records carry no packet, so they reach no capture file and a dump
is the only way to keep them -- which is what `-d` and `-R` are for. The
second is one `ADV_IND` whose advertising data holds three Manufacturer
Specific Data fields, so the decode reads the company identifier table three
ways: an identifier the Bluetooth SIG registered recently
([libbtbb#75](https://github.com/SqueakyFloors/libbtbb/pull/75)), one it has
had from the start, and one it has never assigned, which libbtbb reports as
`not assigned`
([libbtbb#128](https://github.com/SqueakyFloors/libbtbb/pull/128)). The other
reader of that table is `ubertooth-scan -x`, which needs BlueZ, a board and a
third idle Classic device, so the dump is the cheap way to it. The third is
that same advertisement at three of the board's readings, which is the one
way to the level a packet is reported at without a radio.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`.

**Commands.** No board. `btlereplay.py` writes the synthetic dumps, computes
the report each one should produce, and reads back the records and the levels
a replay saved:

```text
btlereplay.py fixture <case dir>/<name>.bin --variant <connection|advertising|levels> --base-time <epoch>
ubertooth-btle -p -R <case dir>/connection.bin             # the promiscuous dump
ubertooth-btle -n -R <case dir>/advertising.bin            # the advertising one
ubertooth-btle -n -R <case dir>/levels.bin -q <case dir>/levels.pcap
btlereplay.py level-check <case dir>/levels.pcap
ubertooth-btle -p -R <case dir>/connection.bin -d <case dir>/again.bin
btlereplay.py dump-check <case dir>/again.bin <case dir>/connection.bin
ubertooth-btle -R <case dir>/connection.bin                # refused: no mode
ubertooth-btle -h
```


**Expected outcome.** Smoke runs three cases. The promiscuous replay
(`-p -R <dump>`) begins by requiring `ubertooth-btle -h` to list `-R` and
`-d`, so a build too old to have them fails saying which option is missing
rather than on a refusal the checks cannot parse. It must then print the
five parameter lines in the order the records hold them -- access address, CRC seed, hop interval, hop increment, and the
algorithm 2 event counter -- then the `CONNECT_IND` decoded field by field:
the connection's access address, CRC seed, transmit window size and offset,
interval, peripheral latency, supervision timeout, channel map, hop increment
and clock accuracy. Then the two data packets, each with its LLID and, for
the control PDU, its opcode. Every packet line must carry the host time of
its own record and the access address it belongs to, so a replay reporting
its own clock fails. The advertising replay (`-n -R <dump>`) must name both
registered companies and say `not assigned` for the third, in that order. A
missing or reworded line fails the case, and the step names the first line it
could not find.

**The level a packet is reported at.** The third smoke case replays the three
readings into a PCAP and checks both the lines and the records. The two
readings the board can measure must come out as two different figures: the
level used to be the loudest reading of the last ten records of the channel,
so a quiet packet after a loud one read as loud as the loud one
([ubertooth#867](https://github.com/SqueakyFloors/ubertooth/pull/867)). The
third reading is at the top of the radio's scale, where every reading gives
the same dBm figure and so none of them is a measurement of the packet: the
line must read `rssi=none`
([ubertooth#1192](https://github.com/SqueakyFloors/ubertooth/pull/1192),
[#1198](https://github.com/SqueakyFloors/ubertooth/pull/1198)). The word is
matched as the word, so a build printing 0 dBm there -- which the low-energy
column did until those changes -- fails rather than passing as "very loud".
The capture's records are then required to carry the dBm figure the host's
model of the radio gives each of those readings, 0 dBm included where the
line said `none`: a record has no field for "not measured", and that
asymmetry between the line and the record is what the case pins. The noise
flag must be set on the one record written with a background estimate and
clear on the two without, which is the rule every low-energy capture this
harness reads is held to (below).

Comprehensive adds two more. A replay dumping with `-d` must write the dump it
read back byte for byte, the parameter records included, since a `-d` that
dropped them would lose what a promiscuous capture was for. And a replay with
no major mode must be refused with exit 1 and decode nothing:
`error: -R needs -f, -n or -p: a dump does not record which mode wrote it,
and the modes stamp their packets with different clocks`.

**Known limits.** The dumps are synthetic, so this covers the decode and the
dump format, not the receive path, the radio, or the firmware that fills a
record in. Nothing here verifies a CRC: the firmware is what checks one and
sets the record's flag, so the fixture's CRC bytes are zero and the decode
never looks at them. The company names are the pinned libbtbb table's, so a
regenerated table that renames a company fails the case; that is a one-line
update in `btlereplay.py`. The rest of the fork's own `-R` and `-d` surface
goes uncovered here -- a destination that is the dump being replayed, the
capture files a replay writes, a dump cut short or unreadable, and the
spellings of standard output -- which the fork's own
`host/ubertooth-tools/tests/test_btle_replay.py` covers at build time; the
same checks for `ubertooth-rx` are in
[Raw receive replay](#raw-receive-replay).

## Refused command lines

**What it does.** Refuses a command line that names output files and requires
that no file was created, truncated, or left behind.

**Theory of operation.** Each case seeds its own directory with files of known
contents, then runs two refused commands: one naming a file that does not
exist and one naming a seeded file. Afterward the new file must not exist, the
seeded file must be byte-identical, and nothing else may have appeared. That
is a complete statement about the directory, not a check of one name, so a
temporary file left behind fails too. The refusal must also come before the
board lookup, which the `UBERTOOTH_TEST_NO_DEVICES=1` switch makes testable:
if the command got as far as looking for a board it prints a note saying so,
and that note is forbidden in a refusal.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`. The
`ubertooth-follow` and `ubertooth-scan` cases declare that they need BlueZ, so
they are left out on a host without it.

**Commands.** No board: every tool runs as
`env UBERTOOTH_TEST_NO_DEVICES=1 <tool> ...`, so a command that got past its
own checks finds no board instead of opening one, and `refusal.py` states the
directory before and after:

```text
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle   -r <case dir>/new.pcapng <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle   -q <case dir>/new.pcap <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle   -c <case dir>/new.pcap <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx     -r|-q|-d <case dir>/new.<ext> <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dump   -d <case dir>/new.dump <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dump   -d <case dir>/new.dump -c -l
                                        # and -l -c: refused naming both,
                                        # a capture uses one modulation
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-specan -d <case dir>/new.specan <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-specan -d <case dir>/missing/new.specan
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu    -s|-u <case dir>/<file> <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-follow -r|-q|-d <case dir>/new.<ext> <refusal>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-scan   stray        # BlueZ builds, no file
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-scan   -- -x        # and -h stray, and a bare -
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-follow stray        # BlueZ builds, no file
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-follow -- -x        # and -h stray, and a bare -
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle -n -q - -c <case dir>/extra.pcap
                                        # accepted: the extra file is warned
                                        # about on stderr and never created
refusal.py probe <tool>                   # opens every case: can the switch be used
refusal.py seed <case dir> <names>        # the files a case must find unchanged
refusal.py files <case dir>               # the cleanup: the directory as it
                                          # ends up, after the check
refusal.py check <case dir> --absent <files> --kept <files>
```

`<refusal>` is what the case refuses the command with: a stray word; `-h`;
the same file option twice; a bare `-`; `--` followed by an option; and a bad
later value, which is each tool's own (`btle -A 40`, `rx -c 79`,
`rx -u 12`, `rx -z -l <LAP>`, `specan -l 9999`, `specan -l <high> -u <low>`,
`dump -U` with no value, `dfu -s <other>`, `dfu -d <bad image>`,
`follow -e 9`, `btle -f -n`, `btle -a -a<address>`, `btle -r -`). The scan
and follow lines take the four that need no file: a stray word, `-h stray`,
a bare `-`, and `-- -x`.

**Expected outcome.** On a Linux host, smoke runs 45 cases and comprehensive
67; a macOS host runs 12 fewer in smoke, since the BlueZ-only cases are not
applicable. Every file option of `btle`, `follow`, `rx`, `dump`, `specan`, and
`dfu` is refused three ways: with a stray word, with `-h` (which exits 0 and
must still create nothing), and with a bad option value later on the line.
Beyond that: the extra-capture-file note goes to stderr, so it cannot land in
a capture written to stdout; a repeated file option is refused with
`error: option '-r' given more than once` before the stray word and before any
board lookup; `dump -d` with `-c` and `-l` in either order is refused with
`a capture uses one modulation`, naming both options as they were written,
before the dump file is opened; and `scan` and `follow` refuse a stray word
with no usage text after the message.

**Known limits.** Forks from before the no-devices switch ignore it, so the
first step of every case reads the built library's bytes and the case skips
when the name is absent. That is a run-time skip because only the built
library can tell. The test says nothing about what the tools do once a command
line is accepted.

## Failed capture files

**What it does.** Drives each capture-output path into a controlled failure and
requires that nothing a user would later read as a capture is left behind, and
that what the run did decode is kept as a file a reader can open.

**Theory of operation.** An empty capture file is worse than no file: a user
cannot tell it apart from a capture that ran and caught nothing. Every tool
creates its capture files once the board is open and usable, so a run refused
before that leaves none; `ubertooth-rx` and `ubertooth-follow` go further,
doing every step that can fail first and taking the files back again if the
run fails after creating them. The line they are kept from is the first whole
record a replay read, whatever the decoder made of it, because from there the
file holds evidence. Two ways in make all of that checkable with no board at
all. `UBERTOOTH_TEST_NO_DEVICES=1` lets a command
line be accepted and then stopped at the board lookup, which is the setup
failure. `ubertooth-rx -i <fixture>` replays a synthetic file
([`rxreplay.py`](../ubertooth_harness/rxreplay.py)) with no board and no USB,
and is the one path on which a capture file is really created, written to, and
then judged: the `partial` fixture is one record cut short, so the run fails
with its files created and nothing in them yet; the `truncated` fixture holds
whole records and then a cut-short one, so the same failure arrives after real
packets; and the `skipped` fixture holds one whole record the decoder skips
and then a cut-short one, which is the line itself: a record was read and no
packet came of it, and the files still stay. Each case states its whole directory before the command
and again afterwards (`dfuimage.py state`), so a temporary file, a truncated
destination, or a file removed that should have stayed fails the case; a file
a case does leave stays in the run directory under the name of the attempt
that wrote it.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`.

**Commands.** No board. A command that is accepted and then stopped at the
board lookup must create no file; a replay really writes one, from an input
`rxreplay.py` builds. `dfuimage.py` states the directory before and after, so
a file created, truncated, or left behind is caught; `mkdir` and `chmod` make
the read-only directory the cases need and `ln -s` the symbolic link, and
`dfuimage.py symlink` is the check that the link still points where it did.
`capturefile.py` reads what was kept:

```text
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -r <case dir>/new.pcapng \
    -q <case dir>/kept.pcap -d <case dir>/new.dump
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle -n -r <case dir>/new.pcapng -q <case dir>/kept.pcap
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-btle -n -c <case dir>/new-ppi.pcap
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dump -d <case dir>/new.dump      # and a seeded file
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/<fixture>.bin \
    -r <case dir>/new.pcapng -q <case dir>/new.pcap -d <case dir>/new.dump
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/no-record.bin -q <destination>
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/input.d -r <case dir>/new.pcapng \
    -q <case dir>/new.pcap -d <case dir>/new.dump   # a directory as the input
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/replay.bin -q <case dir>/in-the-way
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/replay.bin -q <case dir>/ro/x.pcap
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/replay.bin -q <case dir>/gone/x.pcap
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -i <case dir>/replay.bin -r <case dir>/gone/x.pcapng
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-rx   -q -                     # no -i: the board lookup
rxreplay.py fixture <case dir>/<name>.bin --variant <full|partial|skipped|truncated> \
    --base-time <epoch>
rxreplay.py dump-check <case dir>/new.dump <case dir>/<name>.bin --variant <skipped|truncated>
mkdir <case dir>/ro                            # read-only, and in-the-way,
                                               # input.d: a directory where a
                                               # file is expected
ln -s target.pcap <case dir>/link.pcap         # the link a case must not follow
dfuimage.py symlink <case dir>/link.pcap --to target.pcap   # it still points there
dfuimage.py state <case dir>                   # before the command, and after
dfuimage.py state <case dir> --before <directory before> --allow <files>
dfuimage.py not-root
chmod 555|755 <case dir>/ro
refusal.py probe <tool>
refusal.py seed <case dir> <names>
capturefile.py --packets <case dir>/new.pcap
```

`<fixture>` is each input the three-destination replay is given, which is
where the three files are taken back or kept together: `no-record.bin` (the
`partial` variant, which reads no whole record), `skipped-record.bin`
(`skipped`) and `tail-cut.bin` (`truncated`), which do. `replay.bin`, the
`full` variant, is written out above on the lines that give one destination.
`<destination>` is what the
`no-record.bin` line gives one `-q`: a symbolic link, and `/dev/null`. The
three directory destinations have lines of their own above, the seeded ones
belong to the `ubertooth-btle` and `ubertooth-dump` lines, and `-q -` to the
no-input case.

**Expected outcome.** Smoke runs 6 cases: `ubertooth-rx -r -q -d`,
`ubertooth-btle -n -r -q`, and `ubertooth-dump -d` stopped at the board lookup
(exit 1, the error on stderr, nothing on stdout, and no new file created; the
`rx` and `btle` cases give one of their options the seeded file, so they also
require that a destination the user already had is not truncated); a replay that reads no
whole record taking back all three files it created; a replay that reads
records and then meets a cut-short one keeping all three, the two captures
readable and the dump compared with the fixture records it came from, byte
for byte; and a PCAPNG destination in a directory that
is not there, reported as `error: <file>: No such file or directory` with
nothing created. Comprehensive runs 16, adding the same lookup case for
`ubertooth-btle -c`, which is its own create call, and one for
`ubertooth-dump -d` on the seeded destination, which has only the one capture
option to give it; the same missing directory
for a PCAP destination; a replay whose one whole record the decoder skips,
which keeps its files all the same, with the two captures holding no packet
and the dump empty; a capture bound for standard output whose
failure must stay on standard error; a read error before any record; a
destination that is an existing directory; a destination in a read-only
directory; and the two paths a user arranged rather than a file the run may
remove, a symbolic link, which must still be there and still name its target,
and `/dev/null`. The last two cases are skipped when the run is root, where a
wrong removal would succeed in silence. No case expects
`warning: cannot remove`, so a run that reached for a path it should have left
alone fails.

**Known limits.** Only the host side. A writer that fails *while* writing its
header, which is what libbtbb
[#117](https://github.com/SqueakyFloors/libbtbb/pull/117) and
[#119](https://github.com/SqueakyFloors/libbtbb/pull/119) take the file away
for, needs a destination that opens and then refuses a write; nothing here
produces one, so the removal is checked on the create and decode failures
instead. `ubertooth-follow`'s own discard
([ubertooth#1000](https://github.com/SqueakyFloors/ubertooth/pull/1000)) is
not covered: the tool is built only with BlueZ, and it has no replay path, so
reaching its capture files needs the HCI adapter the bench does not have.
`ubertooth-btle` and `ubertooth-dump` appear only in their lookup cases,
because neither takes a capture file back: for them the test states the
ordering, not a removal. `ubertooth-specan -d` with no board
is checked by Refused command lines (`specan-d-unopenable`) and the `-d`
writer in a missing directory by Raw receive replay
(`replay-dump-unopenable`), so neither is repeated here.

## Util mode refusals

**What it does.** Refuses `ubertooth-util` command lines that combine the ISP
mode with another mode or with `-z`, and the removed `-f`, with no board
attached.

**Theory of operation.** `-i` leaves the application firmware, so the harness
never runs it against a board; the refusal is what has to be proven, and it
can be proven with no board at all. The expected message names the given modes
in the tool's own fixed order, so a refusal that names them in the wrong order
or gives the wrong one fails. Running under
`UBERTOOTH_TEST_NO_DEVICES=1` means a regression that accepted a combination
would find no board rather than stranding one, and the "no board is selected"
note is forbidden, so an accepted combination is visible.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`.

**Commands.** No board. Every case opens with the probe, which reads the
built libubertooth and runs no tool, and these create no files, so there is
no directory to state afterwards:

```text
refusal.py probe ubertooth-util
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -i -<mode>     # and -<mode> -i
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -i -z          # and -z-60, both orders
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -f             # and -f -i
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -N -i          # and -i -N
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -U all -i
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -x -i -q       # and -i -q -x, -i -i -x
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -z -x -i
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -x -i stray    # the stray word first
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util -V             # the cleanup, needs no board
```

`-<mode>` is each of `-q -n -e -t -x -I`.

**Expected outcome.** Smoke runs 8 cases: `-i` with each of `-q`, `-n`, `-e`,
`-t`, `-x`, and `-I` (each in both orders), `-z` with `-i`, which must be
refused by the `-z` rule, and `-f`, which must be refused as an unrecognized
option. Comprehensive runs 14, adding three modes at once, a repeated mode,
`-z` with two modes (where the mode refusal comes first), a stray word, `-N`
with `-i` (where `-N` prints the count and the command carries on to `-i`,
which finds no board and says `error: no Ubertooth devices found`, the line
every tool gives for a selection that found nothing since fork `753269b`), and
`-U all` with `-i`, which gives the `-U all` refusal alone.

**Known limits.** As with `Refused command lines`, a fork from before the
no-devices switch makes the case skip. Nothing here shows what `-i` does when
it is accepted.

## DFU image checks

**What it does.** Checks `ubertooth-dfu -d` image validation and `-s`
suffixing with no board attached.

**Theory of operation.** `-d` validates the whole image before opening a
device, so a bad image has a known answer that needs no hardware: exit 1 with
one line naming the file, what is wrong with it, and that the firmware was
left alone. Each bad image gets its own expected reason rather than a shared
one, so a tool that rejects everything for the wrong reason fails. The images
are derived from a deterministic synthetic image the harness builds itself, so
the right answer is computed, including the suffix and CRC that `-s` must
produce byte for byte. An accepted image must go on to the board lookup and
fail there, which is checked by requiring the note that no board is selected,
the opposite of what the refusal cases forbid.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`. One case
compares `-s` against the firmware build's own `.dfu`; it declares that it
needs the firmware build, so a run without `--build --with-firmware` or a test
build artifact leaves it out as not applicable. A run started through the
controller or the GUI asks for the image with its own `with_firmware` option,
which the GUI ticks for a suite holding this test.

**Commands.** No board, and a synthetic image rather than a firmware build
(CI has no ARM toolchain). `dfuimage.py` makes each bad image from it and
states the directory before and after, and `refusal.py probe` opens every
case by reading whether the built libubertooth has the no-devices switch:

```text
dfuimage.py synth <case dir>/synthetic.bin <case dir>/synthetic.dfu
dfuimage.py make <variant> <case dir>/synthetic.dfu <case dir>/<variant>.dfu
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu -d <case dir>/<variant>.dfu -r
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu -d <case dir>/synthetic.dfu -r   # reaches the lookup
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu -s <case dir>/image.bin
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu -s <case dir>/synthetic.dfu      # refused
env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-dfu -s <case dir>/image.bin \
    -d <case dir>/synthetic.dfu -r     # signs, then stops at the board lookup
dfuimage.py check-suffix <case dir>/image.bin <case dir>/image.dfu <reference> \
    --reference-name <name>
dfuimage.py copy <case dir>/synthetic.bin|synthetic.dfu <case dir>/<name>
dfuimage.py same <case dir>/target.dfu <case dir>/synthetic.bin
dfuimage.py strip <case dir>/synthetic.dfu <case dir>/readonly.bin --read-only
dfuimage.py strip <test image> <case dir>/build.bin
dfuimage.py regular <case dir>/image.dfu [--mode 600]
dfuimage.py state <case dir>|<case dir>/ro [--before <directory before>] \
    [--allow <file>]
dfuimage.py not-root
mkdir <case dir>/ro                # read-only, and folder.bin, folder.dfu:
                                   # a directory where a file is expected
ln -s target.dfu <case dir>/image.dfu   # the .dfu link a signing replaces
chmod 555|755 <case dir>/ro        # read-only for the signing, back afterwards
chmod 600 <case dir>/image.dfu     # the mode a replacement must keep
refusal.py probe ubertooth-dfu     # opens every case: can the switch be used
```

Two forms stand outside `<variant>`: `-d <case dir>/missing.dfu -r` and
`-s <case dir>/missing.bin`, which name a file that is not there, and
`dfuimage.py make corrupt <case dir>/synthetic.dfu <case dir>/corrupt.bin`,
which writes a `.bin` rather than a `.dfu`.

`<variant>` is each image `dfuimage.py make` builds from the synthetic one.
Those the check must refuse: a wrong CRC, a truncated file, a binary with no
suffix, a text file, a suffix without `UFD`, another vendor, another product,
and, comprehensive, an empty file, 15 bytes, a suffix with no firmware, an
over-long suffix length, DFU 2.0, and an image with no firmware info block.
Those it must accept, comprehensive: DFU 1.1 and the `ffff:ffff` wildcard,
which reach the board lookup like the synthetic image itself.

**Expected outcome.** Smoke runs 13 cases: `-d` refusing a wrong CRC, a
truncated image, a binary with no suffix, a text file, a suffix without the
UFD signature, another vendor ID, and another product ID; `-d` accepting the
good synthetic image and reaching the board lookup; `-s` suffixing a binary as
the harness computes it; `-s` of the test build's binary matching the build's
`.dfu`; `-s` refusing a `.dfu` and refusing a `.bin` that already carries a
suffix; and `-s` of a read-only input. Comprehensive runs 31, adding edge
images (empty, 15 bytes, a suffix with no payload, a suffix length larger than
the file, DFU version 2.0, no firmware info block, a directory, a missing
file), the accepted DFU 1.1 and wildcard suffixes, inputs `-s` cannot use, and
how `-s` puts its output in place: an unreadable input leaves an existing
`.dfu` alone, a `.dfu` symlink is replaced rather than written through,
permissions are kept, a read-only directory refuses the signing, no temporary
file is left, and `-s` with an accepted `-d` signs before the board lookup
fails. Every refusal case requires the case directory, image included, to be
exactly as it was.

**Known limits.** No board, so nothing here covers writing firmware;
`DFU images` and `Flash reliability` do that. The synthetic image reports a
firmware API version the tools do not speak, so `-d` warns that the two
differ; that warning is left alone rather than pinning the fork's current
number.

## Python tools

**What it does.** Runs the fork's Python tools in the ways that need no board:
`ubertooth-specan-ui -h` and the `btle-extcap` listings.

**Theory of operation.** `ubertooth-specan-ui` imports numpy and a Qt binding
before it parses arguments, so `-h` succeeding headless proves the imports
resolved and the parser works, which is as far as a GUI can be tested without
a display. The `btle-extcap` listings have fixed, machine-readable answers,
because Wireshark parses them: the DLT line and the advertising-channel
selector with 37 as the default. That makes them exact comparisons rather than
loose matches.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`. The
`ubertooth-specan-ui` cases declare that they need the Python tools build, so
a run without `--build --with-python` (or, through the controller and the
GUI, its `with_python` option) leaves them out as not applicable; they
are additionally skipped at run time when the interpreter cannot import numpy,
qtpy, and a Qt binding, which only running it can tell.

**Commands.** No board. `pytools.py` runs the installed
`ubertooth-specan-ui` with no display, and `btle-extcap.py` exactly as it is
in the resolved commit:

```text
pytools.py specan-ui -h
pytools.py extcap -h                  # and -h extra
pytools.py extcap --extcap-dlts       # and --extcap-interface=<interface> --extcap-dlts
pytools.py extcap --extcap-config     # with each interface the cases try
pytools.py extcap --extcap-interface=<interface> --capture      # no --fifo
pytools.py extcap --extcap-interface=<interface> --capture --fifo=unused.fifo --channel=40
pytools.py extcap --extcap-bogus      # an unknown option
pytools.py extcap --extcap-reload-option=x --extcap-config
pytools.py extcap --extcap-control-in=x --extcap-config
pytools.py extcap --extcap-control-out=x --extcap-config
pytools.py extcap --extcap-config-option-name=x --extcap-config
pytools.py extcap --extcap-version=4.2.0 ...                    # and 4.4.1 as a next word
pytools.py extcap --extcap-version ... --extcap-version         # with no value, twice
pytools.py extcap-failing-util --extcap-interfaces
```

`<interface>` is `ubertooth<serial>`, the older `ubertooth0`, and a
non-Ubertooth name.

**Expected outcome.** Smoke runs 4 cases: `ubertooth-specan-ui -h` listing its
real options, `btle-extcap -h` naming its real options, `--extcap-dlts`
listing DLT 256, and `--extcap-config` offering channels 37-39 with 37 as the
default. Comprehensive runs 20, adding an older `ubertooth<index>` interface
name, the refusals when no interface is given or the interface is not an
Ubertooth, `--capture` without `--fifo` refused before `ubertooth-btle` runs,
an unknown option refused with `error: unrecognized option '--extcap-bogus'`
and no usage text after it (the wording every tool here shares since fork
`5310a5d`), `--extcap-version` accepted and ignored in all three forms
Wireshark uses, and the options Wireshark sends only for features this plugin
does not advertise, which must be refused like any unknown option.

**Known limits.** No display and no board, so the spectrum GUI itself is not
exercised; its argument checks are still untested. Nothing here shows that
Wireshark accepts the output, only that it has the documented shape.

## BLE extcap interfaces

**What it does.** Requires `btle-extcap --extcap-interfaces` to list every
attached Ubertooth by serial.

**Theory of operation.** The plugin builds its listing from
`ubertooth-util -U all -s`, and the harness knows every registered serial on
the host, so the expected listing is known: one `interface` line per board,
each naming that board's serial in both the value and the display name, and no
extra lines. Listing order is the USB index order, which the registry does not
know, so the check requires the right set of serials in a complete listing
rather than a particular order.

**Hardware and arguments.** One `Ubertooth` plus every other registered board
as guards. Guarded. `coverage:`.

**Commands.** `btle-extcap.py`, through `pytools.py`, which opens every
attached board:

```text
pytools.py extcap --extcap-interfaces
pytools.py extcap --extcap-interfaces --extcap-version=4.2.0
pytools.py extcap --extcap-version 4.4.1 --extcap-interfaces
pytools.py extcap --extcap-interfaces extra        # refused, no listing
```

**Expected outcome.** Smoke runs 3 cases: the listing has one line per
attached board, each line carries its board's serial, and the listing is
unchanged when `--extcap-version=4.2.0` is added, as Wireshark adds it.
Comprehensive runs 5, adding `--extcap-version` with the value as the next
word and a stray word, which must be refused with nothing listed.

**Known limits.** It checks the listing, not that a capture from any listed
interface works; `BLE extcap capture` does that. An attached board whose
serial cannot be read fails the test rather than being passed over, even when
it is not a registered board: it is left out of the listing, and the call
still exits 1 for it. Wireshark throws away everything a call that failed
printed, so one board it cannot open costs the user the capture list
entirely, which is why the behavior is worth pinning. A real run
always uses the script out of the resolved fork commit, so what the unit
tests pin is the pairing of that script with the `ubertooth-util` of the same
era: a mismatch lists nothing, which is what a host with no board also looks
like. Nothing compares the vendored copy against the fork's own script, so
the fork moving to a third listing form would go unnoticed here until a run
failed.

## BLE extcap capture

**What it does.** Runs `btle-extcap --capture` into a FIFO, reads the FIFO the
way Wireshark would, and stops the script with SIGTERM.

**Theory of operation.** The plugin's whole job is to hand Wireshark a PCAP
stream on a FIFO, so reading that FIFO as a consumer is the only check that
covers what Wireshark sees: the header's DLT must be 256, and the stream must
keep coming until the script is stopped. Driving it from a reader also makes
the two documented ways a capture ends testable: SIGTERM, and the reader
closing the FIFO, which must both exit 0. The `--channel` option is checked
through the board rather than the output, since the channel reaches
`ubertooth-btle`.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:` and a required `duration:`. The Ubertooth defaults to
needing an ordinary link.

**Commands.** `extcapcapture.py` does what Wireshark does: it creates the
FIFO, reads it, and stops the script with SIGTERM after `duration`:

```text
extcapcapture.py --seconds <duration> --fifo <case dir>/extcap.fifo \
    --board <serial> --channel 37 \
    -- --capture --extcap-interface=ubertooth<serial> --fifo=<case dir>/extcap.fifo
```

and these, where the driver's own options say what the capture must do:

```text
extcapcapture.py ... --extcap-capture-filter <filter> -- --capture ...
extcapcapture.py ... --close -- --capture ...          # the FIFO closed early
extcapcapture.py ... --refused -- --capture ...        # a serial no board has
extcapcapture.py ... --refused --no-btle -- --capture ... --channel=40
```

with `--channel=37|38|39` passed on to the script. The script itself runs
`ubertooth-btle -f -U <serial> -q <fifo> -A <channel>`.

**Expected outcome.** Smoke runs 1 case: a DLT 256 PCAP stream on the FIFO
until SIGTERM, with no process left behind, on the default advertising channel
37. Comprehensive runs 8, adding each of channels 37, 38, and 39 explicitly, a
serial no board has (the script must end non-zero with no packet records),
`--channel=40` (exit 1 before `ubertooth-btle` runs), a capture filter that
must be accepted with a warning that it is not applied, and a capture ended by
the reader closing the FIFO, which must exit 0 by itself.

**Known limits.** No peer, so the stream may carry no packet records at all;
the checks are about the header, the stream, and the exit. Nothing here runs
real Wireshark or tshark: `BLE extcap in tshark` does that, with a known
advertiser.

## BLE extcap in tshark

**What it does.** Real `tshark` captures through the `btle-extcap` plugin from
a registered nRF52840 DK advertiser, and its own dissector decodes the DK's
address.

**Theory of operation.** The cases above play Wireshark's part themselves and
check the stream with the harness's own parser, which could share a misreading
with the plugin. A dissector written by someone else cannot: if Wireshark
prints the DK's address, then link type 256 and libbtbb's 10-byte LE
pseudo-header are read the way the plugin writes them, and the calls that
Wireshark version makes to an extcap program are the ones the plugin answers.
The known advertiser is what makes the answer exact rather than "something was
decoded": the DK advertises from its registered address, so the row tshark
must print is known before the capture starts. The plugin is installed in a
Wireshark configuration of its own (a temporary `HOME`, with every directory
Wireshark takes from the environment pointed inside it, and the extcap
directory that tshark itself reports for that environment), so the host user's
configuration is untouched; the installed program records every call it gets,
so what answered for the interface is known to be it and not another extcap
program on the host. tshark runs in a session of its own, so the SIGINT case
reaches tshark alone: sent to the process group it would also hit the
`dumpcap` that reads the FIFO, and tshark would report a child that died
instead of stopping its capture. The SIGINT goes in once a row has been
decoded, for the same reason: that is the proof the capture is under way, and
a signal that lands earlier ends the run as a child that died.

**Hardware and arguments.** One `Ubertooth`, selected by full serial, and one
`advertiser` (nRF52840 DK) with a registered J-Link serial and BLE address.
Guarded. `coverage:` and a required `duration:`. The Ubertooth defaults to
needing an ordinary link. The test writes DK firmware: its first case flashes
the DK when it runs anything but the version the harness pins, so the run
holds the host lock. Every case declares that it needs `tshark` installed, so
a host without it runs none of them (not applicable, named in `--dry-run`)
rather than flashing the DK to find out; the version that read the records is
recorded in the run manifest.

**A host with more than one board.** The plugin lists the boards it could open
and still exits 1 when another one could not, and Wireshark and tshark throw
away the output of a listing call that failed. So one Ubertooth held by
something outside the run leaves tshark with no interface to capture on, and
the harness cannot stop a program it does not control from opening a board it
does not hold. Each case therefore asks the plugin for the listing itself,
before tshark's own copy of the same call, and is given every board the run
holds: each of them must be named in the listing, and the exit status must
match what the listing reported. A listing regression fails there on any host,
busy or not. Only when that check passes and the boards another program holds
are ones this run never had does the case skip, saying how many they were; the
run leases the board under test and every other registered board on the host,
so a board it does hold and cannot open is a failure, not a skip.

**Commands.** `nrfdk.py` drives the advertiser and `tsharkcapture.py`
installs the plugin in a Wireshark configuration of its own and runs the real
`tshark` against it:

```text
nrfdk.py identify  --jlink <jlink serial> --alias <alias> [--port <serial device>]
nrfdk.py ensure    --jlink <jlink serial> --alias <alias> --address <DK address> \
    --hex <nRF52840 advertiser image>
nrfdk.py advertise --jlink <jlink serial> --alias <alias> --chmap 37 \
    --interval 100 --payload <tag>
nrfdk.py stop|idle --jlink <jlink serial> --alias <alias>
tsharkcapture.py capture --interface ubertooth<serial> --seconds <duration> \
    --address <DK address> --min-packets 1 --held ubertooth<serial>
```

and the same capture with these:

```text
tsharkcapture.py capture ... --filter <filter>
tsharkcapture.py capture ... --interrupt-after <seconds>
```

`tshark -i ubertooth<serial>` is what the driver runs, with
`-T fields -e btle.advertising_address -e btle.advertising_header.pdu_type`.

`ensure` takes `--hex` only where the run carries the `nRF52840 advertiser`
artifact, and every `nrfdk.py` command takes `--port <serial device>` where
the registry names one; see "Capture known BLE advertiser" below for both.

**Expected outcome.** Smoke runs 2 cases: the DK is confirmed to run the
pinned firmware at its registered address, then the plugin's listing names
every board the run holds and `tshark -i ubertooth<serial>` captures for the
duration, with at least one row carrying the DK's address as an
ADV_NONCONN_IND, tshark exiting 0 and no process left behind. Comprehensive
runs 4, adding a capture filter, which Wireshark passes on as
`--extcap-capture-filter` and the plugin cannot apply: its warning must reach
tshark (which relays what the plugin wrote) and the capture must run anyway;
and a capture stopped with SIGINT halfway, which must exit 0 with the rows
decoded so far, and is skipped ("no packet reached tshark in the capture
window") when there was nothing decoded to stop it after. Every case leaves
the DK idle in the advertiser role with its boot settings and restores the
Ubertooth's channel, squelch, and LEDs.

**Known limits.** One Wireshark version answers for itself: what the version
on the host does is what the case shows, and tshark hands packets on in
batches, so the rows a case sees are not every packet the board caught. The
checks are about the fields Wireshark decoded, not how many, so the counting
checks stay in `Capture known BLE advertiser`. The GUI is not exercised, only
tshark.

## Capture known BLE advertiser

**What it does.** An nRF52840 DK advertises a known payload from a registered
address on a chosen channel; the Ubertooth captures it and the capture file is
checked against what the DK did.

**Theory of operation.** This is the first test where the air carries a known
signal, so a capture can be checked for content rather than shape. The DK runs
the harness's own role firmware, so its address, advertising channel,
interval, payload, and advertising type are all set by the test, and the DK
reports its own event counter before and after the capture. That gives four
independent bounds on the file: the packets must come from the registered
address, on the RF channel that belongs to the advertising channel the DK was
told to use, carrying the harness payload (magic, channel map, and tag), with
counters inside the window the DK reported, and no more packets than the DK
could have sent at its interval. The mirror image is the stronger check: with
the DK on one advertising channel and the Ubertooth listening on another, none
of its packets may appear at all, and since the bands are 24 MHz or more
apart, a leak is a real fault rather than noise.

**Hardware and arguments.** One `Ubertooth` and one `advertiser`
(nRF52840 DK) with a registered J-Link serial and a static random BLE
address. `coverage:`, a required `duration:`, `interval:` (20-10240 ms,
default 100), and `payload:` (1-12 bytes of hex, or `none`, default
`c0ffee`). The Ubertooth defaults to needing an ordinary link (antenna
attached, no attenuation). The test writes DK firmware: the first case flashes
the DK when it runs anything but the version the harness pins, so the run
holds the host lock.

**Weak-link receiver, measured 2026-11-02.** `ble.yaml`'s `ble-weak-link`
suite points the Ubertooth at the attenuated board with
`weak link: {Ubertooth: 20}` and again at 40 dB, which are the two
attenuations this test has figures for. One `-n -q` leg per attenuation
captured about 1175 advertisements at 20 dB and 1228 at 40 dB, every one with
a valid CRC and none failing, against a floor of a fifth of the DK's events;
`tshark` agreed with the tool's own count exactly. So the link is nowhere near
marginal for this capture at either point, and what the entry adds is that the
capture works from the attenuated board rather than a tighter bound: the
thresholds are the committed ones. The smoke profile is broader than the one
leg that was measured — it also captures on 38 and 39, checks their absence
elsewhere, and follows with `-f` — so what carries those cases is the margin
above the floor rather than a like-for-like figure. See "What attenuating a
receiver is for" above for what the attenuation is and is not.

**Commands.** `nrfdk.py` brings the DK up and reads its counter either side
of the capture, `ubertooth-btle` takes it, and `advcapture.py` decodes it:

```text
nrfdk.py identify  --jlink <jlink serial> --alias <alias> [--port <serial device>]
nrfdk.py ensure    --jlink <jlink serial> --alias <alias> --address <DK address> \
    --hex <nRF52840 advertiser image>
nrfdk.py advertise --jlink <jlink serial> --alias <alias> --chmap <channel> \
    --interval <ms> --payload <tag> --type <nonconn|conn>
nrfdk.py stop|idle --jlink <jlink serial> --alias <alias>
ubertooth-btle -U <serial> -n -A<channel> -q <case dir>/capture.pcap
ubertooth-btle -U <serial> -f -A<channel> -r <case dir>/capture.pcapng
advcapture.py <case dir>/capture.pcap --address <DK address> --channel <channel> \
    --payload <tag> --chmap <channel> --type <type> \
    --counter-min <counter before> --counter-max <counter after> \
    --min-packets <n> --max-packets <n>
advcapture.py <case dir>/capture.pcap --address <DK address> --channel <channel> --absent
```

Two options above are conditional, and both are written in brackets wherever
they appear. Every `nrfdk.py` command, not only `identify`, takes
`--port <serial device>` where the registry names one for that DK; without it
the driver derives the VCOM0 path from the J-Link serial. And `ensure` is
given `--hex` only where the run carries the `nRF52840 advertiser` artifact. With
it, a board reporting another firmware version is flashed with that image;
without it, a board already on the pinned version passes and one on anything
else fails, naming the `--artifact` to run with. One image holds every role,
so the label is the same whichever role the DK is brought up in.

**Expected outcome.** Smoke runs 6 cases: the DK is confirmed to run the
pinned firmware at its registered address; `ubertooth-btle -n -q` sees it on
each of channels 37, 38, and 39; listening on 37 while it advertises on 38
sees nothing of it; and `ubertooth-btle -f -r` captures it into PCAPNG.
Comprehensive runs 17, adding all six listening-and-advertising channel
mismatches, `-f` on all three channels, a connectable ADV_IND, an advertiser
on all channels, a 1000 ms interval where the packet upper bound is the real
check, and an advertiser with no payload tag. A capture case passes when the
file holds at least a fifth of the DK's advertising events (and at least three
packets) and no more than the interval allows; an absence case passes when the
file holds none of the DK's packets. Every case leaves the DK idle in the
advertiser role with its boot settings and restores the Ubertooth's channel,
squelch, and LEDs.

**Every record's power fields.** Both low-energy capture readers decode the
10-byte pseudo-header libbtbb writes ahead of each packet, and hold its power
fields to the rules the writers follow whatever was on the air
(`lell_pcap_append_packet()` and `lell_pcapng_append_packet()`, and the
figures the fork hands them): every record is dewhitened, carries a signal
figure, and carries a count of its access-address errors; the noise flag is
set exactly when the noise figure is below the signal, which is how the
writers decide it, so a board with no background estimate for the channel --
given a figure no signal can reach -- comes out with the flag clear; each
figure the flags call a measurement is inside the -120 to 0 dBm the host's
model of the radio can produce; and the reference access address flag is set
exactly when that field holds an address, which is what the flag is defined
to say. The capture's signal and noise spread, with how many distinct figures
each carries, is reported beside the counts.

A record has no field for "the board could not measure this": a reading at
the top of the radio's scale is recorded as 0 dBm, and it is the tool's own
line that says `rssi=none`
([ubertooth#1192](https://github.com/SqueakyFloors/ubertooth/pull/1192),
[#1198](https://github.com/SqueakyFloors/ubertooth/pull/1198)). That
asymmetry, and the two figures a packet's own level must differ by
([#867](https://github.com/SqueakyFloors/ubertooth/pull/867)), are pinned
deterministically by [BLE replay](#ble-replay), which writes the readings
itself; what is checked here is that a live capture's records are internally
consistent. No floor is put on how much a live capture's levels vary: that is
measured for Classic (see
[Classic capture record fields](#classic-capture-record-fields)) and **not
established** for these two tests.

**Known limits.** The minimum share is a floor a working bench clears
comfortably, not an expected capture rate, and it is bench-specific: a weaker
link or a noisier room can fail it without anything being wrong with the code.
The test needs two kinds of hardware and a flashable DK, so it cannot run
anywhere the DK is absent. The DK firmware image itself comes from a local
build: CI compiles it (the `Firmware` workflow, `scripts/ci.sh firmware-dk`)
but publishes nothing, so a run still needs the image passed in as
`--artifact "nRF52840 advertiser=PATH"`.

## Capture known BLE connection

**What it does.** Two nRF52840 DKs make and remake a connection; the Ubertooth
follows it and the capture is checked against the parameters the central was
told to ask for.

**Theory of operation.** The central drops and remakes the link on a schedule,
so a capture started at any moment contains a whole connection: the
CONNECT_IND, the data length exchange, the parameter update, and the data.
Nothing has to be timed against the capture, which is what makes this
testable one command at a time. The CONNECT_IND then carries parameters the
test chose, so the capture can be checked for exactly those: the connection
interval, the latency, the supervision timeout, a legal hop increment, and
every data channel in the channel map. The central also writes 244-byte
payloads, which only fit in a link-layer packet when Data Length Extension is
in effect, so a packet longer than 27 bytes in the capture proves the
extension took effect on the air. Finally the DKs are asked what they did,
which closes a gap the capture alone leaves: a DK that connected and sent
nothing, or never had its update accepted, would otherwise pass.

**Hardware and arguments.** One `Ubertooth` and two nRF52840 DKs as `central`
and `peripheral`, each with a registered J-Link serial and a static random BLE
address. `coverage:`, a required `duration:`, `interval:` (the interval the
central connects at, default 30 ms), and `update:` (the interval it then asks
for, default 60 ms). The duration is constrained: it must be longer than the
5 s reconnection cycle and shorter than twice it, so the capture spans exactly
one reconnection. The Ubertooth defaults to needing an ordinary link. Writes
DK firmware, so the run holds the host lock.

**Commands.** Two DKs, one capture, and `conncapture.py` reading the file:

```text
nrfdk.py identify   --jlink <jlink serial> --alias <alias> [--port <serial device>]
nrfdk.py ensure     --jlink <jlink serial> --alias <alias> --address <address> \
    --role <role> --hex <nRF52840 advertiser image>
nrfdk.py peripheral --jlink <jlink serial> --alias <alias> --dle on --chmap 37
nrfdk.py central    --jlink <jlink serial> --alias <alias> --peer <address> \
    --interval <ms> --dle on --payload 244 --period 50 --cycle 5000 --autoupdate <ms>
nrfdk.py status|idle --jlink <jlink serial> --alias <alias>
ubertooth-btle -U <serial> -f -A37 -r <case dir>/capture-<n>.pcapng
ubertooth-btle -U <serial> -f -A37 -q <case dir>/capture-<n>.pcap
conncapture.py <case dir>/capture-<n>.pcapng --central <address> --peripheral <address> \
    --interval-units <n> --latency 0 --timeout-units 400 --min-data-packets 5 \
    --min-valid-packets 1 --min-data-length <bytes> --long-packets <n> \
    --check-connection-info
```

`ensure` takes `--hex` only where the run carries the `nRF52840 advertiser`
artifact, and every `nrfdk.py` command takes `--port <serial device>` where
the registry names one; see "Capture known BLE advertiser" above for both.
One image holds every role, so the central and the peripheral are brought up
from the same artifact.

**Expected outcome.** Smoke runs 2 cases: both DKs confirmed on the pinned
firmware at their registered addresses, and `ubertooth-btle -f -r` holding the
pair's CONNECT_IND and long packets. Comprehensive runs 4, adding the same
into PCAP and one that reads the connection information recorded in the
PCAPNG. A capture case passes
when the CONNECT_IND carries the parameters asked for, at least five packets
appear on the connection's access address with at least one decoding with a
correct CRC, at least one packet is longer than 27 bytes, and the central
reports that it connected, wrote, and had its update accepted at the interval
it was told to ask for. The connection-information case also requires that
one of the options libbtbb recorded in the PCAPNG is a CONNECT_IND this
capture decoded from the pair, byte for byte: those 34 bytes are what a
reader takes from the file to follow the connection in it.

A capture that does not hold all of that is taken again, up to three captures
in all, because the pair keeps making and remaking the link and another
capture window is all a second try costs. Each attempt writes its own file
(`capture-1.pcapng`, `capture-2.pcapng`) and is checked on its own, so the run
directory keeps the capture every attempt looked at; the report says which
attempt a step ran on and how many attempts the case took.

**What a weak link adds here.** The HEC and CRC error flags in an LE capture
are what a marginal link exercises in this test: on a clear bench the floors
are cleared with room and no error flag fires, so a weaker link is where the
counts and the flags say something. Attenuating the receiver needs the DK pair
to be bound distinctly, which is the hardware question rather than an RF one.
`weaklink.CHECKS` and the plan files are the live record of which attenuation
this test has figures for and what any remaining entry waits on; see "What
attenuating a receiver is for" above for what the attenuation is and is not.

Every record's power fields are held to the same rules as in
[Capture known BLE advertiser](#capture-known-ble-advertiser), which are the
writers' own and so apply to a followed connection too: a connection's
packets carry no reference access address, because that field is the address
configured into the tool and the tool caught them on a connection it had
synchronized with, so the flag is clear there and set on the advertising-channel
packets that carry `8e89bed6`.

**Known limits.** The packet counts are floors a working follow clears, not
expected counts: of 18 packets in one followed connection measured on the
bench, two decoded with a correct CRC, one of them a full 251 bytes. How much
of the connection arrives intact varies from capture to capture, which is what
the repeated captures are for: three single-capture runs on the bench on
2026-10-30 cleared the floors twice and missed them once. A capture window
spanning several reconnections has been seen to die partway through
([ubertooth#505](https://github.com/SqueakyFloors/ubertooth/issues/505)),
which is why the duration is bounded; whether the end of the followed
connection triggers it is not established. The PCAPNG connection-information
option libbtbb records for a CONNECT_IND used to be copied 6 bytes early
([libbtbb#21](https://github.com/SqueakyFloors/libbtbb/issues/21), fixed);
one comprehensive case now checks it for real: one of the recorded options
must be, byte for byte, a CONNECT_IND this capture decoded from the pair. It
reads every option in the file rather than the first, because libbtbb records
one for every CONNECT_IND it decodes and another device in range gets one
too, and when none matches it names each field of the pair's own option that
differs.

## Follow a BLE connection across its end

**What it does.** The same two nRF52840 DKs connect and reconnect for about
18 seconds while one `ubertooth-btle -f` capture runs across the whole
window, and the board is read back as soon as the capture stops.

**Theory of operation.** `Capture known BLE connection` deliberately keeps its
window inside one connection, so it says nothing about what happens at the
end of one. This test takes the other window. Three things only a multi-cycle
capture can show:

- A follow that ends cleanly finds the next connection. The capture has to
  hold a CONNECT_IND for two of the pair's connections, with packets on each
  one's own access address, which means the board stopped following the first
  connection when it closed and picked the second one up from its own
  CONNECT_IND.
- A follow is not hijacked. During a follow the board writes the access
  address it synchronized on into every record it sends, and it synchronizes
  on exactly one at a time: the advertising address while it hunts, then the
  connection's own. So a packet on an access address that no CONNECT_IND in
  the file handed out is a connection the file cannot explain, and the check
  refuses it. A CONNECT_IND from another device in range explains its own
  packets just as well as the pair's explains the pair's.
- A follow survives the parameter update. The central asks for the new
  interval 1.2 s into each connection and the update's instant is a few
  connection events later, so a packet with a correct CRC 2 s or more after
  its own CONNECT_IND is one the board tracked across the update. The capture
  records carry the time each packet arrived, which is what that distance is
  measured in. Every connection the file holds a later CONNECT_IND for has to
  show one, so a follow that died on the second connection's update cannot
  pass on the first connection's evidence. The last connection in the file is
  left out of that, because the capture window, not the follow, is usually
  what ends it. What keeps that exemption honest is a pair of checks on the
  file's own clock. A board whose follow died goes back to hunting and keeps
  recording advertising packets, so the capture may go on recording for no
  more than two of the central's cycles after the last packet it heard of the
  pair -- two, because the Ubertooth is not expected to catch every
  reconnection, and missing the last one costs a whole connection's worth of
  silence. And the records have to span all but one cycle of the window the
  capture was given, or the capture stopped hearing everything rather than
  just the pair's connections. Neither reads the window's start out of the
  file, because the first record arrives some unknown time after the command
  did.

The DKs are then asked what they did, which is the only place the number of
cycles can be read: a window the central spent on one long connection reports
one connect and no disconnect, and is told apart from one it spent cycling.
Last, the board itself is read, after the capture has been stopped as any
mode is: it must answer an identity and a settings read at once, report
itself back in idle mode rather than still in BLE follow mode, and show no
new unrequested watchdog reset against the count read before the capture.
That says the stop left the board usable, not that the follow ended on its
own -- what says the follow ended on its own is the capture holding the next
connection from its own CONNECT_IND. The other registered Ubertooths on the host are guarded, as in every
case here.

**Hardware and arguments.** The same hardware as `Capture known BLE
connection`: one `Ubertooth` and two nRF52840 DKs as `central` and
`peripheral`. A required `duration:`, `interval:` (default 30 ms) and
`update:` (default 60 ms); no `coverage:`, because there is one capture case.
The duration must be longer than three of the central's 5 s connections and
at most 30 s, so one case does not hold the boards for long. The same entry
is in the `connection` suite and in `ble-comprehensive`, because there is one
capture case either way. The Ubertooth needs an ordinary link. Writes DK
firmware, so the run holds the host lock.

**Commands.** The same pair and the same bring-up as the test above, with
one capture across the whole window and `conncapture.py` reading it:

```text
nrfdk.py identify   --jlink <jlink serial> --alias <alias> [--port <serial device>]
nrfdk.py ensure     --jlink <jlink serial> --alias <alias> --address <address> \
    --role <role> --hex <nRF52840 advertiser image>
nrfdk.py peripheral --jlink <jlink serial> --alias <alias> --dle on --chmap 37
nrfdk.py central    --jlink <jlink serial> --alias <alias> --peer <address> \
    --interval <ms> --dle on --payload 244 --period 50 --cycle 5000 --autoupdate <ms>
nrfdk.py status|idle --jlink <jlink serial> --alias <alias>
ubertooth-btle -U <serial> -f -A37 -r <case dir>/capture-<n>.pcapng
conncapture.py <case dir>/capture-<n>.pcapng --central <address> --peripheral <address> \
    --interval-units <n> --latency 0 --timeout-units 400 --min-data-packets 5 \
    --min-valid-packets 1 --min-connections 2 --min-connection-packets 3 \
    --after-update-ms <ms> --require-announced --max-quiet-ms <ms> --min-span-ms <ms>
ubertooth-util -U <serial> -s -C      # the board answers at once, read back in idle mode
ubertooth-util -U <serial> -w         # no new unrequested watchdog reset
```

`ensure` takes `--hex` only where the run carries the `nRF52840 advertiser`
artifact, and every `nrfdk.py` command takes `--port <serial device>` where
the registry names one; see "Capture known BLE advertiser" above for both.

**Expected outcome.** 2 cases: both DKs confirmed on the pinned firmware at
their registered addresses, and the capture across the window. The capture
case passes when every CONNECT_IND it holds from the pair carries the
parameters the central was told to ask for, at least two distinct connections
appear with at least three packets each on their own access addresses, no
packet is on an unannounced access address, every connection but the last carries a
packet with a correct CRC 2 s or more after its own CONNECT_IND, the capture
records nothing for more than 10 s after the pair's last good packet and its
records span all but 5 s of its window, the central reports at least three
connects and two disconnects, and the board reads back idle with an unchanged
self-reset count. Cleanup leaves both DKs idle in the advertiser
role and restores the Ubertooth's channel, squelch, and LEDs.

**What a weak link adds here.** A follow that has to reacquire the pair's next
connection over a marginal link is the thing worth measuring, since a clear
bench clears the floors with room. It needs the same DK pair bound distinctly
as the single-connection test does. `weaklink.CHECKS` and the plan files are
the live record of which attenuation this test has figures for; see "What
attenuating a receiver is for" above for what an attenuated receiver is and is
not.

**Known limits.** Like the single-connection cases, the counts are floors a
working follow clears, not expected counts, and a capture that misses them is
taken again, up to three captures. Only the capture and the checks of it are
retried: the reads after them have to see the board as the last capture left
it. Two connections is a floor rather than the three or four the window spans,
because the board hunts on one advertising channel and catches part of what is
there, so it is not expected to see every reconnection. A window spanning
several reconnections used to die partway through
([ubertooth#505](https://github.com/SqueakyFloors/ubertooth/issues/505),
fixed), which is why the single-connection test still keeps its window inside
one connection.

The two checks above have tolerances of their own, so between them they
allow a narrow worst case: a capture that stopped recording near the end of
its permitted span, with the pair's last good packet near the start of the
permitted quiet time, hides a longer dead follow than either tolerance
suggests. Reaching it needs the board to stop recording anything in a
particular few seconds, and a failed capture is taken again, so the shape is
unlikely rather than impossible.

Two things the checks cannot separate. A connection's packets look
unexplained whether the board followed a connection it never announced or
the CONNECT_IND itself was lost on the way to the host, which does happen —
the firmware counts capture transfer errors for that reason. Three captures
make it unlikely rather than impossible. And the connect and disconnect
counters the central reports run from the board's boot, so on a second or
third capture attempt they cover the whole case rather than that one window:
they say the central is cycling while the case runs, and the capture check,
which wants two of the pair's connections in the file the attempt just
wrote, is what binds per window.

"No stale records from follow mode" is checked as far as one command can: the
board reports itself back in idle mode, and the capture file holds no packet
on an access address it does not also announce. Whether a *later* capture
picks up records left by this one is a different measurement, which `BLE CRC
transitions` makes with `stalecheck.py`.

## BLE address parity soak

**What it does.** Runs one `ubertooth-btle -n` capture for as long as the plan
asks, while a known advertiser goes on and off to a schedule, and reads the
one capture two ways: the documented `AdvA:` text line the tool prints, and
the capture's own records. It reports whether the two readers agree, whether
the advertiser was detected when it was advertising, and what the capture,
the board, and the host process did over the whole window.

**Theory of operation.** Two questions need a long run and cannot be answered
by a short one.

The first is a compatibility question. A production-style advertising
detection service runs `ubertooth-btle` with its standard error merged into
its standard output and picks advertiser addresses out of the `AdvA:
xx:xx:xx:xx:xx:xx (random)` lines libbtbb prints for each packet it decodes
(`lib/src/bluetooth_le_packet.c`, `_dump_addr`). Whether that line keeps
meaning what it meant is not something a reader of the capture file can be
asked about separately — so here both readers read *the same capture*. The
text reader takes the printed line and timestamps it when the host read it;
the structural reader tails the capture file as the tool writes it, decodes
each `LINKTYPE_BLUETOOTH_LE_LL_WITH_PHDR` record, recomputes the link-layer
CRC, and takes the AdvA of the packets whose CRC is right, timestamped as the
capture recorded them. Both addresses are normalized the same way before
anything is compared, because one source prints lower case and the other is
raw bytes, and an unnormalized comparison would fail for a reason that has
nothing to do with the capture.

The second is what only time finds: a capture process that dies, a board that
resets itself, a host process whose memory grows, a detection that arrives
late, a silence inside a period the advertiser was known to be advertising
in. None of those is visible in a five-second capture, and none of them is
decidable without a schedule: a quiet capture could be a dead board or an
advertiser that is not advertising. So the harness drives the advertiser
itself, one leg at a time, and every on period becomes a detection with a
deadline while every off period becomes a silence with a known cause. That is
also what makes the liveness rule sound. A capture is restarted when its
process exits, or when no packet of any address arrives for the liveness
timeout; the timeout has to be longer than the advertiser's own off period,
or the schedule's silence would restart the capture, and the catalog refuses
a plan where it is not (below).

**The readers are not expected to agree packet for packet, and are not asked
to.** Two asymmetries are real and documented rather than designed away.
libbtbb prints a decoded packet whether or not its CRC checks (the print is
after the `dump:` label, with no CRC gate), so the text reader can report an
address from a corrupted packet that the structural reader drops — including
a corrupted copy of this advertiser's own packet, whose address bytes
survived. And the `AdvA:` of a SCAN_REQ or a CONNECT_REQ is the *target's*
address, not the sender's, so a scanner that addressed this advertiser would
appear in the text reader alone; the advertiser is driven as
ADV_NONCONN_IND, which is neither scannable nor connectable, so that should
not arise, but a device on the bench can send what it likes. What the test
compares is therefore which windows each reader found the address in, with an
allowance that is a share of the windows either reader saw it in, so the
allowance scales with the run instead of being a count that is strict at one
duration and meaningless at another.

A window **beside** one the other reader had is not counted at all. Both
readers see their packets after the fact, through buffers that do not fill
together: the tool's own output reaches the pipe a stdio block at a time, and
its capture file reaches the disk the same way, so a packet near a window's
edge lands either side of it depending on which buffer flushed first. The
bench measured exactly that — two windows the record reader had alone in a
one-minute run of 238 packets, with nothing wrong. What the comparison is for
is a reader that misses the advertiser while the other sees it, and that is a
run of windows rather than an edge.

**The schedule is checked in both directions.** The on periods are where
detection is measured; the off periods are where the schedule itself is
checked. An advertiser that accepts `adv stop` and keeps advertising would
otherwise meet every detection, gap and rate objective, and the run would
report a controlled schedule it never had. So in each judged off period the
advertiser must fall quiet: a sighting by either reader more than two seconds
into it (or half the period, whichever is less, so a short schedule still has
something judged) makes that period noisy, and more noisy periods than the
allowance fails the run. The allowance is there for the same reason the
parity one is: the text reader's time is when the host read a line, so one
delayed flush should not fail three days of work. The two seconds are for the packets already
on the air and already in the tool's buffers when the stop lands.

Two things keep that check from being quietly empty, which would be worse
than a check that fails. **A rollover does not cost its off period.** The
capture file is rolled over at the start of an off period on purpose, so on a
busy channel with a small `capture file limit` every off period holds a gap;
throwing those periods away left nothing judged, and an advertiser that took
`adv stop` and kept advertising would have passed with the schedule half
checked. So such a period is judged from the moment something was listening
again instead, and only a rollover that left no stretch at all, or an
unplanned gap — where the capture's own health is in question — takes the
period out. **And a population of none fails.** If no off period that was
meant to complete could be judged, whatever emptied it, the run fails saying
the stop was never measured, rather than reporting a schedule it did not
check; a population too small for the `minimum detection share`, counted the
way the on periods are, fails too.

**An allowance is never the whole population.** Each one is a share with a
floor of one, so a short run has slack too — but capped one below the number
of periods judged, so a comparison resting on one or two of them can still
fail. Without the cap the floor made the off-period and gap checks
structurally unfailable on the short suite, which judges one or two off
periods and is the suite a change to the driver is tried on.

What the parity comparison really bounds is worth stating in windows, because
both neighbours are checked: a reader can lose two consecutive windows
anywhere with nothing recorded, three give one disagreement, and the floor
absorbs that, so four consecutive windows in one place — eight seconds at the
long suites' two-second window — is where parity starts to bite. A reader
losing a fraction of windows everywhere is caught by sustained presence
rather than by parity, which is why both objectives are there.

**Timing relationships are checked before any board is leased.** The
figures below constrain each other, and a run holds an Ubertooth and a DK for
hours, so a plan whose own figures contradict each other has to fail when the
plan loads rather than on the bench. Every relationship below is refused
(`addresssoak.check_timings`, raised through the catalog's
`check_arguments`), and the driver makes the same checks from the same code
when it is run by hand:

* a schedule leg under a second, or a duration and a schedule that together
  make more periods than a run can hold — each period is kept for the whole
  run and writes two synced records;
* a presence window or a sampling interval too short to run for days, for the
  same reason;
* an advertising interval that leaves fewer than three events in a presence
  window, so one lost or late event would empty it, or that cannot send the
  packets a minute the rate floor asks for;
* a detection deadline longer than the advertiser is on for;
* sustained presence (`presence windows` × `presence window`) that does not
  fit in an on period;
* a liveness timeout that does not clear an off period *and* the wait for the
  next period's first packet, which is the quiet stretch a healthy run has;
* a gap limit below the detection deadline, which every period's wait for its
  first packet would fail, or as long as an on period, which no gap inside one
  could ever reach;
* a duration shorter than two schedule cycles, or a sampling interval that
  would leave fewer than three samples.

**Hardware and arguments.** One `Ubertooth`, selected by serial and receiving
only, and one `advertiser` (nRF52840 DK) with a registered J-Link serial and
a static random BLE address. The Ubertooth defaults to needing an ordinary
link (antenna attached, no attenuation). The test writes DK firmware: the
first case flashes the DK when it runs anything but the version the harness
pins, so the run holds the host lock. There is no `coverage:` argument — one
capture either way — and a `duration:` is needed. The rest are the figures
the verdict is made of: `channel:`, `interval:` and `payload:` for the
advertiser; `advertiser on:` and `advertiser off:` for the schedule;
`sample every:` for how often a progress record is written; `presence
window:` and `presence windows:` for what sustained presence means; `liveness
timeout:` for when a quiet capture is restarted; `detect within:` and
`minimum detection share:` for the detection objective; `maximum gap:` and
`minimum packets per minute:` for the silences and the rate; `maximum window
disagreement share:` for how far the readers may differ; `maximum restarts:`
for how many unplanned restarts still pass; `maximum memory growth:`, which
only applies a limit when a plan sets one; `restart probe:`, which kills the
capture once at the middle of the run; and `capture file limit:`, in MiB,
which rolls the capture over to a new file at the start of an off period; and
`capture budget:`, also in MiB, which is what the whole run's captures may
cost (past it the oldest complete capture and its transcript are removed,
newest kept, each removal recorded).

**Commands.** `nrfdk.py` writes and identifies the DK's firmware and holds it
idle, `addresssoak.py` is the driver that runs the capture and both readers,
and `ubertooth-util` reads the board's settings before the capture and writes
them back after it. The driver runs the `ubertooth-btle -n` capture itself,
with the channel and the capture file the plan gave it:

```text
nrfdk.py identify --jlink <serial> --alias <alias> --port <port>
nrfdk.py flash --jlink <serial> --alias <alias> --port <port> --hex <image>
nrfdk.py advertise --jlink <serial> --alias <alias> --port <port> --address <addr>
nrfdk.py idle --jlink <serial> --alias <alias> --port <port>
addresssoak.py --serial <serial> --channel <37|38|39> --out <capture dir> \
    --jlink <serial> --dk-alias <alias> --dk-port <port> \
    --address <addr> --interval <ms> --payload <bytes> \
    --duration <s> --advertiser-on <s> --advertiser-off <s> \
    --sample-every <s> --presence-window <s> --presence-windows <n> \
    --liveness-timeout <s> --detect-within <s> --min-detection-share <share> \
    --max-gap <s> --min-packets-per-minute <n> \
    --max-disagreement-share <share> --max-restarts <n> \
    --max-capture-bytes <n> --max-total-capture-bytes <n> \
    [--restart-probe]
ubertooth-util -U <serial> -S                 # idle before the reads
ubertooth-util -U <serial> -C                 # channel, PA level, squelch,
ubertooth-util -U <serial> -a                 # and LEDs: read, then written
ubertooth-util -U <serial> -z                 # back after the capture
ubertooth-util -U <serial> -d
ubertooth-util -U <serial> -c <channel>       # the restores
ubertooth-util -U <serial> -l <leds>
ubertooth-util -U <serial> -s                 # the identity read that ends it
```

**Plans.** `testplans/address-soak.yaml`, four suites, none of them in `fast`
or in the non-soak `release` plan: `address-parity` (about a minute of
capture, with the restart probe, for development and for a short bench
validation), `address-soak-4h` (setup validation), `address-soak-24h`, and
`address-soak-72h` (the release-candidate soak
[ubertooth#703](https://github.com/SqueakyFloors/ubertooth/issues/703) asks
for). The long ones run on the persistent target runner, which keeps running
when whatever started the run has disconnected; the run directory holds the
record, and the driver flushes a progress record to it as the run goes, so a
soak is reviewable while it runs and afterwards.

**Which on periods are judged.** An on period says something about detection
only if the advertiser was known to be advertising for the whole of it and
something was listening throughout. Four kinds are therefore left out, each
counted and recorded: the last one, which the run's own duration ended; one
cut short for another reason; one whose schedule command did not reach the
DK; and one with a *capture gap* — a stretch where the capture was stopped or
started, whether by a restart, by the probe, or by a file rotation. Counting
those as missed detections would report the harness's own doing as the
board's. A period also begins when the advertiser was *told* to start, not
when the run decided to tell it, so the shell command's own latency is not
charged to the detection deadline; and each sighting is attributed to the
period its own timestamp falls in rather than to whichever period the loop
happens to be in when it reads it, since both readers hand their sightings
over later than the sightings themselves.

Leaving periods out would leave a hole, though: a capture that kept dying
would pass on the few periods it did cover. So a run must also be able to
*judge* at least `minimum detection share` of the on periods that were meant
to complete, and only the interruptions the run chose — the probe, a capture
file rolled over, and the final period the duration ended — are allowed
against that floor. A flapping capture fails there rather than passing
quietly.

**Expected outcome.** Two cases. The first confirms the DK runs the pinned
firmware at its registered address, flashing the run's `nRF52840 advertiser`
artifact if it runs anything else. The second is the soak. It passes when
every configured objective is met: enough of the on periods could be judged
at all (above); both readers detected the advertiser within `detect within`
of an on period starting, and saw sustained presence, in at least
`minimum detection share` of the judged periods; the
readers disagreed about no more windows than the allowance; no more judged periods than
the allowance held a silence longer than `maximum gap`; the rate over the minutes the advertiser
was on cleared `minimum packets per minute`; at least one captured packet
carried the harness payload tag, so the address is this advertiser's and not
a collision; every capture file could be read to its end; the advertiser fell
quiet in every judged off period; the capture process died of its own accord
no more often than the restart budget allows; there were no more unplanned
restarts than the budget; the final
stop was a clean one (SIGTERM, within its grace, no kill needed); every
schedule command inside the window reached the DK; the run's capture budget
was kept; and the board answered every read afterwards. The closing
stop-and-read is counted apart and is not one of these: it runs after the
last period, which the duration ended and nothing judged, so a DK that
hiccups in the final second of a three-day run affected no measurement. Each failed objective is its own `FAIL:` line and the step
requires `SOAK VERDICT: pass`.

What is reported either way, and is the point of running it: packets per
reader, the windows each found the address in, periods detected and
sustained with the first and median detection time, the longest and median
gap, packets a minute with its spread, resident memory first, last, and
largest, every capture attempt with its exit status, its last `error:` line
and whether it had to be killed, planned file rotations, board resets, and
every watchdog read with the mode and wait the firmware recorded. The count
of lines matching the older "capture died" wording (`libUSB Error`, `could
not open Ubertooth device`) is reported too, which is the compatibility note
[ubertooth#703](https://github.com/SqueakyFloors/ubertooth/issues/703) asks
for: the stable signal now is the exit status plus one documented `error:`
line, and a run says whether the old one would have fired at all.

**The restart probe.** With `restart probe: true` the driver kills the capture
once, at the middle of the run, with SIGKILL and no stop sent to the board —
the way a lost terminal or a dropped session ends a capture, which is the
`#239` stream-stop class — and then restarts it. The advertiser must be heard
again in the first judged on period after it, and where the run was too
short to hold one, the report says the recovery is not established rather
than claiming one. Without the probe a run that
never needed to recover carries no evidence that it could; with it, every run
carries that evidence deterministically, and the restart is counted apart
from the unplanned ones so it cannot be mistaken for a fault. The attempt's
transcript is also checked for libubertooth's "is still in mode" warning,
which is reported rather than required, since whether it appears depends on
the fork's own version.

**A busy board is a configuration failure.** Before the capture starts the
driver reads the board once. A board that does not answer — in use by another
program, or gone — ends the run with exit status 3 and a reason, and is never
reset to make it work: resetting it would spoil whatever is using it as well
as this run. The board's own leases are the harness's business
(README, "Sharing boards: leases"); this is the last check before hours of
capture.

**Measured on the bench, 2026-11-05.** The `address-parity` suite at fork
`828e7322`, one Ubertooth and one DK: the test took 66.8 s, both readers
detected the advertiser in both judged on periods (first detection 0.064 s
text, 0.062 s structural) and saw sustained presence in both; the text reader
counted 226 of the advertiser's packets and the structural reader 225, over
31 windows both of them agreed on, with no window either saw alone; the
longest silence inside a judged period was 0.32 s; the capture process grew
from 3804 to 4340 KiB over five samples; the final stop was a SIGTERM exit 0;
the firmware reported no unrequested reset over three reads; and the restart
probe recovered. The bench is crowded — 40116 packets from 6652 other
addresses arrived in the same window, none of them recorded — which is what
the capture file limit and the transcript budget are sized for.

**The host lock, for three days.** The test flashes the DK when it runs
another firmware version, so it is declared as a test that writes firmware,
and a run therefore holds the harness's host lock for its whole window
(README, "The host lock"). On the 72-hour suite that means no other command
on that host may write firmware or switch USB power for three days, on any
board. That is the other reason these runs are booked rather than scheduled.

**Every comparison carries an allowance, and the run says so.** Detection,
sustained presence, parity, the gap limit and the off-period quiet all judge a
*share* rather than demanding none: each one is measured over thousands of
periods in the long suites, and each rests on a time the host read something,
so a single delayed stdio flush must not end three days of work. The
allowances for the gap limit and the off periods are the complement of
`minimum detection share`; parity's is `maximum window disagreement share`.
A run reports the count and the allowance beside each other.

**A run that cannot finish still reports.** A bench failure, a full disk under
the progress file, or a fault nobody expected is caught, added to the problems
as the reason, and written to `summary.json` with a verdict of `fail` or
`configuration`, because the verdict of a run of days is read from there and
not from a traceback. The capture is stopped on the way out however the run
ends, and a cancellation writes the report before it quiets the advertiser:
the harness allows about two seconds after its SIGTERM, and the report is the
part that must not be lost.

**Known limits.** The two readers keep different clocks by nature. The text
reader's time is when the host read a line, which the tool's own stdio
buffering delays by a block on a busy channel, and the structural reader's is
what the capture wrote. A window near a boundary can fall either side for one
reader and not the other, which is why the comparison carries an allowance
and why a run reports its disagreements rather than requiring none; a host
whose wall clock steps under a long run shifts the structural times against
the text times for the same reason. Nothing here says the two readers would
agree about a device the harness does not drive: the comparison is about one
address whose schedule is known. The detection floors and the rate floor are
bench figures rather than bounds from the specification, so a noisier room
can fail them with nothing wrong in the code. Memory is the operating
system's resident set size, which says nothing about what inside the process
grew, and no limit is applied unless a plan sets one, because what counts as
growth over three days is not established. The schedule is applied by the
host over the DK's serial shell, so a transition that failed twice is
recorded against its period and that period is left out of the verdict
instead of counting as a missed detection. A run the harness kills outright
(its hard timeout, after the cancellation grace) can leave the capture tool
holding the board for a moment; the case's own cleanup stops it. And the
test needs two kinds of hardware and a flashable DK, so it cannot run
anywhere the DK is absent.

## Capture known Classic device

**What it does.** Two ESP32 Classic peers hold a Serial Port Profile link and
stream a payload across it; `ubertooth-rx` has to find that piconet, and the
run says which address was on the air.

**Theory of operation.** Classic Bluetooth gave the harness nothing to check
against until the bench had a piconet of its own: ambient traffic has no known
address, and `ubertooth-rx`'s own replay path only proves the decoder, not the
radio. The peers fix that, and one protocol fact makes the check sharp. The
initiator pages, so the piconet takes its clock and its address from the
initiator, and every packet carries the initiator's LAP; the acceptor's LAP is
on the air only during the page train, which is over before the capture
starts. So the survey must list the initiator's LAP, an unfiltered sweep must
report packets carrying it, and exchanging the two role bindings must move
the LAP that appears to the other board. The pair is asked afterwards what it
did, which closes the gap a capture alone leaves: a link that came up and then
sent nothing would otherwise look the same as a quiet radio.

Opening a peer's USB bridge resets the board, so the pair cannot be set up in
one step and captured in the next. One command, `esp32peer.py stream`, holds
both bridges for the whole session, brings the link up, runs `ubertooth-rx`
as a child while the pair streams, and stops the pair afterwards. The tool
keeps standard output to itself, so the case checks its real output; the
pair's report goes to standard error and into the run record.

**What the session says about the bridges and the boards.** Every test that
drives the pair, here and below, gets three things from that one command, so
they are described once:

- a line per board saying whether its bridge is **framed**. A loaded host
  loses runs of bytes on a peer's bridge, and a line that lost some from its
  middle arrives looking whole, so an image from `utesp-1.6.0` on is asked to
  put a length and a CRC around every line it writes and a line that fails
  the check is read again rather than believed. A board on an older 1.x image
  is driven as before and says so. Either way the session reports how many
  lines were lost and how many commands it had to send twice, so a run that
  was saved by a retry is visible rather than silently slower
  ([#310](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310));
- a **restart** named as one, when a board goes away under the capture. The
  reason the chip gives, the version it came back on, and the lines it said
  on the way are reported, and the case fails saying the peer restarted
  rather than saying the pair never streamed - which is what a board full of
  zeroed counters used to read as
  ([#311](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311));
- the **settings read back** off the initiator. A command line can lose bytes
  too, and `stream payload 200` arriving as `stream payload 20` is another
  valid setting, so the peer address, the period, and the payload are read
  from the board rather than taken from its `OK`.

**Hardware and arguments.** One `Ubertooth` and two ESP32 Classic peers as
`acceptor` and `initiator`, each with a registered bridge serial and BD_ADDR,
and both declaring both roles, because the comprehensive coverage exchanges
them. `coverage:` and a required `duration:`; the pair streams 200 bytes every
20 ms whatever the duration is. Linux only: a peer is reached through
`/dev/serial/by-id`. Nothing flashes a peer, so each board must already run
`utesp-1.0.0` or a later 1.x image, which mandatory preflight checks. The
Ubertooth defaults to needing an ordinary link, so a plan has to say
`weak link:` to point it at the attenuated board, as `classic-weak-link`
does at 40 dB: measured there, the clock was acquired and every decoded
packet carried the target LAP with none carrying another. A peer on
`utesp-1.6.0` or later also has its bridge framed, which no case requires:
an older image is driven without it.

**Commands.** `esp32peer.py stream` holds the pair's link for one whole
session and runs the tool under test inside it, because opening a peer's
bridge resets the board:

```text
esp32peer.py stream --acceptor <peer port> --acceptor-address <BD_ADDR> \
    --acceptor-alias <alias> --initiator <peer port> --initiator-address <BD_ADDR> \
    --initiator-alias <alias> --min-seconds <n> --timeout <n> --period 20 \
    --payload 200 --min-writes 50 -- ubertooth-rx -U <serial> -t <duration> [-z] \
    [-r <case dir>/survey.pcapng]
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
capturefile.py <case dir>/survey.pcapng --packets
```

**Expected outcome.** Smoke runs 2 cases: `ubertooth-rx -z` lists the
central's LAP in its survey results, and a run with no target reports at least
ten packet lines carrying it. Comprehensive runs 4, adding the survey into
PCAPNG (where the file must hold packets, not just a header) and the same
survey with the peers' roles exchanged, which must list the other board's LAP
and not the first one's. Every case also requires that the link never went
down, that neither peer restarted, that the initiator took the stream
settings it was sent and reported at least fifty writes, and that the capture
ran for its whole duration; it then resets both peers to the firmware's boot
state and restores the Ubertooth's channel, squelch, and LEDs. About 40 s per
case on the bench, most of it the two board resets and the page.

**Known limits.** Ten packet lines is a floor, not an expected count: eight
25 s captures on the bench gave 50 to 189 lines of the pair's piconet, and a
quieter room or a weaker link gives fewer. A survey's UAP is inferred and
comes out wrong when few packets have been heard, so only the LAP of a survey
line is checked; take the UAP from the registry and pass it with `-u`. Other
piconets in range are listed too, which is why an unfiltered run is not asked
to report this one alone.

## Follow Classic Bluetooth piconet

**What it does.** The same two peers hold the same link, and
`ubertooth-rx -l`/`-u` has to follow that piconet and report its packets, and
nothing for the board that is not its central.

**Theory of operation.** `-l` asks libbtbb for one access code, so a packet
line this test sees should carry the LAP it asked for and no other: that makes
the positive case a count and the negative case an absence. The negative case
is the one that says the tool is really following a piconet rather than
reporting whatever decodes, because the acceptor is in the same link, a foot
away, transmitting throughout, and still must not appear: its LAP is not the
piconet's. Running `-l` without `-u` is the same check with the UAP recovered
from the air instead of given, which exercises the recovery the tool does for
itself.

The bring-up is the one in `Capture known Classic device`, for the same reason:
opening a bridge resets the board, so one command holds both ports and runs
the tool while the pair streams.

**Hardware and arguments.** As `Capture known Classic device`: one `Ubertooth`
and two ESP32 Classic peers as `acceptor` and `initiator`, `coverage:`, a
required `duration:`, Linux only, and an ordinary link for the Ubertooth by
default. `classic-weak-link` points it at the 20 dB receiver, where a 25 s
window decoded 1793 packets at `-e 0` and 2692 at `-e 2`, all of the target
LAP, with the clock acquired.

**Commands.** The same `esp32peer.py stream` session, with `ubertooth-rx`
given the central's address:

```text
esp32peer.py stream <peer options> -- ubertooth-rx -U <serial> -t <duration> \
    -l <central LAP> [-u <central UAP>] [-q <case dir>/follow.pcap]
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
capturefile.py <case dir>/follow.pcap --packets
```

`<peer options>` is the eleven session options written out in "Capture known
Classic device" above, and named in "How to read a section" at the top.

**Expected outcome.** Smoke runs 2 cases: `-l <central LAP> -u <central UAP>`
reports at least ten packet lines and none of another LAP, and `-l`/`-u` for
the peer that is not the central reports no packet at all. Comprehensive runs
4, adding `-l` without `-u` and `-l -u -q` into PCAP, where the file must hold
packets as well as a header. Every case also refuses a clock reported with no
score, or with a score only equal to the next best candidate's — see **A
clock that is reported is a clock that was scored** under
`Classic capture record fields`, which carries the same two guards and the
reason they sit on every capture case. Nothing here requires a clock to be
reached. The cleanup is the same: both peers reset, the Ubertooth restored.

**Known limits.** Same floors and the same bench dependence as
`Capture known Classic device`. The absence case asserts an exact zero over a
live capture: an access code is 64 bits and `-e` allows two bit errors by
default, so ambient traffic matching the acceptor's access code within two
bits is not something a run will see, but it is an absence over the air rather
than a proof. `ubertooth-follow` is a different tool, which reads the clock
and the AFH map from a local BlueZ controller; it has its own test now, under
`Follow Classic piconet over HCI`, on a bench with an adapter.

## Classic capture record fields

**What it does.** The same two peers hold the same link, and this test reads
what is *inside* the BR/EDR capture files `ubertooth-rx` writes: every field
of every record, decoded from the bytes, rather than the packet lines the tool
prints. What it asserts over the air is the records' addresses, headers,
power fields, packet types and flags, and their agreement with each other;
the payload checks it carries do not fire on this bench's traffic, for the
reason under known limits.

**Theory of operation.** The two Classic tests above count packet lines and
check a LAP. That leaves most of a capture unchecked: another packet's header,
a stale clock, a reference address that belongs to nothing, or a flag that
claims a check nobody made would all pass. The record is a documented layout
(`LINKTYPE_BLUETOOTH_BREDR_BB`, the pseudo-header both of libbtbb's BR/EDR
writers build), so it can be decoded and held to what it says about itself.

Three of the checks need nothing known about the bench, because they tie a
record's fields to each other:

- the payload's own length field, with the packet type's header size and CRC,
  must come to exactly as many payload bytes as the record holds;
- the header's HEC, run backwards, must give the reference upper address part
  (UAP) the record carries, in every record that says the HEC passed and
  carries a reference UAP to compare it with;
- the payload's CRC, recomputed with that UAP, must match the one carried in
  the payload, in every record that says the CRC passed.

The last two need the record to carry a reference UAP, which the record's own
flag says. libbtbb records what it checked whether or not it had an address to
record, so a record can report a passing HEC with no reference UAP beside it;
those two checks skip such a record rather than guessing at the UAP, and the
followed cases require a floor of records that do carry both.

A record that mixed one packet's header with another's metadata fails all
three. The rest are flag-to-field agreements of the same kind, for each flag that
describes a field the record carries: the noise flag is set exactly when the
noise figure is below the signal, the payload flag exactly when the record
holds payload bytes, the two reference flags exactly when the reference fields
hold an address, a valid check is never one that was not made, and the rate
nibble says what a basic-rate capture receives and nothing more. Two flags
are read and reported but not constrained, because this writer never sets
either: a decrypted payload and an aliased channel.

**How much the level figures vary.** Every rule above is satisfied by a
receiver that took one reading and reported it for every packet: the figure
is inside the radio's range and agrees with its own flag, and it says nothing
about any packet but the one it was taken from. That is what a `rssi_reset()`
which left the running estimate standing produced -- nearly every record
carrying one latched figure, where the bench then saw fifteen between -58 and
-94 dBm
([ubertooth#711](https://github.com/SqueakyFloors/ubertooth/pull/711)) -- and
what a table of recent maxima produced for a channel's first ten packets
([#754](https://github.com/SqueakyFloors/ubertooth/pull/754)). So each
capture must carry at least four different signal figures and at least four
different noise figures among the records that carry a measured one.

Those floors are measured, not guessed. Twenty 25-second captures of this
piconet at `2026-11-DEV2` (seven windows, the follow, role-swapped, audio and
survey captures of each) held 122 to 373 records, with 21 to 36 distinct
signal figures and 28 to 53 distinct noise figures among the 90 to 326
records carrying a measured one. The floors are a fifth of the lowest signal
count and a seventh of the lowest noise count, so a working receiver clears
them on a far thinner capture than any of those, while a capture whose
figures do not vary at all fails. The synthetic replay cannot stand in for
this: its fixture carries one fixed reading and no background estimate at
all, which is why the floors are here and not in
[Raw receive replay](#raw-receive-replay).

**A payload longer than the bank it arrived in.** No record may carry more
payload than one capture bank can hold. An Ubertooth sends a Basic Rate
packet as one bank of 400 symbols and libbtbb decodes it from that bank and
no further, so the payload has at most 278 symbols after the access code and
header -- 34 bytes where it carries no error correction, fewer where it does.
A decoder told it had ten banks reported a payload decoded out of whatever
followed the packet, CRC verdict and all
([#754](https://github.com/SqueakyFloors/ubertooth/pull/754)). This is
arithmetic rather than a measurement, so it holds for any capture; the
bench's own records carry at most 30 payload bytes.

The transport nibble is the one of these that the record earns rather than
declares. libbtbb names the logical transport the payload it produced decoded
on — ACL for the data types, SCO for the voice types, eSCO for the extended
voice types — and names none where it produced no payload, clearing it again
wherever it drops one, such as a trial clock it abandons
([libbtbb#133](https://github.com/SqueakyFloors/libbtbb/pull/133), which the
fork picked up in
[ubertooth#1122](https://github.com/SqueakyFloors/ubertooth/pull/1122)). So
two rules hold: a record with no payload names no transport, and a record
with one names its own type's. Only TYPE code `0111` takes two answers, and
that is the point of the field — an HV3 and an EV3 share that code and a
length, so nothing else in a record says which it holds, and so whether the
payload ends in a CRC. A record naming `any` passes either way, which is what
a libbtbb from before that change wrote for every packet, so a run against an
older `--libbtbb-ref` is not failed for a field that library never filled in.
No case puts a floor on how many records name a transport: payload-bearing
records are rare on this bench (see the known limits), so a floor would be a
count of the room rather than of the decoder.

**That `any` is a real gap, kept on purpose.** At the libbtbb the *fork*
pins, a payload-bearing record always names a transport — the library names
one whenever it produced a payload, and an abandoned trial clock clears the
transport and the payload together — so a libbtbb that stopped filling the
field in would pass here. What it covers is a run that names a libbtbb from
before [libbtbb#133](https://github.com/SqueakyFloors/libbtbb/pull/133)
itself, which names no transport at all. A run that takes the fork's pin, as a
deployed runner's `fork-pin` does, has named transports since that change; the
*harness's* own `LIBBTBB_PIN` and the README's example refs named an older one
until
[#341](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/341)
moved them, and those were the runs the escape was written for. A floor is no
substitute either way, at 11 payload-bearing records in 2620. Dropping it is
[#339](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/339),
whose one precondition, that pin, is now met; it is left there because it
changes the rule a live capture is judged by, which is that issue's subject.

On top of those, the bench
contributes what it knows: the piconet's LAP, the reference LAP and UAP the
capture was given, and, in a PCAPNG, the address `ubertooth-rx -l` records
once in an interface option. That address is only as much as the tool knows:
the LAP and, with `-u`, the UAP, with the non-significant address part zero
and marked invalid, so those are the fields compared.

The role-swapped case is what keeps the address checks honest. Which board is
the piconet central is decided by which one pages, so binding the pair the
other way round moves the LAP and the reference address in every record to the
other board; a decoder reading a constant, or a tool writing whatever it had
lying around, passes one case and fails the other.

The audio case puts a synchronous (eSCO) link on the air instead of the data
stream, at `sco type ev3`, so its payload is on the air at the basic rate the
Ubertooth receives; the records must then name `HV3/EV3/3-EV3` packets. That
is the one check here with no equivalent in the other Classic tests, because
voice packets never appear in a capture of the SPP stream. It is held to the
same floors as a stream capture, which the bench measured for it too: 394
records with 112 passing headers against 285 with 59 for the stream.

What the session has to show about the link, beside the records, is read from
both boards twice: once as late as possible before the capture starts and
once as soon as it has finished. Every figure the case matches is the
movement between those two readings, so it belongs to the capture's own
window rather than to the whole life of a board
([#264](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/264)).
Each board reports the frames its application sent and received, its send
errors and damaged frames, and the controller's own count of synchronous
packets - the packets that were on the air, as against what the two
applications made of them. A board whose controller sent or received nothing,
or received packets and not one of them correct, fails: frames handled over a
link the air never carried would describe a capture of nothing.

The driver also refuses a link that is not the one the case asked for: each
board has to report an eSCO link of the packet type both were told to ask for
before the capture starts, and the same link at the end as at the start, so a
capture taken while the two ends renegotiated is reported rather than matched.

`link 0 opened 0 dropped` on each board is the case's assertion that this was
one link throughout. A link that went down and came back leaves both boards
reading `on` either side of the capture and is still two links with a gap in
the middle, which no record in the file would show; the firmware counts the
links a profile has had so that a session can tell the difference, and the
connection handle is compared as a second witness for a link renegotiated
without a drop being counted.

The link is taken down with `sco stop` on both boards before they are
released, and both the stop and the release are checked. A board that refuses
to stop, or that does not come back to its boot state, fails the case - and
the measurements above are reported first either way, because a spent
30-second capture whose figures were thrown away because of what happened
afterwards is the worst of both outcomes.

A record carries no clock field, so no case asserts a clock value. What the
records do carry is their own times, and those are held to the run that
wrote them; the clock a followed capture reads is what can put them wrong.
See **The record times measure the run** below.

The decoder is `ubertooth_harness/bredrrecord.py`, run as an ordinary logged
command per capture file. It prints counts, ranges, flag tallies and packet
type names, and never a payload byte, so no traffic of the piconet reaches the
run record. Its two reverse computations, the HEC and the CRC, are checked in
the unit tests against the forward pair in `rxreplay.py`, which are written
from the Bluetooth Core specification for building synthetic packets, so an
error in either shows up instead of agreeing with itself.

**Hardware and arguments.** As `Capture known Classic device`: one `Ubertooth`
and two ESP32 Classic peers as `acceptor` and `initiator`, `coverage:`, a
required `duration:`, Linux only. The comprehensive profile's audio case needs
the peers on `utesp-1.5.0` or a later 1.x image: `sco type` arrived in
`utesp-1.4.0` and the two link counters in `utesp-1.5.0`. Preflight reports
the version either way, and the case fails naming it on anything older.

**Commands.** The same pair, with `bredrrecord.py` decoding every record of
the capture files:

```text
esp32peer.py stream <peer options> -- ubertooth-rx -U <serial> -t <duration> \
    -l <central LAP> -u <central UAP> -q <case dir>/records.pcap \
    -r <case dir>/records.pcapng
esp32peer.py stream <peer options> -- ubertooth-rx -U <serial> -t <duration> \
    -l <central LAP> -u <central UAP> -r <case dir>/swapped.pcapng
                                   # the roles exchanged, so the other LAP
esp32peer.py stream <peer options> -- ubertooth-rx -U <serial> -t <duration> \
    -z -r <case dir>/survey.pcapng # a survey, with no target
esp32peer.py audio  <peer options> --type ev3 --min-frames <n> \
    -- ubertooth-rx -U <serial> -t <duration> \
    -l <central LAP> -u <central UAP> -r <case dir>/audio.pcapng
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
bredrrecord.py <case dir>/records.pcapng --min-packets 10 --max-access-code-errors 2 \
    --max-payload-bytes 34 --min-distinct-signal 4 --min-distinct-noise 4 \
    --max-span-s <s> --max-backward-step-us <us> --min-hec-valid <n> \
    --lap <central LAP> --reference-lap <central LAP> --reference-uap <central UAP> \
    --following-lap <central LAP> --following-uap <central UAP> \
    --following-uap-mask <mask> --min-reference-uap <n> --require-types <types>
```

**Expected outcome.** Smoke runs 2 cases. `-l <central LAP> -u <central UAP>`
with both `-q` and `-r`, after which each file must hold at least ten records,
all of the central's LAP, all carrying that LAP and UAP as the reference
address, all internally consistent, at least five with a header whose HEC
passed and ten carrying the reference UAP, and the PCAPNG must name the
central's LAP and UAP, with a full UAP mask, as the address it was following; then the same with the roles swapped,
where every record must carry the other board's LAP instead.
Comprehensive runs 4, adding the EV3 audio link, whose records must include at
least one `HV3/EV3/3-EV3` packet and whose session must report one unbroken
link, both applications' frame counters, and both controllers' packet
counters, all measured across the capture alone; and a survey (`-z -r`), whose records must
include at least one carrying a UAP the survey recovered for itself.

Every case, over every capture file it writes, also requires each record's
transport nibble to agree with its own payload, and the file's record times
to span no longer than the capture ran and to step backwards no further than
2000 us. Every case also refuses a clock reported with no score, or with a
score only equal to the next best candidate's. Each case
keeps its capture files in the run directory. The cleanup is the same as the
other two Classic tests: both peers reset to their boot state, the Ubertooth's
channel, squelch, and LEDs restored.

**Known limits.** The packet floors are floors, for the same reason as the
other Classic tests: how much of a piconet a sweeping receiver hears varies
widely between runs.

The payload length and CRC checks need a link whose payloads are not
encrypted, which this pair's are not — they pair with nobody. A capture of an
encrypted piconet would decode neither, and `--no-payload-checks` is there for
one; no catalog case uses it.

**What a weak link is for here.** See "What attenuating a receiver is
for" above: the flag-to-field agreements of this test are what a
marginal link would exercise, and the measurements below are what the
bench found when it was tried.

**The payload checks barely fire on the bench's own traffic.** Measured over
four 25-second captures, no record carried a payload at all: `ubertooth-rx`
kept re-seeding its CLK1-27 candidates and never entered follow mode. A
larger sweep of 19 windows found a handful rather than none — 11
payload-bearing records in all, between 0 and 4 per window — and corrected
the explanation: 7 of the 11 came from windows that recovered no clock, so a
payload-bearing record is not something follow mode is needed for. They are
simply rare, 11 records in 2620. So no case puts a floor on records with a
payload, and the payload length and CRC checks are there for the capture that
does settle a clock — they are exercised deterministically by the unit tests
and by replaying the synthetic fixture, where a DH1 payload's length field and
CRC both have to agree. Whether the clock recovery should settle on this pair
is the fork's question.

A malformed record cannot be produced on purpose here. A well-behaved peer
will not send a payload header that disagrees with its own packet, or a
header whose HEC names another piconet, so what this test proves is that the
fields agree on real traffic, not that the decoder rejects a bad record. The
rejections are covered where they can be made deterministic: in the unit tests
here, which build records field by field and check each failure is named, and
in the forks' own tests, which drive libbtbb and the callbacks with crafted
input.

**The header flag fires; a failed CRC here is not yet a damaged packet.**
This is the entry `testplans/weak-link.yaml` still holds back, and the sweep
of 2026-11-04 gave it most of its answer — 19 windows of dense Serial Port
Profile traffic, 12 interleaved across four receivers at 15 seconds and 7 in
a repeat session at 15 and 25
([#306](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/306)).
A capture file over the weak link exists now, which is what this entry was
first waiting on, and the header's HEC flag does fire: checks failed in 4 of
the 19 windows, at 4.9% (26 of 534), 8.5% (4 of 47), and 0.2% (3 of 1395) in
the three 40 dB windows that saw any, and at 3.6% (6 of 167) on the clear
board.

**Attenuation does not order that**, which is the part worth not
re-learning: the unattenuated board had the highest rate of the four
receivers, 20 dB failed none of 195 checks, and 50 dB none of 49. Pointing
this test at a weaker link is not what would earn it, and 50 dB is unusable
outright — 62 records across six windows, no clock recovered in any.

The CRC half is the real blocker, and it is not a question of how often the
flag fires. Every CRC check made in any window failed, 11 of 11, which reads
as evidence until the records themselves are read: all 11 are DM1 carrying a
**valid HEC** and a payload length field that decoded **exactly**, at every
receiver including the unattenuated one, in windows with and without a
recovered clock. A BR/EDR payload is dewhitened with the clock, so that is
the signature of a payload decoded against a clock that never settled rather
than of a packet damaged on the air — the same open question as the clock
note above. A case that required the CRC flag to have fired would enshrine
that artifact rather than test the link. What this check waits on is a
capture whose CRC failures can be shown to come from the air, and a case that
requires a flag to have fired rather than only that the fields agree.

The survey case cannot assert whose records it reads. A survey is given no
address, so it records every piconet in range, and the bench hears ambient
traffic now and then; its checks are therefore the internal ones plus a count
of records carrying a recovered UAP. It is also the one case where most
records have no decoded header: once the survey has settled a piconet's UAP
it stops reading headers for it, so those records carry an empty header with
the HEC flags clear, and the HEC and CRC checks pass over them. A 25-second
survey on the bench recorded 235 packets, 182 carrying a recovered UAP and
one a header that had been checked, which is why the header floor belongs to
the followed cases and not this one.

**No clock value is asserted, and none can be.** The issue asked for clock
and clock-offset consistency, and there is nothing to assert it against. A
BR/EDR record has no clock field; `ubertooth-rx` never writes the interface
option libbtbb has for a piconet's clock; and the two figures it prints
beside each packet are already masked into range before they are printed (the
clock with `CLKN_MASK`, the offset modulo the slot), so a bound on either
could not fail.

**The record times measure the run.** What a record does carry is a
timestamp, and the clock a followed capture reads is what can put it wrong.
Following a piconet puts the board's clock on the piconet's, which moves it
by however far the two were apart — anything up to its 327.68 s period, in
either direction — and a reconstruction that read that move as time passing
gave times that did not measure the run at all. On the fork's bench a 15 s
follow wrote records spanning 191.3 s, and where the clock moved back
instead, a record's time came out before the record ahead of it
([ubertooth#1195](https://github.com/SqueakyFloors/ubertooth/pull/1195) fixed
it host-side). Every record check here now carries two bounds, which are what
that fault shows up as:

- the records span no more than the capture's own `-t` plus the slack a tool
  is allowed for its exit, so the ceiling moves with `duration:` rather than
  being a constant that stops meaning the run;
- no record is timed more than 2000 us before the record ahead of it.

Both are loose against a working capture and orders of magnitude clear of the
fault. The fork measured spans within 2.9 s of the run across nine follow
captures with the fix, against 41.3 s short and 176 s long without it, and no
step backwards at all in those nine, against steps of 41.16 s and 317.68 s.
Ordinary reordering stays allowed, because two packets decoded out of one
400 us bank land in either order: a bench capture of 285 records had one step
of 225 us, and a slot pair is 1250 us.

**A clock move cannot be ordered on the bench, so the directions are tested
synthetically.** This capture locks a piconet's clock on about one run in
nine here, and the size of the move is whatever the two clocks happen to be
apart, so neither a move nor its direction can be asked for. The fix also
only reaches a live capture, so replaying a saved dump cannot stand in. What
the hardware cases are therefore for is the live wiring — that the bounds
hold on a real capture, which is what would catch them being computed from
the wrong field or never run. Both move directions, at the sizes the bench
measured, are covered deterministically in the unit tests, which feed the
record reader synthetic captures carrying those times.

One thing could fail a good capture: the firmware latches the clock and its
period count with separate reads, so a packet caught as the count ticks over
carries a time a whole period out. No bench capture has shown one, and the
summary line names the size of what it saw, so a reader can tell a one-off
straddle of about 327.68 s from a clock move.

The summary line is there whether or not the bounds are asked for — how many
times a capture carried, over what span, and the largest step backwards — so
a run record holds them for a person to read.

**A clock that is reported is a clock that was scored.** A capture given a
piconet to follow searches for its clock, and every case here refuses two
outcomes of that search. Candidates are scored on the hops they explain
and a clock is accepted only while no other candidate explains them nearly as
well; where the hops cannot tell the candidates apart the search ends with no
clock, which for a caller is better than a wrong one
([libbtbb#125](https://github.com/SqueakyFloors/libbtbb/pull/125), which the
fork picked up in
[ubertooth#1088](https://github.com/SqueakyFloors/ubertooth/pull/1088); on
libbtbb's own synthetic sweep a fifth of the attempts used to confirm a clock
that was not the device's). So a lock line carrying no score at all is
refused, which is the form the elimination search printed when it accepted
whichever candidate outlived the others, and so is a lock whose score only
equals the next best candidate's, which is the coin toss between equally good
clocks. Neither writes down the margin libbtbb enforces: that is its constant
to tune, and asking for a clock strictly ahead of its runner-up is what these
cases need. **Nothing here requires a lock**, for the same reason the packet
counts are floors.

The two guards sit on every capture case of this test and of `Follow Classic
Bluetooth piconet`, rather than only on the ones that target a piconet.
Neither can match a capture that reaches no clock — which most of this
bench's do, and a survey never even starts a clock search — so they cost a
run nothing wherever they sit, and a clock reported unscored or tied is wrong
in any capture. Narrowing them to the targeted cases would drop coverage to
no end.

The flag rules assume this fork's libbtbb, the one the harness pins. It clears
the HEC and CRC answers for a clock trial it abandons, and drops the payload
with them; a libbtbb without those clears can write an honest record that
these checks call inconsistent. A run against another `--libbtbb-ref` should
expect that.

An EV3 capture's packet *names* are read from the records, not from the tool's
packet lines, which carry no packet type. `ubertooth-rx` does not settle on
this pair's clock from audio traffic, so it never enters follow mode on it;
none of these checks needs it to, because a packet line is printed for every
access code that matched and a record's header decodes without a clock of the
piconet. Whether the clock recovery should manage it is the fork's question,
not this test's.

## Classic btbr console

**What it does.** Runs the tool's command line for the option values it must
refuse before it looks for a board, writes the `btbr` firmware image to the
board, checks the board reports that image, drives one `ubertooth-btbr`
console session against a registered ESP32 Classic peer, and always writes the
application image back. It is the only test that puts a firmware image other
than `bluetooth_rxtx` on a board, and the only one that runs
`ubertooth-btbr`.

**Theory of operation.** `ubertooth-btbr` needs the separate `btbr` image and
nothing else runs it, so a board is set up for one tool or for all the others.
Three things make the answers knowable in advance.

The image identity: `ubertooth-util -v` names the image the firmware was built
from ahead of the revision, so the board must report `btbr` and the run's
revision after the write and `bluetooth_rxtx` and the same revision after the
restore. Reading the revision alone would pass a board on either image, and
the application-image check every other board gets would turn this board down.

The peer: the ESP32 Classic peer is discoverable and connectable from boot
(see [`hardware.md`](hardware.md)), its BD_ADDR is in the registry, and the
firmware builds its device name from that address, so both are known before
the run. An inquiry therefore has to hear it, and a page has to reach a device
that is in page scan. The peer answers the central's LMP name request with that
same name, which says on the air which device replied rather than that
something did. The reset before the session reads the board's own name and
checks it is the one the address gives, so the session is not left looking for
a name no board on the bench answers with.

The prompt: a word that is no command, `?` on a word that is no command, an
address the prompt will not take, and a `tx` command line the host will not
take are each one `error:` line with the prompt left up, and none of them
reaches the radio. That is the half of malformed input a shared bench can
produce safely. The prompt takes six bytes written with the five colons or
with none, and nothing else, because a word of another length names a
different address from the one that was typed; the refused list therefore
holds a short address, one with the colons in the wrong places, one with
another separator, and the two forms Python would read as a number
([ubertooth#1075](https://github.com/SqueakyFloors/ubertooth/issues/1075)).
What the console prints back is twelve hex digits whatever the value, so an
address whose first byte is zero comes back as an address the prompt takes.

The transmit formats: `tx` hands the board a packet built to a chosen
basic-rate format and prints, on the capture stream, the packet it asked for —
the data bytes, and the whole payload with the payload header and a `dv`'s
voice field. The driver builds the same packet for itself from the format
table in the tool's man page and compares the two, so the length, the counted
fill, the header, and the boundary between a `dv`'s voice and data fields are
each checked against something other than the tool's own table. The two fills
are deliberately apart — the data counts from `0x00` and the voice field from
`0xa0` — so where the voice field ends is visible in the bytes alone.

The session is driven by
[`btbrconsole.py`](../ubertooth_harness/btbrconsole.py), which starts the tool
once, runs every check down the same prompt, and reports one line per check:
the board has to be flashed before the session and restored after it, so a
second session would cost two more firmware writes. That is why one session
carries as much as it does — the refusals, the inquiry, the page, the
transmit runs inside that connection, the page again, and the two scan modes
([#332](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/332)).

The option values: `-f` and `-b` are written to fields of the radio, six bits
and two, and a value outside the field is refused as the command line is read,
before the board is looked for. That step runs first and needs neither the
board nor the image. Each refusal is given a `-U` naming no board by index,
and a control run shows that same selector reaching the stage that looks for
one, so what the step checks is the order and not only the message. An index
is used rather than a serial because matching a serial has to open every
attached board to read its own, and an index names a board without reading
any. One host cannot show the order at all: the tool looks for the board after
it imports the Python `libusb1` package, so on a host without that package the
control run stops a step earlier, and the step then skips with that as its
reason rather than passing on a control that showed nothing.

The mechanisms this bench cannot produce honestly. A transmit the board could
not put on the air, a scan window the scheduler had no room for, and a page
response that never reached the air are each one line on the board's console,
and none of them happens on a healthy board: showing one needs a firmware
image patched to drop a transmit, which is what the fork's own hardware runs
for [ubertooth#1183](https://github.com/SqueakyFloors/ubertooth/pull/1183)
did. Those mechanisms are covered by deterministic firmware tests in the fork
— `firmware/tests/test_btbr_tx_queue.c` (a dropped slot gives its repeat and
its acknowledgment back), `test_btbr_tx_hop_phase.c` (which phase a transmit
moves, including the four ways one is dropped),
`test_btbr_connection_end.c` (the report reaching the link layer),
`test_btbr_page_response.c` (a dropped page response starts no connection; a
page answer that did not go out goes back to scanning),
`test_btbr_inquiry_response.c` (the same for an inquiry response), and the
scheduling tests beside them (`test_btbr_scan_schedule.c`,
`test_btbr_rx_schedule.c`, `test_btbr_tx_schedule.c`,
`test_btbr_tx_untuned.c`) — and by
`host/python/ubtbr/tests/test_ubtbr.py` for a peer that declines the
connection. What this test adds is the other half: the observable end result
on a real bench. The page completes and exchanges the peer's name every time
it is asked, not once; the link carries packets after the transmit runs; the
scan modes run and return; no connection is reported and then given up
(`Paging resumed`, read once the connection has been held and stopped, which
is long enough for link supervision to have ended a connection that carried
nothing); this end hangs up on nobody who took the connection; and every
refusal line the board can print is counted.

Counted, not forbidden: each of those eleven lines is an exceptional condition
the firmware is *built* to survive, so up to three of one line are reported and
allowed — the checks around it say whether the session came through it — and
more than three of the same line fails, because that is no longer an
exceptional condition. The two lines the board prints about a `tx` this session sent (a
second run while one was still waiting for a slot, a full queue) are not
exceptional and either one fails. A pass therefore says those mechanisms did
not misfire here. It does not say the firmware handles them correctly — that
is what the fork's tests are for — and this file does not claim it.

One limit of that count is worth stating: those thirteen strings are the
board's own console wording, which the tool's man page does not promise. A
fork that rewords one leaves the count reading zero where it should not,
without failing anything. Guarding it would mean reading the firmware sources,
which a bench run does not have, so it is written down here instead.

**Hardware and arguments.** One `Ubertooth` and one ESP32 Classic peer as
`acceptor`; a required `allow RF transmission: true`, because the board
transmits an inquiry train, a page train, and then holds a connection as the
piconet central; `coverage:`; and `sessions:` (default 1) for how many
flash-and-session cycles to run. The default RF placement is an ordinary link,
so the planner keeps it off an attenuated board, and other registered
Ubertooths on the host are guarded.

The case needs a Linux host (the peer is reached through `/dev/serial/by-id`),
the Python tools, and both firmware images, and says so as needs, so a host or
a run that cannot do it leaves the case out before anything is flashed.
`run-suite --build --with-firmware --with-python` is the run that satisfies it:
the alternate image is named by the test itself, so a run that selected it
builds that image too. `--with-btbr` asks for the image outright, and
`--artifact "btbr build=PATH"` and `--artifact "test build=PATH"` name both
images by hand. A controller or GUI run asks for all three with its own
`with_firmware`, `with_btbr` and `with_python` options, which the GUI ticks
and holds for a suite holding this test
([#257](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/257),
[#279](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/279)).

**Commands.** `btbrconsole.py` writes the alternate image, drives the
prompt, and puts the application image back; `ubertooth-dfu` and
`ubertooth-util` are the writes and reads around it:

```text
btbrconsole.py options                                   # the command line, no board
ubertooth-dfu  -U <serial> -d <btbr image> -r
ubertooth-util -U <serial> -v                            # it reports btbr at the revision
ubertooth-util -U <serial> -V
btbrconsole.py session --serial <serial> --peer <BD_ADDR> --peer-name <name> \
    --peer-reset esp32peer.py --peer-device <peer port> --inquiry-seconds <n> \
    --page-seconds <n> --hold-seconds <n> --no-answer-seconds <n> \
    [--comprehensive] ...
ubertooth-dfu  -U <serial> -d <application image> -r     # the cleanup, always
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role acceptor --alias <alias>
```

The session's own `ubertooth-btbr` command line, and the twenty checks it
decides down the prompt, are `btbrconsole.py`'s.

**Expected outcome.** One case per session. The option-value step comes first:
four command lines refused with the line each value earns, one control run
that reaches the board stage, and nothing on standard output. Then the writes
and the version reads, then the session. The startup and the prompt come
first, and a session that does not reach the prompt fails there. Smoke then
decides 20 checks: `?`, `? <command>`, a word that is no command, `?` on one,
eight addresses the prompt will not take, `page` with no address, eleven `tx`
command lines the host will not take, `bdaddr`, an address whose first byte is
zero, the inquiry, the page and the LMP exchange, four transmit runs compared
with the packets their formats give, the link still carrying packets after
them, the hold and the `idle` that end the connection, the once-per-opcode
report of an LMP request with no handler, the page again with the peer reset
before it, the two scan modes, the count of the board's own refusal lines, no
hang-up on a peer that took the connection, and the split between the capture
on standard output and the progress on standard error (`idle` is checked inside the inquiry, the page and
each scan). Comprehensive decides 21: paging an address nothing answers must
end in `paging timeout` with no connection, and it also makes five more
transmit runs, covering three more formats, and pages the peer once more. The report says how many checks passed,
how many failed, and how many never ran, so a session that stopped early is not
read as a pass. The cleanup always writes the application image back, reads it,
and resets the peer; a cleanup failure fails the case, so the board cannot be
left on `btbr` quietly.

The capture itself never leaves the driver. An inquiry hears every discoverable
device in range, so passing it on would put third-party addresses and device
names into the run record; the checks read it in memory and report only the
records they matched for the registered peer, with the other devices counted
and not named.

Measured on the lab bench at fork `b2cd6c68`: each firmware write took 4 s,
the prompt came up in about 2 s, an inquiry heard the peer in about 2 s, a page
connected in about 7 s, and a page nothing answers reported its timeout after
the firmware's whole attempt of about 45 s.

The work added for
[#332](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/332) was
measured on the same bench at fork `80123099`, twice over: the option-value
step 0.5 s, each firmware write 3.5 to 4.1 s, the peer reset 1.7 to 1.8 s, the
smoke session 27.0 s and then 40.0 s, and the comprehensive session 88.6 s and
then 95.1 s — every check passing in all four. What varies is the radio: the
inquiry heard the peer in 2.1 to 6.4 s and a page connected in 0.3 to 17.4 s.
The catalog estimate therefore leaves room above the measurement: 80 s per
session for smoke and 140 s for comprehensive.

**Known limits.** A pass says the image runs, the command line refuses what it
should, the prompt answers, the board can run an inquiry, hold a connection
with one peer, transmit the chosen packet types on it, page that peer again,
and enter and leave both scan modes.

What a pass does not prove:

- **That a transmitted packet reached the air.** The board reports nothing
  back about a packet it sent, so the line `tx` prints is what was asked for.
  The packet the harness compares it with is the harness's own, which is a
  real check of what the host composed and no check at all of the radio. A
  receiver would need a second board following the piconet, and a count taken
  there could not tell a packet lost on the air from one the board never sent
  — which is exactly the distinction
  [ubertooth#1166](https://github.com/SqueakyFloors/ubertooth/issues/1166) is
  about, and why its gate is the fork's four firmware tests on `ll_tx_done()`
  rather than a bench recipe.
- **That the firmware handles a dropped transmit, an exhausted scheduler, or
  a page response that never went out.** None of those happens on a healthy
  board. The fork's deterministic tests are the gate (listed above); this
  test says they did not misfire in this session.
- **That a connection the peer declines is closed at once.** The registered
  peer accepts, and its shell has no way to make it refuse the connection
  request the setup leads to, so that path is unreachable with the bench's
  current fixtures. The fork's `test_ubtbr.py` covers it; this test checks
  the complement, that this end hangs up on nobody who accepted.
- **That a page works while the peer still holds the last link.** It does
  not, and the session resets the peer rather than pretending otherwise; the
  measurement is above. What the repeats say is that each fresh page train
  completes, which is the pager's own side.
- **That the scan modes answer anything.** Nothing on the bench pages or
  inquires on demand — the ESP32 peer's shell has no `inquiry` command and
  pages only through its SPP profile, which `btbr` cannot answer — so
  `page_scan` and `inquiry_scan` are checked for entering the state, running,
  and returning to standby, not for a connection or a response.
- **`discoverable`, `l2ping`, and `monitor`**, which are not run at all:
  `l2ping` needs a peer that answers a 4-byte echo request and `monitor` is a
  race against a real peripheral that often takes several attempts.
- **Encryption**, which `btbr` does not implement.

The unhandled-opcode check leans on the peer. Measured on the bench on
2026-11-01: with the link up, the peer sent `LMP_DECR_POWER_REQ` about four
times a second for the whole connection, this link layer refused each one, and
the tool named the opcode in exactly one `warning:` line. So a connection held
for the hold brings one, and the check requires that, because a check that
decided nothing should not read as a pass; a peer firmware that stopped asking
would fail it, and the reason says why.

## Recover AFH map

**What it does.** The same two peers hold a piconet whose Adaptive Frequency
Hopping map is chosen rather than discovered, and `ubertooth-afh` has to work
that map out from the air: a 40-channel map and the 20-channel minimum, each
compared with the map the piconet's leader reports the link really hopping
over.

**Theory of operation.** `ubertooth-afh` marks a channel used when it finds
the piconet's access code on it, so the map it ends up with is a measurement
of where it met the piconet. The peers make that answer knowable: `afh` on the
initiator, which leads the piconet, tells its controller which channels to
keep, and `status` reports both the map the controller took and the map the
live link is hopping over.

Three things make the comparison honest, and the cases do all three.

The map in force is not the map asked for. These controllers drop channels 0,
24, and 78 of their own accord, so a board told to keep channels 0 to 39 leads
a link hopping over 38 of them. The comparison uses the link's own map, and
the driver refuses a link whose map is not a subset of the requested one,
because that is not the piconet the case set up.

The map has to hold still. A leader may classify channels again while the link
is up, and then no one map describes the capture. The driver reads the link's
map at the start, the middle, and the end of the capture, and a reading that
differs from the others fails the case with all three printed.

Access-code errors have to be off. With the tool's default of two allowed bit
errors the bench saw it mark the whole band used, because ambient traffic
matches 64 bits within two often enough; with `-e 0` the same bench found
every kept channel inside a minute. So these cases run `-e 0`, and that
measurement, not the default, is the reason.

What is left is the channel edge, which is physics rather than software. A
receiver a few tens of centimeters from the piconet reads enough energy from
the channels beside the kept block to decode packets there, so the recovered
map comes out wider than the one in force: the bench's 40-channel run
recovered 57 channels against the 38 in force, the extras being the band next
to the block and a thinner scattering above it. The
comparison therefore judges two measures with tolerances rather than demanding
an exact match, and a plan can ask for a sharper edge with `rf:` (an
attenuated or more distant receiver) if a run's skirt makes it ambiguous.

**Hardware and arguments.** As `Capture known Classic device`: one
`Ubertooth` and two ESP32 Classic peers as `acceptor` and `initiator`,
`coverage:`, a required `duration:` (the capture window), Linux only. The
peers must run `utesp-1.3.0` or a later 1.x image, which is where `afh` and
the link map arrived; an older one fails the case naming what it found. The
Ubertooth defaults to needing an ordinary link, as the other two Classic
tests do, so a plan asking for the attenuated receiver has to say so; two
minutes is the capture window every plan that holds it uses.

**Attenuation makes this test better, which is unusual here.** Measured
2026-11-02 against the peers' known 38-channel map, 70 s per leg:

| receiver | channels marked | true found | false | missed |
|---|---|---|---|---|
| clear | 53 | 38 / 38 | 15 | 0 |
| **20 dB** | **38** | **38 / 38** | **0** | **0** |
| 40 dB | 36 | 36 / 38 | 0 | 2 |

The 20 dB board recovered the map exactly; both clear boards over-marked by
11 to 15 channels, and 40 dB starts eroding the other way. So
`classic-weak-link` runs it at 20 dB, where its committed tolerances are met
with the most room. Note that `-r` prints a timestamp and an 80-bit binary
map, not an `AFH map: 0x…` line; a grep for the hex form reports "no map" on
a run that worked.

The release plan holds it too, as the `smoke` profile, and it is the one
entry there that names a bench host, because the peers are on one host. It
carries `required hardware: true`, so the naming costs the other benches
nothing and hides nothing either: a registry without those peers, or without
that host, runs every other release test and records this one as blocked, and
the run finishes failed rather than passing with the recovery unmeasured. Its
smoke estimate is about 520 s, which takes the release suite to about 1182 s
(roughly 20 minutes) on the host it names; a target without the peers plans
about 662 s of the suite and fails on this step. The third, `-t` case stays in the
`classic-comprehensive` suite, which a release runs alongside the gate.

**Commands.** `esp32peer.py stream` holds the piconet to a chosen channel map
and reports the map the live link is hopping over; `afhmap.py` compares that
with the one `ubertooth-afh` worked out:

```text
esp32peer.py stream <peer options> --map <20 hex digits> \
    --map-report <case dir>/afh-peer-report-<n>.json \
    --command-output <case dir>/afh-maps-<n>.txt --interrupt-after <seconds> \
    -- ubertooth-afh -U <serial> -l <central LAP> -u <central UAP> -e 0 -r
esp32peer.py stream <peer options> ... -- ubertooth-afh ... -t <duration>  # comprehensive
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
afhmap.py --peer-report <case dir>/afh-peer-report-<n>.json \
    --maps <case dir>/afh-maps-<n>.txt --min-found-share 0.9 --max-stray-share 0.7 \
    --min-dropped-channels 10
```

**Expected outcome.** Smoke runs 2 cases, one per map: channels 0 to 39, and
channels 30 to 49, the twenty the spec allows at least and the firmware's own
floor. Each one sets the map, brings the link up, runs `ubertooth-afh -e 0 -r`
for the capture window, and then judges the recovery:

- at least 90% of the channels the link was hopping over are in the recovered
  map. Finding a channel means the sweep and the hop sequence met on it, so
  the last one or two of a map are a matter of luck in a window this long:
  the bench's two 120-second runs found 37 of 38 and 19 of 20, each missing a
  different single channel, and their per-minute series went 31 of 38 after a
  minute to 37 after two. The floor leaves room for one more than that and
  still fails a recovery that stopped working;
- at most 70% of the channels out of force are marked used, which is what
  separates a recovery from a tool marking everything used. The same runs
  marked 49% and 22%. The share is left unjudged, and said to be, when fewer
  than ten channels are out of force.

The measurement is printed either way, because a run that missed a tolerance
is still the measurement of what the tool did: the requested map, the map in
force with its three readings, the recovered map, the channels found and
missing, the stray channels, and a line per minute of the capture showing what
the map had grown to by then. Comprehensive runs 3, adding the tool's other
path: `-t <duration>` detects the map for that long and prints it as
`AFH map: 0x...`. That map gets a looser stray ceiling of its own, 90%: the
detection phase fills a single channel gap between two channels it has seen,
and never drops a channel once seen, so its map is an upper bound. Two runs
bracket how wide: 71% of the channels out of force on an earlier `-t 60` run
of the bench session that measured the peers' maps, and 32% on this test's own
`-t 120` run. 90% clears both and still fails a map that marks the whole band
used. Selectivity proper is what the two `-r` cases measure.

A capture and its comparison are one retried group, run up to twice: whether
a channel was heard depends on where the sweep and the hop sequence met, and
the peers keep making the traffic, so a second window costs only its own time.
Each attempt writes its own pair of files, so the run directory holds what
every window measured.
The cleanup is the same as the other peer cases: both peers reset to their
boot state, the Ubertooth's channel, squelch, and LEDs restored.

**Known limits.** The tolerances are the bench's, not the tool's: the stray
ceiling says the recovery is selective, not how selective, and a receiver
placed differently will measure a different skirt. A pass says the tool found
almost all the channels in use within one capture window of this length, not
that it found every one, and not that it would find them in a shorter window:
the two measured runs were each one channel short of the whole map at two
minutes and still climbing. The capture is stopped by the harness rather than
by the tool, because `ubertooth-afh` watches for map changes until it is
signalled; the map-change path itself is therefore not checked, only that the
map did not change. `-m`, the removal threshold, is left at its default, so a
channel the piconet stops using inside a capture is not something these cases
measure.

## Bluetooth inquiry scan

**What it does.** Runs `ubertooth-scan`, which drives a host Bluetooth
adapter and an Ubertooth at once, against the piconet two registered ESP32
Classic peers hold. With `-s` the adapter's inquiry must report both peers
by address and one of them by the name the peer firmware gives it; with no
`-s` the Ubertooth's sweep must report that piconet by the central's LAP,
with the channel map the scan built for it. `-x` has no case, for the reason
below.

**Theory of operation.** The tool has two halves. The Ubertooth's sweep runs
always; `-s` adds the adapter's inquiry before it. So the cases differ by what
they require, not only by what they run: the `-s` case requires the
inquiry's result alone, so a bench whose sweep happened to hear no piconet
still tests the adapter, and the other requires the sweep's. Both profiles
run those same two cases.

`-s` is the adapter's half: a BlueZ inquiry, and the name read back from each
device that answers. Both ESP32 peers are discoverable and connectable from
boot and stay so for a whole session, because switching a board's role does
not touch its scan mode (`firmware/esp32-classic-peer`, `disc`). So both must
be in the results. Their names are knowable in advance without reading the
boards: the firmware builds a name from the LAP as `UT-ESP32-<LAP>`
(`bt_peer.c`), which `esp32peer.device_name()` repeats and a unit test keeps
in step with that source. One of the two must come back under its own name,
rather than a named board, for the reason under the known limits below.
Other discoverable devices in range are reported too and are not held
against the run.

The other half is the Ubertooth's: the board sweeps the whole band and
libbtbb keeps a piconet per access code it hears. The initiator pages, so it
leads the piconet and its LAP is the access code on the air
(`ubertooth-rx(1)`, DISCOVERING UNDISCOVERABLE DEVICES), which is why the
central's LAP is what the scan results must name. Each result is followed by
the map the scan built for that piconet, as `AFH map: 0x<20 hex digits>`, so
a run that reported a piconet and no map did not finish its result.

The pair is held up by one `esp32peer.py stream` session per case, as the
other Classic tests are: opening an ESP32's bridge resets it, so the pair
cannot be set up in one step and used in the next. `ubertooth-scan` bounds
its own sweep with `-t`, so the driver does not interrupt it; it only refuses
a run that stopped early.

**`-t` is not the whole run.** The tool arms the alarm `-t` asks for only
after the BlueZ inquiry has returned (`main()` in `ubertooth-scan.c`: the
`if (scan)` block comes before `ubertooth_set_timeout()`), so `-s` adds a
whole inquiry — BlueZ is asked for 8 units of 1.28 s, about 10 s — on top of
the window. Each device either half found is then paged for its name. The
cases allow for both in their deadlines, so a `duration:` of 25 s describes
the sweep and not the case.

The adapter is read twice before the tool runs. `hciconfig <device>` says it
exists, it is up, and its address is the one the registry records: the `hciN`
number is the order the kernel attached controllers in rather than a property
of the adapter, so the address is what identifies it, and an adapter that came
back as another `hciN` fails here, naming what was found, instead of the test
driving whatever took the name. `hcitool -i <device> con` then says it holds
no **established** link, so a case does not start with a link an earlier run
left up. The same check runs again, first thing in the cleanup.

Reading the right adapter and *driving* it are two things, and on a host with
one adapter they look the same. Dropping the device from either read would
not fail it: `hciconfig` with no device lists every controller and
`hcitool con` with no `-i` reports every up controller's links, so the output
still holds the bound adapter's block and the patterns above still match,
while the links being refused are any controller's. The fork tools do not
fall back so quietly — without `-b` they take the host's first available
controller — but nothing in the output would say which one that was. Every
command these cases run against a controller therefore names it —
`hciconfig <device>`, `hcitool -i <device> con`, and `-b <device>` for the
fork tool — and because output cannot settle it, a unit test checks the
commands themselves: it builds every case in both tests against a second
adapter and fails if any command, pattern, or expected state would reach the
first.

A pending page, which `hcitool` reports as `state 2`, is not held against a
run: the scan's own name reads leave them behind and the kernel times them
out, and the bench saw three to five at a time, all gone shortly after. An
established link is still refused.

Any device's link, not the peers' addresses alone, and the distinction
matters: the survey half pages the address the scan assembles from a result,
`00:00:<UAP>:<LAP>`, which is not a registered address, and that is the link
most likely to be left behind — the bench confirmed it, among those pending
pages. A link to either peer cannot survive the step in any case, since
`esp32peer.py stream` resets both boards on its way out whatever happened. So
the check before the tool is the stronger of the two: it is what makes the
state afterwards mean anything.

**Hardware and arguments.** One `Ubertooth`, one `Bluetooth adapter`, and an
`acceptor` and an `initiator` (ESP32 Classic peer, `utesp-1.0.0` or a later
1.x image; nothing here flashes them); `coverage:` and a `duration:`, which
is the scan's `-t`. Linux only: the tool is built just with libbluetooth, and
the peers are reached through `/dev/serial/by-id`. The cases also need
`hciconfig` and `hcitool` on the host. Lives in the `hci` plan.

**The page needs a privilege the rest of the harness does not.** The harness
runs as an ordinary user, which is enough for every read here: the bench
measured `hciconfig`, `hcitool con`, `hcitool inq`, `hcitool name` and
`hcitool clock` all working with no capability anywhere. What an
unprivileged process cannot do on a raw HCI socket is page —
`hcitool cc <peer>` answers `Operation not permitted`, and so does
`ubertooth-follow`, which pages its target in every case. So
`ubertooth-follow` needs `CAP_NET_ADMIN`, and `ubertooth-scan` needs it for
`-x` only; `hciconfig` and `hcitool` need nothing. See
[`docs/hardware.md`](hardware.md) for how a bench grants it.
`hostneeds.Needs` has no way to express a capability, so nothing checks this
before the run.

**Commands.** The adapter is read before the tool and first thing in the
cleanup, and the pair's link is held across `ubertooth-scan`:

```text
hciconfig <hci device>                    # up, and at the registered address
hcitool -i <hci device> con               # no link at all
esp32peer.py stream <peer options> -- ubertooth-scan -U <serial> -b <hci device> \
    -t <duration> [-s]
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
```

**Expected outcome.** Two cases, in smoke and comprehensive alike: `-s`
alone, where the inquiry must report both peers at their registered addresses
and one of them under its firmware name, and no `-s`, where the scan results
must name the central's LAP and carry the `AFH map:` line the scan built for
it. Each case also
requires the driver's own
report — both peers' addresses, the central's LAP and UAP, a link that came
up, and a pair that streamed — so a capture of nothing cannot pass as a
capture of a quiet piconet. Each case then checks the adapter holds no link
and is still itself, resets both peers, and restores the board's channel,
squelch, and LEDs — the adapter reads first, because a peer reset drops every
link that peer holds.

**Measured.** Both cases ran against a USB adapter on a Linux bench, with the
pair streaming and one Ubertooth sweeping, and both settled what was open.
The test took 112 s in one run and 155 s in another; the difference is how
many of other people's devices answered the inquiry and had to be paged for a
name, which is also why the catalog estimate carries the slower figure.
The acceptor answers the inquiry while it is in a link: every run listed both
peers, alongside one to three of other people's devices in range. A
25-second window left the sweep the pair's piconet to report, with the
channel map the scan builds for it, in each run.

**Known limits.** The allowance for the name reads is the one number in
either test that is not derived from a tool's own deadline, and a crowded
room is what it is spent on. Each read is bounded at 25 s, and the allowance
is 60: the measured commands took 78 s and 55 s against a 111 s window, so
two silent neighbors would use it up on their own. A busier room fails on the
driver's deadline, which reads like a hung tool; what distinguishes the two is
the tool's own `note: reading the name of <address> failed` lines, which are
in the run record on standard error.

A device's name is read over a page of it, and on a bench
with strangers' devices in range the name can land on the wrong line: one run
printed the acceptor's address against a television's name, while the
television, whose own name read had just failed, was printed as `[unknown]`.
The tool reads each name into a buffer of its own, so the swap is below it,
in the reply the library matched to the read — [fork
#1172](https://github.com/SqueakyFloors/ubertooth/issues/1172). Six runs and
five further inquiries on the same bench had every name right. Nothing here
can tell a swapped name from a wrongly reported one, so the case asks that
**one** of the two peers came back under its own name, which proves a name
read worked, rather than naming which; both peers wrong still fails.

What that floor on its own let through is the swap itself. A failed read
prints `[unknown]`, so a peer's line carries either its own name or that
word, and the case now fails a peer reported under any other name: that is a
name which came from somewhere, which is the fault fork
[#1175](https://github.com/SqueakyFloors/ubertooth/pull/1175) fixed, and the
one run that showed it passed the floor because the other peer was right.
This costs no reliability -- it leaves the two `[unknown]` runs alone and
fails only the run that carried the fault. Whether to go further and require
**both** peers under their own names is a separate decision: it would have
failed 3 of those 19 runs, and two of them were a page that went unanswered
rather than a misattribution, so it is left to
[#384](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/384).

One thing the tool does that no case can prevent. It reads the name of
**every** piconet its sweep heard, by paging the address it put together, so
it pages devices this bench does not own if any are in range. That is the
tool's design, not something the harness chooses; the bench's ambient Classic
traffic is sparse, but a run on a busy site will transmit to strangers'
devices. Their addresses land in the run record, which is why no run output
is committed.

**`-s -x` is not exercised, and the bench settled why.** `-x` fetches its
per-device report over a connection it makes to each device, and the acceptor
is in the pair's link throughout, so the connection does not happen: a run
had the inquiry list the acceptor and its extended report give the address as
nothing at all. An idle acceptor would answer, but then the sweep has no
piconet to report, and with two Classic boards the two cannot both hold. What
the case wants is a third Classic device, idle and connectable, which this
bench has not got ([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).

The same run showed why the case could not have pinned an exit status either.
`-x` asks every piconet the sweep heard as well, and about twenty of those
reads failed against devices the bench does not own, each of which makes the
tool exit 1. Accepting 1 instead would accept the other two things that
status means — a capture stream that died, and a board that would not give
its mode up — and neither is visible to this case's own checks.

`-e` is not exercised: no case passes it, so the survey runs with the tool's
default of two allowed access-code bit errors.

**Why the `hci` plan is still not in the release gate.** Both suites pass:
smoke in 300 to 407 seconds over seven runs, comprehensive in 390, 400 and
526. Its smoke
estimate is 345 s, which would take the release suite from about 1182 s to
about 1527 s — roughly 25 minutes — on the one host that has an adapter, the
way `Recover AFH map` already names a bench host. What holds it back is not
the adapter but the peers. Two faults were measured while running it, both
peer-side:

- bytes lost on a bridge's serial line while an Ubertooth captures on the
  same host, which failed 7 of 91 pair sessions over an evening
  ([#310](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310));
- peers that would not come back to their boot state when a bridge was
  reopened, 3 of those same 91, and one peer in 47 follow runs that was back
  at boot state mid-capture, which the driver reports as a pair that never
  streamed
  ([#311](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311)).

That is 11 of 91 sessions, so a four-case suite fails about two runs in five
and a five-case one nearly half. Both faults also reach the pair tests
already in the gate. So the plan runs on its own, and goes into
`release.yaml` when they are fixed.

**A host without `hciconfig` and `hcitool` runs nothing.** Both are declared
as host tools the harness does not install, and a missing host tool makes a
case *not applicable* rather than failing it. They are BlueZ's older tools
and some distributions have moved them out of the main package, so a bench
that has an adapter but not them would run this plan with every case left out
and the run would pass having exercised nothing. Read the `Not applicable`
section of a run's report before believing an `hci` run proved anything.

## Follow Classic piconet over HCI

**What it does.** Runs `ubertooth-follow`, which pages a target from a host
Bluetooth adapter to learn that target's clock offset and then follows its
piconet with the Ubertooth. The target is the ESP32 peer that leads the
piconet. With `-a` the tool additionally asks that link for its channel map,
so that it can follow with Adaptive Frequency Hopping on — which on this
bench it is told there is no map to have, since the peer runs the paged link
with AFH off. So what the `-a` case measures is the tool reaching the read
and following without a map; the branch that applies one has no fixture here,
and the expected outcome below and the known limits say so.

**Theory of operation.** `ubertooth-follow` is the one host path that gets a
piconet's clock from somewhere other than the air: it reads the local
adapter's clock over HCI, pages the target, reads the target's clock offset,
and adds the two. That is why it needs an adapter and why an Ubertooth cannot
stand in for one. `-a` is likewise the only path that could put the board
into AFH-enabled following, by reading the map over HCI from a link the tool
holds; whether it does depends on the answer, and on this bench the answer is
that the link is not using AFH.

The target is the **initiator**, not the acceptor: the initiator pages the
acceptor, so it leads the piconet and its LAP is the access code on the air.
Following the acceptor would be following a piconet that is not transmitting.
The tool says which piconet it read out of `-t`, on standard error, as
`Target <address>: LAP=<lap> UAP=<uap>`, and the cases pin that line, so a
run whose packet lines matched for another reason is still held to having
been given the piconet the checks look for.

Given the clock, the check is the one `Follow Classic Bluetooth piconet`
makes of `ubertooth-rx -l -u`: libbtbb is looking for one access code, so
every packet line must carry the central's LAP and a line with another LAP
fails the case.

`ubertooth-follow` has no capture timeout — its `-t` is the target address —
so it captures until it is signalled, and the driver is what signals it.

**The window the driver gives it is not the capture.** Everything the tool
does before it starts capturing happens inside that window, so the window is
the `duration:` **plus** an allowance for all of it, which is what makes the
capture at least as long as the plan asked for. A window of the duration
alone would have left a slow page no capture at all, and `--min-seconds`
could not have caught it, because the page would have satisfied the floor on
its own. The floor is the whole window, less half a second, so a command that
returned sooner stopped for a reason of its own.

Most of the allowance is the tool's own deadlines, which it cannot exceed
without failing instead: BlueZ is given 25 s for the page, 1 s each for the
clock and the clock offset, 10 s to drop the link to the target again — which
it does immediately before the receive loop — and, with `-a`, three
channel-map reads with a fifth of a second between them. The rest has no
deadline at all: opening the board, checking its firmware API, creating the
capture files, setting the target address and zeroing the clock, and starting
the transfer thread. A margin covers those. The adapter is read before each case and checked for a
leftover link afterwards: the tool drops the link to the target before the
capture starts, so a link still up at the end is one this host is holding.
The adapter reads, and the rule that every command names the bound device
rather than letting a tool fall back to the first controller on the host,
are as described under `Bluetooth inquiry scan`.

**Hardware and arguments.** The same four roles, arguments, platform, and
host tools as `Bluetooth inquiry scan`, and the same `hci` plan. The
`duration:` is a **floor** on the capture, not an option of the tool's and not
the length of the command: the driver adds the setup allowance to the window
it interrupts at, so a tool whose page was answered at once captures for most
of that allowance as well. Measured on the bench, a `duration: 25s` follow
case runs the tool for about 83 seconds and captures for most of them.

**Commands.** The same adapter reads, and `ubertooth-follow` pages the
piconet's leader from it; the driver interrupts it, since its `-t` is the
target address rather than a timeout:

```text
hciconfig <hci device>
hcitool -i <hci device> con
esp32peer.py stream <peer options> --interrupt-after <seconds> \
    -- ubertooth-follow -U <serial> -b <hci device> -t <central address> \
    [-a] [-q <case dir>/follow.pcap]
esp32peer.py reset --device <peer port> --address <BD_ADDR> --role <role> --alias <alias>
capturefile.py <case dir>/follow.pcap --packets
bredrrecord.py <case dir>/follow.pcap --min-packets 10 --lap <central LAP> \
    --max-access-code-errors 2 --max-payload-bytes 34 --min-hec-valid 0 \
    --reference-lap <central LAP> \
    --reference-uap <central UAP> --min-reference-uap <n> --any-transport
```

**Expected outcome.** Smoke runs two cases: `-t <central>`, where the tool
must report following that piconet, say it is not using AFH, and print at
least ten packet lines, all with the central's LAP; and `-t <central> -a`,
which must report that the remote is not using AFH and follow without it,
with no failed map read and no error — the outcome this bench gives, since
the peer runs the paged link with AFH off. Comprehensive adds `-q`, whose
file must be a BR/EDR PCAP holding at least ten packets, with the packet
lines on standard output checked as in the other two cases. The records
themselves are not decoded; see the known limits below for the measurement
behind that. As with
the scan, every case requires the pair's own report, reads the adapter before
the tool and again first thing in the cleanup, then resets both peers and
restores the board.

**`-l` and `-u` without `-t` are deliberately not exercised.** The tool then
has four of the six address bytes and asks BlueZ to connect to
`00:00:<UAP>:<LAP>`; the fork's message says to give the whole address with
`-t` ([fork
#316](https://github.com/SqueakyFloors/ubertooth/issues/316)). Whether that
page is refused is not knowable in advance: a page carries only the LAP and
the UAP (Bluetooth Core, Vol 2, Part B 1.3, 6.4.6), so the two guessed bytes
need not decide whether it arrives. A case either way would be asserting an
outcome nobody has seen, so there is none.

**Measured.** Every case ran against a USB adapter on a Linux bench, and the
two questions that could not be answered from the source are answered.

The ESP32 takes the second link. Paging it from the adapter makes it a
peripheral in the adapter's piconet while it is already leading the one it
holds with the other peer, and Bluedroid allows that: the page returned the
clock offset in every run, and the pair's own link stayed up and streaming
through it.

Following the target's clock does yield that piconet's packets. A 25-second
capture carried a few hundred packet lines, every one of them the central's
LAP and none of another piconet's — 248 in one run and 368 in another.

**Known limits.** What those packets' headers say is another matter, and it
is what fork
[#518](https://github.com/SqueakyFloors/ubertooth/issues/518) is open about.
The `-q` case wrote 258 records, every one carrying the central's LAP and
that LAP and UAP as its reference address, and then: **none** of them passed
a header HEC, where `ubertooth-rx -l -u` on this same pair passes a fifth to
a third of them, and 35 of them carried `transport 3`, ACL. Both say the same
thing — the access code is found and the header does not decode, which is
about the clock the tool hops on — and a transport is checked against the
packet type the header carries, so a header that did not decode leaves
nothing to check it against. What those 35 records would carry today is **not
established**: that sweep ran against a different libbtbb than the one now
pinned, and at the pinned commit an abandoned trial clock clears a record's
transport and its payload together. The figures above are kept as measured.
The two level floors `Classic capture record fields` carries are left out
here for the same reason the HEC floor is: they were measured on captures of
the ESP32 piconet taken by `ubertooth-rx`, and how much a level varies across
a capture this tool takes is **not established**. The payload bound, which is
arithmetic rather than a measurement, is checked.
So the `-q` case checks that the
file is a BR/EDR pcap holding the piconet's packets and leaves the records to
`Classic capture record fields`, which reads a capture from a tool whose
headers do decode. A bench measurement is what fork #518 was waiting for.

`-a` finds no map to read on this bench, and the case pins that outcome. The
peer reports AFH **off** on the link the page made, so the tool says "the
remote device is not using AFH; following without it", clears `-a`, follows
anyway, and exits 0 — which is its documented behavior for a remote with AFH
off, and what the case therefore requires, forbidding an `AFH map read:`
failure or an error. Whether a link runs with AFH on is the central's choice,
and the adapter's link to the peer does not, so the branch that reads a map,
prints it and hands it to the board is **not** exercised: it wants a peer
that offers one ([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).
That is also narrower than it sounds even when it runs: nothing reads the
board back, so what such a case could check is that the request was made and
accepted. And because the tool takes its disabled branch here, it clears the
board's map rather than leaving one set, exactly as a follow without `-a`
does — so neither case leaves a map behind for the next one. The map the tool reads is of the adapter's link to the peer, not of
the piconet the Ubertooth follows, and the two need not agree, so comparing a
map with a known one stays `Recover AFH map`'s job, from the air.

Whether the allowance for the setup is enough is still not bounded, only
margined. The window the driver interrupts at is the capture plus that
allowance, and the parts of the setup with no deadline of their own are
covered by a margin. If the setup ever outruns the whole allowance the
capture is shorter than the plan asked for and nothing notices, because the
tool prints no mark for the moment its capture begins. On this bench it did
not: the captures came back full length in every passing run. A bench that
sees short captures should widen `BOARD_SETUP_S` rather than the duration.

`-w`, `-e`, `-r` and `-d` are not exercised here.

## BLE soak

**What it does.** Nothing yet: a catalog entry for a long BLE monitoring run
with periodic sampling.

**Theory of operation.** Not established. The sampling loop a soak needs (read
something every `sample every` and judge the series) is not implemented for
any of the three soak entries.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Spectrum soak

**What it does.** Nothing yet: a catalog entry for a long spectrum capture
with periodic sampling.

**Theory of operation.** Not established; see `BLE soak`.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Classic Bluetooth receive soak

**What it does.** Nothing yet: a catalog entry for a long BR/EDR receive run
with periodic sampling.

**Theory of operation.** Not established; see `BLE soak`.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Flash reliability

**What it does.** Writes firmware to a board many times in a row, alternating
two images, and verifies every write.

**Theory of operation.** What makes each write verifiable is the pair of
images. Both are pinned by SHA-256, the test build against the run manifest
and the known-good image against the plan's own hash, checked before the run
and again before each write, so a corrupted or swapped file cannot be mistaken
for a flash failure; `ubertooth-dfu -d ... -r` reads its own write back
against the file it wrote; and `ubertooth-util -v` afterwards must report the
revision the image just written carries.

The images have to be different files, and that is the whole of what the
sequence requires. It used to require a distinct expected revision per image
as well, on the reasoning that every write has to change what the board
reports. Since
[ubertooth#1230](https://github.com/SqueakyFloors/ubertooth/issues/1230)
(`ubertooth#1232`) a version names the release a build came from rather than the
build, so two images can be one release and two files -- and it is the file,
by its SHA-256, that tells them apart. The revision read after each write is
then what it says it is: the right release is on the board. Which of two
builds of that release it is, it does not say, and with the published
known-good image on one side of the alternation (a different release) it does
not have to. Every other
registered board on the host is read before the run and after every flash, so
a write that reached the wrong board is caught rather than inferred.

**Hardware and arguments.** One `Ubertooth`, selected by full serial, plus
every other registered board. Required: `platforms:`, `flashes:` (the total
number of writes), `image sequence:` (at least two image labels, naming
files that differ),
`KGV sha256:`, and `KGV revision:`. The images are supplied as artifacts named
after the labels: the run's own build provides `test build` (`--build
--with-firmware`, or the controller's and the GUI's `with_firmware` option),
while the known-good image comes from `--artifact "KGV=PATH"`, which only the
command line passes, so this test runs from the command line; the GUI says so
for any suite that names it. `alternate Ubertooths: true` rotates the writes across every
registered Ubertooth on the host, in alias order; then `flashes` must divide
evenly by the board count, `hosts:` must name its hosts rather than `each`,
and `Ubertooth: each` is refused. A named host with only one registered
Ubertooth cannot alternate anything, so the step is blocked there and the run
fails rather than passing without it (see "Hardware a step is about"). The test establishes the firmware it
verifies, so preflight checks only the board's serial and not its revision,
and the test must be the last one on that board in the suite.

**Commands.** Per write, with the image alternating between the labels the
plan's `image sequence:` names:

```text
ubertooth-dfu  -U <serial> -d <image> -r      # one write at a time, 25 s each
ubertooth-util -U <serial> -v                 # the revision: the written board
                                              # after every write, and every
                                              # other registered board before
                                              # the run and after each write
ubertooth-util -U <serial> -w                 # the watchdog record, between writes
```

The flash runner assembles these per write rather than as plan steps, so
`<serial>` is each board in turn when `alternate Ubertooths:` is set. Every
board is read with `-v`, the written one and the others alike: the flash
runner reads the other boards itself rather than through the catalog's guard
machinery (`guards_other_ubertooths` is off here), so the revision is the
only thing read of them.

**Expected outcome.** One case, run by the flash runner rather than as a
command line. Because it is one case however many flashes it runs, the run's
firmware watchdog reads happen between its writes rather than between cases,
on the same three-minute interval, so a long soak reports a board that reset
itself while it is still running; what a read takes is added back to the
test's deadline. It passes when every requested write completed and verified. It
fails and stops at the first mismatch: a write that did not take, a revision
that does not match the image just written, a guard board whose revision
changed or that disappeared, a per-flash write over 25 s, or the whole-test
deadline. The report names the board for each flash and the per-board counts,
and records the last revision read from each board.

**Known limits.** It measures the write-and-verify path, not what the firmware
then does. Selectors must be full serials: libubertooth reads an all-digit
`-U` value as a USB index and otherwise matches, case-insensitively, the start
of a serial, so the test refuses digit-only selectors and any selector that is
the beginning of another, which would name the same board twice. A manual
recovery or a power cycle never turns a failed run into a pass.

## DFU images

**What it does.** Covers `ubertooth-dfu` beyond the write path on a real
board: `-s` suffixing, `-u` read-back of what was written, and refusal of bad
images before any write.

**Theory of operation.** The first case writes the test image and reads it
back, so from then on the flash contents are known, and every later case can
assert them. That is what makes a refusal case meaningful on hardware: after
a bad image is refused, the read-back must still find the test image, which
proves the refusal happened before the write rather than after a partial one.
`-s` is checked against the build's own `.dfu` byte for byte, so the host-side
suffixing has an exact right answer.

A read-back is compared with the image as `-d` writes it, which is not quite
the file. `ubertooth-dfu` fills in two things in the image's first block for
the bootloader's own checks: the LPC vector checksum at 0x1c, and, in an
image that carries an image check block, the record of the download at
0xea-0xf7 inside it. Those fields are checked against what the writer should
have worked out -- the eight vector words adding up to zero, the flags, the
length of the region written and its complement, and the checksum that makes
the whole region add up to zero -- rather than passed over, so a board that
kept the file's empty values fails. Every other byte has to match the file.
How long a dump is follows the part: 114688 bytes above the bootloader on a
128 KiB part, 245760 on a 256 KiB one, and the size the firmware is linked
for when the part cannot be read, which includes every board already in the
bootloader. The `read-back` step records the range the command printed
(`Reading back 114688 bytes, 0x4000-0x1ffff: part 0x25011722 (LPC1754) has
128 KiB of flash`) and the comparison takes the length from there, so no part
is assumed; a tool that prints no range fails that step rather than having
its dump measured against a guess.

The length still has to be one the fork's part table explains, and the
harness keeps its own copy of the flash sizes in that table. A length outside
it fails the step and says which flash size it would belong to (`the read-back
covers 1032192 bytes, which no part in the harness's copy of the fork's part
table explains (32 KiB, 64 KiB, 128 KiB, 256 KiB, 512 KiB); that length would
be the application region of a part with 1024 KiB of flash`), so a part the
fork added later shows up as the entry to add rather than as an unexplained
figure. The length also has to follow the part the command itself named: that
same line gives the flash the part has, so a length that is right for some
other part in the table and wrong for this one fails too (`the read-back
covers 16384 bytes, but the command reported a part with 64 KiB of flash,
whose application region is 49152 bytes`). Every run also reads the table out
of the fork's checkout at the
commit under test and records the comparison, so the copy is kept in step
without waiting for such a board to reach the bench: see the `dfu_part_table`
record in guide/builds-and-firmware.md, "DFU images".

All three arrived on the fork's main after the earlier releases, so the case
expects them: the range line in `10c3381`
([ubertooth#257](https://github.com/SqueakyFloors/ubertooth/pull/257)), the
vector checksum in `31228b0`
([ubertooth#1037](https://github.com/SqueakyFloors/ubertooth/pull/1037)) and
the image check record in `d91bb06`
([ubertooth#1048](https://github.com/SqueakyFloors/ubertooth/pull/1048)), the
last two for the bootloader checks of
[ubertooth#242](https://github.com/SqueakyFloors/ubertooth/issues/242).
Against a build between `31228b0` and `d91bb06` the case still passes: the
image check block arrived in `d91bb06` on both sides at once, so an image
from that window carries none and there is no record to fill in or to check.
Against one between `10c3381` and `31228b0` it fails on the vector checksum
alone (`vector checksum at 0x0401c is 0x00000000, expected 0x...`): the dump
equals the file, and that field is checked against what the writer should
have worked out, so equalling the file is not enough.

**Do not run this case against a build older than `10c3381` on an Ubertooth
One.** Such a `-u` reads a fixed 0x4000-0x3ffff whatever the part has, and on
the 128 KiB part the board is built from that asks for flash above the end of
it, which faults the bootloader until the board is power-cycled
([ubertooth#233](https://github.com/SqueakyFloors/ubertooth/issues/233), which
`10c3381` closed). The
case reports it as a failed `-u` and a cleanup that cannot reach the board,
not as a comparison it could make.

**Hardware and arguments.** One `Ubertooth`, selected by full serial, with
every other registered board as guards. `coverage:`. Needs the firmware build
as the test image, which must report the resolved Ubertooth revision; a run
without one is refused rather than leaving cases out, so it comes from
`--build --with-firmware` or a `test build` artifact on the command line, and
from the `with_firmware` option of a controller or GUI run (which the GUI
holds ticked for a suite that names this test). Writes
firmware and establishes it, so preflight checks only the board's serial and
the test must be the last one on that board.

**Commands.** `dfuimage.py` makes the images, strips and signs them, and
reads the flash back; `ubertooth-dfu` is the tool under test:

```text
ubertooth-dfu -U <serial> -d <test image> -r
ubertooth-dfu -U <serial> -u <case dir>/readback.bin -r
ubertooth-dfu -s <case dir>/image.bin                    # and a .dfu, refused:
                                                         # signing needs no board
ubertooth-dfu -U <serial> -d <case dir>/<variant>.dfu -r # each bad image, refused
dfuimage.py make <variant> <test image> <case dir>/<variant>.dfu
dfuimage.py copy <test image> <case dir>/signed.bin|signed.dfu
dfuimage.py same <case dir>/signed.dfu <test image>
dfuimage.py absent <case dir>/signed.dfu
dfuimage.py strip <test image> <case dir>/image.bin|readonly.bin [--read-only]
dfuimage.py check-readback <case dir>/readback.bin <test image> --reported <read-back range>
dfuimage.py check-suffix <case dir>/image.bin <case dir>/image.dfu <test image>
ubertooth-util -U <serial> -v                            # the revision written
dfuimage.py answers --serial <serial> --revision <expected revision>
dfuimage.py after-reject|restore --serial <serial> --image <test image> \
    --revision <expected revision> --readback <case dir>/<name>.bin \
    [--guard <serial>]
```

**Expected outcome.** Smoke runs 2 cases: `-u` reads back the image `-d`
wrote, and `-s` suffixes a binary identically to the build's `.dfu`.
Comprehensive runs 12, adding the bad-image refusals with a read-back after
each. Every case ends with the test image in flash, read back and verified,
and the board in its application firmware. After each case the cleanup reads
the flash back, rewrites the test image only if it differs, and checks the
revision, so a failed case leaves the board usable and the run goes on.

**Known limits.** A board left in the DFU bootloader has no serial; the
cleanup can return it only when exactly one DFU device is attached and every
other registered board answers in its application, and otherwise the run stops
for recovery by hand. A read-back can itself fail and leave the board in DFU
([ubertooth#208](https://github.com/SqueakyFloors/ubertooth/issues/208)), seen
about once in twenty: in a case step that fails the case, in the cleanup it
stops the run.

## Exclusive Ubertooth topology

**What it does.** Powers off every other board so one Ubertooth is alone on
the bus, then runs commands with no `-U` selector.

**Theory of operation.** With more than one board enumerated and no `-U`,
libubertooth refuses to choose and opens nothing, so these commands can only
succeed when isolation really left one board. That makes them a check on the
topology rather than on the tools: the no-selector serial read must report the
selected board's serial, the firmware query must answer, and
`ubertooth-btle -f` with no `-U` must keep running until its deadline. The
capture case is the strictest of the three, because an early exit for any
reason, including a second board appearing, gives a different exit status than
the expected timeout.

**Hardware and arguments.** One `Ubertooth` plus power control over every
other registered board on the host. Required: `platforms:` and `exclusive
Ubertooth: true`. `capture duration:` defaults to 5 s and may be at most 10
minutes, since every other board stays powered off for the whole capture.

**Commands.** No `-U` at all: that is the point. The capture is wrapped so a
long run's output does not have to be kept:

```text
ubertooth-util -s
ubertooth-util -v
bash -c 'timeout --foreground -k 2 -s INT <seconds> ubertooth-btle -f \
    | tail -n 20; exit ${PIPESTATUS[0]}'
```

The capture runs under `timeout`, which sends it SIGINT at the deadline, and
through `tail` so a long run's output does not have to be kept; the pipeline's
first exit status is the one the step is judged on.

**Expected outcome.** Three cases: the no-selector serial is the selected
board's, the no-selector firmware query reaches one board, and the
no-selector follow keeps running and is stopped at its deadline. The
surrounding run powers the other boards down, verifies exactly one expected
no-selector serial, and restores and reads back every original power state on
success, failure, or cancellation.

**Known limits.** It needs a switchable hub, so it is Linux-and-power-control
only. Extra Ubertooths fail closed rather than being ignored: one the registry
has on no power controller blocks the step, and one that turns up unregistered
during the run fails the isolation check. `testplans/exclusive.yaml` selects
`Ubertooth: each`, so on a bench with one unmapped board the step runs for
every board that can be left alone, is blocked for the rest, and the run
fails; with no board that can be left alone there is nothing to run and the
suite is refused at planning. The follow case proves the mode kept running,
not that it captured anything.

## MAC polling soak

**What it does.** Nothing yet: a catalog entry for a long-running MAC bucket
and query workflow against a known advertiser.

**Theory of operation.** The intent is to reproduce the shape of a
production-style MAC polling workload in the harness's own process: a
persistent `ubertooth-btle -n -U <serial>` capture, a
set of in-memory MAC buckets rotated on a timer, lookups on a second timer,
listener-silence detection, and the empty-bucket restart timing. Modeling it
in-process is deliberate: the harness will not start the original workload and
will not add a network listener. Every timing parameter is a plan argument, so
the workload's shape is declared rather than hidden.

**Hardware and arguments.** One `Ubertooth` and one `advertiser`
(nRF52840 DK), required `platforms:` and `exclusive Ubertooth: true`, and
fourteen required timing and bound arguments: `duration:`, `query every:`,
`log every:`, `bucket count:`, `bucket rotation:`, `listener silence
timeout:`, `empty buckets restart after:`, `restart suppression:`, `restart
timeout extension:`, `post-restart wait:`, `search timeout:`, `maximum
consecutive misses:`, and `maximum restarts:`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it. Its resource and power manifest already compiles, including every
required power-off action. On a host where another registered board is on no
power controller the step is blocked, as `Exclusive Ubertooth topology` is;
since `mac-polling.yaml` holds this one step and selects `Ubertooth: default`,
that leaves the suite nothing to run and planning refuses it, naming the
blocked step.

**Known limits.** What counts as a pass is not established beyond the bounds
the arguments name.

## Capture eero setup beacon

**What it does.** Nothing yet: a catalog entry for triggering and capturing an
eero setup beacon.

**Theory of operation.** Not established.

**Hardware and arguments.** One `Ubertooth` and a required `eero:` resource.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a device in setup mode as the transmitter, which is
not a registered fixture.

## Recover a BLE connection promiscuously

**What it does.** Nothing yet: a catalog entry for `ubertooth-btle -p` against
a live connection between two nRF52840 DKs that hops with Bluetooth channel
selection algorithm 1.

**Theory of operation.** `-p` joins a connection that is already up, with no
CONNECT_IND to read, so it has to work the connection out from the air: the
access address, then the CRC init value, then the connection interval from the
spacings between the connection's visits to the one channel it listens on, and
last the hop increment. Only algorithm 1 repeats those visits on a cycle it can
measure, which is why the fork's `ubertooth-btle` documents `-p` as recovering
algorithm 1 connections only.

Until now the bench had no algorithm 1 connection to point it at: every DK
link negotiated algorithm 2, which both ends support. The second DK image
(`firmware/nrf52840dk`, built with `chan-sel-1.conf` and `chan-sel-1.overlay`)
has a controller without algorithm 2, and a link uses algorithm 2 only when
both ends have it, so one DK on that image makes every connection it is part
of an algorithm 1 one. Its `status` carries the proof: `chan_sel_2` false, and
`chan_sel` `1` while the link is up. The peer on the default image reports
`chan_sel` `unknown`, which is honest — a controller that has algorithm 2 is
not told which one was agreed.

**Hardware and arguments.** One `Ubertooth`, a `central` and a `peripheral`
(nRF52840 DK). The central would run the algorithm 1 image, since it is the
side whose CONNECT_IND carries the ChSel bit, and the peripheral the default
one; `coverage:` and a `duration:`. The pair would run with `conn cycle off`,
because `-p` needs a connection that stays up, and the board's squelch has to
be raised from its default −120 dBm first: the access-address hunt is gated by
it, and at the default the hunt latches onto noise.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it. The outcome it is meant to check is all four of the lines `-p`
reports — access address, CRC init, hop interval, and hop increment — with the
hop interval equal to the interval the central asked for.

**Known limits.** The hardware it needs is on the bench, and the connection is
confirmed to be an algorithm 1 one. What is not yet shown is the outcome: in a
90-second run against such a connection, with the squelch at −60 dBm, `-p`
reported the access address and the CRC init value and went no further, and
over a hundred packets on that access address decoded with valid CRCs. Whether
a longer run, another squelch, or a change in the fork reaches the interval and
the increment is open
([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)), so
the case is not written to a result nobody has seen.

## Reset into invalid application

**What it does.** Writes an image the bootloader must refuse to start, cuts
the board's USB power and turns it back on, and requires the board to come up
as a DFU bootloader device rather than running the image. Then it writes the
run's own image back and checks the revision.

**Theory of operation.** On every power-up the bootloader decides whether
there is an application it can start. The first thing it asks is about the
two words at the start of the application region: the initial stack pointer
must be inside RAM and word-aligned, and the reset vector must be a Thumb
address above the bootloader (`firmware/bootloader/app_image.h`,
`app_image_startable()`). Erased flash fails both, which is what an
interrupted download leaves there. Before that check existed, such a board
faulted on every power-up and came back only when someone bridged its P4
entry pin at the bench; with it, the bootloader keeps running and the next
image can go on over USB. The test makes exactly that situation on purpose,
so the answer is knowable: the board must enumerate, and must enumerate as a
bootloader.

It is that check the image fails, not either of the two checksums the
bootloader goes on to ask for (the LPC vector checksum at offset 0x1c, and a
checksum over the whole region when the image carries an image check block).
`ubertooth-dfu` fills both of those in as it writes the first block, so they
say whether what was written reached the flash whole rather than anything
about the file; an image that fails the two words is refused before either is
read.

Two things make the image the right instrument. It is a valid Ubertooth DFU
file — correct suffix, firmware info block intact, only the two vector words
changed — so `ubertooth-dfu` has no reason to refuse it and the flash really
does end up holding it. And the cycle is a power cycle, not the reset `-r`
already does, because the decision under test is the one taken on a cold
boot. The board under test has no serial while it sits in its bootloader, so
it is identified by elimination, as the DFU images test does it: exactly one
serial-less Ubertooth device attached, and every guard board enumerated by
its own serial.

**Hardware and arguments.** One `Ubertooth` whose **bootloader came from this
tree**, with the startable check in it, and which has a `power:` mapping in
the registry on a `USB power controller` that can switch VBUS per port, plus
every other registered board as guards. The bootloader is the precondition
that matters most: `ubertooth-dfu` writes the application region only, so a
bootloader is installed over a programmer on the board's in-system
programming header. On a board carrying the factory bootloader this test does
not fail — it bricks the board, which then needs its P4 entry pin bridged,
because that bootloader jumps into the erased vectors it was given. So the
committed plan names the board the registry pairs with a `Debug programmer`,
which is also what gives that board a recovery path, and holding the board
holds the programmer with it. Read the bootloader before and after
(`ubertooth-util -U <serial> -B`) by hand: `--bootloader check` does not
cover this board, because preflight reads the bootloader of a board whose
firmware the run requires and this test establishes its own, so the read is
skipped for it
([#303](https://github.com/SqueakyFloors/ubertooth-test-harness/pull/303),
[#327](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/327)).
`--bootloader update` does not reach this board either — it installs over a
programmer for the same boards the read covers — so a board that needs a
bootloader gets one before this plan is run: from a run that does require its
firmware, or by hand over the programmer. Required:
`platforms:`, because the hub is reached through a serial-device path that
only Linux has, and
`USB power controller:`, the hub the board must be on. A plan pointed at the
wrong hub fails planning rather than switching whatever is on that hub's
registered port, and a bench that has not got the named hub, or whose board
has no `power:` mapping, blocks the step: the run it belonged to fails
instead of passing without the check. The port itself is only ever read from
the registry; what the planner resolved — the hub, its driver and identity,
and the port — goes into the run record and is passed back to the step, which
refuses if the registry now says anything else. So a registry edited after
the run was planned switches nothing, whether the board moved port or the
alias was pointed at another hub. The run
needs `--allow-destructive` and `--allow-power-changes` and the run's own
firmware image (`--build --with-firmware`, or a `test build` artifact), holds
the host lock throughout, and leases the board and its guards.
`--allow-destructive` is a person's decision: the runner service never passes
it on, so a dispatched run, or a click in the GUI, cannot start this test. It writes firmware and
establishes it, so preflight checks only the board's serial and the test must
be the last one on that board.

Its guard Ubertooths matter more here than in a test that writes nothing, and
they are read under the rules in "Guard Ubertooths" above: a guard that
answered none of `-v`, `-s`, or `-B` turns the run away by name before
any case, since nothing about that board could be compared after the write. A
guard carrying the alternate `btbr` image is not in that position — it answers
all three — and it is guarded by what it answers, not reflashed to make it
answer more.

**Commands.** `invalidapp.py` makes the image the bootloader must refuse to
start, switches the board's registered hub port, and puts the board back:

```text
invalidapp.py make --image <test image> --output <case dir>/unstartable.dfu
invalidapp.py port --hardware <hardware registry> --board <alias> \
    --controller <alias> --expect-port <port> --allow-power-changes
ubertooth-dfu  -U <serial> -d <case dir>/unstartable.dfu -r
invalidapp.py cycle --hardware <hardware registry> --board <alias> \
    --controller <alias> --expect-port <port> --allow-power-changes \
    --serial <serial> --guard <serial>
invalidapp.py recover --serial <serial> --guard <serial> --image <test image> \
    --revision <expected revision>
ubertooth-util -U <serial> -v
```

**Expected outcome.** One case. The write must reach the flash and must not
report a verified revision afterwards: the board stays in its bootloader, so
`ubertooth-dfu -d <image> -r` ends with an error and exit 1, and that is the
expected outcome here. The step accepts exit 0 as well, so that the forbidden
`Verified firmware revision` line is what decides: what that line catches is
a write that did not take, leaving the board still running the application it
had, which answers the revision check and looks like a successful flash. The cycle must then show, after the port comes back,
the board's serial absent and exactly one DFU bootloader device, with every
guard where it was. A board that comes back running the image, or that does
not come back at all, fails the case and the message says which. The
recovery step writes the run's image back — without a selector, since a board
in its bootloader has none — and waits for the application to answer with the
expected revision. It always writes, and does not compare the flash: the
unstartable image carries the same firmware info block as the image it was
made from, so a board that did start it answers with the very revision the
recovery is aiming for, and a comparison would need a read-back that differs
from the file by design (`ubertooth-dfu` fills in the checksums as it writes)
and that itself fails about once in twenty. The board coming back into its
application after the write is the proof that what is in the flash now is
startable. The cleanup runs whatever happened: it powers the port on if it is
off, then writes the image and reads the revision, so a failed case still
ends with a board on the test image. It writes even where the recovery step
already did, which is one write more than a passing run needs: the cleanup
cannot see what the steps did, and a board that started the unstartable image
answers with the same revision as the test image, so a verified write costs
less than being wrong about which it is.

**Known limits.** A pass says what the bootloader in that board's flash does,
and that bootloader is not what the run built: it cannot be written over USB,
so a hardware pass cannot be attributed to the commit the run was given. What
it does show is that the board the lab keeps for this comes up in its
bootloader after a cold boot with an image it must refuse.

It is destructive. If the bootloader does start the
unstartable image, the board faults, leaves USB, and needs its P4 entry pin
bridged by hand — the failure the test exists to catch is also the failure
nothing in the harness can recover from. So it lives in `testplans/power.yaml`
and is run by hand with someone at the bench, not in an unattended plan. A
run killed outright (SIGKILL) leaves whatever it was in the middle of: the
port off if it was between the switch-off and the restore, and the board in
its DFU bootloader with the unstartable image in flash if it was anywhere
between the write and the recovery. Both are put right by hand — switch the
port on (reference/power-control.md) and write an image back with
`ubertooth-dfu -d <image> -r` — because the next run cannot do it for you:
preflight reads the board's serial before any case runs, and a board in DFU
reports none. What the cleanup steps do cover is the case's own command being
stopped: the harness signals a command that outlives its step deadline, with
SIGTERM and then SIGKILL two seconds later, so the helper's own handlers
normally restore the port before it goes. The GUI lists the `power` plan like any other, with nothing to say it is
destructive, and a run dispatched from it is refused for want of
`--allow-destructive`: fail-closed, but it advertises a suite that cannot run
there. The one case covers erased vectors; the other unstartable images the
harness can build (a misaligned or out-of-RAM stack pointer, an ARM-mode
reset vector, an entry inside the bootloader) are checked on the host only,
in the unit tests, because each extra hardware case is another cold boot with
the same risk and the same bootloader check behind it. The recovery writes
without a selector, because a board in its bootloader has no serial and `-U`
takes no bus path, so if that board left the bus between the check and the
write, `ubertooth-dfu` writes whichever board a no-selector command would
reach. The harness cannot prevent that; it catches it where it can, by
requiring every guard board to still run what it ran before the write, and by
the run's own guard checks afterwards. A revision is all there is to compare,
so a guard that was already running the image that was written cannot be told
from one that was not written at all — the ordinary case on a bench whose
boards all carry the run's build; there the run still fails, on the board
under test not answering, but names that rather than the guard. And none of
it covers an Ubertooth nobody registered: a stranger in its own DFU
bootloader, attached while the board under test is off the bus, looks exactly
like the board. The run assumes the registry describes the bench, as leases
and isolation do.

The cold boot is read from the bus, not measured at the board: what the case
requires is that the board leaves USB enumeration and stays gone for a second
before the port comes back. The committed plan's hub cannot measure VBUS at
all, and the test does not read it where a hub can; a hub that reported a
port off without cutting it would make the board's disappearance the only
evidence, which is why the disappearance is what is required rather than the
port's reported state. The hardware run is
done: one pass on the paired board, with the bootloader read by hand either
side and the two `btbr` guards recording `channel`, `squelch` and `crc` as
unanswered
([#141](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/141#issuecomment-6638139774)).

## Receive-only firmware

**What it does.** Nothing yet: a catalog entry for exercising a receive-only
firmware build.

**Theory of operation.** Not established.

**Hardware and arguments.** One `Ubertooth`.

**Commands.** None: no implementation yet.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a firmware image other than the usual one, so it would
write firmware and have to be the last test on its board.

## Out of scope: Uberducky and E-GO devices

Device-level testing of `ubertooth-ducky` and `ubertooth-ego` is out of scope
([#89](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/89)), by
the maintainer's scope decision. There is no catalog test and no backlog entry
for either, and neither is waiting on a fixture: the harness will not drive an
Uberducky HID peer or a registered E-GO skateboard. So `ubertooth-ducky -q`
and `-b`, which transmit, `ubertooth-ego -i` and `-j`, which transmit, and
`ubertooth-ego -f`, which needs a skateboard to follow, all stay untested.

What the harness does keep for both tools needs no peer and no transmission:

- `Host CLI surface` runs `ubertooth-ducky -h` and `ubertooth-ego -h` and
  requires exit 0 with the tool's own name in the output.
- `Argument validation` covers their refusals: `ducky -A36`, `-A40`, a
  malformed `-a` address, `ego -l0`, `-l49`, and a stray word on each, every
  one refused with exit 1 and no device change.
- `Raw receive` runs `ubertooth-ego -r -c 2440` with nothing in range, along
  with `-l 1`, `-l 48`, and `-a 630f9ffe` accepted and `-a zz` refused, so the
  receive path and its parsing are exercised without an E-GO present.

[`COVERAGE.md`](../COVERAGE.md) carries the per-option status for both tools.
