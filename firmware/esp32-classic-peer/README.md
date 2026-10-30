# ESP32 Classic Bluetooth peer firmware

Firmware for the two ESP32-WROOM-32E dev boards the harness uses as Classic
Bluetooth (BR/EDR) test devices. The Ubertooth stays receive-only; these
boards do the transmitting, the same division of work as the
[nRF52840 DK role firmware](../nrf52840dk/README.md) for Bluetooth LE.

Two of these boards hold a Serial Port Profile link and push data across it,
so a test has BR/EDR traffic on the air from a known address, on demand, with
no third-party device involved. That is what `ubertooth-rx`,
`ubertooth-rx -l`/`-u`, `ubertooth-follow`, and `ubertooth-btbr` need to be
tested against something known.

One image holds both roles, and one command switches between them:

| Role | What it does |
|---|---|
| `acceptor` (the boot role) | discoverable and connectable, with an SPP server listening |
| `initiator` | looks the peer's SPP channel up over SDP, connects, and streams a payload |

A board that was reset or power-cycled is an acceptor with its server
listening, so the pair needs setting up on one side only. `role acceptor`
puts the server back, and `role initiator` takes it away, so the role name
always describes what the board is doing.

The channel is opened with no security (`ESP_SPP_SEC_NONE`), so a link needs
no pairing and a capture holds only the SDP lookup, the connection, and the
data.

Firmware version: `utesp-1.0.0` (`UTESP_FW_VERSION` in `main/peer.h`).

## Versions

| Component | Version |
|---|---|
| ESP-IDF | v5.5.6 (`IDF_VERSION`) |
| Target | `esp32` |
| Board | ESP32-WROOM-32E, 4 MB flash, ESP32-D0WD-V3 |
| Bridge | CP2102N USB to UART, 115200 8N1 |
| Flashing | `esptool` 4.12.0, which comes with ESP-IDF |

## Build

