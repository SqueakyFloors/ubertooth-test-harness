# Hardware utilization

Which fixture each test uses, what firmware it has to be running, where it
sits in the RF path, what it cannot be used for at the same time as something
else, and why it is on the bench at all.

Read this to plan a run or a schedule. How to set a fixture up is in
[hardware.md](hardware.md); what each test checks is in [tests.md](tests.md);
the release schedule that applies these conflicts is in
[release-testing.md](release-testing.md).

No bench is named here. A bench is a set of device types in a registry, and
`hardware.yaml` is the only place a real serial, port, or path appears.

## What transmits, and what only receives

The Ubertooth is the device under test, and in most tests it only receives:
known traffic comes from fixtures the harness owns, so a capture is checked
against an address, an interval, and a payload that were chosen rather than
observed.

It is not a receive-only platform, and the harness exercises both directions:

- `Two-device range test` (`range` plan) **transmits from both Ubertooths**:
  the sender runs the firmware's range test at each PA level up to 0 dBm and
  the responder answers as a repeater. Run it only where transmitting is
  allowed, for example in a shielded box or on an RF-isolated bench.
- `Transmit power steps` and `Receiver sensitivity` are the other
  board-to-board tests. Both are catalog entries without an implementation and
  sit in `testplans/weak-link.yaml`.
- `Ubertooth util conflicts` carries `allow RF transmission: true` as a guard:
  fork main refuses the option pairs that would start range test, repeater, or
  TX test, and a regression that accepted one would put the board on the air.

