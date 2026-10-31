import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli, pytools
from ubertooth_harness.build import (
    BuildError, Builder, PYTHON_TOOLS, required_tools, resolve_python,
)
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan

from tests.test_build import (
    LIBBTBB, UBERTOOTH, FakeBuildExecutor, make_plan, publisher_in,
)

REPOSITORY = Path(__file__).resolve().parents[1]

# btle-extcap.py as at fork d04f79e (#342), trimmed to what the cases exercise.
EXTCAP_D04F79E = '''#!/usr/bin/env python3
import getopt, re, sys
from subprocess import Popen, PIPE

def with_optional_values(argv):
    out = []
    for i, word in enumerate(argv):
        if word == "--":
            out.extend(argv[i:]); break
        if word == "--extcap-version":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or nxt.startswith("-"):
                word += "="
        out.append(word)
    return out

def main():
    try:
        opts, args = getopt.getopt(with_optional_values(sys.argv[1:]), "h", ["help",
            "extcap-interfaces", "extcap-dlts", "extcap-config", "capture",
            "extcap-interface=", "fifo=", "channel=", "extcap-version=",
            "extcap-capture-filter="])
    except getopt.GetoptError as err:
        print("error: %s" % err, file=sys.stderr); usage(sys.stderr); sys.exit(1)
    if args:
        typed = sys.argv[1:][len(sys.argv[1:]) - len(args):]
        print("error: unexpected argument '%s'" % typed[0], file=sys.stderr)
        sys.exit(1)
    interface = ''
    fifo = None
    channel = "37"
    dlts = config = capture = False
    for o, a in opts:
        if o in ("-h", "--help"):
            usage(); sys.exit(0)
        elif o == "--extcap-interfaces":
            list_interfaces(); sys.exit(0)
        elif o == "--extcap-dlts":
            dlts = True
        elif o == "--extcap-config":
            config = True
        elif o == "--capture":
            capture = True
        elif o == "--extcap-interface":
            interface = a
        elif o == "--fifo":
            fifo = a
        elif o == "--channel":
            channel = a
    if not re.match(r'ubertooth([0-9a-fA-F]+)$', interface):
        print("error: --extcap-interface must be ubertooth<serial>, as "
              "--extcap-interfaces lists it", file=sys.stderr)
        sys.exit(1)
    if dlts:
        print("dlt {number=256}{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}{display=Bluetooth LE}\\n")
    elif config:
        print("arg {number=0}{call=--channel}{display=Advertising Channel}{type=selector}")
        for v, d in (("37", "true"), ("38", "false"), ("39", "false")):
            print("value {arg=0}{value=%s}{display=%s}{default=%s}" % (v, v, d))
    elif capture:
        if fifo is None:
            print("error: --capture needs --fifo=PATH", file=sys.stderr)
            sys.exit(1)
        if channel not in ("37", "38", "39"):
            print("error: advertising channel index must be 37-39 (got %s)" % channel,
                  file=sys.stderr)
            sys.exit(1)
        sys.exit(3)  # a capture would run ubertooth-btle; no case gets here

def usage(out=sys.stdout):
    print("Usage: %s --extcap-interfaces |"
          " --extcap-interface=ubertooth<serial>"
          " (--extcap-dlts | --extcap-config |"
          " --capture --fifo=PATH [--channel=37|38|39])" % sys.argv[0], file=out)

def list_interfaces():
    out = Popen(['ubertooth-util', '-U', 'all', '-s'], stdout=PIPE, stderr=PIPE,
                universal_newlines=True).communicate()[0]
    for line in out.split('\\n'):
        m = re.match(r'(\\d+): ([0-9a-fA-F]+)$', line.strip())
        if m:
            print("interface {value=ubertooth%s}{display=Ubertooth One %s}"
                  % (m.group(2), m.group(2)))

main()
'''
# btle-extcap.py as at fork 71e1a8a, trimmed to what the cases exercise:
# before fork #342 (no Wireshark options, no stray-word or channel check).
EXTCAP_71E1A8A = '''#!/usr/bin/env python3
import getopt, re, sys
from subprocess import Popen, PIPE

def main():
    try:
        opts, args = getopt.getopt(sys.argv[1:], "h", ["help", "extcap-interfaces",
            "extcap-dlts", "extcap-config", "capture", "extcap-interface=",
            "fifo=", "channel="])
    except getopt.GetoptError as err:
        print("error: %s" % err, file=sys.stderr); usage(sys.stderr); sys.exit(1)
    interface = ''
    fifo = None
    dlts = config = capture = False
    for o, a in opts:
        if o in ("-h", "--help"):
            usage(); sys.exit()
        elif o == "--extcap-interfaces":
            list_interfaces(); exit(0)
        elif o == "--extcap-dlts":
            dlts = True
        elif o == "--extcap-config":
            config = True
        elif o == "--capture":
            capture = True
        elif o == "--extcap-interface":
            interface = a
        elif o == "--fifo":
            fifo = a
    if not re.match(r'ubertooth([0-9a-fA-F]+)$', interface):
        print("error: --extcap-interface must be ubertooth<serial>, as "
              "--extcap-interfaces lists it", file=sys.stderr)
        exit(1)
    if dlts:
        print("dlt {number=256}{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}{display=Bluetooth LE}\\n")
    elif config:
        print("arg {number=0}{call=--channel}{display=Advertising Channel}{type=selector}")
        for v, d in (("37", "true"), ("38", "false"), ("39", "false")):
            print("value {arg=0}{value=%s}{display=%s}{default=%s}" % (v, v, d))
    elif capture and fifo is None:
        print("error: --capture needs --fifo=PATH", file=sys.stderr)
        exit(1)

def usage(out=sys.stdout):
    print("Usage: %s --extcap-interfaces |"
          " --extcap-interface=ubertooth<serial>"
          " (--extcap-dlts | --extcap-config |"
          " --capture --fifo=PATH [--channel=37|38|39])" % sys.argv[0], file=out)

def list_interfaces():
    out = Popen(['ubertooth-util', '-U', 'all', '-s'], stdout=PIPE,
                universal_newlines=True).communicate()[0]
    for line in out.split('\\n'):
        m = re.match(r'(\\d+): ([0-9a-fA-F]+)$', line.strip())
        if m:
            print("interface {value=ubertooth%s}{display=Ubertooth One %s}"
                  % (m.group(2), m.group(2)))

main()
'''
# The listing as at fork 6fee30c (SqueakyFloors/ubertooth#269): the index,
# not the serial, with two or more boards.
OLD_LISTING = '''
def list_interfaces():
    out = Popen(['ubertooth-util', '-U', 'all', '-s'], stdout=PIPE,
                universal_newlines=True).communicate()[0]
    found = [line.split()[0] for line in out.split('\\n') if line.strip()]
    for i in range(len(found)):
        print("interface {value=ubertooth%d}{display=Ubertooth One %s}" % (i, found[i]))

main()
'''

