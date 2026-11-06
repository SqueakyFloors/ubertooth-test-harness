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
| A host Bluetooth adapter | the controller the two BlueZ-only tools drive | BlueZ, by the `hciN` name the kernel gave it (`-b`) |
| A debug programmer | wired to one board's debug or programming header, for flashing and recovery by hand | nothing: the harness only holds it with that board, and a bench tool drives it |
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
has to be the whole thing: libubertooth treats an all-digit `-U` value as a USB
index and matches anything else case-insensitively against the *start* of a
serial, so a short serial is a prefix that another board may answer to, and a
lease could be taken on one board while the tool opens another.

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

**Booking the advertiser for a soak.** `BLE address parity soak`
(`testplans/address-soak.yaml`) holds one Ubertooth and the `advertiser` DK
for its whole window, which its longest suite makes three days, and it drives
the DK on and off throughout, so nothing else can use either device
meanwhile. Book it rather than scheduling it beside other bench work, and run
it from the deployed target runner, which keeps the run going after whatever
started it disconnects. Two things it costs the host: the DK's serial port
stays open for the run, and the capture file grows for as long as the run
lasts — on a crowded advertising channel the capture holds every advertiser in
range, not only this one, so the plan's `capture file limit` is what keeps any
one file manageable. **Check the host's free space before booking a long
run**, because the total is the channel's traffic, not the advertiser's.
Measured on this bench on 2026-11-05, channel 37, over a 30-second capture:
1.3 MB of capture and 14 MB of the tool's own transcript, with 40116 packets
from 6652 other addresses beside the advertiser's 225. At that rate three
days of capture is on the order of ten gigabytes, which is why a long run has
a disk budget as well as a per-file limit: past it the oldest complete
capture is removed and the newest kept, each removal recorded. The transcript
has a
run-wide budget of 64 MiB that restarts and file rotations cannot multiply
(`addresssoak.MAX_LOG_BYTES`); every line is still read either way, so the
figures do not change, and the run reports how much it kept. The run also
reports the capture process's resident memory from `ps`, which needs no
privilege and nothing installed; a host that refuses to run `ps` costs the
memory figures and nothing else.

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
link and push data across it, and they hold a synchronous (eSCO) voice link,
so `ubertooth-rx`, `ubertooth-rx -l`/`-u`, `ubertooth-follow`, and
`ubertooth-btbr` have BR/EDR traffic from a known address on demand, with no
third-party device in the capture.

One image holds both roles:

| Role | What it does |
|---|---|
| `acceptor` (the boot role) | discoverable and connectable, with an SPP server listening; for an audio link, the hands-free unit, which waits to be paged |
| `initiator` | looks the peer's SPP channel up over SDP, connects, and streams a counted payload; for an audio link, the audio gateway, which pages |

The link is opened with no security, so it needs no pairing and a capture holds
only the SDP lookup, the connection, and the data. Nothing in it is encrypted,
which is what lets a capture's payload lengths and CRCs be checked against the
bytes beside them.

**Voice traffic too.** `sco start` opens a synchronous (eSCO) link over the
Hands-Free Profile, so the air carries reserved-slot voice packets. An audio
session is that link and the profile's own service level connection, an ACL
link; it starts no data stream, and a board can hold both at once if a test
asks for them. `sco type ev3` asks for a plain EV3
link, whose payload is on the air at the basic rate a receive-only Ubertooth
can decode; the 2-EV3 link the stack chooses by default carries the same
payload as enhanced rate, which a basic-rate decoder cannot read. The
hands-free unit has to be listening before the gateway pages it, so a session
runs `sco start` on the acceptor first and `sco start` on the initiator last.
`role` takes an audio link down but keeps the packet type a board was told to
ask for; only `defaults` puts that back.
[`esp32peer.py`](../ubertooth_harness/esp32peer.py)'s `audio` mode does the
whole sequence in one invocation, as `stream` does for the data link.

