#!/usr/bin/env bash
# Install or update the harness on this machine, from the checkout this script
# lives in.
#
#   scripts/deploy.sh --runner-host NAME --ubertooth-source DIR [options]
#   scripts/deploy.sh --uninstall
#
# On Linux it installs systemd user services for the controller and one
# runner: python3 bootstrap.py (create/update .venv), write
# ubertooth-harness-controller.service and ubertooth-harness-runner.service to
# the user's systemd directory, then daemon-reload and enable --now. It never
# runs sudo: if systemd linger is off for this user it prints the one-time
# command to enable it.
#
# On macOS it installs launchd LaunchAgents for one runner and its tunnel. The
# controller stays on the Linux host, where it binds 127.0.0.1 and has no
# authentication, so this Mac reaches it through an SSH local forward that
# launchd keeps up: the SSH key is the access control and no listener is
# opened here. Name the Linux host with --controller-host, HARNESS_HOST, or
# the private config file scripts/harness-open reads. Both agents go to
# ~/Library/LaunchAgents with RunAtLoad and KeepAlive, log to DATA/logs, and
# are loaded with launchctl bootstrap. LaunchAgents start when the user logs
# in to the desktop, so a Mac test host should log that user in automatically.
#
# A unit or agent is rewritten, and its service restarted, only when its
# content changes (the content records the checkout's commit, so pulling new
# code restarts them; uncommitted edits and dependency updates alone do not,
# so restart by hand after those). Restarting the runner during a run cancels
# it, so deploy refuses while the controller reports an active run unless
# --force is given. Rerunning with the same options changes nothing.
#
# Options:
#   --port N                  controller port (default 8765): on Linux the
#                             port it binds on 127.0.0.1, on macOS the port
#                             the tunnel forwards at both ends
#   --runner-host NAME        registry Host resource for the runner (required)
#   --ubertooth-source DIR    Ubertooth checkout the runner resolves refs in
#                             (required)
#   --hardware FILE           resource registry (default CHECKOUT/hardware.yaml)
#   --plans-dir DIR           plans for the GUI and runner (default
#                             CHECKOUT/testplans)
#   --ubertooth-repo URL      repository whose refs the GUI offers (default:
#                             the controller's default, the public fork);
#                             Linux only, it is a controller option
#   --controller-host HOST    macOS only: the Linux host running the
#                             controller, which the tunnel connects to
#                             (default $HARNESS_HOST, then the config file)
#   --controller-jump JUMP    macOS only: SSH jump host for that connection
#                             (default $HARNESS_JUMP, then the config file;
#                             "" for a direct connection)
#   --libbtbb-source DIR      libbtbb checkout the runner builds from (default:
#                             a clone of the libbtbb fork that deploy keeps in
#                             DATA/sources/libbtbb, cloned once, then fetched)
#   --libbtbb-ref REF         libbtbb commit the runner builds (default: the
#                             LIBBTBB_REF pin in install.sh on the Ubertooth
#                             checkout's origin/main, else the harness's own
#                             pin, LIBBTBB_PIN in scripts/ci.sh)
#   --no-libbtbb              no libbtbb: the runner cannot build, and refuses
#                             runs that ask for a build
#   --allow-power-changes     passed to the runner (lets runs switch USB power)
#   --no-start                write the units or agents only (Linux also
#                             daemon-reloads)
#   --force                   restart even while a run is active (it is
#                             cancelled); without it, deploy refuses
#   --uninstall               stop and remove this machine's units or agents
#                             only; the venv and results are left in place
#   --dry-run                 print what would be done; change nothing
#   -h, --help                this help
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
CONTROLLER_UNIT=ubertooth-harness-controller.service
RUNNER_UNIT=ubertooth-harness-runner.service
RUNNER_LABEL=com.ubertooth-harness.runner
TUNNEL_LABEL=com.ubertooth-harness.tunnel
LIBBTBB_URL=https://github.com/SqueakyFloors/libbtbb.git
# Hard limit on each clone or fetch deploy makes.
GIT_TIMEOUT_S=60
# launchd respawns a KeepAlive agent no faster than this.
AGENT_THROTTLE_S=10
# Stopping the runner cancels an active run and restores its boards.
RUNNER_STOP_S=180
# A LaunchAgent inherits almost no environment, and a run that builds needs
# cmake, pkg-config and libusb from whichever Homebrew prefix this Mac uses.
AGENT_PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