# Failed at fork d04f79e (SqueakyFloors/ubertooth#362, fixed in 779f4f7).
UTIL_FAILS_EXTCAP = {"extcap-interfaces-util-fails"}
# The cases for an option the script does not have. At d04f79e each one got
# getopt's own words ("error: option --extcap-bogus not recognized"), which
# fork 5310a5d (#481) rewrote to the line every tool gives for that mistake,
# and the usage text after it, which fork 12211ce (#406) dropped. The real
# report_bad_option() tells five mistakes apart; the fixture below stands in
# for the one these cases make, a long option no LONG_OPTIONS entry begins
# with, and would answer the other four in the same words.
BAD_OPTION_EXTCAP = {
    "extcap-unknown-option", "extcap-refuses-extcap-reload-option",
    "extcap-refuses-extcap-control-in", "extcap-refuses-extcap-control-out",
    "extcap-refuses-extcap-config-option-name",
}
# btle-extcap.py as on fork main since 5310a5d: ubertooth-util's failure is
# kept (779f4f7, #374), a described error prints no usage text (12211ce), and
# an option the script does not have is named in the C tools' wording.
EXTCAP = EXTCAP_D04F79E.replace(
    """    out = Popen(['ubertooth-util', '-U', 'all', '-s'], stdout=PIPE, stderr=PIPE,
                universal_newlines=True).communicate()[0]
""", """    proc = Popen(['ubertooth-util', '-U', 'all', '-s'], stdout=PIPE,
                 universal_newlines=True)
    out = proc.communicate()[0]
    if proc.returncode != 0:
        sys.exit(1)
""").replace(
    """        print("error: %s" % err, file=sys.stderr); usage(sys.stderr); sys.exit(1)""",
    """        print("error: unrecognized option '--%s'" % err.opt, file=sys.stderr)
        sys.exit(1)""")
