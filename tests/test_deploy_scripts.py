"""Lab deploy scripts: scripts/deploy.sh, install-desktop-shortcut.sh, harness-open.

Each script runs against a temporary HOME and fake systemctl, loginctl,
launchctl, defaults, sudo, python3, git, uname, gio, ssh and browser commands
on PATH, so no test touches systemd, launchd, the network, or the real home
directory. The macOS deploy is driven by a fake `uname`, so these tests run on
either platform.
"""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
DEPLOY = SCRIPTS / "deploy.sh"
SHORTCUT = SCRIPTS / "install-desktop-shortcut.sh"
HARNESS_OPEN = SCRIPTS / "harness-open"
# Sourced by deploy.sh and harness-open, not run on its own.
CONFIG_LIB = SCRIPTS / "harness-config.sh"
ALL_SCRIPTS = (DEPLOY, SHORTCUT, HARNESS_OPEN)
SHELL_FILES = ALL_SCRIPTS + (CONFIG_LIB,)
CONTROLLER_UNIT = "ubertooth-harness-controller.service"
RUNNER_UNIT = "ubertooth-harness-runner.service"
RUNNER_LABEL = "com.ubertooth-harness.runner"
TUNNEL_LABEL = "com.ubertooth-harness.tunnel"
RUNNER_AGENT = f"{RUNNER_LABEL}.plist"
TUNNEL_AGENT = f"{TUNNEL_LABEL}.plist"
USER = pwd.getpwuid(os.getuid()).pw_name
# Placeholders: no real lab host or SSH alias belongs in this repository.
LAB_HOST = "lab-host.example"
JUMP_HOST = "jump-host.example"

# Each fake logs "<name> <args>" to $FAKE_LOG.
LOGGING_FAKE = '#!/bin/sh\nprintf "%s\\n" "$(basename "$0") $*" >> "$FAKE_LOG"\n'
FAKES = {
    "systemctl": LOGGING_FAKE,
    "sudo": LOGGING_FAKE + "exit 1\n",
    "python3": LOGGING_FAKE,
    "gio": LOGGING_FAKE,
    "open": LOGGING_FAKE,
    "xdg-open": LOGGING_FAKE,
    "loginctl": LOGGING_FAKE + textwrap.dedent("""\
        [ "${FAKE_LOGINCTL_EXIT:-0}" = 0 ] || exit "$FAKE_LOGINCTL_EXIT"
        echo "Linger=${FAKE_LINGER:-no}"
        """),
    # Only `ssh -G` (config lookup) runs in dry-run tests; real tunnels use FAKE_SSH.
    "ssh": '#!/bin/sh\n[ "$1" = -G ] || exit 99\nprintf "hostname %s\\nuser alice\\n" "$2"\n',
    "uname": '#!/bin/sh\necho "${FAKE_UNAME:-Linux}"\n',
    "launchctl": LOGGING_FAKE + textwrap.dedent("""\
        # `print gui/UID` asks whether the login domain is there
        # ($FAKE_LAUNCHD_DOMAIN_EXIT, default 0); `print gui/UID/LABEL` whether
        # that agent is loaded ($FAKE_AGENT_LOADED_EXIT, default 1: it is not).
        if [ "$1" = print ]; then
            case $2 in
                */*/*) exit "${FAKE_AGENT_LOADED_EXIT:-1}" ;;
                *) exit "${FAKE_LAUNCHD_DOMAIN_EXIT:-0}" ;;
            esac
        fi
        exit 0
        """),
    # macOS reports no automatic login user unless one is set.
    "defaults": LOGGING_FAKE + textwrap.dedent("""\
        [ -n "${FAKE_AUTOLOGIN_USER:-}" ] || exit 1
        echo "$FAKE_AUTOLOGIN_USER"
        """),
    "git": textwrap.dedent("""\
        #!/bin/sh
        # Logs "git <args>" to $FAKE_GIT_LOG. clone makes DIR/.git, fetch
        # exits $FAKE_GIT_FETCH_EXIT, show prints $FAKE_INSTALL_SH, and
        # anything else (rev-parse HEAD) prints $FAKE_COMMIT.
        printf '%s\\n' "git $*" >> "$FAKE_GIT_LOG"
        case " $* " in
            *" clone "*)
                [ "${FAKE_GIT_CLONE_EXIT:-0}" = 0 ] || exit "$FAKE_GIT_CLONE_EXIT"
                for last; do :; done
                mkdir -p "$last/.git" && echo "ref: refs/heads/main" > "$last/.git/HEAD"
                exit 0 ;;
            *" fetch "*) exit "${FAKE_GIT_FETCH_EXIT:-0}" ;;
            *" show "*)
                [ -n "${FAKE_INSTALL_SH:-}" ] || { echo "fatal: invalid object name" >&2; exit 128; }
                cat "$FAKE_INSTALL_SH"; exit 0 ;;
        esac
        [ -n "${FAKE_COMMIT:-}" ] || exit 128
        echo "$FAKE_COMMIT"
        """),
}
# The harness's own libbtbb pin, deploy's fallback (scripts/ci.sh).
HARNESS_PIN = re.search(
    r"^LIBBTBB_PIN=([0-9a-f]{40})$", (SCRIPTS / "ci.sh").read_text(encoding="utf-8"), re.M
).group(1)


