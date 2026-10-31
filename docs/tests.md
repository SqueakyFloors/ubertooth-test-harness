# What each test does

One section per test in the harness catalog
([`ubertooth_harness/catalog.py`](../ubertooth_harness/catalog.py)), in catalog
order. Each heading is the exact catalog name, which is also the name a plan
writes, so a test without a section here is easy to spot.

Each section says what the test does, how it produces a known answer, what
hardware and arguments it needs, what passes and fails, and what it cannot
tell you. Plan membership is deliberately absent: any test can go in any plan,
so which plans use a test belongs in
[`testplans/`](../testplans), not here.

Companions: [`COVERAGE.md`](../COVERAGE.md) is the same ground organized by CLI
option, and tells you which option still has no test;
[`hardware.md`](hardware.md) describes the devices these tests bind, and the
firmware they run; [`DESIGN.md`](../DESIGN.md) describes the harness itself.

## How to read a section

**What it does** is the one- or two-sentence summary.

**Theory of operation** is why the check is a real signal rather than a
tautology: what makes the answer knowable in advance.

**Hardware and arguments** lists the roles the test binds and the arguments
worth setting. It leaves out the three arguments every test takes (below).

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
`Exclusive Ubertooth topology`, and `MAC polling soak` must state their
platforms.

**RF placement.** Every test with a hardware role also accepts `rf:`, one
requirement per role, which says what the role's placement on the bench must
be: `antenna`, `min_attenuation_db`, and `max_attenuation_db`
([`rf.py`](../ubertooth_harness/rf.py)). The placement itself is recorded by
hand in the registry and never measured, so this is how a plan asks for a
weak or distant link, or keeps a test off an attenuated board. A test whose
cases need an ordinary link declares that as its own default, so a plan does
not have to; the plan's `rf:` overrides the default role by role. The sections
below name the default where a test has one.

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

**Guard Ubertooths.** A guarded test binds every other registered Ubertooth on
the host as a `guard Ubertooth N` resource and reads each one before the test
and after every case, with queries only and one command per read:
`ubertooth-util -v`, `-V`, `-C`, `-a`, `-z`, `-d`, then `ubertooth-btle -k`
(`-V` ends the command, so it could not share one with the others anyway).
Every read must match the baseline taken before the
first case; one that differs fails the test. This is what catches a regression
that reaches a board the command did not select. The `ubertooth-btle -k` read
is left out unless that board's firmware API is at least the host tools' API,
because `ubertooth-btle` stops older firmware as part of its own API check.

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

**Theory of operation.** `ubertooth_print_host_version()` prints the
libubertooth and libbtbb versions with their git identities as the first line
of every tool's `-V` answer. The check is anchored to that label, so firmware
lines printed under it cannot stand in for it. The same line is what preflight
compares against the resolved host sources, which is how a run proves the
tools under test are the ones the manifest names.

**Hardware and arguments.** No board role. `hosts:` only. The command runs
with a 20 s timeout.

**Expected outcome.** One case. The line
`Host tools version: libubertooth <v> (<id>), libbtbb <v> (<id>)` must appear.
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

**Expected outcome.** One case: exit 0. No output pattern is required.

**Known limits.** It proves almost nothing beyond "the binary runs".
`Host CLI surface` is the same idea applied to every tool, with the tool name
required in the output.

## Host CLI surface

**What it does.** Runs `-h` on every always-built host tool and `-V` on the
four tools that report the host version.

**Theory of operation.** Each tool's usage text names the tool, so requiring
the tool's own name in the output distinguishes a working parser from a
binary that printed something else. `-h` exits 0 on every tool in this fork,
which was not always so: `ubertooth-btle` and then `ubertooth-dfu`,
`ubertooth-scan`, and `ubertooth-follow` used to exit 1. Pinning exit 0 per
tool keeps that fixed.

**Hardware and arguments.** No board role. `hosts:` only. The two BlueZ-only
tools declare that they need BlueZ, so on a host without it (every macOS host)
those cases are left out before the run as not applicable, rather than failing
or being skipped at run time.

**Expected outcome.** 17 cases on a Linux host, 15 on macOS. `-h` on
`ubertooth-util`, `-debug`, `-specan`, `-rx`, `-tx`, `-dump`, `-btle`, `-dfu`,
`-ego`, `-afh`, and `-ducky`, each exit 0 with its own name in the output;
`-h` on `ubertooth-scan` and `-follow` where BlueZ is present; and the host
version line from `ubertooth-util -V`, `-rx -V`, `-tx -V`, and `-afh -V`. A
case failure does not stop the others.

**Known limits.** It checks that help works and that the version line is
present, not what any option does.

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

