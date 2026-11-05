#!/bin/sh
# The harness CI checks, shared by .github/workflows/test.yml and local runs.
#
#   scripts/ci.sh test    unit tests, plan validation, compile
#   scripts/ci.sh build   build the fork's host tools into a temporary prefix
#   scripts/ci.sh all     both
#   scripts/ci.sh libbtbb-pin   only the build's libbtbb pin check
#   scripts/ci.sh dfu-part-table  only the build's DFU part table check
#   scripts/ci.sh firmware-dk   build the nRF52840 DK role firmware (needs docker)
#
# firmware-dk builds both DK images and compares each with the one another
# commit's sources build when DK_BASE_REF (a git ref, for example origin/main) or
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
LIBBTBB_PIN=52b2afcb0c4b8b03412dac759aad88debdfb18e2

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

# The flash sizes a DFU read-back length is checked against are the harness's
# own copy of the fork's part table, so the fork may have added a part since:
# warn once with the entry that is missing, do not fail (#283). The build and
# run-suite steps below still record the check in their run.jsonl, without
# printing it again. This puts it on the workflow run page, where a reader
# sees it without opening a log.
check_dfu_part_table() {
    step "DFU part table against the fork's own"
    .venv/bin/python -m ubertooth_harness.dfuparts \
        --ubertooth-source "$1" --ubertooth-ref "${UBERTOOTH_REF:-main}"
    UBERTOOTH_HARNESS_DFU_PART_TABLE_SHOWN=1
    export UBERTOOTH_HARNESS_DFU_PART_TABLE_SHOWN
}

# What the install describes to the two things that look for a library, and
# what a build against the installed one says about itself (#333; fork
# ubertooth#1044 installs libubertooth.pc, ubertooth#866 builds the tools
# against an installed libubertooth). The harness builds the whole host tree
# into one prefix, so until now nothing here read either file, nor configured
# the tools the way a packager does.
#
# Every program run here is run with UBERTOOTH_TEST_NO_DEVICES=1, so the step
# is about the install and nothing else: without it libubertooth's version
# report opens every attached board, and on a bench host a board another agent
# took between two runs would make two identical installs answer differently.
#
# $1 is the Ubertooth source tree, $2 the prefix the build installed into.
# The names here all begin il_, because POSIX sh has no local variables and
# the other functions in this file use short generic ones.
check_installed_library() {
    il_source=$1
    il_prefix=$2
    il_work=$WORK/installed
    rm -rf "$il_work"
    mkdir -p "$il_work"
    # Which of lib and lib64 this install used, for the loader path and the
    # rpath (host/CMakeLists.txt chooses by system). Both go on the
    # pkg-config and loader paths, because the two libraries choose
    # independently -- libubertooth follows CMAKE_INSTALL_LIBDIR and libbtbb
    # its own LIB_SUFFIX -- so an install can hold one in each.
    il_libdir=$il_prefix/lib
    [ -d "$il_prefix/lib64" ] && il_libdir=$il_prefix/lib64
    il_libpath=$il_prefix/lib:$il_prefix/lib64
    # Prepended, not replaced: libubertooth.pc has "Requires: libbtbb
    # libusb-1.0", and libusb's own file is the system's. The install's
    # directories come first, and the checks below require the answers to be
    # this install's rather than trusting the order. Kept, so the steps after
    # this function see the value the job had.
    il_saved_pkg_config_path=${PKG_CONFIG_PATH:-}
    PKG_CONFIG_PATH=$il_prefix/lib/pkgconfig:$il_prefix/lib64/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}
    export PKG_CONFIG_PATH
    UBERTOOTH_TEST_NO_DEVICES=1
    export UBERTOOTH_TEST_NO_DEVICES
    # The version line the build's own tools printed, which every program
    # built against this install must print too: it names the libubertooth
    # revision, so a system library that answered instead is caught.
    il_version=$(grep '^Host tools version: ' "$WORK/host-version.txt")

    # One program for both consumers. ubertooth_print_versions() is the
    # library call that needs no board: it prints the library's own version
    # and the libbtbb it loaded, and with the no-devices switch set it
    # reports that no Ubertooth is attached and exits 0, so the run proves
    # the headers, the link, and the loader path all reach this install.
    cat > "$il_work/consumer.c" <<'EOF'
#include <stdio.h>
#include <ubertooth.h>