assert EXTCAP.count("sys.exit(1)") > EXTCAP_D04F79E.count("sys.exit(1)")
assert "usage(sys.stderr)" not in EXTCAP
# Each replacement found its text: a fixture whose derivation stopped matching
# would be the d04f79e script under the current script's name.
assert "unrecognized option '--%s'" in EXTCAP
assert "unrecognized option" not in EXTCAP_D04F79E
COMPREHENSIVE_INTERFACES = (
    "extcap-interfaces", "extcap-interfaces-serials", "extcap-interfaces-version",
    "extcap-interfaces-version-word", "extcap-interfaces-stray-word",
)
SERIAL_A = "1ff0ff1967102caf8334fa5ac52000f5"
SERIAL_B = "0b700017490c2cafa039ee5ac52000f5"


def git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(("git", *args), cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeFork:
    """A git repository holding btle-extcap.py and a fake ubertooth-util."""

    def __init__(self, directory, util_output, extcap=EXTCAP):
        self.root = Path(directory)
        script = self.root / "src" / pytools.EXTCAP_PATH
        script.parent.mkdir(parents=True)
        script.write_text(extcap)
        git("init", "-q", cwd=self.root / "src")
        git("add", ".", cwd=self.root / "src")
        git("commit", "-q", "-m", "fixture", cwd=self.root / "src")
        self.commit = git("rev-parse", "HEAD", cwd=self.root / "src")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        util = bin_dir / "ubertooth-util"
        util.write_text("#!/bin/sh\nprintf '%s'\n" % util_output.replace("\n", "\\n"))
        util.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            pytools.SOURCE_ENV: str(self.root / "src"),
            pytools.COMMIT_ENV: self.commit,
        }


def run_case(case, env):
    result = subprocess.run(case.argv, env=env, capture_output=True, text=True, timeout=60)
    command = CommandResult(case.argv, result.returncode, result.stdout, result.stderr, 0.1, False)
    return case.missing_tool(command), case.result_failure(command)


