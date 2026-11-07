# Release testing

This is the release procedure for `SqueakyFloors/ubertooth`. It qualifies one
fixed Ubertooth commit and the exact libbtbb commit that commit pins. Do not
substitute a moving branch after testing starts.

The recurring release gate has a hard wall-clock limit of six hours. Its target
is five hours and thirty minutes, leaving thirty minutes for setup and result
collection. The 24-hour and 72-hour soaks are not release gates. A 72-hour run
may be performed once as an endurance experiment when a substantial release
cycle is otherwise ready.

[`hardware.md`](hardware.md) gives the complete setup and firmware details for
each device. [`tests.md`](tests.md) explains every test and command. This page
records which tests constitute a release, how they share the bench, and what
evidence is retained.

## Rules

- Resolve the release tag to a commit before the first run. Record that commit,
  the harness commit, and the resolved libbtbb commit in every run.
- Build the host tools and firmware from those commits. A system installation
  is not evidence for a release unless the installer test is the test being
  run.
- Every required test must pass. Failed, blocked, or skipped required work
  leaves the release unqualified. A platform marked not applicable is accepted
  only when another eligible host runs that requirement.
- Keep firmware, bootloader, USB-power, and other host-lock operations separate
  from captures already using the host.
- Parallel runs must lease different Ubertooths and peer fixtures. A plan may
  run beside another only when every command selects its board by serial and
  the plan does not bind unrelated boards as guards.
- Fix a failure, then rerun its affected suite. Rerun `fast` after any change to
  the candidate, its libbtbb pin, or the harness code used for the gate.
- Stop once all recurring gates pass and the public evidence bundle is made.
  Do not add an overnight soak to the release critical path.

## Required test matrix

| Gate | Plan and suite | Target time | What it establishes |
|---|---|---:|---|
| Fast | `testplans/fast.yaml`, `fast` | 30 min | Build, flash, command surface, short captures, state restoration, and deterministic replay |
| Main release | `testplans/release.yaml`, `release` | 45 min | Comprehensive host and device behavior, including required AFH recovery |
| Alternate image | `testplans/classic.yaml`, `btbr` | 5 min | `btbr` firmware and `ubertooth-btbr`, followed by application-firmware restoration |
| BLE | `testplans/ble.yaml`, `ble-comprehensive` | 20 min | Known advertising, tshark/extcap, known connection, and reacquisition; this already includes the connection tests |
| Classic | `testplans/classic.yaml`, `classic-comprehensive` | 30 min | Known piconet capture, follow, record fields, audio, survey, and AFH |
| BlueZ | `testplans/hci.yaml`, `hci-comprehensive` | 15 min | `ubertooth-scan` and `ubertooth-follow` through a selected host adapter |
| Weak link | runnable `ble-weak-link`, `classic-weak-link`, and connection weak-link suites | 20 min | Characterized 20 dB and 40 dB receive paths still meet their measured floors |
| Installation | current tagged `install.sh` acceptance | 20 min | Pinned host tools and libbtbb install, application firmware flashes, and exact version output |
| Bootloader | tagged installer on the programmer-paired board | 20 min | Bootloader update, application restoration, `-B`, `-v`, and `-V` readback |
| Stability | `testplans/address-soak.yaml`, `address-soak-4h` | 4 h | Address-reader parity, packet gaps and rates, memory, restarts, and watchdog evidence |

The individual targets are budgets, not reasons to stop a test early. A test
that legitimately needs its configured window keeps it. Exceeding six hours is
a procedure failure to correct before the next release, not permission to omit
a gate.

## Schedule under six hours

The four-hour stability run determines the minimum wall time. Use this schedule
once its plan leases only its selected Ubertooth and advertiser.

| Elapsed | Lane A | Lane B |
|---|---|---|
| 0:00-0:30 | Fast | Prepare immutable source refs and artifacts |
| 0:30-4:30 | Four-hour address soak on one Ubertooth and one DK | Main release, Classic, BlueZ, and Classic weak-link suites on different devices, sequential where they share ESP32 peers |
| 4:30-5:00 | BLE comprehensive and BLE weak-link checks after the soak releases the DK | Collect and inspect completed reports |
| 5:00-5:30 | Alternate-image, installer, firmware, and programmer-paired bootloader acceptance under the host lock | Build the release evidence bundle |
| 5:30-6:00 | Contingency and final review | No new optional testing |

There are two usable DKs on the reference bench. The address soak consumes one,
while BLE connection tests consume two, so those tests cannot overlap. Classic
and BlueZ tests use the ESP32 pair and may run during the one-DK address soak on
a different Ubertooth. Firmware, bootloader, and power-changing tests run after
the soak because they take the host lock.

If the bench cannot provide this isolation, reduce optional concurrency rather
than sharing a device. The release procedure must still be redesigned to fit
six hours; do not silently drop a required row.

