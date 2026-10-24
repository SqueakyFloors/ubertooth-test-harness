#!/usr/bin/env bash
# Add an "Ubertooth Test Harness" launcher that opens the controller's web GUI
# (http://127.0.0.1:PORT/ui/) in the default browser. Run it as the desktop
# user (for example the auto-login user), not as the user running the
# services; the controller is on this machine's loopback, so any local user's
# browser reaches it.
#
#   scripts/install-desktop-shortcut.sh [--port N] [--dry-run]
#   scripts/install-desktop-shortcut.sh --uninstall
#
# Writes $XDG_DATA_HOME/applications/ubertooth-harness.desktop (default
# ~/.local/share/applications), and a copy in ~/Desktop when that directory
# exists, made executable and marked trusted with gio when gio is available
# (GNOME shows untrusted launchers as plain files). Rerunning with the same
# port changes nothing. Self-contained: copy it elsewhere if the desktop user
# cannot read this checkout.
#
# Options:
#   --port N      controller port (default 8765)
#   --uninstall   remove both launchers
#   --dry-run     print what would be done; change nothing
#   -h, --help    this help
set -euo pipefail

NAME=ubertooth-harness.desktop
port=8765
uninstall=0
dry_run=0

say() { printf 'desktop-shortcut: %s\n' "$*"; }
die() { printf 'desktop-shortcut: error: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
    case $1 in
        --port) [ "$#" -ge 2 ] || die "--port needs a value"; port=$2; shift ;;
        --uninstall) uninstall=1 ;;
        --dry-run) dry_run=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
    shift
done

[ "$(uname -s)" = Linux ] || die "desktop launchers are for a Linux desktop (freedesktop .desktop files)"
case $port in
    ''|*[!0-9]*) die "--port must be a number from 1 to 65535" ;;
esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "--port must be a number from 1 to 65535"

data_home=${XDG_DATA_HOME:-}
case $data_home in /*) ;; *) data_home=$HOME/.local/share ;; esac
targets=$data_home/applications/$NAME
desktop_dir=$HOME/Desktop
[ ! -d "$desktop_dir" ] || targets="$targets
$desktop_dir/$NAME"

url=http://127.0.0.1:$port/ui/
content="[Desktop Entry]
Type=Application
Version=1.0
Name=Ubertooth Test Harness
Comment=Open the harness web GUI ($url)
Exec=xdg-open $url
Icon=web-browser
Terminal=false
Categories=Development;
"

run() {
    if [ "$dry_run" = 1 ]; then
        say "would run: $*"
    else
        "$@"
    fi
}

while IFS= read -r path; do
    if [ "$uninstall" = 1 ]; then
        if [ -e "$path" ]; then
            run rm -f "$path"
            [ "$dry_run" = 1 ] || say "removed $path"
        else
            say "$path is not installed"
        fi
        continue
    fi
    if [ -f "$path" ] && printf '%s' "$content" | cmp -s - "$path"; then
        say "$path unchanged"
    elif [ "$dry_run" = 1 ]; then
        say "would write $path:"
        printf '%s' "$content" | sed 's/^/    /'
    else
        mkdir -p "$(dirname "$path")"
        tmp=$(mktemp "$(dirname "$path")/.$NAME.XXXXXX")
        printf '%s' "$content" > "$tmp"
        chmod 755 "$tmp"
        mv -f "$tmp" "$path"
        say "wrote $path"
    fi
    case $path in
        "$desktop_dir"/*)
            if command -v gio >/dev/null 2>&1; then
                run gio set "$path" metadata::trusted true \
                    || say "warning: could not mark $path trusted; right-click it and choose Allow Launching"
            else
                say "gio not found; right-click $path and choose Allow Launching"
            fi
            ;;
    esac
done <<EOF
$targets
EOF

[ "$uninstall" = 1 ] || say "launcher opens $url"
