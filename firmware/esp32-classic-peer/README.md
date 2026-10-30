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

They also open a synchronous (SCO or eSCO) audio link to each other, which
puts reserved-slot voice packets on the air next to the data packets. See
[The audio link](#the-audio-link).

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

Firmware version: `utesp-1.1.0` (`UTESP_FW_VERSION` in `main/peer.h`).

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
| `utesp: utesp-1.1.0 ready` | after boot, once Bluetooth is up |
| `utesp: utesp-1.1.0 start failed: <reason>` | after boot, if it is not |
| `utesp: spp connected <address>` | a link came up, either role |
| `utesp: spp disconnected` | the link went down |
| `utesp: sco connected` | the synchronous audio link came up |
| `utesp: sco disconnected` | it went down |
| `utesp: audio link unavailable: <reason>` | after boot, if only the audio half failed to set up; SPP still works and `sco start` answers `ERR` |

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
| `sco start` | acceptor: be ready; initiator: open the audio link |
| `sco stop` | drop the audio link and stop the profile |
| `sco stats` | one JSON line of the controller's packet counters |
| `defaults` | restore every boot setting and zero the counters |
| `reset` | answer `OK`, then restart the board |

Every command that starts something waits for the controller's event and
answers from it, so a shell answer is never ahead of the radio and a test
never polls. `spp start` on the initiator answers `ERR connect timed out`
after 15 seconds if the peer is not there, and `ERR connect refused` if it is
but says no; on the acceptor it answers only once the server is listening. `spp stop` waits for the link
to close and the server to stop, and answers `ERR the link did not close` or
`ERR the server did not stop` rather than claim a stop that did not happen.
The boot notice comes after the server is listening, so a board that has said
`ready` is ready.

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
{"role":"initiator","fw":"utesp-1.0.0","bt":true,"address":"20:50:0D:E9:C5:BA","lap":"e9c5ba","uap":"0d","nap":"2050","name":"UT-ESP32-E9C5BA","discoverable":true,"spp_server":false,"connecting":false,"connected":true,"peer":"C8:85:41:2A:1B:2A","remote":"C8:85:41:2A:1B:2A","stream":true,"period_ms":20,"payload_len":200,"counter":1483,"writes":1483,"write_errors":0,"rx_bytes":0,"connects":1,"disconnects":0,"last_err":0}
```

- `address`: the BD_ADDR, split out as `lap`, `uap` and `nap` as well,
  because those are what `ubertooth-rx -l`, `-u` and `ubertooth-follow`
  take. It is the board's factory MAC plus two in the last byte, so it is
  the same on every boot of the same board. Register it as the ESP32's
  `BD_ADDR`.
- `peer`: what `spp peer` set, empty until it is set; `remote`: the far end
  of the live link, empty when there is none.
- `counter`: the counter in the payload (below), zeroed when a link comes
  up, and steps with `writes`; `writes` and `write_errors` are counted from
  the stack's own report of each write, not from the call that queued it, so
  a refused write is counted as one. `rx_bytes` is the bytes this board
  received. A write still in flight when a status line is read is why the
  other side's `rx_bytes` can be one payload behind `writes`.
- `last_err`: the last Bluetooth API error, 0 for none.

## The pair

The initiator asks the acceptor over SDP which channel its SPP server is on,
then calls that channel. So a capture of one `spp start` holds the whole
exchange: the page, the connection, the SDP lookup on L2CAP, and the RFCOMM
session coming up.

With `stream on` the initiator writes `stream payload` bytes every
`stream period` ms. The period is a floor: one write is with the stack at a
time, so a link too slow for the period sends fewer, wider-spaced writes
instead of reporting writes that never went out. The first eight bytes are `UTESP` and a little-endian
24-bit counter, which steps once per write, so a capture can be matched to
the run and to the moment inside it. The rest is `U` filler. A write while the link is congested, or while the
previous one is still with the stack, is skipped rather than queued.

Both boards are discoverable and connectable from boot, which is what lets
the initiator find the acceptor's channel and what puts the pair on the air
for `ubertooth-rx`. Turn it off with `disc off` for a test that needs a
silent board.

`bt` is false only if Bluetooth failed to come up; the shell still answers
then, with a zero address, and the boot notice says why.

## The audio link

A synchronous link (SCO or eSCO) carries voice in reserved slots rather than
in the polled slots ACL data uses, so it looks different on the air and a
decoder has to handle it separately. `sco start` puts one on the air between
the two boards.

ESP-IDF gives an application synchronous links only through the Hands-Free
Profile, so that is how the link is opened: the initiator runs the audio
gateway and the acceptor runs the hands-free unit. The gateway pages, which
keeps the initiator the piconet's leader, the same way round as for SPP, so
the LAP on the air is still the initiator's.

ESP-IDF's own
[configuration help](https://docs.espressif.com/projects/esp-idf/en/v5.5.6/esp32/api-reference/bluetooth/esp_hf_ag.html)
says the two roles "can be included simultaneously but they cannot run
simultaneously", so the image holds both and only the one the role needs is
started. Nothing starts until `sco start`, which leaves a board that only
does SPP on the air exactly as it was before this was added.

| Step | Acceptor (hands-free unit) | Initiator (audio gateway) |
|---|---|---|
| `sco start` | start the profile and wait | start the profile, page the acceptor, open the audio link |
| while up | send and count frames | send and count frames |
| `sco stop` | drop the link and the service level connection, stop the profile | the same |

So a test runs `sco start` on the acceptor first, then `spp peer <acceptor
address>` and `sco start` on the initiator. `sco start` on the initiator
answers only once the controller has opened the link, and says what went
wrong otherwise: `ERR service level connect timed out`, `ERR the peer refused
the service level connection`, `ERR audio connect timed out`, or `ERR the
controller did not open a synchronous link`. `role` and `defaults` take the
audio link down, because it belongs to a role; `spp stop` does not, as the
two links are separate.

These commands block the shell while the controller works, so a driver's read
timeout has to allow for the worst case. Counting the tidying up a failure
does: about 32 seconds for `sco start` on the initiator (3 s to start the
profile, 15 s to page and bring the service level connection up, 10 s for the
audio link, then up to 3.5 s to put the profile back), about 10 seconds for
`sco stop` (3 s each for the link and the service level connection, 3.5 s for
the profile), and about 15 seconds for `role` and `defaults`, which stop the
audio link and then the SPP side. Allow 35 seconds and nothing will time out.
A link to a board that is awake takes four or five seconds in practice.

### The audio pattern

There is no microphone on these boards, so the firmware makes the audio. Each
frame is a little-endian 32-bit frame counter followed by a ramp
(`frame[i] = i`), which lets a capture tell one frame from the next and a gap
from a loss. The counter restarts at zero each time the link comes up.

The link clocks the sending. A synchronous link is symmetric and the
controller hands up one frame per interval, so each side answers each frame
it receives with one frame of its own: that matches the link's rate with no
timer here to drift against it. If no frame arrives for 50 ms the firmware
sends one anyway, which both starts the exchange when the link comes up and
keeps a live link from going silent if a frame is ever missed.

The frames are 16-bit linear samples, and the controller does the CVSD coding
(see the codec note below), so the bytes on the air are **not** the bytes the
firmware wrote. The far end therefore counts frames rather than comparing
bytes, and `sco_rx_bad` counts the frames the controller marked as damaged.

### What `status` reports

| Field | Meaning |
|---|---|
| `sco` | `off` (no profile), `ready` (profile started, no link), `slc` (service level connection up), `on` (audio link up) |
| `sco_profile` | `none`, `ag` (audio gateway), `hf` (hands-free unit) |
| `sco_codec` | `none`, `cvsd`, or `msbc`, as the two ends negotiated it |
| `sco_handle` | the controller's (e)SCO connection handle |
| `sco_frame_len` | the frame size the controller asked for, in bytes |
| `sco_tx_frames`, `sco_tx_errors` | frames the stack took, and frames it refused |
| `sco_rx_frames`, `sco_rx_bad` | frames received, and how many of those the controller marked damaged |

`sco stats` asks the controller for its own packet counters, which is the
number a capture can be checked against:

```json
{"rx_total":4057,"rx_correct":4037,"rx_err":0,"rx_none":20,"rx_lost":0,"tx_total":4059,"tx_discarded":0}
```

That is a measured 30-second link: 4059 packets each way, one every 7.4 ms,
which is the rate a 7.5 ms interval gives. The interval itself is not
reported, so that is an inference from the packet rate, not a reading (see
below). The frames are 120 bytes of 16-bit samples at that rate, which is
128 kbit/s handed to the controller; the controller codes it down to the
64 kbit/s a CVSD link carries on the air.

### The packet type is not selectable, and is not reported

A test would like to ask for a particular synchronous packet type (EV3, HV3,
2-EV3) and to read back the link type, packet type, interval, and
retransmission window the controller settled on. ESP-IDF offers neither, and
this firmware does not pretend otherwise. In ESP-IDF v5.5.6:

- **Choosing the parameters.** The eSCO parameters are a compile-time table
  inside the stack, `bta_ag_esco_params[]` in
  `components/bt/host/bluedroid/bta/hf_ag/bta_ag_sco.c`. Which row is used
  follows from the negotiated codec and from whether both ends set the eSCO
  S4 feature bit, and both ESP32s do. The one function that would override
  the row, `bta_ag_set_esco_param()` (declared in
  `bta/hf_ag/include/bta_ag_int.h`), has no caller anywhere in the tree, and
  no `esp_hf_*` or `esp_bt_gap_*` function takes synchronous link
  parameters. So with this image the stack asks for the CVSD S4 row: 2-EV3,
  12 ms maximum latency, retransmission effort "quality". What the controller
  actually opens is whatever is left after that row is masked against the
  packet types the controller supports.
- **Reading them back.** The stack does read them: `bta_ag_sco_conn_cback()`
  in the same file calls `BTM_ReadEScoLinkParms()` and stores the link type,
  transmit interval, retransmission window, and air mode in its own control
  block. Nothing carries them further; the only synchronous-link facts that
  reach an application are the audio state (which distinguishes a CVSD link
  from an mSBC one), the connection handle, the frame size, and the packet
  counters above, and `status` reports all four.

An Ubertooth capture is the way to see the packet type on the air, which is
the direction the fork's tests want anyway. The link is easy to find there:
`ubertooth-rx -l e9c5ba -u 0d -t 30` read 168 packets of the piconet while
an audio link was up, and 0 in the same 30 seconds with the boards idle.

The codec is a build-time choice for the same reason:
`CONFIG_BT_HFP_WBS_ENABLE` decides whether mSBC is offered at all, and this
image sets it to `n`, so the link is always CVSD and `sco_codec` always reads
`cvsd` while one is up. With wide-band speech the two boards would settle on
mSBC, whose air mode is transparent, and then the firmware would have to
produce real mSBC frames rather than samples. `sco_codec` still reports what
the audio state says rather than a constant, so a build that turns wide-band
speech on does not start lying.

## The piconet clock is not readable from this firmware

A test that checks the clock an Ubertooth recovers wants the piconet clock
from the board that leads the piconet, which here is the initiator. That
board's own native clock (CLKN) **is** the piconet clock, and the ESP32's controller will report it:
`HCI_Read_Clock` (opcode `0x1407`) with `Which_Clock` = 0 answers with a
status of success and a rising clock value. Measured on one of these boards
on 2026-10-30 with a throwaway firmware that runs the controller with no host
stack, so the application owns the HCI transport:

```
PROBE read_clock       opcode=0x1407: 04 0e 0c 05 07 14 00 00 00 3e 00 00 00 00 00
PROBE read_clock_again opcode=0x1407: 04 0e 0c 05 07 14 00 00 00 54 00 00 00 00 00
```

That is a Command Complete with status `00`, connection handle `0000`, and a
clock of `0x3e` then `0x54` in units of 312.5 microseconds.

A board doing SPP and HFP cannot send that command, so there is no `clock`
command here. Everything the application could use to send it is closed:

- Bluedroid never sends `HCI_Read_Clock`. The opcode is defined in
  `components/bt/host/bluedroid/stack/include/stack/hcidefs.h` and referenced
  nowhere else in the component, and no `esp_*` function returns a clock.
- `esp_vhci_host_register_callback()` takes one callback, and Bluedroid holds
  it while it is enabled. Sending a command behind its back would hand the
  answer to Bluedroid, which drops an event it has no pending command for.
- `esp_ble_gap_vendor_command_send()` is the one public way to send a chosen
  HCI opcode, and it cannot reach this one: it ends in
  `BTM_VendorSpecificCommand()`, which documents that the "opcode will be
  OR'd with HCI_GRP_VENDOR_SPECIFIC", and `btsnd_hcic_vendor_spec_cmd()` in
  `stack/hcic/hcicmds.c` does exactly that. `0x1407 | 0xFC00` is `0xFC07`, a
  vendor command, not Read Clock.
- `esp_bluedroid_attach_hci_driver()` looks like the seam for this: it lets
  an application supply Bluedroid's HCI transport. It is compiled out when
  the controller is in the same build, which on the ESP32 it always is:
  `hci_host_send_packet()` and `hci_host_register_callback()` in
  `host/bluedroid/api/esp_bluedroid_hci.c` call the VHCI functions directly
  under `#if (BT_CONTROLLER_INCLUDED == TRUE)` and ignore the registered
  operations.

A board that gave up Bluedroid could report the clock: driven over HCI, it
could join a piconet as the other side of a link and read the piconet clock
with `Which_Clock` = 1, or lead one and read its own. That is a
different role from this firmware and is not built here.

## Extending

A new role adds its name to `enum peer_role` (`main/peer.h`) and its
behavior to `bt_peer.c`; `main.c` holds only the shell. Keep every new
command line-based with the same `OK`/`ERR` endings, so one harness driver
serves every role. A command that waits for the controller should block on
its event and answer from it, as `spp start` and `spp stop` do, rather than
leave a test polling `status`. There is no `inquiry` and no transmit power
control: both addresses are fixed and registered, and no planned test sets
the power, so neither was worth the code.

## License

The sources in this directory are under the Apache License 2.0
(`SPDX-License-Identifier: Apache-2.0` in each file), the license of the
ESP-IDF examples they follow, not the GPLv2 of the rest of the repository. A
built image also links ESP-IDF, which is Apache License 2.0, and Espressif's
Bluetooth controller libraries, which are distributed under the ESP-IDF
license for use with Espressif hardware; images are built locally and not
committed.
