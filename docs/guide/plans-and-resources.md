# Plans and registered resources

How a plan names tests, how the registry names hardware, and how the planner
binds the two. Start at [the README](../../README.md); the per-test reference is
[docs/tests.md](../tests.md).

## Named tests and registered resources

Plans contain human-readable test names with direct arguments—never commands or
expected output:

```yaml
fast:
  - Host CLI surface:
      hosts: each

  - Ubertooth identity:
      hosts: [Host_Linux, Host_Mac]
      Ubertooth: default
```

The built-in catalog owns descriptions, accepted/default arguments, hardware
roles, exact argv, timeouts, and result checks. Every plan item must contain
exactly one known test name; unknown names, legacy `cmd/check` entries, unknown
arguments, and invalid values fail during load.

[`docs/tests.md`](../tests.md) describes every catalog test: what it does,
how it produces a known answer, the hardware roles and arguments it needs,
what passes and fails, and what a pass does not prove. Read it to decide which
tests a plan should hold.

Every test accepts `platforms:` (`linux`, `macos`, no duplicates) beside its
`hosts:`, and a test left without it runs on every platform:

```yaml
  - Host CLI surface:
      hosts: each
      platforms: [linux]
```

Every test that binds hardware also accepts `rf:`, which asks for a board by
where it sits on the bench, one requirement per hardware role, and
`weak link:`, the shorthand for one role at an attenuation in dB (see
[RF placement](plans-and-resources.md#rf-placement-where-a-board-sits-on-the-bench)). It also
accepts `required hardware: true`, which says the hardware that step names is
what the step is there to measure: a registry without it blocks the step and
fails the run instead of leaving it out (see
[Hardware a step is about](plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out)).

The two select separately: `hosts:` says which registered hosts the step may
run on, `platforms:` which of them can run it. A host whose platform the step
does not list is left out as not applicable (see
[Host requirements](plans-and-resources.md#host-requirements)), so one plan can hold Linux-only,
macOS-only, and shared steps and still run on `hosts: each`; a step no named
host can run fails planning. Three tests must state their platforms and still
require the argument: `Flash reliability`, `Exclusive Ubertooth topology`,
and `MAC polling soak`.

[`docs/hardware.md`](../hardware.md) describes the bench itself: each device
type and its role, the firmware each one runs and how it is built, flashed and
checked, the APIs and protocols the harness depends on, host setup, the known
limits of a crowded USB host, and a worked bench layout. Read it to set up a
bench, or to judge what a result was measured on. The by-hand helpers that go
with it are in [`scripts/lab/`](../../scripts/lab/README.md): `hold` holds devices
by serial for one command, `peer` drives the Classic Bluetooth peers by
registry alias, and `hub-port` switches a hub port to recover a device that has
stopped answering. Each runs the harness of the checkout it lives in,
whichever interpreter starts it and from whatever directory: it re-runs
itself under that checkout's own bootstrapped environment, carries on in any
other the harness bootstrapped if its own checkout has none, and refuses an
interpreter nobody bootstrapped. So a wrapper on a bench names its registry
and `exec`s the helper by path, with nothing in it about which harness runs
([`scripts/lab/README.md`](../../scripts/lab/README.md)).

`hardware.yaml` is the ignored local resource registry; copy
[`hardware.example.yaml`](../../hardware.example.yaml) to start. Register every macOS/Linux computer as a
`Host`, then attach typed resources such as `UT_Linux0`, `DK_Linux0`, and
`Hub_Linux0`. Aliases are for humans—the explicit type, host attachment, and
stable serial/probe identity are authoritative. A host's `defaults` selects its
preferred Ubertooth so repeated reliability tests stay on one device.

Selection is deterministic: `default`, `each`, or an explicit resource alias.
Ambiguous `any` and random selection are rejected. A test with several hardware
roles may name each one by alias or narrow it by RF placement, but not with
`each`; a future explicit `all pairs` policy will be separate. Target-runner
discovery is reconciled as present/missing/unknown before execution and never
silently reassigns an alias.

### Hardware a registry has not got

One plan is compiled against every bench's registry, so a step may name a
host this registry has not got, or a device no host here has. Run planning
records that as not applicable and leaves the step out, the way it leaves out
a host whose platform the step does not list:

- a `hosts:` alias this registry does not have (`not applicable on Host_Lab:
  Host_Lab is not a host in this registry`);
- a host with no device for a hardware role the test binds: no default for
  the role (`host Host_Mac has no default ESP32 Classic peer for the acceptor
  role`), no device of the type at all (`host Host_Mac has no nRF52840 DK
  resources`), or no device that declares the role (`host Host_Mac has no
  nRF52840 DK for the advertiser role (DK_Mac0 declares peripheral)`) — when
  the host's default cannot take the role but another device of its can, that
  one takes it, as it does for a placement the default cannot satisfy;
- an explicit device alias this registry does not have (`this registry has no
  Ubertooth 'UT_Lab7'`).

So a plan every registered host runs can hold a step for hardware only the
bench host has: the other hosts, and the other registries, record it and run
the rest. Where such a step is a gate rather than a convenience, the plan
says `required hardware: true` and the record fails the run instead (the next
section); the release plan's one peer step does.

A suite with nothing this registry can run is still an error, and it names
the first few steps it left out. A host this registry has not got never runs
anything of its own, so what it left out is recorded in the runs of the hosts
that are here, and `run-suite --dry-run` lists it; one record per test, so a
host that runs that test, or has its own reason for leaving it out, keeps its
own. A blocked record for such a host is carried the same way, by the runs of
the hosts that are here, unless one of them runs that test: that run measures
what the step is about, so none of them fails on the absent host's part of it.

A record for a host the registry *does* have belongs to that host's run
alone, which is the rule for every blocked record. So a bench whose gate host
is registered but has lost the hardware fails that host's run, and a run of
the same plan on another of its hosts passes without the gate: the release is
run on the gate host, or as a run group over the bench, which is where the
measurement is.

What a host here cannot run is decided first, and has not changed: a platform
the step does not list, and a board placed differently from what the test
needs, leave that host out, and a step whose every named host is here and
cannot run it this way still fails planning. Only when the hosts that could
have run it have not got the hardware is the step left out — and a step that
names a host this registry has not got is left out either way, since the host
that would have run it is not here to be judged.

`validate-plan --hardware <registry>` is the strict one: there the registry
is named on purpose, so the same absence fails, which is what catches a plan
entry for hardware a bench is meant to provide; it refuses a blocked step the
same way. Every committed suite is
compiled that way against [`hardware.example.yaml`](../../hardware.example.yaml) in
the unit suite (`tests/test_runnable_plans.py`), bar `range-weak-link`, which
asks for a board behind an attenuator that the example registry carries
commented out; CI validates the main suites with it as well.

Because it is the stricter of the two, `validate-plan --hardware` refuses a
plan on a bench that has only some of the hardware it names, where the run
itself would leave those steps out. On such a bench, check a run with
`run-suite --dry-run`, which lists exactly what would run and what would be
left out.

Two refusals are mistakes in the plan rather than absence, and fail wherever
the plan is compiled: an alias that differs from a registered one only in
case (`it has 'Host_Linux', so check the spelling in the plan`), and a
registered alias used as something it is not (`resource UT_Linux0 is not a
Host`, `resource DK_Linux0 is not a Ubertooth attached to Host_Linux`,
`resource DK_Linux1 declares the role(s) peripheral, not advertiser`). A role
or argument name the test does not have is refused when the plan loads, with
no registry at all.

### Hardware a step is about: blocked, not left out

Two absences are not quietly left out, because the hardware that is missing is
the subject of the test rather than something it merely binds:

- `alternate Ubertooths: true` on a host with one registered Ubertooth
  (`alternate Ubertooths needs at least two registered Ubertooths on
  Host_Linux, which has one`);
- `exclusive Ubertooth: true` where another registered board on the host is on
  no power controller (`extra Ubertooth UT_Linux1 has no power mapping, so it
  cannot be switched off to leave UT_Linux0 alone`);
- a step that says so itself with `required hardware: true`, for the hardware
  it binds. That is how a gate suite holds a test for peer hardware: a host
  with no peer registered (`host Host_Linux has no default ESP32 Classic peer
  for the acceptor role`), and a registry without that host at all
  (`Host_Linux is not a host in this registry`), are both blocked rather than
  left out. `testplans/release.yaml` uses it for `Recover AFH map`, the one
  entry there that names a bench host. A step without the argument, or with
  it written `false`, is left out as before.

The argument is about absence only. What a host here cannot run at all — its
platform, or a board placed differently from what the test needs — is decided
first and is unchanged by it.

Such a step is recorded as **blocked** before anything runs, and it is left
out of the invocations so every unrelated step in the suite still compiles and
runs. The run then finishes `failed`: a gate like these is named on purpose on
the bench that has the hub or the second board, so a quiet "not applicable"
would read as a pass for something nobody measured. The record
is a `blocked` event in run.jsonl, a `Blocked` section in report.md,
`blocked_tests` in the run summary, and `blocked` in the web GUI's failures.
A run that was canceled still ends `canceled`, with the blocked test named in
its reason: nothing after the cancellation ran either.
`run-suite --dry-run` lists them and exits 1, because a dry run that exits 0
says this host can run the suite. Each record belongs to the host it is
about, so another bench's missing hub does not fail this run.
`validate-plan --hardware` refuses both outright, as it does every other
absence.

The controller's [run setup](services.md#web-gui) does not offer a run that could not
measure such a step in the first place: where the step's `hosts:` names no
host of the run, no bench can make that run measure the gate, so the run is
refused with the hardware the step is about named (see
[Run setup](services.md#web-gui)). Absence *on* a host of the run is a different thing
and still starts: that is the bench's own state, which the run reports by
blocking the step and failing, with every other test run.

A suite left with nothing at all to run is still refused at planning, as it
was: the refusal names the blocked step, and there is no run to fail. That is
what `mac-polling` does on a bench with an unmapped board, since it names one
step and one board (`Ubertooth: default`). `testplans/exclusive.yaml` names
one step but `Ubertooth: each`, so the step runs for every board that can be
left alone, is blocked for the rest, and the run fails. A mixed suite, one
holding a blocked step beside steps that run, behaves the same way:
`testplans/release.yaml` is that suite on a bench without the ESP32 Classic
peers, where every other release test runs and the run fails on the one gate
it could not measure.

A `power:` mapping that is present but wrong (an unknown controller, a port
the controller has not got, a controller that cannot switch one) is a registry
to correct rather than a bench without hardware, so it is refused rather than
recorded. Where, depends on how far the registry can be judged on its own:
when it loads, for the drivers it knows how to check; otherwise when the plan
compiles; and for a `driver` no controller is written for, when the run builds
its controller, before any port is switched (`unsupported power-controller
driver`). A serial too short to select an alternated board with is likewise a
registry to correct, and fails planning.

### RF placement: where a board sits on the bench

A unit may record its RF placement, so a test can ask for a weak link and a
report can say what its figures were taken at. `rf:` is set by hand and
describes the bench; the harness never measures it:

```yaml
  UT_Linux2:
    type: Ubertooth
    attached to: Host_Linux
    identity:
      serial: ...
    rf:
      antenna: true        # is the antenna attached
      attenuation_db: 20   # inline attenuation between the board and the air
      distance_m: 1        # how far from the rest of the bench (optional)
```

A unit with no `rf:` is taken to be an ordinary one: antenna attached, no
attenuation, distance not recorded. Keep the numbers current whenever the
attenuator or the position changes. A `Host` and a `USB power controller` may not
have a placement: neither has a radio.

A plan asks for a placement per hardware role:

```yaml
  - Classic Bluetooth receive:
      hosts: [Host_Linux]
      coverage: smoke
      duration: 10s
      rf:
        Ubertooth: {min_attenuation_db: 20, max_attenuation_db: 20}
```

A requirement takes `min_attenuation_db`, `max_attenuation_db`, and `antenna`,
and it narrows selection before the selector is read. `default` takes the
host's default only when that board satisfies the requirement, and otherwise
the lowest-alias board on the host that does; `each` covers the matching
boards alone; an explicit alias that cannot satisfy the requirement fails
planning. A host with no board placed for a role is recorded as not applicable,
naming the requirement and what the host has instead.

#### `weak link:`, the shorthand for the usual case

Most plans want one thing: this role on the attenuated board, every other role
on an ordinary one. `weak link:` says that with the attenuation as its only
number:

```yaml
  - Capture known BLE advertiser:
      hosts: [Host_Linux]
      coverage: smoke
      duration: 5s
      weak link: {Ubertooth: 20}
```

That is the same as asking for `min_attenuation_db: 20, max_attenuation_db: 20,
antenna: true` on the `Ubertooth` role and `max_attenuation_db: 0,
antenna: true` on every other role of the test. Exactly 20, not at least:
figures taken behind 20 dB say nothing about a bench with 10 dB or 40 dB in
the path, so a bench carrying a different attenuator fails planning with the
requirement in the message rather than running against numbers taken
elsewhere. Swapping the attenuator is then a one-line plan change. A test
takes `rf:` or `weak link:`, not both, and the attenuation must be above 0 dB,
since an ordinary board is what every other role gets anyway.

[`testplans/weak-link.yaml`](../../testplans/weak-link.yaml) collects the checks
that need the bench's weak-link board: error flags and recovery under bit
errors, the `ubertooth-rx -e` threshold, AFH map recovery on a marginal
link, transmit power steps, and receiver sensitivity.

The entries a run has characterized live in the plan they belong to, each
carrying the figures it was judged against: `ble-weak-link` in
[`ble.yaml`](../../testplans/ble.yaml) (a known DK advertiser at 20 and 40 dB),
`connection-weak-link` in the same plan (a known DK-pair connection captured
at 40 dB and followed across its end at 20 dB),
`classic-weak-link` in [`classic.yaml`](../../testplans/classic.yaml) (a known
piconet followed at 20 dB), and `range-weak-link` in
[`range.yaml`](../../testplans/range.yaml). None of them changed a threshold: each
is the committed test pointed at a weak-link receiver.
[`testplans/weak-link.yaml`](../../testplans/weak-link.yaml) holds the rest, which
now waits on a measurement or an implementation rather than on hardware, and
`ubertooth_harness/weaklink.py` names what each is waiting on.

A **runnable** plan may only ask for a link a figure has been taken over, and
that is judged **per test** rather than per bench: the requirement must pin
exactly one attenuation, and `weaklink.CHECKS` must record a measurement of
that test, in that role, there. 40 dB is measured for a BLE advertiser
capture and not for a Classic capture file, so a bench-wide list would have
admitted the second on the first's evidence. An attenuation the bench has
been taken to and found to settle nothing is refused with that as the reason
(`weaklink.CHARACTERIZED_UNUSABLE_DB`), rather than being reported as merely
unmeasured. A bare `min_attenuation_db` is
refused as well, because which board it binds depends on what the bench
happens to hold. `validate-plan` applies this to a whole plan and `run-suite`
to the suite it is about to run, so a suite is refused before a board is
touched.

Some tests ask for an ordinary link themselves, so a plan need not:
`Classic Bluetooth receive`, `Raw receive`, `BLE extcap capture`, `BLE extcap
in tshark`, `Capture known BLE advertiser`, `Capture known BLE connection`,
`Follow a BLE connection across its end`, `Classic capture record fields`,
`Capture known Classic device`, `Follow Classic Bluetooth piconet`,
`Recover AFH map`, `Bluetooth inquiry scan`, and
`Follow Classic piconet over HCI` require `antenna: true` with no attenuation on their
`Ubertooth` role (`catalog.RF_DEFAULTS`), which keeps those captures,
surveys, and followed connections off an attenuated board. That is a list,
not a rule: other tests take captures too and carry no default, so read
`RF_DEFAULTS` rather than inferring from what a test does. A plan's `rf:` or
`weak link:` for a role overrides the default, which is how one of these
tests is pointed at the attenuated board on purpose.

Every run records the placement of each board it used that records one, in
`run.jsonl`: on each invocation of the `manifest` record
(`resource_placements`) and in each `test_started` record (`rf`); a board with no `rf:` is absent, since
the registry does not say where it sits. What each role asked of its placement
is recorded beside it (`placement_requirements` and `rf_required`), so the run
record says why a role got the board it did when the host default was passed
over or `each` covered fewer boards than the host has. The report shows the placement in an `RF placement` setup row and beside the
resources of every result, so a range, transmit power, or RSSI figure is read
next to the attenuation it was taken at.

### Host requirements

A case or a whole test declares what it needs from the host in one place
(`ubertooth_harness/hostneeds.py`, `Needs`): a host platform (also the plan's
`platforms:` argument, which every test accepts), a tool built only with
BlueZ, a host program the harness does not install, the Python tools build,
or a firmware build. It is resolved before anything runs, from the host's
registry platform and the run's options and build manifest. What the host
cannot run is left out and recorded as `not applicable on <host>: <reason>`,
counted apart from skips and failures: a `not_applicable` event per case (or
whole test) and a `host_capabilities` event in run.jsonl, a `Not applicable`
section, a `Not applicable cases` column, and a `Host can run` row in report.md,
`not_applicable_cases` and `not_applicable_tests` in the run summary, and
`not applicable` in the web GUI. `run-suite --dry-run` prints the same decision
for every case before any build, lease, or run. A plan entry that no host it
names can run (no case applies on any of them) fails planning, unless what
the hosts lack is the hardware it binds, which is recorded instead (see
[Hardware a registry has not got](#hardware-a-registry-has-not-got)). A test
whose every case is not applicable finishes `not_applicable` and does not
fail the run. A test the bench has not got the hardware the test is *about*
is `blocked` instead and does fail the run (see
[Hardware a step is about](plans-and-resources.md#hardware-a-step-is-about-blocked-not-left-out)).

| Needs | Cases | Not applicable when |
|---|---|---|
| BlueZ (`ubertooth-scan`, `ubertooth-follow`) | `Host CLI surface` scan and follow `-h`; every `follow` and `scan` case of `Refused command lines`; every case of `Bluetooth inquiry scan` and `Follow Classic piconet over HCI` | macOS (no BlueZ): Linux only; on Linux, a run that uses installed tools without them on `PATH` (a Linux `--build` configures with `REQUIRE_ALL=ON` and fails unless both are in the run prefix, so its cases are required) |
| host tools the harness does not install (`tshark`, `hciconfig`, `hcitool`) | `BLE extcap in tshark`; every case of `Bluetooth inquiry scan` and `Follow Classic piconet over HCI` | the program is not on the host's `PATH` |
| Python tools build | `Python tools` `ubertooth-specan-ui -h` | the run has no `--build --with-python` |
| firmware build | `DFU image checks` `sign-test-build` | the run has no `--build --with-firmware` and no `--artifact "test build=PATH"` (a `--build` run that flashes its boards builds the firmware anyway, so the case applies there) |
| platform | any plan entry with `platforms:`, which every test accepts (the `exclusive` and `mac-polling` plans list Linux only; `Flash reliability`, `Exclusive Ubertooth topology`, and `MAC polling soak` must state it) | the host's platform is not listed |

Runtime skips remain only for what running the command alone can tell: a
built libubertooth that predates `UBERTOOTH_TEST_NO_DEVICES` (the refusal
cases' probe), and an interpreter that cannot import numpy, qtpy, and a Qt
binding (specan-ui). The BLE extcap cases need no declaration: btle-extcap.py
is read from the resolved commit and runs `ubertooth-btle`, which every build
has.