port=8765
port_option=0
runner_host=
ubertooth_source=
hardware=$ROOT/hardware.yaml
plans_dir=$ROOT/testplans
ubertooth_repo=
controller_host=
controller_host_option=0
controller_jump=
controller_jump_option=0
libbtbb_source=
libbtbb_ref=
no_libbtbb=0
allow_power_changes=0
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
        --port) need_value "$@"; port=$2; port_option=1; shift ;;
        --runner-host) need_value "$@"; runner_host=$2; shift ;;
        --ubertooth-source) need_value "$@"; ubertooth_source=$2; shift ;;
        --hardware) need_value "$@"; hardware=$2; shift ;;
        --plans-dir) need_value "$@"; plans_dir=$2; shift ;;
        --ubertooth-repo) need_value "$@"; ubertooth_repo=$2; shift ;;
        --controller-host) need_value "$@"; controller_host=$2; controller_host_option=1; shift ;;
        # An empty value is a choice (no jump host), so it is not need_value.
        --controller-jump)
            [ "$#" -ge 2 ] || die "$1 needs a value"
            controller_jump=$2; controller_jump_option=1; shift ;;
        --libbtbb-source) need_value "$@"; libbtbb_source=$2; shift ;;
        --libbtbb-ref) need_value "$@"; libbtbb_ref=$2; shift ;;
        --no-libbtbb) no_libbtbb=1 ;;
        --allow-power-changes) allow_power_changes=1 ;;
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
    Linux) platform=linux ;;
    Darwin) platform=macos ;;
    *) die "unsupported OS $(uname -s); deploy needs Linux with systemd or macOS with launchd" ;;
esac

macos_only="is a macOS option; on Linux the controller runs on this machine"
if [ "$platform" = linux ]; then
    [ "$controller_host_option" = 0 ] || die "--controller-host $macos_only"
    [ "$controller_jump_option" = 0 ] || die "--controller-jump $macos_only"
else
    [ -z "$ubertooth_repo" ] ||
        die "--ubertooth-repo is a controller option; on macOS deploy installs the runner and its tunnel, and the controller runs on the Linux host"
    # The agents record where they came from in an XML comment, which cannot
    # hold "--".
    case $ROOT in
        *--*) die "the checkout path contains '--', which a plist comment cannot hold: $ROOT" ;;
    esac
fi

# Unit and agent files hold the values unquoted, so allow only characters
# systemd and the plist take literally (no whitespace, quotes, %, $, & or
# backslash).
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

# Host names become ssh arguments, so allow only plain host/alias characters
# and no leading '-'.
check_name() {
    case $2 in
        -*|*[!A-Za-z0-9._@-]*) die "$1 must be a host name or SSH alias: $2" ;;
    esac
}