Release coverage of transmit is therefore thin, and
[release-testing.md](release-testing.md#8-coverage-this-plan-does-not-have)
records it as a gap rather than as something the platform cannot do.

## Fixtures

### Ubertooth One — the device under test

| | |
|---|---|
| Roles | `Ubertooth` (one board), `sender`/`responder` (range), `sender`/`receiver` (power steps, sensitivity), `guard Ubertooth N` (every other registered board on the host) |
| Firmware | `bluetooth_rxtx` built from the commit under test; a `--build` run writes it and reads `-v` back. The alternate `btbr` image for `Classic btbr console` only, written and restored by that test |
| Used by | every radio suite: `fast`, `release`, `ble*`, `classic*`, `hci*`, `range`, `exclusive`, `firmware`, `slow`, `mac`, `power` |
| RF placement | an ordinary board (antenna, no attenuation) for every capture, survey, and followed connection; the weak-link board only where a plan asks for it |
| Concurrency | leased by full 32-hex-digit serial; never shared between runs. Two runs may work different boards at the same time |
| Locking | board lease; the host lock as well for any run that writes firmware or switches power |
| Recovery | a hub port power cycle, where the board is on a switchable port |

Guard boards are not flashed. Preflight requires each guard's serial and that
its firmware API is the host tools' own, because `ubertooth-btle` may stop
such a board rather than read it. Every test that opens a board guards the
others, except the tests that open none (`Host CLI surface`, `Raw receive
replay`, `Refused command lines`, `Failed capture files`, `Util mode
refusals`, `DFU image checks`, `Python tools`) and `Ubertooth count` and
`BLE extcap interfaces`, which open all of them on purpose.

### nRF52840 DK — the Bluetooth LE peer

| | |
|---|---|
| Roles | `advertiser` (boot role), `central`, `peripheral`. One image holds all three, and a shell command moves a board between them |
| Firmware | [`firmware/nrf52840dk`](../firmware/nrf52840dk/README.md), `utdk-1.4.0` (`utdk-1.4.0-chansel1` for the channel-selection-algorithm-1 image); reflashed when a board reports another version |
| Used by | `Capture known BLE advertiser`, `BLE extcap in tshark` (one DK); `Capture known BLE connection`, `Follow a BLE connection across its end` (**two** DKs, one `central` and one `peripheral`); `MAC polling soak` (one DK, not implemented) |
| RF placement | ordinary; the attenuation in the BLE weak-link suites is on the Ubertooth, not the DK |
| Concurrency | a connection case holds two DKs for its whole test, so it cannot overlap anything else that needs one. The advertiser cases need one |
| Locking | leased by J-Link serial, with its serial shell held for the run |
| Recovery | it needs no power control; a J-Link stuck in its own bootloader needs a person ([hardware.md](hardware.md#a-j-link-stuck-in-its-own-bootloader)) |

A bench that registers no DK with `central` and another with `peripheral`
cannot run the connection cases at all; they are recorded as not applicable
rather than quietly passing.

### ESP32 Classic peer — the BR/EDR pair

| | |
|---|---|
| Roles | `acceptor` and `initiator`. The two together are **one fixture**: they hold a piconet between themselves |
| Firmware | [`firmware/esp32-classic-peer`](../firmware/esp32-classic-peer/README.md), `utesp-1.6.0`; reflashed when a board reports another version |
| Used by | `Capture known Classic device`, `Classic capture record fields`, `Follow Classic Bluetooth piconet`, `Recover AFH map`, `Bluetooth inquiry scan`, `Follow Classic piconet over HCI` (both peers); `Classic btbr console` (the `acceptor` alone) |
| RF placement | ordinary |
| Concurrency | the pair cannot be split across two tests. `Recover AFH map` is in the `release` suite and leases both bridges for that whole run, so the peer suites run before or after the release plan, never beside it |
| Locking | leased by its USB-UART bridge identity, and the lease takes the bench's own per-board lock file where one is configured. Taking turns with a peer restarts it, which costs the other run its board |
| Recovery | a restart over the bridge; the shell protocol has a frame that catches a bridge losing bytes |

### A host Bluetooth adapter — the BlueZ controller

| | |
|---|---|
| Role | `Bluetooth adapter`, named by the `hciN` the kernel gave it |
| Firmware | the adapter's own; nothing the harness builds |
| Used by | `Bluetooth inquiry scan` and `Follow Classic piconet over HCI` (`hci` plan), the only coverage of `ubertooth-scan` and `ubertooth-follow` |
| Concurrency | Linux only, and both tests also need the ESP32 pair, so they queue behind anything else using it |
| Locking | the adapter is bound as a resource of the invocation |
| Recovery | `hciconfig`-level reset by hand; no remote power control |

### Debug programmer — the recovery path

| | |
|---|---|
| Role | `Debug programmer`, recorded as `programs` exactly one board |
| Used by | `--bootloader update`, which installs a bootloader built from the run's own source on a stale board; `Reset into invalid application` (`power` plan) is run only on the board a programmer is paired with, because that is the board with a way back |
| Concurrency | the programmer and its board are leased as one pair, whichever of the two a command names |
| Recovery | this *is* the recovery: a bootloader is written over the board's in-system programming header, never over USB |

### Acroname USBHub3+ — switchable power with measurement

| | |
|---|---|
| Role | `USB power controller`, one unique port per powered board, per-port VBUS switching and readback |
| Used by | `Exclusive Ubertooth topology` and any plan with `exclusive Ubertooth: true` (every other board switched off, confirmed from USB enumeration); `Reset into invalid application` (the board's own port cut and restored inside one case step); recovering a wedged board by hand |
| Concurrency | a run that switches power holds the **host lock** for its whole length, so no other harness command on that host switches power or writes firmware meanwhile |
| Safety | real changes need `--allow-power-changes`; `Reset into invalid application` needs `--allow-destructive` as well, which the runner service never passes on. Ports are restored after success, failure, and cancellation |
| Recovery | a killed harness can leave a port off: restore it by hand before the next run ([power-control.md](reference/power-control.md)) |

### StarTech managed hub — switchable power without measurement

Same role, ports 1-3 switchable, port 4 refused because it carries the
management controller. It cannot measure VBUS and cannot switch data, so a
port reading "off" is not proof of power-off, and standard `uhubctl` power
control does not cut VBUS on it at all. Use it where an Acroname is not
available; prefer the Acroname for isolation gates.

### A plain powered hub — capacity only

No control interface. It carries the devices that need no power operation: on
the reference layout the DKs and the ESP32 bridges, on a second USB host
controller, because the J-Links are the heaviest users of the endpoint budget.
A board on a plain hub gives up `exclusive Ubertooth` and remote recovery.

### Attenuators — the weak link

| | |
|---|---|
| Role | recorded by hand as the board's `rf:` placement (`antenna`, `attenuation_db`, `distance_m`); nothing measures it |
| Used by | the suites that ask for `weak link:`: `ble-weak-link`, `classic-weak-link`, `range-weak-link`, and the entries waiting in `testplans/weak-link.yaml` |
| Measured | 20 dB and 40 dB. A plan may only ask for an attenuation a figure was taken at, judged per test and per role |
| Not usable | 50 dB: taken there for the Classic damaged-packet checks and the yield collapsed, so there is nothing at that attenuation for a test to be judged on |
| Concurrency | the weak-link board is an ordinary leased board; captures and surveys keep off it by their own placement default |

### The hosts

| | |
|---|---|
| Linux host | builds, every hardware suite, the BlueZ-only tools, power control, and enumeration-verified isolation (Linux only; other platforms fail closed) |
| Mac host | the `mac` plan: the build, the USB stack, streaming, and one smoke pass of the setting tests. No BlueZ tools. A wedged board there needs a person, so keep its runs short |

## Conflicts in one place

| Fixture | Held by | Cannot run beside |
|---|---|---|
| ESP32 peer pair | `release` (for `Recover AFH map`), every `classic*`, `btbr*`, and `hci*` suite | each other, or the release plan |
| DK pair | `connection` cases | the advertiser cases on the same DKs, and anything else needing a DK |
| One Ubertooth | whichever run leases it | itself; other boards are free |
| The host lock | any run that writes firmware or switches power | any other such run on that host |
| A firmware-writing test | `Flash reliability`, `DFU images`, `Classic btbr console`, `Reset into invalid application` | anything later on that board: preflight refuses a test running on an image this run put there, so they go last |

## Keeping this current

A pull request that changes which fixture a test uses, or adds or removes a
fixture, updates this file in the same pull request, beside
[hardware.md](hardware.md) and [tests.md](tests.md). The rule is in
[`AGENTS.md`](../AGENTS.md).