**Expected outcome.** One case: the output is the expected number on a line of
its own.

**Known limits.** It depends on the registry being right. An unregistered
board attached to the host fails this test, correctly, but the message says
only that the count differed.

## Firmware version

**What it does.** Nothing yet: a catalog entry for reading the firmware
revision from one board.

**Theory of operation.** Not established. `Ubertooth identity` already reads
and checks the firmware revision (`ubertooth-util -v`), and preflight reads it
for every selected board on every run.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Covered in practice by `Ubertooth identity` and preflight.

## Firmware compile info

**What it does.** Nothing yet: a catalog entry for reading the firmware's
compile identity from one board.

**Theory of operation.** Not established. `Ubertooth identity` checks the
whole `ubertooth-util -V` answer, the compile-info pattern among it, and the
guard readback uses the same `Firmware build:` line, whose build timestamp
also catches a reflash of the same revision.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Covered in practice by `Ubertooth identity`.

## Device serial

**What it does.** Nothing yet: a catalog entry for reading the
microcontroller serial from one board.

**Theory of operation.** Not established. `Ubertooth identity` checks
`ubertooth-util -s` against the registered serial, and every transition case's
cleanup ends with the same read, which is how a case proves it talked to the
board it selected.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Covered in practice by `Ubertooth identity`.

## Part ID

**What it does.** Nothing yet: a catalog entry for reading the
microcontroller part ID from one board.

**Theory of operation.** Not established. `Ubertooth identity` checks
`ubertooth-util -p` against the part ID pattern.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Covered in practice by `Ubertooth identity`.

## Board ID

**What it does.** Nothing yet: a catalog entry for reading the Ubertooth board
ID from one board.

**Theory of operation.** Not established. `Ubertooth identity` checks
`ubertooth-util -b` against the board ID pattern.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Covered in practice by `Ubertooth identity`.

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
it prints the host tools' version and the board's firmware version and build,
then ends the command, so its case asks for the whole of that answer on its
own.

**Hardware and arguments.** One `Ubertooth`, selected by full serial. Not
guarded (every command is read-only and selects by serial).

