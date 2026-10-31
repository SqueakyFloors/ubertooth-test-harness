# The bench hardware

What a harness bench is made of, what firmware each device runs, how the
harness reaches it, and what a bench cannot do. Read it to set up a bench of
your own, or to judge a result someone else's bench produced.

No bench is described by name here: a bench is a set of device types in a
registry, and `hardware.yaml` is the only place a real serial, port, or path
appears (it is git-ignored; [`hardware.example.yaml`](../hardware.example.yaml)
is the template).

Companions: [`docs/tests.md`](tests.md) says what each test does with this
hardware, [`README.md`](../README.md) how to register and run it,
[`scripts/lab/`](../scripts/lab/README.md) holds the by-hand helpers (holding
a device, switching a hub port, the udev rule), and
[`DESIGN.md`](../DESIGN.md) describes the harness itself.

## The devices

| Device | Role on the bench | How the harness reaches it |
|---|---|---|
| Ubertooth One | the device under test: receive, transmit, range, flash | libusb, selected by its full 32-hex-digit serial (`-U`) |
| nRF52840 DK (PCA10056) | Bluetooth LE peer: advertiser, central, and peripheral | the on-board J-Link: SWD to flash, its virtual COM port for the shell |
| ESP32-WROOM-32E | Classic Bluetooth (BR/EDR) peer pair: SPP link and data | a CP2102N USB-to-UART bridge, for both flashing and the shell |
| Acroname USBHub3+ | per-port USB power and data, with VBUS readback | the BrainStem API over the hub's own USB control link |
| StarTech managed hub | per-port USB power, no measurement | its built-in serial management controller |
| A plain powered hub | carries devices that need no power control | nothing: it has no control interface at all |

Only the Ubertooth is the thing under test. The peers make the traffic, the
hubs cut the power, and everything else is plumbing.

### Ubertooth One

The board the harness tests. A bench needs at least one; most tests use one
board at a time, the two-device range test uses two, and `Ubertooth count`,
the transition tests, and `Flash reliability` read or enumerate every board
registered on the host, so all of them have to be attached and answering for
those.

Registered as type `Ubertooth` with its full 32-hex-digit serial. The serial
has to be the whole thing: libubertooth matches `-U` against the *end* of a
serial and takes the first board that matches, so a short serial can select
a different board than the lease was taken on.

```yaml
  UT_Linux0:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: 0123456789abcdef0123456789abcdef
    power:
      controller: Hub_Linux1
      port: 0
```

`power:` maps the board to a hub port, which is what lets a plan switch other
boards off (`exclusive Ubertooth`) and what lets a person power-cycle a wedged
board. A board on a plain hub has no `power:` entry and can take part in
neither.

An Ubertooth One presents itself on USB as `1d50:6002` in its application
firmware and `1d50:6003` in its bootloader (DFU). A board left in its
bootloader answers no normal command, which is why an exclusive run's
enumeration check treats an unidentified device on the bus as a failure rather
than ignoring it.

**Transmitting.** The two-board range test transmits on 2.4 GHz from both
boards, the sender stepping up to PA level 0 (0 dBm). Run it only where
transmitting is allowed, for example on an RF-isolated bench or in a shielded
box. Every other Ubertooth test receives only.

### nRF52840 DK

Nordic nRF52840 DK boards (PCA10056) are the Bluetooth LE peers: the Ubertooth
stays receive-only and the DKs do the transmitting, so a capture can be checked
against an address, an interval, and a payload that were chosen rather than
observed.

One image holds three roles, and one shell command moves a board between
them, so no test reflashes a DK to change what it does:

| Role | What it does |
|---|---|
| `advertiser` (the boot role) | legacy advertising from a fixed address on chosen channels |
| `central` | connects to the peripheral on the 1M PHY, raises the data length to 251 bytes, writes a counter payload, asks for a parameter update |
| `peripheral` | advertises connectable and accepts that connection |

The connection tests need two DKs, one registered `central` and one
`peripheral`; the advertiser tests need one. A host says which DK takes which
role with a default named after the role (`central:`, `peripheral:`, and the
plain `nRF52840 DK:` for the advertiser).

Registered by its J-Link serial, quoted so leading zeros survive, plus the
BLE address its `status` reports. That address is a static random address
derived from the chip's FICR, so it is the same on every boot of the same
board, which is what makes it worth registering:

```yaml
  DK_Linux0:
    type: nRF52840 DK
    attached to: Host_Linux
    roles: [advertiser]
    identity:
      jlink serial: "000123456789"
      BLE address: "C0:00:00:00:00:01"
```

`serial device:` overrides the derived port path where a host has no
`/dev/serial/by-id`. [`ubertooth_harness/nrfdk.py`](../ubertooth_harness/nrfdk.py)
is the one driver: it checks the pinned tool versions, flashes by J-Link
serial, opens the shell, and logs every line it sends and every line the board
answers into the run's transcript.

### ESP32-WROOM-32E Classic peer

Two ESP32-WROOM-32E boards (ESP32-D0WD-V3, 4 MB flash) are the Classic
Bluetooth pair, the BR/EDR answer to the DKs. They hold a Serial Port Profile
link and push data across it, so `ubertooth-rx`, `ubertooth-rx -l`/`-u`,
`ubertooth-follow`, and `ubertooth-btbr` have BR/EDR traffic from a known
address on demand, with no third-party device in the capture.

One image holds both roles:

| Role | What it does |
|---|---|
| `acceptor` (the boot role) | discoverable and connectable, with an SPP server listening |
| `initiator` | looks the peer's SPP channel up over SDP, connects, and streams a counted payload |

The link is opened with no security, so it needs no pairing and a capture holds
only the SDP lookup, the connection, and the data.

Registered with the bridge serial and the BD_ADDR its `status` reports (the
factory MAC plus two in the last byte, so it too is fixed per board). Those
two identity keys are the ones a run's known-target record reads
([`targets.py`](../ubertooth_harness/targets.py)):

```yaml
  ESP32_Linux0:
    type: ESP32 Classic peer
    attached to: Host_Linux
    roles: [acceptor, initiator]
    identity:
      bridge serial: REPLACE_WITH_BRIDGE_SERIAL
      BD_ADDR: "00:11:22:33:44:55"
```

Both boards boot as the acceptor and either can be made the initiator, so a
pair is registered with both roles and a test picks the direction it needs.
**The initiator pages, so the piconet follows its clock and hopping sequence,
and every packet in the piconet carries its LAP**; the acceptor's LAP shows up
only in the page train. A test that needs a particular board's LAP on the air
has to make that board the initiator.

The device name is not settable and does not need to be: it carries the
board's own LAP (`UT-ESP32-<LAP>`), so it is fixed for the life of the board
and different from the other one with no setup step.

No catalog test binds a peer yet, so a registry entry for one is inert today:
the pair is driven by hand over its shell, and the cases that will use it are
in flight.

### Acroname USBHub3+

The power controller worth having. Eight downstream ports (0 to 7), each with
switchable power and data, VBUS voltage and current readback, and a control
link that keeps answering while every downstream port is off. Registered as a
`USB power controller` with its 32-bit BrainStem serial and the
`acroname BrainStem USBHub3+` driver.

Validated against model 19 with hub firmware `0x28000000` and BrainStem
2.12.5. BrainStem is Acroname's proprietary Python API, so it is not in
[`requirements.txt`](../requirements.txt); install it into the harness
environment on a host that has a hub:

```sh
python3 bootstrap.py --with-acroname
```

Measured on a lab hub: a board leaves USB enumeration 0.06 to 0.22 s after its
port is switched off, VBUS falls below 0.1 V about 0.55 s after switch-off
with a board attached, and a board re-enumerates 0.37 to 0.59 s after power-on
and answers `ubertooth-util -v` about 0.02 s later. The harness's isolation
defaults (5 s removal, 10 s return, 1 s settle) leave ample headroom.

**Map the ports by switching them, never by the sysfs path.** The hub's two
internal hubs number their downstream ports the opposite way from the labels
on the case, so the sysfs paths run backwards against the port numbers. Switch
one port off, see which board leaves the bus, and write that pairing into
`hardware.yaml`.

`ubertooth-harness power --hardware hardware.yaml <controller>` reads every
port (power, data, VBUS, current, and the board mapped to it) without changing
anything.

### StarTech managed hub

