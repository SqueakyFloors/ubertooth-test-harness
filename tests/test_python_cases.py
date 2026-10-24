import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli, pytools
from ubertooth_harness.build import BuildError, Builder, PYTHON_TOOLS, resolve_python
from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.executor import CommandResult
from ubertooth_harness.plan import load_plan

from tests.test_build import (
    LIBBTBB, UBERTOOTH, BUILT_TOOLS, FakeBuildExecutor, make_plan, publisher_in,
)

REPOSITORY = Path(__file__).resolve().parents[1]

# btle-extcap.py as at fork 6fee30c, trimmed to what the cases exercise.
EXTCAP = '''#!/usr/bin/env python3
import getopt, re, sys
from subprocess import Popen, PIPE

def main():
    try:
        opts, args = getopt.getopt(sys.argv[1:], "h", ["help", "extcap-interfaces",
            "extcap-dlts", "extcap-config", "capture", "extcap-interface=",
            "fifo=", "channel="])
    except getopt.GetoptError as err:
        print(str(err)); usage(); sys.exit(2)
    interface = ''
    dlts = config = False
    for o, a in opts:
        if o in ("-h", "--help"):
            usage(); sys.exit()
        elif o == "--extcap-interfaces":
            list_interfaces(); exit(0)
        elif o == "--extcap-dlts":
            dlts = True
        elif o == "--extcap-config":
            config = True
        elif o == "--extcap-interface":
            interface = a
    if not re.match(r'ubertooth(\\d+)', interface):
        exit(1)
    if dlts:
        print("dlt {number=256}{name=DLT_BLUETOOTH_LE_LL_WITH_PHDR}{display=Bluetooth LE}\\n")
    elif config:
        print("arg {number=0}{call=--channel}{display=Advertising Channel}{type=selector}")
        for v, d in (("37", "true"), ("38", "false"), ("39", "false")):
            print("value {arg=0}{value=%s}{display=%s}{default=%s}" % (v, v, d))

def usage():
    print("Usage: %s <--list-interfaces | --list-dlts | --config | --capture>" % sys.argv[0])

def list_interfaces():
    out = Popen(['ubertooth-util', '-s'], stdout=PIPE, universal_newlines=True).communicate()[0]
    found = []
    for line in out.split('\\n'):
        p = line.split()
        if len(p) == 0 or p[0] == 'ubertooth-util':
            break
        if p[0] == 'Serial':
            found.append(p[2])
        elif re.match('[0-9a-f]+', p[0]):
            found.append(p[0])
    for i in range(len(found)):
        print("interface {value=ubertooth%d}{display=Ubertooth One %s}" % (i, found[i]))

main()
'''

SERIAL_A = "1ff0ff1967102caf8334fa5ac52000f5"
SERIAL_B = "0b700017490c2cafa039ee5ac52000f5"


def git(*args, cwd):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(("git", *args), cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeFork:
    """A git repository holding btle-extcap.py and a fake ubertooth-util."""

    def __init__(self, directory, util_output):
        self.root = Path(directory)
        script = self.root / "src" / pytools.EXTCAP_PATH
        script.parent.mkdir(parents=True)
        script.write_text(EXTCAP)
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
        self.assertIn("extcap-help-options", comprehensive)
        for case in comprehensive.values():
            self.assertEqual(sys.executable, case.argv[0])
            self.assertEqual(pytools.SKIP_EXIT, case.skip_returncode)

    def test_extcap_cases_against_the_fork_script(self):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, f"Serial No: {SERIAL_A}\n")
            results = {case_id: run_case(case, fork.env)
                       for case_id, case in self.cases("comprehensive").items()
                       if case_id.startswith("extcap")}
        failing = {case_id for case_id, (skip, failure) in results.items() if failure}
        self.assertEqual({"extcap-help-options"}, failing)
        self.assertTrue(all(not skip for skip, _failure in results.values()))

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

    def run_all(self, util_output, serials):
        with tempfile.TemporaryDirectory() as directory:
            fork = FakeFork(directory, util_output)
            return {case_id: run_case(case, fork.env)[1]
                    for case_id, case in self.cases("comprehensive", serials).items()}

    def test_one_board_passes_both_cases(self):
        self.assertEqual(
            {"extcap-interfaces": "", "extcap-interfaces-serials": ""},
            self.run_all(f"Serial No: {SERIAL_A}\n", [SERIAL_A]))

    def test_two_boards_show_the_index_strict_case_fails(self):
        failures = self.run_all(f"0: {SERIAL_A}\n1: {SERIAL_B}\n", [SERIAL_A, SERIAL_B])
        self.assertEqual("", failures["extcap-interfaces"])
        self.assertIn("expected output pattern", failures["extcap-interfaces-serials"])

    def test_listing_count_must_match_the_registered_boards(self):
        failures = self.run_all(f"Serial No: {SERIAL_A}\n", [SERIAL_A, SERIAL_B])
        self.assertIn("expected output pattern", failures["extcap-interfaces"])
        failures = self.run_all(f"0: {SERIAL_A}\n1: {SERIAL_B}\n", [SERIAL_A])
        self.assertIn("expected output pattern", failures["extcap-interfaces"])

    def test_smoke_has_only_the_listing_case(self):
        self.assertEqual(["extcap-interfaces"], list(self.cases("smoke", [SERIAL_A])))


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
        executor = FakeBuildExecutor(plan, installed_tools=BUILT_TOOLS)
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
        return Builder(executor, publisher_in(plan.work_dir)).run(plan)

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