What the two packet types put on the air, measured on the bench, is in
[the peer firmware's README](../firmware/esp32-classic-peer/README.md): an
EV3 link halves the payload per packet and halves the interval to carry the
same 64 kbit/s, so it is about twice the packet rate and the busiest traffic
the pair can make. What a capture reads from either is the packet types, the
timing, and the count; the audio pattern the firmware sends does not reach the
air as it was sent, because the controller codes the samples to CVSD on the
way out. The Hands-Free service level connection, an ACL link, stays up for as
long as the session does, so a capture holds that traffic beside the
reserved-slot packets.

A board has carried the link, and counted its frames, since `utesp-1.1.0`; the
packet-type choice and the fields reporting what a link settled on came with
`utesp-1.4.0`, and the counters that say how many links a profile has had with
`utesp-1.5.0`, which is what an audio session asks for (below). Every other
command works on any 1.x image.

`utesp-1.6.0` added two things for the bridge and the board rather than for
the air: `frame <on|off>`, which puts a length and a CRC around every line
the firmware writes, and a `utesp: boot reason <why>` notice on every boot.
See [The serial shell](#the-serial-shell-shared-by-the-dk-and-the-esp32-peer)
below for what each is for. It also gives the controller a third baseband
link slot (`CONFIG_BTDM_CTRL_BR_EDR_MAX_ACL_CONN=3`), so a follow test, which
uses two at once, has one spare for anything else in the room that pages a
peer; that is headroom rather than a fix for anything measured, as the
section on a restarting peer below says. The driver asks for `frame on` where
the image has it and drives an older one exactly as before, so nothing needs
reflashing for these.

Because the boot role is discoverable and connectable, one peer on its own is
also a device an Ubertooth can discover and connect to, which is what
`Classic btbr console` pages. That test binds one peer as `acceptor` and resets
it before the session, so an earlier test's role, link, or `disc off` cannot
reach it.

The device name is what the peer answers an inquiry and an LMP name request
with, and the firmware builds it from the board's own address
(`UT-ESP32-` and the LAP in upper case) and will not let the shell set it. So
the harness derives it rather than asking a registry for it, and the reset
before a session checks the board's own against it. A registry may still write
it down beside the address, as it may the LAP and the UAP, and then all three
have to agree with the address.

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

`Capture known Classic device`, `Follow Classic Bluetooth piconet`, and
`Classic capture record fields` bind a pair, through
[`esp32peer.py`](../ubertooth_harness/esp32peer.py). Nothing in the harness
flashes a peer, so the driver asks for `utesp-1.0.0` or a later 1.x image
rather than an exact version, and each board's own version goes in the run
record. The audio link is the one thing that needs more: `sco type` arrived in
`utesp-1.4.0` and the counters that say how many synchronous links a profile
has had in `utesp-1.5.0`, so the `audio` mode asks for the later of those and
names the version a board reports when it is older. Those counters are what
let a session tell one link that lasted its whole capture from a second link
that replaced the first part way through
([#264](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/264)). A peer is leased like a board, under `esp32-<bridge serial>`, and
under its plain bridge serial where the lab locks devices with its own files.

**What a second session can do to a peer**, measured on the bench for
[#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317)
(2026-11-04, one probe at a time, each while a live session held the port with
the Serial Port Profile link up and streaming):

| a second process that | the board |
|---|---|
| opens the port with pyserial's defaults, raising DTR and RTS together | keeps running: `role` still `initiator`, writes still climbing |
| opens the port asking pyserial for an exclusive lock | cannot open it: `errno 11, Could not exclusively lock port` |
| opens it and pulses RTS with DTR low, which is `esptool`'s hard reset | **restarts**, then reports `utesp: boot reason power on`, `role` back to `acceptor` and writes at 0 |

So an overlapping open is harmless. Two sessions cost each other a board by
*taking turns* with one: the first open of a port nobody held resets it, and
so does the reset pulse that releases a peer to its boot state at the end of
a session. That is why a peer's lock has to be held for the whole of a
session, and has to be the file the lab's own scripts take. Why the first
open resets the board and a second one does not is not established: both
raise the two lines, and nothing here depends on the difference.

Two limits that follow, worth knowing before relying on either:

- `pyserial`'s `exclusive=True` is an advisory `flock` on the tty, not
  `TIOCEXCL`. It refuses another caller that also asks for exclusive and lets
  in one that does not, so it is not a substitute for the lease;
- the follow case's own condition showed no restart in 13 runs.
  `ubertooth-follow -t <central> -a -b <device>` against the streaming pair,
  with nothing else on the bridges, 10 runs of 25 s and 3 of 150 s, gave 0
  peer restarts, with the peer's own log showing the second baseband link
  open and close each time. That is one bench and one firmware revision, so
  it bounds how often the follow path alone does this rather than ruling it
  out.

By hand, [`scripts/lab/peer`](../scripts/lab/README.md) names the boards by
their registry aliases, holds them, and runs that module, which is how a
capture against the pair is taken outside a run: `peer stream` for the data
link and `peer audio` for the voice link.

### A host Bluetooth adapter (BlueZ)

One bench has one, a USB adapter on the Linux host that also holds the two
ESP32 Classic peers, and both tests that bind it have run against it. A
second adapter was on that bench for a while, and both suites were run
against each of the two, with a registry per adapter differing only in which
one the host's default named: every command in a run named the adapter that
run was given, and read back that adapter's own address. This section says
what such an adapter has to be and how it is registered; the measurements
below come from those runs.

Two of the fork's host tools are built only when libbluetooth is found, and
both drive a Bluetooth controller of the host's as well as an Ubertooth:

- `ubertooth-scan` runs that controller's inquiry (`-s`) and reads the name
  and, with `-x`, the version and features of each device it reaches;
- `ubertooth-follow` reads the controller's clock, pages a target to read the
  target's clock offset, and with `-a` reads the channel map of the link it
  made, then follows that piconet with the Ubertooth.

An Ubertooth cannot stand in for either: it has no HCI interface, and the
clock and the map come over HCI from a controller joined to the link. So an
ordinary USB Bluetooth adapter, of any make BlueZ drives, is the whole
requirement. BR/EDR is what both tools use; Bluetooth LE support is not
needed. It must be up (`hciconfig <device> up`) and not in use by
anything else: `bluetoothd` may run, since the cases drive the controller
through BlueZ rather than around it, but nothing else may hold a link on it
while a case runs, and the cases refuse to start if one is up.

Registered as a `Bluetooth adapter` with two identity keys: `hci device`, the
`hciN` name BlueZ gave it, which is what the tools take with `-b`, and
`BD_ADDR`, the address `hciconfig <device>` reports for it. An optional
`description` is a note for a person reading the registry. It takes no roles:
one host has one controller for these tools.

The `hciN` number is the order the kernel attached controllers in, not a
property of the adapter, so the address is what identifies it, and what names its
lock. Every case reads `hciconfig <device>` first and fails, naming what it
found, if the address is not the registered one, rather than driving whatever
took the name; it reads it again in the cleanup, so an adapter that went away
during a case is reported rather than passing as one with no links. An
adapter is leased like a board, under
`bluez-<address without its colons>`: one
controller answers one inquiry at a time and holds one link to a target, so
two tests using it at once would read each other's results.

A host may have more than one, and the registry holds them apart in both
directions: two adapters on one host may not share an `hci device`, and they
may not share a `BD_ADDR` either. The second is what registering the same
adapter again produces after it has come back under another `hciN` name — two
aliases for one radio, which would share one lease, since the lease key is
built from the address, while the cases drove two devices. Both are refused
when the registry loads.

Naming the device is not optional anywhere, and that is worth stating because
nothing in a run would complain if it were dropped. What each program does
without one differs, and neither answer is the bound adapter:

- the two fork tools take the host's first available controller, so they would
  drive the wrong radio;
- `hciconfig` with no device lists every controller, and `hcitool con` with no
  `-i` reports the links of every controller that is up. The output then still
  contains the bound adapter's own block, so the patterns these steps match
  would **pass** — while the links reported, and refused, belonged to any
  controller on the host.

So every command a case runs against a controller names the bound one —
`hciconfig <device>`, `hcitool -i <device>`, `-b <device>` for the two fork
tools — and that is checked by its shape rather than by its output, which is
the only way to catch the two reads: a unit test builds every case against a
second adapter and fails if any command would reach the first. On a bench
with one adapter none of this is visible, since the fallback and the choice
are the same radio, which is why a second adapter is what showed it.

The cases also run `hcitool -i <device> con`, before the tool and again at
the start of the cleanup, to catch a link to a peer that a run left up.

`hciconfig` and `hcitool` are declared as host tools the harness does not
install, so a host without them leaves the cases out rather than failing
them — **and that is a trap worth knowing**: they are BlueZ's older tools and
some distributions have moved them out of the main `bluez` package, so a bench
that has an adapter but not them runs the `hci` plan with every case left out
and the run passes having exercised nothing. Install them with the adapter,
and read a run's `Not applicable` section before believing it proved
anything. A mechanism that cannot be missing this way (`btmgmt`,
`bluetoothctl`, or `/sys/class/bluetooth/<device>/address`) is worth
considering once a bench exists to test one on.

**One step of these cases needs a privilege nothing else here does: the
page.** Everything here talks to the controller over a raw Bluetooth HCI
socket, and opening one is not itself privileged — the kernel's own filter
on such a socket (`hci_sec_filter` in `net/bluetooth/hci_sock.c`) lets an
unprivileged opener send a subset of the HCI commands and requires
`CAP_NET_ADMIN` for the rest. Measured on the bench as an ordinary user with
no capabilities anywhere: `hciconfig <device>`, `hcitool con`, `hcitool inq`,
`hcitool name <peer>` and `hcitool clock <peer>` all work, and
`hcitool cc <peer>` does not — `Can't create connection: Operation not
permitted`. HCI Create Connection is the command outside the filter's subset,
where Remote Name Request is inside it.

So of the four binaries only the two fork tools need anything, and only for
what they page: `ubertooth-follow` pages its target for the clock offset in
every case, and `ubertooth-scan` pages only with `-x`. Without the
capability `ubertooth-follow` fails with
`error: connecting to <address> failed: Operation not permitted` — the tool's
own message, early, before it opens the board. An adapter bench therefore
needs one of:

- `CAP_NET_ADMIN` on `ubertooth-scan` and `ubertooth-follow`
  (`sudo setcap cap_net_admin+ep <path>`) — which has to be redone for each
  build of them, since a `--build` run puts fresh binaries in its own run
  prefix. The bench granted `cap_net_raw,cap_net_admin+ep`, which also works
  and is what the runs below were measured with;
- or the run made by a user or service that already holds it.

`hostneeds.Needs` has no way to express a capability, so nothing checks this
before the run.

One caution belongs here too:
`ubertooth-scan` pages every piconet its sweep heard to read its name, and
`-x` fetches a report from every device the inquiry answered. On a site with
other people's Bluetooth devices in range, that transmits to them. Run it
where that is acceptable.

### Why three Bluetooth fixtures, and not one

Three of the devices above are Bluetooth peers of one kind or another, and a
reader coming to the table for the first time reasonably asks why any bench
needs all three. They are complementary, not interchangeable: each one gives
the harness a kind of control or a counter the other two cannot.

- **The nRF52840 DKs are the deterministic LE endpoints.** Their firmware is
  the harness's own, so a test sets the advertising payload, channel, and
  type, and the interval; the connection peer, interval, data length
  extension, write size and period, the data channel map, a parameter update,
  and connection cycling. Its address is **known rather than settable**: a
  static random identity address derived from the board's FICR, fixed across
  boots, which is what it advertises from — knowing it in advance is what a
  test needs, and the shell cannot change it. Channel selection algorithm 1
  or 2 is likewise chosen by **which of the two images is flashed**
  (`chan-sel-1.conf` turns algorithm 2 off in the controller) rather than by
  a command. The DK then reports endpoint-side counters — writes, write
  errors, bytes received, parameter updates, connects, and disconnects — and
  the parameters in force, including the supervision timeout, so a check can
  compare what the Ubertooth saw against what the far end says it did.
- **The ESP32 peers are the deterministic Classic endpoints.** Their firmware
  is likewise the harness's own, and it supplies a known LAP and UAP and a
  known role for each board, Serial Port Profile traffic, EV3 and 2-EV3
  audio, the piconet clock, the AFH map the pair requests and the one in
  force, and frame and controller counters.
- **A USB Bluetooth adapter is a host-side BlueZ fixture.** It is not an
  endpoint the harness programs at all. What it gives is the host's own
  controller: inquiry, the two tools that need one (`ubertooth-scan` and
  `ubertooth-follow`), HCI identity and device selection, and the behavior of
  an adapter that is busy or has re-enumerated under another `hciN` name.

Two BlueZ adapters can be made to connect to each other, which is why the
substitution looks tempting. It does not hold. A pair of adapters exposes far
fewer deterministic link controls and counters than either programmed
endpoint, and both of them run on the same host stack, so the two ends of the
link are no longer independent: one kernel, one BlueZ, and one driver fault
moves both ends at once.

The normal bench therefore needs two shell-capable DKs, two ESP32 peers, and
one host Bluetooth adapter. A second adapter is useful for multi-adapter
testing or for a second runner, and is optional: no plan here binds more than
one adapter per test, so a bench with a single adapter runs everything.

### A debug programmer and the board it programs

A bench may have a programmer left permanently wired to one board: a USB
serial adapter on an Ubertooth's in-system programming header, an SWD probe on
a debug connector, or anything of that kind. It is for writing a bootloader or
recovering a board that no longer answers over USB, which is work done by hand
with the vendor's own tool; no test binds one and the harness never drives it.

What the harness does is keep the two together. The programmer and the board
it is wired to are one piece of hardware as far as using either goes:
programming the board while another command owns it, or using the board while
it is being programmed, spoils a test or leaves the board needing hands on it.
So the registry records the pairing and every command that holds one member
holds the other.

```yaml
  UT_Linux1:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: fedcba9876543210fedcba9876543210

  Prog_Linux0:
    type: Debug programmer
    attached to: Host_Linux
    programs: UT_Linux1
    identity:
      serial: A000AA00
      # Optional, where the adapter has one:
      # serial device: /dev/serial/by-id/usb-Vendor_Adapter-if00-port0
```

- `identity.serial` is the serial the adapter's USB descriptor reports, and is
  required: it names the lease, as `programmer-<serial>`, taken verbatim as
  registered (a `serial device`, where one is recorded, must be the stable
  `/dev/serial/by-id` path, not `/dev/ttyUSB<n>`, which is assignment order).
  It has to leave room for that prefix and for every other lock name built
  from it, so 53 characters at most; a longer one is refused when the registry
  loads, naming the field and the limit.
- `programs:` is the one board it is wired to. A registry is refused when that
  board is not registered, is not a device the harness leases (a host, a hub:
  there would be no lock to take for it), is attached to another host (leases
  are local to a host, so a pair across two of them would hold nothing),
  already has another programmer, or is the programmer itself, and when the
  pairing loops back on itself.
- Its serial has to be its own, as every device's does, and the rule is not
  this type's: see "One lock file is one device" under [Sharing the
  bench](#sharing-the-bench). A programmer is the one device whose serial is
  chosen by whoever labels the adapter rather than read from the hardware,
  so it is the easiest one to give a name something else already has
  (harness [#288](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/288),
  [#290](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/290)).
- A chain is allowed and is held as one set, for a bench that wires a
  programmer to another programmer's own header: holding any member holds all
  of them.
- Holding either member holds both, in one all-or-nothing attempt in the
  harness's own lock order, whichever one was named; naming both is harmless.
  That covers `scripts/lab/hold`, `ubertooth-harness lease`, a run that uses
  or power-cycles the board, and `scripts/lab/hub-port` on either member's
  port. The three by-hand commands say on standard error which device the
  pairing brought in and why, and `ubertooth-harness boards` prints the
  pairing under both. A run says the same line before it takes the locks and
  carries it in its own record: a `lease_pairing` record in `run.jsonl`, a
  "Leases from a wired pair" row in `report.md`, and the same line in the
  browser's live view (harness
  [#289](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/289)).
- The programmer appears in `ubertooth-harness boards` like any other leased
  device, so a holder a reader did not expect is explained where they see it.

Nothing about a particular bench is in the harness: the type, the serial, and
the pairing all come from the registry.

#### Installing a bootloader through it

A board's bootloader is the first 16 KiB of its flash. `ubertooth-dfu` runs
from there, so an ordinary firmware write cannot replace it: a board keeps the
bootloader it came with until somebody installs another one over the
programmer. `ubertooth-util -B` reports which one a board has (see
`Bootloader identity` in [`tests.md`](tests.md)), and a run may be asked to
install the current one where a board is on an older or different bootloader:

```sh
ubertooth-harness run-suite ... --build --bootloader update
```

What that needs on the bench, beyond the pairing above:

- **`lpc21isp` on the host**, which is the programmer command the Ubertooth
  repository documents (`docs/source/programming.rst` there, and the `program`
  target of `firmware/bootloader/Makefile`). The harness runs it exactly as
  documented: `lpc21isp -control <image>.hex <port> 230400 4000`.
- **`arm-none-eabi-gcc`**, because the bootloader to install is built from the
  run's own Ubertooth source rather than taken from a file somewhere.
- **A `serial device` on the programmer**, which is the port `lpc21isp`
  drives. A programmer registered without one is still held with its board,
  but a run cannot program through it and says so.
- **The adapter wired so `lpc21isp` can reset the board into ISP**, which is
  the CTS-to-RTS modification the Ubertooth documentation describes for an
  FTDI breakout. Without it the board has to be put into ISP by hand, which no
  run can do.

The image is the whole 16 KiB region: the built bootloader padded with the
0xff of erased flash, which is what makes the hash the board reports afterwards
a property of the artifact alone rather than of whatever was in the top of the
region before. A run records the region image, its CRC-32, and the commit it
was built from, writes it once, and then requires `-B` to report exactly that
version, build string, and hash; a failed write is a hard failure that is
never retried, and recovery is another deliberate run with the same programmer.

`--bootloader record`, the default, only reads `-B` and records it, which is
what a bench run and the release suite do: a bootloader write is physical and
cannot be undone over USB, so it is never implicit. `--bootloader check`
compares without writing, and fails a run whose board is on an older or
different bootloader.

The Ubertooth fork has since gained `ubertooth-dfu --update-bootloader`, which
installs a bootloader over USB. The harness deliberately does not use it: an
automatic bootloader write goes through a programmer that is physically wired
to the board, so a board that will not come back can be recovered at the bench
(the maintainer's to decide: [#302](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/302)).

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
  actions for every other registered Ubertooth on the host. A board on no
  power controller blocks the step, and the run fails on it; a mapping that is
  there and wrong, including a controller without per-port VBUS switching, is
  refused outright. So a bench that mixes switchable and plain hubs can run
  an exclusive plan only for the boards whose every other Ubertooth is on the
  switchable hub.
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
and its antenna. A plan asks for it per hardware role, which is what a range, transmit power,
margin, sensitivity, or RSSI test wants. `range.yaml`'s `range-weak-link`
suite is the committed example: it asks for a responder at exactly this
attenuation (`rf: {responder: {min_attenuation_db: 20, max_attenuation_db: 20,
antenna: true}}`) and an unattenuated sender. The other way round is already
live: captures, surveys, and follow tests require an ordinary link themselves,
so they stay off an attenuated board with no plan saying so.

**Keep the numbers current.** Swapping the attenuator or moving the board
without editing `rf:` makes every later report quote the wrong conditions, and
nothing in the harness can catch it.

**How much attenuation is useful, measured.** Stacking attenuators to force
decode errors has a ceiling, and on this bench it is below 50 dB. A receiver
taken to 50 dB about a meter from the Classic peers, against a dense Serial
Port Profile link, recorded 62 packets across six 15-second windows where
three 20 dB windows recorded 237; four of the six heard nothing at all, at
`-e 0` it recorded nothing against 20 dB's 190, and no window recovered a
piconet clock
([#306](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/306)).
Past a point, more attenuation stops producing damaged packets and starts
producing no packets, and a test cannot be judged on what a board did not
hear. `weaklink.CHARACTERIZED_UNUSABLE_DB` records such a point with its
reason, and a plan that asks for one is refused with that reason instead of
being told the figure is simply unmeasured. The worked receiver above is the
20 dB one, which is what the bench layout table at the end of this file
describes; 50 dB was a measurement taken, not a placement to keep.

Three things that mislead when reading those runs:

- **A reported signal power only covers what was decoded**, because a packet
  the receiver never detected reports no power at all. Between 20 and 50 dB
  the mean reported signal moved far less than the attenuation did while the
  `-e 0` yield fell from 190 records a window to none, so a signal power that
  barely moves is not evidence that the attenuator did nothing: the packets
  it would have shown are missing from the mean rather than weak within it.
- **Error flags are not ordered by attenuation.** Across 19 windows the
  highest rate of failed header checks was on the *unattenuated* board, 3.6%
  against 1.6% at 40 dB, with none at all in 195 checks at 20 dB and 49 at
  50 dB. If a test wants a damaged packet, a bigger attenuator is not the
  lever. Treat a failed CRC with more care still: every CRC check in that
  sweep failed, and all of those records carried a valid HEC and a payload
  length that decoded exactly, which is what a payload dewhitened against an
  unsettled clock looks like rather than a damaged one.
- **A clear board is only a baseline if it was placed like one.** At `-e 0`
  an unattenuated board elsewhere on the bench recorded 39 packets where a
  20 dB board deliberately set about a meter from the peers recorded 190.
  Give a baseline board its own `rf:` with its distance, or do not read it as
  the best case.

## Firmware

| Device | Source | Version | Built with | Flashed with |
|---|---|---|---|---|
| Ubertooth One | the [fork's](https://github.com/SqueakyFloors/ubertooth) `firmware/bluetooth_rxtx` | the resolved commit under test | `arm-none-eabi-gcc` | `ubertooth-dfu` |
| Ubertooth One, alternate | the fork's `firmware/btbr` | the resolved commit under test | `arm-none-eabi-gcc` | `ubertooth-dfu` |
| nRF52840 DK | [`firmware/nrf52840dk`](../firmware/nrf52840dk/README.md) | `utdk-1.4.0`, and `utdk-1.4.0-chansel1` for the algorithm 1 image | nRF Connect SDK v3.4.1 and its toolchain bundle | `nrfutil device program` |
| ESP32 peer | [`firmware/esp32-classic-peer`](../firmware/esp32-classic-peer/README.md) | `utesp-1.6.0` | ESP-IDF v5.5.6 | `esptool`, through `idf.py flash` |

Each firmware README holds the full build and flash procedure, the pinned
toolchain versions, and the shell command reference. Images are built outside
the repository and never committed; nothing under `firmware/` is generated.

A device is reflashed when it reports a version other than the one the run
asks it for, which for a DK is one of the two in this table, so a change to
the DK or peer sources that changes an image has to move that version, here
and in the driver, or the boards keep the image they have. For the DK, CI builds both of the pull request's images and both images
of the commit it is against, and fails when either pair differs with
`UTDK_FW_VERSION` unchanged
([#210](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/210)).

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
the host tools' own — any other, newer as well as older, is refused there,
because `ubertooth-btle` may stop such a board rather than read it (README,
"Guard Ubertooths") — not the resolved revision. Boards that a firmware-writing test
(`Flash reliability`, `DFU images`, `Classic btbr console`) writes itself are
not flashed either.

### The alternate `btbr` image

One tool, `ubertooth-btbr`, needs a different firmware image, and nothing else
runs it: a board is set up for that image or for every other tool, and
switching means reflashing it. A firmware build makes the application image;
`--with-btbr` adds the alternate one, and a run whose selected tests say they
need it gets it without being asked. Such a run builds both images from the
same commit and records both in its manifest, as `test build` (the application
image) and `btbr build`. `Classic btbr console`
is the only test that writes the alternate one; it writes the application image
back in its cleanup, which runs after a failure or a cancellation too.

A board left on `btbr` runs none of the other tools, so where it reports the
run's own revision, preflight names the image it found rather than only saying
the identity did not match. (A board carrying any image at another revision is
turned down for the revision, which already says to flash it.)

```
Firmware version: UT_Linux5 runs the btbr image at git-b2cd6c68, not
bluetooth_rxtx, which the other tools need: flash it with this build
(the default when building)
```

`ubertooth-util -v`, `-V` and `-w`, and `ubertooth-dfu`, are the commands that
work on either image: the `btbr` firmware answers the version, firmware info,
serial and watchdog requests, which is what those read. Everything else needs
the application image. The watchdog monitor therefore keeps working across the
session, except that `btbr` records no mode, so the mode field of its
breadcrumb is empty.

### How the harness checks the running version

| Device | Check |
|---|---|
| Ubertooth One | `ubertooth-util -U <serial> -v` must report the resolved revision, and the image name in front of it must be the one wanted, read before the flash (a board already on it is not written) and again after, up to 3 reads while the board re-enumerates |
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
so each is recorded with where its sources come from and its SHA-256.

| Image | What it is | Version | Source | SHA-256 |
|---|---|---|---|---|
| `kgv.dfu` | the known-good Ubertooth firmware that `Flash reliability` alternates against the run's own build | `2020-12-R1` | upstream Ubertooth release `2020-12-R1` | `e3fcb486c6e0ce7cc991bf6439f6dbc0156ed1229c2b95ebfd02198f9675bb11` |
| `zephyr.hex` | the nRF52840 DK role firmware | `utdk-1.4.0` | this repository, `firmware/nrf52840dk` at that version ([the change that built this image](https://github.com/SqueakyFloors/ubertooth-test-harness/pull/242)) | `f98bb7b7a08eca4ab9999be6b00bface5e441e3de7592ebb7959ad64205f55c0` |
| `zephyr.hex` (algorithm 1) | the same roles on a controller without channel selection algorithm 2, built with `chan-sel-1.conf` and `chan-sel-1.overlay` | `utdk-1.4.0-chansel1` | the same sources and the same change | `9fac198b2bdb7a46cb63219070f220cbb82fd0507fc369bae79b69fe3451f21b` |
| `esp32-classic-peer.bin` | the ESP32 Classic peer firmware | `utesp-1.6.0` | this repository, `firmware/esp32-classic-peer` at that version ([the change that built this image](https://github.com/SqueakyFloors/ubertooth-test-harness/pull/316)) | `6d463614240b003acdb3a605b0ce778ad0853da24c15b087cd9d0ae67ad03749` |

The KGV is the one the repository pins: the plan carries its revision and its
hash ([`testplans/slow.yaml`](../testplans/slow.yaml),
[`testplans/mac.yaml`](../testplans/mac.yaml)), and the run supplies the file
with `--artifact "KGV=<path>"`. The harness checks every image's SHA-256
before the run starts and again before each write, so a bench whose copy
differs fails before it writes anything.

The two peer images are not pinned, because what the harness checks is the
version a board reports: a DK that reports another one is flashed, and
`nrfdk.py` refuses a DK image that does not hold the pinned version. Nothing
flashes an ESP32 peer, so a run fails instead when one reports a version
older than the test needs. The hashes above let a bench confirm it holds the
same image this repository's sources produce. Each was built as its firmware
README says; a reproducible build is not claimed, so compare the version
first and the hash second. The same sources do build the same peer image
twice, but the ESP32 image holds the line numbers its error checks report,
so even a comment added above one of them gives a different hash for the
same version.

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
| A line that lost bytes | nothing says so | a length and a CRC per line, with `frame on` (`utesp-1.6.0` and later) |
| Why the board restarted | `nrfutil device reset` is the only reset the harness causes | `utesp: boot reason <why>`, on every boot (`utesp-1.6.0` and later) |

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

#### A peer's bridge loses bytes, and the frame that catches it

Measured on the bench: on a host that is also capturing from an Ubertooth
over USB, which is what the peers are there for, a contiguous run of bytes
goes missing between a peer's UART and the reading process, and the newlines
either side of the hole arrive. So a line can turn up looking whole when it
is not — `OK` read as `K`, and a status line arriving as valid JSON with a
shorter channel map than the controller has. Four of about seventy pair
sessions over one evening hit it. There is no flow control to fall back on:
the bridge's RTS and CTS lines are wired to the module's EN and IO0 for the
auto-program circuit.

`frame on` (`utesp-1.6.0` and later) answers it. Every line the firmware's
own writer sends then carries the payload's length and its
CRC-16/CCITT-FALSE, `#<len>,<crc>:<payload>`, so a reader knows a line that
lost bytes anywhere in it. Three kinds of line never carry a frame, because
they do not come from that writer: the ROM's boot log, ESP-IDF's `W (` and
`E (` lines, and whatever a panic prints on its way down.
[`esp32peer.py`](../ubertooth_harness/esp32peer.py) asks for it as soon as it
has read the boot notice, sends a read or an idempotent setter again when a
line fails the check, reports what the bridge cost the session, and drives an
older image exactly as before
([#310](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/310)).

#### A peer that restarts, and the evidence for it

A peer restarted under a capture on the bench and the driver reported it as a
pair that never streamed: a board back at boot has every counter at zero, and
nothing was reading the bridge while the capture ran, so what the board said
on its way down was thrown away. The driver now reads both bridges while the
command under test runs, keeps the boot evidence, and fails the case saying a
peer restarted. `utesp: boot reason <why>` from `esp_reset_reason()` names a
panic, a watchdog, a brownout, and a software reset apart; the ROM's own
`rst:0x...` line, which arrives whatever the firmware does, is reported
beside it
([#311](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/311)).

Two things the bench then ruled out as the cause, which is why the evidence
above was worth having rather than a guess. The controller's baseband link
budget is not it: 113 second links paged to the initiator from the host's
adapter while it held its own link, with a clock read on each, across both
the current image and the one before it, produced no restart. Nor is the
follow path on its own, measured separately in 13 runs. What it was is
[a second process taking turns with the port](#esp32-wroom-32e-classic-peer):
the first open of a port nobody holds resets the board, and so does the
pulse that releases a peer at the end of a session
([#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317),
closed by [#328](https://github.com/SqueakyFloors/ubertooth-test-harness/pull/328)).
So the reason line a restart now carries should read `power on` for that
case, and a panic or a watchdog for a board that really fell over.

### Reset behavior

**Opening an ESP32 bridge resets the board.** The CP2102N's DTR and RTS lines
are wired to the module's EN and IO0 pins, the auto-program circuit that lets
`esptool` flash with no button press and no jumper to move; the first open of
an idle port reboots the board. (A second open, of a port a live session is
already holding, does not: see "What a second session can do to a peer"
above.) So one invocation is one session: a driver
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
A device lease left behind by a session that is gone (a dropped connection, a
batch kill) is taken over by the next command that wants the device, which
says so and what is left of that command's process group, so a leftover
command still on the device can be found; a run also records the takeover in
its run record and report, because it does not promise the device was idle.
The host lock is never taken over,
because a firmware write may still be running; its error says what to look
for. Commands waiting for a device are served in the order they arrived, so a
run that wants several devices is not left behind by short leases on one of
them.
A device permanently wired to another one -- a board and its debug programmer
-- is held together with it, whichever of the two a command names (see [A
debug programmer and the board it programs](#a-debug-programmer-and-the-board-it-programs)).
Where the bench's own scripts lock devices with their own per-device files, a
lease takes those too (`--external-board-lock`), under the plain serial those
scripts were written against, for a run and for a manual command alike
([#317](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/317)).
That matters most on the ESP32 peers, whose bridge has RTS and DTR wired to
the chip's reset and boot pins. A second session restarts the board under the
first one's capture by pulsing those lines, which is what releasing a peer to
its boot state does, or by being the first to open a port nobody held.
Measured for #317: an overlapping open of an already-open port leaves the
board running, while a reset pulse from a second process restarts it and the
board then reports `boot reason power on`. See
[Sharing boards: leases](../README.md#sharing-boards-leases).

**One lock file is one device.** No two devices may be registered with one
serial, whatever their types and whichever way each is written: the bench's
own lock file for a device is named after its plain serial, so a lock of one
would be read as a lock of the other. Nor may two resources take one lease
file, which is a danger a duplicate identity does not cover, since a lease
key is built from one identity field (a DK registered `000123456789` and one
registered `123456789` are two identities and one key). A registry that does
either is refused when it is loaded, naming both devices. `host-power` and
`takeover` are names the lease directory keeps for itself, and no device may
be registered with one.

For a command you run by hand, [`scripts/lab/hold`](../scripts/lab/README.md)
takes the leases and names devices the way a bench note does, by serial or by
an unambiguous prefix of one.

**One checkout reads the registry and takes the leases.** A bench helper runs
the harness of the checkout it lives in: it re-runs itself under that
checkout's bootstrapped interpreter, and the `ubertooth-harness lease` it
delegates to is given that checkout first on its import path with the working
directory left off it. So a wrapper on the
bench names its registry, its own lock files if it has them, and a path to
the helper -- nothing about which harness runs -- and the registry and the
code that reads it cannot come from two different versions
([`scripts/lab/README.md`](../scripts/lab/README.md) has the wrapper). They did once: a wrapper naming a
deployed checkout's interpreter left every device command on the host
refusing a registry that had just gained a role-named default, because the
deployed half predated them
(harness [#312](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/312)).
Point the bench's wrappers at a checkout that tracks `main`, the one the
harness is deployed from, and `scripts/deploy.sh` reads the registry with the
code it installs so a pair that cannot work together is refused at the deploy
rather than at the next hold.

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
exists on Linux only, with the `hciconfig` and `hcitool` commands, which the
two tests that bind a `Bluetooth adapter` read the controller's own state
with (some distributions ship them in a separate package of deprecated
tools); `tshark` from the distribution (no GUI) for
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

Three protocol facts that look like harness or tool faults and are not:

- **The LAP on the air is the initiator's**, because it pages and the piconet
  takes its address and clock from it. `-l <the acceptor's LAP>` sees nothing
  during a link the acceptor did not initiate.
- **A survey's UAP is a guess** and is often wrong when few packets have been
  heard. Take the UAP from the board's `status` or the registry and pass it
  with `-u`, rather than trusting a short survey.
- **An audio link's packets carry their type in the header, which needs no
  clock.** `ubertooth-rx` has not been seen to settle on this pair's clock from
  a voice link, so a follow-mode capture of one is not something to count on
  yet; the packet type, the timing, and the count are read from the records a
  capture writes without it.

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
