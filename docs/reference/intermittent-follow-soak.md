# The intermittent follow soak

Reference for the `Intermittent follow soak` test and the plan that runs it,
[`testplans/follow-soak.yaml`](../../testplans/follow-soak.yaml). What the test
itself checks, phase by phase, is in
[docs/tests.md](../tests.md#intermittent-follow-soak). No soak is a release
gate; see [release-testing.md](../release-testing.md).

## The question it asks

The [address parity soak](address-parity-soak.md) asks whether one steady
state holds for days. This one asks the other long-run question: whether the
board keeps up with the radio state changing under it.

One `ubertooth-btle -f -U <serial>` capture runs for as long as the suite asks
while the two registered nRF52840 DKs go round a three-phase cycle the run
applies a phase at a time: the peripheral advertising connectable and waiting,
the central connected and writing to the link, and both off the air
altogether.

So each advertising phase is a detection with a deadline, each connected phase
a follow with a deadline (a CONNECT_IND between the two registered addresses,
then packets on the access address it handed out), and each absent phase a
silence with a known cause — and every advertising phase after the first
follows an absence, which makes its first-sighting time the time to reacquire
a device that went away and came back.

## The fixture reports on itself

Every transition is a shell command to a DK, and the central's own counters are
read at each end of a connected phase, so a phase whose command did not reach a
board, or whose central never reported itself connected or never wrote
anything, is recorded as a fixture failure and left out of the verdict. That is
what keeps a follow that found nothing apart from a connection that never
happened.

## Watchdog checkpoints

`ubertooth-util -w` is a control read on the board the capture holds, so the
run never asks for one beside a running capture. Every `checkpoint every`
seconds it takes a *planned checkpoint* at the start of an absent phase: it
ends the capture cleanly, waits for the process to go, confirms the board
answers a read, reads the record, and starts the next capture in a file of its
own.

A checkpoint is not a restart and not a recovery, and the phase it lands in is
marked with a planned capture gap, as a file rollover is; what fails is a
checkpoint that could not do its job — a capture that had to be killed, a board
that would not answer, or a board the next capture found still in a mode.
Checkpoints come at least two cycles apart, because one every cycle would
interrupt every absent phase and leave the fixture's going away unmeasured.

Each read — before the run, at each checkpoint, after each unplanned restart,
and at the end — closes an **interval**: the stretch since the previous read,
with the phases and cycles it covered, the packets inside it, and every counter
that rose (unrequested resets, capture transfer errors, radio waits that gave
up, a new processor fault). The board's counters are since power-on, so a single
read at the end of three days can only say that something happened sometime;
the interval records say which stretch it happened in.

## Booking a run

It writes no firmware and guards no other Ubertooth, so `run-suite` takes no
[host lock](../guide/leases.md#the-host-lock) and the run holds its own three
devices and nothing else: host-only work, and a hardware run on other devices,
can go on beside it. Every command it runs names its board by serial, which is
what makes guarding unnecessary.

The price of that is a precondition. Its first case checks that both DKs
already run the pinned role firmware and refuses with the version it found
rather than flashing, so put the DKs on the pinned image first with any other
DK test (`--artifact "nRF52840 advertiser=PATH"`,
[`firmware/nrf52840dk/README.md`](../../firmware/nrf52840dk/README.md)).

What it does hold, it holds for the whole window: one Ubertooth and *both*
DKs, which the longest suite makes three days, and it drives both DKs on and
off throughout. Book the pair rather than scheduling it beside other DK work;
[`docs/hardware.md`](../hardware.md) has the booking note and the disk cost.

Four suites, none of them in `fast` or in the non-soak `release` plan: a
`follow-soak` run of five minutes for development and a short bench check, then
`follow-soak-4h`, `follow-soak-24h`, and `follow-soak-72h`. Every figure the
verdict rests on is a plan argument, and the relationships between them are
checked when the plan loads, before any board is leased: a detection deadline
longer than the advertiser is on for, a follow deadline longer than the
connected phase, a checkpoint interval shorter than two cycles, and more are
each refused with their reason.

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/follow-soak.yaml --suite follow-soak \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --output runs
```

## What a long run writes, and what bounds it

Run the long suites from the deployed
[target runner](../guide/services.md#target-runner-service), as for the address
soak, and for the same reason: the driver appends a progress record to
`progress.jsonl` in the run's own capture directory as it goes and writes
`summary.json` at the end, so a soak is reviewable while it runs and after the
client has gone.

The capture files, the tool's own transcript, and `progress.jsonl` are bounded
exactly as they are there: a capture file that reaches the plan's `capture file
limit` is rolled over at the start of an absent phase, the run's whole `capture
budget` removes the oldest complete capture and its transcript past the budget
with the newest always kept, and the transcript and `progress.jsonl` have
run-wide budgets of their own. A `-f` capture records the advertising channel's
traffic *and* every connection the board follows, so it costs more per second
than the address soak's `-n` capture; no rate has been measured for it on this
bench yet, so treat the budget as the bound it is rather than as a prediction.