int main(void)
{
	ubertooth_print_versions(stdout, NULL);
	return 0;
}
EOF

    step "pkg-config describes the installed libubertooth"
    pkg-config --exists libubertooth
    il_pcversion=$(pkg-config --modversion libubertooth)
    echo "libubertooth.pc: version $il_pcversion, prefix \
$(pkg-config --variable=prefix libubertooth)"
    pkg-config --cflags --libs libubertooth
    # The file that answered is this install's, whatever else is on the
    # search path: the prefix it reports, and the include directory the
    # consumer will compile against, both have to be here. Compared as
    # resolved paths, since the file works its prefix out from where it is
    # (${pcfiledir}/../..) and /tmp is a link to /private/tmp on macOS.
    .venv/bin/python -m ubertooth_harness.installedlib same-path \
        "$(pkg-config --variable=prefix libubertooth)" "$il_prefix" \
        --what "the prefix libubertooth.pc reports"
    .venv/bin/python -m ubertooth_harness.installedlib under-prefix \
        /dev/stdin --prefix "$il_prefix" --marker 'includedir: ' \
        <<EOF
includedir: $(pkg-config --variable=includedir libubertooth)
EOF
    # Deliberately word-split, which is how a compiler takes pkg-config's
    # answer; a prefix holding a space would need an array and this is sh.
    # shellcheck disable=SC2086
    cc $(pkg-config --cflags libubertooth) "$il_work/consumer.c" \
        -o "$il_work/pc-consumer" $(pkg-config --libs libubertooth)
    # The .pc names no run path, as a distribution's does not: the loader is
    # told where the library is, which is what a packaged program's own
    # install would do.
    DYLD_LIBRARY_PATH=$il_libpath LD_LIBRARY_PATH=$il_libpath \
        "$il_work/pc-consumer" > "$il_work/pc-consumer.txt" 2>&1
    cat "$il_work/pc-consumer.txt"
    .venv/bin/python -m ubertooth_harness.installedlib consumer-identity \
        "$il_work/pc-consumer.txt" --prefix "$il_prefix" \
        --version-line "$il_version"

    step "a CMake project finds the installed ubertooth package"
    mkdir -p "$il_work/cmake-consumer"
    cp "$il_work/consumer.c" "$il_work/cmake-consumer/consumer.c"
    # The version is asked for exactly, so ubertoothConfigVersion.cmake is
    # read and has to agree with what libubertooth.pc answered: the build
    # writes all three files, and this is what says they describe one
    # library. The imported target's location is printed so the step can
    # require the library that will be linked to be this install's.
    cat > "$il_work/cmake-consumer/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.16)
