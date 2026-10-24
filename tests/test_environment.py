import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ubertooth_harness import cli, environment
from ubertooth_harness.environment import (
    HarnessEnvironmentError,
    capture,
    require_harness_environment,
)


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap", PROJECT / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def identity(**python):
    base = {
        "version": "3.12.6",
        "implementation": "CPython",
        "executable": "/h/.venv/bin/python",
        "prefix": "/h/.venv",
        "virtual_environment": True,
        "harness_environment": True,
    }
    base.update(python)
    return {
        "python": base,
        "pip": "24.2",
        "dependencies": {"PyYAML": "6.0.3", "pyserial": "3.5"},
        "os": {"system": "Linux", "release": "6.8", "architecture": "x86_64"},
    }


class EnvironmentIdentityTests(unittest.TestCase):

    def test_capture_records_python_pip_dependencies_os_and_architecture(self):
        current = capture()

        self.assertEqual(
            {"python", "pip", "dependencies", "os"}, set(current)
        )
        self.assertEqual(sys.executable, current["python"]["executable"])
        self.assertEqual(
            set(environment.requirement_names()), set(current["dependencies"])
        )
        self.assertTrue(current["os"]["architecture"])
        json.dumps(current)

    def test_requirement_names_ignore_comments_and_specifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.txt"
            path.write_text(
                "# pins\n-r other.txt\n--only-binary :all:\n"
                "PyYAML==6.0.3\npyserial==3.5  # driver\n\n"
            )

            self.assertEqual(("PyYAML", "pyserial"), environment.requirement_names(path))

    def test_harness_environment_is_required(self):
        require_harness_environment(identity())
        for bad, message in (
            (identity(harness_environment=False, executable="/usr/bin/python3"), "not the harness environment"),
            (identity(version="3.11.9"), "Python 3.11.9"),
        ):
            with self.subTest(message):
                with self.assertRaisesRegex(HarnessEnvironmentError, message):
                    require_harness_environment(bad)
        missing = identity()
        missing["dependencies"]["pyserial"] = None
        with self.assertRaisesRegex(HarnessEnvironmentError, "missing pyserial"):
            require_harness_environment(missing)

    def test_hardware_commands_refuse_a_foreign_interpreter(self):
        foreign = identity(harness_environment=False, executable="/opt/glados/bin/python")
        with mock.patch.object(environment, "capture", return_value=foreign):
            for argv in (
                ["run-suite", "--plan", "p", "--suite", "s", "--hardware", "h",
                 "--host", "H", "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
                 "--output", "o"],
                ["build", "--ubertooth-source", ".", "--ubertooth-ref", "HEAD",
                 "--libbtbb-source", ".", "--libbtbb-ref", "HEAD", "--output", "o"],
            ):
                with self.subTest(argv[0]):
                    with mock.patch("sys.stderr"):
                        self.assertEqual(2, cli.main(argv))

    def test_manifest_records_the_environment(self):
        from ubertooth_harness.manifest import MANIFEST_SCHEMA_VERSION
        from tests.test_sequencer import run_contract
        from ubertooth_harness.planner import TestInvocation

        with tempfile.TemporaryDirectory() as directory:
            invocation = TestInvocation("001-Host version", "Host version", "Host_Mac", {}, {})
            manifest, _checks = run_contract(Path(directory), invocation)
            path = Path(directory) / "manifest.json"
            manifest.write_atomic(path)
            recorded = json.loads(path.read_text())

        self.assertEqual(4, MANIFEST_SCHEMA_VERSION)
        self.assertEqual(MANIFEST_SCHEMA_VERSION, recorded["schema_version"])
        self.assertEqual(capture(), recorded["environment"])


