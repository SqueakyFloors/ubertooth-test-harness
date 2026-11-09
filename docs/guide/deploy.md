# Deploying the controller and runners

## Deploy on the lab host

Single-node deploy: the controller and one runner run as systemd user services
of the account that owns the checkout (no sudo needed), the GUI is opened from
the desktop or through a tunnel. The scripts touch only the checkout's
`.venv`, the harness data directory, the two user units, and the desktop
launcher. The controller runs on Linux; a Mac test host gets a runner of its
own, as launchd LaunchAgents, from the same script (see [A Mac test
host](#a-mac-test-host)).

As the service user, from a clone of this repository (`git pull` it to
update), with `hardware.yaml` describing the host:

```sh
scripts/deploy.sh --runner-host Host_Linux --ubertooth-source ~/ubertooth
```

It runs `python3 bootstrap.py`, writes `ubertooth-harness-controller.service`
and `ubertooth-harness-runner.service` to `~/.config/systemd/user/`, then
`systemctl --user daemon-reload` and `enable --now`, and prints their status
and the GUI URL. The controller runs `.venv/bin/python -m ubertooth_harness
controller` on `127.0.0.1:8765` (`--port N` to change it) with results in
`${XDG_DATA_HOME:-~/.local/share}/ubertooth-harness/controller/runs`; the
runner (`After=`/`Wants=` the controller) dials
`ws://127.0.0.1:PORT/ws/runner` and writes its run directories to
`.../ubertooth-harness/runner/runs`. Both have `Restart=on-failure`. Options:

| Option | |
|---|---|
| `--runner-host NAME` | registry Host resource of this machine (required) |
| `--ubertooth-source DIR` | Ubertooth checkout the runner runs the listed commit from (required; a commit missing there is fetched from its `origin` at run time) |
| `--hardware FILE` | resource registry (default `hardware.yaml` in the checkout); it is loaded with this checkout's own code, and one this code cannot read ends the deploy rather than the next run or hold (`--dry-run` says it would and carries on) |
| `--plans-dir DIR` | plans for the GUI and the runner (default `testplans/`) |
| `--ubertooth-repo URL` | repository whose refs the GUI offers (default the public fork) |
| `--libbtbb-source DIR` | libbtbb checkout the runner builds from (default: deploy's own clone of the [libbtbb fork](https://github.com/SqueakyFloors/libbtbb), see below) |
| `--libbtbb-ref REF` | libbtbb commit the runner builds (default `fork-pin`: the pin of each Ubertooth commit it builds, see below) |
| `--no-libbtbb` | no libbtbb: the runner cannot build and refuses runs with Build checked |
| `--allow-power-changes` | passed to the runner |
| `--allow-hci-capability` | passed to the runner: a run may grant `cap_net_admin+ep` to a private copy of its own `ubertooth-follow` for the length of the run, so the tests that page a target over HCI can run. Needs libcap and a sudoers rule letting the runner user run `setcap` without a password ([hardware.md](../hardware.md)); without it those tests are recorded blocked |
| `--with-acroname` | bootstrap with the Acroname BrainStem API (`python3 bootstrap.py --with-acroname`), for a USBHub3+ power controller |
| `--external-board-lock TEMPLATE` | passed to the runner: runs also hold a lab's own per-board lock files ([External board locks](leases.md#external-board-locks)); `~/` is expanded here |
| `--port N` | controller port on `127.0.0.1` (default 8765); on macOS the port the tunnel forwards at both ends |
| `--controller-host HOST` | macOS only: the Linux host running the controller |
| `--controller-jump JUMP` | macOS only: SSH jump host for that connection (`""` for none) |
| `--no-start` | write the units or agents only (Linux also daemon-reloads) |
| `--uninstall` | stop and remove this machine's units or agents only; the venv and results stay |
| `--force` | restart even while a run is active (cancels it) |
| `--dry-run` | print what it would do, change nothing |

`--ubertooth-repo` is a controller option, so it is refused on macOS;
`--controller-host` and `--controller-jump` are refused on Linux, where the
controller is on the same machine.

**libbtbb for Build.** A GUI run with Build checked builds libbtbb too, so by
default the deployed runner gets one. Without `--libbtbb-source`, deploy
clones `https://github.com/SqueakyFloors/libbtbb.git` into
`${XDG_DATA_HOME:-~/.local/share}/ubertooth-harness/sources/libbtbb` the first
time and fetches it (branches and tags) on every later deploy; a path there
that is not a git clone is refused, and a failed clone stops the deploy
before any unit is written (pass `--libbtbb-source` or `--no-libbtbb`). A
`--libbtbb-source` you name is used as it is, never cloned or fetched by
deploy. Without `--libbtbb-ref`, the runner gets `fork-pin`, so every run
builds the libbtbb the Ubertooth commit it was asked for pins in its own
`install.sh` (see [Building the resolved
revision](builds-and-firmware.md#building-the-resolved-revision)): deploy reads no pin itself, and a
pin bump on the fork reaches the runner with no redeploy. Name a commit with
`--libbtbb-ref` to hold every run to one libbtbb instead. Each clone or fetch
is limited to 60 s and never prompts for credentials. `--no-libbtbb` deploys a
runner without libbtbb, which refuses Build runs up front with a clear reason.

Rerunning with the same options changes nothing. A unit is rewritten, and its
service restarted, only when its content changes; the units record the
checkout's commit, so a deploy after `git pull` restarts both. Uncommitted
edits and dependency updates alone do not change the units; restart by hand
after those (`systemctl --user restart ubertooth-harness-controller.service
ubertooth-harness-runner.service`). Restarting either service cancels an
active run (cleanly, restoring its boards), so a deploy that would restart
them refuses while the controller reports an active run; `--force` restarts
anyway.

The runner protocol has no version negotiation: the controller and runner
must speak the same version: protocol v2 (which added the listed commit to
`run`), v3 (which added `flash_build`), v4 (which added the `run_file`
request and its answers), v5 (which added the run's `with_firmware` and
`with_python`), v6 (which added its `bootloader`), v7 (which added its
`with_btbr`) and v8 (which added per-board status to `register` and
`heartbeat`, with the board-management requests) are each a hard break
from the version before. An older runner is refused at `register` (the
controller closes the connection, code 1008), and since it did register it
reconnects after the shortest backoff (0.5 s) and is refused again,
indefinitely; its log shows `registered with ...` then `controller connection
closed` over and over, and the target never comes online. A newer runner
against an older controller is refused the same way. `deploy.sh` installs both services
from the same checkout and restarts both when its commit changes, so a
single-node deploy is always matched; a runner started by hand (or on another
machine) must be updated and restarted together with the controller.

User services stop when the user's last session ends unless systemd linger
is on. `deploy.sh` checks `loginctl show-user $USER -p Linger` and, when it is
`no`, prints the one-time command for an administrator; it never runs sudo
itself:

```sh
sudo loginctl enable-linger <user>
```

**Desktop shortcut.** On the lab host's own desktop, run as the desktop
(auto-login) user, not the service user:

```sh
scripts/install-desktop-shortcut.sh [--port 8765]   # --uninstall removes it
```

It writes `~/.local/share/applications/ubertooth-harness.desktop` (and a copy
on `~/Desktop` when that exists, marked trusted with `gio`) that runs
`xdg-open http://127.0.0.1:PORT/ui/`. The controller is on the machine's
loopback, so any local user's browser reaches it (see the trust boundary in
[Controller](services.md#controller)). The script is self-contained; copy it if the
desktop user cannot read the service user's checkout.

**From a laptop.** `scripts/harness-open` (macOS or Linux) opens the tunnel,
waits until it answers, opens the GUI in the browser, and keeps the tunnel in
the foreground until Ctrl-C:

```sh
export HARNESS_HOST=lab-host.example HARNESS_JUMP=jump-host.example
scripts/harness-open                     # ssh -J $HARNESS_JUMP $HARNESS_HOST
scripts/harness-open --two-hop           # ssh the jump host, then the lab host
scripts/harness-open --host lab2 --jump bastion --port 8765 --dry-run
```

No host is built in: `--host`, `HARNESS_HOST`, or `HARNESS_HOST=...` in a
private config file names the lab host, and `--jump` / `HARNESS_JUMP` /
`HARNESS_JUMP=...` the jump host (unset or empty means a direct connection).
`HARNESS_PORT` sets the default port. An option wins over the environment,
which wins over the file; with no host the script stops and says how to set
one. An empty `HARNESS_JUMP` is a choice (no jump host) and wins, while an
empty `HARNESS_HOST` or `HARNESS_PORT` falls through to the file.

The config file is `$HARNESS_OPEN_CONFIG`, by default
`$XDG_CONFIG_HOME/ubertooth-harness/harness-open.conf` (so
`~/.config/ubertooth-harness/harness-open.conf` unless `XDG_CONFIG_HOME` is
set), outside the checkout and never committed. `deploy.sh` on a Mac reads the
same file for its tunnel. It is sourced as shell, so it
must be owned by you and not writable by anyone else; the script refuses it
otherwise, and reports a file that fails rather than exiting silently.

The forward is `-L 127.0.0.1:LOCAL:127.0.0.1:PORT` on every hop: it binds
only the laptop's loopback and reaches only the controller's loopback listener
(no `-g`, no `GatewayPorts`). When a jump host is set, it relays the
connection (`-J`). Use `--two-hop` when the jump host refuses that
("administratively prohibited" or "stdio forwarding failed") or when the lab
host's name is known
only to the jump host's SSH configuration. It relays through a Unix socket on
the jump host that only your account there can open (mode 0600, default
`/tmp/ubertooth-harness-USER-PORT.sock`, `--jump-socket` to pick another),
not a TCP port: the controller has no authentication, so a loopback port on
a shared jump host would let every user there drive it. It overrides a
`RemoteCommand` that the jump host's SSH configuration sets. With `-J`, the
lab host's key is checked against the laptop's `known_hosts`; with
`--two-hop`, against the jump host's. Keep
`--local-port` equal to `--port`: the controller accepts only
`Host: 127.0.0.1:PORT`.

**Screen sharing** of the lab host's desktop (for example GNOME Remote
Desktop or VNC, itself through an SSH tunnel) is an alternative when a browser
on the lab host is easier than a tunnel; it needs no harness changes.

### A Mac test host

Tests that need macOS run on a Mac with its own Ubertooths, and the plans,
history and GUI stay in the one controller on the Linux host. That controller
binds `127.0.0.1` and has no authentication, so the Mac reaches it through an
SSH local forward that launchd keeps up: the key that forward uses is the
access control, and nothing on the Mac is reachable from the network. The
forward does bind the Mac's own loopback, so every local user and process
there can drive the controller through it, exactly as the `ssh -R` case in
[Controller](services.md#controller) describes; run it only on a Mac whose local users
you trust. Run `deploy.sh` there as the user who is logged in to the Mac's
desktop, with a `hardware.yaml` that registers that host and its boards:

```sh
scripts/deploy.sh --runner-host Host_Mac --ubertooth-source ~/ubertooth \
    --controller-host lab-host.example --controller-jump jump-host.example
```

It bootstraps `.venv`, writes `com.ubertooth-harness.tunnel.plist` and
`com.ubertooth-harness.runner.plist` to `~/Library/LaunchAgents`, and loads
them with `launchctl bootstrap gui/$(id -u)`. Both have `RunAtLoad` and
`KeepAlive`, so launchd starts them at login and restarts them whenever they
exit (no faster than every 10 s), and both write to
`${XDG_DATA_HOME:-~/.local/share}/ubertooth-harness/logs/` as `runner.log`,
`runner.err.log`, `tunnel.log` and `tunnel.err.log` (nothing rotates those
files, and a tunnel that cannot connect writes a line every 10 s, so look
there first when a target does not come online). The runner's
`ExitTimeOut` is 180 s, so a stop can cancel an active run and restore its
boards before launchd kills it. A LaunchAgent inherits almost no environment,
so each gets a `PATH` of `/opt/homebrew/bin:/usr/local/bin` and the system
directories, which is what a run with Build checked needs to find CMake,
pkg-config and libusb.

The tunnel agent runs
`/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes
-o StrictHostKeyChecking=yes -o ControlPath=none -o RemoteCommand=none
-o ServerAliveInterval=30 -o ServerAliveCountMax=3
-L 127.0.0.1:PORT:127.0.0.1:PORT [-J JUMP] HOST`, and the runner dials
`ws://127.0.0.1:PORT/ws/runner` through it, so both ends are loopback, as
with `scripts/harness-open`. The GUI is then also on this Mac's
`http://127.0.0.1:PORT/ui/`.

**The key is the access control, so set it up before deploying.** The agent
runs `ssh` with `BatchMode=yes`: nothing can answer a prompt, so

- the Mac user needs a key the controller host accepts with no passphrase
  (`ssh-keygen -t ed25519 -f ~/.ssh/harness -N ""`, and an `IdentityFile
  ~/.ssh/harness` entry for the host in `~/.ssh/config`), and
- that host's key must already be in the Mac's `~/.ssh/known_hosts`, so
  connect once by hand first (through the jump host, whose key is needed too).
  The agent passes `StrictHostKeyChecking=yes`, so an unknown or changed host
  key stops the tunnel rather than trusting whatever answered.

Give that key only the forward, since it needs no passphrase: one line in the
controller account's `authorized_keys`, with the port the controller uses,

```
restrict,port-forwarding,permitopen="127.0.0.1:8765",command="/usr/bin/false" ssh-ed25519 AAAA... harness tunnel
```

`restrict` turns off agent and X11 forwarding and pty allocation,
`port-forwarding` puts back the one thing the tunnel needs, `permitopen`
limits it to the controller's own loopback port, and the forced command means
the key cannot run anything (the agent's `ssh -N` asks for no command, so the
forward is unaffected). A key without those restrictions is an ordinary login
to the controller host, which is more than the tunnel needs.

With `--controller-jump` the same key also authenticates to the jump host, so
it needs an entry there too, as narrow:
`restrict,port-forwarding,permitopen="<the lab host>:22"`, which lets it reach
nothing but the SSH port it relays to.

The agent connects with `-J`, so the jump host must allow that; it has no
`--two-hop` equivalent (see **From a laptop** above for what that option is
for). It also passes `-o RemoteCommand=none`, so a host alias whose ssh
configuration runs a command still works.

Since that configuration is also read by the agent, deploy resolves it first
with `ssh -G`, which connects to nothing, and refuses to install anything if
it finds a `LocalForward`, `RemoteForward` or `DynamicForward` of its own for
this host (the agent would open that too, and it may bind where the network
can reach it), or if it throws the agent's own forward away with
`ClearAllForwardings` (which would leave a tunnel forwarding nothing). Keep
the harness host's stanza to what the connection needs: `HostName`, `User`,
`IdentityFile`, `ProxyJump`.

The host, jump host and port can come from `--controller-host`,
`--controller-jump` and `--port`, from `HARNESS_HOST`, `HARNESS_JUMP` and
`HARNESS_PORT` in the environment, or from the same private config file
`scripts/harness-open` reads, under the same rules (both scripts share
[`scripts/harness-config.sh`](../../scripts/harness-config.sh); see **From a
laptop** above). An option wins over the environment, which wins over the
file, and deploy stops rather than guessing a host.

A LaunchAgent runs only while its user is logged in to the Mac's desktop, so
a test host should log that user in by itself after a reboot: deploy warns
when the account is not the automatic login user (System Settings > Users &
Groups > Automatic login). FileVault turns automatic login off in effect,
since someone must unlock the disk at boot before any login happens, so a Mac
that must come back on its own cannot have it on. Rerunning changes nothing unless an agent's
content changed, in which case that agent is booted out and bootstrapped
again; a deploy that would restart the runner while *this* Mac's target has
an active run is refused unless `--force` is given. A change to the tunnel
alone restarts nothing else and is never refused: the runner keeps its run
going while the controller is away and replays its events when it is back. `--uninstall` boots out
both agents and removes the two plists. Deploy needs the GUI login domain
(`launchctl print gui/$(id -u)`); over SSH with nobody logged in, use
`--no-start` and load the agents from a Terminal on the Mac.

