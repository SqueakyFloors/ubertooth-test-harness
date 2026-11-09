# Ubertooth test harness

Hardware test harness for the [Ubertooth
fork](https://github.com/SqueakyFloors/ubertooth) and its companion
[libbtbb](https://github.com/SqueakyFloors/libbtbb). It builds one fixed
commit of each, flashes the boards with that build, runs a named test plan
against real radios, and records what it measured.

This file is the entry point: set the harness up, run a suite, find the
document that answers your question. The architecture is in
[DESIGN.md](DESIGN.md).

| If you want to | Read |
|---|---|
| understand how the harness is put together | [DESIGN.md](DESIGN.md) |
| know what a test does and what a pass proves | [docs/tests.md](docs/tests.md) |
| qualify a release | [docs/release-testing.md](docs/release-testing.md) |
| run every bounded release suite as one job | [docs/guide/release-gate.md](docs/guide/release-gate.md) |
| set up a bench | [docs/hardware.md](docs/hardware.md) |
| see which test uses which fixture | [docs/hardware-utilization.md](docs/hardware-utilization.md) |
| write or compile a plan | [docs/guide/plans-and-resources.md](docs/guide/plans-and-resources.md) |
| build, flash, or chase a version mismatch | [docs/guide/builds-and-firmware.md](docs/guide/builds-and-firmware.md) |
| share the bench with other runs | [docs/guide/leases.md](docs/guide/leases.md) |
| run the services or the GUI | [docs/guide/services.md](docs/guide/services.md) |
| be emailed when a long run finishes | [docs/guide/notifications.md](docs/guide/notifications.md) |
| deploy them | [docs/guide/deploy.md](docs/guide/deploy.md) |
| read a run's records | [docs/reference/run-records.md](docs/reference/run-records.md) |
| publish a release's evidence | [docs/reference/release-evidence.md](docs/reference/release-evidence.md) |
| switch USB power | [docs/reference/power-control.md](docs/reference/power-control.md) |
| know which CLI option is covered | [COVERAGE.md](COVERAGE.md) |

The persistent [target runner](docs/guide/services.md#target-runner-service)
dials the [controller](docs/guide/services.md#controller), which listens on
`127.0.0.1` only; remote access is an SSH tunnel.

## Quick start: use the web GUI from your laptop

The controller and runner run on the lab host (see
[Deploy on the lab host](docs/guide/deploy.md)); the GUI is a web page the
controller serves on the lab host's `127.0.0.1:8765`. From a laptop you reach
it through an SSH tunnel; nothing is exposed on the network.

1. **Get a checkout** of this repository on the laptop (macOS or Linux):

   ```sh
   git clone https://github.com/SqueakyFloors/ubertooth-test-harness.git
   cd ubertooth-test-harness
   ```

2. **Name your hosts once**, in a private file outside the checkout (never
   committed), readable only by you:

   ```sh
   mkdir -p ~/.config/ubertooth-harness
   cat > ~/.config/ubertooth-harness/harness-open.conf <<'EOF'
   HARNESS_HOST=<lab host, as your SSH config or the jump host knows it>
   HARNESS_JUMP=<jump host, or leave empty for a direct connection>
   EOF
   chmod 600 ~/.config/ubertooth-harness/harness-open.conf
   ```

3. **Open the GUI:**

   ```sh
   scripts/harness-open --two-hop    # through the jump host's own SSH login
   ```

   It opens the tunnel, waits until the controller answers, opens
   `http://127.0.0.1:8765/ui/` in your browser, and keeps the tunnel up until
   Ctrl-C. Use plain `scripts/harness-open` (one `ssh -J` hop) instead when
   your laptop can log in to the lab host directly with its own key.

4. **Run something:** in Run setup pick a git ref (for example `main`), a plan
   (`host` needs no board and is a safe first run), tick the host or hosts to
   run it on (every ticked host gets its own run, started together as one run
   group), keep "Build the
   ref on the target first" and "Flash the firmware to the boards first"
   ticked (the two "Build … too" boxes follow what the suite needs), and
   press Run. The Live run group card ticks off each test with one pill per
   host it is tested on; History keeps every group with each host's
   `report.md`, newest 50 first, with Target, Status and Ref filters and
   Load older behind them. Beside every
   test name is a small *i* button: it opens what that test is for, what it
   drives, and what a pass does not prove, with a link to its
   [docs/tests.md](docs/tests.md) section.

If it does not connect:

| Message | Cause and fix |
|---|---|
| `no such file or directory: scripts/harness-open` | Run it from the root of this repository's checkout. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The laptop's `known_hosts` has an old key for the lab host. Check the new key with the lab host's owner, then `ssh-keygen -R <lab host>`. `--two-hop` checks the key on the jump host instead. |
| `Permission denied (publickey...)` with plain `harness-open` | The laptop's key is not accepted on the lab host; use `--two-hop`. |
| `127.0.0.1:8765 is already in use on this machine` | Another tunnel is already open; use that browser tab, or stop the old tunnel. |
| The page loads but Run fails at once | The run's reason is shown under Run setup and in History; see [Target runner service](docs/guide/services.md#target-runner-service). |

On the lab host itself, open `http://127.0.0.1:8765/ui/` in its browser, or
use the desktop launcher from `scripts/install-desktop-shortcut.sh`.


## Environment

The harness runs only in its own Python 3.12 virtual environment, never in a
system, user, or another application's interpreter. Create or update it with
one idempotent command, on macOS or Linux:

```sh
python3 bootstrap.py          # creates or updates .venv
. .venv/bin/activate          # or call .venv/bin/python explicitly
```

[`bootstrap.py`](bootstrap.py) uses only the standard library. It finds Python 3.12 (or use
`--python PATH`), fails with install instructions if 3.12 or its `venv`
support is missing (Ubuntu: `python3.12-venv`), creates `.venv` with a
harness marker file, and installs the pinned [`requirements.txt`](requirements.txt) with the new
environment's own interpreter, pip's virtualenv guard on, and the user site
disabled; then it runs `pip check`. Rerunning reuses the environment. It
refuses to modify an existing directory that lacks the marker, and
`--recreate` rebuilds only a marked harness environment. `--venv PATH` selects
another location.

`run-suite` and `build` refuse to start outside a bootstrapped harness
environment. Every run manifest (schema version 5)
records the Python version, implementation, executable and prefix, pip and
dependency versions, OS and release, and CPU architecture under
`environment`. Commands below assume the environment is active.

## Validate plans

```sh
python -m ubertooth_harness validate-plan testplans/slow.yaml
```

A plan may name only tests that have an implementation, since `run-suite`
refuses a suite with one that has none. `validate-plan` (and so CI) fails on
such a plan, unless the plan says `runnable: false` at the top: that marks
a plan of planned tests. [`testplans/backlog.yaml`](testplans/backlog.yaml)
(peer tests, `BLE monitor`, and the soaks) and
[`testplans/mac-polling.yaml`](testplans/mac-polling.yaml) are the two such plans.

## Run a suite

```sh
python -m ubertooth_harness validate-plan testplans/fast.yaml \
  --hardware hardware.yaml --suite fast

python -m ubertooth_harness run-suite \
  --plan testplans/fast.yaml --suite fast \
  --hardware hardware.yaml --host Host_Linux \
  --ubertooth-source /path/to/ubertooth --ubertooth-ref main \
  --libbtbb-source /path/to/libbtbb \
  --libbtbb-ref d356e9da7fb03969371aa14d603a8aad2f0ef198 \
  --build --output runs
```

`--libbtbb-ref fork-pin` resolves per run from the Ubertooth commit under
test, which is what a deployed runner uses; the commit above is the pin this
repository builds by default.

`--dry-run` prints what would run on this host, and what would be left out and
why, without leasing or building anything. Each run writes one directory
holding `run.jsonl`, `report.md`, and the output files its cases saved.

Useful options, in the order they come up:

| Option | What it does |
|---|---|
| `--build` | clone and build the resolved commits into the run's own prefix |
| `--with-firmware`, `--with-btbr`, `--with-python` | also build the application image, the alternate `btbr` image, the Python tools |
| `--no-flash-build` | keep the boards' current firmware instead of writing the build's |
| `--bootloader record\|check\|update` | record, compare, or install the bootloader under the firmware |
| `--artifact "KGV=<path>"` | supply a pinned image a test alternates against |
| `--allow-power-changes` | arm real USB power and data changes |
| `--allow-hci-capability` | let the run grant `cap_net_admin+ep` to a private copy of its own `ubertooth-follow`, so the tests that page a target over HCI can run; taken back when the run ends ([hardware.md](docs/hardware.md)) |
| `--allow-destructive` | arm a test that can leave a board needing the bench |
| `--test "<name>"` | run one test of the suite |

Plans live in [`testplans/`](testplans); the registry of this bench's hardware
is `hardware.yaml`, which is ignored — copy
[`hardware.example.yaml`](hardware.example.yaml) to start. A test that needs a
board behind an attenuator asks for it with `weak link:`
([plans-and-resources.md](docs/guide/plans-and-resources.md#weak-link-the-shorthand-for-the-usual-case)).

## Other commands

| Command | What it does |
|---|---|
| `boards` | which registered boards, and this host's power/firmware lock, are free or leased, and by whom |
| `lease ... -- <command>` | hold boards while running one command by hand |
| `power <controller>` | read a power controller's ports; changes nothing |
| `build` | build and install the resolved commits into a prefix |
| `validate <run dir>` | re-run a finished run's recorded output checks |
| `report <run dir>...` | render one Markdown report from one or more runs |
| `release-evidence <run dir>...` | build one release's public-safe summary, sanitized logs, and sums ([release-evidence.md](docs/reference/release-evidence.md)); uploads nothing |
| `runner`, `controller` | the persistent services ([services.md](docs/guide/services.md)) |
| `notify-test` | send one test completion notice, to check that setup ([notifications.md](docs/guide/notifications.md)) |

By-hand bench helpers (holding a device, driving a peer, switching a hub port)
are in [`scripts/lab/`](scripts/lab/README.md).

## Tests

```sh
python scripts/test.py                 # the whole suite, one process per core
python scripts/test.py --durations 10  # with the ten slowest tests
python scripts/test.py test_gui -v     # one module, naming each test
python -m unittest discover -s tests   # the same tests, serially
```

Every test must pass both ways: no test may depend on another module's state,
a fixed port, or a lock file another module uses. CI runs the suite and
validates the committed plans with Python 3.12 on Ubuntu 24.04 and the current
macOS, and its build job builds the fork's host tools and runs
`testplans/host.yaml`, which needs no Ubertooth.

[`COVERAGE.md`](COVERAGE.md) lists every Ubertooth host CLI option, which test
covers it, and what is still hard to test. Use it to pick the next test.

## Repository boundary

This repository owns the controller and GUI, the target runners, the test
plans, provisioning, the private lab adapters, and the result formats. Product
host and firmware code, and deterministic C tests, stay in the Ubertooth
repository: runs consume explicit repository URLs and refs plus pinned
firmware artifacts, and the harness imports no product internals.

It is public. Keep bench- and device-specific details (host names, serials,
endpoints, credentials) in private configuration, and never commit
credentials or captured device traffic.

## Networking status

The only listener is the controller, on `127.0.0.1`; the target runner only
dials a loopback controller, and remote use goes through SSH tunnels. Before
binding beyond loopback or connecting remote runners directly, define and
approve the HTTPS, browser authentication, runner authentication, outbound
connection, reconnect, and authorization contract. The provisional
shared-token text in [DESIGN.md](DESIGN.md#open-items) is not production
authorization.

## License

GPLv2; see [COPYING](COPYING).