data_home=${XDG_DATA_HOME:-}
case $data_home in /*) ;; *) data_home=$HOME/.local/share ;; esac
config_home=${XDG_CONFIG_HOME:-}
case $config_home in /*) ;; *) config_home=$HOME/.config ;; esac
state_dir=$data_home/ubertooth-harness
results_dir=$state_dir/controller/runs
runner_output=$state_dir/runner/runs
log_dir=$state_dir/logs
python=$ROOT/.venv/bin/python
# The GUI login session's launchd domain, where a LaunchAgent lives.
launchd_domain=gui/$(id -u)
if [ "$platform" = macos ]; then
    unit_dir=$HOME/Library/LaunchAgents
else
    unit_dir=$config_home/systemd/user
fi

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

# A LaunchAgent runs only while its user is logged in to the desktop, so a
# Mac test host should log that user in by itself after a reboot.
check_autologin() {
    local user auto
    user=$(id -un)
    auto=$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null) || auto=
    if [ "$auto" = "$user" ]; then
        return
    fi
    say "warning: $user is not this Mac's automatic login user, so these agents start"
    say "  only once $user logs in to the desktop and stop when that user logs out."
    say "  Set it in System Settings > Users & Groups > Automatic login."
}

# The agents or units this machine installs, in the order to stop them.
if [ "$platform" = macos ]; then
    installed_units=("$RUNNER_LABEL.plist" "$TUNNEL_LABEL.plist")
else
    installed_units=("$RUNNER_UNIT" "$CONTROLLER_UNIT")
fi

if [ "$uninstall" = 1 ]; then
    removed=0
    for unit in "${installed_units[@]}"; do
        if [ -e "$unit_dir/$unit" ]; then
            if [ "$platform" = macos ]; then
                run launchctl bootout "$launchd_domain/${unit%.plist}" ||
                    say "warning: could not stop $unit"
                run rm -f "$unit_dir/$unit"
            else
                run systemctl --user disable --now "$unit" || say "warning: could not stop $unit"
                run rm -f "$unit_dir/$unit" "$unit_dir/default.target.wants/$unit"
            fi
            [ "$dry_run" = 1 ] || say "removed $unit_dir/$unit"
            removed=1
        else
            say "$unit is not installed"
        fi
    done
    if [ "$removed" = 1 ] && [ "$platform" = linux ]; then
        run systemctl --user daemon-reload
    fi
    say "left in place: $ROOT/.venv and $state_dir"
    exit 0
fi

# After the uninstall branch, which needs neither a host nor a port: removing
# the agents is what you want when the config file itself is the problem.
# macOS: the tunnel's host, jump host and port, from the options, then the
# environment, then the private config file scripts/harness-open reads.
if [ "$platform" = macos ]; then
    lib=$ROOT/scripts/harness-config.sh
    [ -f "$lib" ] || die "$lib is missing; run this script from its checkout"
    # Linted on its own; shellcheck does not need to follow it here.
    # shellcheck source=/dev/null
    . "$lib"
    harness_config_load
    [ "$controller_host_option" = 1 ] || controller_host=${HARNESS_HOST-}
    [ "$controller_jump_option" = 1 ] || controller_jump=${HARNESS_JUMP-}
    [ "$port_option" = 1 ] || port=${HARNESS_PORT:-$port}
fi

case $port in
    ''|*[!0-9]*) die "--port must be a number from 1 to 65535" ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then die "--port must be a number from 1 to 65535"; fi

[ -n "$runner_host" ] || die "--runner-host NAME is required (the Host resource in the registry)"
[ -n "$ubertooth_source" ] || die "--ubertooth-source DIR is required (the runner's Ubertooth checkout)"
if [ "$platform" = macos ]; then
    if [ -z "$controller_host" ]; then
        [ "$controller_host_option" = 0 ] || die "--controller-host must not be empty"
        die "no controller host: pass --controller-host HOST, set HARNESS_HOST, or name one in $HARNESS_CONFIG_FILE (the Linux host the controller runs on)"
    fi
    check_name --controller-host "$controller_host"
    [ -z "$controller_jump" ] || check_name --controller-jump "$controller_jump"
fi
if [ "$no_libbtbb" = 1 ] && { [ -n "$libbtbb_source" ] || [ -n "$libbtbb_ref" ]; }; then
    die "--no-libbtbb cannot be combined with --libbtbb-source or --libbtbb-ref"
fi
ubertooth_source=$(absolute "$ubertooth_source")
hardware=$(absolute "$hardware")
plans_dir=$(absolute "$plans_dir")
[ -z "$libbtbb_source" ] || libbtbb_source=$(absolute "$libbtbb_source")
# Deploy keeps its own libbtbb clone unless one is named (or none is wanted).
manage_libbtbb=0
if [ "$no_libbtbb" = 0 ] && [ -z "$libbtbb_source" ]; then
    libbtbb_source=$state_dir/sources/libbtbb
    manage_libbtbb=1
fi

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
[ "$manage_libbtbb" = 1 ] || [ -z "$libbtbb_source" ] || check_path --libbtbb-source "$libbtbb_source" -d

commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)

if [ "$no_start" = 0 ] && [ "$dry_run" = 0 ]; then
    if [ "$platform" = macos ]; then
        launchctl print "$launchd_domain" >/dev/null 2>&1 ||
            die "no launchd domain $launchd_domain for $(id -un) (log in on this Mac's desktop, or deploy with --no-start and load the agents from a Terminal there); nothing was changed"
    else
        systemctl --user show-environment >/dev/null 2>&1 ||
            die "no systemd user manager for $(id -un) (log in as this user over SSH or the desktop, not su/sudo -u); nothing was changed"
    fi
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

# git for a clone or fetch: bounded, never prompting for credentials.
net_git() {
    if [ "$dry_run" = 1 ]; then
        say "would run: git $*"
        return 0
    fi
    if command -v timeout >/dev/null 2>&1; then
        GIT_TERMINAL_PROMPT=0 timeout "$GIT_TIMEOUT_S" git "$@" </dev/null
    elif command -v perl >/dev/null 2>&1; then
        # macOS has no timeout(1); perl's alarm bounds the command the same way.
        GIT_TERMINAL_PROMPT=0 perl -e 'alarm shift; exec @ARGV' \
            "$GIT_TIMEOUT_S" git "$@" </dev/null
    else
        GIT_TERMINAL_PROMPT=0 git "$@" </dev/null
    fi
}

# The literal LIBBTBB_REF commit install.sh sets on the Ubertooth checkout's
# origin/main (fetched first); nothing when it cannot be read.
fork_libbtbb_pin() {
    local text
    # Runs inside $(...): everything but the pin goes to stderr.
    if ! net_git -C "$ubertooth_source" fetch --quiet origin main >&2; then
        say "warning: could not fetch origin main in $ubertooth_source; reading the pin from what it has" >&2
    fi
    text=$(git -C "$ubertooth_source" show origin/main:install.sh 2>/dev/null) || return 0
    printf '%s\n' "$text" |
        sed -n "s/^LIBBTBB_REF=[\"']\{0,1\}\([0-9a-fA-F]\{7,40\}\)[\"']\{0,1\}[[:space:]]*\(#.*\)\{0,1\}\$/\1/p" |
        head -n 1
}

setup_libbtbb() {
    if [ "$no_libbtbb" = 1 ]; then
        say "libbtbb: none (--no-libbtbb); the runner cannot build and refuses runs that ask for a build"
        return
    fi
    if [ "$manage_libbtbb" = 1 ]; then
        if [ -d "$libbtbb_source/.git" ]; then
            if net_git -C "$libbtbb_source" fetch --quiet --tags origin; then
                [ "$dry_run" = 1 ] || say "libbtbb: fetched $libbtbb_source"
            else
                say "warning: could not fetch $libbtbb_source; the runner fetches a missing commit itself"
            fi
        elif [ -e "$libbtbb_source" ]; then
            die "$libbtbb_source exists but is not a git clone; move it away, or pass --libbtbb-source DIR or --no-libbtbb"
        else
            [ -d "$(dirname "$libbtbb_source")" ] || run mkdir -p "$(dirname "$libbtbb_source")"
            if ! net_git clone --quiet "$LIBBTBB_URL" "$libbtbb_source"; then
                rm -rf "$libbtbb_source"
                die "could not clone $LIBBTBB_URL into $libbtbb_source; pass --libbtbb-source DIR, or --no-libbtbb for a runner that cannot build"
            fi
            [ "$dry_run" = 1 ] || say "libbtbb: cloned $LIBBTBB_URL into $libbtbb_source"
        fi
    fi
    if [ -z "$libbtbb_ref" ]; then
        libbtbb_ref=$(fork_libbtbb_pin)
        if [ -n "$libbtbb_ref" ]; then
            say "libbtbb: ref $libbtbb_ref, the LIBBTBB_REF pin in install.sh on origin/main of $ubertooth_source"
        else
            libbtbb_ref=$(sed -n 's/^LIBBTBB_PIN=\([0-9a-f]\{40\}\)$/\1/p' "$ROOT/scripts/ci.sh" 2>/dev/null | head -n 1)
            [ -n "$libbtbb_ref" ] || die "no libbtbb ref: install.sh on origin/main of $ubertooth_source has no literal LIBBTBB_REF and $ROOT/scripts/ci.sh has no LIBBTBB_PIN; pass --libbtbb-ref REF or --no-libbtbb"
            say "libbtbb: ref $libbtbb_ref, the harness's pin (LIBBTBB_PIN in scripts/ci.sh): no literal LIBBTBB_REF in install.sh on origin/main of $ubertooth_source"
        fi
    else
        say "libbtbb: ref $libbtbb_ref (--libbtbb-ref)"
    fi
}
setup_libbtbb
safe --libbtbb-source "$libbtbb_source"
safe --libbtbb-ref "$libbtbb_ref"

controller_args="controller --port $port --results-dir $results_dir --plans-dir $plans_dir"
[ -z "$ubertooth_repo" ] || controller_args="$controller_args --ubertooth-repo $ubertooth_repo"

runner_argv=(runner --controller "ws://127.0.0.1:$port/ws/runner"
             --hardware "$hardware" --host "$runner_host"
             --ubertooth-source "$ubertooth_source" --plans-dir "$plans_dir"
             --output "$runner_output")
[ -z "$libbtbb_source" ] || runner_argv+=(--libbtbb-source "$libbtbb_source")
[ -z "$libbtbb_ref" ] || runner_argv+=(--libbtbb-ref "$libbtbb_ref")
[ "$allow_power_changes" = 0 ] || runner_argv+=(--allow-power-changes)
runner_args=${runner_argv[*]}

# Loopback at both ends: the forward binds only this machine's loopback and
# reaches only the controller's loopback listener. BatchMode so a missing key
# or host key fails instead of waiting for an answer nobody can give. This
# connection is the only thing standing between the controller and the
# network, so host-key checking is pinned on and connection sharing off,
# whatever the user's ssh configuration says.
tunnel_argv=(/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes
             -o StrictHostKeyChecking=yes -o ControlPath=none
             -o ServerAliveInterval=30 -o ServerAliveCountMax=3
             -L "127.0.0.1:$port:127.0.0.1:$port")
[ -z "$controller_jump" ] || tunnel_argv+=(-J "$controller_jump")
tunnel_argv+=("$controller_host")

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

# One LaunchAgent: $1 label, $2 log file prefix, $3 seconds to allow for a
# clean stop, then the program and its arguments.
agent_plist() {
    local label=$1 logs=$2 stop_s=$3 item
    shift 3
    printf '%s\n' \
        '<?xml version="1.0" encoding="UTF-8"?>' \
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
        "<!-- Written by scripts/deploy.sh from $ROOT (commit $commit)." \
        '     Rerun that script to change it; edits here are overwritten. -->' \
        '<plist version="1.0">' \
        '<dict>' \
        "  <key>Label</key><string>$label</string>" \
        '  <key>ProgramArguments</key>' \
        '  <array>'
    for item; do
        safe "an agent argument" "$item"
        printf '    <string>%s</string>\n' "$item"
    done
    printf '%s\n' \
        '  </array>' \
        "  <key>WorkingDirectory</key><string>$ROOT</string>" \
        '  <key>RunAtLoad</key><true/>' \
        '  <key>KeepAlive</key><true/>' \
        "  <key>ThrottleInterval</key><integer>$AGENT_THROTTLE_S</integer>" \
        "  <key>ExitTimeOut</key><integer>$stop_s</integer>" \
        '  <key>EnvironmentVariables</key>' \
        '  <dict>' \
        "    <key>PATH</key><string>$AGENT_PATH</string>" \
        '  </dict>' \
        "  <key>StandardOutPath</key><string>$logs.log</string>" \
        "  <key>StandardErrorPath</key><string>$logs.err.log</string>" \
        '</dict>' \
        '</plist>'
}

if [ "$platform" = macos ]; then
    # Command substitution drops the trailing newline a plist file wants.
    runner_agent=$(agent_plist "$RUNNER_LABEL" "$log_dir/runner" "$RUNNER_STOP_S" \
                               "$python" -m ubertooth_harness "${runner_argv[@]}")$'\n'
    # ssh exits as soon as it is asked to; nothing to clean up.
    tunnel_agent=$(agent_plist "$TUNNEL_LABEL" "$log_dir/tunnel" 20 "${tunnel_argv[@]}")$'\n'
fi

# Prints the IDs of active runs the controller on $1 reports, one "host:run"
# per line; nothing when no controller answers. With a host name in $2, only
# that target's runs.
active_runs() {
    "$python" - "$1" "${2-}" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
port, only = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/targets", timeout=5) as reply:
        targets = json.load(reply).get("targets", [])
except Exception:
    sys.exit(0)
for target in targets:
    if only and target.get("host") != only:
        continue
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
restart_needed=0
if [ "$platform" = macos ]; then
    if unit_changes "$RUNNER_LABEL.plist" "$runner_agent" ||
       unit_changes "$TUNNEL_LABEL.plist" "$tunnel_agent"; then
        restart_needed=1
    fi
    # Only this Mac's runner is restarted, so only its runs are at stake.
    busy_filter=$runner_host
else
    if unit_changes "$CONTROLLER_UNIT" "$controller_unit" ||
       unit_changes "$RUNNER_UNIT" "$runner_unit"; then
        restart_needed=1
    fi
    busy_filter=
fi
# The port the installed runner uses, which is not the requested one when
# --port changes. The running controller answers there, so that is the port
# to ask about active runs; asking the new one would find nothing and restart
# the runner under an active run.
running_port() {
    local path=$unit_dir/$1
    [ -f "$path" ] || return 0
    sed -n 's|.*ws://127\.0\.0\.1:\([0-9]\{1,5\}\)/ws/runner.*|\1|p' "$path" | head -n 1
}

if [ "$no_start" = 0 ] && [ "$restart_needed" = 1 ]; then
    if [ "$platform" = macos ]; then
        busy_port=$(running_port "$RUNNER_LABEL.plist")
    else
        busy_port=$(running_port "$RUNNER_UNIT")
    fi
    if [ -z "$busy_port" ]; then
        busy_port=$port
    elif [ "$busy_port" != "$port" ]; then
        say "note: the installed runner uses port $busy_port; asking that controller about active runs"
    fi
    busy=$(active_runs "$busy_port" "$busy_filter")
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

if [ "$platform" = macos ]; then
    made_dirs=("$runner_output" "$log_dir")
else
    made_dirs=("$results_dir" "$runner_output")
fi
for dir in "${made_dirs[@]}"; do
    if [ ! -d "$dir" ]; then
        run mkdir -p "$dir"
    fi
done

if [ "$platform" = macos ]; then
    write_unit "$TUNNEL_LABEL.plist" "$tunnel_agent"
    write_unit "$RUNNER_LABEL.plist" "$runner_agent"
else
    write_unit "$CONTROLLER_UNIT" "$controller_unit"
    write_unit "$RUNNER_UNIT" "$runner_unit"
fi

agent_loaded() {
    launchctl print "$launchd_domain/$1" >/dev/null 2>&1
}

# Load one agent, or replace it when its plist changed. launchctl has no
# "reload", so a changed agent is booted out and bootstrapped again.
load_agent() {
    # $1 plist name
    local label=${1%.plist}
    case $changed_units in
        *" $1"*)
            if agent_loaded "$label"; then
                run launchctl bootout "$launchd_domain/$label" ||
                    say "warning: could not unload $label"
            fi
            bootstrap_agent "$1" "$label" ;;
        *)
            if agent_loaded "$label"; then
                say "$label is already loaded"
            else
                bootstrap_agent "$1" "$label"
            fi ;;
    esac
}

bootstrap_agent() {
    # $1 plist name, $2 label
    run launchctl bootstrap "$launchd_domain" "$unit_dir/$1" ||
        die "launchctl bootstrap $2 failed; its plist is written, so read the reason with: launchctl print $launchd_domain"
}

url=http://127.0.0.1:$port/ui/
if [ "$no_start" = 1 ]; then
    if [ "$platform" = macos ]; then
        say "agents written, not started (--no-start). Load them with:"
        say "  launchctl bootstrap $launchd_domain $unit_dir/$TUNNEL_LABEL.plist $unit_dir/$RUNNER_LABEL.plist"
    else
        [ -z "$changed_units" ] || run systemctl --user daemon-reload
        say "units written, not started (--no-start). Start them with:"
        say "  systemctl --user enable --now $CONTROLLER_UNIT $RUNNER_UNIT"
    fi
elif [ "$platform" = macos ]; then
    load_agent "$TUNNEL_LABEL.plist"
    load_agent "$RUNNER_LABEL.plist"
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

if [ "$platform" = macos ]; then
    say "tunnel: 127.0.0.1:$port on this Mac to 127.0.0.1:$port on $controller_host${controller_jump:+ through $controller_jump}"
    say "  it runs ssh with BatchMode=yes, so $(id -un) needs a key $controller_host accepts"
    say "  without a passphrase, and that host's key in ~/.ssh/known_hosts (connect once by hand)."
    say "logs: $log_dir/runner.log, $log_dir/tunnel.log (and .err.log beside each)"
    say "GUI: $url (on this Mac, through the tunnel)"
    check_autologin
else
    say "GUI: $url (on this machine; from another, scripts/harness-open)"
    check_linger
fi
