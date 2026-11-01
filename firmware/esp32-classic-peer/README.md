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

And each board reports its clocks. `clock` gives the board's own native
clock and the clock of the piconet it is in, which is the number an
Ubertooth's recovered clock can be checked against. See
[The clocks](#the-clocks).

The piconet can also be held to part of the band: `afh` asks the controller
to hop over a chosen channel map, so a test can check that an Ubertooth
following the piconet sees it only on those channels. See
[The channel map](#the-channel-map).

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

Firmware version: `utesp-1.3.0` (`UTESP_FW_VERSION` in `main/peer.h`).

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
| `utesp: utesp-1.3.0 ready` | after boot, once Bluetooth is up |
| `utesp: utesp-1.3.0 start failed: <reason>` | after boot, if it is not |
| `utesp: spp connected <address>` | a link came up, either role |
| `utesp: spp disconnected` | the link went down |
| `utesp: sco connected` | the synchronous audio link came up |
| `utesp: sco disconnected` | it went down |
| `utesp: audio link unavailable: <reason>` | after boot, if only the audio half failed to set up; SPP still works and `sco start` answers `ERR` |
| `utesp: sco frame size <n> unusable, sending no audio` | a link came up asking for a frame size this firmware cannot fill |
| `utesp: sco the profile is still running: <reason>` | a start failed and could not put the profile away either |
| `utesp: clock read unavailable` | after boot, if only the clock read failed to set up; everything else works and `clock` answers `ERR` |
| `utesp: channel map read unavailable` | after boot, if only the read of the link's own channel map failed to set up; everything else works and `status` says so in `afh_link_error` |

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
| `sco stats` | one JSON line of the controller's packet counters, then `OK` |
| `sco type <ev3\|2ev3\|default>` | the packet type the next audio link asks for |
| `clock` | one JSON line of the native and piconet clocks, then `OK` |
| `afh <20 hex digits>` | hop over this channel map |
| `afh off` | hop over every channel again |
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
{"role":"initiator","fw":"utesp-1.3.0","bt":true,"address":"20:50:0D:E9:C5:BA","lap":"e9c5ba","uap":"0d","nap":"2050","name":"UT-ESP32-E9C5BA","discoverable":true,"spp_server":false,"connecting":false,"connected":true,"peer":"C8:85:41:2A:1B:2A","remote":"C8:85:41:2A:1B:2A","stream":true,"period_ms":20,"payload_len":200,"counter":1483,"writes":1483,"write_errors":0,"rx_bytes":0,"connects":1,"disconnects":0,"last_err":0,"sco":"off","sco_profile":"none","sco_codec":"none","sco_handle":0,"sco_frame_len":0,"sco_tx_frames":0,"sco_tx_errors":0,"sco_rx_frames":0,"sco_rx_bad":0,"afh":"ffffffffff0000000000","afh_used":40,"afh_link":"fefffffeff0000000000","afh_link_used":38,"afh_mode":"on","afh_link_error":""}
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
- the `sco_*` fields are the audio link's, described under
  [What `status` reports](#what-status-reports), and the `afh*` fields are
  the channel map's, under [The channel map](#the-channel-map).

Reading a status line while a link is up asks the controller for that link's
channel map, which normally takes well under a millisecond and at worst a
second; see [The channel map](#the-channel-map).

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
started. Nothing starts until `sco start`, so a board that has not run it is on the
air exactly as it was before this was added. After a `sco start` and
`sco stop` the SPP side works as before, but whether the Hands-Free SDP
record and any Class of Device the profile set are fully undone is ESP-IDF's
business and is not checked here: a test that compares two SPP captures byte
for byte should run them on a board that has had no audio link, or reset it
in between.

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
two links are separate. Those two answer `ERR` and change nothing else if the
audio link will not stop, rather than half applying themselves.

A second `sco start` for the role a board is already in keeps the profile and
the service level connection it has, and opens only what is missing, so a
retry cannot pull down the link it is asking for. A frame size this firmware
cannot fill is a failed start: the link carries no pattern, so `sco start`
answers `ERR the link asked for a frame size this firmware cannot fill` and
puts the profile away.

A start that fails tries to put the profile away, and if that does not work
either it says so with a `utesp: sco the profile is still running` notice:
the answer to the command is why the start failed, and the notice is the
reason `role` and `defaults` will refuse next.

These commands block the shell while the controller works, so a driver's read
timeout has to allow for the worst case. Counting the tidying up a failure
does: about 32 seconds for `sco start` on the initiator (3 s to start the
profile, 15 s to page and bring the service level connection up, 10 s for the
audio link, then up to 3.5 s to put the profile back), about 10 seconds for
`sco stop` (3 s each for the link and the service level connection, 3.5 s for
the profile), and about 18 seconds for `role` and `defaults`, which stop the
audio link, then the SPP side, and then start the SPP server again. Allow 40 seconds and nothing will time out.
A link to a board that is awake takes four or five seconds in practice.

If `sco stop` keeps answering `ERR`, the profile is still the firmware's to
stop and `role` and `defaults` will keep refusing with it. `reset` is the way
out; the SPP side goes on working in the meantime.

### The audio pattern

There is no microphone on these boards, so the firmware makes the audio. Each
frame is a little-endian 32-bit frame counter followed by a ramp
(`frame[i] = i`), so no two frames of a link are alike and the content is
fixed rather than whatever the buffer happened to hold. The counter restarts
at zero each time the link comes up.

The counter is **not** readable from an Ubertooth capture: the controller
codes these samples to CVSD, so the bytes on the air are not these bytes. It
is readable by whatever sees the frames before that coding, which is the far
end over HCI, and it would reach the air unchanged in a build whose codec is
transparent. What a capture gets from this link is its timing and its packet
count, not its payload.

The link clocks the sending. A synchronous link is symmetric and the
controller hands up one frame per interval, so each side answers each frame
it receives with one frame of its own: that matches the link's rate with no
timer here to drift against it. If no frame arrives for 50 ms the firmware
sends one anyway, which both starts the exchange when the link comes up and
keeps a live link from going silent if a frame is ever missed.

The far end counts frames rather than comparing bytes, and `sco_rx_bad`
counts the frames the controller marked as damaged. Nothing in the harness
decodes CVSD, so no test compares payloads today.

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
| `sco_type` | the packet type the next link will ask for: `default`, `ev3`, or `2ev3` |
| `sco_link` | `none` (no link), `sco`, `esco`, or `unknown` |
| `sco_pkt_type` | the packet type the live link is using, named from its packet length (see below) |
| `sco_tx_len`, `sco_rx_len` | bytes per packet on the air, sending and receiving |
| `sco_interval` | Tesco, the slots between one packet and the next; a slot is 625 microseconds |
| `sco_retrans_window` | the retransmission window, in the same slots; 0 is none |
| `sco_types_supported` | the packet types the controller supports, as four hex digits (see below) |

`sco stats` asks the controller for its own packet counters, which is the
number a capture can be checked against:

```json
{"rx_total":4057,"rx_correct":4037,"rx_err":0,"rx_none":20,"rx_lost":0,"tx_total":4059,"tx_discarded":0}
```

That is a measured 30-second 2-EV3 link: 4059 packets each way, one every
7.4 ms, which is the rate the 12-slot interval `sco_interval` reports gives.
The frames are 120 bytes of 16-bit samples at that rate, which is 128 kbit/s
handed to the controller; the controller codes it down to the 64 kbit/s a
CVSD link carries on the air. An EV3 link carries half as much per packet, so
the controller halves the interval and the packet rate doubles.

### Choosing the packet type

`sco type` picks the synchronous packet type the next audio link asks for:

| Value | What the link asks for |
|---|---|
| `default` | whatever the stack's own table picks, which is 2-EV3 |
| `ev3` | EV3 and nothing else: no EDR type, and no SCO type either |
| `2ev3` | 2-EV3 and nothing else, which is the default spelled out |

It matters because of what is on the air. EV3 carries its payload at the
basic rate, so a basic-rate receiver can read it; 2-EV3 carries the same
payload as EDR, which is a different modulation. A decoder that reads
basic-rate payloads needs a link that uses plain EV3.

The command takes effect on the next link, so set it before `sco start`. It
is refused while a link is up (`ERR stop the audio link before choosing a
packet type`), because the choice reaches the controller only as a link is
opened. Set it on **both** boards: each board states the types it will allow,
the opening one in its request and the accepting one in its answer, and the
link uses a type both allowed. `defaults` puts it back to `default`.

`ev3` names no SCO type on purpose. On the air an HV3 and an EV3 share one
packet type code, so a capture could not tell them apart; leaving HV out
means a link that cannot be EV3 fails rather than quietly becoming an HV3.

#### How the choice is made

The parameters of a Hands-Free Profile audio link are compile-time tables
inside the stack, one per side, and no `esp_hf_*` or `esp_bt_gap_*` function
takes synchronous link parameters. So `bluedroid/utesp_sco_type.c` is built
into ESP-IDF's `bt` component, as the clock and channel-map reads are, and
reaches the two hooks from there. ESP-IDF's own tree is not changed. In
ESP-IDF v5.5.6:

- **The board that opens the link** is the audio gateway, and its table is
  `bta_ag_esco_params[]` in
  `components/bt/host/bluedroid/bta/hf_ag/bta_ag_sco.c`. Which row it uses
  follows from the negotiated codec and from whether both ends set the eSCO
  S4 feature bit, and both ESP32s do, so the row is CVSD S4: 2-EV3, 12 ms
  maximum latency, retransmission effort "quality". Bluedroid already has an
  override for that row, `bta_ag_set_esco_param()` (declared in
  `bta/hf_ag/include/bta_ag_int.h`), which `bta_ag_create_sco()` reads in
  place of the table; nothing in ESP-IDF calls it. This firmware does, on
  every `sco start` on the initiator, with the S4 row's own bandwidths,
  latency, voice setting, and retransmission effort and only the packet-type
  list changed.
- **The board that accepts the link** answers with its own list, from
  `bta_hf_client_esco_params[]` in `bta/hf_client/bta_hf_client_sco.c`. That
  table is `static const` and has no override, and its CVSD S4 row leaves the
  EV3 bit clear, so an accepting board would refuse a plain EV3 link however
  the opening board asked. The one call that carries the list to the
  controller, `BTM_EScoConnRsp()`, is therefore wrapped at link time
  (`-Wl,--wrap` in the project's `CMakeLists.txt`) and the list adjusted on
  its way past. Only the list is changed; the rest of what the hands-free
  unit chose suits the codec it negotiated.

The list is a mask in which bits 0 to 5 allow HV1, HV2, HV3, EV3, EV4, and
EV5, and bits 6 to 9 **forbid** 2-EV3, 3-EV3, 2-EV5, and 3-EV5 (Core 6.0
Vol 4 Part E 7.1.26), so a list that names no EDR type still has to forbid
each one. `ev3` sends `0x03c8` and `2ev3` sends `0x0380`.

BTM masks whatever is asked for against the types the controller itself
supports, so a controller without EV3 refuses rather than being asked for
something it cannot do. `sco_types_supported` in `status` is that mask, read
with `BTM_ReadDeviceScoPacketTypes()`, in the same bit order.

#### What the link settled on

`sco_pkt_type` names the type the live link is using, and the fields beside
it are the parameters it is named from. The Synchronous Connection Complete
event carries the link type, the interval, the retransmission window, the
packet lengths, and the air mode, but **not** the packet type (Core 6.0 Vol 4
Part E 7.7.35): nothing in HCI reports it. So the packet length names it,
because each type has its own maximum payload and a link negotiated for
64 kbit/s of voice is given that maximum — 30 bytes for EV3, 60 for 2-EV3, 90
for 3-EV3. Where two types could carry one length the smaller is named, which
is why `sco_tx_len` and `sco_interval` are reported next to it.

A capture is the other half of the answer, and the one the fork's tests want:
on the air libbtbb names a 2-EV3 `HV2/2-EV3` and an EV3 `HV3/EV3/3-EV3`,
which are different packet type codes, so the two are easy to tell apart
there. The link is easy to find: `ubertooth-rx -l e9c5ba -u 0d -t 30` read
168 packets of the piconet while an audio link was up, and 0 in the same 30
seconds with the boards idle.

The codec is a build-time choice for the same reason:
`CONFIG_BT_HFP_WBS_ENABLE` decides whether mSBC is offered at all, and this
image sets it to `n`, so the link is always CVSD and `sco_codec` always reads
`cvsd` while one is up. With wide-band speech the two boards would settle on
mSBC, whose air mode is transparent, and then the firmware would have to
produce real mSBC frames rather than samples. `sco_codec` still reports what
the audio state says rather than a constant, so a build that turns wide-band
speech on does not start lying.

## The clocks

`clock` answers with one JSON line:

```json
{"native":4942,"piconet":4946,"accuracy":65535,"handle":129,"t_us":2119521,"read_us":750}
```

- `native`: this board's own clock (CLKN). It is 28 bits in units of 312.5
  microseconds, so it advances 3200 times a second and wraps about every
  23.3 hours.
- `piconet`: the clock of the piconet the board's link belongs to, which is
  the piconet leader's native clock. Both boards of a pair report the same
  piconet clock, whichever of them is the leader. The initiator pages, so it
  starts as the leader, and while it is, its two numbers are the same
  counter read twice; the link policy allows a role switch, so do not rely
  on that half.
- `accuracy`: the largest error the piconet clock may have, in the same
  units as the clock itself, so one tick is 312.5 microseconds. 65535 means
  the controller does not know, which is what these boards' controllers say.
  The spec sets it to zero for a native read, which is why the one reported
  is the piconet read's.
- `handle`: the connection handle of the baseband link, as the controller
  echoed it back.
- `t_us`: microseconds since this board booted, monotonic, taken when the
  answer reached the host. The controller sampled the clock some time before
  that, so it is an upper bound on that instant, and `read_us` bounds how
  far off it can be.
- `read_us`: how long the read took, from the command joining the queue to
  the answer reaching the host.

The two clocks are two separate commands, a fraction of a millisecond apart,
so on the leader the two numbers differ by a tick or two: in the line above,
one read took 750 microseconds and the two values are 4 ticks, 1.25
milliseconds, apart. On a follower the native clock is the board's own and
has nothing to do with the piconet's.

With no link there is no piconet: `piconet`, `accuracy` and `handle` read
`null`, `native` still reads, and `t_us` and `read_us` are the native read's.
A link that goes down while the command is in flight reads the same way, as
no piconet rather than as a failure. So does a board with more than one
baseband link, where "the piconet" would be a guess: these boards are
connectable, so something else in the room can open one.
A controller that refuses a read answers `ERR the controller refused the
clock read, status 0x<hh>` and prints no line, rather than an answer with a
hole in it. A controller that stops answering altogether is a different
case: the command it never answered keeps its place in the host's pending
list, so later reads answer `ERR the controller did not answer the clock
read` without sending anything more, and `reset` is the way back.

### How the command is sent

The ESP32's controller answers `HCI_Read_Clock` (opcode `0x1407`), but
ESP-IDF's Bluetooth host, Bluedroid, never sends it and gives an application
no way to send a chosen opcode:

- Bluedroid defines the opcode in
  `components/bt/host/bluedroid/stack/include/stack/hcidefs.h` and references
  it nowhere else, and no `esp_*` function returns a clock.
- `esp_ble_gap_vendor_command_send()` is the one public way to send a chosen
  opcode, and it cannot reach this one: it ends in
  `BTM_VendorSpecificCommand()`, and `btsnd_hcic_vendor_spec_cmd()` in
  `stack/hcic/hcicmds.c` ORs `HCI_GRP_VENDOR_SPECIFIC` into the opcode it is
  given. `0x1407 | 0xFC00` is `0xFC07`, a vendor command.
- `esp_vhci_host_register_callback()` takes one callback, and Bluedroid holds
  it while it is enabled, so a command sent that way would hand its answer to
  Bluedroid, which drops an event it has no pending command for.
- `esp_bluedroid_attach_hci_driver()`, which lets an application supply
  Bluedroid's transport, is compiled out when the controller is in the same
  build, which on the ESP32 it always is: `hci_host_send_packet()` and
  `hci_host_register_callback()` in `host/bluedroid/api/esp_bluedroid_hci.c`
  call the VHCI functions directly under
  `#if (BT_CONTROLLER_INCLUDED == TRUE)`.

What is reachable is Bluedroid's own command queue, from inside Bluedroid.
So `bluedroid/utesp_clock.c` here is compiled as part of ESP-IDF's `bt`
component rather than as part of this firmware's component: the top-level
`CMakeLists.txt` adds it to that component's library and include path. It
builds the command with Bluedroid's own `HCI_GET_CMD_BUF()`, sets the
completion callback in the buffer's metadata as `btu_hcif_send_cmd()` does,
and hands it to `hci_layer_get_interface()->transmit_command()`.

The command then joins the queue every other HCI command goes on, under the
same command credits, and the HCI layer matches the Command Complete to it by
opcode and calls the callback. Host and controller stay in step, and nothing
in ESP-IDF's own tree is modified: a different ESP-IDF checkout needs no
patching, only the same version.

## The channel map

A Classic Bluetooth piconet using Adaptive Frequency Hopping (AFH) hops over
the channels its leader has kept, not all 79. `afh` picks those channels, so
a test can watch an Ubertooth follow a piconet that is using part of the band
and check that it sees traffic on those channels and nowhere else:

```
afh ffffffffff0000000000    channels 0 to 39, the lower half of the band
afh off                     every channel again
```

The map is the 79 channels as twenty hex digits: ten bytes, in which bit `b`
of byte `i` is channel `8i + b`, a set bit meaning the channel may be used.
That is the order the spec's `Set_AFH_Host_Channel_Classification` uses, and
the order libbtbb prints a recovered map in, so what `ubertooth-afh` prints
as `AFH map: 0x...` can be compared with this string as it stands.

Set it on the board that leads the piconet, which is the initiator because it
pages: the leader decides the map and announces it to the other board. The
acceptor takes the command too, because the spec lets either side tell its
controller what it thinks of the channels, but on this bench only the leader's
map decides what is on the air. Nothing here pins the baseband role, and the
board does not report it, so a test that depends on which board leads should
read the LAP on the air, which is the leader's.

Either command answers once the controller has taken the map, rather than as
soon as the command was sent. Taken is not yet on the air: the leader then
announces the map to the other board and the hop sequence follows, so a test
that has just set a map should give the link a moment and watch the air, not
assume the next packet is on the new map.

Set it before the link comes up for a test that wants the whole link on one
map; that is the way the bench has measured it. The command is accepted with
a link up as well, and the controller reports the new map on the link, but
how soon the air follows a mid-link change has not been measured here.

Every answer `afh` can give:

| Answer | Why |
|---|---|
| `OK` | the controller took the map |
| `ERR the channel map must be twenty hex digits` | not twenty hex digits |
| `ERR the top bit of the last byte is reserved and must be zero` | bit 79 is reserved |
| `ERR a piconet needs at least 20 channels` | fewer than 20 channels marked used |
| `ERR the controller refused the channel map, status <n>` | the controller said no, with its own status |
| `ERR the channel map was not answered` | nothing came back within three seconds |
| `ERR the last channel map has not been answered` | an earlier `afh` timed out and is still unanswered; `reset` is the way out |
| `ERR the stack would not take the channel map` | ESP-IDF refused the call |
| `ERR bluetooth is not up` | the board's Bluetooth failed to start, which the boot notice says |
| `ERR afh takes twenty hex digits or off` | the wrong number of words |

A refused map changes nothing.

The 20-channel minimum is the spec's (Core 6.0 Vol 4 Part E 7.3.46, and
`N_min` in Vol 2 Part B 2.6.3), and nothing in ESP-IDF checks it:
`BTM_SetAfhChannels()` in
`components/bt/host/bluedroid/stack/btm/btm_devctl.c` passes the ten bytes
straight to the controller. The reserved bit it does handle, by clearing it:
`esp_bt_gap_set_afh_channels()` in
`components/bt/host/bluedroid/api/esp_gap_bt_api.c` masks the last byte with
`0x7f`, so a map that sets that bit would be applied as a different map from
the one asked for. Refusing is the honest answer to both, and it keeps a test
from reading whatever the controller does with a map no piconet can use as a
result.

### The two maps in `status`

| Field | Meaning |
|---|---|
| `afh` | the map the controller has taken from this board, twenty hex digits; the all-channel map until `afh` has been run |
| `afh_used` | how many of the 79 channels that map marks used |
| `afh_link` | the map the live link is really hopping over, read from the controller, or `null` |
| `afh_link_used` | how many channels that map marks used, or `null` |
| `afh_mode` | `on` or `off` as the controller reports AFH for the link, `null` with no link or no answer |
| `afh_link_error` | why `afh_link` is `null` when something went wrong, otherwise empty |

The two maps are worth having separately because they can differ. The map the
host asks for is a classification, not an order: the leader's controller
keeps its own view of the channels as well, so the map it ends up hopping
over can be a subset of what was asked for. `afh_link` is that map, which is
the one to compare a recovered map against.

`afh` changes when the controller takes the map, not when the command sends
it, so a map the controller accepts after `afh` has given up waiting on it
still shows up there rather than leaving `status` reporting the map before
it.

With no one link there is no link map: `afh_link`, `afh_link_used` and
`afh_mode` read `null` and `afh_link_error` is empty, and `afh` still reports
what was asked for. That covers a board with no link and a board with more
than one, which is no one piconet, exactly as the clock read treats it; these
boards are connectable, so something else in the room can open the second
one. A link whose AFH the controller reports as off reads the same way except
that `afh_mode` is `"off"`, because the map means nothing then.

An empty `afh_link_error` therefore means nothing went wrong, and anything
else is a reason: a board that has a link the controller will not answer
about reads `the controller does not know this link`, which is not the same
as having no link, and a link that changed under the read says so too.

The link map is read first and the rest of the line is gathered after it, so
on a link that is going down the three link fields can describe a link the
same line reports as `"connected":false`. The read is also what makes a
status line with a link up cost a round trip to the controller.

`defaults` puts every channel back, and only sends a command if a map was
asked for, so `defaults` behaves exactly as it did on a board that has never
run `afh`. It does that last, after everything else is restored, and answers
with the reasons in the table above if the channel map is the part that
failed. A `reset` or a power cycle leaves the controller hopping over every
channel.

### How the link's map is read

Nothing in ESP-IDF reports the map a link is hopping over.
`esp_bt_gap_set_afh_channels()` is the only AFH call it offers, and its
completion event, `ESP_BT_GAP_SET_AFH_CHANNELS_EVT`, carries a status and
nothing else. Bluedroid names the HCI command that would answer the question,
`HCI_READ_AFH_CH_MAP` in
`components/bt/host/bluedroid/stack/include/stack/hcidefs.h`, and references
it nowhere else in the tree.

So it is read the way the clock is read: `bluedroid/utesp_afh.c` is compiled
as part of ESP-IDF's `bt` component and sends `HCI_Read_AFH_Channel_Map`
(opcode `0x1406`) down Bluedroid's own command queue, which is the only route
to a chosen opcode. [How the command is sent](#how-the-command-is-sent)
carries the whole argument for why no public route works; it applies here
word for word.

One read at a time, and a read that times out cannot leave its answer for the
next one. A controller that answers the command and refuses it costs nothing
but the `afh_link_error` line: the map that was asked for, and everything else
`status` reports, is unaffected. A controller that stops answering altogether
is the case the clock section describes: the command it never answered keeps
its place in the host's pending list, so later status lines say the controller
did not answer without sending anything more, and `reset` is the way back.

If an answer never comes, the `afh` that was waiting says so after three
seconds, and every `afh` after it answers `ERR the last channel map has not
been answered` at once, because a late answer must not be read as the new
map's. `reset` is the way out, as it is for the clock. Neither message blames
the controller, because the answer can also go missing inside the stack:
`bta_dm_set_afh_channels()` drops `BTM_SetAfhChannels()`'s own refusal, so a
map ESP-IDF never sent looks the same from here.

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