project(ubertooth-consumer C)
find_package(ubertooth ${UBERTOOTH_EXPECTED_VERSION} EXACT REQUIRED)
get_target_property(ubertooth_location ubertooth::ubertooth IMPORTED_LOCATION)
message(STATUS "consumer links ${ubertooth_location}")
add_executable(consumer consumer.c)
target_link_libraries(consumer PRIVATE ubertooth::ubertooth)
EOF
    cmake -S "$il_work/cmake-consumer" -B "$il_work/cmake-build" \
        -DCMAKE_PREFIX_PATH="$il_prefix" -DCMAKE_BUILD_RPATH="$il_libdir" \
        -DUBERTOOTH_EXPECTED_VERSION="$il_pcversion" \
        > "$il_work/cmake-configure.txt" 2>&1 || {
            cat "$il_work/cmake-configure.txt"; return 1; }
    grep -E '^-- consumer links ' "$il_work/cmake-configure.txt"
    .venv/bin/python -m ubertooth_harness.installedlib under-prefix \
        "$il_work/cmake-configure.txt" --prefix "$il_prefix" \
        --marker '-- consumer links '
    cmake --build "$il_work/cmake-build" > "$il_work/cmake-build.txt" 2>&1 || {
            cat "$il_work/cmake-build.txt"; return 1; }
    "$il_work/cmake-build/consumer" > "$il_work/cmake-consumer.txt" 2>&1
    cat "$il_work/cmake-consumer.txt"
    # The same answer as the pkg-config consumer: the two files describe one
    # install, which is what the fork writes them side by side to do. Both
    # runs are board-free, so the comparison is about the install.
    cmp "$il_work/pc-consumer.txt" "$il_work/cmake-consumer.txt"

    step "the host tools configure and build against the installed library"
    # host/ubertooth-tools on its own, which is how a packager builds the
    # tools and the one path FindUBERTOOTH.cmake is read on; a whole-tree
    # build uses the libubertooth beside it and never reads that file.
    cmake -S "$il_source/host/ubertooth-tools" -B "$il_work/tools-build" \
        -DCMAKE_PREFIX_PATH="$il_prefix" > "$il_work/tools-configure.txt" 2>&1 \
        || { cat "$il_work/tools-configure.txt"; return 1; }
    grep -E '^-- Found LIBUBERTOOTH: ' "$il_work/tools-configure.txt"
    .venv/bin/python -m ubertooth_harness.installedlib under-prefix \
        "$il_work/tools-configure.txt" --prefix "$il_prefix" \
        --marker '-- Found LIBUBERTOOTH: '
    # The configure says which libbtbb it found and which one the tree pins,
    # so a measurement taken from a build can be read back to a libbtbb
    # (installedlib.py, which the unit tests exercise).
    # Against the pin the fork's own FindBTBB.cmake sets, so the line is
    # read back to the tree rather than only being well formed. The configure
    # prints that variable, so the two agree unless the message stops
    # reporting the tree's own pin -- which is the thing a reader of a
    # measurement relies on.
    il_pin=$(.venv/bin/python -m ubertooth_harness.installedlib fork-pin \
        "$il_source/host/cmake/modules/FindBTBB.cmake")
    .venv/bin/python -m ubertooth_harness.installedlib configure-identity \
        "$il_work/tools-configure.txt" --prefix "$il_prefix" \
        --expected-pin "$il_pin"
    cmake --build "$il_work/tools-build" --target ubertooth-util \
        > "$il_work/tools-util.txt" 2>&1 || {
            cat "$il_work/tools-util.txt"; return 1; }
    # Where the standalone build writes it, named rather than searched for,
    # so a build that wrote nothing is this step's failure and not a later
    # grep's.
    DYLD_LIBRARY_PATH=$il_libpath LD_LIBRARY_PATH=$il_libpath \
        "$il_work/tools-build/src/ubertooth-util" -V \
        > "$il_work/tools-version.txt" 2>&1
    cat "$il_work/tools-version.txt"
    grep -qxF "$il_version" "$il_work/tools-version.txt"

    step "a libubertooth without what the tools call is refused by name"
    # A copy of the install with one of the functions FindUBERTOOTH.cmake
    # probes for taken out of the headers, which is what an install from an
    # older commit of the fork looks like: the library and the header are
    # both in place, so without the probe the configure would pass and the
    # compiler would fail with a page of errors about missing symbols.
    cp -R "$il_prefix" "$il_work/old-prefix"
    .venv/bin/python -m ubertooth_harness.installedlib age-headers \
        "$il_work/old-prefix/include"
    # pkg-config pointed at the aged copy too, so which headers the probe
    # reads does not rest on CMake preferring CMAKE_PREFIX_PATH to the
    # hints pkg-config gave it.
    if PKG_CONFIG_PATH=$il_work/old-prefix/lib/pkgconfig:$il_work/old-prefix/lib64/pkgconfig \
        cmake -S "$il_source/host/ubertooth-tools" -B "$il_work/old-build" \
            -DCMAKE_PREFIX_PATH="$il_work/old-prefix" \
            > "$il_work/old-configure.txt" 2>&1
    then
        cat "$il_work/old-configure.txt"
        echo "error: the configure accepted a libubertooth missing a function" \
             "the tools call" >&2
        return 1
    fi
    # Both on one line: a refusal that said the install is older while
    # naming some other function would be a different failure, and the name
    # appearing anywhere in the log would not be the refusal at all. The
    # message wraps, so the log is read with its line endings squeezed out.
    tr '\n' ' ' < "$il_work/old-configure.txt" | tr -s ' ' \
        | grep -qE 'is older than these tools: it does not have [^.]*cmd_get_bootloader_info' \
        || { cat "$il_work/old-configure.txt"
             echo "error: the configure refused the aged install for some" \
                  "other reason than the function it is missing" >&2
             return 1; }
    # The refusal as the user would read it, which is three wrapped lines of
    # a log the rest of which is an ordinary configure.
    grep -A 2 'is older than these tools' "$il_work/old-configure.txt"
    unset UBERTOOTH_TEST_NO_DEVICES
    # Put back what the job had, so the steps after this one see it.
    if [ -n "$il_saved_pkg_config_path" ]; then
        PKG_CONFIG_PATH=$il_saved_pkg_config_path
        export PKG_CONFIG_PATH
    else
        unset PKG_CONFIG_PATH
    fi
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
    check_dfu_part_table "$ut"
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

    check_installed_library "$ut" "$WORK/build/prefix"
    # The source tree is only read by the checks above; say so here rather
    # than leave a reader to trust it.
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
    # The second image's build directory, kept apart from the one the
    # comparison builds in.
    chansel1=$workspace/utdk-chansel1
    chansel1_hex=$chansel1/zephyr/zephyr.hex
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
    # Chained, so a copy that failed is reported and not built over.
    dk_sources() {
        rm -rf "$src" && mkdir -p "$src" && cp -R "$1/." "$src/"
    }

    # These sources build two images, named `default` and `chansel1` here.
    # Each has a build directory of its own, so one never overwrites the
    # other's image.
    dk_build_dir() {
        case $1 in
            default) echo "$build" ;;
            chansel1) echo "$chansel1" ;;
            *) echo "error: no such DK image: $1" >&2; return 1 ;;
        esac
    }

    # What an image is called in the output a reader sees.
    dk_image_name() {
        case $1 in
            default) echo "default" ;;
            chansel1) echo "algorithm 1" ;;
            *) echo "$1" ;;
        esac
    }

    # Build image $2 from the sources in $1. The two images differ only in
    # which controller is compiled, which the Kconfig fragment and the
    # devicetree overlay select. Every step is checked here, because callers
    # run this where set -e does not end the script, and a copy or a build
    # that failed would otherwise leave the last build's image to be read as
    # this one's.
    dk_build() {
        extra=
        case $2 in
            default) ;;
            chansel1) extra=" -- -DEXTRA_CONF_FILE=chan-sel-1.conf \
                         -DEXTRA_DTC_OVERLAY_FILE=chan-sel-1.overlay" ;;
            *) echo "error: no such DK image: $2" >&2; return 1 ;;
        esac
        where=$(dk_build_dir "$2") || return 1
        dk_sources "$1" || return 1
        ncs "west build -p always --no-sysbuild -b $NCS_BOARD \
             -d $where -s $src$extra"
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
        step "the $(dk_image_name "$3") image of $2 builds a second time"
        # Checked here, because this function is called in a || list, where
        # set -e does not end the script: a build that failed would otherwise
        # be read as an image that came out different.
        if ! dk_build "$1" "$3"; then
            echo "error: $2 did not build its $(dk_image_name "$3") image a" \
                 "second time, so the images cannot be compared" >&2
            return 1
        fi
        cmp -s "$4" "$(dk_build_dir "$3")/zephyr/zephyr.hex" && return 0
        echo "error: two builds of $2 gave different $(dk_image_name "$3")" \
             "images, so comparing two trees says nothing; make the build" \
             "reproducible (fixed build paths and timestamps) first" >&2
        return 1
    }

    # What a source change leaves on the boards: `nrfdk.py ensure` flashes a
    # DK only when it reports a firmware version other than the pinned one,
    # so a change that leaves UTDK_FW_VERSION alone is never flashed, and a
    # run then tests the old image and says nothing about the new one (#210).
    # The images decide, not the files: a comment or formatting change leaves
    # the image identical and asks for no bump.
    # One of the two images, built from the base's sources and compared with
    # this tree's. $base, $label, and $here are compare_dk_image's.
    compare_one_dk_image() {
        name=$1
        human=$(dk_image_name "$name")
        here_hex=$workspace/utdk-$name-here.hex
        there_hex=$workspace/utdk-$name-there.hex
        step "the $human image against $label"
        # A base from before the algorithm 1 image builds none, and no board
        # can be carrying one: that image's version string arrived with it,
        # so `ensure` flashes every board asked for it whatever a comparison
        # would say.
        if [ "$name" = chansel1 ] &&
           { [ ! -f "$base/chan-sel-1.conf" ] ||
             [ ! -f "$base/chan-sel-1.overlay" ]; }; then
            echo "$label builds no $human image, so no board carries one and" \
                 "there is nothing to compare"
            return 0
        fi
        # Checked, because this runs where set -e does not end the script.
        if ! dk_build "$base" "$name" ||
           ! cp "$(dk_build_dir "$name")/zephyr/zephyr.hex" "$there_hex"
        then
            echo "error: $label did not build its $human image, so the images" \
                 "cannot be compared" >&2
            return 1
        fi
        if cmp -s "$here_hex" "$there_hex"; then
            echo "the $human image is the one $label builds, so $here still" \
                 "describes it"
            return 0
        fi
        # Before blaming the change: each side has to repeat itself, or the
        # difference is in the build and not in the sources.
        dk_image_repeats "$repo/$DK_FIRMWARE" "this tree" "$name" "$here_hex" \
            || return 1
        dk_image_repeats "$base" "$label" "$name" "$there_hex" || return 1
        echo "error: the $human image differs from the one $label builds while" \
             "UTDK_FW_VERSION is still $here: bump it in" \
             "$DK_FIRMWARE/src/roles.h and ubertooth_harness/targets.py, or a" \
             "board already running $here keeps the old image" >&2
        return 1
    }

    compare_dk_image() {
        base=$1
        label=$2
        step "the images against $label"
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
                 "ubertooth_harness/targets.py" >&2
            return 1
        fi
        # This tree's images, copied out of the build directories the base's
        # builds write into next.
        for name in default chansel1; do
            cp "$(dk_build_dir "$name")/zephyr/zephyr.hex" \
               "$workspace/utdk-$name-here.hex"
        done
        # Both images are compared. A board carries one or the other, and
        # `ensure` leaves either in place while the version is unchanged, so
        # a change that reaches only the algorithm 1 image needs the bump as
        # much as one that reaches the default image. The first image that
        # fails ends the step: the remedy is the same bump either way, since
        # one version string names both images, so building the second pair
        # would only cost time.
        for name in default chansel1; do
            compare_one_dk_image "$name" || return 1
        done
        # Each image is the one the base builds, so every build directory
        # holds this tree's image whichever build wrote it last. Leave the
        # sources in the workspace as this tree's.
        dk_sources "$repo/$DK_FIRMWARE"
    }

    step "build $DK_FIRMWARE for $NCS_BOARD"
    dk_build "$repo/$DK_FIRMWARE" default

    # The same check the harness makes before it flashes a board: an image
    # that does not carry the pinned version string would be left in place by
    # `nrfdk.py ensure` on a board that already runs the old one.
    step "the image carries the pinned firmware version"
    ls -l "$hex"
    # The harness package runs in its own Python 3.12 environment only, as
    # run_build does it.
    [ -x .venv/bin/python ] || "$PYTHON" bootstrap.py
    .venv/bin/python -m ubertooth_harness.nrfdk check-image "$hex"

    # The second image: the same roles on a controller without channel
    # selection algorithm 2. Its Kconfig fragment and devicetree overlay are
    # compiled nowhere else, so a change that breaks them would otherwise
    # reach the bench unnoticed. It is built here, before the comparison,
    # which needs this tree's copy of both images.
    chansel1_version=$(.venv/bin/python -c \
        'from ubertooth_harness import targets; print(targets.DK_FIRMWARE_CHAN_SEL_1)')
    step "build $DK_FIRMWARE for $NCS_BOARD without channel selection algorithm 2"
    dk_build "$repo/$DK_FIRMWARE" chansel1
    step "the second image carries $chansel1_version"
    ls -l "$chansel1_hex"
    .venv/bin/python -m ubertooth_harness.nrfdk check-image \
        --version "$chansel1_version" "$chansel1_hex"

    # The sources to compare the images against: a directory of them
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
        step "the images against another commit"
        echo "no base sources given (DK_BASE_REF or DK_BASE_SRC), so the images" \
             "are not compared and a missing version bump would not show up here"
    elif ! git -C "$repo" cat-file -e "$DK_BASE_REF:$DK_FIRMWARE/src/roles.h" 2>/dev/null; then
        step "the images against $DK_BASE_REF"
        echo "error: $DK_BASE_REF does not resolve to a tree holding" \
             "$DK_FIRMWARE here, so the images cannot be compared: the ref is" \
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
    # Only the part table check of the build, against UBERTOOTH_SOURCE (tests).
    dfu-part-table) check_dfu_part_table "${UBERTOOTH_SOURCE:?UBERTOOTH_SOURCE is required}" ;;
    firmware-dk) run_firmware_dk ;;
    all) run_test; run_build ;;
    *) echo "usage: scripts/ci.sh test|build|all|libbtbb-pin|dfu-part-table|firmware-dk" >&2; exit 2 ;;
esac
step "${1} passed"
