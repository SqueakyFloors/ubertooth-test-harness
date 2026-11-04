"""Steps that run one of the harness's own helper scripts.

A helper script (dfuimage.py, invalidapp.py) is run with this interpreter,
prints short summary lines, and exits 0 when its check holds and 1 when it
does not. The step below turns that into a transition step: the line the
helper prints when the check holds is the expected pattern, and its exit 1 is
reported with the helper's own lines, so the failure reason in the run record
is what the helper said rather than "command exited 1".
"""

from pathlib import Path
import re
import sys
from typing import Callable, Sequence

from ubertooth_harness.outputfiles import search_lines, text_lines
from ubertooth_harness.stateful import CAPTURE_DIR_KEY, Context, Step, require

# A line of a command record a helper prints (rawlog format, UTC stamp): it
# belongs to a command the helper ran, not to the helper's own verdict.
_RECORD_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} \[")


def script_path(module) -> str:
    """The helper module's file, run as a script."""
    return str(Path(module.__file__).resolve())


def resolved(parts: Sequence, context: Context) -> tuple[str, ...]:
    """Command parts with every callable resolved from the case context."""
    return tuple(
        str(part(context) if callable(part) else part) for part in parts
    )


def helper_argv(script: str, *parts) -> Callable[[Context], tuple[str, ...]]:
    """Argv of a helper command; callable parts come from the context."""
    def argv(context: Context) -> tuple[str, ...]:
        return (sys.executable, script) + resolved(parts, context)
    return argv


def tool_argv(*parts) -> Callable[[Context], tuple[str, ...]]:
    """Argv of a host tool; callable parts come from the context."""
    def argv(context: Context) -> tuple[str, ...]:
        return resolved(parts, context)
    return argv


def case_file(name: str) -> Callable[[Context], str]:
    """A file in the case's own directory under the run's captures/."""
    return lambda context: str(Path(require(context, CAPTURE_DIR_KEY)) / name)


def helper_check(pattern: str) -> Callable:
    """Output check for a helper step: the verdict line, or the helper's lines."""
    def check(output) -> str:
        if search_lines(output, pattern):
            return ""
        # Command records stay in the transcript; the reason keeps the
        # helper's own lines.
        lines = [line.strip() for line in text_lines(output)
                 if line.strip() and not _RECORD_LINE.match(line)]
        if not lines:
            return "no output"
        # The helper prints its verdict last; the earlier lines are evidence.
        detail = f" ({'; '.join(lines[:-1])})" if len(lines) > 1 else ""
        return (lines[-1] + detail)[:600]

    return check


def helper_step(step_id: str, name: str, script: str, *parts, pattern: str,
                timeout_s: float = 10.0, mutates: bool = False) -> Step:
    """A step that runs a helper command and requires its verdict line."""
    return Step(step_id, name, helper_argv(script, *parts), timeout_s=timeout_s,
                expected_returncodes=(0, 1), output_check=helper_check(pattern),
                mutates=mutates)