A fallback power controller: it switches power on ports 1 to 3 and nothing
else. Port 4 carries the hub's own serial management controller and the
harness refuses it as a precaution. Registered with the stable
`/dev/serial/by-id` path of that controller and the
`startech managed USB hub` driver; it needs
[`pyserial`](https://pyserial.readthedocs.io/), which is pinned in
`requirements.txt`.

It cannot measure VBUS, so a snapshot reports voltage and current as unknown
and **a port reading "off" is not proof that power is off**. Isolation is
therefore confirmed from USB enumeration instead. Standard hub power control
([`uhubctl`](https://github.com/mvp/uhubctl)) only changes the reported state
on this hub without cutting power, so it must not be used.

### A plain powered hub

Any ordinary powered hub carries devices fine and controls nothing. What a
device behind one gives up:

- **No power cycle.** A wedged board, DK, or bridge waits for someone at the
  bench, because `scripts/lab/hub-port` has no port to switch. A DK can still
  be rebooted over its J-Link (`nrfutil device reset`), and an ESP32 over its
  shell (`reset`) while it still answers at all.
- **No isolation.** A plan with `exclusive Ubertooth: true` compiles power-off
  actions for every other registered Ubertooth on the host, and compilation
  fails if one of them has no valid controller mapping or no per-port VBUS
  capability. So a bench that mixes switchable and plain hubs cannot run an
  exclusive plan unless every Ubertooth is on the switchable one.
- **No VBUS evidence.** Nothing can say whether a port is really powered.

It is the right place for devices that need no power control and would
otherwise eat the switchable hub's capacity, which on a crowded host is the
real constraint (see [Known limits](#known-limits)).

### RF placement: antenna, attenuators, and distance

A radio unit may record where it sits, so a test can ask for a weak link and a
report can say what its figures were taken at
([#168](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/168)):

```yaml
    rf:
      antenna: true        # is the antenna attached
      attenuation_db: 20   # inline attenuation between the board and the air
      distance_m: 1        # how far from the rest of the bench (optional)
```

`rf:` describes the bench by hand and the harness never measures it. A unit
with no `rf:` is taken to be an ordinary one: antenna attached, no attenuation,
distance not recorded. A `Host` and a `USB power controller` have no radio and
are refused a placement.

A worked weak-link receiver: one Ubertooth on a USB extension about a meter
from the rest of the bench, with a 20 dB inline attenuator between the board
and its antenna. A plan asks for it per hardware role, for example
`rf: {Ubertooth: {min_attenuation_db: 10}}`, which is what a range, transmit
power, margin, sensitivity, or RSSI test wants; no committed plan asks yet, so
a recorded placement is latent until one does. The other way round is already
live: captures, surveys, and follow tests require an ordinary link themselves,
so they stay off an attenuated board with no plan saying so.

**Keep the numbers current.** Swapping the attenuator or moving the board
without editing `rf:` makes every later report quote the wrong conditions, and
nothing in the harness can catch it.

## Firmware

| Device | Source | Version | Built with | Flashed with |
|---|---|---|---|---|
| Ubertooth One | the [fork's](https://github.com/SqueakyFloors/ubertooth) `firmware/bluetooth_rxtx` | the resolved commit under test | `arm-none-eabi-gcc` | `ubertooth-dfu` |
| nRF52840 DK | [`firmware/nrf52840dk`](../firmware/nrf52840dk/README.md) | `utdk-1.2.1` | nRF Connect SDK v3.4.1 and its toolchain bundle | `nrfutil device program` |
| ESP32 peer | [`firmware/esp32-classic-peer`](../firmware/esp32-classic-peer/README.md) | `utesp-1.0.0` | ESP-IDF v5.5.6 | `esptool`, through `idf.py flash` |

Each firmware README holds the full build and flash procedure, the pinned
toolchain versions, and the shell command reference. Images are built outside
the repository and never committed; nothing under `firmware/` is generated.

### Which Ubertooth firmware, and why

The harness never tests host tools against firmware from another commit:
preflight requires every board whose firmware it cares about to report the
revision of the resolved Ubertooth commit. The host tools check the firmware's
API version on their own account as well. So on a shared bench, a board that
someone else flashed fails every run until it is written again.

A `--build` run therefore flashes its boards with the `bluetooth_rxtx.dfu` it
has just built, after the build and before the manifest and preflight. That is
the default whenever a run builds; `--no-flash-build` keeps the boards'
firmware and lets preflight check it as before, and a run without `--build`
cannot flash at all. Because flashing implies the firmware build, such a run
needs `arm-none-eabi-gcc`, and is refused up front without it.

Guard boards are not flashed: preflight only requires their firmware API to be
no older than the host tools'. Boards that a firmware-writing test
(`Flash reliability`, `DFU images`) writes itself are not flashed either.

### How the harness checks the running version

| Device | Check |
|---|---|
| Ubertooth One | `ubertooth-util -U <serial> -v` must report the resolved revision, read before the flash (a board already on it is not written) and again after, up to 3 reads while the board re-enumerates |
| nRF52840 DK | the shell's `status` must report the pinned version; a board reporting anything else, or nothing, is flashed, and the version and address are read again afterwards |
| ESP32 peer | the shell's `version` and `status` report it; the version the driver requires is pinned in [`targets.py`](../ubertooth_harness/targets.py) and a unit test keeps it in step with `main/peer.h` |

A DK image is also checked before it is written: `nrfdk.py` reads the `.hex`
and refuses one that does not contain the pinned version string, so a stale
build cannot be flashed by mistake. An Ubertooth image's SHA-256 is checked
again immediately before each write.

### Prebuilt images

An Ubertooth's firmware is built per run and hashed into that run's manifest,
so it needs no record here: the run says what it flashed. The other three
images are built or fetched once and flashed for as long as they are current,
so each is recorded with the commit its sources come from and its SHA-256.

| Image | What it is | Version | Source | SHA-256 |
|---|---|---|---|---|
| `kgv.dfu` | the known-good Ubertooth firmware that `Flash reliability` alternates against the run's own build | `2020-12-R1` | upstream Ubertooth release `2020-12-R1` | `e3fcb486c6e0ce7cc991bf6439f6dbc0156ed1229c2b95ebfd02198f9675bb11` |
| `zephyr.hex` | the nRF52840 DK role firmware | `utdk-1.2.1` | this repository at `94b2d85`, which is where `firmware/nrf52840dk` carries that version | `88c9479f792e3480c1362a74577fe387d89d342efde5b0a1e2d107098097c1ed` |
| `esp32-classic-peer.bin` | the ESP32 Classic peer firmware | `utesp-1.0.0` | this repository at `1d3a049`, which is where `firmware/esp32-classic-peer` carries that version | `6ff85eb6d7069b8663ac78af7836b99af25f3a72b6b34f4f55881145c62266c0` |

The KGV is the one the repository pins: the plan carries its revision and its
hash ([`testplans/slow.yaml`](../testplans/slow.yaml),
[`testplans/mac.yaml`](../testplans/mac.yaml)), and the run supplies the file
with `--artifact "KGV=<path>"`. The harness checks every image's SHA-256
before the run starts and again before each write, so a bench whose copy
differs fails before it writes anything.

The two peer images are not pinned, because the version string inside the
image is what the harness checks: it refuses a DK image that does not contain
the pinned version, and it flashes a board that reports another one. The
hashes above let a bench confirm it holds the same image this repository's
sources produce. Each was built as its firmware README says; a reproducible
build is not claimed, so compare the version first and the hash second.

Record any other image a bench keeps the same way, in its own private notes:

```sh
git -C <checkout> rev-parse HEAD
sha256sum /tmp/utdk-build/zephyr/zephyr.hex          # shasum -a 256 on macOS
sha256sum /tmp/utesp-build/esp32-classic-peer.bin
```

An image built from a branch rather than from `main` belongs in that branch's
pull request, not here, so this table never describes firmware the repository
cannot build.

## The APIs and protocols the harness depends on

### The serial shell, shared by the DK and the ESP32 peer

Both peer firmwares speak the same line protocol, so one style of driver
serves both. 115200 baud, 8N1, no flow control, no prompt, no echo, and no
colors. Send one command line; the board answers with zero or more data lines
and then exactly one line, `OK` or `ERR <reason>`.

| | nRF52840 DK | ESP32 peer |
|---|---|---|
| Port | the J-Link's first virtual COM port (VCOM0) | the board's CP2102N bridge |
| Line ending in | CR | LF or CR |
| Notices, not answers | `utdk: ...`, and Zephyr's `*** Booting nRF Connect SDK ...` banner | `utesp: ...`, and ESP-IDF's `W (` and `E (` log lines |
| A malformed command | Zephyr's own `<command>: wrong parameter count` or `: command not found`, which the driver treats as a refusal | `ERR` |

`status` answers with one JSON line and then `OK`, which is how a test reads
an address, the live connection parameters, and the counters without parsing
prose. A command that starts something waits for the controller's event and
answers from it, so a shell answer is never ahead of the radio and a test
never polls. A bad value answers `ERR` and changes nothing.

Stable device paths on Linux, derived from the registry identity:

```text
/dev/serial/by-id/usb-SEGGER_J-Link_<12-digit serial>-if00
/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_<serial>-if00-port0
```

**Always use the by-id path.** A `/dev/ttyUSB0` or `/dev/ttyACM0` number is
assignment order, and two bridges of the same type swap between boots.

### Reset behavior

**Opening an ESP32 bridge resets the board.** The CP2102N's DTR and RTS lines
are wired to the module's EN and IO0 pins, the auto-program circuit that lets
`esptool` flash with no button press and no jumper to move; asserting them as
the port opens reboots the board. So one invocation is one session: a driver
that opens the port, sends `role initiator`, and exits, then opens it again to
send `spp start`, has reset the board in between and lost the first command's
effect. Anything that needs several commands in order does them in one process
with the port held open.

The DK's virtual COM port carries no reset line to the target, so opening the
shell leaves the running role alone. A DK returns to a known state through its
own commands (`role`, `adv defaults`, `conn defaults`) or
`nrfutil device reset`, which reboots it into the boot role.

An Ubertooth resets when `ubertooth-dfu -r` finishes a write, and when its hub
port is power-cycled.

### BrainStem port control (Acroname)

The driver imports BrainStem lazily, connects over USB to the one configured
hub serial (a hub with another serial is simply not found), reads the serial
and model back, and addresses ports 0 to 7 through the hub's `usb` entity:

| Call | What it gives |
|---|---|
| `getPortState` | power in bit 0, USB 2.0 data in bit 1, and USB 3.0 data in bit 3 |
| `setPowerEnable` / `setPowerDisable` | switches VBUS for one port |
| `setDataEnable` / `setDataDisable` | switches both data bits of one port |
| `getPortVoltage` | VBUS in microvolts |
| `getPortCurrent` | VBUS current in microamps |

Data is reported and switched as a pair, so a port with only one of the two
data bits on reads as data off; isolation switches only VBUS and leaves the
data bits as it found them. Every BrainStem error code fails the call, every
write is read back (a stale readback fails), and a port outside 0 to 7 is
refused before the hub is asked. Real power and data changes are refused
unless the run is armed with `--allow-power-changes`, and every port is
restored to the state it was found in after success, failure, or cancellation.

### The StarTech management controller

The driver opens the controller exclusively by its `/dev/serial/by-id` path,
refuses unvalidated controller firmware, and checks every write against the
controller's readback of the full port mask. Any I/O error, timeout, or
malformed reply closes the link. Only the factory-default password is
supported.

The protocol is two CR-terminated commands: `GP` reads an 8-hex-digit port
mask, least-significant byte first with bit 0 meaning port 1, and `SP`
followed by the password padded to 8 characters and the new mask writes one.
That is also how a port is restored by hand if the harness is killed outright
while boards are isolated: read the mask, OR in the bits of the ports in the
last `isolation_applied` event, and write it back.

### J-Link and nrfutil

| Command | What it does |
|---|---|
| `nrfutil device list` | lists attached probes with their serials, padded to 12 digits |
| `nrfutil device program --serial-number <serial> --firmware <hex> --options chip_erase_mode=ERASE_ALL,verify=VERIFY_READ,reset=RESET_SYSTEM` | flashes a DK and verifies the write |
| `nrfutil device reset` | reboots a DK, which is the only remote reset it has when it is not on a switchable port |
| `JLinkExe` | J-Link Commander: updates the probe's own firmware, and `vcom enable` turns its virtual COM port on |

The harness checks the pinned `nrfutil` (8.2.1) and `nrfutil device` (2.23.0)
versions before it flashes, and every command fails closed with an `error:`
line. SEGGER J-Link 9.88 is the validated host software.

### esptool

ESP-IDF's `esptool` (4.12.0, which comes with the SDK) writes an ESP32 image,
usually through `idf.py`:

```sh
idf.py -B /tmp/utesp-build -p <by-id path> -b 460800 flash
```

About 20 seconds per board. No button press and no jumper change, because of
the bridge's reset wiring above.

### Sharing the bench

Several agents or people share one host's devices through leases. An
Ubertooth or a DK is leased by its serial before anything touches it, and
anything that switches USB power or writes firmware also takes the host lock.
Where the bench's own scripts lock devices with their own per-device files, a
lease takes those too (`--external-board-lock`). See
[Sharing boards: leases](../README.md#sharing-boards-leases).

For a command you run by hand, [`scripts/lab/hold`](../scripts/lab/README.md)
takes the leases and names devices the way a bench note does, by serial or by
an unambiguous prefix of one.

## Host setup

### Linux

**The Ubertooth's udev rule.** The fork's host tools install
`40-ubertooth.rules`, which gives each Ubertooth USB ID mode `0660` and the
group its CMake found (`usb`, else `plugdev`); add your account to that group.
The simplest route is the fork's own
[`install.sh`](https://github.com/SqueakyFloors/ubertooth/blob/main/install.sh),
which installs the build prerequisites and the rules; the harness never runs
it and installs nothing system-wide itself.

**Serial ports.** The DK's virtual COM ports and the ESP32 bridges appear as
`/dev/ttyACM*` and `/dev/ttyUSB*`, usually group `dialout`; add your account to
it as well. Nothing in the harness needs root.

**The Acroname hub.** BrainStem devices need a udev rule of their own.
Acroname ships the script that writes it (`udev.sh`, in the BrainStem
driverless package; see
[Acroname's USB drivers reference](https://acroname.com/reference/brainstem/usbdrivers.html)).
Without it, only root can open the hub's control link.

After adding or changing any rule:

```sh
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then unplug and replug the device, since a rule does not change the
permissions of an already-open device node.

**Packages.** Python 3.12 with its `venv` support (Ubuntu:
`python3.12-venv`), then `python3 bootstrap.py`; the fork's host-tool build
prerequisites; `arm-none-eabi-gcc` for any run that builds firmware or flashes
a board; BlueZ, which `ubertooth-scan` and `ubertooth-follow` need and which
exists on Linux only; `tshark` from the distribution (no GUI) for
`BLE extcap in tshark`, which is not applicable on a host without it.
Add `--with-acroname` to `bootstrap.py` on a host with an Acroname hub.
Install the peer SDKs only on the host that builds the peer
images: nRF Connect SDK v3.4.1 with `nrfutil sdk-manager` (about 6 GB), and
ESP-IDF v5.5.6 (about 3 GB). A host with docker can build the DK image
without that install, with `scripts/ci.sh firmware-dk` (see the
[firmware README](../firmware/nrf52840dk/README.md)); flashing still needs
`nrfutil`.

### macOS

A Mac needs no udev rules and has no BlueZ, so `ubertooth-scan` and
`ubertooth-follow` are not applicable there. A Mac is a test host with its own
Ubertooths and runs a runner, never the controller; the peers and the power
controllers stay on the Linux host. See
[A Mac test host](../README.md#a-mac-test-host).

## Known limits

### The USB endpoint budget

This is the limit that bites a crowded bench, and it looks like broken
hardware. A USB host controller has a fixed number of active endpoint
contexts, set in the silicon: Intel puts the ceiling at 64 for its Ivy
Bridge-era xHCI parts, the kind found in small desktop hosts, so no kernel
setting raises it ([Intel's own answer on the
limit](https://community.intel.com/t5/Embedded-Intel-Core-Processors/Hardware-limitations-on-USB-endpoints-XHCI/m-p/264563)).
Once the budget is spent, configuring the next device fails and whichever
device enumerated last loses.

What the bench's devices cost in non-control endpoints:

| Device | Endpoints each |
|---|---|
| Ubertooth One | 2 |
| J-Link with its virtual COM ports | 8 |
| J-Link in its reduced single-interface mode | 2 |
| CP2102N bridge | 2 |

Three DKs with working COM ports are 24 endpoints on their own, which is why a
full bench can want more than one controller's budget.

**How it shows up.** A device sits on the bus but every claim fails with
`libusb -5`; `cat /sys/bus/usb/devices/<path>/bConfigurationValue` is empty;
`dmesg` says a host controller ran out of resources. A J-Link comes up with
one interface instead of five and has no COM port at all. Check those three
things before concluding that a board is wedged or that a change broke
something.

**What helps.**

- Ask the kernel to configure the device once:
  `sudo sh -c 'echo 1 > /sys/bus/usb/devices/<path>/bConfigurationValue'`. It
  wins when another device has just let go of endpoints, and costs nothing when
  it fails. Try it once; do not loop. A reboot or a re-plug loses it again.
- Take idle devices off the bus: spare mice, keyboards, and anything else that
  is not part of the bench.
- The real fix is a second host controller: add a PCIe USB host controller
  card and move the hub carrying the DKs and the bridges onto it, which gives
  that hub a budget of its own. Leave the switchable hub where it is. Note
  that a chipset's EHCI controllers may be no help: where its `XUSB2PR`
  register routes every USB 2 port to the xHCI controller, nothing can be
  plugged into them.

**The consequence to plan around:** on a host at its ceiling, a reflash or a
reset can lose a board that was working, because the device has to be
configured again when it comes back. Prefer a board on a host with headroom
for anything that flashes.

### A J-Link stuck in its own bootloader

A J-Link whose probe firmware did not start comes up with a single USB
interface and no virtual COM port, so the DK's shell is unreachable even
though the board can still be flashed over SWD. Run `JLinkExe` and let it
update the probe's firmware when it offers; if the probe is current but still
has no COM port, `vcom enable` at the `J-Link>` prompt turns the feature on,
and the probe has to be power-cycled for it to take effect
([SEGGER's note on VCOM](https://kb.segger.com/Using_J-Link_VCOM_functionality)).

### Devices with no remote recovery

Anything not on a switchable hub port cannot be power-cycled remotely. A DK
answers `nrfutil device reset` over its J-Link and an ESP32 answers `reset`
over its shell, but a device that has stopped answering altogether waits for
someone at the bench. Keep that in mind when deciding what goes on which hub.

For a device that does have a port,
[`scripts/lab/hub-port`](../scripts/lab/README.md) is the recovery:
`hub-port <controller> cycle <port>`, once, then look.

### Enumeration verification is Linux-only

The isolation and exclusive-Ubertooth checks confirm what is on the bus with a
read-only scan of sysfs, which exists on Linux only; on any other platform
they fail closed rather than assume. An exclusive plan is therefore a Linux
plan.

### Reading a Classic capture of the peer pair

Two protocol facts that look like harness or tool faults and are not:

- **The LAP on the air is the initiator's**, because it pages and the piconet
  takes its address and clock from it. `-l <the acceptor's LAP>` sees nothing
  during a link the acceptor did not initiate.
- **A survey's UAP is a guess** and is often wrong when few packets have been
  heard. Take the UAP from the board's `status` or the registry and pass it
  with `-u`, rather than trusting a short survey.

## A worked bench layout

One Linux host, two USB host controllers, and one Mac:

| What | Where | Why |
|---|---|---|
| Each Ubertooth | its own port of the Acroname hub, one board per port | a power cycle recovers a wedged board without taking anyone else's board down, and `exclusive Ubertooth` can switch the others off |
| The weak-link Ubertooth | an Acroname port, on a USB extension about a meter away, 20 dB inline attenuator, `rf:` recording both | ready for a range, margin, or RSSI test that wants a weak link, and kept away from captures and surveys by their own requirement |
| An empty Acroname port or two | left free | room for a spare board, and headroom in the endpoint budget |
| The nRF52840 DKs | a plain powered hub on the *second* host controller | they need no power cycling, and the J-Links are the heaviest endpoint users on the bench, so they get a budget of their own |
| The ESP32 bridges | the same hub as the DKs | they need no power cycling either, and they sit next to the DKs in the same budget |
| The Acroname's control link | the first controller, with the Ubertooths | it must stay reachable while it switches the ports it owns |
| Mice, keyboards, and anything else | off the bench host, or accepted as spent endpoints | each one costs endpoints that a board or a COM port wants |
| The Mac's Ubertooths | the Mac, no power controller | macOS steps only; a wedged board there needs a person, so keep its runs short |

A bench with one controller and one hub still works. It gives up the exclusive
and isolation plans for any board on a plain hub, remote recovery for
everything, and headroom: expect to choose between a third DK's COM port and a
sixth board.

## Keeping this current

Hardware, firmware, and the helper scripts change, and a doc that lags is
worse than none, because a reader cannot tell which parts still hold. A change
to any of them updates this file in the same pull request; the rule is in
[`AGENTS.md`](../AGENTS.md).
