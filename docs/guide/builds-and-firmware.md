# Builds, firmware, and flashing

Building the resolved commits, which libbtbb the tools load, flashing the
boards, the bootloader modes, preflight identity, flash reliability, and DFU
images.

## Building the resolved revision

By default a run tests the Ubertooth tools already installed, and preflight
refuses to start when they do not match the requested commit. With `--build`
the run builds the exact commits it tests instead (a `--ubertooth-ref` or
`--libbtbb-ref` its checkout does not have yet is fetched from that
checkout's remote first, as described under [Target runner
service](services.md#target-runner-service)):

```sh
python -m ubertooth_harness run-suite --plan testplans/fast.yaml --suite fast \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref feature/my-change \
  --libbtbb-source /path/to/libbtbb \
  --libbtbb-ref d356e9da7fb03969371aa14d603a8aad2f0ef198 \
  --build [--no-flash-build] [--with-firmware] [--with-btbr] \
  [--with-python[=PYTHON]] [--bootloader record|check|update] \
  --output runs
```

A `--build` run also flashes its boards with the firmware it built before
preflight, unless `--no-flash-build`; see [Flashing the boards with the run's
build](builds-and-firmware.md#flashing-the-boards-with-the-runs-build). Every run also reads which
bootloader is underneath that firmware, and `--bootloader` says what to do
about an older one; see [The boards'
bootloaders](#the-boards-bootloaders).

The examples here and the default in `scripts/ci.sh` use one commit of the
[`SqueakyFloors/libbtbb`](https://github.com/SqueakyFloors/libbtbb) fork: the
commit the Ubertooth fork pins for its own builds, so a run builds the libbtbb
that fork is built and tested against, not whatever upstream's `master` branch
holds that day. The harness keeps its own copy of that pin (it never calls the
fork's installer) and `tests/test_libbtbb_pin.py` keeps the copies in
`scripts/ci.sh` and this file in step; bump both when the fork bumps its pin.
To notice a bump, `run-suite` and `build` read `LIBBTBB_REF` from the fork's
`install.sh` at the resolved Ubertooth commit and record the result as a
`libbtbb_pin` record in run.jsonl and a "libbtbb pin" row in report.md, so a
run started from the GUI shows it too: `passed`, `warning` (naming both
commits when `--libbtbb-ref` differs), `unchecked` (the check could not run,
for example install.sh unreadable at that commit or a pin built from a shell
variable; the reason is given), or `skipped` (an older fork commit without
the pin). Anything but `passed` is also printed on stderr. The CI `build` job
runs the check once against the fork's `main` (`scripts/ci.sh libbtbb-pin`
runs only that step) and shows the warning on the workflow run; its `build`
and `run-suite` steps then record it without printing it again
(`UBERTOOTH_HARNESS_LIBBTBB_PIN_SHOWN`). Nothing fails, since another libbtbb
can be deliberate; what the run's tools actually load is a separate check that
does fail (see [Which libbtbb the tools
load](#which-libbtbb-the-tools-load)).
Only libbtbb's C library is installed and linked (`-DENABLE_PYTHON=OFF` skips
its Python tools): the Ubertooth tools need nothing else from it.

`--libbtbb-ref fork-pin` builds that pin instead of naming a commit: the run
reads `LIBBTBB_REF` from `install.sh` at the Ubertooth commit it resolved and
builds exactly that libbtbb, so each commit is tested against the libbtbb it
asks for. The run prints the commit it took (`note: libbtbb ref 'fork-pin'
is ...`) and records it in the manifest as that commit, named `fork-pin`, so
report.md shows `` `fork-pin` at `<commit>` ``. It is refused, naming the
Ubertooth commit, the problem, and both ways to name a libbtbb ref instead
(the option, or a redeploy of the runner), when that commit has no
`install.sh` or sets no `LIBBTBB_REF` (an older fork commit), when the pin is
built from a shell variable, and when it is a name that can move rather than
a commit, since `fork-pin` is a promise of one exact commit. Every run
resolves it, build or not, because the libbtbb a run records is checked
against the tools' own version line. This is what a long-lived runner is
deployed with, so a pin bump on the fork reaches it without a redeploy;
`scripts/ci.sh` keeps naming the commit, since comparing it with the fork's
is the point of its check.

`--libbtbb-ref` can still name any ref, with one limit: the harness passes no
`-DCMAKE_POLICY_VERSION_MINIMUM`, so a libbtbb declaring
`cmake_minimum_required` below 3.5 — upstream's `master` branch declares 2.8
at the time of writing — fails to configure under CMake 4 and needs CMake 3.x.
The pinned commit declares a `3.5...3.31` range and configures under both.

The resolved commits are cloned into `<run>/build/src` (the source checkouts
are never modified), built out of tree, and installed into `<run>/build/prefix`.
Every test and preflight command then runs with that prefix first on `PATH`, so
the run tests what it built. Host tools use `-DREQUIRE_ALL=ON` on Linux (BlueZ
exists only there), with udev rules off and the Python tools off unless
`--with-python` is given. `--with-python` builds them (`-DENABLE_PYTHON=ON`)
for the named interpreter, `python3` on `PATH` by default, passed as
`-DPython3_EXECUTABLE` (a virtual environment's `python` is kept as given, so
its packages are used). The installed `ubertooth-specan-ui` and
`ubertooth-btbr` must name that interpreter on their first line, and the
manifest records its path and version. specan-ui itself needs numpy, qtpy, and
a Qt binding (PyQt5/6 or PySide2/6) in that interpreter. `--with-firmware`
also builds `bluetooth_rxtx.dfu` with the built `ubertooth-dfu`, the
application image every other test needs on its board, and hashes it into the
manifest as the artifact `test build` (a run that flashes its boards builds it
even without the option). `--with-btbr` adds `btbr.dfu`, the alternate image
`ubertooth-btbr` needs and nothing else runs, as the artifact `btbr build`,
and asks for the firmware build itself. It is its own option because a run can
carry one image without the other: `Classic btbr console` writes the alternate
image and puts the application image back from the same commit, so it needs
both, and it says so in the catalog, which is how a run that selected it gets
the alternate image without `--with-btbr` being given
([#279](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/279)).
A suite that never runs it builds the application image alone, so nothing
waits for an image it does not flash. The built `ubertooth-util -V` must report
both resolved revisions on its `Host tools version:` line, without a
modified-source `*`.

The manifest records the toolchain (CMake, C compiler, pkg-config, libusb,
BlueZ on Linux, the ARM compiler when building firmware, and the Python with
`--with-python`), the CMake flags,
every build command with its exit code, and hashes of every built tool,
library, and firmware image. Every host tool the catalog runs must be in the
prefix, so a missing one cannot fall through to an installed copy. On Linux
that includes `ubertooth-scan` and `ubertooth-follow` (BlueZ), so their cases
are always required in a Linux `--build` run, never left out as not
applicable. The build
commands are recorded in run.jsonl and appear under `=== BUILD ===` in the
report. Each step closes with an outcome of its own, from a
`build_step_finished` record (`--- <step> EXIT: <status> ---`, and
`: <reason>` where the step has something to say), which is not the status of
its last command: the host-version step reads the version line and not the
exit status, so `-V` exiting 1 because of an attached board this build does
not need leaves the step passed, with a reason saying so. A missing tool or
library stops the run first, naming what to install. Ctrl-C cancels the
build, and the whole build has a one-hour deadline on top of per-command
timeouts.

`python -m ubertooth_harness build` does the same build without running tests.

Install the build prerequisites and udev rules once per machine, for example
with the fork's [`install.sh`](https://github.com/SqueakyFloors/ubertooth/blob/main/install.sh). The harness never runs it or installs anything
system-wide.

### Flashing the boards with the run's build

Preflight refuses to test tools against firmware from another commit, so on a
shared bench a board flashed by someone else fails every run. A `--build` run
therefore flashes its boards with the `bluetooth_rxtx.dfu` it just built,
after the build and before the manifest and preflight ([#135](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/135)). It is the
default whenever the run builds; `--no-flash-build` keeps the boards'
firmware (preflight then checks it as before), and a run without `--build`
cannot flash. Flashing implies the firmware build, so it needs
`arm-none-eabi-gcc`; without it the run is refused before anything starts,
naming `--no-flash-build`.

- Which boards: every Ubertooth whose firmware revision preflight requires,
  in run order (`PreflightPolicy.boards_needing_firmware`). Guard-only boards
  are not flashed, because preflight only requires their firmware API to be
  the host tools' own, not the resolved revision; boards a firmware-writing
  test (`Flash
  reliability`, `DFU images`) writes itself are not flashed either. A plan
  that uses no Ubertooth (the `host` plan) flashes nothing and needs no ARM
  compiler.
- How: one board at a time, `ubertooth-util -U <serial> -v` first (a board
  that already reports the build's revision is not written), then
  `ubertooth-dfu -U <serial> -d <image> -r` (25 s), then `-v` again until it
  reports the build's revision (up to 3 reads, 5 s each, 1 s apart, while the
  board re-enumerates). The image's SHA-256 is checked again before each
  write. The whole stage has a deadline of 10 s plus 48 s per board, and a
  write only starts with its whole 25 s left: a write cut off part-way would
  leave the board in its bootloader, so a board that cannot be written in the
  time left is not written at all, and the stage says so.
- The first failure stops the run before preflight, with the reason (the
  `ubertooth-dfu` exit, a board that does not answer after the write, or the
  revision it reports instead), in `run_finished` and the GUI failure
  summary. Cancellation (Ctrl-C, SIGTERM, SIGHUP, the GUI's Cancel) lets a
  write in progress finish, stops after it, and ends the run as canceled,
  not failed; a fault that arrives with a cancellation is still a failure.
- Locks: the run takes the [host lock](leases.md#the-host-lock) with its board leases
  (host lock first), holds it while writing, and releases it once every board
  is verified, unless the run itself switches power or writes firmware later.
  The board leases are held for the whole run, as always.
- Record: `build_flash_started`, then per board `build_flash_board_started`
  and `build_flash_board_finished` (status `skipped` for a board already on
  the build's revision, `flashed`, `failed` or `canceled`; revision before
  and after, written or not), each command as `command_started`/`command_finished` with phase
  `flash`, and `build_flash_finished` (status `skipped` with the reason when
  the run builds but does not flash). report.md has a "Flash" row and an
  `=== FLASH ===` section in the transcript.

When preflight finds a board on other firmware, its reason names both
revisions and the fix, for example `Firmware version: UT_Linux0 runs
git-a8e65d9, the tools are git-7388776: flash it with this build (the default
when building)`; a run without `--build` says to build and flash it, and a
`--no-flash-build` run to leave that option out.

### Which libbtbb the tools load

The pin above is about what a run *builds*. A run that builds nothing tests
the installed tools, and those are linked against whatever libbtbb is
installed on that host, which may be another commit entirely: the fork's
configure step only looks for a symbol, so neither the build nor the link
stage can tell two libbtbb commits apart. A behavior-only libbtbb commit
therefore changes what a run measures while everything about the run still
reads as correct. One such drift cost a bench session chasing a false
reproduction of a fork regression ([#294](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/294)).

So mandatory preflight holds the host tools to one libbtbb. Every tool prints
it on its own version line, beside the tools' own version:
`Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)`. What libbtbb's
half carries is
`btbb_get_release()`: the release its own build names since `libbtbb#139`,
and the commit the installed library was built from before that. Either way
the string comes from a function in libbtbb, so it describes the library the
tools load and not one they were compiled against. The `Host version` check
reads that line and requires:

- the libbtbb the run resolved, when it resolved one (`--libbtbb-source` and
  `--libbtbb-ref`, with or without `--build`), by the release a build of that
  commit names, or by its full commit where that build stamps one. Which it
  is comes from the libbtbb commit itself, worked out the way its build works
  it out (`release.py`), so the two repositories may have moved over at
  different times;
- otherwise the `LIBBTBB_REF` the Ubertooth commit under test sets in its own
  `install.sh`, which is the only statement of which libbtbb that commit is
  meant to run against. No option is needed, so a `run-suite` started by hand
  is held to it too, with one limit: a commit is not a release name, and
  turning one into the other needs the libbtbb checkout. So where the
  Ubertooth commit under test reports releases, a run given no
  `--libbtbb-source` records the check as **not checked**, with the pin and
  `--libbtbb-source`/`--libbtbb-ref` named in the reason, rather than
  comparing a commit against a release name and failing a run that is
  perfectly able to measure everything else. A run from a deployed runner is
  unaffected: `scripts/deploy.sh` passes `--libbtbb-source` and
  `--libbtbb-ref fork-pin` to every run, build or not, so it resolves the
  commit and the check is made.

A mismatch fails preflight before any measurement, and the reason names the
revision the tools reported, the one wanted, where that came from, and how to
fix it. A library that cannot name what was wanted fails the same way: against
a commit, one built from a tarball reports a release such as `2020-12-R1` and
one built from a modified checkout adds a `*`, and neither says it is the
pinned commit. Those
two can only be put right by installing a libbtbb built from a clean checkout
of that commit, or by running with `--build`; naming a ref cannot help, since
what the check compares is then a commit. report.md carries a "libbtbb in
the tools" row with what was required and how it went: `(verified)`,
`**failed**` with the requirement, or `**not reached**` where the check
stopped on the tools' own Ubertooth revision before it read the libbtbb half.
The run record keeps the check, so `ubertooth-harness validate` can repeat it.

An Ubertooth commit from before the fork pinned a libbtbb claims none, so
there is nothing to check; the run records that and the report row says **not
checked** with the reason, rather than leaving a reader to guess whether the
library was verified. A commit whose pin cannot be compared with a revision, a
branch or tag name or a pin built from a shell variable, refuses the run
instead, naming `--libbtbb-source` and `--libbtbb-ref` as the way to say which
library the installed tools hold.

What is compared is an abbreviation of a commit against another abbreviation
of one, because the library stamps whatever length its own build chose. Either
side may be the shorter, and the shorter one decides how many digits are
compared, which is what an abbreviation means: git shortens a hash only as far
as stays unique in that repository. So a requirement taken from the full
commit, which is what a run that resolved its own libbtbb has, is still
satisfied by the library's seven-digit stamp; what the full commit buys is
that a library reporting more digits must agree on all of them.
### The boards' bootloaders

A board's bootloader is the first 16 KiB of its flash. `ubertooth-dfu` runs
from there, so an ordinary firmware write cannot replace it: a board keeps the
bootloader it came with until somebody installs another one over a serial
in-system programming adapter or JTAG on its header. The Ubertooth fork reports
which one a board has on `ubertooth-util -B`, and every run reads it
([#298](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/298)).

`--bootloader` says what the run does about it:

- `record` (the default) reads `-B` after the firmware checks and records the
  version, the build string, and the CRC-32 of the whole region, for every
  board whose firmware preflight requires. Nothing is compared and nothing is
  written: installing a bootloader is physical and cannot be undone over USB,
  so a bench run and the release suite never do it by implication.
- `check` also builds the bootloader from the run's own Ubertooth source and
  fails the run when a board is on an older or different one. All three of the
  version, the build string, and the region hash must agree; the version
  alone is never enough, because two different 2.00 images are two different
  bootloaders.
- `update` installs that bootloader on a stale board whose registry pairs it
  with a [debug programmer](../hardware.md), writes it once, and then
  requires `-B` to report exactly what was written and the application to
  still report the run's revision. A stale board with no programmer fails, and
  the reason names the missing capability rather than trying USB.

`check` and `update` need `--build` and `arm-none-eabi-gcc`; `update` also
needs `lpc21isp` and a programmer with a `serial device` in the registry, and
takes the [host lock](leases.md#the-host-lock) with the board and its programmer for
the write. A failed write is a hard failure that keeps every command and its
output and is never retried: a second attempt over a half-written region would
make the board's state less knowable, not more.

**What "current" means.** Not the resolved commit's bootloader, but the newest
commit reachable from it that touched the sources the bootloader image is
built from. The build string the bootloader carries is the version the whole
repository reported at build time -- the release it came from, or a commit
hash on a commit from before the release names -- so pinning the artifact to
the resolved commit would
change its identity on every unrelated commit and demand a physical write to
every board on most days for no change in behavior. The pin, the paths it
watches, and the sources the bootloader's own makefile names all go in the run
manifest, and a makefile that starts building from a path outside those is
refused rather than quietly leaving the pin stale.

**Record:** `bootloader_build_started`/`bootloader_build_finished` for the
artifact, `bootloader_stage_started`/`bootloader_stage_finished` for the
stage, one `bootloader_identity` per board, and
`bootloader_update_started`/`bootloader_update_finished` around a write. Every
command is recorded with phase `bootloader` or `bootloader-update`. report.md
has a "Bootloader" section with a row per board, and the GUI shows each
board's bootloader in its board list.

## Source identity, manifest, and mandatory preflight

Every named-suite run requires `--ubertooth-source` and `--ubertooth-ref`. The
ref may name a branch, tag, or commit; the CLI resolves it without switching the
checkout and records the full immutable commit plus the repository and short
build revision. Dirty or untracked source content is rejected. Supply
`--libbtbb-source` and `--libbtbb-ref` together when validating an exact
companion build; `--libbtbb-ref fork-pin` takes that commit from the
Ubertooth commit's own `install.sh` (see [Building the resolved
revision](builds-and-firmware.md#building-the-resolved-revision)). When `--ubertooth-ref` is a
commit chosen by name elsewhere
(the target runner passes the commit the controller listed),
`--ubertooth-ref-name NAME` records that branch or tag name next to it in the
manifest (`ref_name`) and report.md; it is never resolved. Repeat `--artifact NAME=PATH` to hash build products into the
manifest.

Before preflight, the `manifest` record in run.jsonl captures the source revisions,
SHA-256 identities of the plan, hardware registry, supplied artifacts, and
immutable resource-bound invocations. The sequencer then generates mandatory
preflight from the selected resources; it does not depend on optional test-plan
entries:

- `Host version` once per selected host, matched to the version a build of
  the resolved commit reports (below), and to the libbtbb those tools are
  linked against (see [Which libbtbb the tools
  load](#which-libbtbb-the-tools-load));
- firmware version, the same version through `-V`, and registered serial once
  per selected
  Ubertooth, always selected by serial. The firmware version must be the
  application image (`bluetooth_rxtx`) built from the resolved commit, or the
  bare revision older firmware prints; a board left on another image, such as
  `btbr`, is a mismatch, and the reason names the image it found, because that
  board runs none of the other tools.

Checks are deduplicated by host/resource and emit normal check/command events.
Any mismatch prevents the first `test_started` event. This local slice verifies
artifacts that are already installed. Building and installing the resolved ref
on local/remote targets remains a later preparation phase; preflight cannot be
skipped to compensate for a missing installation.

### The version a build reports

Every version line names the release a build came from rather than the commit
it was built at
([ubertooth#1230](https://github.com/SqueakyFloors/ubertooth/issues/1230),
`ubertooth#1232`), and the whole of `-V` is those two lines since
[ubertooth#1233](https://github.com/SqueakyFloors/ubertooth/issues/1233) took
the image's build string out from under them:

```text
Host tools version: 2026-11-DEV2 (libbtbb 2020-12-R1+79)
Firmware version: bluetooth_rxtx 2026-11-DEV2 (API:1.58)
```

That answer is what the harness requires of the release under test, and the
only one it accepts: current-release validation keeps no requirement that
output carry a commit hash or a build stamp, and a run of a commit from
before the release names is refused in preflight by name
([#358](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/358)).

Preflight, the guard reads and the flash and bootloader checks all compare
what a board or an installed tool reports against what a build of the resolved
commit *would* report, so `ubertooth_harness/release.py` works that string out
the way the build works it out, from the resolved checkout: the nearest tag
named `YYYY-MM-Rn`, `-RCn` or `-DEVn`, then how many commits the build is past
it as `+N`, and `<latest release>+unknown` where no release tag is in reach --
a clone fetched without tags, or a shallow one -- because a count from the
start of history is not the distance from a release. Two files in the fork do
the same thing and agree with each other on purpose,
`host/cmake/set_release.cmake` and `firmware/version.mk`, and libbtbb's own
`cmake/set_release.cmake` does it for `btbb_get_release()`. A `*` for a
modified tree is never predicted: a run refuses a dirty source checkout and
builds from a clean clone of it, so a reported `*` is a mismatch to report.

**An older ref still reports a commit.** A commit from before that change
stamps `git-<hash>`, and its version *lines* are laid out differently too, so
the harness reads the style off the checkout at the commit it resolved rather
than assuming one: the file that decides the string either derives a release
name or formats a hash, and it says which in its own text. A file that does
neither, and a tree whose firmware names releases while that file has moved,
refuse the run rather than being guessed at -- a wrong guess would compare
every version in the run against the wrong string. A commit with no such file
at all is read as the hash style, which is what every such build stamped.

The version the run expects is in the manifest as each source's
`expected_revision`, and in report.md beside the ref and the commit, so a
reader of a run sees which it was.

## Flash reliability

`Flash reliability` is an ordinary plan test (see [`testplans/slow.yaml`](../../testplans/slow.yaml)):

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
  the run starts and again before each write, and refuses a sequence whose
  images are the same file: the images, not their versions, are what the run
  alternates. Two images may report one release, since a version names the
  release a build came from rather than the build, and then the revision read
  after a write says the right release is on the board rather than which
  build of it;
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
hosts (`hosts: [Host_Linux]`, not `each`), so the plan says which benches are
expected to have two boards; `validate-plan` reports this without a registry.
Planning refuses `Ubertooth: each` with `alternate Ubertooths` (it
would rotate once per board) and any board without a full 32-hex-digit serial.
A named host with only one registered Ubertooth has nothing to alternate
across, so the step is blocked there and the run fails, while the other hosts
and the rest of the suite still run (see
[Hardware a step is about](plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out)). Every registered Ubertooth on
the host is written, including boards the plan does not otherwise use. After every flash the target is read back and every
other board must still report the revision it had when last verified. The
report names the board for each flash and the per-board count, and
`flash_run_finished` carries `serials`, `verified_flashes_by_serial`, and
`final_revisions`: the last revision read from each board, `""` when a failed
write or unreadable output left it unknown, and absent for a board never
queried. `final_revision` stays the last verified revision of the selected
board.

Device selectors are serials: libubertooth treats an all-digit `-U` value as a
USB index and matches anything else case-insensitively against the start of a
serial. The flash test therefore refuses digit-only selectors and any selector
that is the beginning of another, because the board the longer one names
answers both. Register full 32-hex-digit serials.

Because the test writes the firmware it verifies, preflight checks only its
Ubertooth's serial, not its firmware revision. It must be the last test on that
Ubertooth in the suite, so no later test runs on firmware the run did not verify.

## DFU images

`DFU images` covers `ubertooth-dfu` beyond the write path: `-s` suffixing,
`-u` read-back, and refusal of bad images. It writes the selected board's
firmware, so, like `Flash reliability`, preflight checks only that board's
serial and the test must be the last on its board. Smoke runs in
[`testplans/slow.yaml`](../../testplans/slow.yaml); comprehensive is the whole
[`testplans/firmware.yaml`](../../testplans/firmware.yaml). It needs the test image,
`--artifact "test build=/path/to/bluetooth_rxtx.dfu"` or `--build
--with-firmware`, which must report the resolved Ubertooth revision:

```sh
python -m ubertooth_harness run-suite --plan testplans/firmware.yaml --suite firmware \
  --hardware hardware.yaml --host Host_Mac \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --artifact "test build=/path/to/bluetooth_rxtx.dfu" --build --output runs
```

The first case writes the test image with `-d -r` and reads it back; every
later case starts from it. Files the cases make (a binary, bad images,
read-backs) go in the run's `captures/` directory; `dfuimage.py` checks them
and prints summaries only. Every bad image is followed by a read-back: the
flash must still hold the test image. After each case the cleanup reads the
flash back, rewrites the test image only if it differs, and checks the
revision, so a failed case leaves the board on the test image and the run goes
on. A board left in the DFU bootloader (fork #198, before e8edd15) has no serial; the cleanup
returns it only when exactly one DFU device is attached and every other
registered board answers in its application, using `ubertooth-dfu -u <file> -r`
without `-U`, which writes nothing. Otherwise the run stops for recovery by
hand. A read-back can fail and leave the board in DFU (fork #208): in a case
step that fails the case and the cleanup returns the board; in the cleanup it
returns the board but fails the cleanup, so the run stops. Other registered boards are guards, read before and after every case.

How long a read-back is follows the part, so the comparison takes the length
from the range the command printed and checks it against the flash sizes in
the fork's DFU part table. Those sizes are the harness's own copy of that
table, which a part the fork adds later would leave behind: a correct
read-back on such a part would then be reported as a length no part explains.
To notice that, `run-suite` and `build` read the table out of the fork's
checkout at the resolved Ubertooth commit and record the comparison as a
`dfu_part_table` record in run.jsonl and a "DFU part table" row in report.md:
`passed` (the same flash sizes), `warning` (they differ; it names the part
whose entry the copy is missing, and the read-back length such a board would
report), `unchecked` (the figures are not where they are read from: the table
renamed, moved to another file, a second part list elsewhere, defined twice,
written in a way the reader does not know, or a read-back length the fork now
works out from something other than these three figures; the reason is
given), or `skipped` (the commit names none of `DFU_PARTS`,
`dfu_upload_length`, and `DFU_ASSUMED_FLASH_SIZE` anywhere, so it is older
than the figures and has none to compare with). A comparison
that stopped working reports itself, which is the failure this check exists
to avoid, and three things are beyond it: the arithmetic, since a fork that
still names all three figures and works the length out of them differently
compares as a match; a fork that renames all three at once, which leaves
nothing to find; and a second part list that is neither of the fork's own
part type nor named for the table, and whose rows name no part. The `DFU
images` step is what catches all three: the line a `-u` prints names the
length and the flash the part has, and the step checks one against the
other, so a length that is wrong for its part fails even where it would be
right for some other part in the table. The CI `build` job runs the same
comparison once against the fork's `main` and shows it on the workflow run
page (`scripts/ci.sh dfu-part-table` runs only that step); its `build` and
`run-suite` steps then record it without printing it again
(`UBERTOOTH_HARNESS_DFU_PART_TABLE_SHOWN`). A difference and an unreadable
table are also printed on
stderr and shown in the browser live view; a match and a skip leave the
report row to say so. Nothing fails on this: the read-back check stays the
harness's copy, so a length no part explains is still loud, and the warning
is what says to add the entry (`ubertooth_harness/dfuparts.py`).