## Running a suite

Validate before leasing hardware:

```sh
python -m ubertooth_harness validate-plan <plan> \
  --hardware hardware.yaml --suite <suite>
```

Run against immutable commits:

```sh
python -m ubertooth_harness run-suite \
  --plan <plan> --suite <suite> \
  --hardware hardware.yaml --host <registered-host> \
  --ubertooth-source <ubertooth-checkout> --ubertooth-ref <commit> \
  --ubertooth-ref-name <release-tag> \
  --libbtbb-source <libbtbb-checkout> --libbtbb-ref fork-pin \
  --build --output <run-directory>
```

Add only the build switches and artifacts the selected suite declares. The
`btbr` suite needs both firmware images and the Python tool. Long runs belong
on the persistent runner, so closing a client or SSH tunnel does not end them.

## Test hardware and utilization

Private registries contain real serials, ports, and paths. Public reports and
documentation use resource aliases or device types.

| Hardware | Harness role | Release use | Concurrency and safety |
|---|---|---|---|
| Linux host | Primary build and hardware runner | All Linux and BlueZ suites, firmware installation, and soaks | One host lock serializes firmware, bootloader, and USB-power changes; ordinary serial-selected captures may overlap on distinct fixtures |
| macOS host | Secondary host runner | Host-tool, parser, build, and applicable USB cases | Has no BlueZ-only tools; hardware runs require a physically attached registered board |
| Ubertooth One | Device under test | Every radio suite; separate boards may cover soak, Classic, weak-link, and administration lanes | Lease by full serial. Never share one board between runs. A board in DFU is unavailable to ordinary tests |
| Clear-path Ubertooth | Normal receive reference | Fast, release, BLE, Classic, HCI, installer, and soak tests | Preferred for functional gates and known-target captures |
| 20 dB receiver | Characterized weak link | BLE, Classic, and connection weak-link suites | Receive-only; thresholds come from measured runs rather than comparison with a clear board |
| 40 dB receiver | Characterized weak link | BLE and selected Classic weak-link suites | Receive-only; remains useful for packet-volume and map recovery checks |
| 50 dB receiver | Limit observation | Optional experiments only | Too weak for dense Classic release gates; not a required release lane |
| Two nRF52840 DKs | BLE advertiser, central, and peripheral | Known advertiser, extcap, connection, reacquisition, and address soak | One advertiser test needs one DK; a connection test needs both, so it cannot overlap the one-DK soak on this bench |
| Two ESP32 Classic peers | Classic acceptor and initiator | SPP traffic, audio/eSCO traffic, Classic follow, AFH, BlueZ scan/follow, and `btbr` | The pair is one fixture and cannot be split across tests. It can run beside a one-DK soak on another Ubertooth |
| Host Bluetooth adapters | Selected BlueZ controllers | HCI comprehensive suite | Select by registered `hciN`; adapters complement rather than replace deterministic DK or ESP32 peers |
| Debug programmer and paired Ubertooth | Bootloader recovery fixture | Required bootloader installer acceptance | Update only the physically paired target. Lease programmer and target together and keep the recovery path available |
| Managed USB power hub | Per-port recovery and exclusive-topology fixture | Power-cycle, exclusive-board, failed-flash recovery, and diagnostics | Any power change takes the host lock. Never power-cycle a port owned by another run |
| Plain powered hub | Additional stable USB capacity | Devices that need no automated power operation | No recovery control; unsuitable for tests that require a power cycle |
| Stackable attenuators | RF-path definition | 20 dB and 40 dB release checks; 50 dB experiments | Record total attenuation and placement in the private registry; do not infer it from signal strength |

The detailed firmware versions, shell protocols, registration examples, USB
identities, hub behavior, and known limitations remain in
[`hardware.md`](hardware.md). Update that document whenever a fixture or its
firmware changes; update this matrix when the release schedule uses it
differently.

## Evidence retained with the release

Issue #370 defines the public-safe export. Attach these files to the matching
fork release:

- `validation-summary.md`, with exact commits, plans, UTC times, durations,
  totals, measurements, failures and dispositions, and any missing gate;
- `validation-logs.tar.zst`, containing sanitized reports and command logs;
- `validation-SHA256SUMS`.

The export removes machine names, local paths, private aliases, operator data,
hardware serials, and unrelated captured traffic. Unredacted records stay on
the test host. The release owner explicitly uploads the reviewed bundle; a
harness run never publishes it automatically.

## Optional endurance work

A 72-hour soak is useful once to characterize a substantial release cycle, but
it is not a release gate and is not repeated for every release. The optional
2020-12-R1 comparison measures improvement; current-release stability decides
the release. Record optional results separately so their absence cannot look
like a missing required gate.
