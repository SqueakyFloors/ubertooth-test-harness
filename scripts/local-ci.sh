#!/bin/sh
# Run scripts/ci.sh against an exact committed revision, in a fresh export,
# locally or on another host. Uncommitted changes are never tested.
#
#   scripts/local-ci.sh [-r REV] [-s SSH_PREFIX] [test|build|all|firmware-dk]
#
# -r REV         revision to test (default HEAD)
# -s SSH_PREFIX  run on another host. The prefix is a command that runs its
#                last arguments remotely, e.g. "ssh lab-host" or, through a
#                jump host, "ssh jump ssh lab-host". Only `sh -s` is sent as
#                the remote command; the tree arrives on stdin, so no quoting
#                passes through the hops.
#
# Ends with "LOCAL CI PASSED <sha> on <host>" or exits nonzero. Paste that
# line (and the host OS) into the PR when GitHub Actions cannot run.
set -eu

rev=HEAD
remote=
while getopts r:s: opt; do
    case $opt in
        r) rev=$OPTARG ;;
        s) remote=$OPTARG ;;
        *) echo "usage: scripts/local-ci.sh [-r REV] [-s SSH_PREFIX] [test|build|all]" >&2; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
what=${1:-all}
case $what in
    test|build|all|firmware-dk) ;;
    *) echo "unknown check set: $what" >&2; exit 2 ;;
esac

sha=$(git rev-parse --verify "$rev^{commit}")

# One self-contained script: unpack the exported tree, run the checks, clean up.
script() {
    cat <<EOF
set -eu
d=\$(mktemp -d "\${TMPDIR:-/tmp}/harness-ci.XXXXXX")
trap 'rm -rf "\$d"' EXIT
mkdir "\$d/tree" "\$d/work"
base64 -d > "\$d/tree.tgz" <<'PAYLOAD'
EOF
    git archive --format=tar.gz "$sha" | base64
    cat <<EOF
PAYLOAD
tar -xzf "\$d/tree.tgz" -C "\$d/tree"
cd "\$d/tree"
CI_TEMP="\$d/work" sh scripts/ci.sh $what
echo "LOCAL CI PASSED $sha on \$(hostname) (\$(uname -sr))"
EOF
}

if [ -n "$remote" ]; then
    # shellcheck disable=SC2086 # the prefix is deliberately word-split
    script | $remote sh -s
else
    script | sh -s
fi
