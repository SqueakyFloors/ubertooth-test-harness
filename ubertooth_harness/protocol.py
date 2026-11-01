"""Versioned controller <-> target runner wire protocol.

Every message is one JSON object in one WebSocket text frame:

    {"version": 4, "type": "<type>", ...fields}

Each type has an exact field set. Decoding is strict: a message that is not a
JSON object, has an unknown version or type, lacks a field, carries an extra
field, or has a field of the wrong type or shape is rejected with
ProtocolError and never acted on. Encoding applies the same schema, so the
runner cannot emit a message the controller would reject.

Run events are carried unchanged in the ``event`` message: the object is the
run's ``RunEvent`` record exactly as written to run.jsonl, with its own
``schema_version``. The run directory remains the source of truth.
"""

import json
import math
import re
from typing import Any, Callable, Mapping


# Version 2 added ``commit`` to the run message (version 1 runners resolved
# the ref name in their own checkout); version 3 added ``flash_build``;
# version 4 added the ``run_file`` request and its two answers.
PROTOCOL_VERSION = 4
# Bounds what the runner accepts from the controller. Outbound events may be
# larger (the manifest record is bounded at 1 MiB).
MAX_INBOUND_MESSAGE_BYTES = 64 * 1024
# Bounds what the controller accepts from a runner: one event message carries
# one run.jsonl record, and the manifest record alone may reach 1 MiB.
MAX_RUNNER_MESSAGE_BYTES = 4 * 1024 * 1024
# The most bytes of a saved output file one ``run_file`` answer may carry.
# The page travels as JSON text, where a byte that did not decode can cost
# six characters (``\uFFFD``), so this keeps the largest answer well inside
# MAX_RUNNER_MESSAGE_BYTES.
MAX_FILE_PAGE_BYTES = 256 * 1024
# What a path in a ``run_file`` request may be; the real confinement is
# ``runfiles.confined`` on the side that opens the file.
MAX_FILE_PATH_CHARS = 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A full SHA-1 or SHA-256 commit ID, lower case.
_COMMIT_ID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_MAX_TEXT = 4096


class ProtocolError(ValueError):
    """A message does not match the protocol schema."""


Validator = Callable[[Any], bool]


def _identifier(value) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def _name(value) -> bool:
    return isinstance(value, str) and bool(_NAME_RE.fullmatch(value))


def _text(value) -> bool:
    return isinstance(value, str) and len(value) <= _MAX_TEXT


