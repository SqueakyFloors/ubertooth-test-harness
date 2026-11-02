#!/bin/sh
# The harness CI checks, shared by .github/workflows/test.yml and local runs.
#
#   scripts/ci.sh test    unit tests, plan validation, compile
#   scripts/ci.sh build   build the fork's host tools into a temporary prefix
#   scripts/ci.sh all     both
#   scripts/ci.sh libbtbb-pin   only the build's libbtbb pin check
#   scripts/ci.sh firmware-dk   build the nRF52840 DK role firmware (needs docker)
#
# firmware-dk compares the image it builds with the one another commit's
# sources build when DK_BASE_REF (a git ref, for example origin/main) or
# DK_BASE_SRC (a directory holding the sources, which wins where both are
# set) says what to compare against.
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
# libbtbb the fork is built and tested against, not upstream's `master` branch.
# This pin is the harness's own copy, deliberately: the harness never calls
# the fork's installer. It must match the fork's (LIBBTBB_REF in the fork's
# install.sh, also in its .github/workflows/build.yml); the build step warns
# when it does not (check_libbtbb_pin, ubertooth_harness/libbtbbpin.py). Bump
# it here and in README.md together, which tests/test_libbtbb_pin.py checks.
# A LIBBTBB_SOURCE clone has to hold the pin: a clone of the fork does, but
# one of another libbtbb, or one made before the pin landed, needs
# LIBBTBB_REF as well or the build stops at "unable to resolve libbtbb ref".
LIBBTBB_URL=https://github.com/SqueakyFloors/libbtbb.git
LIBBTBB_PIN=017676965a77cd7b00bfede343b7800dee6aa6a5

step() { printf '\n=== ci: %s ===\n' "$1"; }

# Bluetooth piconet roles are central and peripheral, never the older words
# (AGENTS.md). Only the external names this project does not own are allowed,
# each written here as it appears in the tree: libbtbb's and git's `master`
# branch, ESP-IDF's two SPP role constants, and one Ubertooth fork pull request
# title quoted in coverage/closed-pr-audit.jsonl, which names that branch too.
# A line is judged on its text with every allowed form deleted, so the path is
# never read as content and a second occurrence on the same line still fails.
# AGENTS.md and this file are skipped, because the rule and the check have to
# name the words they forbid.
ROLE_TERMS='master|slave'
ROLE_TERMS_ALLOW='`master` branch|"branch:master"|libbtbb-ref master|LIBBTBB_BRANCH = "master"|ESP_SPP_ROLE_MASTER,|ESP_SPP_ROLE_SLAVE,|pin to the current master'

check_role_terms() {
    step "Bluetooth role terms"
    if [ ! -f AGENTS.md ] || [ ! -f scripts/ci.sh ]; then
        echo "error: run scripts/ci.sh from the repository root" >&2
        return 1
    fi
    out=$WORK/role-terms
    if [ "$(git rev-parse --show-toplevel 2>/dev/null)" = "$(pwd -P)" ]; then
        # Tracked files, so a scratch file in a working tree is nobody's
        # failure. Only at the top level, where the pathspecs mean what
        # they say.
        git grep -nIiE "$ROLE_TERMS" -- ':!scripts/ci.sh' ':!AGENTS.md' \
            > "$out.lines" || [ $? = 1 ]
    else
        # Every file, which is what the tree scripts/local-ci.sh exports holds.
        grep -rnIiE "$ROLE_TERMS" . --exclude=ci.sh --exclude=AGENTS.md \
            --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
            > "$out.lines" || [ $? = 1 ]
    fi
    # One command each, so a step that errors ends the run instead of leaving
    # an empty file that reads as a clean tree.
    sed -E 's/^[^:]*:[0-9]+://' "$out.lines" > "$out.text"
    sed -E "s@$ROLE_TERMS_ALLOW@@g" "$out.text" > "$out.judged"
    grep -niE "$ROLE_TERMS" "$out.judged" > "$out.hits" || [ $? = 1 ]
    if [ -s "$out.hits" ]; then
        # The file's own lines, found by their position in the search output,
        # so a reader can search for what they see.
        awk -F: 'NR == FNR { bad[$1]; next } FNR in bad' \
            "$out.hits" "$out.lines" >&2
        echo "error: the lines above name a Bluetooth piconet role with an" \
             "old word; write central or peripheral. If the line holds an" \
             "external name this project does not own, or a pattern that has" \
             "to match the fork's own output, add that form to" \
             "ROLE_TERMS_ALLOW in scripts/ci.sh" >&2
        return 1
    fi
    echo "every Bluetooth role word is central or peripheral"
}

