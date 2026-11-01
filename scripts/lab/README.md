# Bench helper scripts

The by-hand tools a bench needs beside the harness itself: holding a device
while you run something on it, driving the Classic Bluetooth peers, and
switching a hub port to recover a device that has stopped answering. A harness
run needs none of them; these are for the manual work around it.

Nothing about a bench is built in. Every script takes its registry, lock
paths, and labels from options or the environment, so the same script serves
any bench. See [`docs/hardware.md`](../../docs/hardware.md) for what a bench
is made of and [`hardware.example.yaml`](../../hardware.example.yaml) for the
registry.

Run them with the harness environment's interpreter. The path to the script is
all they need, from any working directory:

```sh
python3 bootstrap.py                      # once, in the checkout
.venv/bin/python scripts/lab/hold --help
~/checkout/.venv/bin/python ~/checkout/scripts/lab/hub-port --help
```

Set the two paths once and the commands stay short:

```sh
export UBERTOOTH_HARNESS_HARDWARE=~/bench/hardware.yaml
export UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK=~/bench/board-{serial}.lock
export UBERTOOTH_HARNESS_OWNER="alice"
```

## `hold`

Holds devices for the length of one command, naming them the way a bench note
does: an alias, a full serial, an unambiguous prefix of one, or
a J-Link serial with or without its leading zeros.

```sh
.venv/bin/python scripts/lab/hold UT_Linux0 -- ubertooth-util -U <serial> -v
.venv/bin/python scripts/lab/hold a1b2c3d4 -- ubertooth-util -U <serial> -v
.venv/bin/python scripts/lab/hold ESP32_Linux0,ESP32_Linux1,UT_Linux0 -- ./my-capture.sh
```

The locks themselves are the harness's: for the device types it leases, this
resolves the names and runs `ubertooth-harness lease`, so there is one
implementation of the lock order, the all-or-nothing attempt, the wait, the
holder reporting, and the automatic host lock for a command line that names
`ubertooth-dfu`. A type the harness does not lease yet is held here, by the
key the harness will use when it does.

A name that matches two devices is refused rather than resolved to the first
one, so a short name can never quietly select the wrong device.

`UBERTOOTH_HARNESS_EXTERNAL_BOARD_LOCK` is how a bench whose own scripts lock
devices with per-device `flock` files keeps both sides honest: every hold
takes that file too. Point it at the bench's existing lock files.

An Ubertooth's lock file is named after its serial. A DK's and a peer's are
named after the harness's lease key for them (`nrf52840dk-<serial without
leading zeros>`, `esp32-<serial>`), because that is what a harness run takes.
A bench script that names such a lock after the plain serial instead would not
meet a harness run on the same file, so `hold` takes **both** names for those
two types.

That makes every `hold` exclude every other `hold`, whichever name a bench
script was written against. It does not reach further than that: a
`run-suite` or an `ubertooth-harness lease` takes only the harness's own key,
so a bench script holding the plain name alone can still overlap a run on a DK
or a peer. The way to be sure is for the bench's scripts to take their devices
through `hold`, which is what this script is for.

## `peer`

Drives the ESP32 Classic Bluetooth peers by registry alias, holding them while
it runs:

```sh
.venv/bin/python scripts/lab/peer status   ESP32_Linux0
.venv/bin/python scripts/lab/peer identify ESP32_Linux0
.venv/bin/python scripts/lab/peer reset    ESP32_Linux0
```

It looks up what [`ubertooth_harness/esp32peer.py`](../../ubertooth_harness/esp32peer.py)
needs — the bridge's stable `by-id` path and the board's registered `BD_ADDR` —
wraps the call in `hold`, and runs it. The module does the work: it opens the
bridges, checks the firmware and the address, brings a link up between the two
boards, takes it down afterwards, reports what both boards did, and fails
closed.

**Opening a bridge resets the board**, so one invocation has to be one session
and the command under test runs *inside* it. That is what `stream`, for a
Serial Port Profile data link, and `audio`, for a voice link, are for:

```sh
.venv/bin/python scripts/lab/peer stream ESP32_Linux0 ESP32_Linux1 \
    --with UT_Linux1 -- ubertooth-rx -U <serial> -z -t 30