class PythonToolCaseTests(unittest.TestCase):

    def cases(self, coverage):
        definition = builtin_catalog().get("Python tools")
        self.assertEqual({}, dict(definition.hardware_roles))
        return {case.case_id: case for case in definition.command_cases(
            definition.normalize_arguments({"coverage": coverage}), {}, {})}

    def test_smoke_and_comprehensive_case_sets(self):
        smoke = self.cases("smoke")
        self.assertEqual(
            ["specan-ui-help", "extcap-help", "extcap-dlts", "extcap-config"], list(smoke))
        comprehensive = self.cases("comprehensive")
        self.assertLess(set(smoke), set(comprehensive))
        self.assertIn("extcap-unknown-option", comprehensive)
        for case in comprehensive.values():
            self.assertEqual(sys.executable, case.argv[0])
            self.assertEqual(pytools.SKIP_EXIT, case.skip_returncode)

    def test_extcap_cases_against_the_fork_script(self):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, f"0: {SERIAL_A}\n")
            results = {case_id: run_case(case, fork.env)
                       for case_id, case in self.cases("comprehensive").items()
                       if case_id.startswith("extcap")}
        failing = {case_id for case_id, (skip, failure) in results.items() if failure}
        self.assertEqual(set(), failing)
        self.assertTrue(all(not skip for skip, _failure in results.values()))

    def test_the_util_and_bad_option_cases_fail_at_d04f79e(self):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, f"0: {SERIAL_A}\n", EXTCAP_D04F79E)
            failing = {case_id for case_id, case in self.cases("comprehensive").items()
                       if case_id.startswith("extcap") and run_case(case, fork.env)[1]}
        self.assertEqual(UTIL_FAILS_EXTCAP | BAD_OPTION_EXTCAP, failing)

    def test_the_util_case_passes_once_a_failing_util_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, f"0: {SERIAL_A}\n", EXTCAP)
            case = self.cases("comprehensive")["extcap-interfaces-util-fails"]
            self.assertEqual(("", ""), run_case(case, fork.env))
            # The stand-in is used only by this case: the listing still works.
            listing = self.cases("smoke")["extcap-dlts"]
            self.assertEqual(("", ""), run_case(listing, fork.env))

    def test_the_fork_342_cases_fail_before_it(self):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, f"0: {SERIAL_A}\n", EXTCAP_71E1A8A)
            failing = {case_id for case_id, case in self.cases("comprehensive").items()
                       if case_id.startswith("extcap") and run_case(case, fork.env)[1]}
        self.assertEqual({
            "extcap-version-attached", "extcap-version-next-word", "extcap-version-bare",
            "extcap-stray-word", "extcap-help-stray-word", "extcap-capture-channel-40",
        } | UTIL_FAILS_EXTCAP | BAD_OPTION_EXTCAP, failing)

    def test_extcap_without_the_source_fails_rather_than_skips(self):
        case = self.cases("smoke")["extcap-dlts"]
        skip, failure = run_case(case, {"PATH": os.environ["PATH"]})
        self.assertEqual("", skip)
        self.assertIn("exited 2", failure)

    def test_specan_ui_skips_without_a_python_build(self):
        case = self.cases("smoke")["specan-ui-help"]
        skip, _failure = run_case(case, {"PATH": os.environ["PATH"]})
        self.assertIn("skipped, ubertooth-specan-ui is built only by run-suite "
                      "--build --with-python", skip)

    def test_specan_ui_skips_when_numpy_or_qtpy_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text(
                "#!/bin/sh\necho \"ModuleNotFoundError: No module named 'numpy'\" >&2\nexit 1\n")
            python.chmod(0o755)
            case = self.cases("smoke")["specan-ui-help"]
            skip, _failure = run_case(case, {
                "PATH": os.environ["PATH"], pytools.PYTHON_ENV: str(python),
                pytools.PYTHON_TOOLS_ENV: directory,
            })
        self.assertIn("cannot import numpy, qtpy and a Qt binding", skip)
        self.assertIn("No module named 'numpy'", skip)

    def test_specan_ui_help_runs_the_wrapper_without_a_display(self):
        with tempfile.TemporaryDirectory() as directory:
            python = Path(directory) / "python"
            python.write_text("#!/bin/sh\necho numpy 1 qtpy 2 PySide6\n")
            python.chmod(0o755)
            wrapper = Path(directory) / "ubertooth-specan-ui"
            wrapper.write_text(
                "#!/bin/sh\n[ -z \"$DISPLAY$WAYLAND_DISPLAY\" ] || exit 9\n"
                "echo 'usage: ubertooth-specan-ui [-h] [-U index|serial] [-l LOWER_FREQ]'\n"
                "echo '  [-u UPPER_FREQ] [--wifi [channel(s)]] [--padding PADDING]'\n")
            wrapper.chmod(0o755)
            case = self.cases("smoke")["specan-ui-help"]
            skip, failure = run_case(case, {
                "PATH": os.environ["PATH"], "DISPLAY": ":0",
                pytools.PYTHON_ENV: str(python), pytools.PYTHON_TOOLS_ENV: directory,
            })
        self.assertEqual(("", ""), (skip, failure))


