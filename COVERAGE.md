# Ubertooth host CLI test coverage

This file tracks which Ubertooth host command-line tools and options the
[Ubertooth test harness](https://github.com/SqueakyFloors/ubertooth-test-harness)
exercises, so we can pick the next tests to write. Update it in the same PR as any test
that changes coverage. Options come from each
tool's `getopt` string and usage text on this fork's `main`. Coverage comes
from the harness catalog (`ubertooth_harness/catalog.py`,
`ubertooth_harness/transitions.py`), its plans (`testplans/*.yaml`) and its
preflight, flash, and guard code. Profiles: **smoke** runs in the `fast` plan,
**comprehensive** in the `release` plan; tests without a profile name their plan.
Status legend:
**Covered**: a harness test runs the option and checks its output or effect.
The test and profile are named.
**Partial**: the option runs, but only some of its behavior is checked, or the
test is in no plan. The notes say what is missing.
**Not covered**: no harness test runs the option.
**Hard**: needs an RF peer, a GUI, RF transmission, firmware other than
`bluetooth_rxtx`, a second device, or is destructive. The notes say which.
**N/A**: nothing to test (for example a no-op).
Tests that are only catalog entries (`BLE monitor`, `Spectrum RSSI sanity`,
`Classic Bluetooth receive`, the soaks, and every `backlog.yaml` item) have no
implementation yet. They count as Not covered.

Harness-wide checks that use the CLIs:

- **Preflight** (every run): `ubertooth-util -H` must match the resolved host
  sources. For each selected board, `ubertooth-util -U <serial>` runs `-v`
  (firmware revision and API), `-V`, and `-s`.
- **Guards** (stateful tests): every other registered board on the host is read
  before and after the test, with queries only: `ubertooth-util -v -V -C -a -z -d`
  and then `ubertooth-btle -v`. The `ubertooth-btle -v` read is skipped unless
  the board's firmware API is at least the host API.
- **Flash reliability** (`slow` plan): repeated `ubertooth-dfu -U <serial> -d <image> -r`.
  Each flash is verified with `ubertooth-util -v`.
- **Exclusive Ubertooth topology** (`exclusive` plan, Linux, power control):
  runs `ubertooth-util -s`, `ubertooth-util -v`, and `ubertooth-btle -f` with no
  `-U` while only one board is powered.
- **BTLE follow polling soak** (planned; catalog-only): long `ubertooth-btle -f`
  without `-U` on a single board, with a known advertiser. Not implemented yet.

## ubertooth-util

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (fast, release): exit 0 and tool name in the output |
| `-H`, `--host-version` | host tools version | Covered | `-H` in preflight on every run; `--host-version` in Host CLI surface (fast, release) |
| `-U<index\|serial>` | select device | Partial | Every device test selects by serial. Repeated `-U` (index 99, then serial) is last-wins (util compositions, comprehensive). Selecting a real index is not tested |
| `-U all` | identity queries for every board | Not covered | Needs two or more boards |
| `-N` | count devices | Covered | Ubertooth count (fast, release): equals the registered boards on the host |
| `-v` | firmware revision | Covered | Ubertooth identity (fast, release), preflight, util compositions (smoke) |
| `-V` | compile info | Covered | Ubertooth identity, preflight, util compositions (smoke) |
| `-s` | MCU serial | Covered | Ubertooth identity, preflight, cleanup identity check |
| `-p` | part ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-b` | board ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-a[0-7]` | get/set PA level | Covered | Get and set: Radio setting transitions (smoke). Repeated and invalid `-a9`: comprehensive. `-a0` was a query; fork main now sets level 0 ([ubertooth#161](https://github.com/SqueakyFloors/ubertooth/issues/161), `4ed4a1d`), and the harness still skips PA-0 boards ([#5](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/5)). Boot and reset default is 7 (docs: [ubertooth#160](https://github.com/SqueakyFloors/ubertooth/issues/160)) |
| `-c[2402-2480]` | get/set channel (MHz) | Covered | Radio setting transitions (smoke). Repeated and mixed with `-C`: comprehensive. Out-of-range `-c2401`/`-c2481` rejected with no change: Argument validation (comprehensive; `-c2401` smoke) |
| `-C[0-78]` | get/set channel (index) | Covered | Get: smoke. Set, repeated, and mixed: comprehensive. `-C79` rejected: Argument validation (comprehensive). `-Cabc` sets 2402 (fork bug [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-z[level]` | get/set squelch | Covered | Radio setting transitions (smoke); repeated: comprehensive. `-zabc` sets 0 and `-z-200` wraps to 56 (fork bug [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-l[0-1]` | get/set USR LED | Covered | Get: util compositions (smoke). Set across a mode stop and repeated: comprehensive. `-l2` acts as a query ([ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-d[0-1]` | get/set all LEDs | Covered | `-d1` and the `-d` readback in every setting case (smoke); repeated `-d`: comprehensive. `-d5` is silently ignored ([ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-S` | stop current operation | Covered | Starts and ends every stateful case (smoke) |
| `-r` | full reset | Covered | Full reset returns the boot defaults (Radio setting transitions, comprehensive) |
| `-x` | xmas lights mode | Covered | Used to enter an active mode before a stop (Radio setting transitions, smoke) |
| `-I` | identify (flash LEDs) | Partial | Only the conflict rejections run it (in no plan). It runs until stopped; moving it into the firmware is [ubertooth#167](https://github.com/SqueakyFloors/ubertooth/issues/167). [#34](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/34) |
| `-q[1-225]` | LED spectrum analyzer | Partial | Only the conflict rejections (in no plan). `-q300` reaches the firmware ([ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)). Running the mode: [#34](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/34) |
| `-t` | continuous TX test | Hard | Transmits RF. Only the conflict rejections run it (in no plan) |
| `-n` | start range test | Hard | Transmits RF; a useful test needs a responder board. Conflict rejections only |
| `-e` | repeater mode | Hard | Transmits RF; needs a sender board. Conflict rejections only |
| `-m` | show range test result | Not covered | With no result it prints "invalid range test result" and exits 0 (seen by hand). [#34](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/34) |
| `-f` | DFU mode (deprecated) | Hard | Leaves the application firmware. The deprecation message could be checked through a rejected conflict |
| `-i` | ISP mode | Hard | Leaves the application firmware; recovery needs ISP tooling |
| `-9` | firmware test command (undocumented) | Not covered | Prints 4 reply bytes; low value |

Combinations to test:

- Read-only queries in one command (`-v -V`, all reports, every compatible pair): Covered (smoke, comprehensive).
- Repeated set options are last-wins (`-c -C -a -z -l -d`): Covered (comprehensive).
- `-c`/`-C` share one value in both orders: Covered (comprehensive).
- Rejected set followed by a successful report still exits nonzero (`-a9 -v`): Covered (comprehensive).
- Settings kept by a stop from idle and reset by an active-mode stop: Covered (smoke; USR LED comprehensive).
- Two or more modes rejected before any mode starts (`-q -n -e -t -x -I`): Partial. The `Ubertooth util conflicts` test exists but is in no plan. Fork #82 is fixed (#134), so it can go into the release plan after a hardware run ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)).
- `-f`/`-i` combined with another mode is rejected: Not covered (risk: an accepted pair strands the board).
- `-U all` with a setter (`-a5`) or mode (`-x`) is rejected with no change: Covered (Argument validation, comprehensive).
- Read-only queries without `-U` while several boards are attached list every board: Not covered. The single-board path is covered by Exclusive topology.
- `-z` combined with `-q`, `-n`, `-e`, or `-t`: was silently ignored ([ubertooth#159](https://github.com/SqueakyFloors/ubertooth/issues/159)); fork main now refuses it (`4ed4a1d`). Not covered: add an `Argument validation` case ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)).
- A value given as the next word (`-c 2426`, `-z -70`) sets the same as an attached one, and a stray argument is rejected (fork #156): Not covered ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)).

## ubertooth-btle

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-f` | follow connections | Covered | BLE CRC transitions (smoke: CRC on, channel 37; comprehensive: every CRC value × channel and capture format). Exclusive topology runs it without `-U`. Following a real connection is not checked (needs a peer) |
| `-n` | advertisements only | Covered | BLE CRC transitions and capture cases (smoke, comprehensive). Packet content is not checked |
| `-p` | promiscuous | Partial | Only BLE mode conflicts (release). Never run alone; a useful test needs an active connection |
| `-a[addr]` | get/set access address | Covered | BLE settings (smoke: set/read back; comprehensive: reset by `util -r`). Bad value and `-a -v` fail until [ubertooth#171](https://github.com/SqueakyFloors/ubertooth/issues/171) |
| `-s<mac>` | faux peripheral mode | Hard | Transmits RF; needs a central peer |
| `-t<mac[/mask]>`, `-tnone` | set/unset follow target | Partial | Set, masked, and cleared: BLE settings (smoke); malformed MAC rejected: Argument validation. The filter itself needs a peer |
| `-i` / `-j` | interfere once (`-j` alias) | Hard | Jams a live connection (transmits RF) |
| `-I` / `-J` | interfere continuously (`-J` alias) | Hard | Same as `-i` |
| `-U<index\|serial>` | select device | Covered | Every btle case uses a serial |
| `-r<file>` | PCAPNG capture | Covered | BLE CRC transitions: the header and link type 256 are checked (smoke with `-n`, comprehensive with `-n` and `-f`). `-r -` (stdout) and packet content are not tested |
| `-q<file>` | PCAP (LE_LL_WITH_PHDR) | Covered | As `-r`: pcap with link type 256 |
| `-c<file>` | PCAP (PPI) | Covered | As `-r`: pcap with link type 192 |
| `-A<37-39>` | advertising channel | Covered | 37 in smoke; 37, 38, and 39 in comprehensive. `-A36`/`-A40` rejected: Argument validation (comprehensive) |
| `-v[01]` | get/set CRC check | Covered | Get, set, and applied by a mode (smoke); `-v0` and repeated `-v` (comprehensive) |
| `-x<0-32>` | allowed access-address errors | Partial | `-x33` rejected (Argument validation); `-x0`/`-x32` accepted (BLE settings, comprehensive). The effect on capture needs traffic |

Combinations to test:

- `-f`, `-n`, `-p`: any two or all three are rejected: Covered (BLE mode conflicts, release).
- `-v[01]` applied after the mode starts, overriding an opposite preset: Covered (smoke, comprehensive).
- Repeated `-v` is last-wins: Covered (comprehensive).
- Repeated capture options are first-wins and the extra file is never created (`-r -r`, `-q -c`, `-c -q`): Covered (comprehensive).
- `-r` together with `-q` (separate handles): both files written: Covered (comprehensive). `-r` with `-c`: Covered (comprehensive).
- Capture with each CRC value and mode: Covered (comprehensive).
- `-i`/`-I` without `-f`/`-p`: accepted and ignored (the jam mode is only sent with a sniffing mode, per source). Hard: with a mode it transmits.
- `-a`/`-v` get and set without a mode do not start sniffing: Covered (BLE settings); `-a -v` reports both fails until ubertooth#171.
- `-A` with `-p` or `-f` after a follow hop: Not covered (needs a peer).

## ubertooth-debug

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-U<index\|serial>` | select device | Covered | Debug register read (fast) |
| `-r <reg>[,<reg>...]` / `-r <start>-<end>` | read CC2400 registers | Covered | Debug registers: list (smoke), range and reversed range (comprehensive), order checked, nothing changed. Exit status checked; fails on builds before the [ubertooth#164](https://github.com/SqueakyFloors/ubertooth/issues/164) fix |
| `-v<0-2>` | verbosity | Covered | Debug registers (comprehensive): `-v0` header only, `-v1` fields, `-v2` reserved fields; `-v3` rejected |

Combinations: list and range output: Covered. `-v0`/`-v2` output: Covered. `Debug registers` is in the release plan (comprehensive).

## ubertooth-dfu

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-d <file>` | write DFU image | Covered | Flash reliability (slow): repeated exact-verified writes |
| `-r` | reset after the operation | Covered | Flash reliability (slow), always with `-d` |
| `-U <index\|serial>` | select device | Covered | Flash reliability (by serial; optionally alternating boards) |
| `-u <file>` | read firmware from the device | Not covered | Read-back; compare with the image last written |
| `-s <file>` | add DFU suffix to a binary | Not covered | Host-only, no device; easy |

Combinations: `-d` without `-r` (board stays in DFU): Not covered (Hard: leaves the application firmware). `-u` then `-d` of the same image: Not covered. A bad image or wrong suffix is rejected: Not covered.

## ubertooth-specan

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0) |
| `-v` | verbose to stderr | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-g` | feedgnuplot output | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-G` | 3D feedgnuplot output | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-d <file>` | write output to a file | Covered | Spectrum analyzer (comprehensive): binary 3-byte records checked by `specandump.py` |
| `-l <2402-2480>` | lower frequency | Covered | Spectrum analyzer (comprehensive) ranges; `-l 2401` rejected (Argument validation). Range changes fail until [ubertooth#173](https://github.com/SqueakyFloors/ubertooth/issues/173) |
| `-u <2402-2480>` | upper frequency | Covered | As `-l` |
| `-U <index\|serial>` | select device | Covered | Spectrum analyzer by serial, guard board unchanged |

Combinations: `-l` > `-u` rejected: Covered (Argument validation). `-g` vs `-G` output shape: Covered. Every reported frequency falls between `-l` and `-u`: Covered (fails after a range change until ubertooth#173).

## ubertooth-specan-ui (Python, GUI)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help (argparse) | Not covered | The harness builds with `ENABLE_PYTHON=OFF`, so specan-ui is not installed ([#37](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/37)) |
| `-U <index\|serial>` | select device | Hard | Qt GUI |
| `-l <freq>` | lower frequency | Hard | GUI. The argument checks could run before the window opens |
| `-u <freq>` | upper frequency | Hard | GUI; `-u` < `-l` must be rejected |
| `--wifi[=ch\|a-b]` | Wi-Fi channel band | Hard | GUI |
| `--padding <MHz>` | band padding with `--wifi` | Hard | GUI |

Combinations: `--padding` without `--wifi`, `--wifi` with `-l`/`-u`: Not covered (Hard).

## ubertooth-rx

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-l <LAP>` | LAP to decode | Not covered | A useful test needs a BR/EDR piconet |
| `-u <UAP>` | UAP to decode | Not covered | Needs `-l` and a piconet |
| `-z` | survey mode | Not covered | Classic Bluetooth receive is catalog-only |
| `-i <file>` | input file instead of a device | Not covered | Raw receive replay is in the backlog; no hardware needed |
| `-c <ch>` | fixed BT channel | Not covered | |
| `-e <0-4>` | max access-code errors | Not covered | |
| `-t <s>` | sniff timeout | Not covered | Bounds a live receive without `timeout` |
| `-r<file>` | PCAPNG capture | Not covered | |
| `-q<file>` | PCAP capture | Not covered | |
| `-d<file>` | binary dump | Not covered | |
| `-s` | deprecated sweep flag | Not covered | Prints a deprecation notice only |
| `-V` | version | Covered | Host CLI surface (fast, release): host version line |
| `-U <0-7>` | select device | Not covered | |

Combinations: `-u` without `-l` rejected: Covered (Argument validation, smoke). `-z` with an address rejected: Covered (Argument validation, comprehensive). `-z -t N` exits after N s: Not covered. `-i` replay into `-r`/`-q`: Not covered.

## ubertooth-dump

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-b` | raw bitstream only | Not covered | |
| `-c` | classic modulation | Not covered | |
| `-l` | LE modulation | Not covered | |
| `-d <file>` | write to a file | Not covered | |
| `-U<0-7>` | select device | Not covered | |

Combinations: `-c` with `-l`: last one wins (per source). Not covered. Output size over a bounded run: Not covered.

## ubertooth-tx

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-l <LAP>` | target LAP | N/A | The tool does nothing (its usage text says so) |
| `-u <UAP>` | target UAP | N/A | Same |
| `-t <s>` | timeout | N/A | Same |
| `-V` | version | Covered | Host CLI surface (fast, release): host version line |
| `-U <0-7>` | select device | N/A | Same |

## ubertooth-afh

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-l <LAP>` | target LAP | Hard | Needs a BR/EDR piconet using AFH (Recover AFH map is in the backlog) |
| `-u <UAP>` | target UAP | Hard | Same |
| `-m <n>` | channel-removal threshold | Hard | Same |
| `-r` | print the map every second | Hard | Same |
| `-t <s>` | initial detection timeout | Not covered | Timeout without a piconet is testable |
| `-e <0-4>` | max access-code errors | Not covered | |
| `-a <map>` | initial AFH map (undocumented) | N/A | Parsed and ignored (the code is commented out) |
| `-V` | version | Covered | Host CLI surface (fast, release): host version line |
| `-U <0-7>` | select device | Not covered | |

## ubertooth-ego

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-f` | follow mode | Hard | Needs an E-GO skateboard (backlog) |
| `-r` | continuous RX on one channel | Not covered | Runs without a peer (no packets expected) |
| `-i` / `-j` | interfere (`-j` alias) | Hard | Transmits RF |
| `-c <2402-2480>` | RX channel (MHz) | Not covered | Used with `-r` |
| `-l <1-48>` | capture length | Partial | `-l0` (smoke) and `-l49` rejected: Argument validation. Accepted lengths are not tested |
| `-a <code>` | access code | Not covered | |
| `-U <index\|serial>` | select device | Not covered | |

Combinations: two or more of `-f`/`-r`/`-i`: last one wins (`do_mode` is overwritten, per source). Not covered; `-i` transmits.

## ubertooth-ducky

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-q[uuid]` | send a quack | Hard | Transmits RF; needs an Uberducky (backlog) |
| `-b` | Uberducky bootloader | Hard | Transmits; changes the peer's firmware state |
| `-A <37-39>` | advertising channel | Partial | `-A36` (smoke) and `-A40` rejected: Argument validation. Only matters with `-q` |
| `-a <bdaddr>` | source address | Partial | A malformed address is rejected (Argument validation, comprehensive). Using it needs `-q` (transmits) |
| `-s` | sarcastic output (undocumented) | N/A | Cosmetic |
| `-U <index\|serial>` | select device | Hard | Only matters with `-q` |

## ubertooth-scan (BlueZ builds only)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1); skipped where not built (no BlueZ) |
| `-s` | BlueZ HCI scan | Hard | Needs a Bluetooth adapter and discoverable peers |
| `-x` | extended inquiry | Hard | Same |
| `-t <s>` | scan time | Hard | Same |
| `-e <0-4>` | max access-code errors | Hard | Same |
| `-b <hciN>` | HCI device | Hard | Same |
| `-U<0-7>` | select device | Hard | Same (Bluetooth inquiry scan is in the backlog) |

## ubertooth-follow (BlueZ builds only)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1); skipped where not built (no BlueZ) |
| `-l<LAP>` | target LAP | Hard | Needs a BR/EDR piconet and an adapter (backlog) |
| `-u<UAP>` | target UAP | Hard | Same |
| `-U<0-7>` | select device | Hard | Same |
| `-r<file>` | PCAPNG capture | Hard | Same |
| `-q<file>` | PCAP capture | Hard | Same |
| `-e <n>` | max access-code errors | Hard | Same |
| `-d <file>` | binary dump | Hard | Same |
| `-a` | enable AFH | Hard | Same |
| `-b <hciN>` | HCI device | Hard | Same |
| `-w <slots>` | USB delay | Hard | Same |

## ubertooth-btbr (Python)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-f <n>` | frequency offset | Hard | Needs the `btbr` firmware, not `bluetooth_rxtx`; interactive console |
| `-b <n>` | max access-code errors | Hard | Same |

## btle-extcap (Python, Wireshark extcap; not installed by CMake)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h`, `--help` | help | Not covered | |
| `--extcap-interfaces` | list interfaces | Not covered | Host-side; needs the Python tools installed ([#37](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/37)) |
| `--extcap-dlts` | list DLTs | Not covered | Host-side; needs the Python tools installed ([#37](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/37)) |
| `--extcap-config` | config options | Not covered | Host-side; needs the Python tools installed ([#37](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/37)) |
| `--capture` | start a capture | Not covered | Needs a device; check the FIFO header |
| `--extcap-interface=` | interface | Not covered | |
| `--fifo=` | output FIFO | Not covered | |
| `--channel=` | advertising channel | Not covered | |

## Fork bugs found by these tests

Open or recently fixed SqueakyFloors/ubertooth issues that harness tests found
or that strict harness cases check. Strict cases assert the documented behavior
and fail until the fix is on the commit under test.

| Fork issue | Tool | Problem | Status | Harness case |
|---|---|---|---|---|
| [#158](https://github.com/SqueakyFloors/ubertooth/issues/158) | firmware | Board stops answering after a sniffing or specan stream ends, until re-plugged | Open (fix in testing) | Any BLE or specan run |
| [#149](https://github.com/SqueakyFloors/ubertooth/issues/149) | firmware | GET_CHANNEL control requests stall in follow mode | Open | Not yet |
| [#159](https://github.com/SqueakyFloors/ubertooth/issues/159) | ubertooth-util | `-z` dropped with a mode | Fixed on main (`4ed4a1d`) | To add ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)) |
| [#160](https://github.com/SqueakyFloors/ubertooth/issues/160) | docs | PA boot/reset default (7) undocumented | Open | Radio setting transitions checks 7 |
| [#161](https://github.com/SqueakyFloors/ubertooth/issues/161) | ubertooth-util | `-a0` queried instead of setting level 0 | Fixed on main (`4ed4a1d`) | PA-0 handling ([#5](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/5)) |
| [#164](https://github.com/SqueakyFloors/ubertooth/issues/164) | ubertooth-debug | Exit status was the register's low byte | Fixed on main (`89afc1c`) | Debug registers (comprehensive) |
| [#171](https://github.com/SqueakyFloors/ubertooth/issues/171) | ubertooth-btle | Bad `-a` value sets garbage; `-a` skips `-v` and a later set | Open | BLE settings (comprehensive) |
| [#172](https://github.com/SqueakyFloors/ubertooth/issues/172) | ubertooth-util | Non-numeric and out-of-range values accepted (`-z`, `-l`, `-d`, `-C`, `-q`) | Open | To add to Argument validation |
| [#173](https://github.com/SqueakyFloors/ubertooth/issues/173) | ubertooth-specan | A run first reports one stale packet from the previous range | Open | Spectrum analyzer (comprehensive) |

A failed test ends the run ([#41](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/41)), so the release plan lists the tests with strict cases last.

## Hard or impossible to test

- **GUI:** `ubertooth-specan-ui` needs Qt and a display. Only `-h` and the
  argument checks can run headless (an offscreen Qt platform may allow a smoke run).
- **Transmits RF:** `ubertooth-util -t -n -e`, `ubertooth-btle -s -i -I -j -J`,
  `ubertooth-ego -i -j`, and `ubertooth-ducky -q -b`. These need an explicit
  RF allowance in the plan, like `allow RF transmission` on Ubertooth util conflicts.
- **Leaves the application firmware:** `ubertooth-util -f -i` and
  `ubertooth-dfu -d` without `-r`. They need a recovery path (USB power cycle,
  DFU/ISP reflash). The Reset into invalid application backlog item covers part of this.
- **Needs an RF peer:** BR/EDR piconet (`ubertooth-rx -l -u`,
  `ubertooth-afh`, `ubertooth-follow`), BLE connection (`ubertooth-btle -f/-p`
  with real traffic, `-t`), E-GO skateboard, Uberducky.
- **Needs a host Bluetooth adapter / BlueZ:** `ubertooth-scan`, `ubertooth-follow` (Linux).
- **Needs a second Ubertooth:** `ubertooth-util -n`/`-e`/`-m` range test,
  `-U all`, and multi-board no-selector listing.
- **Other firmware:** `ubertooth-btbr`.
- **Does nothing:** `ubertooth-tx`.

## Suggested next tests

Ordered by value/effort:

1. Done (#27): host-side argument rejections across util, btle, ego, ducky, rx, specan (`Argument validation`). Still open: `ubertooth-dump -c -l` (last-wins, not a rejection) and `ubertooth-rx -e5`.
2. Done (#28): `--host-version`, `-N`, `-V` on rx/tx/afh, BlueZ tool help.
Next, harness work:

- Keep running later tests after a test fails cleanly ([#41](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/41)).
- Catch up with fork main: `-a0` sets PA 0 ([#5](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/5)), `-z` with a mode is refused and next-word values ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)), and `#172` rejections once fixed ([#27](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/27)).

3. `ubertooth-dfu -s` (suffix a binary; host-only) and `-u` read-back compared with the last
   flashed image.
4. Done (#30): `Spectrum analyzer` ranges, `-g`, `-G`, `-v`, `-d`.
5. Done (#29): `BLE settings` (`-a`, `-t`, `-x`) and `-r` with `-q`/`-c`. Still open: `-r -` to stdout (would log raw capture bytes; needs a header-only check).
6. Done (#32): `Debug registers` lists, ranges, verbosity, exit status.
7. Raw receive replay: `ubertooth-rx -i <fixture>` with `-r`/`-q`/`-d`. Needs a public, synthetic fixture.
8. Implement BLE monitor / Classic Bluetooth receive (`ubertooth-rx -z -t N`,
   `ubertooth-dump -c/-l -d`, `ubertooth-ego -r -c`), checking exit and output shape, not content.
9. Add Ubertooth util conflicts to the release plan (fork #82 is fixed; [#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25));
   add `-f`/`-i` rejection cases only if no accepted pair can reach those modes.
10. Headless `ubertooth-specan-ui -h` and argument checks; `btle-extcap --extcap-interfaces/--extcap-dlts/--extcap-config`.
11. Done (#28): `ubertooth-scan -h` and `ubertooth-follow -h` (skipped where not built).
12. Peer-based tests from the backlog (known BLE advertiser, AFH map,
    piconet follow, two-board range test), once fixtures are registered.