```

`--with` puts the Ubertooth in the same lock group, so the capture and the
peers are held together. `esp32peer.py` prints `central LAP` and `central UAP`
on standard error, which is what `ubertooth-rx -l` and `-u` take.

**The initiator pages, so it is the piconet central and its LAP is the one on
the air.** To capture a particular board's LAP, name that board as the
initiator; swapping the two arguments swaps which LAP a survey sees:

```sh
# ESP32_Linux1 pages, so its LAP is on the air
peer stream ESP32_Linux0 ESP32_Linux1 --with UT_Linux1 -- \
    ubertooth-rx -U <serial> -l <ESP32_Linux1's LAP> -u <its UAP> -t 25

# the other way round, so the other LAP is
peer stream ESP32_Linux1 ESP32_Linux0 --with UT_Linux1 -- \
    ubertooth-rx -U <serial> -l <ESP32_Linux0's LAP> -u <its UAP> -t 25
```

`--period`, `--payload`, `--min-writes`, `--min-seconds`, and `--timeout` are
passed through; left out, the module's own defaults apply.

`audio` is the same session with a synchronous (eSCO) voice link instead of
the data link. A synchronous link carries its packets in reserved slots, so it
is a different thing on the air and in a decoder. The air also carries the
Hands-Free service level connection the link is opened through, an ACL link,
for the whole session: a capture holds both:

```sh
.venv/bin/python scripts/lab/peer audio ESP32_Linux0 ESP32_Linux1 \
    --type ev3 --with UT_Linux1 -- \
    ubertooth-rx -U <serial> -l <ESP32_Linux1's LAP> -u <its UAP> -t 30