**Expected outcome.** Six cases: firmware version, the whole `-V` answer (its
four lines in the fork's order: host version, libbtbb path, firmware version,
firmware build), device serial, part ID, board ID, and the four-query
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
answer (its four lines, in order) and no serial line. The fork sets the flags
while it reads the command line and acts only once the whole line has parsed,
so both orders must behave alike. Asking for `-V` beside the others would
hide every query behind it.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:`.

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

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded, which is what catches the two `-U all` cases if a regression let them
reach every attached board. `coverage:`.

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

**Expected outcome.** With two boards, smoke runs 3 cases and comprehensive 9;
the index case's step count grows with the number of boards. Smoke: `-U all
-s` lists every registered board and only those, `-s` without `-U` does the
same, and `-N` plus one `-U<index> -s` per board agree with the listing.
Comprehensive adds the four identity queries listed per board in index
order, both with `-U all` and with no selector; `-V` reporting every board
itself, also both ways, as the host tools' two lines (the version and the
libbtbb path) followed by each board's firmware version and build under its
index; `-C` without `-U` refusing to
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

Both boards are reset first, so neither can report a stale result, and both
are set to 2410 MHz, which is neither the boot default nor the value the idle
reset leaves. That way the channel reads after the test show the idle reset
ran rather than just showing what `-r` left behind. The responder's LEDs are
read while it repeats, before the sender starts and again after the test, so a
failure tells a wedged repeater from a wedged sender.

**Hardware and arguments.** Two Ubertooths as `sender` and `responder`, both
required as explicit aliases with full 32-hex-digit serials, and `host:` must
name one host rather than `each`. `allow RF transmission: true` is required:
both boards transmit, the sender at every PA level up to 0 dBm. `maximum PA
level:` (0-7, default 7) is the highest level the plan accepts for a pass.
Guarded.

**Expected outcome.** One case of 35 steps. It passes when the sender's `-m`
reports four result lines with request and reply PA levels at or below
`maximum PA level` and copy numbers 0-15; the repeater answers USB with its RX
LED on both before and after the test and keeps no result; the sender is idle
at 2441 MHz once the test ends by itself; `-S` returns the repeater to idle;
and every setting of both boards is restored. A reply that differs from the
request fails with the byte and the field it belongs to named.

**Known limits.** The boards sit centimeters apart on a bench, so a working
link gets through at a low PA level and a pass at level 0 says little about
range. What `maximum PA level` buys is a tighter bound than "it worked at full
power". An occasional `mismatch on byte 0` is a known fork behavior
([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)). The
test needs a second board, so it cannot run on a single-board host.

## BLE monitor

**What it does.** Nothing yet: a catalog entry for a bounded BLE monitoring
run on one board.

**Theory of operation.** Not established. The known advertiser the useful
version of this would need now exists
([#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86)) and
is used by `Capture known BLE advertiser`.

**Hardware and arguments.** One `Ubertooth` and a required `duration:`.

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
shape or a malformed signal-and-noise pair shows up. Where the command fixes a
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

**Expected outcome.** Smoke runs 3 cases: a survey (`-z -t`) that ends by
itself and lists its results, a `-r` PCAPNG capture, and the AFH no-packets
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
modulation actually took effect, including which of `-c` and `-l` won.

The stale-packet case uses the fact that `ubertooth-btle -n` and
`ubertooth-dump` read the board over different paths, so the two meet only at
a mode change. Alternating them and requiring that no capture holds anything
from the mode before it is a direct check for leftover buffered packets.

**Hardware and arguments.** One `Ubertooth`, selected by full serial.
Guarded. `coverage:` and a required `duration:`. The Ubertooth defaults to
needing an ordinary link.

**Expected outcome.** Smoke runs 3 cases: full records to a file, the `-b`
bitstream to a file, and an E-GO receive run on 2440 MHz. Comprehensive runs
11, adding `-l` LE modulation verified through MDMCTRL, `-l -c` and `-c -l`
where the last option wins, E-GO capture lengths 1 and 48, an E-GO access
code, the refusal of a bad access code, and the stale-packet case, which
alternates BLE and BR/EDR five times and requires zero packets from the
previous mode in every dump and every capture.

**Known limits.** No peer: the E-GO cases print nothing with no skateboard in
range, so they check that the tool runs and reports nothing malformed. The
dump cases check record structure and rate, not that the symbols mean
anything.

## Raw receive replay

**What it does.** Decodes a synthetic capture file with `ubertooth-rx -i`,
with no Ubertooth and no USB at all.

**Theory of operation.** This is the one receive test with a known answer for
every packet, because the harness writes the input. The fixture
([`rxreplay.py`](../ubertooth_harness/rxreplay.py)) carries two LAPs, chosen
errors, chosen channels, chosen host times, and decoy records, so the expected
output is computed rather than pattern-matched: which packet lines appear, in
what order, with which systime, which ones a filter drops, and how many
packets reach a capture file. That makes it possible to check decode behavior
that no live run can pin down, and to run the whole thing in CI.

**Hardware and arguments.** No board role. `hosts:` and `coverage:`.

**Expected outcome.** Smoke runs 6 cases: decode every LAP in the fixture,
derive the UAP with `-l`, replay into PCAPNG and into PCAP (link type 255, the
fixture's packet count), a `-d` file that cannot be opened (exit 1, one error
line, no usage text, nothing created or changed anywhere in the directory),
and `-s` decoding the same as without it, with `-h` listing `-s` as the
default. Comprehensive runs 19, adding the survey listing of both piconets,
a LAP filter, a correct and a wrong `-u`, `-e 0` dropping the packet with an
error, a `-d` dump compared byte for byte with the fixture, a filtered PCAP,
an empty file, a file whose last record is cut short (exit 1 after the whole
records), a missing file, the recorded systime on every packet line, a
directory passed as `-i` (exit 1 with the read error), and `-u` without `-l`
refused before any record is decoded.

**Known limits.** The fixture is synthetic, never an over-the-air capture, so
this covers the decode path and not the receive path. There is no LE
equivalent: no tool calls libbtbb's LE file replay.

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

**Expected outcome.** On a Linux host, smoke runs 45 cases and comprehensive
65; a macOS host runs 12 fewer in smoke, since the BlueZ-only cases are not
applicable. Every file option of `btle`, `follow`, `rx`, `dump`, `specan`, and
`dfu` is refused three ways: with a stray word, with `-h` (which exits 0 and
must still create nothing), and with a bad option value later on the line.
Beyond that: the extra-capture-file note goes to stderr, so it cannot land in
a capture written to stdout; a repeated file option is refused with
`error: option '-r' given more than once` before the stray word and before any
board lookup; and `scan` and `follow` refuse a stray word with no usage text
after the message.

**Known limits.** Forks from before the no-devices switch ignore it, so the
first step of every case reads the built library's bytes and the case skips
when the name is absent. That is a run-time skip because only the built
library can tell. The test says nothing about what the tools do once a command
line is accepted.

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
build artifact leaves it out as not applicable.

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
a run without `--build --with-python` leaves them out as not applicable; they
are additionally skipped at run time when the interpreter cannot import numpy,
qtpy, and a Qt binding, which only running it can tell.

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

**Expected outcome.** Smoke runs 1 case: a DLT 256 PCAP stream on the FIFO
until SIGTERM, with no process left behind, on the default advertising channel
37. Comprehensive runs 8, adding each of channels 37, 38, and 39 explicitly, a
serial no board has (the script must end non-zero with no packet records),
`--channel=40` (exit 1 before `ubertooth-btle` runs), a capture filter that
must be accepted with a warning that it is not applied, and a capture ended by
the reader closing the FIFO, which must exit 0 by itself.

**Known limits.** No peer, so the stream may carry no packet records at all;
the checks are about the header, the stream, and the exit. Nothing here runs
real Wireshark or tshark; harness issue
[#148](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/148)
asks for one tshark case so a real dissector reads what the plugin sends.

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

**Expected outcome.** Smoke runs 2 cases: both DKs confirmed on the pinned
firmware at their registered addresses, and `ubertooth-btle -f -r` holding the
pair's CONNECT_IND and long packets. Comprehensive runs 4, adding the same
into PCAP and one case that pins a known libbtbb defect. A capture case passes
when the CONNECT_IND carries the parameters asked for, at least five packets
appear on the connection's access address with at least one decoding with a
correct CRC, at least one packet is longer than 27 bytes, and the central
reports that it connected, wrote, and had its update accepted at the interval
it was told to ask for.

**Known limits.** The packet counts are floors a working follow clears, not
expected counts: of 18 packets in one followed connection measured on the
bench, two decoded with a correct CRC, one of them a full 251 bytes. Catching
the CONNECT_IND is not yet reliable
([#149](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/149)),
so the capture cases pass or fail partly at random. A capture window spanning
several reconnections has been seen to die partway through
([ubertooth#505](https://github.com/SqueakyFloors/ubertooth/issues/505)),
which is why the duration is bounded; whether the end of the followed
connection triggers it is not established. The PCAPNG connection-information
option libbtbb writes for a CONNECT_IND is copied 6 bytes early
([libbtbb#21](https://github.com/SqueakyFloors/libbtbb/issues/21)); one
comprehensive case pins that, so it fails when libbtbb is fixed and the case
can become a real check.

## CLI usage smoke

**What it does.** Nothing yet: a catalog entry for exercising host tool usage
paths.

**Theory of operation.** Not established. `Host CLI surface` covers `-h` and
`-V` on every always-built tool, and `Help smoke` covers the single-tool case.

**Hardware and arguments.** No board role. `hosts:` only.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Superseded in practice by `Host CLI surface`.

## BLE soak

**What it does.** Nothing yet: a catalog entry for a long BLE monitoring run
with periodic sampling.

**Theory of operation.** Not established. The sampling loop a soak needs (read
something every `sample every` and judge the series) is not implemented for
any of the three soak entries.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Spectrum soak

**What it does.** Nothing yet: a catalog entry for a long spectrum capture
with periodic sampling.

**Theory of operation.** Not established; see `BLE soak`.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Classic Bluetooth receive soak

**What it does.** Nothing yet: a catalog entry for a long BR/EDR receive run
with periodic sampling.

**Theory of operation.** Not established; see `BLE soak`.

**Hardware and arguments.** One `Ubertooth`, a required `duration:`, and a
required `sample every:`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** What a soak should fail on is not written down anywhere yet.

## Flash reliability

**What it does.** Writes firmware to a board many times in a row, alternating
two images, and verifies every write.

**Theory of operation.** Alternating the images is what makes each write
verifiable: every flash changes the expected revision, so reading
`ubertooth-util -v` afterward distinguishes a successful write from a board
that simply still holds what it had. Both images are pinned by SHA-256, the
test build against the run manifest and the known-good image against the
plan's own hash, checked before the run and again before each write, so a
corrupted or swapped file cannot be mistaken for a flash failure. Every other
registered board on the host is read before the run and after every flash, so
a write that reached the wrong board is caught rather than inferred.

**Hardware and arguments.** One `Ubertooth`, selected by full serial, plus
every other registered board. Required: `platforms:`, `flashes:` (the total
number of writes), `image sequence:` (at least two distinct image labels),
`KGV sha256:`, and `KGV revision:`. The images are supplied as artifacts named
after the labels. `alternate Ubertooths: true` rotates the writes across every
registered Ubertooth on the host, in alias order; then `flashes` must divide
evenly by the board count, `hosts:` must name its hosts rather than `each`,
and `Ubertooth: each` is refused. The test establishes the firmware it
verifies, so preflight checks only the board's serial and not its revision,
and the test must be the last one on that board in the suite.

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

**Hardware and arguments.** One `Ubertooth`, selected by full serial, with
every other registered board as guards. `coverage:`. Needs the firmware build
as the test image, which must report the resolved Ubertooth revision. Writes
firmware and establishes it, so preflight checks only the board's serial and
the test must be the last one on that board.

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

**Expected outcome.** Three cases: the no-selector serial is the selected
board's, the no-selector firmware query reaches one board, and the
no-selector follow keeps running and is stopped at its deadline. The
surrounding run powers the other boards down, verifies exactly one expected
no-selector serial, and restores and reads back every original power state on
success, failure, or cancellation.

**Known limits.** It needs a switchable hub, so it is Linux-and-power-control
only. Unknown or unpowerable extra Ubertooths fail closed rather than being
ignored. The follow case proves the mode kept running, not that it captured
anything.

## MAC polling soak

**What it does.** Nothing yet: a catalog entry for a long-running MAC bucket
and query workflow against a known advertiser.

**Theory of operation.** The intent is to reproduce the shape of a
production-style MAC polling workload in the harness's own process: a
persistent `ubertooth-btle -f` capture with no `-U` on an isolated board, a
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

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it. Its resource and power manifest already compiles, including every
required power-off action.

**Known limits.** What counts as a pass is not established beyond the bounds
the arguments name.

## Capture eero setup beacon

**What it does.** Nothing yet: a catalog entry for triggering and capturing an
eero setup beacon.

**Theory of operation.** Not established.

**Hardware and arguments.** One `Ubertooth` and a required `eero:` resource.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a device in setup mode as the transmitter, which is
not a registered fixture.

## Recover AFH map

**What it does.** Nothing yet: a catalog entry for recovering a Classic
Bluetooth AFH map from a known piconet.

**Theory of operation.** Not established beyond the tool's own behavior.
`Classic Bluetooth receive` covers the no-piconet path of `ubertooth-afh`,
where the expected answer is that it says so and exits 2.

**Hardware and arguments.** One `Ubertooth` and a required `piconet:`
resource.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a Classic piconet as a fixture
([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).

## Follow Classic Bluetooth piconet

**What it does.** Nothing yet: a catalog entry for following a registered
Classic Bluetooth piconet.

**Theory of operation.** Not established in this revision of the catalog.

**Hardware and arguments.** One `Ubertooth` and a required `piconet:`
resource.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a Classic piconet as a fixture
([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).

## Bluetooth inquiry scan

**What it does.** Nothing yet: a catalog entry for running inquiry with a host
Bluetooth adapter alongside an Ubertooth.

**Theory of operation.** Not established.

**Hardware and arguments.** One `Ubertooth` and a required `Bluetooth
adapter:` resource. Would be Linux-only: the tools involved are built with
BlueZ.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a host Bluetooth adapter as a fixture
([#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)).

## Reset into invalid application

**What it does.** Nothing yet: a catalog entry for verifying that a board with
an invalid application firmware can be recovered.

**Theory of operation.** Not established. The recovery path it would need now
exists, since the lab hub can switch board power; the test itself is tracked
as [#141](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/141).

**Hardware and arguments.** One `Ubertooth` and a required `USB power
controller:` resource.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** This is a destructive test by nature: it deliberately leaves
a board outside its application firmware, so it needs both power control and
an agreed recovery path before it can run.

## Receive-only firmware

**What it does.** Nothing yet: a catalog entry for exercising a receive-only
firmware build.

**Theory of operation.** Not established.

**Hardware and arguments.** One `Ubertooth`.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs a firmware image other than the usual one, so it would
write firmware and have to be the last test on its board.

## Uberducky HID

**What it does.** Nothing yet: a catalog entry for exercising an Uberducky HID
fixture.

**Theory of operation.** Not established.

**Hardware and arguments.** A required `Uberducky:` resource; no Ubertooth
role.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs an Uberducky fixture
([#89](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/89)), and
`ubertooth-ducky` transmits, so a plan would have to allow RF.

## E-GO skateboard capture

**What it does.** Nothing yet: a catalog entry for capturing a registered E-GO
skateboard.

**Theory of operation.** Not established. `Raw receive` covers
`ubertooth-ego -r` with nothing in range, where the expected answer is that it
runs and prints nothing malformed.

**Hardware and arguments.** A required `skateboard:` resource; no Ubertooth
role.

**Expected outcome.** No implementation; only a `runnable: false` plan may
name it.

**Known limits.** Needs an E-GO skateboard as a fixture
([#89](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/89)).
