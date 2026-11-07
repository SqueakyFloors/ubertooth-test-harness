# Release test plan

## 1. Purpose and scope

Qualify one candidate release of
[`SqueakyFloors/ubertooth`](https://github.com/SqueakyFloors/ubertooth) on
real hardware, in one pass that finishes inside six hours.

In scope: the host tools, the libubertooth library, the application firmware,
the alternate `btbr` image, the bootloader, the Python tools, the installer,
and the radio behavior all of those produce on a registered bench.

Out of scope: anything a deterministic test in the fork already covers, and
every long soak. No soak is a release gate ([section 11](#11-optional-endurance-work)).

What each test does, and what a pass does not prove, is in
[tests.md](tests.md). Which fixture a case needs is in
[hardware-utilization.md](hardware-utilization.md); bench setup is in
[hardware.md](hardware.md).

## 2. Release artifact under test

| Item | How it is fixed |
|---|---|
| Ubertooth commit | the release tag resolved to a full commit before the first run; no moving branch after testing starts |
| libbtbb commit | the `LIBBTBB_REF` that commit sets in its own `install.sh`, resolved once (`--libbtbb-ref fork-pin`) |
| Host tools, library, firmware images, bootloader, Python tools | built from those two commits by the run (`--build`, plus `--with-firmware`, `--with-btbr`, `--with-python` where a suite needs them) |
| Harness commit | recorded in every run manifest |
| Known-good image, where a case alternates against one | the pinned upstream `2020-12-R1` `bluetooth_rxtx.dfu`, sha256 `e3fcb486…bb11`, passed as `--artifact "KGV=<path>"` |

A system installation is not evidence for a release, except in the installer
case that is about the installation itself.

## 3. Entry criteria

All of these before the first gate run:

1. The release tag resolves to a commit, and that commit's libbtbb pin
   resolves to a commit. Both are recorded.
2. Both source checkouts are clean: a run refuses a dirty or untracked tree.
3. The fork's own CI is green on that commit.
4. `arm-none-eabi-gcc` is installed on the build host; the firmware build is
   implied by flashing.
5. The bench registry is current for the hardware actually attached, and every
   fixture a case below needs is registered and answering.
6. No stale lease and no held host lock: `ubertooth-harness boards` shows the
   boards and the host lock free.
7. `run-suite --dry-run` exits 0 for every suite in [section 5](#5-test-cases)
   that this bench is to run. A dry run that exits 1 names a blocked step, and
   that is dealt with before the gate starts, not during it.
8. The harness unit suite passes on the harness commit (`python
   scripts/test.py`).

## 4. Test environment and equipment

| Equipment | Needed for | Notes |
|---|---|---|
| Linux host, registered, with the harness bootstrapped | every case but TC-15 | builds, BlueZ tools, power control, enumeration-verified isolation |
| macOS host, registered, with a runner | TC-15, and TC-14 on macOS | no BlueZ tools |
| Ubertooth One, ordinary RF path | every radio case | one board is enough for TC-01 to TC-08; more boards shorten the schedule |
| Second Ubertooth | TC-11, TC-12, and the multi-board cases inside TC-01/TC-02 | TC-11 needs two boards it may transmit between |
| Ubertooth behind a measured attenuator | TC-09, TC-10 | exactly 20 dB, antenna attached, recorded in `rf:` |
| nRF52840 DK, `advertiser` | TC-03 | one board |
| nRF52840 DK registered `central` **and** another registered `peripheral` | TC-04 | a bench with only one DK cannot run TC-04; record it as a gap |
| Two ESP32 Classic peers (`acceptor`, `initiator`) | TC-02 (`Recover AFH map`), TC-05, TC-06, TC-07, TC-08 | the pair is one fixture and cannot be split |
| Host Bluetooth adapter | TC-08 | Linux only |
| Switchable USB power controller | TC-12 | Acroname preferred; the run must be armed |
| Debug programmer paired with one board | TC-17 | the only way back for a bootloader write |
| RF-isolated bench or shielded box | TC-11 | TC-11 transmits |

Private registries hold the real serials, ports, and paths. Reports and this
document use resource aliases and device types.

Every run is started with the plan, the suite, the host, the resolved commits,
and `--output` under the retained run directory, on the persistent runner
where the run is long, so closing a client does not end it:

```sh
python -m ubertooth_harness validate-plan <plan> --hardware hardware.yaml --suite <suite>
python -m ubertooth_harness run-suite \
  --plan <plan> --suite <suite> \
  --hardware hardware.yaml --host <registered-host> \
  --ubertooth-source <ubertooth-checkout> --ubertooth-ref <commit> \
  --ubertooth-ref-name <release-tag> \
  --libbtbb-source <libbtbb-checkout> --libbtbb-ref fork-pin \
  --build --output <run-directory>
```

Add only the build switches and artifacts the selected suite declares.

## 5. Test cases

Time budgets are the catalog's estimates for the suite's tests on one host,
which is what `/api/plans` reports; they exclude the build, flashing, and
preflight, whose duration the harness learns per host. Treat a budget as a
planning figure, not a licence to cut a test short.

| ID | Case | Plan / suite | Method | Pass criteria | Fixtures | Budget |
|---|---|---|---|---|---|---|
| TC-01 | Smoke | `testplans/fast.yaml`, `fast` | `--build` run of the whole suite | every test passes; preflight confirms host, libbtbb, firmware, serial, bootloader identity | 1 Ubertooth (others guarded) | 3 min |
| TC-02 | Main release suite | `testplans/release.yaml`, `release` | `--build --with-firmware --with-python` run on the host the AFH step names, so the `Python tools` and `DFU image checks` cases are not left out | every test passes, `Recover AFH map` included; no test recorded `blocked` | 1 Ubertooth + ESP32 pair | 20 min |
| TC-03 | BLE known traffic | `testplans/ble.yaml`, `ble-comprehensive` | `--build` run; the tshark case needs `tshark` installed on the host | known advertiser captured on each advertising channel, extcap and tshark paths decode it | 1 Ubertooth + 1 DK | 8 min |
| TC-04 | BLE connection | `testplans/ble.yaml`, `connection` | `--build` run | a CONNECT_IND between the two registered addresses with the asked parameters, followed data packets, and the follow surviving the connection's end | 1 Ubertooth + 2 DKs | 3 min |
| TC-05 | Classic comprehensive | `testplans/classic.yaml`, `classic-comprehensive` | `--build` run | known piconet captured and followed, record fields correct, AFH map recovered including the detection case | 1 Ubertooth + ESP32 pair | 23 min |
| TC-06 | Classic long | `testplans/classic.yaml`, `classic-long` | `--build` run | the bounded long follow holds up for its window | 1 Ubertooth + ESP32 pair | 11 min |
| TC-07 | Alternate `btbr` image | `testplans/classic.yaml`, `btbr-comprehensive` | `--build --with-firmware --with-btbr --with-python` run; **last on its board** | `ubertooth-btbr` console behavior on the `btbr` image, then the application image written back and verified | 1 Ubertooth + ESP32 `acceptor` | 3 min |
| TC-08 | BlueZ-only tools | `testplans/hci.yaml`, `hci-comprehensive` | `--build` run on Linux | `ubertooth-scan` and `ubertooth-follow` through the selected adapter find and follow the peer piconet | 1 Ubertooth + ESP32 pair + adapter | 8 min |
| TC-09 | BLE weak link | `testplans/ble.yaml`, `ble-weak-link` | `--build` run | the 20 dB and 40 dB advertiser captures meet their measured floors | attenuated Ubertooth + 1 DK | 2 min |
| TC-10 | Classic weak link | `testplans/classic.yaml`, `classic-weak-link` | `--build` run | the 20 dB followed piconet meets its measured floor | attenuated Ubertooth + ESP32 pair | 2 min |
| TC-11 | Two-device range, **transmitting** | `testplans/range.yaml`, `range` | `--build` run on an RF-isolated bench | the sender gets a clean round trip, `-n` reports its request and reply PA levels and copy numbers, and `-m` reads the same result back | 2 Ubertooths, both transmitting | 1 min |
| TC-12 | Exclusive board topology | `testplans/exclusive.yaml`, `exclusive` | `--build --allow-power-changes` run | every other board switched off and confirmed from enumeration, the no-selector tools reach the one board, every port restored | 2+ Ubertooths + power controller | 1 min |
| TC-13 | DFU image handling | `testplans/firmware.yaml`, `firmware` | `--build --with-firmware` run; **last on its board** | `-s` suffixing, `-u` read-back, and every bad image refused before a write; the test image verified at the end | 1 Ubertooth | 2 min |
| TC-14 | Flash reliability, per platform | `testplans/slow.yaml`, `slow` | `--build --with-firmware --artifact "KGV=<path>"`, once on Linux and once on macOS; **last on its board** | 100/100 verified writes alternating the build and the pinned KGV, zero manual recoveries, zero power cycles, no change to an unselected board | 1 Ubertooth per platform | 5 min per platform |
| TC-15 | macOS host behavior | `testplans/mac.yaml`, `mac` | `--build --with-firmware --with-python --artifact "KGV=<path>"` run on the Mac runner | every test passes: build, USB stack, streaming, and the smoke setting passes | Mac host + 1 Ubertooth | 6 min |
| TC-16 | Installer acceptance | by hand, no harness suite | run the candidate tag's own `install.sh` on a clean prefix, then read `ubertooth-util -V` | the tools and libbtbb install from the pinned refs, firmware flashes, and `-V` reports the release-name line a build of this commit would report (see below) | 1 Ubertooth | 20 min |
| TC-17 | Bootloader acceptance | `--bootloader check` on TC-01/TC-02, then `--bootloader update` by hand | the gate runs record every board's bootloader; the update is run on the programmer-paired board only | the installed bootloader matches one built from this commit; `-B`, `-v`, and `-V` read back after the update; the application image runs again | programmer + its paired board | 20 min |

### The version output a release requires

Normal release validation targets the **current release-name format**. A build
of the candidate commit reports the release it came from, with libbtbb's
beside it:

```
Host tools version: 2026-11-DEV1+22 (libbtbb 2020-12-R1+79)
Firmware version: bluetooth_rxtx 2026-11-DEV1+22 (API:1.58)
```

The harness works out what a build of the resolved commit would report and
requires exactly that layout, so TC-16's readback and every run's preflight
are checked against the release name, not against a commit hash. The older
two-line `git-<hash>` layout belongs to refs before that change and is not
part of qualifying a current release. `2020-12-R1` appears here in two
legitimate places only: as the pinned known-good image TC-14 alternates
against, and as an **optional** comparison arm ([section
11](#11-optional-endurance-work)).

## 6. Execution order and schedule

Order rules, in force whatever the bench:

1. TC-01 first. A failure there is cheaper to diagnose than anywhere else.
2. The ESP32 pair is one fixture, and TC-02 holds both bridges for its whole
   run. TC-02, TC-05, TC-06, TC-07, and TC-08 are therefore **sequential with
   each other**.
3. Cases that write firmware or switch power (TC-07, TC-12, TC-13, TC-14,
   TC-17) take the host lock and go **after** the cases that need the run's
   firmware on that board. Nothing runs on a board after a test that wrote its
   firmware.
4. Two runs may overlap only when they lease different boards and different
   peer fixtures, and when neither takes the host lock.

A schedule that fits a bench with at least two Ubertooths, the ESP32 pair, one
DK, a power controller, and a Mac runner:

| Elapsed | Lane A (peer fixtures) | Lane B (boards with no peer) |
|---|---|---|
| 0:00-0:30 | resolve commits, entry criteria, dry runs | TC-01 |
| 0:30-1:30 | TC-02 | TC-03, then TC-09 on the attenuated board |
| 1:30-2:15 | TC-05 | TC-11 on the isolated pair, then TC-15 on the Mac |
| 2:15-2:45 | TC-06 | TC-04 where a DK pair is registered |
| 2:45-3:15 | TC-08 | TC-10 after TC-05 releases the peers |
| 3:15-3:45 | TC-07 (writes firmware; host lock) | collect and read the finished reports |
| 3:45-4:30 | TC-12, TC-13, TC-14 under the host lock | TC-14 on the Mac in parallel (its own host lock) |
| 4:30-5:00 | TC-16, TC-17 | build the evidence bundle |
| 5:00-6:00 | contingency and final review | no new optional testing |

The harness-run cases total about 90 minutes of test time on one host; the
rest of the budget is builds, flashing, preflight, the two manual cases, and
contingency. If the bench cannot provide the isolation a lane needs, reduce
concurrency rather than sharing a fixture: a shared fixture produces a result
that belongs to neither run.

Exceeding six hours is a procedure failure to correct before the next release.
It is not permission to drop a case.

## 7. Failure handling and rerun rules

- A failed, blocked, or canceled required case leaves the release
  **unqualified**. There is no "known flaky, carry on".
- Fix the cause, then rerun the affected suite in full. Rerun TC-01 after any
  change to the candidate, its libbtbb pin, or the harness code used for the
  gate.
- A case may be rerun **once** without a code change only where the retry is
  recorded and both results are retained, and only for a failure already
  characterized as bench noise. One such failure is characterized today:
  TC-11's firmware occasionally takes a CRC-OK packet that is not a copy of
  its request, about one run in ten on a close-coupled bench, and `-n` then
  reports `invalid range test: mismatch on byte 0`
  ([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)).
  Record the rerun either way.
- A power cycle or a manual recovery may gather diagnostics after a failure.
  It never converts that run into a pass.
- A `blocked` record means the bench could not measure something the plan is
  about. Register the fixture and rerun, or decide explicitly to run a suite
  that does not hold that step and record the reduced coverage.
- A platform recorded `not applicable` is accepted only when another eligible
  host runs that requirement.
- A failure that looks like the fork rather than the bench gets an issue on
  the fork, referenced from the release summary.

## 8. Coverage this plan does not have

Record these with the release rather than discovering them later:

- **Ubertooth-to-Ubertooth transmit beyond the range test.** TC-11 covers the
  range test in both directions. `Transmit power steps` and `Receiver
  sensitivity` are catalog entries without an implementation, and the
  firmware's TX test and repeater modes are covered only by
  `Ubertooth util conflicts` refusing to start them. The platform can
  transmit; the gate measures one case of it.
- **BLE connection cases need two DKs.** On a bench that registers only one,
  TC-04 is not applicable and nothing in the gate follows a live LE
  connection.
- **Weak-link entries still waiting on a measurement or an implementation**
  live in `testplans/weak-link.yaml`; `ubertooth_harness/weaklink.py` names
  what each one needs. 50 dB is characterized as unusable rather than
  unmeasured.
- **No soak is a gate**, so nothing here measures hours-long stability.
- **`ubertooth-ego` and the E-Go device** are out of scope.

## 9. Exit criteria

The candidate is qualified when all of these hold:

1. Every applicable case in [section 5](#5-test-cases) passed, on runs whose
   manifests name the resolved Ubertooth commit, the resolved libbtbb commit,
   and the harness commit.
2. No run finished `failed`, `canceled`, or `lost`, and no run recorded a
   `blocked` test.
3. Every case that was not run is recorded as not applicable with its reason,
   and no required coverage rests on a host that did not run it.
4. TC-16 read back the release-name `-V` output for this commit.
5. Every board is back on the candidate's own application firmware, every hub
   port is restored, and no lease or host lock is held.
6. The evidence in [section 10](#10-retained-evidence) exists, and
   `ubertooth-harness validate <run dir>` reproduces each run's recorded
   checks.
7. The whole gate finished within six hours, or the overrun is written down
   with its cause.

## 10. Retained evidence

Keep, per run: the run directory with `run.jsonl`, `report.md`, and
`output/`. Keep, per release: one summary that names the exact commits, the
plans and suites, UTC start and end times, durations, totals, the
measurements, every failure and its disposition, every rerun, and any case not
run.

The public export is the one defined by
[#370](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/370),
attached to the matching fork release:

- `validation-summary.md`;
- `validation-logs.tar.zst`, sanitized reports and command logs;
- `validation-SHA256SUMS`.

The export removes machine names, local paths, private aliases, operator data,
hardware serials, and unrelated captured traffic. Unredacted records stay on
the test host. The release owner uploads the reviewed bundle; a harness run
never publishes it.

## 11. Optional endurance work

A 72-hour soak may be run **once**, as an endurance exercise, when a
substantial release cycle is otherwise ready. It is never a gate, never on the
release critical path, and never repeated per release. The only modeled
workload is designed and not implemented
([soak workload](reference/soak-workload.md)); its capture is one persistent
`ubertooth-btle -n -U <serial>`.

An optional comparison arm against `2020-12-R1` measures improvement. It
decides nothing: the current release's own results decide the release. Record
optional results separately so their absence cannot be mistaken for a missing
gate.
