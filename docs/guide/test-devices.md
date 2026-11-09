# Test devices: how the harness drives them

The registry side of the bench radios. The bench itself, its firmware, and its
setup are in [docs/hardware.md](../hardware.md); which test uses which fixture
is in [docs/hardware-utilization.md](../hardware-utilization.md).

## BLE test devices (nRF52840 DK)

Tests that need a real Bluetooth LE peer use Nordic nRF52840 DKs (PCA10056)
running the harness's own firmware, [`firmware/nrf52840dk`](../../firmware/nrf52840dk/README.md)
(build, flash, shell protocol, pinned SDK and tool versions);
[`docs/hardware.md`](../hardware.md) puts them beside the rest of the bench.
Register each
one as an `nRF52840 DK` resource with its J-Link serial, its role, and, for
the `advertiser` role, the BLE address its `status` reports
([`hardware.example.yaml`](../../hardware.example.yaml)):

```yaml
DK_Linux0:
  type: nRF52840 DK
  attached to: Host_Linux
  roles: [advertiser]
  identity:
    jlink serial: "000123456789"
    BLE address: "C0:00:00:00:00:01"
```

One image holds three roles — `advertiser`, `central` and `peripheral` — and
one shell command switches a board between them, so no test reflashes a DK
to change what it does. The connection tests need two DKs, one registered
with the `central` role and one with the `peripheral` role, both with a BLE
address. A host picks which DK takes which role with a default named after
the role:

```yaml
Host_Linux:
  type: Host
  defaults:
    Ubertooth: UT_Linux0
    nRF52840 DK: DK_Linux0   # the advertiser
    central: DK_Linux1
    peripheral: DK_Linux2
```

