#!/bin/sh
# The harness CI checks, shared by .github/workflows/test.yml and local runs.
#
#   scripts/ci.sh test    unit tests, plan validation, compile
#   scripts/ci.sh build   build the fork's host tools into a temporary prefix
#   scripts/ci.sh all     both
#   scripts/ci.sh libbtbb-pin   only the build's libbtbb pin check
#   scripts/ci.sh firmware-dk   build the nRF52840 DK role firmware (needs docker)
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
LIBBTBB_PIN=bf878df27e131dd8ab56bb3497918eb9a83f607b

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

    # One process per module, one per core (scripts/test.py), with the ten
    # slowest tests printed so a slow test shows up in the job log. The plain
    # `python -m unittest discover -s tests` still runs the same suite.
    step "unit tests"
    python scripts/test.py --durations 10

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

# The nRF52840 DK role firmware (firmware/nrf52840dk), which nothing else
# compiles: a source change that does not build used to reach main unnoticed
# (#147). It is built with the nRF Connect SDK version the firmware pins
# (firmware/nrf52840dk/NCS_VERSION), in Nordic's own toolchain image for that
# version, so neither CI nor a developer installs the 6 GB SDK toolchain to
# compile it. Needs docker; NCS_WORKSPACE keeps the SDK workspace (several
# gigabytes) across runs, and NCS_IMAGE replaces the image.
#
# This is not part of `all`: the image and the workspace are gigabytes and the
# firmware changes rarely, so the workflow runs it on a change under
# firmware/nrf52840dk, on a schedule, and by hand.
NCS_URL=https://github.com/nrfconnect/sdk-nrf
NCS_BOARD=nrf52840dk/nrf52840
DK_FIRMWARE=firmware/nrf52840dk

run_firmware_dk() {
    # The physical path: docker mounts and `west build -s` must not depend on
    # a symlinked parent (/tmp on macOS).
    repo=$(pwd -P)
    version=$(cat "$DK_FIRMWARE/NCS_VERSION")
    image=${NCS_IMAGE:-ghcr.io/nrfconnect/sdk-nrf-toolchain:$version}
    workspace=${NCS_WORKSPACE:-$WORK/ncs}
    build=$workspace/utdk-build
    hex=$build/zephyr/zephyr.hex
    stamp=$workspace/.ncs-version
    mkdir -p "$workspace"

    # Every toolchain command runs in that image, which takes one shell
    # string: bash is its entry point and loads the toolchain environment
    # itself (BASH_ENV). The repository is mounted read-only, so a build
    # cannot write into the working tree, and everything written goes to the
    # workspace. git is told the mounted workspace is safe to use, since the
    # container is root and the directory belongs to whoever ran this.
    ncs() {
        docker run --rm \
            -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory \
            -e GIT_CONFIG_VALUE_0='*' \
            -v "$repo:$repo:ro" -v "$workspace:$workspace" -w "$workspace" \
            "$image" "$1"
    }

    step "nRF Connect SDK $version toolchain ($image)"
    ncs 'west --version; cmake --version | head -1; arm-zephyr-eabi-gcc --version | head -1'

    step "nRF Connect SDK $version workspace in $workspace"
    if [ -d "$workspace/.west" ]; then
        # A workspace made for another version keeps that version's manifest
        # revision, so reusing it would build sources the firmware does not
        # pin. Say so instead of deleting someone's workspace.
        kept=$([ -f "$stamp" ] && cat "$stamp" || echo unknown)
        if [ "$kept" != "$version" ]; then
            echo "error: the workspace in $workspace was made for nRF Connect SDK" \
                 "$kept, not $version; remove it or set NCS_WORKSPACE elsewhere" >&2
            return 1
        fi
        echo "reusing the $version workspace that is there"
    else
        ncs "west init -m $NCS_URL --mr $version ."
        echo "$version" > "$stamp"
    fi
    # --narrow with a depth of one: the manifest's revisions, not every ref
    # and the whole history of forty repositories.
    ncs 'west update --narrow -o=--depth=1'

    step "build $DK_FIRMWARE for $NCS_BOARD"
    ncs "west build -p always --no-sysbuild -b $NCS_BOARD -d $build -s $repo/$DK_FIRMWARE"

    # The same check the harness makes before it flashes a board: an image
    # that does not carry the pinned version string would be left in place by
    # `nrfdk.py ensure` on a board that already runs the old one.
    step "the image carries the pinned firmware version"
    ls -l "$hex"
    # The harness package runs in its own Python 3.12 environment only, as
    # run_build does it.
    [ -x .venv/bin/python ] || "$PYTHON" bootstrap.py
    .venv/bin/python -m ubertooth_harness.nrfdk check-image "$hex"
}

case "${1:-}" in
    test) run_test ;;
    build) run_build ;;
    # Only the pin check of the build, against UBERTOOTH_SOURCE (tests).
    libbtbb-pin) check_libbtbb_pin "${UBERTOOTH_SOURCE:?UBERTOOTH_SOURCE is required}" ;;
    firmware-dk) run_firmware_dk ;;
    all) run_test; run_build ;;
    *) echo "usage: scripts/ci.sh test|build|all|libbtbb-pin|firmware-dk" >&2; exit 2 ;;
esac
step "${1} passed"
