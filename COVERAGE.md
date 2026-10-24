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
Tests that are only catalog entries (`BLE monitor`, `Spectrum analyzer`,
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
| `-H`, `--host-version` | host tools version | Partial | `-H` runs in preflight on every run. `--host-version` is never run |
| `-U<index\|serial>` | select device | Partial | Every device test selects by serial. Repeated `-U` (index 99, then serial) is last-wins (util compositions, comprehensive). Selecting a real index is not tested |
| `-U all` | identity queries for every board | Not covered | Needs two or more boards |
| `-N` | count devices | Not covered | Easy to add (compare with the registry) |
| `-v` | firmware revision | Covered | Ubertooth identity (fast, release), preflight, util compositions (smoke) |
| `-V` | compile info | Covered | Ubertooth identity, preflight, util compositions (smoke) |
| `-s` | MCU serial | Covered | Ubertooth identity, preflight, cleanup identity check |
| `-p` | part ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-b` | board ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-a[0-7]` | get/set PA level | Covered | Get and set: Radio setting transitions (smoke). Repeated and invalid `-a9`: comprehensive. `-a0` is a query, so level 0 cannot be set |
| `-c[2402-2480]` | get/set channel (MHz) | Covered | Radio setting transitions (smoke). Repeated and mixed with `-C`: comprehensive. Out-of-range error is not tested |
| `-C[0-78]` | get/set channel (index) | Covered | Get: smoke. Set, repeated, and mixed: comprehensive. Out-of-range error is not tested |
| `-z[level]` | get/set squelch | Covered | Radio setting transitions (smoke); repeated: comprehensive |
| `-l[0-1]` | get/set USR LED | Covered | Get: util compositions (smoke). Set across a mode stop and repeated: comprehensive |
| `-d[0-1]` | get/set all LEDs | Covered | `-d1` and the `-d` readback in every setting case (smoke); repeated `-d`: comprehensive |
| `-S` | stop current operation | Covered | Starts and ends every stateful case (smoke) |
| `-r` | full reset | Covered | Full reset returns the boot defaults (Radio setting transitions, comprehensive) |
| `-x` | xmas lights mode | Covered | Used to enter an active mode before a stop (Radio setting transitions, smoke) |
| `-I` | identify (flash LEDs) | Partial | Only the conflict rejections run it (Ubertooth util conflicts, in no plan). The mode itself is never run |
| `-q[1-225]` | LED spectrum analyzer | Partial | Only the conflict rejections (in no plan) |
| `-t` | continuous TX test | Hard | Transmits RF. Only the conflict rejections run it (in no plan) |
| `-n` | start range test | Hard | Transmits RF; a useful test needs a responder board. Conflict rejections only |
| `-e` | repeater mode | Hard | Transmits RF; needs a sender board. Conflict rejections only |
| `-m` | show range test result | Not covered | Only meaningful after `-n`/`-e`. Reading with no result is easy to test |
| `-f` | DFU mode (deprecated) | Hard | Leaves the application firmware. The deprecation message could be checked through a rejected conflict |
| `-i` | ISP mode | Hard | Leaves the application firmware; recovery needs ISP tooling |
| `-9` | firmware test command (undocumented) | Not covered | Prints 4 reply bytes; low value |

Combinations to test:

- Read-only queries in one command (`-v -V`, all reports, every compatible pair): Covered (smoke, comprehensive).
- Repeated set options are last-wins (`-c -C -a -z -l -d`): Covered (comprehensive).
- `-c`/`-C` share one value in both orders: Covered (comprehensive).
- Rejected set followed by a successful report still exits nonzero (`-a9 -v`): Covered (comprehensive).
- Settings kept by a stop from idle and reset by an active-mode stop: Covered (smoke; USR LED comprehensive).
- Two or more modes rejected before any mode starts (`-q -n -e -t -x -I`): Partial. The test exists but needs `allow RF transmission` and is in no plan until fork issue #82 is fixed.
- `-f`/`-i` combined with another mode is rejected: Not covered (risk: an accepted pair strands the board).
- `-U all` with a setter or mode is rejected: Not covered. Host-side; needs no second board.
- Read-only queries without `-U` while several boards are attached list every board: Not covered. The single-board path is covered by Exclusive topology.
- Squelch combined with a mode is dropped: Not covered.
- A detached optional value (`-c 2426`) is parsed as a positional argument: Not covered.

## ubertooth-btle

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-f` | follow connections | Covered | BLE CRC transitions (smoke: CRC on, channel 37; comprehensive: every CRC value × channel and capture format). Exclusive topology runs it without `-U`. Following a real connection is not checked (needs a peer) |
| `-n` | advertisements only | Covered | BLE CRC transitions and capture cases (smoke, comprehensive). Packet content is not checked |
| `-p` | promiscuous | Partial | Only BLE mode conflicts (release). Never run alone; a useful test needs an active connection |
| `-a[addr]` | get/set access address | Not covered | Get/set needs no peer; `-r` clears it |
| `-s<mac>` | faux peripheral mode | Hard | Transmits RF; needs a central peer |
| `-t<mac[/mask]>`, `-tnone` | set/unset follow target | Not covered | Parsing and the set path are testable; a useful filter test needs a peer |
| `-i` / `-j` | interfere once (`-j` alias) | Hard | Jams a live connection (transmits RF) |
| `-I` / `-J` | interfere continuously (`-J` alias) | Hard | Same as `-i` |
| `-U<index\|serial>` | select device | Covered | Every btle case uses a serial |
| `-r<file>` | PCAPNG capture | Covered | BLE CRC transitions: the header and link type 256 are checked (smoke with `-n`, comprehensive with `-n` and `-f`). `-r -` (stdout) and packet content are not tested |
| `-q<file>` | PCAP (LE_LL_WITH_PHDR) | Covered | As `-r`: pcap with link type 256 |
| `-c<file>` | PCAP (PPI) | Covered | As `-r`: pcap with link type 192 |
| `-A<37-39>` | advertising channel | Covered | 37 in smoke; 37, 38, and 39 in comprehensive. An invalid index is not tested |
| `-v[01]` | get/set CRC check | Covered | Get, set, and applied by a mode (smoke); `-v0` and repeated `-v` (comprehensive) |
| `-x<0-32>` | allowed access-address errors | Not covered | Range check (>32 rejected) is host-side and easy |

Combinations to test:

- `-f`, `-n`, `-p`: any two or all three are rejected: Covered (BLE mode conflicts, release).
- `-v[01]` applied after the mode starts, overriding an opposite preset: Covered (smoke, comprehensive).
- Repeated `-v` is last-wins: Covered (comprehensive).
- Repeated capture options are first-wins and the extra file is never created (`-r -r`, `-q -c`, `-c -q`): Covered (comprehensive).
- `-r` together with `-q` or `-c` (separate handles; are both written?): Not covered.
- Capture with each CRC value and mode: Covered (comprehensive).
- `-i`/`-I` without `-f`/`-p`: Not covered (should be rejected or ignored; check source).
- `-a`/`-v` get and set without a mode do not start sniffing: Partial (covered for `-v` only).
- `-A` with `-p` or `-f` after a follow hop: Not covered (needs a peer).

## ubertooth-debug

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-U<index\|serial>` | select device | Covered | Debug register read (fast) |
| `-r <reg>[,<reg>...]` / `-r <start>-<end>` | read CC2400 registers | Partial | One register (`0x00`, fast only). Lists and ranges are not tested |
| `-v<0-2>` | verbosity | Not covered | |

Combinations: register list and range output: Not covered. `-v0`/`-v2` output format: Not covered. Debug register read is not in the release plan: Not covered.

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
| `-v` | verbose to stderr | Not covered | |
| `-g` | feedgnuplot output | Not covered | |
| `-G` | 3D feedgnuplot output | Not covered | |
| `-d <file>` | write output to a file | Not covered | |
| `-l <2402-2480>` | lower frequency | Not covered | |
| `-u <2402-2480>` | upper frequency | Not covered | |
| `-U <index\|serial>` | select device | Not covered | Spectrum analyzer and Spectrum RSSI sanity are catalog-only |

Combinations: `-l` > `-u` rejected: Not covered. `-g` vs `-G` output shape: Not covered. Every reported frequency falls between `-l` and `-u`: Not covered.

## ubertooth-specan-ui (Python, GUI)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help (argparse) | Not covered | Testable without a display |
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
| `-V` | version | Not covered | Easy |
| `-U <0-7>` | select device | Not covered | |

Combinations: `-u` without `-l` rejected: Not covered. `-z -t N` exits after N s: Not covered. `-i` replay into `-r`/`-q`: Not covered.

## ubertooth-dump

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-b` | raw bitstream only | Not covered | |
| `-c` | classic modulation | Not covered | |
| `-l` | LE modulation | Not covered | |
| `-d <file>` | write to a file | Not covered | |
| `-U<0-7>` | select device | Not covered | |

Combinations: `-c` with `-l` (last-wins or rejected?): Not covered. Output size over a bounded run: Not covered.

## ubertooth-tx

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 1) |
| `-l <LAP>` | target LAP | N/A | The tool does nothing (its usage text says so) |
| `-u <UAP>` | target UAP | N/A | Same |
| `-t <s>` | timeout | N/A | Same |
| `-V` | version | Not covered | Easy |
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
| `-V` | version | Not covered | Easy |
| `-U <0-7>` | select device | Not covered | |

## ubertooth-ego

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-f` | follow mode | Hard | Needs an E-GO skateboard (backlog) |
| `-r` | continuous RX on one channel | Not covered | Runs without a peer (no packets expected) |
| `-i` / `-j` | interfere (`-j` alias) | Hard | Transmits RF |
| `-c <2402-2480>` | RX channel (MHz) | Not covered | Used with `-r` |
| `-l <1-48>` | capture length | Not covered | The range check is host-side |
| `-a <code>` | access code | Not covered | |
| `-U <index\|serial>` | select device | Not covered | |

Combinations: two or more of `-f`/`-r`/`-i` (last-wins on `do_mode`?): Not covered.

## ubertooth-ducky

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-q[uuid]` | send a quack | Hard | Transmits RF; needs an Uberducky (backlog) |
| `-b` | Uberducky bootloader | Hard | Transmits; changes the peer's firmware state |
| `-A <37-39>` | advertising channel | Not covered | The range check is host-side |
| `-a <bdaddr>` | source address | Hard | Only matters with `-q` |
| `-s` | sarcastic output (undocumented) | N/A | Cosmetic |
| `-U <index\|serial>` | select device | Hard | Only matters with `-q` |

## ubertooth-scan (BlueZ builds only)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Not covered | Not in Host CLI surface (BlueZ-only build); exit 1 |
| `-s` | BlueZ HCI scan | Hard | Needs a Bluetooth adapter and discoverable peers |
| `-x` | extended inquiry | Hard | Same |
| `-t <s>` | scan time | Hard | Same |
| `-e <0-4>` | max access-code errors | Hard | Same |
| `-b <hciN>` | HCI device | Hard | Same |
| `-U<0-7>` | select device | Hard | Same (Bluetooth inquiry scan is in the backlog) |

## ubertooth-follow (BlueZ builds only)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Not covered | Not in Host CLI surface; exit 1 |
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
| `--extcap-interfaces` | list interfaces | Not covered | Host-side; easy |
| `--extcap-dlts` | list DLTs | Not covered | Host-side; easy |
| `--extcap-config` | config options | Not covered | Host-side; easy |
| `--capture` | start a capture | Not covered | Needs a device; check the FIFO header |
| `--extcap-interface=` | interface | Not covered | |
| `--fifo=` | output FIFO | Not covered | |
| `--channel=` | advertising channel | Not covered | |

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

1. Host-only argument errors (no device): `ubertooth-util -C79`, `-c2401`,
   `-U all -a5`; `ubertooth-btle -x33`, `-A40`; `ubertooth-ego -l49`; `ubertooth-ducky -A36`;
   `ubertooth-rx -u 12` without `-l`; `ubertooth-specan -l 2480 -u 2402`. Each must exit
   nonzero with no device change.
2. Version and count: `ubertooth-util --host-version`, `-N`; `ubertooth-rx -V`,
   `ubertooth-tx -V`, `ubertooth-afh -V`.
3. `ubertooth-dfu -s` (suffix a binary; host-only) and `-u` read-back compared with the last
   flashed image.
4. Implement Spectrum analyzer: `ubertooth-specan -l/-u` (bounded by `timeout`),
   with every frequency in range, plus `-d`, `-g`, `-G` output shape.
5. `ubertooth-btle -a` get/set/reset, `-tnone`/`-t<mac>` set path, `-r -` to stdout,
   and `-r` combined with `-q`.
6. `ubertooth-debug -r` list and range, `-v0`/`-v2`; add Debug register read to the release plan.
7. Raw receive replay: `ubertooth-rx -i <fixture>` with `-r`/`-q`/`-d`. Needs a public, synthetic fixture.
8. Implement BLE monitor / Classic Bluetooth receive (`ubertooth-rx -z -t N`,
   `ubertooth-dump -c/-l -d`, `ubertooth-ego -r -c`), checking exit and output shape, not content.
9. Add Ubertooth util conflicts to the release plan once fork issue #82 is fixed;
   add `-f`/`-i` rejection cases only if no accepted pair can reach those modes.
10. Headless `ubertooth-specan-ui -h` and argument checks; `btle-extcap --extcap-interfaces/--extcap-dlts/--extcap-config`.
11. Add `ubertooth-scan -h` and `ubertooth-follow -h` to Host CLI surface on BlueZ builds.
12. Peer-based tests from the backlog (known BLE advertiser, AFH map,
    piconet follow, two-board range test), once fixtures are registered.