`ubertooth_harness/nrfdk.py` drives a DK: it checks the pinned `nrfutil`
(8.2.1) and `nrfutil device` (2.23.0), flashes by J-Link serial, and talks to
the shell on VCOM0 (`/dev/serial/by-id/usb-SEGGER_J-Link_<serial>-if00` on
Linux; `serial device` in the identity overrides it). Every command fails
closed with an `error:` line. A run passes the board's registry alias as
`--alias`, and each command then writes every line it sends to the shell and
every line the board answers on standard error, `[DK_Linux0 >] adv start` and
`[DK_Linux0 <] OK`, so the whole conversation is in the run's record and its
transcript (see [Known targets](../reference/run-records.md#known-targets)). `Capture known BLE advertiser`
([`testplans/ble.yaml`](../../testplans/ble.yaml)) flashes the DK only when it
reports another firmware version, from the image given as
`--artifact "nRF52840 advertiser=PATH/zephyr.hex"`:

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/ble.yaml --suite ble \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --artifact "nRF52840 advertiser=/tmp/utdk-build/zephyr/zephyr.hex" \
  --output runs
```

`Capture known BLE connection` (the `connection` suite of the same plan) uses
the pair: the peripheral advertises connectable, the central connects to its
address on the 1M PHY at the interval the plan sets, raises the data length
to 251 bytes, writes 244-byte payloads, and asks for a connection parameter
update, dropping and remaking the link every few seconds. A capture started
at any moment therefore holds a whole connection, which is what
`ubertooth-btle -f` is checked against.

`Follow a BLE connection across its end`, in the same suite, takes the window
that test leaves out: one capture across several of those reconnections. It
asks what a capture of one connection cannot — that the follow ends when the
connection does and finds the next one, that every connection the file
carries packets for is one the file also announced, and that the board is
idle and answering as soon as the capture is stopped.

The DKs transmit at normal BLE power; the Ubertooth only receives.

A DK is leased like an Ubertooth (`ubertooth-harness boards` and `lease`
list it), keyed by its J-Link serial (`nrf52840dk-<serial>.lock` in the
lease directory). A lab that also guards boards with its own per-board lock
files can hand the harness their path with `--external-board-lock`, and every
lease then takes the DK's file too (see [External board
locks](leases.md#external-board-locks)); the DK's file is the one named after its
J-Link serial, `board-<J-Link serial>.lock` for the template
`<lab lock dir>/board-{serial}.lock`, which is the file a lab's own scripts
already lock. One spelling of that serial, because one device has one file:
the registry takes the 12 digits the by-id name carries, zeros and all, and
refuses the zero-stripped spelling of the same number with the padded form
to write instead
([#324](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/324)).
The lease key strips the leading zeros and the lock file keeps them, so a
wrapper written against either spelling resolves the board through the
registry or `scripts/lab/hold` and takes the padded file. Hold
such a lock only for the run, so other users of the DK get their turn.

## Classic test devices (ESP32 Classic peers)

Tests that need real Classic Bluetooth (BR/EDR) traffic use two
ESP32-WROOM-32E boards running the harness's own peer firmware,
[`firmware/esp32-classic-peer`](../../firmware/esp32-classic-peer/README.md)
(build, flash, shell protocol, versions). They are the Classic counterpart of
the DKs: the two boards hold a Serial Port Profile link to each other and
stream a payload across it, so a test has BR/EDR traffic from an address it
knows, on demand, with no third-party device involved. The Ubertooth only
receives.

Register each board as an `ESP32 Classic peer` with the serial of its CP2102N
USB to UART bridge and the BD_ADDR its `status` reports
([`hardware.example.yaml`](../../hardware.example.yaml)):

```yaml
ESP32_Linux0:
  type: ESP32 Classic peer
  attached to: Host_Linux
  roles: [acceptor, initiator]
  identity:
    bridge serial: "0123456789abcdef0123456789abcdef"
    BD_ADDR: "C8:00:00:00:00:01"
```

A registry may also write down the `LAP`, `UAP` and `name` the board's
`status` reports, for a person reading it; the code derives them from the
address, and a copy that disagrees is refused as a typo.

One image holds both roles, `acceptor` (the boot role, which listens) and
`initiator` (which pages, connects, and streams), and one shell command
switches between them, so no test reflashes a peer. Both boards may declare
both roles; a host picks which one listens and which one pages:

```yaml
Host_Linux:
  type: Host
  defaults:
    ESP32 Classic peer: ESP32_Linux0
    acceptor: ESP32_Linux0
    initiator: ESP32_Linux1
```

**The LAP on the air is the central's, and the central is whichever board
pages.** The initiator pages, so every packet in the piconet carries the
initiator's LAP; the acceptor's LAP is on the air only during the page train,
which is over before a capture starts. That is why these cases take the LAP
and UAP from the initiator's registered BD_ADDR, and why exchanging the two
bindings moves the piconet to the other address.

`ubertooth_harness/esp32peer.py` drives a peer. It opens the bridge by its
stable path (`/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_<serial>-if00-port0`,
derived from the bridge serial unless the identity names a `serial device`),
waits for the firmware's boot notice, and speaks its line shell: `status`
(one JSON line with the BD_ADDR, LAP and UAP), `role`, `spp peer`,
`spp start`, `stream`, `spp stop`, `afh`. `reset` restarts the board with the
bridge's own RTS line, which is how a case releases one. Every command fails
closed with an `error:` line.

**Opening the bridge resets the board.** DTR and RTS are wired to the chip's
EN and IO0 pins, and the kernel asserts both when the port is first opened,
so the pair cannot be set up in one command and captured in the next, the way
a DK's can. `esp32peer.py stream` is the answer: it holds both ports for one
whole session, brings the link up, runs the tool under test while the pair
streams, stops the pair, and reports what both boards did. The tool's own
output is passed straight through on standard output, and the pair's report
(both BD_ADDRs, the central's LAP and UAP, writes, bytes received) is on
standard error, so a case checks the tool's output exactly as it would
without the peers:

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/classic.yaml --suite classic \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --output runs
```

Nothing in the harness flashes a peer, so the driver asks for a minimum
version (`utesp-1.0.0`) rather than an exact one: a later image of the same
major has these commands and more, and the run record keeps whichever
version each board reported. An older image, or another major version,
fails mandatory preflight naming what it found; build and flash it from
`firmware/esp32-classic-peer` at this harness commit.

**The bridge loses bytes, and the board can go away.** Both were measured on
the bench, and both are the driver's business rather than each case's:

- on a host that is also capturing over USB, a run of bytes goes missing on
  a peer's bridge, and the newlines either side of the hole arrive, so the
  line looks whole: `OK` has been read as `K`, and a status line has arrived
  as valid JSON with a shorter channel map than the controller has. There is
  no flow control to fall back on, because the bridge's RTS and CTS are
  wired to the chip's EN and IO0. So an image from `utesp-1.6.0` on is asked
  to put a length and a CRC around every line it writes (`frame on`), and a
  line that fails the check is read again, for the commands that can be
  asked twice safely: reads, and setters whose second application sets the
  same value. A status line that is not readable is still refused, framed or
  not. A board on an older 1.x image is driven exactly as before, and either
  way the session reports what the bridge cost it
  ([#310](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310));
- a peer that restarts under the capture is reported as that, with the
  reason the chip gives and the version it came back on, rather than leaving
  a board of zeroed counters to read as a pair that never streamed. The
  board is read while the command under test runs, which is the only moment
  it can say so, and the boards are reset either way
  ([#311](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311)).

`stream` also holds the piconet to part of the band. `--map <20 hex digits>`
(or `off`) is set on the initiator, which leads the piconet, before the link
comes up, so a whole capture runs on one map; `--map-report` writes the map
asked for and the map the live link is really hopping over, read from the
leader's controller at the start, the middle, and the end of the capture;
`--command-output` saves the tool's output for a later step; and
`--interrupt-after` stops a tool that runs until it is signalled. That is what
`Recover AFH map` is built from, and
[`ubertooth_harness/afhmap.py`](../../ubertooth_harness/afhmap.py) is the step that
compares the two maps. The map commands need `utesp-1.3.0` or a later image,
where the rest of the driver needs only `utesp-1.0.0`.

The `classic` suite of [`testplans/classic.yaml`](../../testplans/classic.yaml) is
the smoke run above plus `Recover AFH map`, `classic-comprehensive` adds the
capture files, the role exchange, and the AFH detection phase, and
`classic-long` is the bounded long variant: the same two follow cases over a
five-minute capture each, which is what shows that a receive run and the
pair's link hold up for minutes rather than seconds. The sampling
soak, `Classic Bluetooth receive soak`, is still a backlog entry; sampling is
what it waits for, not traffic.

One peer on its own is also a device an Ubertooth can discover and connect to,
because the boot role is discoverable and connectable. That is what the same
plan's `btbr` suite uses: `Classic btbr console` writes the alternate `btbr`
firmware image and pages the peer from the board. It is a suite of its own
because it writes the board's firmware, so no test may follow it on that
board; `btbr-comprehensive` adds the page nothing answers, and `btbr-long`
repeats the flash-and-session cycle five times.

Because the comprehensive coverage exchanges the two roles, both peers a plan
binds must declare both: the registry's role list is what a plan picks a board
by, and one image holds both roles anyway.

A peer is leased like an Ubertooth (`ubertooth-harness boards` and `lease`
list it), keyed by its bridge serial (`esp32-<serial>.lock` in the lease
directory). A lab that guards devices with its own per-device `flock` files is
met on those too, under the plain bridge serial, once the run is given the
template ([External board locks](leases.md#external-board-locks)). Hold the pair that
way for anything that drives it. The bridge's RTS and DTR are wired to the
chip's reset and boot pins, so a second session restarts the board under the
first one's capture in either of two ways: by pulsing those lines, which is
what releasing a peer to its boot state does, or by being the first to open a
port nobody held. An overlapping open is harmless. What is not is two
sessions taking turns with one peer
([#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317)).

The peers have no switchable power on this bench, so nothing power-cycles
one: a board that stops answering needs someone at it.

### The host Bluetooth adapter and the BlueZ-only tools

Two of the fork's host tools, `ubertooth-scan` and `ubertooth-follow`, are
built only when libbluetooth is found, and both drive a Bluetooth controller
of the host's as well as an Ubertooth: the scan runs that controller's
inquiry, and the follow reads its clock, pages a target for the target's
clock offset, and with `-a` reads the channel map of the link it made. An
Ubertooth cannot stand in for one. Register the controller as a
`Bluetooth adapter`:

```yaml
  BT_Linux0:
    type: Bluetooth adapter
    attached to: Host_Linux
    identity:
      hci device: hci0
      BD_ADDR: "00:1A:7D:DA:71:13"
```

`hci device` is the name BlueZ gave it, which the tools take with `-b`, and
`BD_ADDR` is the address `hciconfig <device>` reports. The `hciN` number is
attach order rather than a property of the adapter, so the address is what
identifies it: every case reads the device first and fails, naming what it
found, if the address is not the registered one. An adapter takes no roles
and is leased like a board, under `bluez-<address without its colons>`:
the name is attach order, so the address is what names the lock too. The
cases need
`hciconfig` and `hcitool` on the host, declared as host tools the harness
does not install. Reading the controller needs no privilege, but paging a
device does: `ubertooth-follow` pages its target in every case and needs
`CAP_NET_RAW`, which nothing else in the harness does. A run grants it to a
private copy of its own tool when it is given
`--allow-hci-capability`, and records the test blocked when it has neither
that nor the capability itself (see [`docs/hardware.md`](../hardware.md)).

[`testplans/hci.yaml`](../../testplans/hci.yaml) holds the two tests that bind it,
`Bluetooth inquiry scan` and `Follow Classic piconet over HCI`, on the same
ESP32 pair: the pair gives the adapter a discoverable device to find and the
Ubertooth a piconet to sweep for.

Both tests have run against a USB adapter, and against a second one: smoke
passed in 300 to 407 s over seven runs and comprehensive in 390, 400 and
526 s. The `hci` plan is
still not in `release.yaml`, and the reason is the peers rather than the
adapter — the runs found serial bytes lost under load
([#310](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310))
and peers that would not come back to their boot state
([#311](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311)) --
11 of 91 pair sessions between them, so four more pair-driven cases would
fail about two gate runs in five. What the runs settled and what they left open is in
[`docs/tests.md`](../tests.md), per case. Every step of the plan binds the
adapter, so running it on a bench without one is refused, naming the missing
resource; in a suite that holds other work the two tests are left out as not
applicable, like any other hardware a registry has not got.

