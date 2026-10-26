#!/usr/bin/env bash
# Install or update the harness on this machine, from the checkout this script
# lives in, as systemd user services for the controller and one runner.
#
#   scripts/deploy.sh --runner-host NAME --ubertooth-source DIR [options]
#   scripts/deploy.sh --uninstall
#
# Steps: python3 bootstrap.py (create/update .venv), write
# ubertooth-harness-controller.service and ubertooth-harness-runner.service to
# the user's systemd directory, then daemon-reload and enable --now. A unit is
# rewritten, and its service restarted, only when its content changes (the
# content records the checkout's commit, so pulling new code restarts both;
# uncommitted edits and dependency updates alone do not, so restart by hand
# with systemctl --user restart after those). Restarting either service
# during a run cancels it, so deploy refuses while the controller reports an
# active run unless --force is given. Rerunning with the same options changes
# nothing. It never runs sudo: if systemd linger is off
# for this user it prints the one-time command to enable it.
#
# The controller listens on 127.0.0.1 only (the harness has no bind option);
# use scripts/harness-open or an SSH tunnel from another machine.
#
# Options:
#   --port N                  controller port on 127.0.0.1 (default 8765)
#   --runner-host NAME        registry Host resource for the runner (required)
#   --ubertooth-source DIR    Ubertooth checkout the runner resolves refs in
#                             (required)
#   --hardware FILE           resource registry (default CHECKOUT/hardware.yaml)
#   --plans-dir DIR           plans for the GUI and runner (default
#                             CHECKOUT/testplans)
#   --ubertooth-repo URL      repository whose refs the GUI offers (default:
#                             the controller's default, the public fork)
#   --libbtbb-source DIR      passed to the runner
#   --libbtbb-ref REF         passed to the runner
#   --allow-power-changes     passed to the runner (lets runs switch USB power)
#   --external-board-lock TEMPLATE
#                             passed to the runner: every run also holds each
#                             board's own lock file from other tools on this
#                             host, TEMPLATE with {serial} (the full serial),
#                             e.g. /var/lock/board-{serial}.lock; absolute or
#                             starting with ~/
#   --no-start                write the units (and daemon-reload) only
#   --force                   restart even while a run is active (it is
#                             cancelled); without it, deploy refuses
#   --uninstall               stop, disable and remove the two units only; the
#                             venv and results are left in place
#   --dry-run                 print what would be done; change nothing
#   -h, --help                this help
#
# Linux (systemd) only for now; macOS (launchd) is not supported yet.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
CONTROLLER_UNIT=ubertooth-harness-controller.service
RUNNER_UNIT=ubertooth-harness-runner.service

port=8765
runner_host=
ubertooth_source=
hardware=$ROOT/hardware.yaml
plans_dir=$ROOT/testplans
ubertooth_repo=
libbtbb_source=
libbtbb_ref=
allow_power_changes=0
external_board_lock=
no_start=0
force=0
uninstall=0
dry_run=0

say() { printf 'deploy: %s\n' "$*"; }
die() { printf 'deploy: error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

need_value() {
    if [ "$#" -lt 2 ] || [ -z "$2" ]; then die "$1 needs a value"; fi
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --port) need_value "$@"; port=$2; shift ;;
        --runner-host) need_value "$@"; runner_host=$2; shift ;;
        --ubertooth-source) need_value "$@"; ubertooth_source=$2; shift ;;
        --hardware) need_value "$@"; hardware=$2; shift ;;
        --plans-dir) need_value "$@"; plans_dir=$2; shift ;;
        --ubertooth-repo) need_value "$@"; ubertooth_repo=$2; shift ;;
        --libbtbb-source) need_value "$@"; libbtbb_source=$2; shift ;;
        --libbtbb-ref) need_value "$@"; libbtbb_ref=$2; shift ;;
        --allow-power-changes) allow_power_changes=1 ;;
        --external-board-lock) need_value "$@"; external_board_lock=$2; shift ;;
        --no-start) no_start=1 ;;
        --force) force=1 ;;
        --uninstall) uninstall=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift
done

case $(uname -s) in
    Linux) ;;
    Darwin) die "macOS is not supported yet (launchd units are not implemented); deploy on Linux with systemd" ;;
    *) die "unsupported OS $(uname -s); deploy needs Linux with systemd" ;;
esac

case $port in
    ''|*[!0-9]*) die "--port must be a number from 1 to 65535" ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then die "--port must be a number from 1 to 65535"; fi

# Unit files hold the values unquoted, so allow only characters systemd takes
# literally (no whitespace, quotes, %, $ or backslash).
safe() {
    case $2 in
        *[!A-Za-z0-9._/:@+=,~-]*) die "$1 contains characters this script does not quote: $2" ;;
    esac
}

absolute() {
    case $1 in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s\n' "$PWD/$1" ;;
    esac
}

