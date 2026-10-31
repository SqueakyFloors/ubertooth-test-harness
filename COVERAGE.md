# Ubertooth host CLI test coverage

This file tracks which Ubertooth host command-line tools and options the
[Ubertooth test harness](https://github.com/SqueakyFloors/ubertooth-test-harness)
exercises, so we can pick the next tests to write. Update it in the same PR as any test
that changes coverage. Options come from each
tool's `getopt` string and usage text on this fork's `main`. Coverage comes
from the harness catalog (`ubertooth_harness/catalog.py`,
`ubertooth_harness/transitions.py`, `ubertooth_harness/util_cases.py`,
`ubertooth_harness/receive_cases.py`, `ubertooth_harness/dfu_cases.py`,
`ubertooth_harness/refusal_cases.py`, `ubertooth_harness/dfu_host_cases.py`,
`ubertooth_harness/util_refusal_cases.py`), its plans (`testplans/*.yaml`) and its
preflight, flash, and guard code. Profiles: **smoke** runs in the `fast` plan,
**comprehensive** in the `release` plan; tests without a profile name their
plan. The `mac` plan picks per test what a Mac host changes (see the README),
so it runs both profiles.
The `host` plan holds the tests that need no Ubertooth (Host CLI surface, Raw
receive replay, Refused command lines, Util mode refusals, DFU image checks,
and Python tools smoke); CI runs it against the tools it builds.
Status legend:
**Covered**: a harness test runs the option and checks its output or effect.
The test and profile are named.
**Partial**: the option runs, but only some of its behavior is checked, or the
test is in no plan. The notes say what is missing.
**Not covered**: no harness test runs the option.
**Hard**: needs an RF peer, a GUI, RF transmission, firmware other than
`bluetooth_rxtx`, a second device, or is destructive. The notes say which.
**N/A**: nothing to test (for example a no-op).
Tests that are only catalog entries (every `backlog.yaml` item, including
`BLE monitor` and the soaks, and the `mac-polling` soak) have no
implementation yet. They count as Not covered. Only `runnable: false` plans
may name them; `validate-plan` fails on any other plan that does.

Harness-wide checks that use the CLIs:

- **Preflight** (every run): the `Host tools version:` line of
  `ubertooth-util -V` must match the resolved host sources; forks before the
  one `-V` answer (ubertooth#813) are asked `-H` instead, there and in the
  build's verify step only: every other check wants this tree's tools. For each selected board, `ubertooth-util -U <serial>` runs `-v`
  (firmware revision and API), `-V` (the build string its `Firmware build:`
  line carries), and `-s`, one command each.
- **Guards** (stateful tests): every other registered board on the host is read
  before and after the test, with queries only, one command per read:
  `ubertooth-util -v`, `-V`, `-C`, `-a`, `-z`, `-d`, and then
  `ubertooth-btle -k`. The `ubertooth-btle -k` read is skipped unless
  the board's firmware API is at least the host API.
- **Flash reliability** (`slow` and `mac` plans): repeated `ubertooth-dfu -U <serial> -d <image> -r`.
  Each flash is verified with `ubertooth-util -v`.
- **DFU images** (smoke in the `slow` and `mac` plans, comprehensive in the
  `firmware` plan): writes the test image, reads it back, and tries bad images on the
  selected board; every case ends with the test image read back and verified.
- **DFU image checks** (no Ubertooth; smoke in the `host` and `fast` plans and
  CI, comprehensive in `release`): `ubertooth-dfu -d <image> -r` and `-s` with
  `UBERTOOTH_TEST_NO_DEVICES=1` on a synthetic image; the test build only for
  `sign-test-build`, which is not applicable without one (left out before
  the run; README "Host requirements").
- **Linux only** (need BlueZ; not applicable on macOS, decided at planning, not
  skipped): `ubertooth-scan -h` and `ubertooth-follow -h` (Host CLI surface),
  and every `follow` and `scan` case of Refused command lines. A Linux `--build` must
  install both tools into the run prefix (the build fails otherwise), so these
  cases always run there ([#40](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/40)). `ubertooth-specan-ui -h`
  (Python tools) is not applicable without `--build --with-python`.
- **Exclusive Ubertooth topology** (`exclusive` plan, Linux, power control):
  runs `ubertooth-util -s`, `ubertooth-util -v`, and `ubertooth-btle -f` with no
  `-U` while only one board is powered.
- **MAC polling soak** (`mac-polling` plan; planned, catalog-only): long
  `ubertooth-btle -f` without `-U` on a single board, with a known advertiser.
  Not implemented yet.
- **One argument rule** (fork 8b58f75 for `ubertooth-util`, 0b63b01 for every
  other tool): a stray word is refused with `error: unexpected argument` and
  no device change on every built tool (util, btle, afh, debug, dfu, dump, ego,
  ducky, rx, specan, tx): Argument validation (one stray per tool for afh,
  debug, dfu, dump, tx in smoke; the rest comprehensive). `ubertooth-follow` and
  `ubertooth-scan`, built only with BlueZ: Refused command lines (no Ubertooth,
  so it runs in CI; Linux only, not applicable on macOS), a stray word on each
  in smoke; comprehensive adds `-h stray` (refused, no usage), a bare `-`,
  and `-- -x`, each refused naming the word
  ([#100](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/100)).
  Optional values as the next word are covered where they can be read back
  without RF: `ubertooth-util -c -C -a -z -l -d` (Radio setting transitions),
  `ubertooth-btle -k` (BLE CRC transitions) and `-a` (BLE settings).
  `ubertooth-util -q <n>` and `ubertooth-ducky -q <uuid>` are not: the LED
  specan threshold cannot be read back, and ducky `-q` transmits.

## ubertooth-util

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (fast, release): exit 0 and tool name in the output |
| `-U<index\|serial>` | select device | Covered | Every device test selects by serial. Repeated `-U` (index 99, then serial) names two boards for one command and is refused with exit 1 and `error: option '-U' given more than once`, before either is opened (util compositions, comprehensive; the last one used to be selected). Multi-board selection (smoke): each `-U<index>` opens the board that index names in `-U all -s`. Comprehensive: an index past the last board is refused with exit 1 and `no Ubertooth at index <n> (<n> attached)` (it opened another device before `0c7e9c0`, [ubertooth#188](https://github.com/SqueakyFloors/ubertooth/issues/188)) |
| `-U all` | identity queries for every board | Covered | Multi-board selection (smoke): `-U all -s` lists every registered board, and only those, with indices in order; comprehensive: `-U all -bvps` gives four lines per board. Each line carries the label the single-board report prints with the board's index in it, `Serial No (0): <serial>`, since fork [#610](https://github.com/SqueakyFloors/ubertooth/issues/610). `-V` is not one of the queries the listing can show: it is the version command, so `-U all -V` is not refused and answers for every board itself, with its own labels (`Firmware build (0): ...`), also covered there (comprehensive). Rejected with a setter or mode: Argument validation (comprehensive). `-U all` with no query, and `-U all -N` with nothing else beside it, are both refused with `error: -U all needs one of the read-only queries -b, -v, -p or -s to list`, before `-N` prints anything (the count is not a per-device query, and a listing is what `-U all` asks for): Not covered |
| `-N` | count devices | Covered | Ubertooth count (fast, release): equals the registered boards on the host. With `-i` it prints the count and the command carries on (fork `db5649b`; it dropped the mode before, [ubertooth#332](https://github.com/SqueakyFloors/ubertooth/issues/332)): `-N -i` and `-i -N` print `0`, then `-i` finds no board, exit 1: Util mode refusals (comprehensive) |
| `-v` | firmware revision | Covered | Ubertooth identity (fast, release), preflight, util compositions (smoke) |
| `-V` | version report | Covered | Host version and Host CLI surface (fast, release): the `Host tools version:` line, which needs no board, and which preflight and the build's verify step check against the resolved sources (falling back to `-H` on forks before the one `-V` answer, [ubertooth#813](https://github.com/SqueakyFloors/ubertooth/issues/813)). Ubertooth identity requires the whole answer on one board as the four consecutive lines the fork prints, in its order: the host version, the libbtbb path, `Firmware version:`, and `Firmware build:` carrying the build string. Preflight checks that build string alone, against the resolved sources, with a pattern of its own that is not tied to the line. Since fork `f898fe5` it is the version command and ends the command, so it composes with nothing: util compositions (smoke) requires `-V -s` and `-s -V` to print that answer and no serial line, and Multi-board selection (comprehensive) requires `-U all -V` and `-V` with no selector to report every board, indexed |
| `-s` | MCU serial | Covered | Ubertooth identity, preflight, cleanup identity check |
| `-p` | part ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-b` | board ID | Covered | Ubertooth identity, util compositions (smoke) |
| `-a[0-7]` | get/set PA level | Covered | Get and set: Radio setting transitions (smoke). `-a0` sets level 0 and reads back (smoke; fork `4ed4a1d`, [ubertooth#161](https://github.com/SqueakyFloors/ubertooth/issues/161); the harness requires it). `-a 5` and `-a 0` as the next word, a repeated `-a` (refused, PA level unchanged), `-a<level> -a` (sets, then reports the new level; [ubertooth#228](https://github.com/SqueakyFloors/ubertooth/issues/228)), and invalid `-a9`: comprehensive. The full-reset case sets PA 0 before `-r`, and a board found at PA 0 runs every case ([#5](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/5)). `-a8`, `-a 8`, and `-aa` rejected with no change; `-a 7x` refused as a stray word: Argument validation (comprehensive). Boot and reset default is 7 (docs: [ubertooth#160](https://github.com/SqueakyFloors/ubertooth/issues/160)) |
| `-c[2402-2480]` | get/set channel (MHz) | Covered | Radio setting transitions (smoke), including `-c 2426` as the next word. A repeated `-c`, and a value given to both `-c` and `-C`, are refused with the channel unchanged: comprehensive. Out-of-range `-c2401`/`-c2481`/`-c 2481`, `-cabc`, and `-c2440x` rejected with no change: Argument validation (comprehensive; `-c2401` smoke). A set and a read in one command, `-c<MHz> -c` and `-c -c<MHz>`, set the channel and report the new one (comprehensive; fork `5b45b3e`, [ubertooth#228](https://github.com/SqueakyFloors/ubertooth/issues/228)) |
| `-C[0-78]` | get/set channel (index) | Covered | Get: smoke. Set and `-C 24` as the next word: comprehensive; a repeated `-C`, and `-c` with `-C` in either order, are refused with the channel unchanged. `-C79`, `-Cabc`, and `-C-1` rejected with no change: Argument validation (comprehensive; strict parsing since `658730c`, [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-z[level]` | get/set squelch | Covered | Radio setting transitions (smoke); `-z <n>` sets silently (no `Setting squelch` line), as every setter in a stateful case prints nothing on stdout (fork `fcadf34`, [ubertooth#326](https://github.com/SqueakyFloors/ubertooth/issues/326)); `-z -60` as the next word, and a repeated `-z` (refused, squelch unchanged): comprehensive. `-zabc`, `-z128`, `-z-129`, `-z -129`, `-z -` (stray), and `-z` with `-I`, `-q`, or `-x` rejected with no change: Argument validation (comprehensive; [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172), [ubertooth#159](https://github.com/SqueakyFloors/ubertooth/issues/159)). `-z` and `-z-60` with `-i` refused with no Ubertooth: Util mode refusals (smoke) |
| `-l[0-1]` | get/set USR LED | Covered | Get: util compositions (smoke). Set across a mode stop, `-l 1` as the next word, a repeated `-l` (refused, the LED unchanged), and `-l<state> -l` (sets, then reports it; [ubertooth#228](https://github.com/SqueakyFloors/ubertooth/issues/228)): comprehensive. `-l2` and `-lx` rejected with no change: Argument validation (comprehensive; [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)) |
| `-d[0-1]` | get/set all LEDs | Covered | `-d1` and the `-d` readback in every setting case (smoke); `-d 1` as the next word, a repeated `-d` (refused, every LED unchanged), and `-d<state> -d` (sets, then reports all three; [ubertooth#228](https://github.com/SqueakyFloors/ubertooth/issues/228)): comprehensive. `-d2` and `-dx` rejected with no change: Argument validation (comprehensive; [ubertooth#172](https://github.com/SqueakyFloors/ubertooth/issues/172)). The LED sampler in Ubertooth util modes reads `-d` repeatedly; a failed read (a stall while LED specan runs, [ubertooth#213](https://github.com/SqueakyFloors/ubertooth/issues/213)) is retried once and the retry is recorded on stderr, and a second failure in a row fails the case |
| `-S` | stop current operation | Covered | Starts and ends every stateful case (smoke); `Stopping ubertooth device <selector>` on stderr and nothing on stdout (fork `fcadf34`, [ubertooth#326](https://github.com/SqueakyFloors/ubertooth/issues/326)) |
| `-r` | full reset | Covered | Full reset returns the boot defaults (Radio setting transitions, comprehensive). Clears the range-test result (Ubertooth util modes, smoke). `Resetting ubertooth device` on stderr, nothing on stdout (fork `fcadf34`) |
| `-x` | xmas lights mode | Covered | Used to enter an active mode before a stop (Radio setting transitions, smoke) |
| `-I` | identify (flash LEDs) | Covered | Ubertooth util modes (smoke): returns at once with one line on stderr, `note: the board is blinking; stop it with ubertooth-util -U <serial> -S or any mode option`, and no `Flashing LEDs` line (fork `fcadf34`, [ubertooth#326](https://github.com/SqueakyFloors/ubertooth/issues/326)); all LEDs blink (`-d` is sampled by `ledblink.py` with pauses jittered over one blink period until a whole-on and a whole-off sample have both appeared, at most 60 samples or 10 s on a monotonic clock; a read still running at 10 s is stopped with SIGTERM, so sampling ends within 12 s), channel and PA kept, `-S` ends it (LEDs off, idle reset), originals restored. Comprehensive: `-I` ends a running `-q` and `-q` ends `-I` (the new mode runs at 2441 MHz: ending the prior mode ran the idle reset), `-s -I` |
| `-q[20-120]` | LED spectrum analyzer, threshold -N dBm | Covered | Ubertooth util modes (smoke): `Entering LED specan mode (threshold -N dBm)` on stderr, not stdout (fork `fcadf34`); `-q120` (-120 dBm) lights every LED, channel and PA kept, `-S` runs the idle reset (LEDs off, 2441 MHz, squelch -120, PA kept; [ubertooth#191](https://github.com/SqueakyFloors/ubertooth/issues/191), fixed in `aa4f620`), originals restored. Comprehensive: `-q20` and bare `-q` (-85 dBm) run until `-S` with the same reset. `-q0`, `-q19`, `-q121`, `-q183` (where the old int8 threshold wrapped, [ubertooth#189](https://github.com/SqueakyFloors/ubertooth/issues/189)), `-qabc` rejected: Argument validation (comprehensive) |
| `-t` | continuous TX test | Hard | Transmits RF. Only the conflict rejections run it (Ubertooth util conflicts, release) |
| `-n` | start range test | Partial | Two-device range test (`range` plan; two boards on one Linux host, `allow RF transmission: true`): after `-r` on both boards and both set to 2410 MHz, `-e` on the responder (which must answer USB with its RX LED on before `-n`), then `-n` on the sender prints `Starting range test` on stderr and nothing on stdout; the sender's `-m` must report a result within 10 s, with request and reply PA levels at most the plan's `maximum PA level` (3) and copy numbers 0-15, and the sender must be idle afterwards with no `-S` (LEDs off, back to 2441 MHz: leaving the mode ran the idle reset); a mismatch fails naming the field that differs. Passes on fork main since [ubertooth#601](https://github.com/SqueakyFloors/ubertooth/pull/601) (before it no result, [ubertooth#26](https://github.com/SqueakyFloors/ubertooth/issues/26), then `mismatch on byte 17`, [ubertooth#483](https://github.com/SqueakyFloors/ubertooth/issues/483)): 9 of 10 bench runs at `699960b`; about one in ten ends in `mismatch on byte 0` ([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)); since fork `0684c11` ([ubertooth#445](https://github.com/SqueakyFloors/ubertooth/pull/445)) both boards keep answering USB and clean up, before it both stopped answering until power was removed. Also the conflict rejections. Capture known BLE advertiser (`ble` plan, needs an nRF52840 DK advertiser): packet content checked, `-n -A37/38/39 -q` holds the advertiser's packets (address, PDU type, payload, counter, CRC) on that channel's RF channel only, and none while it advertises on another channel |
| `-e` | repeater mode | Partial | Two-device range test: `Starting repeater` on stderr, nothing on stdout; once it starts, and before the sender's `-n`, the responder must answer USB with its RX LED on and keep the 2410 MHz channel; after the sender's wait it must still answer with RX on, its `-m` must still say `no range test result yet`, and `-S` must stop it (LEDs off, back to 2441 MHz). Since fork `0684c11` ([ubertooth#445](https://github.com/SqueakyFloors/ubertooth/pull/445)) the repeater answers USB and stops on `-S`; before it, it answered no USB request until power was removed ([ubertooth#26](https://github.com/SqueakyFloors/ubertooth/issues/26)). Also the conflict rejections |
| `-m` | show range test result | Partial | Ubertooth util modes (smoke): after `-r`, exit 0, `no range test result yet`, and no result values (it said "invalid range test result" before `7180f74`, [ubertooth#190](https://github.com/SqueakyFloors/ubertooth/issues/190)). A real result: Two-device range test (the result lines, PA levels at most the plan's maximum, copy numbers 0-15, and a mismatch fails); seen on hardware since fork #601 (PA 0, copy numbers 0-6 on a bench pair) |
| `-f` | DFU mode (removed) | Covered | Removed in fork `db5649b` (use `ubertooth-dfu`). `-f` alone and `-f -i` are refused as an unknown option, `error: unrecognized option '-f'`, exit 1, with no Ubertooth: Util mode refusals (smoke). The deprecation note above the `-U all` refusal ([ubertooth#333](https://github.com/SqueakyFloors/ubertooth/issues/333)) went with it. Capture known BLE advertiser (`ble` plan): `-f -A37 -r` (smoke; 37, 38, 39 comprehensive) captures the advertiser's advertising packets. Capture known BLE connection (`ble` plan, `connection` suite): `ubertooth-btle -f -r` follows a connection between two registered nRF52840 DKs; the CONNECT_IND parameters and the followed data packets (including ones longer than 27 bytes, so Data Length Extension) are checked |
| `-i` | ISP mode | Hard | Leaves the application firmware; recovery needs ISP tooling, so never run alone. Its refusals run with no Ubertooth: Util mode refusals (below) |
| `-w` | watchdog record | Covered | Every run reads it on each Ubertooth it binds (guards included) after preflight, between cases of a long test, between the writes of a flash soak, after each test, and at the end of the run; the parser covers each line form from captured output, including an older host build that prints neither the transfer-error nor the radio-wait line. A board that reset itself is a warning, a board that does not come back is a failure, firmware without the request reads as not reported, and a plan may set `max_watchdog_resets` per test (README, "Firmware watchdog") |
| `-9` | firmware test command (undocumented) | N/A | Removed in fork `5ac22a8` ([ubertooth#350](https://github.com/SqueakyFloors/ubertooth/issues/350)) |

Combinations to test:

- Read-only queries in one command (all nine composable reports, every compatible pair): Covered (smoke, comprehensive). `-V` is not one of them, because it ends the command: `-V -s` must answer the version and print no serial line (smoke).
- A set option given a value twice is refused, and the setting is unchanged (`-c -C -a -z -l -d`, and `ubertooth-util -U`): Covered (comprehensive). `opt_check_once()` in the fork refuses it before any device is opened; the last value used to win.
- `-c` and `-C` are one channel setting written two ways, so a value given to both is refused in either order, with the channel unchanged: Covered (comprehensive).
- Rejected set followed by a report (`-a9 -v`): refused with exit 1 before the report runs (strict parsing, fork `658730c`): Covered (comprehensive).
- Settings kept by a stop from idle and reset by an active-mode stop: Covered (smoke; USR LED comprehensive).
- Two or more modes rejected before any mode starts (`-q -n -e -t -x -I`): Covered (release). The `Ubertooth util conflicts` test checks the fork's `error: options ... cannot be combined` for every pair (fork #82, fixed in #134); it passed on hardware at fork 3b22c95 ([#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25)). It keeps `allow RF transmission: true`, since an accepted pair would transmit.
- `-i` combined with another mode is rejected: Covered with no Ubertooth (Util mode refusals; `host` and `fast` plans, and CI). Every command runs as `env UBERTOOTH_TEST_NO_DEVICES=1 ubertooth-util ...`, so an accepted pair would find no board instead of stranding one. Smoke: `-i` with each of `-q -n -e -t -x -I`, in both orders, refused with exit 1 and `error: options <modes in the fork's order> cannot be combined; give only one of -q -n -e -t -x -I -i`, with no `no board is selected` note; `-z`, `-z-60` with `-i`, in both orders, refused with `error: -z cannot be combined with -i`; `-f` and `-f -i` refused as an unknown option (fork `db5649b` removed `-f`). Comprehensive adds `-x -i -q` and `-i -q -x` (all three named), `-i -i -x` (each named once), `-z -x -i` (the mode refusal comes first), `-x -i stray` (the stray word is refused), `-N -i` and `-i -N` (the count `0`, then `-i` runs and finds no board: `error: no Ubertooth devices found`, exit 1; [ubertooth#332](https://github.com/SqueakyFloors/ubertooth/issues/332) fixed by `db5649b`, the line libubertooth's since `753269b`), and `-U all -i` (the `-U all` refusal alone). A skip when the built libubertooth predates the switch (fork `b6d8ede`).
- `-U all` with a setter (`-a5`) or mode (`-x`) is rejected with no change: Covered (Argument validation, comprehensive).
- Read-only queries without `-U` while several boards are attached list every board: Covered (Multi-board selection: `-s` smoke, `-bvps` comprehensive; `-V` reports every board itself, comprehensive). Another option without `-U` (`-C`) refuses to pick a board and lists them: comprehensive, as `error: multiple Ubertooth devices found; select one with -U <index|serial>` and one indented `Device <index>: <serial>` line per board, since fork `753269b` (the line used to address the reader, each board's serial carried a `Serial No:` label under its index, and an unprefixed `could not open Ubertooth device` followed). The single-board path is covered by Exclusive topology.
- `-z` combined with a mode: was silently ignored ([ubertooth#159](https://github.com/SqueakyFloors/ubertooth/issues/159)); fork main refuses it (`4ed4a1d`). Covered with `-I`, `-q`, `-x` (Argument validation, comprehensive) and, with no Ubertooth, `-i` (Util mode refusals, smoke); `-n -e -t` are not used because they transmit if accepted.
- A value given as the next word sets the same as an attached one (fork #156): Covered for `-c -C -a -z -l -d` and `-a 0`, each preset to a different value first and read back, and all of `-c -a -z -d -l` as next words in one command (Radio setting transitions; `-c 2426` smoke, the rest comprehensive). A following option is not taken as a value (`-c -b`): the read-only pairs (util compositions).
- A stray word is refused with no device change (fork #156): after a set (`-c2470 stray`), after a mode (`-x stray`), a bare `-`, a word after `--`, and a next word that is not a value (`-a 7x`): Covered (Argument validation, comprehensive).

## ubertooth-btle

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0 since fork `e4f1dc5`, fork #377; it was 1) |
| `-f` | follow connections | Covered | BLE CRC transitions (smoke: CRC on, channel 37; comprehensive: every CRC value × channel and capture format). Exclusive topology runs it without `-U`. Following a real connection is not checked (needs a peer) |
| `-n` | advertisements only | Covered | BLE CRC transitions and capture cases (smoke, comprehensive). Packet content is not checked. Raw receive (comprehensive): `-n -A37 -q` after a `dump -d` captures no basic-rate packet (every packet on RF channel 0; [ubertooth#318](https://github.com/SqueakyFloors/ubertooth/issues/318)) |
| `-p` | promiscuous | Partial | BLE settings (comprehensive): a bounded receive-only run, then `-S`, resets LEDs, channel 2441 MHz, and squelch -120 (PA kept); access address and CRC are restored afterwards. BLE mode conflicts (release). Following a connection needs a peer |
| `-a[addr]` | get/set access address | Covered | BLE settings (smoke: set/read back, attached and as the next word; comprehensive: reset by `util -r`, `-a -k` reports both, and `-azzzz` is refused with no change; [ubertooth#171](https://github.com/SqueakyFloors/ubertooth/issues/171), fixed in `09e9a46`). `-a zzzz` rejected and `-a -` refused as a stray: Argument validation (comprehensive) |
| `-s<mac>` | faux peripheral mode | Hard | Transmits RF; needs a central peer |
| `-t<mac[/mask]>`, `-tnone` | set/unset follow target | Partial | Set, masked, and cleared: BLE settings (smoke); malformed MAC rejected: Argument validation. The filter itself needs a peer |
| `-i` / `-j` | interfere once (`-j` alias) | Hard | Jams a live connection (transmits RF) |
| `-I` / `-J` | interfere continuously (`-J` alias) | Hard | Same as `-i` |
| `-U<index\|serial>` | select device | Covered | Every btle case uses a serial |
| `-r<file>` | PCAPNG capture | Covered | BLE CRC transitions: the header and link type 256 are checked (smoke with `-n`, comprehensive with `-n` and `-f`). Packet content is not tested. Refused command lines (no Ubertooth, smoke): a stray word, `-h` (exit 0 since fork `e4f1dc5`), or `-A 40` after `-r` creates no file and leaves an existing one unchanged; comprehensive adds `-f -n`. `-r -` is refused (comprehensive, with `-c`). Capture known BLE advertiser (`ble` plan): PCAPNG packet content decoded and checked against the advertiser |
| `-q<file>` | PCAP (LE_LL_WITH_PHDR) | Covered | As `-r`: pcap with link type 256. Refused command lines (smoke): as `-r`; `-n -q - -c <file>` prints `warning: ignoring extra capture file` (wording since fork `e4f1dc5`) on stderr, not into the stdout capture, and never creates the file; `-q` twice with a stray word is refused with `error: option '-q' given more than once` and creates neither file. Comprehensive adds `-a -a<address>`. Capture known BLE advertiser (`ble` plan): PCAP packet content decoded and checked against the advertiser |
| `-c<file>` | PCAP (PPI) | Covered | As `-r`: pcap with link type 192. Refused command lines (smoke): as `-r`; comprehensive adds `-c <file> -r -` |
| `-A<37-39>` | advertising channel | Covered | 37 in smoke; 37, 38, and 39 in comprehensive. `-A36`/`-A40` rejected: Argument validation (comprehensive). Capture known BLE advertiser (`ble` plan): with a real advertiser on each channel, `-A` picks only that channel's packets |
| `-k[01]` | get/set CRC check | Covered | Get, set, and applied by a mode (smoke); `-k0`, and `-k 1` and `-k 0` as the next word (comprehensive). `-k 2` rejected and `-k stray` refused: Argument validation (comprehensive). A repeated `-k` is refused with exit 1 and `error: option '-k' given more than once`, with the CRC setting unchanged (comprehensive; the last value used to win). The option moved from `-v` in [ubertooth#813](https://github.com/SqueakyFloors/ubertooth/issues/813); the deprecated `-v` alias is not covered |
| `-x<0-32>` | allowed access-address errors | Partial | `-x33` rejected (Argument validation); `-x0`/`-x32` accepted (BLE settings, comprehensive). The effect on capture needs traffic |

Combinations to test:

- `-f`, `-n`, `-p`: any two or all three are rejected: Covered (BLE mode conflicts, release).
- `-k[01]` applied after the mode starts, overriding an opposite preset: Covered (smoke, comprehensive).
- A repeated `-k` is refused and the CRC setting is unchanged: Covered (comprehensive).
- `-q` and `-c` share one PCAP handle, so either after the other is first-option-wins and the extra file is never created: Covered (comprehensive). The same option twice names two files for one capture and is refused: Covered (Refused command lines, no Ubertooth).
- `-r` together with `-q` (separate handles): both files written: Covered (comprehensive). `-r` with `-c`: Covered (comprehensive).
- Capture with each CRC value and mode: Covered (comprehensive).
- `-i`/`-I` without `-f`/`-p`: refused before any device is opened since fork `e4f1dc5` (fork #377; it was accepted and ignored): Not covered. Hard: with a mode it transmits.
- `-a`/`-s` with `-f`, `-n` or `-p`: refused before any device is opened since fork `e4f1dc5`: Not covered.
- `-a`/`-k` get and set without a mode do not start sniffing: Covered (BLE settings); `-a -k` reports both (comprehensive).
- `-A` with `-p` or `-f` after a follow hop: Not covered (needs a peer).

## ubertooth-debug

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-U<index\|serial>` | select device | Covered | Debug register read (fast) |
| `-r <reg>[,<reg>...]` / `-r <start>-<end>` | read CC2400 registers | Covered | Debug registers: list (smoke), range and reversed range (comprehensive), order checked, nothing changed. Exit status checked (fixed in `89afc1c`, [ubertooth#164](https://github.com/SqueakyFloors/ubertooth/issues/164)) |
| `-v<0-2>` | verbosity | Covered | Debug registers (comprehensive): `-v0` header only, `-v1` fields, `-v2` reserved fields; `-v3` rejected |

Combinations: list and range output: Covered. `-v0`/`-v2` output: Covered. `Debug registers` is in the release plan (comprehensive).

## ubertooth-dfu

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0 since fork `12211ce`, fork #406; it was 1) |
| `-d <file>` | write DFU image | Covered | Flash reliability (slow): repeated exact-verified writes. DFU images (smoke): write, then read back. Bad images (DFU images, comprehensive): a wrong CRC, a truncated file, a binary without a suffix, a text file, a suffix without `UFD`, and a suffix for another vendor or product must be refused with exit 1 before a device is opened (no "operation started", no "firmware programming attempt"), each with one `error:` line naming the file, what is wrong with it, and that the firmware will not be updated; the board must stay in its application with the same revision and flash (read back). Fixed in `e8edd15` ([ubertooth#198](https://github.com/SqueakyFloors/ubertooth/issues/198), [ubertooth#199](https://github.com/SqueakyFloors/ubertooth/issues/199)); at 3b22c95 every rejection left the board in the DFU bootloader and a wrong vendor or product was written. `-d <image> -r` exited 1 after a good flash at 6fee30c ([ubertooth#267](https://github.com/SqueakyFloors/ubertooth/issues/267), fixed in `89d9197`). DFU image checks (no Ubertooth, smoke, in CI): the same seven bad images, made from a synthetic image, are refused by `-d <image> -r` with exit 1 before any board lookup (no "no board is selected" note, no DFU stage log), each with its own reason in that one line, the image and its directory unchanged; a good image passes the check, which names the image and the revision from its firmware info block, and reaches the lookup. Comprehensive adds an empty file, 15 bytes, a suffix with no firmware, a suffix length of 200 in an 80-byte file, DFU version 2.0, an image with no firmware info block, a directory and a missing file (all refused before the lookup), and DFU 1.1 and the `ffff:ffff` wildcard (accepted) |
| `-r` | reset after the operation | Covered | Flash reliability (slow) and DFU images, with `-d` and with `-u`; after `-u -r` the application must answer with the same revision. DFU image checks: with `-r`, `-d` names the image's expected revision (`DFU expected revision:`), read from the firmware info block the image carries; an image with no block there is refused before the board lookup (comprehensive) |
| `-U <index\|serial>` | select device | Covered | Flash reliability and DFU images (by serial; optionally alternating boards) |
| `-u <file>` | read firmware from the device | Covered | DFU images (smoke): after `-d <test image> -r`, `-u <file> -r` must give 245760 bytes (flash 0x4000-0x3ffff) that start with the whole image, suffix included, padded with 0xff to 256 bytes. Comprehensive: every rejection is checked by a read-back. A read-back fails now and then (about 1 in 20) and leaves the board in DFU despite `-r` ([ubertooth#208](https://github.com/SqueakyFloors/ubertooth/issues/208)); `-u` truncates its file before it finds a device ([ubertooth#200](https://github.com/SqueakyFloors/ubertooth/issues/200) item 4, fixed). Refused command lines (no Ubertooth, smoke): a stray word, `-h`, or a later `-s other.txt` creates no file, no temporary file, and leaves an existing one unchanged; comprehensive: `-u <file> -s <missing .bin>` and `-u <file> -d <bad image>` (exit 1, no device) leave the file and no temporary file |
| `-s <file>` | add DFU suffix to a binary | Covered | DFU images (smoke): the test image without its suffix, suffixed with `-s`, must equal the build's `.dfu` (VID:PID 1d50:6003, bcdDFU 0100, CRC). Comprehensive: a `.dfu` is refused (exit 1, unchanged); a `.bin` that already has a suffix is refused ("already has a DFU suffix, so ... was not written", nothing written) and a read-only `.bin` is suffixed (fork `9d710fe`, [ubertooth#200](https://github.com/SqueakyFloors/ubertooth/issues/200)). Refused command lines (smoke): a stray word, `-h`, or a second `-s other.txt` writes no `.dfu` and leaves an existing one unchanged. Comprehensive: `-s <file.bin> -d <bad image>` neither creates nor replaces the `.dfu` (fixed in f2e06fa, [ubertooth#286](https://github.com/SqueakyFloors/ubertooth/issues/286)). DFU image checks (no Ubertooth, smoke, in CI): `-s` of a synthetic binary gives exactly the suffix and CRC the harness computes; a read-only `.bin` is suffixed; a `.dfu` and a suffixed `.bin` are refused with nothing written; `-s` of the test build's binary gives the build's `.dfu` byte for byte when the run has a firmware build (not applicable otherwise). Comprehensive: a missing `.bin` and a `.bin` whose suffix has a wrong CRC write nothing; `-s` of an input it cannot read (a directory) leaves an existing `.dfu` alone (fixed in f2e06fa, [ubertooth#305](https://github.com/SqueakyFloors/ubertooth/issues/305)); the signed file goes in place through a temporary file (f2e06fa): only `<file>.dfu` is added, an earlier `.dfu` is replaced keeping its mode (600), a `.dfu` symlink is replaced by a regular file and its target left alone, signing into a read-only directory fails (`<file>.dfu: Permission denied`, exit 1, the directory unchanged; skipped as root), and `-s` with an accepted `-d <image> -r` writes the `.dfu` before the board lookup fails (exit 1). Not checked: `-s` of an empty `.bin` writes a 16-byte `.dfu` that `-d` refuses ("no firmware"); the docs do not say what it should do |

Combinations: `-d` without `-r` and `-u` without `-r` (board stays in DFU): Not covered (Hard: leaves the application firmware). A command line with no function (`-d`, `-u`, `-s` or `-r`), `-U <serial>` alone for one, is refused with `error: nothing to do: ...` and exit 1 before any device is opened since fork `12211ce` (it exited 0 having done nothing): Not covered. `-d` then `-u` of the same image: Covered (DFU images). A bad image or wrong suffix is rejected: Covered (DFU images, comprehensive; fails on builds without the ubertooth#198/#199 fix `e8edd15`). `-d -r` exit status after a good flash: Covered (every write; fails at 6fee30c, ubertooth#267, fixed in `89d9197`).

## ubertooth-specan

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0) |
| `-v` | verbose to stderr | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-g` | feedgnuplot output | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-G` | 3D feedgnuplot output | Covered | Spectrum analyzer (comprehensive): line format and range |
| `-d <file>` | write output to a file | Covered | Spectrum analyzer (comprehensive): binary 3-byte records checked by `specandump.py`; on Linux, `-d /dev/full` ends by itself with one `error: writing samples failed: ...` line and exit 1, nothing on stdout (fork `fcadf34`, [ubertooth#326](https://github.com/SqueakyFloors/ubertooth/issues/326); not applicable on macOS, which has no `/dev/full`); a `-d` file is opened once a board is open and its firmware accepted (fork `47bae592`), so with no Ubertooth a `-d` file in a missing directory is neither created nor mentioned and the command stops at the board lookup, exit 1 (Refused command lines, comprehensive); `-d -x.bin` writes the file `-x.bin` and nothing to stdout (fork `053363a`, [ubertooth#231](https://github.com/SqueakyFloors/ubertooth/issues/231)). `-d -` (binary samples on stdout): Not covered. Refused command lines (no Ubertooth, smoke): a stray word, `-h` (exit 0), or `-l 9999` after `-d` creates no file and leaves an existing one unchanged; comprehensive adds `-l` above `-u` and `-d` twice with `-h` |
| `-l <2402-2480>` | lower frequency | Covered | Spectrum analyzer (comprehensive) ranges; `-l 2401` rejected (Argument validation). A range change reported stale samples before `20bcf11` ([ubertooth#173](https://github.com/SqueakyFloors/ubertooth/issues/173)) |
| `-u <2402-2480>` | upper frequency | Covered | As `-l` |
| `-U <index\|serial>` | select device | Covered | Spectrum analyzer by serial, guard board unchanged |

The RSSI value is the CC2400 RSSI register, dBm + 54 (specan-ui adds -54). Spectrum RSSI sanity (fast) checks that it is live without a signal source: in a full-band sweep every frequency's noise floor is -115 to -80 dBm (up to 5 of the 79 frequencies may stay above -80 dBm, for a continuous emitter nearby; more fail, each named), the mean is negative (below -54 dBm), and there are at least three distinct values. Fork main `7bb07c9` on a Mac board (5 s, 32672 samples): floors -105 to -98 dBm, mean -97 dBm, 52 distinct values.

Stdout carries only the samples: `Ubertooth stopped`, errors and notes on stdout fail every sweep (fork `fcadf34`, [ubertooth#326](https://github.com/SqueakyFloors/ubertooth/issues/326)). Text output to a stream that takes no data (exit 1 after the closing flush) and `-d -` into a closed pipe (SIGPIPE, 141): Not covered.

Combinations: `-l` > `-u` rejected: Covered (Argument validation). `-g` vs `-G` output shape: Covered. Every reported frequency falls between `-l` and `-u`: Covered, also after a range change. Stopping a run resets the board (LEDs off, 2441 MHz, squelch -120, PA kept): Covered (Spectrum analyzer, comprehensive; [ubertooth#191](https://github.com/SqueakyFloors/ubertooth/issues/191), fixed in `aa4f620`).

## ubertooth-specan-ui (Python, GUI)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help (argparse) | Covered | Python tools: usage with `-U`, `-l`, `-u`, `--wifi`, `--padding`, no display; needs `run-suite --build --with-python` (not applicable without it) and numpy, qtpy, and a Qt binding in that Python (else skipped with the reason) |
| `-U <index\|serial>` | select device | Hard | Qt GUI |
| `-l <freq>` | lower frequency | Hard | GUI. The argument checks could run before the window opens |
| `-u <freq>` | upper frequency | Hard | GUI; `-u` < `-l` must be rejected |
| `--wifi[=ch\|a-b]` | Wi-Fi channel band | Hard | GUI |
| `--padding <MHz>` | band padding with `--wifi` | Hard | GUI |

Combinations: `--padding` without `--wifi`, `--wifi` with `-l`/`-u`: Not covered (Hard).

## ubertooth-rx

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0) |
| `-l <LAP>` | LAP to decode | Covered | Raw receive replay: `-i -l` keeps only that LAP's lines and derives the UAP from the fixture (smoke); `-l` with ID packets only gives no UAP, `-l -q` captures only that LAP (comprehensive). Follow Classic Bluetooth piconet (`classic` plan, needs two ESP32 Classic peers): live, `-l <master LAP> -u <master UAP>` reports ten or more packet lines and none of another LAP, `-l` alone recovers the UAP from the air (comprehensive), and `-l`/`-u` for the peer that is not the master reports nothing. |
| `-u <UAP>` | UAP to decode | Covered | Raw receive replay (comprehensive): `-i -l -u` with the right UAP confirms it (CLK6 0, hop reversal starts), a wrong UAP confirms nothing. Follow Classic Bluetooth piconet (`classic` plan): live, the registered master's UAP follows its piconet and the other peer's reports nothing (a survey's inferred UAP can be wrong, so the cases take it from the registry) |
| `-z` | survey mode | Covered | Classic Bluetooth receive (smoke): `-z -t N` exits 0 after N s; `Survey Results` and piconet address lines. Raw receive replay (comprehensive): `-i -z` lists exactly the fixture's two piconets, one with its UAP. Capture known Classic device (`classic` plan): live, the survey lists the LAP of the piconet two registered ESP32 peers make, and lists the other board's instead when their roles are swapped |
| `-i <file>` | input file instead of a device | Covered | Raw receive replay, no Ubertooth (smoke, also in CI): a synthetic fixture (`rxreplay.py`) decodes to the exact LAP/error/channel lines, decoy records (LE, DISCARD, channel 79) skipped. Comprehensive: empty file exits 0 with no lines; a cut last record is decoded up to the cut, then reported ("ends in a partial record: 58 of 68 bytes, ignored"), exit 1; a missing file exits 1 ("error: <file>: No such file or directory", fork `5ac22a8`); lines carry the recorded systime ([ubertooth#202](https://github.com/SqueakyFloors/ubertooth/issues/202)); a directory exits 1 with "error: reading <file> failed: Is a directory" ([ubertooth#203](https://github.com/SqueakyFloors/ubertooth/issues/203)); fixed in `a55ac89`, after 3b22c95 |
| `-c <ch>` | fixed BT channel | Covered | Classic Bluetooth receive (comprehensive): `-c 39`, every packet on channel 39. `-c 79` is refused (0-78, [ubertooth#155](https://github.com/SqueakyFloors/ubertooth/issues/155)): Refused command lines uses it as the bad value; the usage's "Default: 39" is wrong, the default sweeps ([ubertooth#186](https://github.com/SqueakyFloors/ubertooth/issues/186)) |
| `-e <0-4>` | max access-code errors | Covered | Classic Bluetooth receive (comprehensive): `-e 0` and `-e 4`, no packet over the limit. Raw receive replay (comprehensive): the fixture's one-bit-error access code is reported as `err=1` by default and dropped with `-e 0`. `-e 5` is accepted ([ubertooth#155](https://github.com/SqueakyFloors/ubertooth/issues/155)) |
| `-t <s>` | sniff timeout | Covered | Classic Bluetooth receive: every `ubertooth-rx` case must exit 0 by itself, no earlier than N-0.5 s and within N+5 s |
| `-r<file>` | PCAPNG capture | Covered | Classic Bluetooth receive (smoke): pcapng, link type 255. Raw receive replay (smoke): `-i -r` writes one packet per decoded line. Refused command lines (no Ubertooth, smoke): a stray word, `-h`, or `-c 79` after `-r` creates no file and leaves an existing one unchanged; `-r` twice with a stray word is refused with `error: option '-r' given more than once`, before the stray word and before any board lookup, and creates neither file |
| `-q<file>` | PCAP capture | Covered | Classic Bluetooth receive (comprehensive): pcap, link type 255. Raw receive replay (smoke): `-i -q` writes one packet per decoded line. Refused command lines (smoke): as `-r`; comprehensive adds `-u` without `-l` |
| `-d<file>` | binary dump | Covered | Classic Bluetooth receive (comprehensive): whole 68-byte BR records (`rxdump.py`); with no traffic the file is empty. Raw receive replay (comprehensive): `-i -d` writes exactly the fixture's packet records, byte for byte. Raw receive replay (smoke, CI): `-i <fixture> -d <missing dir>/x.bin` exits 1 with one `error: <file>: No such file or directory` line, no usage, no packet line, and the directory holds only the unchanged fixture (fork `5ac22a8`; the file is opened before any board lookup, so no board is needed). Refused command lines (smoke): as `-r`; comprehensive adds `-z -l` and `-d` twice with `-h` |
| `-s` | sweep all channels (the default, stated) | Covered | Raw receive replay (smoke, no Ubertooth, CI): `-i <fixture> -s` prints the same packet lines as without it and no deprecation notice, warning or error; `-h` lists `-s sweep all channels - the default; a -c channel wins over it`. Classic Bluetooth receive (comprehensive): `-t N -s` exits 0 by itself with no notice; `-t N -s -c 39`, every packet on channel 39. Fork `7b46ec4` ([ubertooth#361](https://github.com/SqueakyFloors/ubertooth/issues/361)) kept `-s` after `5ac22a8` removed it; before `5ac22a8` it printed a deprecation notice |
| `-V` | version | Covered | Host CLI surface (fast, release): host version line |
| `-U <0-7>` | select device | Covered | Classic Bluetooth receive by serial, guard board unchanged |

Combinations: `-u` without `-l` rejected: Covered (Argument validation, smoke; Raw receive replay, comprehensive: `-i <file> -u` refused with "requires -l <LAP>" before the input is used, no Ubertooth needed; the bare `-u` is left to Argument validation, which holds a lease, because a fork before the fix opens the default board first; fork `57469f2`, [ubertooth#236](https://github.com/SqueakyFloors/ubertooth/issues/236)). `-z` with an address rejected: Covered (Argument validation, comprehensive). `-z -t N` exits after N s: Covered (Classic Bluetooth receive, smoke). `-i` replay into `-r`/`-q`: Covered (Raw receive replay, smoke); into `-d`, with `-l`, `-l -u`, `-z`, `-e`: Covered (Raw receive replay, comprehensive). Replay capture timestamps taken from the recorded times (fixed in `a55ac89`, [ubertooth#202](https://github.com/SqueakyFloors/ubertooth/issues/202)): Not checked.

## ubertooth-dump

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0) |
| `-b` | raw bitstream only | Covered | Raw receive (smoke): 401-byte records of 400 `0`/`1` symbols and a newline |
| `-c` | classic modulation | Covered | Raw receive (comprehensive): `-l -c` leaves MDMCTRL 0x0029 (read with `ubertooth-debug`) |
| `-l` | LE modulation | Covered | Raw receive (comprehensive): `-l` and `-c -l` leave MDMCTRL 0x0040 |
| `-d <file>` | write to a file | Covered | Raw receive (smoke): whole 68-byte records (host time, usb_pkt_rx), at least 50 per second (`rxdump.py`). Refused command lines (no Ubertooth, smoke): a stray word, `-h`, or `-U` without its value after `-d` creates no file and leaves an existing one unchanged; `-d` twice with `-h` (comprehensive) |
| `-U<0-7>` | select device | Covered | Raw receive by serial, guard board unchanged |

Combinations: `-c` with `-l`: last one wins. Covered (Raw receive, comprehensive). Stale packets across a mode change ([ubertooth#318](https://github.com/SqueakyFloors/ubertooth/issues/318)): Covered (Raw receive, comprehensive): five rounds of a bounded `dump -d` after a bounded `btle -n -A37` and a `btle -n -A37 -q` after that dump; no dump record may predate the dump (clk100ns restarts at mode entry; no LE records) and no `-q` packet may be off RF channel 0. Failed on every iteration at 89d9197 (two records from the previous dump); passes since fork `9e31dd5` (#437) and `ed30131` (#479), checked on the lab host at `699960b`. Output size over a bounded run: Covered (record count). Output to stdout: Not covered (it would log raw symbols).

## ubertooth-tx

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0) |
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
| `-r` | print the map every second | Partial | Classic Bluetooth receive (comprehensive): one `<time> <80 bits>` line per second without a piconet. Map content needs a piconet |
| `-t <s>` | initial detection timeout | Covered | Classic Bluetooth receive (smoke): with no piconet in range it exits 2 after about N s with `error: no packets from ??:??:<UAP>:<LAP> within N s; AFH map unknown, not monitoring` and prints no map (since `3b22c95`, [ubertooth#187](https://github.com/SqueakyFloors/ubertooth/issues/187); before, an all-zero map and monitoring). The map and monitoring with packets need a piconet |
| `-e <0-4>` | max access-code errors | Partial | Classic Bluetooth receive (comprehensive): `-t N -e 2` accepted (exit 2, no packets). No range check ([ubertooth#155](https://github.com/SqueakyFloors/ubertooth/issues/155)) |
| `-a <map>` | initial AFH map (undocumented) | N/A | Removed in fork `5ac22a8` ([ubertooth#350](https://github.com/SqueakyFloors/ubertooth/issues/350)) |
| `-V` | version | Covered | Host CLI surface (fast, release): host version line |
| `-U <index\|serial>` | select device | Covered | Classic Bluetooth receive by serial; an unknown serial exits 1 with `no Ubertooth found with serial matching` (comprehensive; it segfaulted before `219a5b0`, [ubertooth#185](https://github.com/SqueakyFloors/ubertooth/issues/185)) |

The receive transfer is given back between the detection and monitoring phases (libubertooth, fork `553b8b8`, [ubertooth#307](https://github.com/SqueakyFloors/ubertooth/issues/307)): Hard, the monitoring phase needs a piconet; with none, `-t N` ends after the detection phase.

## ubertooth-ego

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface |
| `-f` | follow mode | Hard | Needs an E-GO skateboard (backlog) |
| `-r` | continuous RX on one channel | Covered | Raw receive (smoke): `-r -c 2440` runs until stopped; any packet line must be on 2440 MHz |
| `-i` / `-j` | interfere (`-j` alias) | Hard | Transmits RF |
| `-c <2402-2480>` | RX channel (MHz) | Partial | Raw receive: `-c 2440` accepted; the channel cannot be read during the mode. `-c 2500` is accepted ([ubertooth#155](https://github.com/SqueakyFloors/ubertooth/issues/155)) |
| `-l <1-48>` | capture length | Covered | `-l1` and `-l48` accepted (Raw receive, comprehensive); `-l0` (smoke) and `-l49` rejected: Argument validation |
| `-a <code>` | access code | Covered | Raw receive (comprehensive): `-a 630f9ffe` accepted, `-a zz` rejected with exit 1 |
| `-U <index\|serial>` | select device | Covered | Raw receive by serial, guard board unchanged |

Combinations: two or more of `-f`/`-r`/`-i`: last one wins (`do_mode` is overwritten, per source). Not covered; `-i` transmits. `-c` with `-f`, `-i` or `-j` is refused before any device is opened since fork `12211ce` (fork #394; it was dropped without a word): Not covered.

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
| `-h` | help | Covered | Host CLI surface (exit 0 since fork `12211ce`, fork #406; it was 1); Linux only: not applicable on macOS (no BlueZ); required in a Linux `--build` run (the tool must be in the run prefix, #40), checked in CI |
| `-s` | BlueZ HCI scan | Hard | Needs a Bluetooth adapter and discoverable peers |
| `-x` | extended inquiry | Hard | Same |
| `-t <s>` | scan time | Hard | Same |
| `-e <0-4>` | max access-code errors | Hard | Same |
| `-b <hciN>` | HCI device | Hard | Same |
| `-U<0-7>` | select device | Hard | Same (Bluetooth inquiry scan is in the backlog) |

A stray word, `-h stray`, a bare `-`, and `-- -x` are refused with `error: unexpected argument '<word>'` and exit 1 before any HCI device or board (one argument rule, `ubertooth-scan.md` ARGUMENTS): Covered (Refused command lines, no Ubertooth; `scan stray` in smoke, the rest comprehensive; Linux only).

## ubertooth-follow (BlueZ builds only)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h` | help | Covered | Host CLI surface (exit 0 since fork `12211ce`, fork #406; it was 1); Linux only: not applicable on macOS (no BlueZ); required in a Linux `--build` run (the tool must be in the run prefix, #40), checked in CI |
| `-l<LAP>` | target LAP | Hard | Needs a BR/EDR piconet and an adapter (backlog) |
| `-u<UAP>` | target UAP | Hard | Same |
| `-U<0-7>` | select device | Hard | Same |
| `-r<file>` | PCAPNG capture | Partial | Capture needs a piconet (Hard). Refused command lines (no Ubertooth, smoke, Linux only: not applicable on macOS): a stray word, `-h`, or `-e 9` after it creates no file and leaves an existing one unchanged |
| `-q<file>` | PCAP capture | Partial | Capture needs a piconet (Hard). Refused command lines (no Ubertooth, smoke, Linux only: not applicable on macOS): a stray word, `-h`, or `-e 9` after it creates no file and leaves an existing one unchanged; `-q` twice with a stray word is refused with `error: option '-q' given more than once` and creates neither file |
| `-e <n>` | max access-code errors | Hard | Same |
| `-d <file>` | binary dump | Partial | Capture needs a piconet (Hard). Refused command lines (no Ubertooth, smoke, Linux only: not applicable on macOS): a stray word, `-h`, or `-e 9` after it creates no file and leaves an existing one unchanged; `-d` twice with `-h` (comprehensive) |
| `-a` | enable AFH | Hard | Same |
| `-b <hciN>` | HCI device | Hard | Same |
| `-w <slots>` | USB delay | Hard | Same |

A stray word with no file option, `-h stray`, a bare `-`, and `-- -x` are refused with `error: unexpected argument '<word>'` and exit 1 before any HCI device or board (one argument rule, `ubertooth-follow.md` ARGUMENTS): Covered (Refused command lines, no Ubertooth; `follow stray` in smoke, the rest comprehensive; Linux only).

## ubertooth-btbr (Python)

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-f <n>` | frequency offset | Hard | Needs the `btbr` firmware, not `bluetooth_rxtx`; interactive console |
| `-b <n>` | max access-code errors | Hard | Same |

## btle-extcap (Python, Wireshark extcap; not installed by CMake)

Run from the resolved Ubertooth commit (`git show`), with the `--with-python`
interpreter or `python3`.

| Option | Meaning (short) | Status | Harness test / notes |
|---|---|---|---|
| `-h`, `--help` | help | Covered | Python tools: exit 0, the usage line names `--extcap-interfaces`, `--extcap-interface=ubertooth<serial>`, `--extcap-dlts`, `--extcap-config`, `--capture --fifo=PATH`, `--channel=37\|38\|39` (fixed in fork [#268](https://github.com/SqueakyFloors/ubertooth/issues/268)); `-h extra`: the stray word is refused, exit 1 (comprehensive) |
| unknown option | usage error | Covered | Python tools (comprehensive): `--extcap-bogus` gives `error: unrecognized option '--extcap-bogus'`, the wording every tool here shares (since fork `5310a5d`, #481), and no usage text (since fork `12211ce`, #406), exit 1; so do the options Wireshark sends only for a feature the listing advertises: `--extcap-reload-option`, `--extcap-control-in`, `--extcap-control-out`, `--extcap-config-option-name` (no config listed) |
| operand | stray word | Covered | Python tools (comprehensive): `--extcap-dlts extra` gives `error: unexpected argument 'extra'`, exit 1, no DLT line; BLE extcap interfaces (comprehensive): `--extcap-interfaces extra`, no listing ([ubertooth#342](https://github.com/SqueakyFloors/ubertooth/pull/342)) |
| `--extcap-version[=V]` | Wireshark's version | Covered | BLE extcap interfaces: `--extcap-interfaces --extcap-version=4.2.0`, as Wireshark calls it, gives the same listing (smoke; fork [#297](https://github.com/SqueakyFloors/ubertooth/issues/297), fixed in d04f79e); `--extcap-version 4.4.1` (comprehensive). Python tools (comprehensive): `=4.2.0`, the next-word form and the bare form (first and last) are ignored by `--extcap-dlts`/`--extcap-config` |
| `--extcap-capture-filter=` | capture filter | Covered | BLE extcap capture (comprehensive): `--extcap-capture-filter "btle.length > 20"`: the capture runs as without it, and `warning: this interface applies no capture filter; ...` is on stderr . BLE extcap in tshark (comprehensive): real tshark passes `-f` on as `--extcap-capture-filter`, the plugin's warning reaches tshark, and the capture still decodes the advertiser |
| `--extcap-interfaces` | list interfaces | Covered | BLE extcap interfaces: exactly one `interface {value=ubertooth<serial>}{display=Ubertooth One <serial>}` line per attached board, and every registered board's serial among them, guards unchanged (fixed in fork [#269](https://github.com/SqueakyFloors/ubertooth/issues/269)). Python tools (comprehensive, no Ubertooth): with a stand-in `ubertooth-util` that fails as a libusb failure does, the listing exits 1 and passes its message on (fixed in fork `779f4f7`, [ubertooth#362](https://github.com/SqueakyFloors/ubertooth/issues/362); at d04f79e it exited 0 with an empty listing and dropped the message) |
| `--extcap-dlts` | list DLTs | Covered | Python tools: DLT 256 `DLT_BLUETOOTH_LE_LL_WITH_PHDR`; without an interface: the `must be ubertooth<serial>` error, exit 1 (comprehensive) |
| `--extcap-config` | config options | Covered | Python tools: `--channel` selector, 37 (default), 38, 39; a non-Ubertooth interface: the `must be ubertooth<serial>` error, exit 1 (comprehensive) |
| `--capture` | start a capture | Covered | BLE extcap capture: runs `ubertooth-btle -f -U <serial> -q <fifo> -A <channel>`; the harness reads the FIFO as Wireshark would and stops the script with SIGTERM: a PCAP header for link type 256, whole records, at most 32 MiB (a longer stream fails), exit 0, no process left, channel, squelch and LEDs restored, guards unchanged. Comprehensive: exits non-zero when the capture fails, with a serial no board has (`ubertooth-btle`'s `no Ubertooth found with serial matching` and its status) or `--channel=40` (exit 1, no `ubertooth-btle` started, recorded by a shim `ubertooth-btle` first on PATH) (fixed in d04f79e, [ubertooth#334](https://github.com/SqueakyFloors/ubertooth/issues/334)); a capture ended by closing the FIFO (Wireshark's other way to stop), after at least one record, exits 0 by itself once the next packet gets SIGPIPE, no process left; skipped ("no advertising traffic in range") when no record arrives in the capture window. BLE extcap in tshark: real `tshark -i ubertooth<serial>` captures through the plugin it installs itself and Wireshark's own dissector decodes a registered DK advertiser's address (link type 256 and the LE pseudo-header), exit 0 after the duration and after a SIGINT halfway; not applicable where `tshark` is not installed |
| `--extcap-interface=` | interface | Covered | `ubertooth<serial>` with `--extcap-dlts`/`--extcap-config`; the older `ubertooth0` still accepted, `wlan0` refused (comprehensive); BLE extcap capture: `ubertooth<serial>` of the selected board reaches `ubertooth-btle -U <serial>`; `ubertooth0badc0de...` (no board) exits non-zero (comprehensive) |
| `--fifo=` | output FIFO | Covered | BLE extcap capture: a FIFO the harness creates and reads; Python tools (comprehensive): `--capture` without it gives `error: --capture needs --fifo=PATH`, exit 1 |
| `--channel=` | advertising channel | Covered | BLE extcap capture: default 37 (smoke); 37, 38, 39 (comprehensive): `ubertooth-btle` gets `-A <channel>` and every advertising record is on its RF channel (0, 12, 39); 40 is refused by the script before `ubertooth-btle` runs (`error: advertising channel index must be 37-39 (got 40)`, exit 1): BLE extcap capture, and Python tools with no Ubertooth (comprehensive) |

## Fork bugs found by these tests

Open or recently fixed SqueakyFloors/ubertooth issues that harness tests found
or that harness cases check. A case asserts the documented behavior, so it
fails until the fix is on the commit under test.

| Fork issue | Tool | Problem | Status | Harness case |
|---|---|---|---|---|
| [#149](https://github.com/SqueakyFloors/ubertooth/issues/149) | firmware | GET_CHANNEL control requests stall in follow mode | Open | Not yet |
| [#158](https://github.com/SqueakyFloors/ubertooth/issues/158) | firmware | Board stops answering after a sniffing or specan stream ends, until re-plugged | Fixed on main (`b7437da`); boards still wedge, see [#239](https://github.com/SqueakyFloors/ubertooth/issues/239) | Any BLE or specan run |
| [#159](https://github.com/SqueakyFloors/ubertooth/issues/159) | ubertooth-util | `-z` dropped with a mode | Fixed on main (`4ed4a1d`) | Argument validation (comprehensive) |
| [#160](https://github.com/SqueakyFloors/ubertooth/issues/160) | docs | PA boot/reset default (7) undocumented | Fixed on main (`4ed4a1d`) | Radio setting transitions checks 7 |
| [#161](https://github.com/SqueakyFloors/ubertooth/issues/161) | ubertooth-util | `-a0` queried instead of setting level 0 | Fixed on main (`4ed4a1d`) | Radio setting transitions: `-a0` (smoke), `-a 0`, reset from PA 0 (comprehensive) |
| [#164](https://github.com/SqueakyFloors/ubertooth/issues/164) | ubertooth-debug | Exit status was the register's low byte | Fixed (`89afc1c`, #170) | Debug registers (comprehensive) |
| [#171](https://github.com/SqueakyFloors/ubertooth/issues/171) | ubertooth-btle | Bad `-a` value sets garbage; `-a` skips the CRC read and a later set | Fixed (`09e9a46`, #178) | BLE settings (comprehensive) |
| [#172](https://github.com/SqueakyFloors/ubertooth/issues/172) | ubertooth-util | Non-numeric and out-of-range values accepted (`-z`, `-l`, `-d`, `-C`, `-q`) | Fixed (`658730c`, #177) | Argument validation (comprehensive) |
| [#173](https://github.com/SqueakyFloors/ubertooth/issues/173) | ubertooth-specan | A run first reports one stale packet from the previous range | Fixed (`20bcf11`, #180) | Spectrum analyzer (comprehensive) |
| [#185](https://github.com/SqueakyFloors/ubertooth/issues/185) | ubertooth-afh | Segmentation fault when the selected board is not found | Fixed (`219a5b0`, #196) | Classic Bluetooth receive (comprehensive) |
| [#186](https://github.com/SqueakyFloors/ubertooth/issues/186) | ubertooth-rx | Usage says `-c` defaults to channel 39; the default sweeps | Fixed on main (`0b63b01`) | None (usage text) |
| [#187](https://github.com/SqueakyFloors/ubertooth/issues/187) | ubertooth-afh | `-t` with no packets prints an all-zero map and keeps running, undocumented | Fixed (`3b22c95`, #207): exits 2 with a message | Classic Bluetooth receive (smoke: `-t`; comprehensive: `-t -e 2`) |
| [#188](https://github.com/SqueakyFloors/ubertooth/issues/188) | libubertooth | With several boards, `-U<index>` past the end opens another USB device and exits 0 | Fixed (`0c7e9c0`, #201) | Multi-board selection (comprehensive) |
| [#189](https://github.com/SqueakyFloors/ubertooth/issues/189) | firmware | LED specan threshold wraps in int8: `-q183`..`-q225` and bare `-q` light nothing | Fixed (`aa4f620`, #204): 16-bit threshold, `-q` now 20-120, default -85 dBm | Ubertooth util modes (`-q120` lights every LED, smoke); Argument validation (`-q183` refused) |
| [#190](https://github.com/SqueakyFloors/ubertooth/issues/190) | ubertooth-util | `-m` says "invalid range test result" when there is no result | Fixed (`7180f74`, #197): `no range test result yet` | Ubertooth util modes (smoke) |
| [#191](https://github.com/SqueakyFloors/ubertooth/issues/191) | firmware | Stopping LED specan (and specan) skips the idle reset (LEDs stay on, channel and squelch kept) | Fixed (`aa4f620`, #204) | Ubertooth util modes (every `-q` case), Spectrum analyzer and BLE settings (`-p`) stop resets (comprehensive) |
| [#193](https://github.com/SqueakyFloors/ubertooth/issues/193) | btle, rx, follow, specan, dump, dfu | A refused command line still creates or truncates its output file (`-q`, `-r`, `-d`, `-u`) | Fixed on main (`6fee30c`, #264) | Refused command lines (smoke, in CI) |
| [#198](https://github.com/SqueakyFloors/ubertooth/issues/198) | ubertooth-dfu | `-d` with a rejected image leaves the board in the DFU bootloader | Fixed on main after 3b22c95 (`e8edd15`, #217); fails at 3b22c95 | DFU images (comprehensive, every reject case: refused before a device is opened) |
| [#199](https://github.com/SqueakyFloors/ubertooth/issues/199) | ubertooth-dfu | `-d` writes an image whose suffix names another vendor or product | Fixed on main after 3b22c95 (#217); fails at 3b22c95 | DFU images (comprehensive: the refusal names the ID) |
| [#200](https://github.com/SqueakyFloors/ubertooth/issues/200) | ubertooth-dfu | `-s` adds a second suffix and needs a writable input; `-u` truncates on failure | Fixed on main (`9d710fe`) | DFU images (comprehensive `-s` cases); `-u` replacing only a complete dump: not checked |
| [#202](https://github.com/SqueakyFloors/ubertooth/issues/202) | ubertooth-rx | `-i` lines and captures carry the replay time, not the recorded systime | Fixed on main after 3b22c95 (`a55ac89`, #212); fails at 3b22c95 | Raw receive replay (comprehensive) |
| [#203](https://github.com/SqueakyFloors/ubertooth/issues/203) | ubertooth-rx | `-i` read errors (a directory) and a cut last record are not reported; exit 0 | Fixed on main after 3b22c95 (`a55ac89`, #212); fails at 3b22c95 | Raw receive replay (comprehensive: the directory and cut-record cases check the messages and exit 1) |
| [#208](https://github.com/SqueakyFloors/ubertooth/issues/208) | ubertooth-dfu | An intermittent `-u` read-back failure leaves the board in DFU despite `-r` | Fixed on main (`3c92a19`) | DFU images (cleanup returns the board) |
| [#213](https://github.com/SqueakyFloors/ubertooth/issues/213) | firmware | Control requests occasionally stall while LED specan runs | Fixed on main (`234ebb3`) | Ubertooth util modes: the LED sampler retries a failed read once and records it |
| [#228](https://github.com/SqueakyFloors/ubertooth/issues/228) | ubertooth-util | A set and a read of the same value in one command (`-c2426 -c`) drop the set, exit 0 | Fixed on main (`5b45b3e`): both run, the set first | Radio setting transitions (comprehensive: `-c<MHz> -c`, `-c -c<MHz>`, `-a<level> -a`, `-l<state> -l`, `-d<state> -d`) |
| [#231](https://github.com/SqueakyFloors/ubertooth/issues/231) | ubertooth-specan | `-d` writes to stdout for any value starting with `-`, not only `-` | Fixed on main (`053363a`) | Spectrum analyzer (comprehensive: `-d -x.bin`) |
| [#235](https://github.com/SqueakyFloors/ubertooth/issues/235) | ubertooth-debug | `-r` error says `> 0x00 and < 0xff`; 0x00-0xff are accepted | Fixed on main (`b833a0f`): `register address must be 0x00-0xff` | Debug registers (comprehensive, `Reject -r 0x100` checks the whole message) |
| [#236](https://github.com/SqueakyFloors/ubertooth/issues/236) | ubertooth-rx | `-u` without `-l` is refused only after the board is opened | Fixed on main (`57469f2`) | Raw receive replay (comprehensive, no Ubertooth: `-i <file> -u`); Argument validation for the bare `-u` |
| [#239](https://github.com/SqueakyFloors/ubertooth/issues/239) | firmware | Boards still stop answering control requests (-7) after a stream ends, until re-plugged | Open | Any streaming test |
| [#267](https://github.com/SqueakyFloors/ubertooth/issues/267) | ubertooth-dfu | `-d <image> -r` exits 1 ("Detach failed") after a good flash and skips the revision check | Fixed on main (`89d9197`); regression in `3c92a19`, fails at 6fee30c | Flash reliability and DFU images (every write) |
| [#268](https://github.com/SqueakyFloors/ubertooth/issues/268) | btle-extcap | `-h` names options that do not exist (`--list-interfaces`, `--list-dlts`, `--config`) | Fixed on main (`be2561e`, #289) | Python tools (smoke: `-h` names the real options) |
| [#269](https://github.com/SqueakyFloors/ubertooth/issues/269) | btle-extcap | With two or more boards, `--extcap-interfaces` shows `0:` instead of the serial | Fixed on main (`be2561e`, #289): interfaces are `ubertooth<serial>` | BLE extcap interfaces (smoke) |
| [#286](https://github.com/SqueakyFloors/ubertooth/issues/286) | ubertooth-dfu | `-s <file.bin> -d <bad image>` writes the `.dfu` before the image check refuses the command | Fixed on main (`f2e06fa`, #341) | Refused command lines (comprehensive) |
| [#297](https://github.com/SqueakyFloors/ubertooth/issues/297) | btle-extcap | `--extcap-version`, which Wireshark adds to `--extcap-interfaces`, is refused, so no Ubertooth reaches Wireshark's list | Fixed on main (`d04f79e`, #342) | BLE extcap interfaces (smoke: `--extcap-version=4.2.0`) |
| [#298](https://github.com/SqueakyFloors/ubertooth/issues/298) | btle-extcap | Interfaces are named by enumeration index, which changes across a replug | Fixed on main (`d04f79e`, #342): `ubertooth<serial>` | BLE extcap interfaces (smoke), BLE extcap capture (`-U <serial>`) |
| [#305](https://github.com/SqueakyFloors/ubertooth/issues/305) | ubertooth-dfu | `-s <file.bin>` truncates `<file>.dfu` before it reads the input, so a failed `-s` leaves it empty | Fixed on main (`f2e06fa`, #341) | DFU image checks (comprehensive) |
| [#318](https://github.com/SqueakyFloors/ubertooth/issues/318) | firmware | A bulk stream starts with packets left from the previous mode: `btle -n` (read by control-request polling) leaves packets on the unread bulk IN endpoint, and idle sends what is still queued there; a dump after a dump starts with the previous dump's packets (every time at 89d9197), after `btle -n` sometimes with LE packets | Fixed on main (`9e31dd5`, #437; `ed30131`, #479); passes on the lab host at `699960b` | Raw receive (comprehensive): `stale-packets-across-mode-change` alternates `btle -n` and `dump -d` five times and records the per-iteration stale count; `dump -d` record types (smoke) catch the LE case |
| [#319](https://github.com/SqueakyFloors/ubertooth/issues/319) | libubertooth, streaming tools | A capture whose USB stream fails (transfer error, board gone) exits 0 | Open | None yet: the step passes on exit status; the cleanup that follows fails |
| [#334](https://github.com/SqueakyFloors/ubertooth/issues/334) | btle-extcap | `--capture` exits 0 when the `ubertooth-btle` it runs fails (no board with the serial, `--channel=40`) | Fixed on main (`d04f79e`, #342) | BLE extcap capture (comprehensive) |
| [#332](https://github.com/SqueakyFloors/ubertooth/issues/332) | ubertooth-util | `-N` prints the count and exits 0, silently dropping every other option (`-f`, `-i`, `-z`, ...) | Fixed on main (`db5649b`): `-N` prints the count and the command carries on | Util mode refusals (comprehensive) |
| [#333](https://github.com/SqueakyFloors/ubertooth/issues/333) | ubertooth-util | `-f -U all` prints the `-f` deprecation note above the `-U all` refusal | Fixed on main (`db5649b`): `-f` removed | Util mode refusals (comprehensive: `-U all -i`; smoke: `-f` refused) |
| [#362](https://github.com/SqueakyFloors/ubertooth/issues/362) | btle-extcap | `--extcap-interfaces` exits 0 with an empty listing when `ubertooth-util` fails, and drops its stderr | Fixed on main (`779f4f7`, #374); passes at `699960b` | Python tools (comprehensive): `extcap-interfaces-util-fails` |
| [#677](https://github.com/SqueakyFloors/ubertooth/issues/677) | firmware | The range test takes the first CRC-OK packet on the reply channel, so a packet that is not a copy of the request ends about one bench run in ten in `mismatch on byte 0` | Open | Two-device range test |

A test that fails cleanly (device verified restored, guards unchanged) no longer ends the run ([#41](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/41)), so a case that fails on a known issue does not hide later tests.

## Hard or impossible to test

- **GUI:** `ubertooth-specan-ui` needs Qt and a display. Only `-h` and the
  argument checks can run headless (an offscreen Qt platform may allow a smoke run).
- **Transmits RF:** `ubertooth-util -t -n -e`, `ubertooth-btle -s -i -I -j -J`,
  `ubertooth-ego -i -j`, and `ubertooth-ducky -q -b`. These need an explicit
  RF allowance in the plan, like `allow RF transmission` on Ubertooth util conflicts.
- **Leaves the application firmware:** `ubertooth-util -i` and
  `ubertooth-dfu -d` or `-u` without `-r`. They need a recovery path (USB power cycle,
  DFU/ISP reflash). The Reset into invalid application backlog item covers part of this.
- **Needs an RF peer:** `ubertooth-afh` and `ubertooth-follow` against a
  piconet (the two ESP32 Classic peers now make one, so live `ubertooth-rx
  -l -u` is covered by Follow Classic Bluetooth piconet; no AFH or follow case
  is written yet, [#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88)),
  BLE connection (`ubertooth-btle -f/-p` with real traffic, `-t`), E-GO
  skateboard, Uberducky.
- **Needs a host Bluetooth adapter / BlueZ:** `ubertooth-scan`, `ubertooth-follow` (Linux).
- **Needs a second Ubertooth:** the `ubertooth-util -n`/`-e`/`-m` range test
  (Two-device range test, `range` plan), `-U all` and no-selector listing
  (Multi-board selection).
- **Other firmware:** `ubertooth-btbr`.
- **Does nothing:** `ubertooth-tx`.

## Suggested next tests

Ordered by value/effort:

1. Done (#27): host-side argument rejections across util, btle, ego, ducky, rx, specan (`Argument validation`). Still open: `ubertooth-dump -c -l` (last-wins, not a rejection) and `ubertooth-rx -e5`.
2. Done (#28): the host version line, `-N`, `-V` on util/rx/tx/afh, BlueZ tool help.
Next, harness work:

- Done (#5, #25): `-a0` sets PA 0, next-word values, and stray words on every tool. Done (#34): `-z` with a mode and the `#172` rejections.

3. Done (#33): `DFU images` (`ubertooth-dfu -s`, `-u` read-back, bad-image refusal).
4. Done (#30): `Spectrum analyzer` ranges, `-g`, `-G`, `-v`, `-d`.
   Done (#107): `Spectrum RSSI sanity` (fast): live RSSI with no signal source (noise floor, negative mean, varying readings).
5. Done (#29): `BLE settings` (`-a`, `-t`, `-x`) and `-r` with `-q`/`-c`. Still open: `-r -` to stdout (would log raw capture bytes; needs a header-only check).
6. Done (#32): `Debug registers` lists, ranges, verbosity, exit status.
7. Done (#35): `Raw receive replay` (`ubertooth-rx -i` with a synthetic fixture, no Ubertooth, in CI). No LE replay exists: no tool calls libubertooth's `rx_btle_file()`.
8. Done (#31): `Classic Bluetooth receive` (`ubertooth-rx`, `ubertooth-afh`) and `Raw receive`
   (`ubertooth-dump`, `ubertooth-ego -r`). Still open: BLE monitor (in the backlog
   plan; the known BLE advertiser it needs exists since [#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86)).
9. Add Ubertooth util conflicts to the release plan after a passing hardware run (fork #82 is fixed; [#25](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/25));
   Done (#72): `-i` (and, before fork `db5649b`, `-f`) rejection cases run with no Ubertooth (`Util mode refusals`, in CI), so no accepted pair can reach those modes.
10. Done (#37): headless `ubertooth-specan-ui -h` (`--build --with-python`) and `btle-extcap --extcap-interfaces/--extcap-dlts/--extcap-config`; `btle-extcap --capture` (#73). Still open: specan-ui argument checks.
11. Done (#28): `ubertooth-scan -h` and `ubertooth-follow -h` (Linux only; not applicable on macOS, #71).
12. Done (#63): `Refused command lines`: no file created, truncated, or left behind by a refused command line, for every file option, with no Ubertooth (in CI).
    Done (#65): `DFU image checks`: `ubertooth-dfu -d` bad-image refusals and `-s`, with no Ubertooth (in CI).
13. Peer-based tests from the backlog (AFH map, piconet follow), once
    fixtures are registered. Implemented (#86): Capture known BLE advertiser
    (`ble` plan) with an nRF52840 DK running `firmware/nrf52840dk`.
    Implemented (#89): the two-board range test (`range` plan); it passes
    since fork [ubertooth#601](https://github.com/SqueakyFloors/ubertooth/pull/601),
    with an occasional `mismatch on byte 0`
    ([ubertooth#677](https://github.com/SqueakyFloors/ubertooth/issues/677)).
    Implemented (#88): Capture known Classic device and Follow Classic
    Bluetooth piconet (`classic` plan) with two ESP32 Classic peers running
    `firmware/esp32-classic-peer`. Recover AFH map and Bluetooth inquiry scan
    are still backlog entries: the piconet is there now, but `ubertooth-afh`
    needs a case and `ubertooth-scan` needs a host Bluetooth adapter.
    The fixtures each needs are tracked as `hardware needed` issues: BLE
    advertiser [#86](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/86),
    BLE connection [#87](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/87),
    Classic piconet and Bluetooth adapter [#88](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/88),
    other boards and peers [#89](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/89).