def _write_exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _tree(root: Path) -> dict:
    """Every file under root with its content and mtime."""
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class FakeLab:
    """A temporary home, a copied checkout, and fake system commands."""

    def __init__(self, test: unittest.TestCase):
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.log = self.root / "commands.log"
        self.log.touch()
        self.git_log = self.root / "git.log"
        self.git_log.touch()
        for name, text in FAKES.items():
            _write_exe(self.bin / name, text)
        # The checkout deploy.sh installs from is the one it lives in.
        self.checkout = self.root / "checkout"
        (self.checkout / "scripts").mkdir(parents=True)
        for script in SHELL_FILES:
            shutil.copy2(script, self.checkout / "scripts" / script.name)
        # deploy.sh reads the harness's libbtbb pin from here.
        shutil.copy2(SCRIPTS / "ci.sh", self.checkout / "scripts" / "ci.sh")
        (self.checkout / "bootstrap.py").write_text("# fake\n", encoding="utf-8")
        _write_exe(self.checkout / ".venv" / "bin" / "python", "#!/bin/sh\n")
        (self.checkout / "testplans").mkdir()
        (self.checkout / "hardware.yaml").write_text("{}\n", encoding="utf-8")
        self.ubertooth = self.root / "ubertooth"
        self.ubertooth.mkdir()
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_LOG": str(self.log),
            "FAKE_GIT_LOG": str(self.git_log),
            "LANG": "C",
            # harness-open has no built-in host; these stand in for the lab.
            "HARNESS_HOST": LAB_HOST,
            "HARNESS_JUMP": JUMP_HOST,
        }

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    @property
    def agent_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def data_dir(self) -> Path:
        return self.home / ".local" / "share" / "ubertooth-harness"

    def script(self, name: str) -> Path:
        return self.checkout / "scripts" / name

    def run(self, name: str, *args: str, **env: str | None) -> subprocess.CompletedProcess:
        """Run a script. An env value of None removes that variable."""
        merged = {**self.env, **env}
        return subprocess.run(
            ["bash", str(self.script(name)), *args],
            env={key: value for key, value in merged.items() if value is not None},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def deploy(self, *args: str, **env: str | None) -> subprocess.CompletedProcess:
        base = ("--runner-host", "Host_Linux", "--ubertooth-source", str(self.ubertooth))
        if "--uninstall" in args:
            base = ()
        return self.run("deploy.sh", *base, *args, **env)

    def mac_deploy(self, *args: str, **env: str | None) -> subprocess.CompletedProcess:
        """deploy.sh on a Mac: the controller host comes from the environment."""
        base = ("--runner-host", "Host_Mac", "--ubertooth-source", str(self.ubertooth))
        if "--uninstall" in args:
            base = ()
        env.setdefault("FAKE_UNAME", "Darwin")
        return self.run("deploy.sh", *base, *args, **env)

    def commands(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def git_commands(self) -> list[str]:
        """git invocations other than reading the checkout's commit."""
        return [line for line in self.git_log.read_text(encoding="utf-8").splitlines()
                if not line.endswith(" rev-parse HEAD")]

    @property
    def libbtbb(self) -> Path:
        return self.data_dir / "sources" / "libbtbb"

    def clear_log(self) -> None:
        self.log.write_text("", encoding="utf-8")
        self.git_log.write_text("", encoding="utf-8")


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.lab = FakeLab(self)

    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installs_loopback_units_with_venv_python(self):
        result = self.lab.deploy()
        self.assertOk(result)
        python = self.lab.checkout / ".venv" / "bin" / "python"
        runs = self.lab.data_dir / "controller" / "runs"
        controller = (self.lab.unit_dir / CONTROLLER_UNIT).read_text(encoding="utf-8")
        runner = (self.lab.unit_dir / RUNNER_UNIT).read_text(encoding="utf-8")
        self.assertIn(
            f"ExecStart={python} -m ubertooth_harness controller --port 8765 "
            f"--results-dir {runs} --plans-dir {self.lab.checkout}/testplans\n",
            controller,
        )
        self.assertIn(
            f"ExecStart={python} -m ubertooth_harness runner "
            "--controller ws://127.0.0.1:8765/ws/runner "
            f"--hardware {self.lab.checkout}/hardware.yaml --host Host_Linux "
            f"--ubertooth-source {self.lab.ubertooth} "
            f"--plans-dir {self.lab.checkout}/testplans "
            f"--output {self.lab.data_dir}/runner/runs "
            f"--libbtbb-source {self.lab.libbtbb} --libbtbb-ref {HARNESS_PIN}\n",
            runner,
        )
        for unit in (controller, runner):
            self.assertIn("Restart=on-failure\n", unit)
            self.assertIn("WantedBy=default.target\n", unit)
            self.assertIn(f"WorkingDirectory={self.lab.checkout}\n", unit)
            self.assertNotIn("0.0.0.0", unit)
            self.assertNotIn("--bind", unit)
        self.assertIn("Description=Ubertooth test harness controller (127.0.0.1:8765)", controller)
        self.assertIn(f"After={CONTROLLER_UNIT}\n", runner)
        self.assertIn(f"Wants={CONTROLLER_UNIT}\n", runner)
        self.assertNotIn("After=", controller)
        self.assertTrue(runs.is_dir())
        self.assertTrue((self.lab.data_dir / "runner" / "runs").is_dir())
        self.assertIn(f"python3 {self.lab.checkout}/bootstrap.py", self.lab.commands())
        systemctl = [c for c in self.lab.commands() if c.startswith("systemctl")]
        self.assertEqual(systemctl, [
            "systemctl --user show-environment",
            "systemctl --user daemon-reload",
            f"systemctl --user restart {CONTROLLER_UNIT}",
            f"systemctl --user restart {RUNNER_UNIT}",
            f"systemctl --user enable --now {CONTROLLER_UNIT} {RUNNER_UNIT}",
            f"systemctl --user --no-pager --lines=0 status {CONTROLLER_UNIT} {RUNNER_UNIT}",
        ])
        self.assertIn("GUI: http://127.0.0.1:8765/ui/", result.stdout)

    def busy_controller(self):
        """Make the venv python report an active run, as the controller would."""
        _write_exe(self.lab.checkout / ".venv" / "bin" / "python",
                   "#!/bin/sh\ncat >/dev/null\necho Host_Linux:run-7\n")

    def test_refuses_to_restart_during_an_active_run(self):
        self.assertOk(self.lab.deploy(FAKE_COMMIT="a" * 40))
        before = _tree(self.lab.home)
        self.busy_controller()
        self.lab.clear_log()
        result = self.lab.deploy(FAKE_COMMIT="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active runs Host_Linux:run-7", result.stderr)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertNotIn(f"systemctl --user restart {RUNNER_UNIT}", self.lab.commands())

    def test_force_restarts_during_an_active_run(self):
        self.assertOk(self.lab.deploy(FAKE_COMMIT="a" * 40))
        self.busy_controller()
        self.lab.clear_log()
        result = self.lab.deploy("--force", FAKE_COMMIT="b" * 40)
        self.assertOk(result)
        self.assertIn("restarting during active runs (--force)", result.stdout)
        self.assertIn(f"systemctl --user restart {RUNNER_UNIT}", self.lab.commands())

    def test_active_run_does_not_block_an_unchanged_deploy(self):
        self.assertOk(self.lab.deploy())
        self.busy_controller()
        self.assertOk(self.lab.deploy())

    def test_no_user_manager_changes_nothing(self):
        _write_exe(self.lab.bin / "systemctl", LOGGING_FAKE + "exit 1\n")
        result = self.lab.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no systemd user manager", result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})
        self.assertNotIn(f"python3 {self.lab.checkout}/bootstrap.py", self.lab.commands())

    def test_second_run_changes_nothing(self):
        self.assertOk(self.lab.deploy())
        before = _tree(self.lab.home)
        self.lab.clear_log()
        result = self.lab.deploy()
        self.assertOk(result)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertIn(f"{CONTROLLER_UNIT} unchanged", result.stdout)
        self.assertIn(f"{RUNNER_UNIT} unchanged", result.stdout)
        systemctl = [c for c in self.lab.commands() if c.startswith("systemctl")]
        self.assertEqual(systemctl, [
            "systemctl --user show-environment",
            f"systemctl --user enable --now {CONTROLLER_UNIT} {RUNNER_UNIT}",
            f"systemctl --user --no-pager --lines=0 status {CONTROLLER_UNIT} {RUNNER_UNIT}",
        ])

    def test_new_commit_rewrites_and_restarts_both(self):
        self.assertOk(self.lab.deploy(FAKE_COMMIT="a" * 40))
        self.lab.clear_log()
        self.assertOk(self.lab.deploy(FAKE_COMMIT="b" * 40))
        unit = (self.lab.unit_dir / CONTROLLER_UNIT).read_text(encoding="utf-8")
        self.assertIn("commit " + "b" * 40, unit)
        self.assertIn(f"systemctl --user restart {CONTROLLER_UNIT}", self.lab.commands())
        self.assertIn(f"systemctl --user restart {RUNNER_UNIT}", self.lab.commands())

    def test_runner_option_change_restarts_only_the_runner(self):
        self.assertOk(self.lab.deploy())
        self.lab.clear_log()
        self.assertOk(self.lab.deploy("--allow-power-changes"))
        runner = (self.lab.unit_dir / RUNNER_UNIT).read_text(encoding="utf-8")
        self.assertIn(" --allow-power-changes\n", runner)
        self.assertNotIn(f"systemctl --user restart {CONTROLLER_UNIT}", self.lab.commands())
        self.assertIn(f"systemctl --user restart {RUNNER_UNIT}", self.lab.commands())

    def test_port_and_pass_through_options(self):
        libbtbb = self.lab.root / "libbtbb"
        libbtbb.mkdir()
        self.assertOk(self.lab.deploy(
            "--port", "9123",
            "--ubertooth-repo", "https://example.org/ubertooth.git",
            "--libbtbb-source", str(libbtbb), "--libbtbb-ref", "master",
        ))
        controller = (self.lab.unit_dir / CONTROLLER_UNIT).read_text(encoding="utf-8")
        runner = (self.lab.unit_dir / RUNNER_UNIT).read_text(encoding="utf-8")
        self.assertIn(" controller --port 9123 ", controller)
        self.assertIn(" --ubertooth-repo https://example.org/ubertooth.git\n", controller)
        self.assertIn(" --controller ws://127.0.0.1:9123/ws/runner ", runner)
        self.assertIn(f" --libbtbb-source {libbtbb} --libbtbb-ref master", runner)
        # A named libbtbb is the operator's: deploy neither clones nor fetches it.
        self.assertFalse([c for c in self.lab.git_commands() if c.startswith(f"git -C {libbtbb}")])
        self.assertFalse([c for c in self.lab.git_commands() if " clone " in c])

    def runner_unit(self) -> str:
        return (self.lab.unit_dir / RUNNER_UNIT).read_text(encoding="utf-8")

    def test_default_libbtbb_is_cloned_once_then_fetched(self):
        result = self.lab.deploy()
        self.assertOk(result)
        libbtbb = self.lab.libbtbb
        self.assertTrue((libbtbb / ".git").is_dir())
        self.assertIn(f"git clone --quiet https://github.com/SqueakyFloors/libbtbb.git {libbtbb}",
                      self.lab.git_commands())
        self.assertIn(f"libbtbb: cloned https://github.com/SqueakyFloors/libbtbb.git into {libbtbb}",
                      result.stdout)
        # No install.sh pin to read here, so the harness's own pin, said so.
        self.assertIn(f"libbtbb: ref {HARNESS_PIN}, the harness's pin", result.stdout)
        self.assertIn(f" --libbtbb-source {libbtbb} --libbtbb-ref {HARNESS_PIN}\n",
                      self.runner_unit())
        before = _tree(self.lab.home)
        self.lab.clear_log()
        result = self.lab.deploy()
        self.assertOk(result)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertIn(f"{RUNNER_UNIT} unchanged", result.stdout)
        git = self.lab.git_commands()
        self.assertFalse([c for c in git if " clone " in c])
        self.assertIn(f"git -C {libbtbb} fetch --quiet --tags origin", git)

    def test_libbtbb_ref_defaults_to_the_fork_install_sh_pin(self):
        install = self.lab.root / "install.sh"
        install.write_text(
            "#!/bin/sh\nLIBBTBB_URL=https://github.com/SqueakyFloors/libbtbb.git\n"
            "LIBBTBB_REF=\"0123456789abcdef0123456789abcdef01234567\"  # fork #320\n"
            "case $1 in --libbtbb-ref) LIBBTBB_REF=\"${2:-}\" ;; esac\n",
            encoding="utf-8",
        )
        result = self.lab.deploy(FAKE_INSTALL_SH=str(install))
        self.assertOk(result)
        pin = "0123456789abcdef0123456789abcdef01234567"
        self.assertIn(f" --libbtbb-ref {pin}\n", self.runner_unit())
        self.assertIn(f"libbtbb: ref {pin}, the LIBBTBB_REF pin in install.sh on origin/main",
                      result.stdout)
        git = self.lab.git_commands()
        self.assertIn(f"git -C {self.lab.ubertooth} fetch --quiet origin main", git)
        self.assertIn(f"git -C {self.lab.ubertooth} show origin/main:install.sh", git)

    def test_named_libbtbb_source_gets_the_pin_as_its_ref(self):
        libbtbb = self.lab.root / "my-libbtbb"
        libbtbb.mkdir()
        self.assertOk(self.lab.deploy("--libbtbb-source", str(libbtbb)))
        self.assertIn(f" --libbtbb-source {libbtbb} --libbtbb-ref {HARNESS_PIN}\n",
                      self.runner_unit())
        self.assertFalse(self.lab.libbtbb.exists())

    def test_no_libbtbb_deploys_a_runner_that_cannot_build(self):
        result = self.lab.deploy("--no-libbtbb")
        self.assertOk(result)
        self.assertNotIn("--libbtbb", self.runner_unit())
        self.assertIn("libbtbb: none (--no-libbtbb)", result.stdout)
        self.assertEqual([], self.lab.git_commands())
        self.assertFalse(self.lab.libbtbb.exists())
        result = self.lab.deploy("--no-libbtbb", "--libbtbb-ref", "master")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--no-libbtbb cannot be combined", result.stderr)

    def test_libbtbb_clone_failure_stops_before_any_unit(self):
        result = self.lab.deploy(FAKE_GIT_CLONE_EXIT="128")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not clone https://github.com/SqueakyFloors/libbtbb.git", result.stderr)
        self.assertIn("--no-libbtbb", result.stderr)
        self.assertFalse((self.lab.unit_dir / RUNNER_UNIT).exists())
        self.assertFalse(self.lab.libbtbb.exists())

    def test_libbtbb_fetch_failure_is_a_warning(self):
        self.assertOk(self.lab.deploy())
        result = self.lab.deploy(FAKE_GIT_FETCH_EXIT="1")
        self.assertOk(result)
        self.assertIn(f"warning: could not fetch {self.lab.libbtbb}", result.stdout)
        self.assertIn(f" --libbtbb-ref {HARNESS_PIN}\n", self.runner_unit())

    def test_libbtbb_path_that_is_not_a_clone_is_refused(self):
        self.lab.libbtbb.mkdir(parents=True)
        (self.lab.libbtbb / "notes.txt").write_text("mine\n", encoding="utf-8")
        result = self.lab.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exists but is not a git clone", result.stderr)
        self.assertEqual("mine\n", (self.lab.libbtbb / "notes.txt").read_text(encoding="utf-8"))

    def test_xdg_dirs_are_honoured(self):
        data = self.lab.root / "xdg-data"
        config = self.lab.root / "xdg-config"
        self.assertOk(self.lab.deploy(XDG_DATA_HOME=str(data), XDG_CONFIG_HOME=str(config)))
        controller = (config / "systemd" / "user" / CONTROLLER_UNIT).read_text(encoding="utf-8")
        self.assertIn(f"--results-dir {data}/ubertooth-harness/controller/runs ", controller)
        self.assertFalse(self.lab.unit_dir.exists())

    def test_dry_run_changes_nothing_and_prints_units(self):
        result = self.lab.deploy("--dry-run")
        self.assertOk(result)
        self.assertEqual(_tree(self.lab.home), {})
        self.assertEqual(
            [c for c in self.lab.commands() if not c.startswith("loginctl")], []
        )
        self.assertIn(f"would create {self.lab.unit_dir}/{CONTROLLER_UNIT}:", result.stdout)
        self.assertIn("--controller ws://127.0.0.1:8765/ws/runner", result.stdout)
        self.assertIn(f"would run: python3 {self.lab.checkout}/bootstrap.py", result.stdout)
        self.assertIn(f"would run: systemctl --user restart {CONTROLLER_UNIT}", result.stdout)
        self.assertIn("would run: git clone --quiet https://github.com/SqueakyFloors/libbtbb.git "
                      f"{self.lab.libbtbb}", result.stdout)
        self.assertIn(f"--libbtbb-source {self.lab.libbtbb} --libbtbb-ref {HARNESS_PIN}",
                      result.stdout)
        # Dry run reads the pin but neither clones nor fetches.
        self.assertEqual(
            [f"git -C {self.lab.ubertooth} show origin/main:install.sh"],
            self.lab.git_commands(),
        )

    def test_no_start_writes_units_only(self):
        result = self.lab.deploy("--no-start")
        self.assertOk(result)
        self.assertTrue((self.lab.unit_dir / CONTROLLER_UNIT).is_file())
        systemctl = [c for c in self.lab.commands() if c.startswith("systemctl")]
        self.assertEqual(systemctl, ["systemctl --user daemon-reload"])
        self.assertIn("not started (--no-start)", result.stdout)

    def test_uninstall_removes_only_the_units(self):
        self.assertOk(self.lab.deploy())
        other = self.lab.unit_dir / "other-app.service"
        other.write_text("[Service]\n", encoding="utf-8")
        wants = self.lab.unit_dir / "default.target.wants"
        wants.mkdir()
        (wants / CONTROLLER_UNIT).symlink_to(self.lab.unit_dir / CONTROLLER_UNIT)
        (wants / "other-app.service").symlink_to(other)
        results_file = self.lab.data_dir / "controller" / "runs" / "keep.json"
        results_file.write_text("{}", encoding="utf-8")
        self.lab.clear_log()
        result = self.lab.deploy("--uninstall")
        self.assertOk(result)
        self.assertEqual(
            sorted(p.name for p in self.lab.unit_dir.iterdir()),
            ["default.target.wants", "other-app.service"],
        )
        self.assertEqual([p.name for p in wants.iterdir()], ["other-app.service"])
        self.assertTrue(results_file.is_file())
        self.assertTrue((self.lab.checkout / ".venv" / "bin" / "python").is_file())
        self.assertEqual(self.lab.commands(), [
            f"systemctl --user disable --now {RUNNER_UNIT}",
            f"systemctl --user disable --now {CONTROLLER_UNIT}",
            "systemctl --user daemon-reload",
        ])
        # Again: nothing left to remove, no systemctl calls.
        self.lab.clear_log()
        result = self.lab.deploy("--uninstall")
        self.assertOk(result)
        self.assertIn(f"{CONTROLLER_UNIT} is not installed", result.stdout)
        self.assertEqual(self.lab.commands(), [])

    def test_linger_off_prints_the_one_time_command(self):
        result = self.lab.deploy(FAKE_LINGER="no")
        self.assertOk(result)
        self.assertIn(f"loginctl show-user {USER} -p Linger", self.lab.commands())
        self.assertIn(f"    sudo loginctl enable-linger {USER}\n", result.stdout)
        self.assertIn("stop when", result.stdout)
        self.assertIn("never runs sudo", result.stdout)

    def test_linger_on_prints_no_warning(self):
        result = self.lab.deploy(FAKE_LINGER="yes")
        self.assertOk(result)
        self.assertNotIn("linger", result.stdout.lower())

    def test_linger_unknown_is_a_warning(self):
        result = self.lab.deploy(FAKE_LOGINCTL_EXIT="1")
        self.assertOk(result)
        self.assertIn("could not read systemd linger", result.stdout)

    def test_never_runs_sudo(self):
        for args in ((), ("--no-start",), ("--dry-run",), ("--uninstall",)):
            self.lab.deploy(*args, FAKE_LINGER="no")
        self.assertFalse([c for c in self.lab.commands() if c.startswith("sudo")])
        # The only sudo in the script is text it prints.
        for line in DEPLOY.read_text(encoding="utf-8").splitlines():
            if "sudo" in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith(("#", "say ", "die ")), line
                )

    def test_refuses_an_unknown_os(self):
        result = self.lab.deploy(FAKE_UNAME="SunOS")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported OS SunOS", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})
        self.assertEqual(self.lab.commands(), [])

    def test_refuses_the_macos_only_options(self):
        for option in ("--controller-host", "--controller-jump"):
            result = self.lab.deploy(option, LAB_HOST)
            self.assertNotEqual(result.returncode, 0, option)
            self.assertIn(f"{option} is a macOS option", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_requires_runner_host_and_source(self):
        result = self.lab.run("deploy.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--runner-host NAME is required", result.stderr)
        result = self.lab.run("deploy.sh", "--runner-host", "Host_Linux")
        self.assertIn("--ubertooth-source DIR is required", result.stderr)
        result = self.lab.deploy("--hardware", str(self.lab.root / "missing.yaml"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_rejects_bad_port_and_unquotable_values(self):
        for port in ("0", "65536", "80a", "-1"):
            result = self.lab.deploy("--port", port)
            self.assertNotEqual(result.returncode, 0, port)
            self.assertIn("--port must be", result.stderr)
        for args in (
            ("--ubertooth-repo", "https://example.org/a b"),
            ("--ubertooth-repo", "https://example.org/$HOME"),
            ("--libbtbb-ref", "100%"),
        ):
            result = self.lab.deploy(*args)
            self.assertNotEqual(result.returncode, 0, args)
            self.assertIn("does not quote", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_unknown_option(self):
        result = self.lab.deploy("--host", "0.0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option --host", result.stderr)

    def test_help(self):
        result = self.lab.run("deploy.sh", "--help")
        self.assertOk(result)
        self.assertIn("--uninstall", result.stdout)
        self.assertNotIn("set -euo", result.stdout)


class MacDeployTest(unittest.TestCase):
    """deploy.sh on macOS: a runner LaunchAgent and its SSH tunnel."""

    def setUp(self):
        self.lab = FakeLab(self)
        self.domain = f"gui/{os.getuid()}"

    def assertOk(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def agent(self, name: str) -> str:
        return (self.lab.agent_dir / name).read_text(encoding="utf-8")

    def parsed(self, name: str) -> dict:
        """The agent as launchd would read it, which also checks the XML."""
        return plistlib.loads((self.lab.agent_dir / name).read_bytes())

    def launchctl(self) -> list[str]:
        return [c for c in self.lab.commands() if c.startswith("launchctl")]

    def test_installs_a_runner_agent_and_a_loopback_tunnel(self):
        result = self.lab.mac_deploy()
        self.assertOk(result)
        python = self.lab.checkout / ".venv" / "bin" / "python"
        runner = self.parsed(RUNNER_AGENT)
        tunnel = self.parsed(TUNNEL_AGENT)
        self.assertEqual(RUNNER_LABEL, runner["Label"])
        self.assertEqual([
            str(python), "-m", "ubertooth_harness", "runner",
            "--controller", "ws://127.0.0.1:8765/ws/runner",
            "--hardware", f"{self.lab.checkout}/hardware.yaml",
            "--host", "Host_Mac",
            "--ubertooth-source", str(self.lab.ubertooth),
            "--plans-dir", f"{self.lab.checkout}/testplans",
            "--output", f"{self.lab.data_dir}/runner/runs",
            "--libbtbb-source", str(self.lab.libbtbb),
            "--libbtbb-ref", HARNESS_PIN,
        ], runner["ProgramArguments"])
        self.assertEqual(TUNNEL_LABEL, tunnel["Label"])
        self.assertEqual([
            "/usr/bin/ssh", "-N",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L", "127.0.0.1:8765:127.0.0.1:8765",
            "-J", JUMP_HOST,
            LAB_HOST,
        ], tunnel["ProgramArguments"])
        for agent, prefix in ((runner, "runner"), (tunnel, "tunnel")):
            self.assertTrue(agent["RunAtLoad"])
            self.assertTrue(agent["KeepAlive"])
            self.assertEqual(10, agent["ThrottleInterval"])
            self.assertEqual(str(self.lab.checkout), agent["WorkingDirectory"])
            self.assertEqual(f"{self.lab.log_dir}/{prefix}.log", agent["StandardOutPath"])
            self.assertEqual(f"{self.lab.log_dir}/{prefix}.err.log", agent["StandardErrorPath"])
            # A LaunchAgent inherits almost no PATH, and a build needs Homebrew.
            self.assertEqual(
                "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                agent["EnvironmentVariables"]["PATH"],
            )
        # Cancelling a run and restoring its boards takes time; ssh needs none.
        self.assertEqual(180, runner["ExitTimeOut"])
        self.assertEqual(20, tunnel["ExitTimeOut"])
        for name in (RUNNER_AGENT, TUNNEL_AGENT):
            text = self.agent(name)
            self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'), name)
            self.assertTrue(text.endswith("</plist>\n"), name)
            self.assertIn(f"from {self.lab.checkout}", text)
        # No controller here: it runs on the Linux host.
        self.assertEqual(sorted(p.name for p in self.lab.agent_dir.iterdir()),
                         [RUNNER_AGENT, TUNNEL_AGENT])
        self.assertFalse(self.lab.unit_dir.exists())
        self.assertFalse((self.lab.data_dir / "controller").exists())
        self.assertTrue(self.lab.log_dir.is_dir())
        self.assertTrue((self.lab.data_dir / "runner" / "runs").is_dir())
        self.assertIn(f"python3 {self.lab.checkout}/bootstrap.py", self.lab.commands())
        self.assertEqual(self.launchctl(), [
            f"launchctl print {self.domain}",
            f"launchctl print {self.domain}/{TUNNEL_LABEL}",
            f"launchctl bootstrap {self.domain} {self.lab.agent_dir}/{TUNNEL_AGENT}",
            f"launchctl print {self.domain}/{RUNNER_LABEL}",
            f"launchctl bootstrap {self.domain} {self.lab.agent_dir}/{RUNNER_AGENT}",
        ])
        self.assertNotIn("systemctl", "".join(self.lab.commands()))
        self.assertIn(f"tunnel: 127.0.0.1:8765 on this Mac to 127.0.0.1:8765 on {LAB_HOST}"
                      f" through {JUMP_HOST}", result.stdout)
        self.assertIn("BatchMode=yes", result.stdout)
        self.assertIn("known_hosts", result.stdout)
        self.assertIn("GUI: http://127.0.0.1:8765/ui/", result.stdout)

    def test_second_run_changes_nothing_and_leaves_loaded_agents_alone(self):
        self.assertOk(self.lab.mac_deploy())
        before = _tree(self.lab.home)
        self.lab.clear_log()
        result = self.lab.mac_deploy(FAKE_AGENT_LOADED_EXIT="0")
        self.assertOk(result)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertIn(f"{RUNNER_AGENT} unchanged", result.stdout)
        self.assertIn(f"{TUNNEL_LABEL} is already loaded", result.stdout)
        self.assertEqual(self.launchctl(), [
            f"launchctl print {self.domain}",
            f"launchctl print {self.domain}/{TUNNEL_LABEL}",
            f"launchctl print {self.domain}/{RUNNER_LABEL}",
        ])

    def test_a_changed_agent_is_booted_out_and_bootstrapped_again(self):
        self.assertOk(self.lab.mac_deploy(FAKE_COMMIT="a" * 40))
        self.lab.clear_log()
        result = self.lab.mac_deploy(FAKE_COMMIT="b" * 40, FAKE_AGENT_LOADED_EXIT="0")
        self.assertOk(result)
        self.assertIn("commit " + "b" * 40, self.agent(RUNNER_AGENT))
        self.assertEqual(self.launchctl(), [
            f"launchctl print {self.domain}",
            f"launchctl print {self.domain}/{TUNNEL_LABEL}",
            f"launchctl bootout {self.domain}/{TUNNEL_LABEL}",
            f"launchctl bootstrap {self.domain} {self.lab.agent_dir}/{TUNNEL_AGENT}",
            f"launchctl print {self.domain}/{RUNNER_LABEL}",
            f"launchctl bootout {self.domain}/{RUNNER_LABEL}",
            f"launchctl bootstrap {self.domain} {self.lab.agent_dir}/{RUNNER_AGENT}",
        ])

    def test_port_and_jump_options(self):
        self.assertOk(self.lab.mac_deploy(
            "--port", "9123", "--controller-host", "other.example",
            "--controller-jump", "", "--allow-power-changes",
        ))
        tunnel = self.parsed(TUNNEL_AGENT)["ProgramArguments"]
        self.assertEqual(["-L", "127.0.0.1:9123:127.0.0.1:9123", "other.example"],
                         tunnel[-3:])
        self.assertNotIn("-J", tunnel)
        self.assertNotIn(JUMP_HOST, tunnel)
        runner = self.parsed(RUNNER_AGENT)["ProgramArguments"]
        self.assertIn("ws://127.0.0.1:9123/ws/runner", runner)
        self.assertEqual("--allow-power-changes", runner[-1])

    def test_the_controller_host_comes_from_the_config_file(self):
        config = self.lab.root / "harness-open.conf"
        config.write_text(
            "HARNESS_HOST=conf-host.example\nHARNESS_JUMP=conf-jump.example\n"
            "HARNESS_PORT=9300\n",
            encoding="utf-8",
        )
        self.assertOk(self.lab.mac_deploy(
            HARNESS_HOST=None, HARNESS_JUMP=None, HARNESS_OPEN_CONFIG=str(config),
        ))
        tunnel = self.parsed(TUNNEL_AGENT)["ProgramArguments"]
        self.assertEqual(
            ["-L", "127.0.0.1:9300:127.0.0.1:9300", "-J", "conf-jump.example",
             "conf-host.example"],
            tunnel[-5:],
        )
        # The environment wins over the file, and an option over both.
        self.assertOk(self.lab.mac_deploy(
            "--port", "9400", HARNESS_OPEN_CONFIG=str(config),
        ))
        tunnel = self.parsed(TUNNEL_AGENT)["ProgramArguments"]
        self.assertEqual(
            ["-L", "127.0.0.1:9400:127.0.0.1:9400", "-J", JUMP_HOST, LAB_HOST],
            tunnel[-5:],
        )

    def test_a_config_file_others_can_write_is_refused(self):
        config = self.lab.root / "writable.conf"
        config.write_text("HARNESS_HOST=conf-host.example\n", encoding="utf-8")
        config.chmod(0o666)
        result = self.lab.mac_deploy(HARNESS_OPEN_CONFIG=str(config))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable by others", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_refuses_to_run_without_a_controller_host(self):
        result = self.lab.mac_deploy(HARNESS_HOST=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no controller host", result.stderr)
        self.assertIn("--controller-host", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_rejects_option_like_or_odd_hosts(self):
        for args in (
            ("--controller-host", "-oProxyCommand=touch x"),
            ("--controller-host", "a;b"),
            ("--controller-host", ""),
            ("--controller-jump", "a b"),
            ("--port", "0"),
        ):
            result = self.lab.mac_deploy(*args)
            self.assertNotEqual(result.returncode, 0, args)
        self.assertEqual(_tree(self.lab.home), {})

    def test_refuses_the_linux_only_option(self):
        result = self.lab.mac_deploy("--ubertooth-repo", "https://example.org/u.git")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--ubertooth-repo is a controller option", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})

    def test_no_launchd_domain_changes_nothing(self):
        result = self.lab.mac_deploy(FAKE_LAUNCHD_DOMAIN_EXIT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"no launchd domain {self.domain}", result.stderr)
        self.assertIn("nothing was changed", result.stderr)
        self.assertEqual(_tree(self.lab.home), {})
        self.assertNotIn(f"python3 {self.lab.checkout}/bootstrap.py", self.lab.commands())

    def test_no_start_writes_agents_only(self):
        result = self.lab.mac_deploy("--no-start")
        self.assertOk(result)
        self.assertTrue((self.lab.agent_dir / RUNNER_AGENT).is_file())
        self.assertEqual(self.launchctl(), [])
        self.assertIn("not started (--no-start)", result.stdout)
        self.assertIn(f"launchctl bootstrap {self.domain} "
                      f"{self.lab.agent_dir}/{TUNNEL_AGENT} "
                      f"{self.lab.agent_dir}/{RUNNER_AGENT}", result.stdout)

    def test_dry_run_changes_nothing_and_prints_the_agents(self):
        result = self.lab.mac_deploy("--dry-run")
        self.assertOk(result)
        self.assertEqual(_tree(self.lab.home), {})
        self.assertIn(f"would create {self.lab.agent_dir}/{RUNNER_AGENT}:", result.stdout)
        self.assertIn("<string>127.0.0.1:8765:127.0.0.1:8765</string>", result.stdout)
        self.assertIn(f"would run: launchctl bootstrap {self.domain}", result.stdout)
        self.assertIn(f"would run: python3 {self.lab.checkout}/bootstrap.py", result.stdout)
        self.assertEqual([c for c in self.launchctl() if " print " not in c], [])

    def test_uninstall_boots_out_and_removes_only_the_agents(self):
        self.assertOk(self.lab.mac_deploy())
        other = self.lab.agent_dir / "com.example.other.plist"
        other.write_text("<plist/>\n", encoding="utf-8")
        runs = self.lab.data_dir / "runner" / "runs" / "keep.json"
        runs.write_text("{}", encoding="utf-8")
        self.lab.clear_log()
        result = self.lab.mac_deploy("--uninstall")
        self.assertOk(result)
        self.assertEqual([p.name for p in self.lab.agent_dir.iterdir()],
                         ["com.example.other.plist"])
        self.assertTrue(runs.is_file())
        self.assertTrue((self.lab.checkout / ".venv" / "bin" / "python").is_file())
        self.assertEqual(self.launchctl(), [
            f"launchctl bootout {self.domain}/{RUNNER_LABEL}",
            f"launchctl bootout {self.domain}/{TUNNEL_LABEL}",
        ])
        # Again: nothing left to remove.
        self.lab.clear_log()
        result = self.lab.mac_deploy("--uninstall")
        self.assertOk(result)
        self.assertIn(f"{RUNNER_AGENT} is not installed", result.stdout)
        self.assertEqual(self.launchctl(), [])

    def busy_controller(self, *runs: str):
        """Make the venv python answer as the controller would, logging argv."""
        _write_exe(
            self.lab.checkout / ".venv" / "bin" / "python",
            "#!/bin/sh\ncat >/dev/null\n"
            'printf "python %s\\n" "$*" >> "$FAKE_LOG"\n'
            + "".join(f"echo {run}\n" for run in runs),
        )

    def test_refuses_to_restart_during_this_host_s_run(self):
        self.assertOk(self.lab.mac_deploy(FAKE_COMMIT="a" * 40))
        before = _tree(self.lab.home)
        self.busy_controller("Host_Mac:run-7")
        self.lab.clear_log()
        result = self.lab.mac_deploy(FAKE_COMMIT="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active runs Host_Mac:run-7", result.stderr)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertEqual(self.launchctl(), [f"launchctl print {self.domain}"])
        # Only this Mac's runner is restarted, so only its runs are asked for.
        self.assertIn("python - 8765 Host_Mac", self.lab.commands())

    def test_force_restarts_during_an_active_run(self):
        self.assertOk(self.lab.mac_deploy(FAKE_COMMIT="a" * 40))
        self.busy_controller("Host_Mac:run-7")
        result = self.lab.mac_deploy("--force", FAKE_COMMIT="b" * 40)
        self.assertOk(result)
        self.assertIn("restarting during active runs (--force)", result.stdout)
        self.assertIn(f"launchctl bootstrap {self.domain} "
                      f"{self.lab.agent_dir}/{RUNNER_AGENT}", self.launchctl())

    def test_automatic_login_is_checked(self):
        result = self.lab.mac_deploy()
        self.assertOk(result)
        self.assertIn("is not this Mac's automatic login user", result.stdout)
        self.assertIn("Automatic login", result.stdout)
        result = self.lab.mac_deploy(FAKE_AUTOLOGIN_USER=USER)
        self.assertOk(result)
        self.assertNotIn("automatic login", result.stdout)
        self.assertIn("defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser",
                      self.lab.commands())

    def test_never_runs_sudo_or_systemctl(self):
        for args in ((), ("--no-start",), ("--dry-run",), ("--uninstall",)):
            self.lab.mac_deploy(*args)
        self.assertFalse([c for c in self.lab.commands()
                          if c.startswith(("sudo", "systemctl", "loginctl"))])


class DesktopShortcutTest(unittest.TestCase):
    def setUp(self):
        self.lab = FakeLab(self)
        self.app = self.lab.home / ".local" / "share" / "applications" / "ubertooth-harness.desktop"

    def test_writes_launcher_for_loopback_gui(self):
        result = self.lab.run("install-desktop-shortcut.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.app.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("[Desktop Entry]\n"))
        self.assertIn("Type=Application\n", text)
        self.assertIn("Exec=xdg-open http://127.0.0.1:8765/ui/\n", text)
        self.assertFalse((self.lab.home / "Desktop").exists())
        self.assertEqual(self.lab.commands(), [])

    def test_desktop_copy_is_trusted_and_idempotent(self):
        desktop = self.lab.home / "Desktop"
        desktop.mkdir()
        result = self.lab.run("install-desktop-shortcut.sh", "--port", "9000")
        self.assertEqual(result.returncode, 0, result.stderr)
        copy = desktop / "ubertooth-harness.desktop"
        self.assertIn("Exec=xdg-open http://127.0.0.1:9000/ui/\n", copy.read_text(encoding="utf-8"))
        self.assertTrue(os.access(copy, os.X_OK))
        self.assertEqual(self.lab.commands(), [f"gio set {copy} metadata::trusted true"])
        before = _tree(self.lab.home)
        result = self.lab.run("install-desktop-shortcut.sh", "--port", "9000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_tree(self.lab.home), before)
        self.assertEqual(result.stdout.count("unchanged"), 2)

    def test_uninstall(self):
        (self.lab.home / "Desktop").mkdir()
        other = self.app.parent / "other.desktop"
        self.assertEqual(self.lab.run("install-desktop-shortcut.sh").returncode, 0)
        other.write_text("[Desktop Entry]\n", encoding="utf-8")
        result = self.lab.run("install-desktop-shortcut.sh", "--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.app.exists())
        self.assertFalse((self.lab.home / "Desktop" / "ubertooth-harness.desktop").exists())
        self.assertTrue(other.is_file())

    def test_dry_run_and_xdg_data_home(self):
        data = self.lab.root / "data"
        result = self.lab.run("install-desktop-shortcut.sh", "--dry-run", XDG_DATA_HOME=str(data))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"would write {data}/applications/ubertooth-harness.desktop:", result.stdout)
        self.assertFalse(data.exists())
        self.assertEqual(_tree(self.lab.home), {})

    def test_refuses_non_linux_and_bad_port(self):
        result = self.lab.run("install-desktop-shortcut.sh", FAKE_UNAME="Darwin")
        self.assertNotEqual(result.returncode, 0)
        result = self.lab.run("install-desktop-shortcut.sh", "--port", "x")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(_tree(self.lab.home), {})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


FAKE_SSH = """\
#!{python}
# Log argv, then listen where the -L forward would and wait to be stopped.
import os, signal, socket, sys, time
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("ssh " + " ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_SSH_EXIT"):
    sys.exit(int(os.environ["FAKE_SSH_EXIT"]))
spec = sys.argv[sys.argv.index("-L") + 1]
bind, port = spec.split(":")[0], int(spec.split(":")[1])
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((bind, port))
sock.listen(8)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
sock.settimeout(0.2)
while True:
    try:
        sock.accept()[0].close()
    except socket.timeout:
        pass
"""


class HarnessOpenTest(unittest.TestCase):
    def setUp(self):
        self.lab = FakeLab(self)

    def dry_run(self, *args):
        result = self.lab.run("harness-open", "--dry-run", *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_default_uses_jump_host_and_loopback_binds(self):
        argv, url = self.dry_run()
        self.assertEqual(argv, (
            "ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
            f"-L 127.0.0.1:8765:127.0.0.1:8765 -J {JUMP_HOST} {LAB_HOST}"
        ))
        self.assertEqual(url, "http://127.0.0.1:8765/ui/")
        self.assertEqual(self.lab.commands(), [])

    def _dry_run_env(self, *args, **env):
        result = self.lab.run("harness-open", "--dry-run", *args, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()[0]

    def test_no_host_or_jump_name_is_built_into_the_script(self):
        lines = [
            line
            for script in (HARNESS_OPEN, CONFIG_LIB, DEPLOY)
            for line in script.read_text(encoding="utf-8").splitlines()
        ]
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            # Any assignment that can decide the host or jump host may only
            # take its value from HARNESS_* or an option value, never from a
            # literal or a literal fallback. Covers env_host=, HARNESS_HOST=
            # and := defaults as well as host= itself. The expansion fallback
            # must be empty, so ${HARNESS_HOST:-a-real-host} fails too, and a
            # quoted literal reads as an empty value, which also fails.
            for name, value in re.findall(
                r"(?i)\b(?:env_|harness_)?(host|jump)[:]?=([^\s;\"]*)", line
            ):
                self.assertRegex(
                    value,
                    r"^(\$\{HARNESS_[A-Z_]+:?[-+]\}|\$2|\$env_\w+)$",
                    f"{name}: {line}",
                )

    @unittest.skipIf(os.geteuid() == 0, "running as root owns every file")
    def test_refuses_a_config_file_it_does_not_own(self):
        result = self.lab.run(
            "harness-open", "--dry-run", HARNESS_OPEN_CONFIG="/etc/hosts",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owned by you", result.stderr)

    def test_refuses_a_config_file_others_can_write(self):
        config = self.lab.root / "writable.conf"
        config.write_text("HARNESS_HOST=conf-host.example\n", encoding="utf-8")
        config.chmod(0o666)
        result = self.lab.run(
            "harness-open", "--dry-run", HARNESS_OPEN_CONFIG=str(config),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable by others", result.stderr)

    def test_a_failing_config_file_is_reported(self):
        # Read like a shell profile: an earlier failure does not stop it, but
        # the file's own exit status is reported rather than killing us mute.
        broken = self.lab.root / "broken.conf"
        broken.write_text("HARNESS_HOST=conf-host.example\nfalse\n", encoding="utf-8")
        result = self.lab.run(
            "harness-open", "--dry-run", HARNESS_OPEN_CONFIG=str(broken),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed (status 1)", result.stderr)

        survives = self.lab.root / "noisy.conf"
        survives.write_text("false\nHARNESS_HOST=conf-host.example\n", encoding="utf-8")
        argv = self._dry_run_env(
            HARNESS_HOST=None, HARNESS_JUMP=None,
            HARNESS_OPEN_CONFIG=str(survives),
        )
        self.assertTrue(argv.endswith("conf-host.example"), argv)

    def test_help_works_without_a_usable_config_or_host(self):
        for env in (
            {"HARNESS_OPEN_CONFIG": "/etc/hosts"},
            {"HARNESS_HOST": None, "HARNESS_JUMP": None},
        ):
            with self.subTest(env=sorted(env)):
                result = self.lab.run("harness-open", "--help", **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--two-hop", result.stdout)

    def test_refuses_to_run_without_a_host(self):
        result = self.lab.run("harness-open", "--dry-run", HARNESS_HOST=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no lab host", result.stderr)
        self.assertIn("HARNESS_HOST", result.stderr)

    def test_a_private_config_file_supplies_the_host_and_jump(self):
        config = self.lab.root / "harness-open.conf"
        config.write_text(
            "HARNESS_HOST=conf-host.example\nHARNESS_JUMP=conf-jump.example\n",
            encoding="utf-8",
        )
        argv = self._dry_run_env(
            HARNESS_HOST=None, HARNESS_JUMP=None, HARNESS_OPEN_CONFIG=str(config),
        )
        self.assertTrue(argv.endswith("-J conf-jump.example conf-host.example"), argv)

        # The environment wins over the file, and an option over both.
        argv = self._dry_run_env(HARNESS_OPEN_CONFIG=str(config))
        self.assertTrue(argv.endswith(f"-J {JUMP_HOST} {LAB_HOST}"), argv)
        argv = self._dry_run_env(
            "--host", "opt-host.example", HARNESS_OPEN_CONFIG=str(config),
        )
        self.assertTrue(argv.endswith(f"-J {JUMP_HOST} opt-host.example"), argv)

    def test_an_empty_jump_in_the_environment_beats_the_config_file(self):
        config = self.lab.root / "harness-open.conf"
        config.write_text("HARNESS_JUMP=conf-jump.example\n", encoding="utf-8")
        argv = self._dry_run_env(HARNESS_JUMP="", HARNESS_OPEN_CONFIG=str(config))
        self.assertNotIn("-J", argv)
        self.assertTrue(argv.endswith(LAB_HOST), argv)

    def test_two_hop(self):
        argv, _ = self.dry_run("--two-hop", "--host", "lab1", "--jump", "bastion",
                               "--port", "9001", "--jump-socket", "/tmp/h.sock")
        self.assertEqual(argv, (
            "ssh -t -o RemoteCommand=none -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
            "-L 127.0.0.1:9001:/tmp/h.sock bastion "
            "ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
            "-o StreamLocalBindMask=0177 -o StreamLocalBindUnlink=yes "
            "-L /tmp/h.sock:127.0.0.1:9001 lab1"
        ))

    def test_two_hop_default_socket_is_per_jump_user(self):
        argv, _ = self.dry_run("--two-hop", "--jump", "bastion")
        self.assertIn("-L 127.0.0.1:8765:/tmp/ubertooth-harness-alice-8765.sock bastion", argv)
        self.assertNotRegex(argv, r"127\.0\.0\.1:\d+:127\.0\.0\.1:\d+ bastion")
        for bad in ("relative.sock", "/tmp/a b.sock"):
            result = self.lab.run("harness-open", "--dry-run", "--two-hop",
                                  "--jump-socket", bad)
            self.assertNotEqual(result.returncode, 0, bad)

    def test_no_jump_and_local_port(self):
        result = self.lab.run("harness-open", "--dry-run", "--jump", "", "--local-port", "9999")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv, url = result.stdout.splitlines()
        self.assertEqual(argv, (
            "ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
            f"-L 127.0.0.1:9999:127.0.0.1:8765 {LAB_HOST}"
        ))
        self.assertEqual(url, "http://127.0.0.1:9999/ui/")
        self.assertIn("refuses Host: 127.0.0.1:9999", result.stderr)

    def test_never_widens_the_forward(self):
        # Both the laptop's own tunnel and the one a Mac runner's LaunchAgent
        # keeps up must stay loopback to loopback.
        for script in (HARNESS_OPEN, DEPLOY):
            text = script.read_text(encoding="utf-8")
            self.assertNotIn("GatewayPorts", text, script.name)
            self.assertNotIn("0.0.0.0", text, script.name)
            for line in text.splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertNotRegex(line, r"(^|\s)-g(\s|$)", script.name)
        text = HARNESS_OPEN.read_text(encoding="utf-8")
        for args in ((), ("--two-hop",), ("--jump", "")):
            argv = self.dry_run(*args)[0].split()
            self.assertNotIn("-g", argv)
            forwards = [argv[i + 1] for i, item in enumerate(argv) if item == "-L"]
            self.assertTrue(forwards)
            for forward in forwards:
                # Loopback TCP to loopback TCP, or to/from an owner-only
                # Unix socket on the jump host; never a TCP port there.
                self.assertRegex(
                    forward,
                    r"^(127\.0\.0\.1:\d+:(127\.0\.0\.1:\d+|/tmp/[^:]+\.sock)"
                    r"|/tmp/[^:]+\.sock:127\.0\.0\.1:\d+)$",
                )

    def test_rejects_option_like_or_odd_hosts(self):
        for args in (
            ("--host", "-oProxyCommand=touch x"),
            ("--jump", "a;b"),
            ("--host", ""),
            ("--port", "0"),
            ("--local-port", "70000"),
            ("--two-hop", "--jump", ""),
        ):
            result = self.lab.run("harness-open", "--dry-run", *args)
            self.assertNotEqual(result.returncode, 0, args)

    def _start(self, port, *args, **env):
        _write_exe(self.lab.bin / "ssh", FAKE_SSH.format(python=sys.executable))
        return subprocess.Popen(
            ["bash", str(self.lab.script("harness-open")), "--port", str(port), "--wait", "20", *args],
            env={**self.lab.env, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_opens_browser_once_the_tunnel_answers(self):
        for uname, opener in (("Darwin", "open"), ("Linux", "xdg-open")):
            with self.subTest(uname=uname):
                self.lab.clear_log()
                self._check_opens(uname, opener)

    def _check_opens(self, uname, opener):
        port = _free_port()
        url = f"http://127.0.0.1:{port}/ui/"
        process = self._start(port, FAKE_UNAME=uname)
        try:
            opened = self._wait_for(
                lambda: any(c.endswith(f"open {url}") for c in self.lab.commands())
            )
            self.assertTrue(opened, self.lab.commands())
            self.assertIsNone(process.poll(), "the tunnel stays in the foreground")
        finally:
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=20)
        commands = self.lab.commands()
        self.assertIn(
            f"ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
            f"-L 127.0.0.1:{port}:127.0.0.1:{port} -J {JUMP_HOST} {LAB_HOST}",
            commands,
        )
        self.assertIn(f"{opener} {url}", commands)
        self.assertIn("tunnel up", stderr)

    def test_ssh_failure_opens_no_browser(self):
        port = _free_port()
        process = self._start(port, FAKE_SSH_EXIT="255")
        # Reap first: the browser helper watches the tunnel's PID, which stays
        # a zombie (still "alive" to kill -0) until it is waited for.
        self.assertEqual(process.wait(timeout=20), 255)
        _, stderr = process.communicate(timeout=10)
        time.sleep(1)
        self.assertFalse([c for c in self.lab.commands() if "open " in c])

    def test_refuses_a_local_port_already_in_use(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            result = self.lab.run("harness-open", "--port", str(port))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in use", result.stderr)
        self.assertEqual(self.lab.commands(), [])


class ScriptLintTest(unittest.TestCase):
    def test_bash_syntax(self):
        for script in SHELL_FILES:
            result = subprocess.run(
                ["bash", "-n", str(script)], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_executable_with_strict_mode(self):
        for script in ALL_SCRIPTS:
            self.assertTrue(os.access(script, os.X_OK), script.name)
            text = script.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/usr/bin/env bash\n"), script.name)
            self.assertIn("\nset -euo pipefail\n", text)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck is not installed")
    def test_shellcheck(self):
        result = subprocess.run(
            ["shellcheck", *map(str, SHELL_FILES)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
