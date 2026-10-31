# Bench helper scripts

The by-hand tools a bench needs beside the harness itself: holding a device
while you run something on it, and switching a hub port to recover one that
has stopped answering. A harness run needs neither; these are for the manual
work around it.

Nothing about a bench is built in. Every script takes its registry, lock
paths, and labels from options or the environment, so the same script serves
any bench. See [`docs/hardware.md`](../../docs/hardware.md) for what a bench
is made of and [`hardware.example.yaml`](../../hardware.example.yaml) for the
registry.

Run them with the harness environment's interpreter, from the root of a
checkout:

```sh
python3 bootstrap.py                      # once
.venv/bin/python scripts/lab/hold --help
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

It does not take the device's lease. Switch a port only when you know nobody
is using what is on it: `ubertooth-harness boards` says who holds what, and
`hold` is how to hold it while you work. Switch only the port of the device
you are recovering, and cycle once before looking rather than looping, because
on a host near its USB endpoint limit a device that leaves the bus may not
come back ([`docs/hardware.md`](../../docs/hardware.md), "Known limits").

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