class ExtcapInterfaceCaseTests(unittest.TestCase):

    def cases(self, coverage, serials):
        definition = builtin_catalog().get("BLE extcap interfaces")
        self.assertTrue(definition.guards_other_ubertooths)
        identities = {"Ubertooth": {"serial": serials[0].upper()}}
        identities.update({f"guard Ubertooth {i}": {"serial": serial}
                           for i, serial in enumerate(serials[1:], 1)})
        return {case.case_id: case for case in definition.command_cases(
            definition.normalize_arguments({"coverage": coverage}), {}, identities)}

    def run_all(self, util_output, serials, extcap=EXTCAP, coverage="comprehensive"):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, util_output, extcap)
            return {case_id: run_case(case, fork.env)[1]
                    for case_id, case in self.cases(coverage, serials).items()}

    def test_one_and_two_boards_pass(self):
        passed = dict.fromkeys(COMPREHENSIVE_INTERFACES, "")
        self.assertEqual(passed, self.run_all(f"0: {SERIAL_A}\n", [SERIAL_A]))
        # Registry order need not be the USB index order.
        self.assertEqual(
            passed, self.run_all(f"0: {SERIAL_B}\n1: {SERIAL_A}\n", [SERIAL_A, SERIAL_B]))

    def test_wireshark_options_fail_before_fork_342(self):
        failures = self.run_all(f"0: {SERIAL_A}\n", [SERIAL_A], EXTCAP_71E1A8A)
        self.assertEqual(
            {"extcap-interfaces-version", "extcap-interfaces-version-word",
             "extcap-interfaces-stray-word"},
            {case_id for case_id, failure in failures.items() if failure})

    def test_the_old_index_listing_fails(self):
        extcap = EXTCAP[:EXTCAP.index("def list_interfaces")] + OLD_LISTING.lstrip()
        failures = self.run_all(f"0: {SERIAL_A}\n1: {SERIAL_B}\n", [SERIAL_A, SERIAL_B],
                                extcap)
        self.assertIn("expected output pattern", failures["extcap-interfaces"])
        self.assertIn("expected output pattern", failures["extcap-interfaces-serials"])

    def test_listing_count_must_match_the_registered_boards(self):
        failures = self.run_all(f"0: {SERIAL_A}\n", [SERIAL_A, SERIAL_B])
        self.assertIn("expected output pattern", failures["extcap-interfaces"])
        failures = self.run_all(f"0: {SERIAL_A}\n1: {SERIAL_B}\n", [SERIAL_A])
        self.assertIn("expected output pattern", failures["extcap-interfaces"])

    def test_an_unreadable_serial_is_left_out(self):
        failures = self.run_all(f"0: {SERIAL_A}\n1: <unavailable>\n", [SERIAL_A])
        self.assertEqual("", failures["extcap-interfaces"])

    def test_case_sets(self):
        self.assertEqual(list(COMPREHENSIVE_INTERFACES[:3]),
                         list(self.cases("smoke", [SERIAL_A])))
        self.assertEqual(list(COMPREHENSIVE_INTERFACES),
                         list(self.cases("comprehensive", [SERIAL_A])))


class SkipReturncodeTests(unittest.TestCase):

    def test_skip_needs_the_exit_status_and_a_skip_line(self):
        case = builtin_catalog().get("Python tools").command_cases(
            {"coverage": "smoke"}, {}, {})[0]

        def result(code, stdout):
            return CommandResult(case.argv, code, stdout, "", 0.1, False)

        self.assertIn("skipped, why", case.missing_tool(result(77, "SKIP: why\n")))
        self.assertEqual("", case.missing_tool(result(77, "no reason\n")))
        self.assertEqual("", case.missing_tool(result(1, "SKIP: why\n")))
        self.assertIn("exited 77", case.result_failure(result(77, "no reason\n")))