class BootstrapTests(unittest.TestCase):

    def test_finds_only_python_3_12(self):
        with mock.patch.object(bootstrap, "probe", return_value={"version": [3, 11, 9]}):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "3.11.9; Python 3.12"):
                bootstrap.find_base_python("/usr/bin/python3.11")
        with mock.patch.object(bootstrap.sys, "version_info", (3, 11, 9)), \
                mock.patch.object(bootstrap.shutil, "which", return_value=None):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "Python 3.12 was not found"):
                bootstrap.find_base_python(None)

    def test_non_python_interpreter_is_an_explicit_error(self):
        for stdout in ("", "not json", '{"version": "x"}'):
            ran = mock.Mock(returncode=0, stdout=stdout, stderr="")
            with self.subTest(stdout=stdout), mock.patch.object(bootstrap, "_run", return_value=ran):
                with self.assertRaisesRegex(bootstrap.BootstrapError, "not a working Python"):
                    bootstrap.probe("/usr/bin/true")

    def test_old_launchers_reach_the_explicit_errors(self):
        import subprocess

        launcher = "/usr/bin/python3"
        if not Path(launcher).exists():
            self.skipTest("no system python3")
        result = subprocess.run(
            [launcher, str(PROJECT / "bootstrap.py"), "--python", launcher,
             "--venv", str(Path(tempfile.gettempdir()) / "never-created")],
            capture_output=True, text=True, timeout=60,
        )
        if "3.12" in subprocess.run([launcher, "-V"], capture_output=True, text=True).stdout:
            self.skipTest("system python3 is already 3.12")
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertIn("Python 3.12 is required", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_module_entry_point_rejects_other_python_versions(self):
        import subprocess

        launcher = "/usr/bin/python3"
        version = subprocess.run([launcher, "-V"], capture_output=True, text=True).stdout if Path(launcher).exists() else ""
        if not version or "3.12" in version:
            self.skipTest("no non-3.12 system python3")
        result = subprocess.run(
            [launcher, "-m", "ubertooth_harness", "validate-plan"],
            capture_output=True, text=True, timeout=60, cwd=PROJECT,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("needs its own Python 3.12 environment", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_venv_support_is_explicit(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="No module named ensurepip")
        with mock.patch.object(bootstrap, "_run", return_value=failed):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "python3.12-venv"):
                bootstrap.check_venv_support("python3.12")

    def test_refuses_to_adopt_or_delete_a_foreign_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "glados-venv"
            foreign.mkdir()
            (foreign / "pyvenv.cfg").write_text("home = /usr/bin\n")
            with mock.patch.object(bootstrap, "_run") as run:
                for recreate in (False, True):
                    with self.subTest(recreate=recreate):
                        with self.assertRaisesRegex(bootstrap.BootstrapError, "not a harness environment"):
                            bootstrap.ensure_venv(foreign, "python3.12", recreate=recreate)
                run.assert_not_called()
            self.assertTrue((foreign / "pyvenv.cfg").exists())

    def test_reuses_an_existing_harness_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            (venv / "bin").mkdir(parents=True)
            (venv / bootstrap.MARKER).write_text("{}\n")
            (venv / "bin" / "python").write_text("")
            with mock.patch.object(bootstrap, "probe", return_value={"version": [3, 12, 6]}), \
                    mock.patch.object(bootstrap, "_run") as run:
                self.assertFalse(bootstrap.ensure_venv(venv, "python3.12", recreate=False))
                run.assert_not_called()
            with mock.patch.object(bootstrap, "probe", return_value={"version": [3, 11, 9]}):
                with self.assertRaisesRegex(bootstrap.BootstrapError, "--recreate"):
                    bootstrap.ensure_venv(venv, "python3.12", recreate=False)

    def test_installs_only_with_the_environment_interpreter_and_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            commands = []

            def run(argv, *, env=None, timeout=None, capture=True):
                commands.append((argv, env))
                return mock.Mock(returncode=0)

            with mock.patch.object(bootstrap, "probe", return_value={"prefix": str(venv)}), \
                    mock.patch.object(bootstrap, "_run", side_effect=run):
                bootstrap.install_requirements(venv, PROJECT / "requirements.txt")

            self.assertEqual(2, len(commands))
            for argv, env in commands:
                self.assertEqual(bootstrap.venv_python(venv), argv[0])
                self.assertEqual("1", env["PIP_REQUIRE_VIRTUALENV"])
                self.assertEqual("0", env["PIP_USER"])
                self.assertEqual("1", env["PYTHONNOUSERSITE"])
                self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(["-m", "pip", "check"], [str(a) for a in commands[1][0][1:]])

    def test_refuses_to_install_into_an_interpreter_outside_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / ".venv"
            with mock.patch.object(bootstrap, "probe", return_value={"prefix": "/usr"}), \
                    mock.patch.object(bootstrap, "_run") as run:
                with self.assertRaisesRegex(bootstrap.BootstrapError, "refusing to install"):
                    bootstrap.install_requirements(venv, PROJECT / "requirements.txt")
                run.assert_not_called()

    def test_bootstrap_is_standard_library_only(self):
        import ast

        tree = ast.parse((PROJECT / "bootstrap.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])

        self.assertTrue(imported <= set(sys.stdlib_module_names), imported)

    def test_marker_names_match(self):
        self.assertEqual(bootstrap.MARKER, environment.MARKER)


if __name__ == "__main__":
    unittest.main()
