#!/usr/bin/env bash
# shellcheck disable=SC2034  # the HARNESS_* variables are for the caller
#
# Private connection settings for the lab controller, shared by
# scripts/harness-open and scripts/deploy.sh. This file is sourced, not run.
#
# harness_config_load sets HARNESS_HOST, HARNESS_JUMP and HARNESS_PORT from,
# in this order of precedence: what the environment already holds, then a
# private file of shell assignments at $HARNESS_OPEN_CONFIG, by default
# $XDG_CONFIG_HOME/ubertooth-harness/harness-open.conf (so
# ~/.config/ubertooth-harness/harness-open.conf unless XDG_CONFIG_HOME is
# set). The path it read is left in HARNESS_CONFIG_FILE for messages, whether
# that file exists or not.
#
# An empty HARNESS_JUMP in the environment is a choice (no jump host) and
# wins; an empty HARNESS_HOST or HARNESS_PORT is nothing to go on and falls
# through to the file.
#
# No host name is built in here: a caller with nothing to go on must say so
# and stop. The caller must define die().

harness_config_load() {
    local env_host env_jump env_jump_set env_port _hc_mode _hc_status _hc_errexit
    HARNESS_CONFIG_FILE=${HARNESS_OPEN_CONFIG:-${XDG_CONFIG_HOME:-${HOME:-}/.config}/ubertooth-harness/harness-open.conf}
    # The environment wins over the file, so remember it before sourcing.
    env_host=${HARNESS_HOST-}
    env_jump=${HARNESS_JUMP-}
    env_jump_set=${HARNESS_JUMP+yes}
    env_port=${HARNESS_PORT-}
    if [ -f "$HARNESS_CONFIG_FILE" ]; then
        # Sourcing runs whatever the file contains, so accept only a file that
        # is yours and that no one else can write; $HARNESS_OPEN_CONFIG could
        # otherwise point at a file another local user controls.
        [ -O "$HARNESS_CONFIG_FILE" ] || die "$HARNESS_CONFIG_FILE is not owned by you; refusing to source it"
        _hc_mode=$(stat -f '%Lp' "$HARNESS_CONFIG_FILE" 2>/dev/null) ||
            _hc_mode=$(stat -c '%a' "$HARNESS_CONFIG_FILE" 2>/dev/null) || _hc_mode=
        if [ -n "$_hc_mode" ] && [ "$(( 0$_hc_mode & 0022 ))" -ne 0 ]; then
            die "$HARNESS_CONFIG_FILE is writable by others (mode $_hc_mode); chmod go-w it"
        fi
        # Read it like a shell profile: a failing line inside must not kill
        # the caller without a word, so drop errexit and check the status.
        # Whether the caller had it on is remembered, so this never turns it
        # on for a caller that does not want it.
        case $- in *e*) _hc_errexit=yes ;; *) _hc_errexit= ;; esac
        set +e
        # The file is the user's own and lives outside the repository, so
        # there is nothing for a linter to follow.
        # shellcheck source=/dev/null
        . "$HARNESS_CONFIG_FILE"
        _hc_status=$?
        [ -z "$_hc_errexit" ] || set -e
        [ "$_hc_status" -eq 0 ] ||
            die "$HARNESS_CONFIG_FILE failed (status $_hc_status); fix it or unset HARNESS_OPEN_CONFIG"
    fi
    [ -z "$env_host" ] || HARNESS_HOST=$env_host
    [ -z "$env_jump_set" ] || HARNESS_JUMP=$env_jump
    [ -z "$env_port" ] || HARNESS_PORT=$env_port
}
