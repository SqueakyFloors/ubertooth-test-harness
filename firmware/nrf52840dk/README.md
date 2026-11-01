# nRF52840 DK role firmware

Firmware for the Nordic nRF52840 DK (PCA10056) boards the harness uses as
Bluetooth LE test devices. The Ubertooth stays receive-only; these boards do
the transmitting.

One image holds three roles, and one command switches between them:

| Role | What it does | Test |
|---|---|---|
| `advertiser` (the boot role) | legacy advertising from a fixed address on chosen channels | `Capture known BLE advertiser` ([#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86)) |
| `central` | connects to the peripheral on the 1M PHY at a set interval, raises the data length to 251 bytes, sends a counter payload, asks for a connection parameter update | `Capture known BLE connection` ([#87](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/87)) |
| `peripheral` | advertises connectable and accepts the central's connection | the same |

`role <name>` leaves the previous role idle with its boot settings and takes
the new one, so a board can be handed from one test to another without
reflashing. The boot role is the advertiser, so a board that was reset or
power-cycled is in a known state.

Firmware version: `utdk-1.3.0` (`UTDK_FW_VERSION` in `src/roles.h`; the
harness pins the same string in `ubertooth_harness/nrfdk.py` and flashes a
board that reports anything else).

## Versions

| Component | Version |
|---|---|
| nRF Connect SDK | nRF Connect SDK v3.4.1 (`NCS_VERSION`), installed with `nrfutil sdk-manager` 1.16.1 |
| Toolchain | the nRF Connect SDK v3.4.1 toolchain bundle (Zephyr SDK GCC for Arm) from `nrfutil sdk-manager` |
| Board target | `nrf52840dk/nrf52840` |
| Flashing | nrfutil 8.2.1 with `nrfutil device` 2.23.0, SEGGER J-Link 9.88 |

The harness checks the nrfutil and `nrfutil device` versions before it
flashes (`nrfdk.py`), and refuses an image that does not contain the pinned
firmware version string.

## Build

Install the SDK and its toolchain once (about 6 GB):

```sh
nrfutil install sdk-manager
nrfutil sdk-manager install v3.4.1 --install-dir ~/ncs
```

Then build from the SDK workspace, pointing `-s` at this directory:

```sh
cd ~/ncs/v3.4.1
nrfutil sdk-manager toolchain launch --ncs-version v3.4.1 --install-dir ~/ncs -- \
  west build --no-sysbuild -b nrf52840dk/nrf52840 -d /tmp/utdk-build \
  -s /path/to/ubertooth-test-harness/firmware/nrf52840dk
```

The image is `/tmp/utdk-build/zephyr/zephyr.hex` (about 130 KB of flash,
27 KB of RAM). Builds go outside the repository; nothing under `firmware/`
is generated.

### Without installing the SDK

`scripts/ci.sh firmware-dk` builds the same image in Nordic's toolchain
image for the version `NCS_VERSION` pins, so nothing but docker is needed:

```sh
scripts/ci.sh firmware-dk                 # or scripts/local-ci.sh firmware-dk
NCS_WORKSPACE=~/ncs-ci scripts/ci.sh firmware-dk   # keep the SDK workspace
DK_BASE_REF=origin/main scripts/ci.sh firmware-dk  # compare with main's image
```

`DK_BASE_REF` needs this repository's history, so it works with
`scripts/ci.sh` and not under `scripts/local-ci.sh`, which tests a tree
exported with `git archive`; there, give `DK_BASE_SRC` a directory of the
sources to compare against instead.

It creates the SDK workspace (`west init --mr $NCS_VERSION`, `west update`),
builds the image, and checks that it carries the firmware version string
(`nrfdk.py check-image`), which is what `nrfdk.py ensure` looks for before it
flashes a board. A kept workspace records the version it was made for and is
refused when `NCS_VERSION` moves on, since `west init` is what pins the
manifest: remove it, or point `NCS_WORKSPACE` somewhere else. The repository is mounted read-only, so a build writes
nothing into the working tree. The `Firmware` workflow runs it on a change
under this directory, once a week, and on request
([#147](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/147));
it is not a required check, because the image and the workspace are gigabytes.
The image it builds is not published: a board is flashed from a local build.

With `DK_BASE_REF` (a git ref, which the workflow sets to the commit a pull
request is against) or `DK_BASE_SRC` (a directory of these sources) it builds
those sources too and compares the two images, which is how the workflow
holds a pull request to the rule that a change to the image bumps
`UTDK_FW_VERSION`
([#210](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/210)):
without a bump, `nrfdk.py ensure` leaves the old image on every board that
already reports this version. A change that leaves the image identical, such
as a comment, needs no bump and the step passes. Both builds compile a copy
of the sources at one path in the SDK workspace, so a difference in the image
is a difference in the sources; two builds of one tree gave byte-identical
images when this was measured in CI on 2026-10-31. When the images do differ,
each side is built once more, and a difference there is reported as a build
that does not repeat itself rather than as a missing bump.

Both builds use the SDK and the toolchain image this directory pins, so a
comparison cannot see what moving `NCS_VERSION` did to the image. A new
toolchain builds a new image, so that needs the version bump as a source
change does, and the step says so rather than comparing.

## Flash

```sh
nrfutil device program --serial-number <J-Link serial> \
  --firmware /tmp/utdk-build/zephyr/zephyr.hex \
  --options chip_erase_mode=ERASE_ALL,verify=VERIFY_READ,reset=RESET_SYSTEM
```

The harness does the same when a run needs it: pass the image to
`run-suite` as `--artifact "nRF52840 advertiser=/tmp/utdk-build/zephyr/zephyr.hex"`.
Its first case flashes the DK only when `status` reports another firmware
version (or nothing), then checks the version and the address again.

## Shell protocol

The shell is on uart0, which is the J-Link's first virtual COM port (VCOM0;
`/dev/serial/by-id/usb-SEGGER_J-Link_<12-digit serial>-if00` on Linux),
115200 baud, 8N1, no flow control. It prints no prompt, no echo and no
colors. Send one command line ended by CR; the board answers with zero or
more data lines, then exactly one line `OK` or `ERR <reason>`. Any other line
(the boot banner `*** Booting nRF Connect SDK ...`, `utdk: utdk-1.3.0 ready`)
is not a response.

| Command | Answer |
|---|---|
| `status` | one JSON line, then `OK` |
| `version` | the firmware version, then `OK` |
| `role <advertiser\|central\|peripheral>` | leave both roles idle, then take this one |
| `adv start` | start advertising with the current settings |
| `adv stop` | stop advertising |
| `adv chmap <37\|38\|39\|all>` | advertising channels |
| `adv interval <ms>` | advertising interval, 20-10240 ms (default 100) |
| `adv payload <hex\|none>` | payload tag, up to 12 bytes (default none) |
| `adv type <nonconn\|conn>` | `ADV_NONCONN_IND` (default) or `ADV_IND` |
| `adv defaults` | stop, and restore every boot setting |
| `conn peer <address>` | the peripheral the central connects to |
| `conn interval <ms>` | connection interval, 8-4000 ms (default 30) |
| `conn chmap <37\|38\|39\|all>` | the peripheral's advertising channels (default 37) |
| `conn datamap <all\|0,12,36>` | central only: change the live connection's data channel map (two channels or more) |
| `conn dle <on\|off>` | ask for a 251-byte data length (default on) |
| `conn payload <bytes>` | bytes per write, 1-244 (default 244) |
| `conn period <ms>` | time between writes, 10-10000 ms (default 100) |
| `conn cycle <ms\|off>` | drop the link after this long and make a new one (default off) |
| `conn autoupdate <ms\|off>` | interval to ask for once connected (default off) |
| `conn start` | central: connect; peripheral: advertise connectable |
| `conn stop` | disconnect and stop |
| `conn update <ms>` | ask for a connection parameter update now |
| `conn defaults` | stop, and restore every boot setting |

A setting changed while advertising restarts advertising with it. A `conn`
setting may only change while the connection role is stopped, so the
parameters a capture shows are the ones that were asked for; `conn update`
and `conn datamap` are the two that act on the link while it is up, and both
need one. Bad values
answer `ERR` and change nothing. `adv` or `conn` alone, or with an unknown
subcommand, answers `ERR`; a `conn` command while the role is the advertiser
answers `ERR` and says which `role` command to run. A command with the wrong
number of words, or an unknown top-level command, gets Zephyr's own
`<command>: wrong parameter count` or `<command>: command not found` line
instead; `nrfdk.py` treats both as a refusal.

`status` reports every role's fields on one line, whichever role is running,
for example a central with a live connection:

```json
{"role":"central","fw":"utdk-1.3.0","bt":true,"address":"C0:7D:CD:95:52:86","address_type":"random static","adv":false,"running":false,"type":"nonconn","chmap":"all","interval_ms":100,"payload":"","counter":0,"starts":0,"connections":0,"last_err":0,"conn_on":true,"connected":true,"peer":"C1:92:EC:E7:71:BC","conn_interval_ms":30,"update_ms":60,"dle":true,"interval_units":48,"latency":0,"timeout_units":400,"tx_octets":251,"rx_octets":251,"mtu":247,"phy":"1M","payload_len":244,"period_ms":100,"conn_chmap":"37","data_map":"ffffffff1f","data_map_used":37,"data_map_sets":0,"cycle_ms":4000,"autoupdate_ms":60,"writes":74,"write_errors":4,"received":0,"updates":3,"connects":3,"disconnects":2,"conn_err":0}
```

- `address`: the identity address, a static random address derived from
  the chip's FICR, so it is the same on every boot of the same board.
  Register it as the DK's `BLE address`.
- `adv`: advertising was asked for; `running`: the controller is
  advertising now (false for a moment after a connection to `ADV_IND`).
  Both describe the advertiser role only; the peripheral role's connectable
  advertising is reported as `conn_on`.
- `counter`: the payload counter (below); `starts`: advertising starts
  since boot; `connections`: connections accepted to `ADV_IND` (each is
  dropped at once and advertising starts again); `last_err`: the last
  Bluetooth API error (0 is none).
- `conn_on`: the connection role was started; `connected`: the link is up
  now. `interval_units`, `latency` and `timeout_units` are the connection
  parameters in use, in the units the air interface uses (1.25 ms and
  10 ms), so a capture can be compared with them directly;
  `conn_interval_ms` is what the central asks for in the CONNECT_IND and
  `update_ms` the last update it asked for.
- `tx_octets` and `rx_octets`: the negotiated data length, 251 with the
  extension and 27 without; `mtu`: the ATT MTU after the exchange; `phy`:
  `1M`, the only PHY built in.
- `data_map`: the channel classification this board last gave its
  controller, as ten hex digits in the order a CONNECT_IND and an
  `LL_CHANNEL_MAP_IND` carry it (bit b of byte i is channel 8i + b), so a
  capture's map lines up with it field for field. It is what the central
  asked for, not a map read back from the link: the controller may drop
  channels of its own as well, so a recovered map can be narrower.
  `data_map_used`: how many of the 37 channels the classification leaves in
  use; `data_map_sets`: how many `conn datamap` requests the controller has
  taken since boot, which is how the driver tells a real change from an
  echo. Putting the channels back on leaving the role does not count.
- `writes` and `write_errors` count the central's writes (a write can be
  refused when buffers are full as a link is coming down); `received` is
  the bytes the peripheral's characteristic got; `updates`, `connects`
  and `disconnects` count parameter updates and links.

The board does not count advertising events: the counter steps once a
second while advertising, so a capture's counters show which second of the
run each packet came from.

## The connection pair

The central connects straight to the peripheral's identity address, so
nothing scans and the CONNECT_IND carries exactly the parameters asked for.
Once the link is up the central raises the data length to 251 bytes,
exchanges the ATT MTU, finds the peripheral's one characteristic by UUID, and
writes `conn payload` bytes to it every `conn period` ms with Write Without
Response, which puts the whole payload in one link-layer packet. Only the 1M
PHY is built in, so a connection can never move off it.

The central connects on the channel it hears the peripheral on, so the
peripheral advertises on one channel (`conn chmap`, 37 by default): that is
what puts the CONNECT_IND on the channel a receiver is listening to. With all
three channels the connection still works, but a single-channel receiver sees
the CONNECT_IND only when it happens to land on its channel.

### Changing the channel map of a live connection

`conn datamap <all|0,12,36>` narrows the data channels the connection may use
while it is up. The central hands the controller a channel classification,
and the controller carries it to every link where it is the central as an
`LL_CHANNEL_MAP_IND`, so the connection moves to the new map at a connection
event both ends agree on and a sniffer following it has to follow the change.
That is the one thing the roles could not do before: `conn chmap` is the
peripheral's *advertising* channels, and the map in the CONNECT_IND used to
hold for the life of the link.

A channel map has to leave at least two channels used: the Bluetooth Core
specification puts the minimum number of used channels at 2 (Vol 6 Part B,
4.5.8.1), and the controller here refuses a one-channel map (`-EINVAL`,
measured on the bench), so the firmware refuses one first and says why. Two channels is therefore the most
demanding map a test can ask for, and the smallest divisor the remapping can
be given. A channel above 36 is refused as well.

Two classifications have to be at least a second apart (Bluetooth Core, HCI
LE Set Host Channel Classification), so a second `conn datamap` sooner than
that answers `ERR` with how long ago the last one was, rather than being
dropped by the controller without a word. The second runs from every command
that reached the controller, including one it refused. The classification belongs to the
controller and outlives the link that carried it, so leaving the role (`conn
defaults`, `role <name>`, `nrfdk.py idle`) puts all 37 channels back, waiting
out that second if it has to; otherwise the next test's CONNECT_IND would
carry the narrowed map.

With `conn cycle <ms>` the central drops the link after that long and
connects again a moment later, and the peripheral advertises again by itself.
A capture started at any moment then holds a whole connection: the
CONNECT_IND, the data length exchange, the parameter update
(`conn autoupdate`) and the data. That is what lets the harness run one
command at a time and still catch a connection being made.

## Advertising payload

Legacy advertising on the 1M PHY, from the identity address (TxAdd set).
AdvData is two AD structures:

| AD | Bytes |
|---|---|
| Flags | `02 01 04` (`ADV_NONCONN_IND`) or `02 01 06` (`ADV_IND`) |
| Manufacturer specific | length, `ff`, then the fields below |

| Field | Size | Value |
|---|---|---|
| Company ID | 2 | `0xffff` (little-endian `ff ff`, the ID reserved for tests) |
| Magic | 4 | `UTAD` |
| Format | 1 | `1` |
| Channel map | 1 | bit 0 = 37, bit 1 = 38, bit 2 = 39 |
| Counter | 4 | little-endian, steps once a second while advertising |
| Tag | 0-12 | `adv payload` |

`ubertooth_harness/advcapture.py` decodes it from a capture.

## Extending

A new role adds its own source file with a `<role>_status()` and a
`<role>_leave()`, a command group of its own, and a name in `enum utdk_role`
(`src/roles.h`). `main.c` dispatches `role` and `status`; the protocol stays
line-based with the same `OK`/`ERR` endings, and `nrfdk.py` keeps one shell
driver for every role. A role that keeps a connection must check
`utdk_role()` in the shared Bluetooth callbacks, as `advertiser.c` and
`connection.c` do, because those callbacks are global.

## License

The sources in this directory are under the Apache License 2.0
(`SPDX-License-Identifier: Apache-2.0` in each file), the license of the
Zephyr samples they follow, not the GPLv2 of the rest of the repository. A
built image also links Nordic's SoftDevice Controller and MPSL from the nRF
Connect SDK, which are under the LicenseRef-Nordic-5-Clause license (use only
with Nordic hardware); images are built locally and not committed.