def _line(value) -> bool:
    """Non-empty single printable line that cannot be read as an option."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and value.isprintable()
        and not value.startswith("-")
        and value == value.strip()
    )


def _git_ref(value) -> bool:
    return _line(value) and not any(char.isspace() for char in value)


def commit_id(value) -> bool:
    """A full lower-case SHA-1 or SHA-256 commit ID."""
    return isinstance(value, str) and bool(_COMMIT_ID_RE.fullmatch(value))


def _optional(check: Validator) -> Validator:
    return lambda value: value is None or check(value)


def _integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_negative_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _non_negative_integer(value) -> bool:
    return _integer(value) and value >= 0


def _boolean(value) -> bool:
    return isinstance(value, bool)


def _string_map(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and (item is None or _text(item))
        for key, item in value.items()
    )


def _serials(value) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value)


def _test_names(value) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 100
        and len(set(value)) == len(value)
        and all(_line(item) for item in value)
    )


def _run_event(value) -> bool:
    return (
        isinstance(value, dict)
        and _integer(value.get("schema_version"))
        and _identifier(value.get("run_id"))
        and _integer(value.get("sequence"))
        and isinstance(value.get("event_type"), str)
    )


def _recorded_path(value) -> bool:
    """A path a run recorded for one of its saved output files.

    Every such path is relative to the run directory and uses forward
    slashes. This only keeps a message that cannot name a readable file from
    being carried at all; whether the path may be opened is decided by
    ``runfiles.confined`` on the side that holds the run directory.
    """
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_FILE_PATH_CHARS
        and not value.startswith("/")
        and "\x00" not in value
        and "\n" not in value
    )


def _page_limit(value) -> bool:
    return _integer(value) and 0 < value <= MAX_FILE_PAGE_BYTES


def _page_text(value) -> bool:
    # A page is at most MAX_FILE_PAGE_BYTES bytes of the file, and no byte
    # decodes to more than one character, so this bounds the text as well.
    return isinstance(value, str) and len(value) <= MAX_FILE_PAGE_BYTES


# One page of a saved output file: what the run recorded about the file
# (``runfiles.read_output_page``), then where this page sits in it.
_FILE_PAGE_FIELDS: Mapping[str, Validator] = {
    "path": _recorded_path,
    "stream": _text,
    "bytes": _non_negative_integer,
    "lines": _non_negative_integer,
    "sha256": _text,
    "file_bytes": _non_negative_integer,
    "offset": _non_negative_integer,
    "length": _non_negative_integer,
    "eof": _boolean,
    "page_bytes": _page_limit,
    "text": _page_text,
}


def _file_page(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(_FILE_PAGE_FIELDS)
        and all(check(value[field]) for field, check in _FILE_PAGE_FIELDS.items())
    )


def _refusal_status(value) -> bool:
    """The HTTP status the controller answers a refused file request with."""
    return value in (400, 404, 503) and _integer(value)


# Controller -> runner.
CONTROLLER_MESSAGES: Mapping[str, Mapping[str, Validator]] = {
    # Start one run. ``commit`` is the commit the controller listed for
    # ``ref`` (git ls-remote of the configured repository); the runner runs
    # exactly that commit, which must be in its Ubertooth checkout. ``commit``
    # is null only when the controller could not list the refs; the runner
    # then resolves ``ref`` in its own checkout. ``plan`` names
    # <plans dir>/<plan>.yaml; ``tests`` selects exact test names (empty: the
    # whole suite); ``build`` builds the resolved commits into the run
    # directory first; ``flash_build`` (meaningful only with ``build``) then
    # flashes the run's boards with that build's firmware before preflight.
    "run": {
        "request_id": _identifier,
        "ref": _git_ref,
        "commit": _optional(commit_id),
        "plan": _name,
        "suite": _name,
        "tests": _test_names,
        "build": _boolean,
        "flash_build": _boolean,
    },
    # Cooperatively cancel the active run.
    "cancel": {
        "run_id": _identifier,
        "reason": _text,
    },
    # One page of a saved output file of a run this runner carried out. The
    # output/ files stay in the runner's own run directory, so this is how
    # the controller's GUI opens one. ``path`` must be a path that run's own
    # run.jsonl records; ``offset`` and ``limit`` are in bytes, so a page of
    # a soak's output costs the same as a page of a short answer.
    "run_file": {
        "request_id": _identifier,
        "run_id": _identifier,
        "path": _recorded_path,
        "offset": _non_negative_integer,
        "limit": _page_limit,
    },
}

# Runner -> controller.
RUNNER_MESSAGES: Mapping[str, Mapping[str, Validator]] = {
    # First message on every connection.
    "register": {
        "host": _name,
        "hostname": _text,
        "os": _string_map,
        "python": _text,
        # Null when enumeration is unavailable (see enumeration_error).
        "ubertooth_serials": _optional(_serials),
        "enumeration_error": _optional(_text),
        "heartbeat_interval_s": _non_negative_number,
        # Run still executing or not yet reported; its events are replayed
        # from the start on this connection.
        "active_run_id": _optional(_identifier),
    },
    "heartbeat": {
        "active_run_id": _optional(_identifier),
        "elapsed_s": _optional(_non_negative_number),
        "last_event_sequence": _optional(_integer),
        "last_event_type": _optional(_text),
    },
    "run_accepted": {
        "request_id": _identifier,
        "run_id": _identifier,
    },
    "run_rejected": {
        "request_id": _optional(_identifier),
        "reason": _text,
    },
    "cancel_accepted": {
        "run_id": _identifier,
    },
    "cancel_rejected": {
        "run_id": _optional(_identifier),
        "reason": _text,
    },
    # One run.jsonl record, unchanged, in run.jsonl order.
    "event": {
        "run_id": _identifier,
        "event": _run_event,
    },
    # The run wrote its terminal run_finished record; status is that
    # record's status (passed, failed, canceled, ...).
    "run_finished": {
        "request_id": _identifier,
        "run_id": _identifier,
        "status": _name,
        "exit_code": _integer,
        "run_directory": _text,
    },
    # The run ended without a run_finished record (for example, it was
    # refused before its run directory was created).
    "run_error": {
        "request_id": _identifier,
        "run_id": _identifier,
        "exit_code": _optional(_integer),
        "reason": _text,
    },
    # The page a run_file request asked for: what the run recorded about the
    # file, including the size it recorded, and the bytes of this page.
    "run_file_page": {
        "request_id": _identifier,
        "run_id": _identifier,
        "page": _file_page,
    },
    # The runner will not serve that file: it has no such run, the run
    # recorded no such file, the path is not one it may open, or it is
    # already answering as many requests as it serves at once. ``reason``
    # names the path only, never where the run directory is on the target.
    "run_file_refused": {
        "request_id": _identifier,
        "run_id": _identifier,
        "status": _refusal_status,
        "reason": _text,
    },
    # The controller sent a message the runner rejected; nothing was done.
    "protocol_error": {
        "reason": _text,
    },
}


def _validate(message, schemas: Mapping[str, Mapping[str, Validator]]) -> dict:
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    version = message.get("version")
    if not _integer(version) or version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version!r}; expected {PROTOCOL_VERSION}"
        )
    message_type = message.get("type")
    if not isinstance(message_type, str) or message_type not in schemas:
        raise ProtocolError(f"unknown message type {message_type!r}")
    schema = schemas[message_type]
    fields = set(message) - {"version", "type"}
    missing = sorted(set(schema) - fields)
    if missing:
        raise ProtocolError(f"{message_type}: missing field(s) {', '.join(missing)}")
    extra = sorted(fields - set(schema))
    if extra:
        raise ProtocolError(f"{message_type}: unknown field(s) {', '.join(extra)}")
    for field, check in schema.items():
        if not check(message[field]):
            raise ProtocolError(f"{message_type}: invalid {field}")
    return message


def _decode(text, schemas, max_bytes: int) -> dict:
    if isinstance(text, (bytes, bytearray)):
        raise ProtocolError("binary frames are not accepted")
    if not isinstance(text, str):
        raise ProtocolError("message must be a text frame")
    if len(text.encode("utf-8")) > max_bytes:
        raise ProtocolError("message too large")
    try:
        message = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as error:
        raise ProtocolError(f"message is not valid JSON: {error}") from error
    return _validate(message, schemas)


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def decode_controller_message(text) -> dict:
    """Strictly decode one controller -> runner message."""
    return _decode(text, CONTROLLER_MESSAGES, MAX_INBOUND_MESSAGE_BYTES)


def decode_runner_message(text) -> dict:
    """Strictly decode one runner -> controller message (controller side)."""
    return _decode(text, RUNNER_MESSAGES, MAX_RUNNER_MESSAGE_BYTES)


def _encode(message_type: str, fields: Mapping, schemas) -> str:
    message = {"version": PROTOCOL_VERSION, "type": message_type, **fields}
    _validate(message, schemas)
    return json.dumps(message, sort_keys=True, allow_nan=False)


def encode_runner_message(message_type: str, **fields) -> str:
    """Encode one runner -> controller message, validated against the schema."""
    return _encode(message_type, fields, RUNNER_MESSAGES)


def encode_controller_message(message_type: str, **fields) -> str:
    """Encode one controller -> runner message (controller side and tests)."""
    return _encode(message_type, fields, CONTROLLER_MESSAGES)
