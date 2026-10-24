"""Case types shared by catalog tests and the sequencer."""

from dataclasses import dataclass
import re
from typing import Mapping


# Role names for the other registered Ubertooths on the host that a guarded
# test binds and must leave unchanged; the plan-selected board keeps the
# "Ubertooth" role.
GUARD_ROLE_PREFIX = "guard Ubertooth "


def guard_roles(resources: Mapping) -> tuple[str, ...]:
    """Guard-board roles of an invocation, in binding order."""
    return tuple(
        sorted(
            (role for role in resources if role.startswith(GUARD_ROLE_PREFIX)),
            key=lambda role: int(role.removeprefix(GUARD_ROLE_PREFIX)),
        )
    )


@dataclass(frozen=True)
class CommandCase:
    case_id: str
    name: str
    argv: tuple[str, ...]
    output_patterns: tuple[str, ...] = ()
    timeout_s: float = 5.0
    expected_returncodes: tuple[int, ...] = (0,)
    # The tool is built only on some hosts (for example the BlueZ tools): a
    # tool the executor cannot start skips the case instead of failing it.
    optional_tool: bool = False
    # Exit status by which the command says it cannot run on this host (for
    # example pytools.py without a Python-tool build); its "SKIP: <reason>"
    # output line is the skip reason.
    skip_returncode: int | None = None

    def missing_tool(self, result) -> str:
        """The skip reason when the case cannot run on this host, else ''."""
        if (
            self.skip_returncode is not None
            and result.returncode == self.skip_returncode
            and not result.timed_out
            and not result.cancelled
        ):
            match = re.search(r"(?m)^SKIP: (.+)$", result.stdout)
            if match:
                return f"{self.name}: skipped, {match[1].strip()}"
        if (
            self.optional_tool
            and result.returncode == 127
            and "unable to start command" in result.stderr
            # Only a missing file; a present but unusable tool must fail.
            and "No such file or directory" in result.stderr
        ):
            return f"{self.name}: skipped, {self.argv[0]} is not built on this host"
        return ""

    def result_failure(self, result) -> str:
        if result.timed_out:
            return f"{self.name}: command timed out"
        if result.cancelled:
            return f"{self.name}: command canceled"
        if result.returncode not in self.expected_returncodes:
            return f"{self.name}: command exited {result.returncode}"
        output = result.stdout + result.stderr
        for pattern in self.output_patterns:
            if not re.search(pattern, output):
                return f"{self.name}: expected output pattern {pattern!r} was not found"
        return ""


@dataclass(frozen=True)
class FlashCase:
    """Flash reliability: run by the sequencer's flash runner, not as argv."""

    case_id: str
    name: str
    arguments: object
    serial: str