run_test() {
    check_role_terms

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
# gigabytes) across runs, and NCS_IMAGE replaces the image. With DK_BASE_REF
# (a git ref) or DK_BASE_SRC (a directory of DK firmware sources) it also
# builds those sources and fails when the image changed with no version bump
# (#210).
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
    # The sources every build compiles, copied here from the tree or from
    # another commit, so the comparison below builds the same paths twice and
    # a difference in the image is a difference in the sources.
    src=$workspace/utdk-src
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

    # The app sources, in the one place every build of them is compiled from.
    dk_sources() {
        rm -rf "$src"
        mkdir -p "$src"
        cp -R "$1/." "$src/"
    }

    dk_fw_version() {
        fw=$(sed -n 's/^#define UTDK_FW_VERSION "\(.*\)".*/\1/p' "$1/src/roles.h")
        if [ -z "$fw" ]; then
            echo "error: no UTDK_FW_VERSION in $1/src/roles.h" >&2
            return 1
        fi
        echo "$fw"
    }

    dk_sdk_version() {
        sdk=$(cat "$1/NCS_VERSION")
        if [ -z "$sdk" ]; then
            echo "error: $1/NCS_VERSION is empty" >&2
            return 1
        fi
        echo "$sdk"
    }

    # Whether these sources build the image they built before. Two builds of
    # one tree have to give one image, or a difference between two trees says
    # nothing about the change between them. Measured in CI on 2026-10-31:
    # two builds of one tree gave byte-identical images.
    dk_image_repeats() {
        step "the image $2 builds a second time"
        # Checked here, because this function is called in a || list, where
        # set -e does not end the script: a build that failed would otherwise
        # be read as an image that came out different.
        if ! dk_sources "$1" ||
           ! ncs "west build -p always --no-sysbuild -b $NCS_BOARD -d $build -s $src"
        then
            echo "error: $2 did not build a second time, so the images cannot" \
                 "be compared" >&2
            return 1
        fi
        cmp -s "$3" "$hex" && return 0
        echo "error: two builds of $2 gave different images, so comparing two" \
             "trees says nothing; make the build reproducible (fixed build" \
             "paths and timestamps) first" >&2
        return 1
    }

    # What a source change leaves on the boards: `nrfdk.py ensure` flashes a
    # DK only when it reports a firmware version other than the pinned one,
    # so a change that leaves UTDK_FW_VERSION alone is never flashed, and a
    # run then tests the old image and says nothing about the new one (#210).
    # The images decide, not the files: a comment or formatting change leaves
    # the image identical and asks for no bump.
    compare_dk_image() {
        base=$1
        label=$2
        step "the image against $label"
        for file in src/roles.h NCS_VERSION; do
            if [ ! -f "$base/$file" ]; then
                echo "error: $base holds no $file, so it is not a copy of" \
                     "$DK_FIRMWARE to compare against" >&2
                return 1
            fi
        done
        here=$(dk_fw_version "$repo/$DK_FIRMWARE")
        there=$(dk_fw_version "$base")
        # Any difference reflashes every board that runs the base's image,
        # which is what the comparison is about. A version reused from an
        # older release would not reach a board still carrying that old
        # image, which nothing here can see.
        if [ "$here" != "$there" ]; then
            echo "UTDK_FW_VERSION moved from $there to $here, so every board is" \
                 "reflashed; the images are not compared"
            return 0
        fi
        # Both builds use the SDK and the toolchain image this tree pins, so a
        # comparison cannot see what moving them did to the image. Since a new
        # toolchain builds a new image, that needs the bump as any source
        # change does; building the base with its own SDK would mean a second
        # workspace of several gigabytes.
        base_sdk=$(dk_sdk_version "$base")
        if [ "$base_sdk" != "$version" ]; then
            echo "error: the nRF Connect SDK moved from $base_sdk to $version" \
                 "while UTDK_FW_VERSION is still $here: a new toolchain builds" \
                 "a new image, so bump it in $DK_FIRMWARE/src/roles.h and" \
                 "ubertooth_harness/nrfdk.py" >&2
            return 1
        fi
        cp "$hex" "$workspace/utdk-here.hex"
        dk_sources "$base"
        ncs "west build -p always --no-sysbuild -b $NCS_BOARD -d $build -s $src"
        cp "$hex" "$workspace/utdk-there.hex"
        if cmp -s "$workspace/utdk-here.hex" "$workspace/utdk-there.hex"; then
            # The images are the same bytes, so the build directory holds this
            # tree's image whichever build wrote it last. Leave the sources in
            # the workspace as this tree's.
            dk_sources "$repo/$DK_FIRMWARE"
            echo "the image is the one $label builds, so $here still describes it"
            return 0
        fi
        # Before blaming the change: each side has to repeat itself, or the
        # difference is in the build and not in the sources.
        dk_image_repeats "$repo/$DK_FIRMWARE" "this tree" \
            "$workspace/utdk-here.hex" || return 1
        dk_image_repeats "$base" "$label" "$workspace/utdk-there.hex" || return 1
        echo "error: the image differs from the one $label builds while" \
             "UTDK_FW_VERSION is still $here: bump it in" \
             "$DK_FIRMWARE/src/roles.h and ubertooth_harness/nrfdk.py, or a" \
             "board already running $here keeps the old image" >&2
        return 1
    }

    step "build $DK_FIRMWARE for $NCS_BOARD"
    dk_sources "$repo/$DK_FIRMWARE"
    ncs "west build -p always --no-sysbuild -b $NCS_BOARD -d $build -s $src"

    # The same check the harness makes before it flashes a board: an image
    # that does not carry the pinned version string would be left in place by
    # `nrfdk.py ensure` on a board that already runs the old one.
    step "the image carries the pinned firmware version"
    ls -l "$hex"
    # The harness package runs in its own Python 3.12 environment only, as
    # run_build does it.
    [ -x .venv/bin/python ] || "$PYTHON" bootstrap.py
    .venv/bin/python -m ubertooth_harness.nrfdk check-image "$hex"

    # The sources to compare the image against: a directory of them
    # (DK_BASE_SRC, which wins where both are set), or a git ref this
    # repository holds (DK_BASE_REF, which the Firmware workflow sets to the
    # commit a pull request is against). With neither, nothing is compared.
    # This tree's side is the working tree, so a local run compares what is
    # there now, where the other side is a commit. A ref
    # that is set and does not resolve is an error rather than a skip: it
    # means the history the comparison needs is not there, and skipping would
    # pass a pull request nobody checked.
    if [ -n "${DK_BASE_SRC:-}" ]; then
        compare_dk_image "$DK_BASE_SRC" "$DK_BASE_SRC"
    elif [ -z "${DK_BASE_REF:-}" ]; then
        step "the image against another commit"
        echo "no base sources given (DK_BASE_REF or DK_BASE_SRC), so the image" \
             "is not compared and a missing version bump would not show up here"
    elif ! git -C "$repo" cat-file -e "$DK_BASE_REF:$DK_FIRMWARE/src/roles.h" 2>/dev/null; then
        step "the image against $DK_BASE_REF"
        echo "error: $DK_BASE_REF does not resolve to a tree holding" \
             "$DK_FIRMWARE here, so the image cannot be compared: the ref is" \
             "not in this history (the workflow checks the whole history out" \
             "for it), or it is a commit from before that directory. Name a" \
             "ref that has it, or unset DK_BASE_REF" >&2
        return 1
    else
        base_tree=$workspace/base-firmware
        rm -rf "$base_tree"
        mkdir -p "$base_tree"
        git -C "$repo" archive -o "$workspace/base-firmware.tar" \
            "$DK_BASE_REF" "$DK_FIRMWARE"
        tar -xf "$workspace/base-firmware.tar" -C "$base_tree"
        rm -f "$workspace/base-firmware.tar"
        compare_dk_image "$base_tree/$DK_FIRMWARE" "$DK_BASE_REF"
    fi
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