```

`ubertooth-rx` has not been seen to settle on this pair's clock from a voice
link, so do not expect it to enter follow mode and print packet detail. What
a run like that gives is the records it writes without a clock, where each
packet's header carries its type code: that is what tells an EV3 link from a
2-EV3 one on the air ([`docs/hardware.md`](../../docs/hardware.md), "Reading a
Classic capture of the peer pair").

`--type` picks the packet type the link asks for, on both boards, because a
link uses a type both of them allowed:

| `--type` | What the link carries |
|---|---|
| `ev3` | EV3: the payload goes out at the basic rate, so a basic-rate decoder can read it |
| `2ev3` | 2-EV3: the same payload as EDR, which is what the stack asks for by itself |
| `default` | whatever the stack's own table picks, which is 2-EV3 |

A link that is not eSCO, or whose packet type is not the one asked for, fails
rather than being reported as a pass, so a decode test cannot quietly get EDR
or a plain SCO link. Both boards are read for it, before the command and
again after, and both have to agree. What that proves is that the request
reached each controller and the link settled on the length that type carries:
nothing in HCI reports the packet type itself, so the firmware works it out
from the types the board allowed and the length the link uses.

### What it reports, and what the floors mean

First the two boards and the packet types their controllers support, before
anything is started. Then the link, once it is up: the type, the packet
length, the interval in slots and milliseconds, and the retransmission
window. Then, after the command, **counted across it rather than since the
link came up**, each board's own frame counters and its controller's packet
counters.

The difference is the point. Bringing the link up and settling put frames on
the air before the command starts, so a total would flatter the run. The
window is bounded by the reads either side of the command rather than by the
command itself, and those reads wait on each board's controller: a status
read can wait on its channel map and a counter read on its packet counters,
so allow a second or two of traffic either side, not milliseconds.

`--min-frames` and `--min-packets` are the floors those differences must
reach, on both boards, so a link that fell silent part way through does not
pass on what it carried at first. Set them from the rate the link reports,
not to the last packet. An EV3 link on the bench runs at about 268 packets a
second each way, so a 30-second capture window is a floor in the thousands,
not the tens of thousands a longer link totals.

The controller's packet count is the number to hold a capture against,
bearing in mind that a receiver reads a fraction of what the air carries: the
bench's 70-second EV3 link gave 18800 packets at the controller against 2891
read from the air.

`--min-seconds` and `--timeout` work as they do for `stream`.

### What fails a session

- A link that went down during the command, even if it came straight back:
  its counters would otherwise add two links' traffic together. The board's
  own `utesp: sco disconnected` is what says so. Behind it are a changed
  connection handle and a controller's count that started again, and neither
  is certain — a controller may hand the same handle back, and the frame
  counters run on across a new link — so the notice is the check to rely on.
- A board that cannot be put back in its boot state, even when the capture
  went well: a board whose audio profile is still running refuses the next
  `role` or `defaults`. The session has already pulsed a reset at it; one
  that stays stuck needs another, or the maintainer.
- A counter read that times out or cannot be read, as against one the board
  refuses. A refusal — no link up, a request of its own that went unanswered,
  or a controller that would not give the counters — is reported as not
  counted, with the reason, and its floor as not checked. A link that dropped
  during the capture is then named in its own words, with the frame counters
  still there to read.

An EV3 link is the busiest traffic this pair can make: half the payload per
packet, half the interval, so about twice the packet rate of the 2-EV3 the
stack picks by itself. The audio pattern itself is not readable from a
capture, because the controller codes the samples to CVSD before they reach
the air — the session reports the codec the link settled on, and this
firmware's build offers nothing but CVSD, so a board reporting another one is
not the image these notes describe. What a capture gets is the link's packet
types, timing, and count
([`firmware/esp32-classic-peer/README.md`](../../firmware/esp32-classic-peer/README.md),
"The audio link").

The audio link needs a newer peer image than the other commands, `utesp-1.4.0`
or a later 1.x, because that is where the packet-type choice and the fields
that report what the link settled on arrived; the link itself and its frame
counters are older. A board on an older image is told so, and nothing is
started on either board.

The firmware's own notices from the session are passed on as `said:` lines,
which is where a link that went down during the capture announces itself.

Inside an outer `hold` that already has these devices, add `--no-hold`: holding
them twice would wait for a lock this process already holds.

## `hub-port`

Switches one port of a registered USB power controller, which is the manual
recovery for a device that has left the bus or stopped answering.

```sh
.venv/bin/python scripts/lab/hub-port Hub_Linux1 cycle 3
.venv/bin/python scripts/lab/hub-port Hub_Linux1 off 5
```

The hub's serial, its driver, and which ports it has come from the registry.
It holds the harness's host power/firmware lock while it switches, so it
cannot overlap a run that is flashing or switching power, and it prints the
port's state afterwards so you can see the result.

It holds the port's registered device as well, so it cannot cut power under
another run or another person, and a device already held is reported with its
holder's name rather than switched. `--wait SECONDS` sets how long it waits
for either lock (two minutes by default, since a run holds the host lock
across a build and a flash); `--wait 0` reports the holder after about a
second rather than waiting. `--force`
switches without holding the device, which is what a wedged device whose
holder has given up on it needs. A cycle whose power-on fails tries once more
rather than leaving the port off.

Switch only the port of the device you are recovering, and cycle once before
looking rather than looping, because on a host near its USB endpoint limit a
device that leaves the bus may not come back
([`docs/hardware.md`](../../docs/hardware.md), "Known limits").

Reading the ports changes nothing and belongs to the harness:

```sh
ubertooth-harness power --hardware hardware.yaml Hub_Linux1
```

## `99-acroname.rules`

The udev rule that lets a normal account drive an Acroname USBHub3+. Install
it, change the group to one the harness account is in, then reload and replug
the hub; the file's own comments give the commands.

## What stays off the bench's scripts

A bench keeps its own `hardware.yaml`, its lock files, and its notes. Those
carry real serials, paths, and host names, so they are not in this repository
and never should be.
