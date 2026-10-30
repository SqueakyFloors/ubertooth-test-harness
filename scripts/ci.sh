#!/bin/sh
# The harness CI checks, shared by .github/workflows/test.yml and local runs.
#
#   scripts/ci.sh test    unit tests, plan validation, compile
#   scripts/ci.sh build   build the fork's host tools into a temporary prefix
#   scripts/ci.sh all     both
#   scripts/ci.sh libbtbb-pin   only the build's libbtbb pin check
#
# Run from the repository root. PYTHON selects the interpreter that runs
# bootstrap.py (default python3). UBERTOOTH_SOURCE / LIBBTBB_SOURCE point the
# build at existing clones instead of cloning from GitHub; the build always
# uses UBERTOOTH_REF (default main) and LIBBTBB_REF (default LIBBTBB_PIN).
# Use scripts/local-ci.sh to run these checks against an exact commit.
set -eu

PYTHON=${PYTHON:-python3}
WORK=${CI_TEMP:-${RUNNER_TEMP:-$(mktemp -d)}}

# libbtbb comes from this project's fork at a pinned commit, so a push to
# libbtbb cannot change a harness build. It is the commit the Ubertooth fork
# pins for its own builds (fork issue #320), so a harness run builds the
# libbtbb the fork is built and tested against, not upstream master.
# This pin is the harness's own copy, deliberately: the harness never calls
# the fork's installer. It must match the fork's (LIBBTBB_REF in the fork's
# install.sh, also in its .github/workflows/build.yml); the build step warns
# when it does not (check_libbtbb_pin, ubertooth_harness/libbtbbpin.py). Bump
# it here and in README.md together, which tests/test_libbtbb_pin.py checks.
# A LIBBTBB_SOURCE clone has to hold the pin: a clone of the fork does, but
# one of another libbtbb, or one made before the pin landed, needs
# LIBBTBB_REF as well or the build stops at "unable to resolve libbtbb ref".
LIBBTBB_URL=https://github.com/SqueakyFloors/libbtbb.git
LIBBTBB_PIN=2fd0d391d99e9b0620146f63f439c1ec74f79861

step() { printf '\n=== ci: %s ===\n' "$1"; }