Install ESP-IDF once (about 3 GB), following
[Espressif's Linux and macOS setup guide](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/get-started/linux-macos-setup.html):

```sh
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.6 --depth 1 --shallow-submodules --recursive \
  https://github.com/espressif/esp-idf.git esp-idf
cd esp-idf && ./install.sh esp32
```

Then build outside the repository, as the DK firmware does:

```sh
. ~/esp/esp-idf/export.sh
cd /path/to/ubertooth-test-harness/firmware/esp32-classic-peer
idf.py set-target esp32
idf.py -B /tmp/utesp-build build
```

The image is `/tmp/utesp-build/esp32-classic-peer.bin`, about 670 KB, which
needs the large single-app partition table `sdkconfig.defaults` selects.
`idf.py -B` keeps every build product in the build directory. The one file
`set-target` does write here is `sdkconfig`, the resolved configuration; it
is generated from `sdkconfig.defaults` and git-ignored, so edit the defaults
file, not it.

## Flash

```sh
idf.py -B /tmp/utesp-build -p /dev/ttyUSB0 -b 460800 flash
```

The bridge's reset wiring means `esptool` needs no button press, and no
jumper has to move. Reflashing takes about 20 seconds per board.

## Shell protocol

The shell is on uart0, which is the board's CP2102N bridge
(`/dev/serial/by-id/usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_<serial>-if00-port0`
on Linux), 115200 baud, 8N1, no flow control. It prints no prompt, no echo
and no colors. Send one command line ended by LF or CR; the board answers
with zero or more data lines, then exactly one line `OK` or `ERR <reason>`.

Lines that start with `utesp: ` are notices the board sends by itself and are
not part of any answer, so a driver skips them:

| Notice | When |
|---|---|
| `utesp: utesp-1.0.0 ready` | after boot, once Bluetooth is up |
| `utesp: utesp-1.0.0 start failed: <reason>` | after boot, if it is not |
| `utesp: spp connected <address>` | a link came up, either role |
| `utesp: spp disconnected` | the link went down |

ESP-IDF's own log lines start with `W (` or `E (` and are warnings or errors;
`sdkconfig.defaults` keeps the log at that level so nothing else appears.

| Command | Answer |
|---|---|
| `status` | one JSON line, then `OK` |
| `version` | the firmware version, then `OK` |
| `help` | one line per command, then `OK` |
| `role <acceptor\|initiator>` | stop, then take this role |
| `disc <on\|off>` | discoverable and connectable, or neither |
| `spp peer <aa:bb:cc:dd:ee:ff>` | the address the initiator calls |
| `spp start` | acceptor: listen; initiator: look up and connect |
| `spp stop` | stop streaming, drop the link, stop listening |
| `stream <on\|off>` | send the payload while the link is up |
| `stream period <ms>` | time between writes, 5 to 10000 (default 100) |
| `stream payload <bytes>` | bytes per write, 8 to 512 (default 100) |
| `inquiry <seconds>` | one general inquiry, 2 to 60 seconds |
| `txpower <min> <max>` | transmit power in dBm, from -12, -9, -6, -3, 0, 3, 6, 9 |
| `defaults` | restore every boot setting and zero the counters |
| `reset` | answer `OK`, then restart the board |

`spp start` on the initiator and `inquiry` wait for the controller and answer
when it is done, so a test runs one command at a time and never polls.
`spp start` answers `ERR connect timed out` after 15 seconds if the peer is
not there, and `ERR connect refused` if it is but says no.

A bad value answers `ERR` and changes nothing. A command with the wrong
number of words, or an unknown one, answers `ERR` too; `help` is the only
hint, as there is no usage text.

The Bluetooth device name is not settable, and does not need to be: it holds
the board's own LAP, so it is fixed for the life of the board and different
from the other one with no setup step at all, `UT-ESP32-<LAP>`. Register it
with the board.

`stream on` needs a live link and answers `ERR no connection to stream on`
without one, so a test cannot arm it in advance. Streaming stops by itself
when the link goes down, and `spp stop` and either `role` command stop it
too. On the initiator, `spp start` counts only a link to the address `spp
peer` named as success, so an incoming link from somewhere else cannot be
mistaken for its own.

`status` reports every field on one line, whichever role is running:

```json
{"role":"initiator","fw":"utesp-1.0.0","bt":true,"address":"20:50:0D:E9:C5:BA","lap":"e9c5ba","uap":"0d","nap":"2050","name":"UT-ESP32-E9C5BA","discoverable":true,"spp_server":false,"connecting":false,"connected":true,"peer":"C8:85:41:2A:1B:2A","remote":"C8:85:41:2A:1B:2A","stream":true,"period_ms":20,"payload_len":200,"counter":1483,"writes":1483,"write_errors":0,"rx_bytes":0,"connects":1,"disconnects":0,"tx_power_dbm":[0,3],"last_err":0}
```

- `address`: the BD_ADDR, split out as `lap`, `uap` and `nap` as well,
  because those are what `ubertooth-rx -l`, `-u` and `ubertooth-follow`
  take. It is the board's factory MAC plus two in the last byte, so it is
  the same on every boot of the same board. Register it as the ESP32's
  `BD_ADDR`.
- `peer`: what `spp peer` set, empty until it is set; `remote`: the far end
  of the live link, empty when there is none.
- `counter`: the counter in the payload (below), zeroed when a link comes
  up; `writes` counts the writes the Bluetooth stack accepted and
  `write_errors` those it refused, and `rx_bytes` is the bytes this board
  received. A write accepted at the moment a status line is read may not
  have reached the air yet, which is why the other side's `rx_bytes` can be
  one payload behind `writes`.
- `tx_power_dbm`: the range the controller reports, not what was asked for.
- `last_err`: the last Bluetooth API error, 0 for none.

## The pair

The initiator asks the acceptor over SDP which channel its SPP server is on,
then calls that channel. So a capture of one `spp start` holds the whole
exchange: the page, the connection, the SDP lookup on L2CAP, and the RFCOMM
session coming up.

With `stream on` the initiator writes `stream payload` bytes every
`stream period` ms. The first eight bytes are `UTESP` and a little-endian
24-bit counter, which steps once per write, so a capture can be matched to
the run and to the moment inside it. The rest is `U` filler. A write while
the link is congested is skipped rather than queued, so `writes` counts what
actually went out.

Both boards are discoverable and connectable from boot, which is what makes
the pair findable by `inquiry` from either side and by `ubertooth-rx`. Turn
that off with `disc off` for a test that needs a silent board.

`bt` is false only if Bluetooth failed to come up; the shell still answers
then, with a zero address, and the boot notice says why.

## Extending

A new role adds its name to `enum peer_role` (`main/peer.h`) and its
behavior to `bt_peer.c`; `main.c` holds only the shell. Keep every new
command line-based with the same `OK`/`ERR` endings, so one harness driver
serves every role. A command that waits for the controller should block and
answer when it is done, as `spp start` and `inquiry` do, rather than leave a
test polling `status`.

## License

The sources in this directory are under the Apache License 2.0
(`SPDX-License-Identifier: Apache-2.0` in each file), the license of the
ESP-IDF examples they follow, not the GPLv2 of the rest of the repository. A
built image also links ESP-IDF, which is Apache License 2.0, and Espressif's
Bluetooth controller libraries, which are distributed under the ESP-IDF
license for use with Espressif hardware; images are built locally and not
committed.