class PythonBuildTests(unittest.TestCase):

    def python_plan(self, work):
        from ubertooth_harness.build import plan_build
        return plan_build(
            ubertooth=UBERTOOTH, libbtbb=LIBBTBB, work_dir=Path(work), platform="linux",
            jobs=4, python="/opt/py/bin/python3",
        )

    def run_build(self, plan, interpreter="/opt/py/bin/python3", tools=PYTHON_TOOLS):
        # Every C tool the plan needs (the BlueZ tools on Linux); the Python
        # tools are written below, for ``interpreter``.
        executor = FakeBuildExecutor(plan, installed_tools=tuple(
            tool for tool in required_tools(plan) if tool not in PYTHON_TOOLS))
        original = executor.run

        def run(argv, **options):
            if tuple(argv)[0] == "/opt/py/bin/python3":
                return CommandResult(tuple(argv), 0, "Python 3.12.6 /opt/py/bin/python3\n",
                                     "", 0.01, False)
            result = original(argv, **options)
            if tuple(argv)[:2] == ("cmake", "--install") and "ubertooth-host" in argv[-1]:
                for tool in tools:
                    (plan.bin_dir / tool).write_text(f"#!{interpreter}\nimport os\n")
            return result

        executor.run = run
        return Builder(executor, publisher_in(plan.work_dir), run_dir=plan.work_dir).run(plan)

    def test_default_build_keeps_python_off(self):
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            self.assertIn("-DENABLE_PYTHON=OFF", plan.host_cmake_flags)
            self.assertIsNone(plan.python)
            env = plan.environment({"PATH": "/usr/bin", pytools.PYTHON_ENV: "/x"})
            self.assertNotIn(pytools.PYTHON_ENV, env)

    def test_with_python_builds_for_that_interpreter_and_records_it(self):
        with tempfile.TemporaryDirectory() as work:
            plan = self.python_plan(work)
            self.assertIn("-DENABLE_PYTHON=ON", plan.host_cmake_flags)
            self.assertIn("-DPython3_EXECUTABLE=/opt/py/bin/python3", plan.host_cmake_flags)
            self.assertNotIn("-DENABLE_PYTHON=OFF", plan.host_cmake_flags)
            result = self.run_build(plan)
            manifest = result.to_manifest()
            self.assertEqual("/opt/py/bin/python3", manifest["python"])
            self.assertEqual("Python 3.12.6 /opt/py/bin/python3", manifest["toolchain"]["python"])
            env = plan.environment({"PATH": "/usr/bin"})
            self.assertEqual("/opt/py/bin/python3", env[pytools.PYTHON_ENV])
            self.assertEqual(str(plan.bin_dir), env[pytools.PYTHON_TOOLS_ENV])
            names = {item.name for item in result.artifacts}
            self.assertIn("built ubertooth-specan-ui", names)

    def test_python_tools_must_be_installed_for_that_interpreter(self):
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(BuildError, "did not install: ubertooth-btbr"):
                self.run_build(self.python_plan(work), tools=("ubertooth-specan-ui",))
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaisesRegex(BuildError, "not the --with-python interpreter"):
                self.run_build(self.python_plan(work), interpreter="/usr/bin/python3")

    def test_resolve_python_keeps_symlinks_and_names_a_missing_one(self):
        self.assertEqual("/venv/bin/python", resolve_python("python", lambda _n: "/venv/bin/python"))
        with self.assertRaisesRegex(BuildError, "no Python interpreter 'nope'"):
            resolve_python("nope", lambda _n: None)

    def test_with_python_requires_build(self):
        argv = ["run-suite", "--plan", "testplans/fast.yaml", "--suite", "fast",
                "--hardware", "hardware.example.yaml", "--host", "Host_Mac",
                "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
                "--output", "/tmp/never-created", "--test", "Host CLI surface",
                "--with-python"]
        with mock.patch.object(cli, "_detected_platform", return_value="macos"), \
                mock.patch.object(cli, "_resolve_sources", return_value=(UBERTOOTH,)), \
                mock.patch("sys.stderr") as stderr:
            code = cli.main(argv)
        self.assertEqual(2, code)
        self.assertIn("--with-python requires --build",
                      "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_test_environment_names_the_resolved_commit(self):
        with mock.patch.dict(os.environ, {pytools.PYTHON_ENV: "/stale"}):
            env = cli._test_environment((UBERTOOTH, LIBBTBB), None)
        self.assertEqual(UBERTOOTH.source_path, env[pytools.SOURCE_ENV])
        self.assertEqual(UBERTOOTH.commit, env[pytools.COMMIT_ENV])
        self.assertNotIn(pytools.PYTHON_ENV, env)
        with tempfile.TemporaryDirectory() as work:
            plan = make_plan(work)
            env = cli._test_environment((UBERTOOTH, LIBBTBB), plan)
        self.assertEqual(str(plan.work_dir / "src" / "ubertooth"), env[pytools.SOURCE_ENV])
        self.assertTrue(env["PATH"].startswith(str(plan.bin_dir)))


class PlanTests(unittest.TestCase):

    def test_plans_use_the_python_tests(self):
        catalog = builtin_catalog()
        expected = {"fast.yaml": "smoke", "host.yaml": "smoke", "release.yaml": "comprehensive"}
        for filename, profile in expected.items():
            plan = load_plan(REPOSITORY / "testplans" / filename, catalog)
            entries = [entry for suite in plan.suites.values() for entry in suite
                       if entry.test_name == "Python tools"]
            self.assertEqual([profile], [entry.arguments["coverage"] for entry in entries],
                             filename)

    def test_case_ids_are_valid(self):
        for case in PythonToolCaseTests().cases("comprehensive").values():
            self.assertRegex(case.case_id, re.compile(r"^[a-z0-9][a-z0-9.-]*$"))


if __name__ == "__main__":
    unittest.main()
