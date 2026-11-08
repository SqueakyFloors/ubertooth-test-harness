# The BLE address parity soak

Reference for the `BLE address parity soak` test and the plan that runs it,
[`testplans/address-soak.yaml`](../../testplans/address-soak.yaml). What the
test itself checks, and why the two readers differ, is in
[docs/tests.md](../tests.md#ble-address-parity-soak). No soak is a release
gate; see [release-testing.md](../release-testing.md).

## The capture, read two ways

One `ubertooth-btle -n -U <serial>` capture for as long as the suite asks,
against a registered nRF52840 DK advertising its address on a schedule the
run applies, with that one capture read two ways.

The *text* reader takes the documented `AdvA:  xx:xx:xx:xx:xx:xx (random)`
line libbtbb prints for each packet it decodes, which is what a
production-style advertising-detection service parses out of the tool's
output. The *structural* reader tails the capture file as it grows, decodes
each record, recomputes the link-layer CRC, and takes the AdvA of the packets
whose CRC is right. Both addresses are normalized before anything is
compared, and what is compared is which windows each reader found the address
in, with an allowance that is a share of the windows either of them saw it in.

## What a long window measures

Around that, the run measures what only a long window finds: detection within
a deadline and sustained presence per on period, the longest silence inside
one, packets a minute, the capture process's resident memory, every process
exit with its status and last `error:` line, restarts and what caused them,
board resets, and the firmware's watchdog record, read before the run, after
each restart, and at the end.

A pass needs the detection objective met by both readers, the readers in
agreement, no gap beyond the configured limit, no restart beyond the budget, a
clean final stop, and a board that answers afterwards; every figure is
reported with its distribution either way.

## Booking a run

The test flashes the DK when it runs another firmware version, so a run holds
the [host lock](../guide/leases.md#the-host-lock) for its whole window: on the
72-hour suite no other command on that host may write firmware or switch power
for three days. Book these runs rather than scheduling them beside other bench
work.

Four suites, none of them in `fast` or in the non-soak `release` plan: an
`address-parity` run of about a minute for development and a short bench
check, then `address-soak-4h`, `address-soak-24h`, and `address-soak-72h`,
the release-candidate soak. Every figure the verdict rests on is a plan
argument, and the relationships between them are checked when the plan loads,
before any board is leased: a detection deadline longer than the advertiser
is on for, a liveness timeout the schedule's own silence would trip, a gap
limit every period crosses, and three more are each refused with their
reason.

```sh
python -m ubertooth_harness run-suite \
  --plan testplans/address-soak.yaml --suite address-parity \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --artifact "nRF52840 advertiser=/path/to/zephyr.hex" --output runs
```

## What a long run writes, and what bounds it

Run the long suites from the deployed
[target runner](../guide/services.md#target-runner-service), which keeps the
run going when whatever started it disconnects: the driver appends a progress
record to `progress.jsonl` in the run's own capture directory as it goes, and
writes `summary.json` at the end, so a soak is reviewable while it runs and
after the client has gone.

Each capture attempt keeps its own `capture-NNNN.pcap` and the tool's own
transcript in `btle-NNNN.log`, and a capture file that reaches the plan's
`capture file limit` is rolled over at the start of an off period, so no one
file grows without bound, and the run's whole `capture budget` bounds the
rest: past it the oldest complete capture and its transcript are removed, the
newest always kept, and each removal is recorded. A file the budget needs gone
and cannot remove ends the run with that as the reason, rather than writing
captures past the budget for however long the run had left.

The transcript and `progress.jsonl` have run-wide budgets instead (64 MiB
each, the transcript shared across every attempt so restarts and rotations
cannot multiply it); every line is read whether or not it is kept, so the
figures do not change, `summary.json` holds them whatever happened to either
file, and the run reports how much of each it kept.

What the captures themselves cost is the channel's traffic rather than the
advertiser's, so check the host's free space before booking a long run;
[`docs/hardware.md`](../hardware.md) has the measured rate.

The short suite also asks for a `restart probe`: the driver kills the capture
once, at the middle of the run, with no stop sent to the board — the way a
lost session ends one — and the advertiser must be heard again afterwards, so
a run carries evidence that the restart path recovers rather than only
evidence that it was never needed.
