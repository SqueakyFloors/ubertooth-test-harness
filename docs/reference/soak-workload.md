# The MAC polling soak workload

Reference for the one long-running workload the harness models itself, the
`MAC polling soak` catalog test. It is **not implemented**: `validate-plan`
compiles [`testplans/mac-polling.yaml`](../../testplans/mac-polling.yaml),
which is marked `runnable: false`, and `run-suite` refuses it. No soak is a
release gate; see [release-testing.md](../release-testing.md).

## The capture

One persistent capture, selecting its board by serial:

```
ubertooth-btle -n -U <serial>
```

`-n` decodes advertising packets, and `-U <serial>` names the board the plan
bound, so the workload never depends on which board a no-selector tool happens
to open.

## The workload the harness models in-process

Around that capture the harness keeps the polling behavior of a
production-style MAC polling workload that runs on the lab host, with every
interval and bound a plan argument rather than a constant in code:

- every `AdvA` address is taken from the capture as it arrives, independent of
  log throttling;
- `bucket count` in-memory address buckets, rotated every `bucket rotation`;
- an address counts as found only when it is in every bucket;
- target lookups every `query every`;
- `listener silence timeout` without any received address marks the listener
  unhealthy;
- empty buckets persisting for `empty buckets restart after` start restart
  handling, with the `restart suppression`, `post-restart wait`, and
  `restart timeout extension` intervals;
- `maximum consecutive misses` and `maximum restarts` are the pass bounds.

The harness models those semantics in its own process. It must not start the
original application, and must not add a network listener: the only listener
in this repository is the controller on `127.0.0.1`.

## What the plan binds

One registered Linux host, that host's preferred Ubertooth, and a registered
nRF52840 DK advertiser whose BLE address comes from its resource identity
rather than from plan text. The catalog also requires
`exclusive Ubertooth: true`, so the planner records a power-off action for
every other registered board on the host and the run checks the enumeration
before the workload starts
([power-control.md](power-control.md)). Unknown or unpowerable extra boards
fail closed.

Validate its catalog and resource bindings with:

```sh
python -m ubertooth_harness validate-plan testplans/mac-polling.yaml \
  --hardware hardware.yaml --suite mac-polling
```