data_home=${XDG_DATA_HOME:-}
case $data_home in /*) ;; *) data_home=$HOME/.local/share ;; esac
config_home=${XDG_CONFIG_HOME:-}
case $config_home in /*) ;; *) config_home=$HOME/.config ;; esac
unit_dir=$config_home/systemd/user
state_dir=$data_home/ubertooth-harness
results_dir=$state_dir/controller/runs
runner_output=$state_dir/runner/runs
python=$ROOT/.venv/bin/python

run() {
    if [ "$dry_run" = 1 ]; then
        say "would run: $*"
    else
        "$@"
    fi
}

check_linger() {
    local user linger
    user=$(id -un)
    if ! linger=$(loginctl show-user "$user" -p Linger 2>/dev/null); then
        say "warning: could not read systemd linger for $user (loginctl show-user failed)."
        say "  Without linger the services stop when $user's last session ends."
        return
    fi
    if [ "$linger" = "Linger=no" ]; then
        say "warning: systemd linger is off for $user, so these services stop when"
        say "  $user's last login session ends and do not start at boot. Enable it once"
        say "  (this script never runs sudo):"
        say "    sudo loginctl enable-linger $user"
    fi
}

if [ "$uninstall" = 1 ]; then
    removed=0
    for unit in "$RUNNER_UNIT" "$CONTROLLER_UNIT"; do
        if [ -e "$unit_dir/$unit" ]; then
            run systemctl --user disable --now "$unit" || say "warning: could not stop $unit"
            run rm -f "$unit_dir/$unit" "$unit_dir/default.target.wants/$unit"
            [ "$dry_run" = 1 ] || say "removed $unit_dir/$unit"
            removed=1
        else
            say "$unit is not installed"
        fi
    done
    if [ "$removed" = 1 ]; then
        run systemctl --user daemon-reload
    fi
    say "left in place: $ROOT/.venv and $state_dir"
    exit 0
fi

[ -n "$runner_host" ] || die "--runner-host NAME is required (the Host resource in the registry)"
[ -n "$ubertooth_source" ] || die "--ubertooth-source DIR is required (the runner's Ubertooth checkout)"
ubertooth_source=$(absolute "$ubertooth_source")
hardware=$(absolute "$hardware")
plans_dir=$(absolute "$plans_dir")
[ -z "$libbtbb_source" ] || libbtbb_source=$(absolute "$libbtbb_source")

safe checkout "$ROOT"
safe "data directory" "$state_dir"
safe --runner-host "$runner_host"
safe --ubertooth-source "$ubertooth_source"
safe --hardware "$hardware"
safe --plans-dir "$plans_dir"
safe --ubertooth-repo "$ubertooth_repo"
safe --libbtbb-source "$libbtbb_source"
safe --libbtbb-ref "$libbtbb_ref"
case $runner_host in -*) die "--runner-host must not start with -" ;; esac
if [ -n "$external_board_lock" ]; then
    # The harness checks it again; checked here so a bad value never
    # reaches the unit. ~/ is expanded now: systemd does not expand it.
    case $external_board_lock in
        "~/"*) external_board_lock=$HOME/${external_board_lock#"~/"} ;;
    esac
    case $external_board_lock in
        /*) ;;
        *) die "--external-board-lock must be an absolute path (or start with ~/): $external_board_lock" ;;
    esac
    lock_rest=${external_board_lock/"{serial}"/}
    case $lock_rest in
        *"{"*|*"}"*) die "--external-board-lock must contain {serial} exactly once and no other brace: $external_board_lock" ;;
    esac
    [ "$lock_rest" != "$external_board_lock" ] || die "--external-board-lock must contain {serial}: $external_board_lock"
    safe --external-board-lock "$lock_rest"
fi
case $libbtbb_ref in -*) die "--libbtbb-ref must not start with -" ;; esac
case $ubertooth_repo in -*) die "--ubertooth-repo must not start with -" ;; esac

check_path() {
    # $1 option, $2 path, $3 test flag (-f or -d)
    if ! test "$3" "$2"; then
        if [ "$dry_run" = 1 ]; then
            say "warning: $1 $2 does not exist"
        else
            die "$1 $2 does not exist"
        fi
    fi
}
check_path --ubertooth-source "$ubertooth_source" -d
check_path --hardware "$hardware" -f
check_path --plans-dir "$plans_dir" -d
[ -z "$libbtbb_source" ] || check_path --libbtbb-source "$libbtbb_source" -d

commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)

controller_args="controller --port $port --results-dir $results_dir --plans-dir $plans_dir"
[ -z "$ubertooth_repo" ] || controller_args="$controller_args --ubertooth-repo $ubertooth_repo"

runner_args="runner --controller ws://127.0.0.1:$port/ws/runner --hardware $hardware --host $runner_host"
runner_args="$runner_args --ubertooth-source $ubertooth_source --plans-dir $plans_dir --output $runner_output"
[ -z "$libbtbb_source" ] || runner_args="$runner_args --libbtbb-source $libbtbb_source"
[ -z "$libbtbb_ref" ] || runner_args="$runner_args --libbtbb-ref $libbtbb_ref"
[ "$allow_power_changes" = 0 ] || runner_args="$runner_args --allow-power-changes"
[ -z "$external_board_lock" ] || runner_args="$runner_args --external-board-lock $external_board_lock"

header="# Written by scripts/deploy.sh from $ROOT (commit $commit).
# Rerun that script to change it; edits here are overwritten."

controller_unit="$header
[Unit]
Description=Ubertooth test harness controller (127.0.0.1:$port)

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$python -m ubertooth_harness $controller_args
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"

runner_unit="$header
[Unit]
Description=Ubertooth test harness runner for $runner_host
After=$CONTROLLER_UNIT
Wants=$CONTROLLER_UNIT

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$python -m ubertooth_harness $runner_args
Restart=on-failure
RestartSec=5
# Stopping cancels an active run and restores its boards; give it time.
TimeoutStopSec=180

[Install]
WantedBy=default.target
"

if [ "$no_start" = 0 ] && [ "$dry_run" = 0 ] && ! systemctl --user show-environment >/dev/null 2>&1; then
    die "no systemd user manager for $(id -un) (log in as this user over SSH or the desktop, not su/sudo -u); nothing was changed"
fi

say "checkout $ROOT (commit $commit)"
run python3 "$ROOT/bootstrap.py"
if [ ! -x "$python" ]; then
    if [ "$dry_run" = 1 ]; then
        say "warning: $python does not exist yet"
    else
        die "$python is missing after bootstrap"
    fi
fi

# Prints the IDs of active runs the controller on this port reports; nothing
# when no controller answers.
active_runs() {
    "$python" - "$port" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/targets", timeout=5) as reply:
        targets = json.load(reply).get("targets", [])
except Exception:
    sys.exit(0)
for target in targets:
    if target.get("active_run_id"):
        print(f"{target.get('host')}:{target['active_run_id']}")
PY
}

changed_units=
write_unit() {
    # $1 unit name, $2 content
    local path=$unit_dir/$1 tmp
    if [ -f "$path" ] && printf '%s' "$2" | cmp -s - "$path"; then
        say "$path unchanged"
        return
    fi
    changed_units="$changed_units $1"
    if [ "$dry_run" = 1 ]; then
        if [ -f "$path" ]; then say "would update $path:"; else say "would create $path:"; fi
        printf '%s' "$2" | sed 's/^/    /'
        return
    fi
    mkdir -p "$unit_dir"
    tmp=$(mktemp "$unit_dir/.$1.XXXXXX")
    printf '%s' "$2" > "$tmp"
    chmod 644 "$tmp"
    mv -f "$tmp" "$path"
    say "wrote $path"
}

unit_changes() {
    local path=$unit_dir/$1
    ! { [ -f "$path" ] && printf '%s' "$2" | cmp -s - "$path"; }
}
if [ "$no_start" = 0 ] && { unit_changes "$CONTROLLER_UNIT" "$controller_unit" || unit_changes "$RUNNER_UNIT" "$runner_unit"; }; then
    busy=$(active_runs)
    if [ -n "$busy" ]; then
        if [ "$force" = 1 ]; then
            say "warning: restarting during active runs (--force): $busy"
        elif [ "$dry_run" = 1 ]; then
            say "warning: would refuse: active runs $busy (pass --force to cancel them)"
        else
            die "active runs $busy; restarting would cancel them. Deploy after they finish, or pass --force"
        fi
    fi
fi

for dir in "$results_dir" "$runner_output"; do
    if [ ! -d "$dir" ]; then
        run mkdir -p "$dir"
    fi
done
write_unit "$CONTROLLER_UNIT" "$controller_unit"
write_unit "$RUNNER_UNIT" "$runner_unit"

url=http://127.0.0.1:$port/ui/
if [ "$no_start" = 1 ]; then
    [ -z "$changed_units" ] || run systemctl --user daemon-reload
    say "units written, not started (--no-start). Start them with:"
    say "  systemctl --user enable --now $CONTROLLER_UNIT $RUNNER_UNIT"
else
    if [ -n "$changed_units" ]; then
        run systemctl --user daemon-reload
        # Changed units restart (or start) so new code and options take effect,
        # controller first.
        for unit in $changed_units; do
            run systemctl --user restart "$unit"
        done
    fi
    run systemctl --user enable --now "$CONTROLLER_UNIT" "$RUNNER_UNIT"
    run systemctl --user --no-pager --lines=0 status "$CONTROLLER_UNIT" "$RUNNER_UNIT" || true
fi

say "GUI: $url (on this machine; from another, scripts/harness-open)"
check_linger