run_test() {
    step "bootstrap"
    "$PYTHON" bootstrap.py | tee "$WORK/first.log"
    grep -q "bootstrap: created" "$WORK/first.log"
    "$PYTHON" bootstrap.py | tee "$WORK/second.log"
    grep -q "bootstrap: updated" "$WORK/second.log"
    py=$PWD/.venv/bin/python

    if [ "$(uname -s)" = Darwin ] && [ -x /usr/bin/python3 ]; then
        step "bootstrap from the stock macOS python3"
        /usr/bin/python3 -V
        /usr/bin/python3 bootstrap.py --venv "$WORK/stock-launcher-venv"
        "$WORK/stock-launcher-venv/bin/python" -V | grep -q "Python 3.12"
    fi

    step "harness environment identity"
    PATH=$PWD/.venv/bin:$PATH
    export PATH
    test "$(command -v python)" = "$py"
    python -c "import json; from ubertooth_harness.environment import capture, require_harness_environment; require_harness_environment(); print(json.dumps(capture(), indent=2))"

    step "unit tests"
    python -m unittest discover -s tests -v

    step "validate plans and resource template"
    for plan in testplans/*.yaml; do
        python -m ubertooth_harness validate-plan "$plan"
    done
    python -c "from ubertooth_harness.resources import FileResourceRegistry; FileResourceRegistry.load('hardware.example.yaml')"
    for suite in fast slow mac-polling release host mac; do
        python -m ubertooth_harness validate-plan "testplans/$suite.yaml" \
            --hardware hardware.example.yaml --suite "$suite"
    done

    step "compile Python sources"
    python -m compileall -q ubertooth_harness tests
}

# The fork may have moved its libbtbb pin (install.sh) since LIBBTBB_PIN was
# copied here: warn once with both commits, do not fail (#85). The build and
# run-suite steps below still record the check in their run.jsonl, without
# printing it again.
check_libbtbb_pin() {
    step "libbtbb pin against the fork's install.sh"
    .venv/bin/python -m ubertooth_harness.libbtbbpin \
        --ubertooth-source "$1" --ubertooth-ref "${UBERTOOTH_REF:-main}" \
        --libbtbb-url "$LIBBTBB_URL" --libbtbb-ref "${LIBBTBB_REF:-$LIBBTBB_PIN}"
    UBERTOOTH_HARNESS_LIBBTBB_PIN_SHOWN=1
    export UBERTOOTH_HARNESS_LIBBTBB_PIN_SHOWN
}

run_build() {
    step "build"
    [ -x .venv/bin/python ] || "$PYTHON" bootstrap.py
    ut=${UBERTOOTH_SOURCE:-}
    bb=${LIBBTBB_SOURCE:-}
    if [ -z "$ut" ]; then
        ut=$WORK/ubertooth
        git clone --quiet https://github.com/SqueakyFloors/ubertooth.git "$ut"
    fi
    if [ -z "$bb" ]; then
        bb=$WORK/libbtbb
        git clone --quiet "$LIBBTBB_URL" "$bb"
    fi

    check_libbtbb_pin "$ut"
    .venv/bin/python -m ubertooth_harness build \
        --ubertooth-source "$ut" --ubertooth-ref "${UBERTOOTH_REF:-main}" \
        --libbtbb-source "$bb" --libbtbb-ref "${LIBBTBB_REF:-$LIBBTBB_PIN}" \
        --with-python --output "$WORK/build" > "$WORK/build.json"
    cat "$WORK/build.json"
    # -V prints the host tools' version, then reads every attached board and
    # exits 1 if one cannot be read. Only the version line is wanted here, so
    # the status is not what decides, as in the build step itself: a busy
    # board on a lab host must not abort the job.
    "$WORK/build/prefix/bin/ubertooth-util" -V > "$WORK/host-version.txt" 2>&1 || true
    cat "$WORK/host-version.txt"
    grep -q '^Host tools version: ' "$WORK/host-version.txt"
    test -z "$(git -C "$ut" status --porcelain)"

    step "host-only tests with the built tools (no Ubertooth)"
    case "$(uname -s)" in
        Darwin) platform=macos ;;
        *) platform=linux ;;
    esac
    printf 'resources:\n  CI_Host:\n    type: Host\n    platform: %s\n    roles: [test target]\n' \
        "$platform" > "$WORK/host-only.yaml"
    PATH="$WORK/build/prefix/bin:$PATH" .venv/bin/python -m ubertooth_harness run-suite \
        --plan testplans/host.yaml --suite host \
        --hardware "$WORK/host-only.yaml" --host CI_Host \
        --ubertooth-source "$ut" --ubertooth-ref "${UBERTOOTH_REF:-main}" \
        --libbtbb-source "$bb" --libbtbb-ref "${LIBBTBB_REF:-$LIBBTBB_PIN}" \
        --run-id ci-host --output "$WORK/runs"

    step "not applicable on this host: decided at planning, matches the dry run"
    PATH="$WORK/build/prefix/bin:$PATH" .venv/bin/python -m ubertooth_harness run-suite \
        --dry-run --plan testplans/host.yaml --suite host \
        --hardware "$WORK/host-only.yaml" --host CI_Host \
        --ubertooth-source "$ut" --ubertooth-ref "${UBERTOOTH_REF:-main}" \
        --output "$WORK/dry-runs" | tee "$WORK/dry-run.txt"
    .venv/bin/python - "$platform" "$WORK/runs/ci-host/run.jsonl" "$WORK/dry-run.txt" <<'EOF'
import json, re, sys
platform, record, dry_run = sys.argv[1:]
events = [json.loads(line) for line in open(record)]
items = [e["details"] for e in events if e["event_type"] == "not_applicable"]
summary = next(e["details"] for e in events if e["event_type"] == "run_finished")
bluez = [i for i in items if "BlueZ" in i["reason"]]
planned = int(re.search(r"(\d+) case\(s\) and \d+ test\(s\) not applicable",
                        open(dry_run).read())[1])
print(f"{platform}: {len(items)} not applicable ({len(bluez)} BlueZ), "
      f"{summary['skipped_cases']} skipped, dry run planned {planned}")
assert summary["passed"], summary
assert summary["skipped_cases"] == 0, "a case skipped at run time"
assert summary["not_applicable_cases"] == len(items) == planned, (len(items), planned)
# Every follow/scan case on macOS (no BlueZ); none on Linux (built with BlueZ).
assert (len(bluez) >= 12) if platform == "macos" else not bluez, bluez
EOF

    step "BlueZ tools: required in a Linux build's prefix, and their cases run"
    .venv/bin/python - "$platform" "$WORK/build.json" "$WORK/runs/ci-host/run.jsonl" <<'EOF'
import json, sys
platform, build, record = sys.argv[1:]
tools = ("ubertooth-scan", "ubertooth-follow")
names = {item["name"] for item in json.load(open(build))["artifacts"]}
built = [tool for tool in tools if f"built {tool}" in names]
results = {}
for line in open(record):
    event = json.loads(line)
    case_id = event["details"].get("case_id", "")
    if event["event_type"] == "check_finished":
        for tool in tools:
            if case_id.endswith(f"-{tool}-help"):
                results[tool] = event["status"]
print(f"{platform}: built {built}, help cases {results}")
if platform == "linux":
    assert built == list(tools), built
    assert results == {tool: "passed" for tool in tools}, results
else:
    assert not built and not results, (built, results)
EOF
}

case "${1:-}" in
    test) run_test ;;
    build) run_build ;;
    # Only the pin check of the build, against UBERTOOTH_SOURCE (tests).
    libbtbb-pin) check_libbtbb_pin "${UBERTOOTH_SOURCE:?UBERTOOTH_SOURCE is required}" ;;
    all) run_test; run_build ;;
    *) echo "usage: scripts/ci.sh test|build|all|libbtbb-pin" >&2; exit 2 ;;
esac
step "${1} passed"
