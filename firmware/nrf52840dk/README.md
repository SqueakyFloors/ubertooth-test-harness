# nRF52840 DK role firmware

Firmware for the Nordic nRF52840 DK (PCA10056) boards the harness uses as
Bluetooth LE test devices. The Ubertooth stays receive-only; these boards do
the transmitting. One app holds every role; today it is the **advertiser**
for `Capture known BLE advertiser`
([#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86)).
The connection pair (central and peripheral,
[#87](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/87)) is
planned as further roles of the same app and shell (see [Extending](#extending)).

Firmware version: `utdk-1.0.0` (`UTDK_FW_VERSION` in `src/roles.h`; the
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

Then build from this directory:

```sh
nrfutil sdk-manager toolchain launch --ncs-version v3.4.1 --install-dir ~/ncs -- \
  west build --no-sysbuild -b nrf52840dk/nrf52840 -d /tmp/utdk-build \
  -s firmware/nrf52840dk -- -DZEPHYR_BASE=$HOME/ncs/v3.4.1/zephyr
```

Run `west build` from inside the SDK workspace (`~/ncs/v3.4.1`) or pass
`ZEPHYR_BASE` as above. The image is `/tmp/utdk-build/zephyr/zephyr.hex`.
Builds go outside the repository; nothing under `firmware/` is generated.

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
colours. Send one command line ended by CR; the board answers with zero or
more data lines, then exactly one line `OK` or `ERR <reason>`. Any other line
(the boot banner `*** Booting nRF Connect SDK ...`, `utdk: utdk-1.0.0 ready`)
is not a response.

| Command | Answer |
|---|---|
| `status` | one JSON line, then `OK` |
| `version` | the firmware version, then `OK` |
| `adv start` | start advertising with the current settings |
| `adv stop` | stop advertising |
| `adv chmap <37\|38\|39\|all>` | advertising channels |
| `adv interval <ms>` | advertising interval, 20-10240 ms (default 100) |
| `adv payload <hex\|none>` | payload tag, up to 12 bytes (default none) |
| `adv type <nonconn\|conn>` | `ADV_NONCONN_IND` (default) or `ADV_IND` |
| `adv defaults` | stop, and restore every boot setting |

A setting changed while advertising restarts advertising with it. Bad values
answer `ERR` and change nothing. `status`, for example:

```json
{"role":"advertiser","fw":"utdk-1.0.0","bt":true,"address":"C3:11:22:33:44:55","address_type":"random static","adv":true,"running":true,"type":"nonconn","chmap":"37","interval_ms":100,"payload":"c0ffee","counter":12,"starts":1,"connections":0,"last_err":0}
```

- `address`: the identity address, a static random address derived from
  the chip's FICR, so it is the same on every boot of the same board.
  Register it as the DK's `BLE address`.
- `adv`: advertising was asked for; `running`: the controller is
  advertising now (false for a moment after a connection to `ADV_IND`).
- `counter`: the payload counter (below); `starts`: advertising starts
  since boot; `connections`: connections accepted to `ADV_IND` (each is
  dropped at once and advertising starts again); `last_err`: the last
  Bluetooth API error (0 is none).

The board does not count advertising events: the counter steps once a
second while advertising, so a capture's counters show which second of the
run each packet came from.

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

The connection pair (#87) adds `central` and `peripheral` roles to this app:
a `role <advertiser|central|peripheral>` command, a `conn ...` command group
(interval, data length extension, start/stop), and their fields in
`status`, whose `role` already names the running role. The protocol stays
line-based with the same `OK`/`ERR` endings, and `nrfdk.py` keeps one shell
driver for every role.

## License

The sources in this directory are under the Apache License 2.0
(`SPDX-License-Identifier: Apache-2.0` in each file), the license of the
Zephyr samples they follow, not the GPLv2 of the rest of the repository. A
built image also links Nordic's SoftDevice Controller and MPSL from the nRF
Connect SDK, which are under the LicenseRef-Nordic-5-Clause license (use only
with Nordic hardware); images are built locally and not committed.
