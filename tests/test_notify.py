"""Opt-in completion notices: configuration, message, delivery, dedupe (#392).

Nothing here reaches a real relay. The SMTP tests talk to a fake relay on
127.0.0.1 (``tests/fake_smtp.py``) so the assertions are about the bytes that
really went out; the sendmail tests run a script in a temporary directory,
which is also how "argv and stdin, never a shell" is checked.
"""

import asyncio
import contextlib
import dataclasses
import email
import email.policy
import io
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from ubertooth_harness.cli import (
    HarnessError,
    _notifier,
    _notify_config_path,
    main,
)
from ubertooth_harness.controller import Controller
from ubertooth_harness.notify import (
    LEDGER_DIR,
    PUBLICATIONS_DIR,
    FAILED,
    Notice,
    NotifyConfig,
    NotifyConfigError,
    NotifyError,
    Notifier,
    SENDING,
    SENT,
    SendmailTransport,
    SmtpTransport,
    build_message,
    default_config_path,
    digest_events,
    duration_text,
    group_notice,
    header_text,
    load_config,
    parse_config,
    read_publication,
    resolve_password,
    run_notice,
    sample_notice,
)

try:
    from fake_smtp import FakeSmtpServer
except ImportError:  # run as tests.test_notify
    from tests.fake_smtp import FakeSmtpServer


SENDER = "harness-notices@example.invalid"
RECIPIENT = "bench-owner@example.invalid"

RUN = {
    "run_id": "20260101T120000Z-0123456789ab",
    "target": "Host_Linux",
    "status": "failed",
    "plan": "release",
    "suite": "release",
    "created_at": "2026-01-01T12:00:00.000+00:00",
    "finished_at": "2026-01-01T13:12:05.000+00:00",
    "reason": "Raw receive: no packets",
    "runner_run_directory": "/results/20260101T120000Z-0123456789ab",
    "group_id": None,
}

EVENTS = [
    {
        "event_type": "manifest",
        "status": "running",
        "details": {"manifest": {"sources": [
            {
                "name": "ubertooth", "commit": "a" * 40, "ref_name": "main",
                "requested_ref": "a" * 40, "build_version": "2026-01-DEV1+7",
                "repository": "https://example.invalid/ubertooth.git",
            },
            {
                "name": "libbtbb", "commit": "b" * 40,
                "ref_name": "fork-pin", "requested_ref": "fork-pin",
            },
        ]}},
    },
    {"event_type": "test_finished", "status": "passed"},
    {"event_type": "test_finished", "status": "passed"},
    {"event_type": "test_finished", "status": "failed"},
    {"event_type": "test_finished", "status": "skipped"},
    {
        "event_type": "run_finished",
        "status": "failed",
        "details": {
            "requested_tests": 5, "completed_tests": 4, "blocked_tests": 0,
            "failure_reason": "Raw receive: no packets",
        },
    },
]


def smtp_config(server, **overrides) -> NotifyConfig:
    """A configuration pointing at a fake relay, through the real validator."""
    data = {
        "enabled": True,
        "sender": SENDER,
        "recipients": [RECIPIENT],
        "smtp": {"host": server.host, "port": server.port, "tls": "none"},
        "attempts": 1,
        "retry_delay_s": 0,
        # Generous: a loaded CI runner can be slow even on loopback, and a
        # timeout here would look like a delivery failure.
        "timeout_s": 60,
        "gui_base_url": "http://127.0.0.1:8765",
    }
    data.update(overrides)
    return parse_config(data)


def parsed(data):
    """One built message, read back with the modern policy."""
    if isinstance(data, str):
        data = data.encode()
    return email.message_from_bytes(data, policy=email.policy.default)


class CollectLog:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, message: str) -> None:
        self.lines.append(str(message))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# ----- configuration -------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_nothing_configured_is_disabled(self):
        self.assertFalse(parse_config({}).enabled)
        self.assertFalse(load_config(None).enabled)
        self.assertFalse(NotifyConfig().enabled)

    def test_default_path_is_outside_the_repository(self):
        path = default_config_path({"XDG_CONFIG_HOME": "/private/config"})
        self.assertEqual(path, Path("/private/config/ubertooth-harness/notify.yaml"))
        self.assertNotIn(
            str(Path(__file__).resolve().parents[1]), str(default_config_path({}))
        )

    def test_a_disabled_file_needs_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notify.yaml"
            path.write_text("enabled: false\n", encoding="utf-8")
            self.assertFalse(load_config(path).enabled)

    def test_unknown_settings_are_refused(self):
        for data, word in (
            ({"enabled": True, "tls": "none"}, "unknown setting"),
            ({"smtp": {"tsl": "starttls"}}, "unknown setting"),
            ({"sendmail": {"binary": "/usr/sbin/sendmail"}}, "unknown setting"),
        ):
            with self.subTest(data=data):
                with self.assertRaisesRegex(NotifyConfigError, word):
                    parse_config(data)

    def test_enabled_needs_a_sender_recipients_and_a_relay(self):
        base = {"enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {"host": "relay.example.invalid"}}
        self.assertTrue(parse_config(base).enabled)
        with self.assertRaisesRegex(NotifyConfigError, "recipients"):
            parse_config({**base, "recipients": []})
        with self.assertRaisesRegex(NotifyConfigError, "smtp.host is required"):
            parse_config({**base, "smtp": {}})
        with self.assertRaisesRegex(NotifyConfigError, "sender"):
            parse_config({**base, "sender": ""})

    def test_an_address_with_a_header_in_it_is_refused(self):
        for bad in (
            "you@example.invalid\nBcc: someone@example.invalid",
            "you@example.invalid\r\nSubject: hijacked",
            "You <you@example.invalid>",
            "one@example.invalid, two@example.invalid",
            "no-at-sign",
        ):
            with self.subTest(address=bad):
                with self.assertRaises(NotifyConfigError):
                    parse_config({
                        "enabled": True, "sender": SENDER, "recipients": [bad],
                        "smtp": {"host": "relay.example.invalid"},
                    })
                with self.assertRaises(NotifyConfigError):
                    parse_config({
                        "enabled": True, "sender": bad, "recipients": [RECIPIENT],
                        "smtp": {"host": "relay.example.invalid"},
                    })

    def test_authenticated_smtp_requires_tls(self):
        data = {
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {
                "host": "relay.example.invalid", "tls": "none",
                "username": SENDER,
                "password_env": "UBERTOOTH_HARNESS_SMTP_PASSWORD",
            },
        }
        with self.assertRaisesRegex(NotifyConfigError, "requires smtp.tls"):
            parse_config(data)
        ok = parse_config({**data, "smtp": {**data["smtp"], "tls": "starttls"}})
        self.assertEqual(ok.tls, "starttls")

    def test_cleartext_only_for_a_relay_on_this_machine(self):
        data = {
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {"host": "relay.example.invalid", "tls": "none"},
        }
        with self.assertRaisesRegex(NotifyConfigError, "only allowed"):
            parse_config(data)
        self.assertEqual(
            parse_config({**data, "smtp": {"host": "127.0.0.1", "tls": "none"}}).tls,
            "none",
        )

    def test_the_password_variable_must_be_purpose_specific(self):
        def build(name):
            return {
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {
                    "host": "relay.example.invalid", "username": SENDER,
                    "password_env": name,
                },
            }

        with self.assertRaisesRegex(NotifyConfigError, "must start with"):
            parse_config(build("SMTP_PASSWORD"))
        with self.assertRaisesRegex(NotifyConfigError, "upper-case"):
            parse_config(build("ubertooth_harness_smtp_password"))
        self.assertEqual(
            parse_config(build("UBERTOOTH_HARNESS_SMTP_PASSWORD")).password_env,
            "UBERTOOTH_HARNESS_SMTP_PASSWORD",
        )

    def test_exactly_one_password_source_with_a_username(self):
        def build(**smtp):
            return {
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {"host": "relay.example.invalid", **smtp},
            }

        with self.assertRaisesRegex(NotifyConfigError, "exactly one"):
            parse_config(build(username=SENDER))
        with self.assertRaisesRegex(NotifyConfigError, "exactly one"):
            parse_config(build(
                username=SENDER, password_env="UBERTOOTH_HARNESS_SMTP_PASSWORD",
                password_file="/private/secret",
            ))
        with self.assertRaisesRegex(NotifyConfigError, "need smtp.username"):
            parse_config(build(password_env="UBERTOOTH_HARNESS_SMTP_PASSWORD"))

    def test_a_world_writable_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notify.yaml"
            path.write_text("enabled: false\n", encoding="utf-8")
            os.chmod(path, 0o666)
            with self.assertRaisesRegex(NotifyConfigError, "writable by others"):
                load_config(path)

    def test_a_readable_password_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "smtp-password"
            secret.write_text("hunter2\n", encoding="utf-8")
            os.chmod(secret, 0o644)
            path = Path(directory) / "notify.yaml"
            path.write_text(textwrap.dedent(f"""\
                enabled: true
                sender: {SENDER}
                recipients: [{RECIPIENT}]
                smtp:
                  host: relay.example.invalid
                  tls: starttls
                  username: {SENDER}
                  password_file: {secret}
                """), encoding="utf-8")
            with self.assertRaisesRegex(NotifyConfigError, "readable or writable"):
                load_config(path)
            os.chmod(secret, 0o600)
            config = load_config(path)
            self.assertEqual(resolve_password(config), "hunter2")

    def test_a_missing_password_says_which_source_was_empty(self):
        config = parse_config({
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {
                "host": "relay.example.invalid", "username": SENDER,
                "password_env": "UBERTOOTH_HARNESS_SMTP_PASSWORD",
            },
        })
        with self.assertRaisesRegex(NotifyError, "UBERTOOTH_HARNESS_SMTP_PASSWORD"):
            resolve_password(config, {})

    def test_the_description_never_holds_a_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "smtp-password"
            secret.write_text("hunter2\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            config = parse_config({
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {
                    "host": "relay.example.invalid", "username": SENDER,
                    "password_file": str(secret),
                },
            })
            for text in (config.describe(), repr(config), str(dataclasses.asdict(config))):
                self.assertNotIn("hunter2", text)
            self.assertIn(str(secret), config.describe())

    def test_the_gui_link_must_be_loopback(self):
        for bad in (
            "http://lab-host.example:8765", "https://127.0.0.1:8765",
            "http://127.0.0.1:8765/ui/", "127.0.0.1:8765",
            # A name is resolved when it is used, so it is not an address.
            "http://localhost:8765",
        ):
            with self.subTest(url=bad):
                with self.assertRaisesRegex(NotifyConfigError, "gui_base_url"):
                    parse_config({"gui_base_url": bad})
        self.assertEqual(
            parse_config({"gui_base_url": "http://127.0.0.1:9000"}).gui_base_url,
            "http://127.0.0.1:9000",
        )

    def test_cleartext_needs_a_literal_loopback_address(self):
        # `localhost` resolves when the connection is made, so it is not the
        # "relay on this machine" that `tls: none` is allowed for.
        for host in ("localhost", "relay.example.invalid", "127.0.0.1.nip.io"):
            with self.subTest(host=host):
                with self.assertRaisesRegex(NotifyConfigError, "only allowed"):
                    parse_config({
                        "enabled": True, "sender": SENDER,
                        "recipients": [RECIPIENT],
                        "smtp": {"host": host, "tls": "none"},
                    })

    def test_sendmail_must_be_an_absolute_executable_path(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "sendmail"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            base = {
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "transport": "sendmail",
            }
            with self.assertRaisesRegex(NotifyConfigError, "sendmail.path is required"):
                parse_config(base)
            with self.assertRaisesRegex(NotifyConfigError, "must be absolute"):
                parse_config({**base, "sendmail": {"path": "sendmail"}})
            with self.assertRaisesRegex(NotifyConfigError, "not executable"):
                parse_config({**base, "sendmail": {"path": str(script)}})
            os.chmod(script, 0o777)
            with self.assertRaisesRegex(NotifyConfigError, "writable by others"):
                parse_config({**base, "sendmail": {"path": str(script)}})
            os.chmod(script, 0o700)
            self.assertEqual(
                parse_config({**base, "sendmail": {"path": str(script)}}).sendmail_path,
                str(script),
            )

    def test_bounds_are_enforced(self):
        for data in (
            {"attempts": 99}, {"attempts": 0}, {"retry_delay_s": 10_000},
            {"timeout_s": 0}, {"smtp": {"port": 0}}, {"smtp": {"port": 70000}},
        ):
            with self.subTest(data=data):
                with self.assertRaises(NotifyConfigError):
                    parse_config(data)

    def test_the_ehlo_name_must_be_a_host_name(self):
        def build(name):
            return {
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {"host": "relay.example.invalid", "ehlo_name": name},
            }

        for bad in ("bench example com", "bench\nEHLO evil", "bench;rm -rf /"):
            with self.subTest(name=bad):
                with self.assertRaises(NotifyConfigError):
                    parse_config(build(bad))
        self.assertEqual(
            parse_config(build("bench.example.com")).client_name,
            "bench.example.com",
        )
        # With none configured, the sender's own domain: nothing asks this
        # host what it is called.
        self.assertEqual(parse_config(build("")).client_name, "example.invalid")


# ----- the message ---------------------------------------------------------


class MessageTests(unittest.TestCase):
    def test_a_run_notice_carries_what_a_reader_needs(self):
        notice = run_notice(
            RUN, events=EVENTS, gui_base="http://127.0.0.1:8765",
            results_dir="/controller/runs/20260101T120000Z-0123456789ab",
            subject_prefix="[ubertooth-harness]",
        )
        self.assertEqual(notice.key, f"run-{RUN['run_id']}")
        self.assertEqual(
            notice.subject,
            "[ubertooth-harness] FAILED: release/release run "
            "20260101T120000Z-0123456789ab",
        )
        body = notice.body
        for wanted in (
            "Verdict: failed",
            RUN["run_id"],
            "Host_Linux",
            "Plan / suite: release / release",
            "2026-01-01T12:00:00Z",
            "2026-01-01T13:12:05Z",
            "Duration: 1h 12m 05.000s",
            "2 passed, 1 failed, 1 skipped",
            "(4 of 5 planned tests reached)",
            "Reason: Raw receive: no packets",
            "a" * 40,
            "b" * 40,
            "ref main",
            "/results/20260101T120000Z-0123456789ab",
            "/controller/runs/20260101T120000Z-0123456789ab",
            "http://127.0.0.1:8765/ui/#run=20260101T120000Z-0123456789ab",
        ):
            self.assertIn(wanted, body)

    def test_nothing_is_attached_and_no_log_is_quoted(self):
        data = build_message(
            parse_config({
                "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
                "smtp": {"host": "127.0.0.1", "tls": "none"},
            }),
            run_notice(RUN, events=EVENTS),
        )
        text = data.decode()
        self.assertIn("Content-Type: text/plain", text)
        self.assertNotIn("multipart", text)
        self.assertNotIn("Content-Disposition: attachment", text)
        self.assertNotIn("base64", text)

    def test_a_canceled_run_says_so(self):
        run = {**RUN, "status": "canceled", "reason": "canceled from the web GUI"}
        events = [
            {"event_type": "test_finished", "status": "passed"},
            {
                "event_type": "run_finished", "status": "canceled",
                "details": {"requested_tests": 5, "completed_tests": 1,
                            "failure_reason": "canceled from the web GUI"},
            },
        ]
        notice = run_notice(run, events=events)
        self.assertIn("CANCELED:", notice.subject)
        self.assertIn("Verdict: canceled", notice.body)
        self.assertIn("(1 of 5 planned tests reached)", notice.body)
        self.assertIn("Reason: canceled from the web GUI", notice.body)

    def test_a_run_that_ended_before_its_tests_reports_zero(self):
        notice = run_notice({**RUN, "status": "error", "reason": "build failed"})
        self.assertIn("Tests: no test finished", notice.body)
        self.assertIn("Reason: build failed", notice.body)

    def test_a_reason_with_a_newline_cannot_add_a_header(self):
        run = {
            **RUN,
            "plan": "release\r\nBcc: someone@example.invalid",
            "reason": "line one\nBcc: someone@example.invalid",
        }
        notice = run_notice(run, events=EVENTS, subject_prefix="[harness]")
        self.assertNotIn("\n", notice.subject)
        self.assertNotIn("\r", notice.subject)
        config = parse_config({
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {"host": "127.0.0.1", "tls": "none"},
        })
        data = build_message(config, notice)
        message = parsed(data)
        # The text survives as text, in the subject and the body; no second
        # header has appeared, and the recipients are still only ours.
        self.assertIsNone(message.get("Bcc"))
        self.assertEqual(message.get_all("Subject") and len(message.get_all("Subject")), 1)
        self.assertEqual(message["To"], RECIPIENT)
        self.assertIn("Bcc: someone@example.invalid", message.get_content())

    def test_a_header_value_with_a_newline_is_flattened_by_the_builder(self):
        config = parse_config({
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {"host": "127.0.0.1", "tls": "none"},
        })
        # Whatever a caller hands over, the builder flattens the subject
        # before the header is set, and the SMTP policy stands behind it: a
        # value with a newline in it never reaches a header either way.
        data = build_message(
            config,
            Notice(key="run-x", subject="a\r\nBcc: someone@example.invalid",
                   body="body\n"),
        )
        message = parsed(data)
        self.assertIsNone(message.get("Bcc"))
        self.assertEqual(message["Subject"], "a Bcc: someone@example.invalid")

    def test_header_text_bounds_and_flattens(self):
        self.assertEqual(header_text("a\nb\tc  d"), "a b c d")
        self.assertEqual(header_text("x" * 50, 10), "x" * 7 + "...")
        self.assertEqual(header_text(None), "None")

    def test_the_whole_message_is_plain_ascii_text(self):
        config = parse_config({
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {"host": "127.0.0.1", "tls": "none"},
        })
        data = build_message(config, run_notice(RUN, events=EVENTS))
        self.assertIn(b"Content-Transfer-Encoding: 7bit", data)
        data.decode("ascii")

    def test_duration_of_unrecorded_times(self):
        self.assertEqual(duration_text(None, None), "not recorded")
        self.assertEqual(duration_text("2026-01-01T00:00:00Z", "nonsense"), "not recorded")
        self.assertEqual(duration_text("2026-01-01T00:00:00Z", "2026-01-01T00:00:03Z"),
                         "3.000s")

    def test_a_group_notice_lists_every_host(self):
        group = {
            "group_id": "grp-" + "0" * 32,
            "status": "failed",
            "plan": "release", "suite": "release",
            "created_at": "2026-01-01T12:00:00+00:00",
            "finished_at": "2026-01-01T13:12:05+00:00",
            "hosts_asked": 3, "hosts_started": 2,
            "cancel_requested": False,
            "members": [
                {"host": "Host_Linux", "run_id": RUN["run_id"], "error": None,
                 "status": "failed", "run": RUN},
                {"host": "Host_Mac", "run_id": "20260101T120001Z-0123456789ac",
                 "error": None, "status": "passed",
                 "run": {**RUN, "run_id": "20260101T120001Z-0123456789ac",
                         "target": "Host_Mac", "status": "passed", "reason": None,
                         "runner_run_directory": "/results/mac"}},
                {"host": "Host_Spare", "run_id": None,
                 "error": "target Host_Spare is offline", "status": "error",
                 "run": None},
            ],
        }
        notice = group_notice(
            group,
            {
                RUN["run_id"]: digest_events(EVENTS),
                "20260101T120001Z-0123456789ac": digest_events(EVENTS),
            },
            gui_base="http://127.0.0.1:8765", results_dir="/controller/runs",
            subject_prefix="[harness]",
        )
        self.assertEqual(notice.key, f"group-{group['group_id']}")
        self.assertIn("FAILED: release/release group grp-", notice.subject)
        body = notice.body
        self.assertIn("Hosts: 2 of 3 host(s) ran", body)
        self.assertIn("Host_Linux: failed", body)
        self.assertIn("Host_Mac: passed", body)
        self.assertIn("Host_Spare: no run (target Host_Spare is offline)", body)
        self.assertIn("http://127.0.0.1:8765/ui/#run=20260101T120001Z-0123456789ac", body)
        self.assertIn("a" * 40, body)

    def test_a_group_digest_keeps_only_what_a_notice_needs(self):
        digest = digest_events(EVENTS)
        self.assertEqual(sorted(digest), ["sources", "totals"])
        self.assertEqual(digest["totals"]["counts"],
                         {"passed": 2, "failed": 1, "skipped": 1})
        self.assertEqual(len(digest["sources"]), 2)
        # Small enough that one per host of a sixteen-host group is nothing,
        # which is the point of reducing each run's records as they are read.
        self.assertLess(len(json.dumps(digest)), 2000)

    def test_a_report_that_was_not_written_is_named(self):
        notice = run_notice(
            {**RUN, "report": False,
             "report_error": "could not write report.md: No space left on device"},
            events=EVENTS,
        )
        self.assertIn(
            "Report: report.md was not written (could not write report.md: "
            "No space left on device)",
            notice.body,
        )
        short = run_notice(
            {**RUN, "report": True, "report_error": "could not compile report.md"},
            events=EVENTS,
        )
        self.assertIn("Report: report.md is short: could not compile", short.body)
        self.assertNotIn("Report:", run_notice({**RUN, "report": True}).body)

    def test_a_notice_with_no_gui_port_has_no_link(self):
        self.assertNotIn("/ui/#run=", run_notice(RUN, events=EVENTS).body)

    def test_the_sample_notice_names_no_real_run(self):
        notice = sample_notice("[harness]", "http://127.0.0.1:8765")
        self.assertIn("is not a real run", notice.body)
        self.assertIn("TEST:", notice.subject)


# ----- the public link gate ------------------------------------------------


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.results = Path(self.directory.name)
        (self.results / PUBLICATIONS_DIR).mkdir()
        self.log = CollectLog()
        self.addCleanup(self.directory.cleanup)

    def write(self, identifier, data):
        path = self.results / PUBLICATIONS_DIR / f"{identifier}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_no_record_means_no_public_link(self):
        self.assertIsNone(read_publication(self.results, RUN["run_id"], self.log))
        self.assertEqual(self.log.lines, [])
        notice = run_notice(
            RUN, events=EVENTS,
            public_url=read_publication(self.results, RUN["run_id"], self.log),
        )
        self.assertNotIn("published validation bundle", notice.body)

    def test_a_sanitized_record_is_linked(self):
        url = "https://github.com/example/ubertooth/releases/tag/v1"
        self.write(RUN["run_id"], {"sanitized": True, "url": url})
        self.assertEqual(read_publication(self.results, RUN["run_id"], self.log), url)
        notice = run_notice(RUN, events=EVENTS, public_url=url)
        self.assertIn(f"published validation bundle: {url}", notice.body)

    def test_an_unsanitized_or_wrong_record_is_not_linked(self):
        url = "https://example.invalid/bundle"
        for data in (
            {"url": url},
            {"sanitized": False, "url": url},
            {"sanitized": "yes", "url": url},
            {"sanitized": True},
            {"sanitized": True, "url": "http://example.invalid/bundle"},
            {"sanitized": True, "url": "https://example.invalid/a b"},
            {"sanitized": True, "url": url, "run_id": "another-run"},
            [{"sanitized": True, "url": url}],
        ):
            with self.subTest(record=data):
                self.write(RUN["run_id"], data)
                self.assertIsNone(
                    read_publication(self.results, RUN["run_id"], self.log)
                )

    def test_an_unreadable_record_warns_and_links_nothing(self):
        path = self.results / PUBLICATIONS_DIR / f"{RUN['run_id']}.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_publication(self.results, RUN["run_id"], self.log))
        self.assertIn("unreadable", self.log.text)

    def test_a_record_named_by_something_that_is_not_an_id_is_ignored(self):
        self.assertIsNone(read_publication(self.results, "../escape", self.log))


# ----- delivery through a fake relay ---------------------------------------


class SmtpDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / LEDGER_DIR
        self.log = CollectLog()
        self.addCleanup(self.directory.cleanup)

    def notifier(self, config, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: None)
        return Notifier(config, self.ledger, log=self.log, **kwargs)

    def test_a_notice_reaches_the_relay(self):
        with FakeSmtpServer() as server:
            config = smtp_config(server)
            notice = run_notice(RUN, events=EVENTS, gui_base=config.gui_base_url)
            self.assertTrue(self.notifier(config).send(notice))
        self.assertEqual(len(server.messages), 1)
        message = server.messages[0]
        self.assertEqual(message["sender"], SENDER)
        self.assertEqual(message["recipients"], [RECIPIENT])
        text = message["data"].decode()
        self.assertIn(f"Subject: {notice.subject}", text)
        self.assertIn("Auto-Submitted: auto-generated", text)
        self.assertIn("Verdict: failed", text)
        self.assertIn(RUN["run_id"], text)
        self.assertNotIn("Bcc:", text)
        self.assertEqual(server.errors, [])

    def test_the_relay_sees_no_header_the_run_smuggled_in(self):
        run = {**RUN, "reason": "boom\r\nBcc: someone@example.invalid"}
        with FakeSmtpServer() as server:
            config = smtp_config(server)
            self.assertTrue(self.notifier(config).send(run_notice(run, events=EVENTS)))
        message = parsed(server.messages[0]["data"])
        self.assertIsNone(message.get("Bcc"))
        self.assertIn("Bcc: someone@example.invalid", message.get_content())
        self.assertEqual(server.messages[0]["recipients"], [RECIPIENT])

    def test_the_client_names_itself_without_asking_this_host(self):
        # smtplib would call socket.getfqdn() for the EHLO name, which
        # depends on the host's DNS and stalled for a minute on a CI runner,
        # long enough for the relay to time the session out.
        with FakeSmtpServer() as server:
            config = smtp_config(server)
            with patch("socket.getfqdn", side_effect=AssertionError("asked DNS")):
                self.assertTrue(
                    self.notifier(config).send(run_notice(RUN, events=EVENTS))
                )
        self.assertEqual(server.client_names, ["example.invalid"])
        self.assertEqual(server.errors, [])

    def test_a_configured_ehlo_name_is_what_the_relay_sees(self):
        with FakeSmtpServer() as server:
            config = smtp_config(
                server,
                smtp={"host": server.host, "port": server.port, "tls": "none",
                      "ehlo_name": "bench.example.com"},
            )
            self.assertTrue(
                self.notifier(config).send(run_notice(RUN, events=EVENTS))
            )
        self.assertEqual(server.client_names, ["bench.example.com"])

    def test_a_refused_recipient_is_a_warning_not_a_verdict(self):
        with FakeSmtpServer(reject_recipients=True) as server:
            config = smtp_config(server)
            sent = self.notifier(config).send(run_notice(RUN, events=EVENTS))
        self.assertFalse(sent)
        self.assertIn("was not sent", self.log.text)
        self.assertEqual(self.state(f"run-{RUN['run_id']}")["state"], FAILED)

    def test_starttls_that_the_relay_cannot_do_fails_closed(self):
        # The relay offers STARTTLS and then refuses the upgrade: nothing is
        # sent in the clear as a fallback.
        with FakeSmtpServer(starttls=True) as server:
            config = dataclasses.replace(smtp_config(server), tls="starttls")
            sent = self.notifier(config).send(run_notice(RUN, events=EVENTS))
        self.assertFalse(sent)
        self.assertEqual(server.messages, [])
        self.assertIn("SMTP", self.log.text)

    def test_starttls_against_a_relay_without_it_fails_closed(self):
        with FakeSmtpServer(starttls=False) as server:
            config = dataclasses.replace(smtp_config(server), tls="starttls")
            sent = self.notifier(config).send(run_notice(RUN, events=EVENTS))
        self.assertFalse(sent)
        self.assertEqual(server.messages, [])
        self.assertIn("SMTPNotSupportedError", self.log.text)

    def test_a_rejected_password_is_reported_without_the_password(self):
        # `parse_config` refuses username without TLS, and this fake has no
        # certificate, so the configuration is built directly: what is under
        # test is the transport's handling of a 535, not the validator.
        with FakeSmtpServer(username=SENDER, password="right") as server:
            config = dataclasses.replace(
                smtp_config(server), username=SENDER,
                password_env="UBERTOOTH_HARNESS_SMTP_PASSWORD",
            )
            transport = SmtpTransport(
                config, {"UBERTOOTH_HARNESS_SMTP_PASSWORD": "wrong"}
            )
            sent = self.notifier(config, transport=transport).send(
                run_notice(RUN, events=EVENTS)
            )
        self.assertFalse(sent)
        self.assertEqual(set(server.auth_attempts), {SENDER})
        self.assertEqual(server.messages, [])
        self.assertIn("SMTPAuthenticationError", self.log.text)
        self.assertNotIn("wrong", self.log.text)

    def test_retries_are_bounded_and_then_give_up(self):
        delays = []
        with FakeSmtpServer(fail_first=5) as server:
            config = dataclasses.replace(
                smtp_config(server), attempts=3, retry_delay_s=0.01
            )
            notifier = self.notifier(config, sleep=delays.append)
            self.assertFalse(notifier.send(run_notice(RUN, events=EVENTS)))
        self.assertEqual(server.deliveries, 3)
        self.assertEqual(delays, [0.01, 0.01])
        self.assertEqual(self.state(f"run-{RUN['run_id']}")["attempts"], 3)

    def test_a_retry_that_succeeds_sends_one_message(self):
        with FakeSmtpServer(fail_first=1) as server:
            config = dataclasses.replace(
                smtp_config(server), attempts=3, retry_delay_s=0
            )
            self.assertTrue(
                self.notifier(config).send(run_notice(RUN, events=EVENTS))
            )
        self.assertEqual(len(server.messages), 1)
        self.assertEqual(self.state(f"run-{RUN['run_id']}")["state"], SENT)

    def test_a_disabled_configuration_sends_nothing(self):
        with FakeSmtpServer() as server:
            config = dataclasses.replace(smtp_config(server), enabled=False)
            self.assertFalse(
                self.notifier(config).send(run_notice(RUN, events=EVENTS))
            )
        self.assertEqual(server.messages, [])
        self.assertFalse(self.ledger.exists())

    def state(self, key):
        return json.loads((self.ledger / f"{key}.json").read_text(encoding="utf-8"))


class DeduplicationTests(unittest.TestCase):
    """One notice per run or group ID, across restarts and replays."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Path(self.directory.name) / LEDGER_DIR
        self.log = CollectLog()
        self.addCleanup(self.directory.cleanup)

    def test_a_second_send_of_one_run_is_dropped(self):
        with FakeSmtpServer() as server:
            config = smtp_config(server)
            notice = run_notice(RUN, events=EVENTS)
            first = Notifier(config, self.ledger, log=self.log)
            self.assertTrue(first.send(notice))
            # A new Notifier is what a restarted controller has: the ledger
            # on disk is all it knows, and it is enough.
            second = Notifier(config, self.ledger, log=self.log)
            self.assertFalse(second.send(notice))
            self.assertFalse(second.send(run_notice(RUN, events=EVENTS)))
        self.assertEqual(len(server.messages), 1)

    def test_a_claim_left_by_a_crash_is_not_sent_again(self):
        self.ledger.mkdir(parents=True)
        key = f"run-{RUN['run_id']}"
        (self.ledger / f"{key}.json").write_text(
            json.dumps({"state": SENDING}), encoding="utf-8"
        )
        with FakeSmtpServer() as server:
            notifier = Notifier(smtp_config(server), self.ledger, log=self.log)
            self.assertFalse(notifier.send(run_notice(RUN, events=EVENTS)))
        self.assertEqual(server.messages, [])
        self.assertIn("did not finish sending it", self.log.text)

    def test_a_failed_notice_is_not_retried_by_a_later_run(self):
        with FakeSmtpServer(reject_recipients=True) as server:
            config = smtp_config(server)
            self.assertFalse(
                Notifier(config, self.ledger, log=self.log).send(
                    run_notice(RUN, events=EVENTS))
            )
        with FakeSmtpServer() as working:
            config = smtp_config(working)
            self.assertFalse(
                Notifier(config, self.ledger, log=self.log).send(
                    run_notice(RUN, events=EVENTS))
            )
            self.assertEqual(working.messages, [])

    def test_a_different_run_still_gets_its_own_notice(self):
        with FakeSmtpServer() as server:
            config = smtp_config(server)
            notifier = Notifier(config, self.ledger, log=self.log)
            self.assertTrue(notifier.send(run_notice(RUN, events=EVENTS)))
            other = {**RUN, "run_id": "20260101T140000Z-0123456789ac"}
            self.assertTrue(notifier.send(run_notice(other, events=EVENTS)))
        self.assertEqual(len(server.messages), 2)

    def test_a_notice_key_that_is_not_an_identifier_is_refused(self):
        with FakeSmtpServer() as server:
            notifier = Notifier(smtp_config(server), self.ledger, log=self.log)
            self.assertFalse(
                notifier.send(Notice(key="../escape", subject="x", body="y"))
            )
        self.assertEqual(server.messages, [])
        self.assertIn("cannot claim notice", self.log.text)

    def test_without_a_ledger_nothing_is_deduplicated(self):
        with FakeSmtpServer() as server:
            notifier = Notifier(smtp_config(server), log=self.log)
            notice = sample_notice()
            self.assertTrue(notifier.send(notice, deduplicate=False))
            self.assertTrue(notifier.send(notice, deduplicate=False))
        self.assertEqual(len(server.messages), 2)


class SendmailTests(unittest.TestCase):
    """The local transport: argv and standard input, never a shell."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.log = CollectLog()
        self.addCleanup(self.directory.cleanup)

    def script(self, body: str) -> Path:
        path = self.root / "sendmail"
        path.write_text(body, encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    def config(self, path: Path, **overrides) -> NotifyConfig:
        data = {
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "transport": "sendmail", "sendmail": {"path": str(path)},
            "attempts": 1, "retry_delay_s": 0,
        }
        data.update(overrides)
        return parse_config(data)

    def test_the_message_arrives_on_standard_input(self):
        out = self.root / "delivered"
        argv = self.root / "argv"
        path = self.script(
            f'#!/bin/sh\nprintf "%s\\n" "$@" > {argv}\ncat > {out}\n'
        )
        config = self.config(path)
        notifier = Notifier(config, self.root / LEDGER_DIR, log=self.log)
        self.assertTrue(notifier.send(run_notice(RUN, events=EVENTS)))
        self.assertEqual(
            argv.read_text(encoding="utf-8").split("\n")[:4],
            ["-i", "-f", SENDER, "--"],
        )
        self.assertIn(RECIPIENT, argv.read_text(encoding="utf-8"))
        delivered = out.read_text(encoding="utf-8")
        self.assertIn("Verdict: failed", delivered)
        self.assertIn(f"To: {RECIPIENT}", delivered)

    def test_a_failing_binary_is_a_warning_with_its_message(self):
        path = self.script('#!/bin/sh\necho "no relay configured" >&2\nexit 75\n')
        notifier = Notifier(self.config(path), self.root / LEDGER_DIR, log=self.log)
        self.assertFalse(notifier.send(run_notice(RUN, events=EVENTS)))
        self.assertIn("exited 75", self.log.text)
        self.assertIn("no relay configured", self.log.text)

    def test_nothing_in_the_message_can_become_a_command(self):
        # The transport never goes through a shell, so a run ID or a reason
        # holding shell syntax is only ever text on standard input.
        out = self.root / "delivered"
        marker = self.root / "pwned"
        path = self.script(f'#!/bin/sh\ncat > {out}\n')
        run = {**RUN, "reason": f"; touch {marker} #"}
        notifier = Notifier(
            self.config(path), self.root / LEDGER_DIR, log=self.log
        )
        self.assertTrue(notifier.send(run_notice(run, events=EVENTS)))
        self.assertFalse(marker.exists())
        delivered = parsed(out.read_bytes()).get_content()
        self.assertIn(f"Reason: ; touch {marker} #", delivered)

    def test_the_transport_describes_itself_without_a_secret(self):
        path = self.script("#!/bin/sh\nexit 0\n")
        config = self.config(path)
        self.assertEqual(SendmailTransport(config).describe(), str(path))
        self.assertIn(str(path), config.describe())


# ----- the controller's own path -------------------------------------------


class RecordingTransport:
    """A transport that keeps the message instead of sending it anywhere."""

    def __init__(self):
        self.sent: list[str] = []

    def describe(self) -> str:
        return "recording"

    def deliver(self, sender, recipients, data) -> None:
        self.sent.append(data.decode())


class ControllerNoticeTests(unittest.IsolatedAsyncioTestCase):
    """What the controller sends when a run or a group ends."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.results = Path(self.directory.name) / "runs"
        self.log = CollectLog()
        self.transport = RecordingTransport()
        self.addCleanup(self.directory.cleanup)

    @property
    def sent(self) -> list[str]:
        return self.transport.sent

    def controller(self, **kwargs):
        config = parse_config({
            "enabled": True, "sender": SENDER, "recipients": [RECIPIENT],
            "smtp": {"host": "127.0.0.1", "tls": "none"},
            "attempts": 1, "retry_delay_s": 0,
        })
        self.notifier = Notifier(
            config, self.results / LEDGER_DIR, log=self.log,
            transport=self.transport,
        )
        controller = Controller(
            self.results, log=self.log, notifier=self.notifier, **kwargs
        )
        controller.port = 8765
        return controller

    def finished_run(
        self, controller, run_id, *, group_id=None, status="passed",
        finished=True,
    ):
        """A stored run, with its records on disk as a real one has them."""
        from ubertooth_harness.controller import Run

        run = Run(
            run_id=run_id, target="Host_Linux",
            directory=controller.results_dir / run_id,
            ref="main", commit="a" * 40, plan="release", suite="release",
            status=status if finished else "running",
            created_at="2026-01-01T12:00:00.000+00:00",
            finished_at="2026-01-01T13:12:05.000+00:00" if finished else None,
            group_id=group_id,
        )
        run.directory.mkdir(parents=True, exist_ok=True)
        with (run.directory / "run.jsonl").open("w", encoding="utf-8") as stream:
            for event in EVENTS:
                stream.write(json.dumps({**event, "run_id": run_id}) + "\n")
        controller.runs[run_id] = run
        return run

    def finish(self, run, status="passed") -> None:
        run.status = status
        run.finished_at = "2026-01-01T13:12:05.000+00:00"

    def group(self, controller, hosts=("Host_Linux", "Host_Mac")):
        from ubertooth_harness.rungroups import new_group

        group = new_group(
            list(hosts),
            {"ref": "main", "commit": "a" * 40, "plan": "release",
             "suite": "release", "tests": [], "build": True,
             "flash_build": True, "with_firmware": False, "with_btbr": False,
             "with_python": False, "bootloader": "record"},
        )
        group.created_at = "2026-01-01T12:00:00+00:00"
        group.dispatched_at = "2026-01-01T12:00:01+00:00"
        controller.groups[group.group_id] = group
        return group

    async def test_a_single_run_gets_one_notice(self):
        controller = self.controller()
        run = self.finished_run(controller, "20260101T120000Z-0123456789ab")
        controller.notify_finished(run)
        await self.drain(controller)
        self.assertEqual(len(self.sent), 1)
        message = self.sent[0]
        self.assertIn("Verdict: passed", message)
        self.assertIn(run.run_id, message)
        self.assertIn(f"http://127.0.0.1:8765/ui/#run={run.run_id}", message)
        self.assertIn(str(run.directory), message)
        self.assertIn("a" * 40, message)

    async def test_the_same_run_reported_twice_sends_once(self):
        controller = self.controller()
        run = self.finished_run(controller, "20260101T120000Z-0123456789ab")
        controller.notify_finished(run)
        controller.notify_finished(run)
        await self.drain(controller)
        self.assertEqual(len(self.sent), 1)

    async def test_a_group_gets_one_notice_when_its_last_host_ends(self):
        controller = self.controller()
        group = self.group(controller)
        first = self.finished_run(
            controller, "20260101T120000Z-0123456789ab", group_id=group.group_id
        )
        second = self.finished_run(
            controller, "20260101T120001Z-0123456789ac", group_id=group.group_id,
            finished=False,
        )
        group.set_member_run("Host_Linux", first.run_id)
        group.set_member_run("Host_Mac", second.run_id)
        controller._persist_group(group)
        # The first host's end tells nobody: the other host is still running.
        controller.notify_finished(first)
        await self.drain(controller)
        self.assertEqual(self.sent, [])
        self.finish(second, "failed")
        controller.notify_finished(second)
        await self.drain(controller)
        self.assertEqual(len(self.sent), 1)
        message = self.content(self.sent[0])
        self.assertIn(group.group_id, message)
        self.assertIn("Host_Linux: passed", message)
        self.assertIn("Host_Mac: failed", message)
        self.assertIn("Verdict: failed", message)
        self.assertIn("Hosts: 2 of 2 host(s) ran", message)
        self.assertIn("Duration: 1h 12m 05.000s", message)
        # One notice for the group, and none of its own for either child.
        self.assertFalse((controller.results_dir / LEDGER_DIR
                          / f"run-{first.run_id}.json").exists())

    async def test_two_children_ending_together_send_one_group_notice(self):
        controller = self.controller()
        group = self.group(controller)
        first = self.finished_run(
            controller, "20260101T120000Z-0123456789ab", group_id=group.group_id
        )
        second = self.finished_run(
            controller, "20260101T120001Z-0123456789ac", group_id=group.group_id
        )
        group.set_member_run("Host_Linux", first.run_id)
        group.set_member_run("Host_Mac", second.run_id)
        controller._persist_group(group)
        controller.notify_finished(first)
        controller.notify_finished(second)
        await self.drain(controller)
        self.assertEqual(len(self.sent), 1)

    async def test_a_group_whose_host_never_ran_still_reports(self):
        controller = self.controller()
        group = self.group(controller)
        only = self.finished_run(
            controller, "20260101T120000Z-0123456789ab", group_id=group.group_id
        )
        group.set_member_run("Host_Linux", only.run_id)
        group.set_member_error("Host_Mac", "target Host_Mac is offline")
        controller._persist_group(group)
        controller.notify_finished(only)
        await self.drain(controller)
        message = self.content(self.sent[0])
        self.assertIn("Hosts: 1 of 2 host(s) ran", message)
        self.assertIn("Host_Mac: no run (target Host_Mac is offline)", message)
        self.assertIn("Verdict: error", message)

    async def test_a_group_settled_by_a_hosts_reason_is_still_reported(self):
        """The group's last word is a host that never answered, not a child.

        Its only child has already ended, so nothing else would ever reach
        this group's notice.
        """
        controller = self.controller()
        group = self.group(controller)
        only = self.finished_run(
            controller, "20260101T120000Z-0123456789ab", group_id=group.group_id
        )
        group.set_member_run("Host_Linux", only.run_id)
        controller._persist_group(group)
        # A start request to the other host is still outstanding, so the
        # group reads as running and the child's end says nothing.
        pending = self.pending(controller, group, "Host_Mac")
        controller.notify_finished(only)
        await self.drain(controller)
        self.assertEqual(self.sent, [])
        # Now that request comes to nothing: the runner rejected it, its
        # connection went, or it expired.
        controller._pending_runs.clear()
        controller._note_group_failure(
            pending, "the runner did not answer the start request"
        )
        await self.drain(controller)
        self.assertEqual(len(self.sent), 1)
        message = self.content(self.sent[0])
        self.assertIn("Host_Linux: passed", message)
        self.assertIn(
            "Host_Mac: no run (the runner did not answer the start request)",
            message,
        )

    async def test_a_group_that_started_nowhere_is_not_reported(self):
        controller = self.controller()
        group = self.group(controller)
        pending = self.pending(controller, group, "Host_Mac")
        controller._pending_runs.clear()
        controller._note_group_failure(pending, "target Host_Mac is offline")
        await self.drain(controller)
        self.assertEqual(self.sent, [])

    def pending(self, controller, group, host):
        """One outstanding start request for a host of this group."""
        from ubertooth_harness.controller import _Pending

        entry = _Pending(
            host, asyncio.get_running_loop().create_future(),
            group_id=group.group_id,
        )
        entry.waiters = 1
        controller._pending_runs["request-" + host] = entry
        return entry

    async def test_a_notice_failure_never_reaches_the_run(self):
        controller = self.controller()
        run = self.finished_run(controller, "20260101T120000Z-0123456789ab")

        def explode(*args, **kwargs):
            raise RuntimeError("the relay is on fire")

        self.notifier.deliver = explode
        controller.notify_finished(run)
        await self.drain(controller)
        self.assertEqual(run.status, "passed")
        self.assertIn("the relay is on fire", self.log.text)

    async def test_no_notifier_means_no_work_at_all(self):
        controller = Controller(self.results, log=self.log)
        run = self.finished_run(controller, "20260101T120000Z-0123456789ab")
        controller.notify_finished(run)
        self.assertEqual(controller._tasks, set())

    async def test_a_public_link_appears_only_with_a_record(self):
        controller = self.controller()
        run = self.finished_run(controller, "20260101T120000Z-0123456789ab")
        controller.notify_finished(run)
        await self.drain(controller)
        self.assertNotIn("published validation bundle", self.sent[0])
        published = controller.results_dir / PUBLICATIONS_DIR
        published.mkdir(parents=True, exist_ok=True)
        other = self.finished_run(controller, "20260101T140000Z-0123456789ac")
        (published / f"{other.run_id}.json").write_text(
            json.dumps({
                "sanitized": True,
                "url": "https://example.invalid/validation.tar.zst",
            }),
            encoding="utf-8",
        )
        controller.notify_finished(other)
        await self.drain(controller)
        self.assertIn(
            "published validation bundle: https://example.invalid/validation.tar.zst",
            self.sent[1],
        )

    async def test_a_run_may_not_take_the_ledger_or_publication_name(self):
        from ubertooth_harness.controller import _Violation

        controller = self.controller()
        for reserved in (LEDGER_DIR, PUBLICATIONS_DIR, "groups"):
            with self.subTest(name=reserved):
                with self.assertRaises(_Violation):
                    controller._run_directory(reserved)

    async def drain(self, controller) -> None:
        """Wait for the notice tasks the controller started."""
        while controller._tasks:
            await asyncio.gather(*list(controller._tasks), return_exceptions=True)

    @staticmethod
    def content(message: str) -> str:
        """The body of one recorded message."""
        return parsed(message).get_content()


class CommandLineTests(unittest.TestCase):
    """The controller's option, the environment variable, and notify-test."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def write(self, text: str, name: str = "notify.yaml") -> Path:
        path = self.root / name
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def enabled(self) -> Path:
        return self.write(f"""\
            enabled: true
            sender: {SENDER}
            recipients: [{RECIPIENT}]
            smtp:
              host: 127.0.0.1
              tls: none
            """)

    def test_the_option_wins_then_the_variable_then_the_default(self):
        chosen, named = self.enabled(), self.write("enabled: false\n", "named.yaml")
        environment = {
            "UBERTOOTH_HARNESS_NOTIFY_CONFIG": str(named),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.assertEqual(_notify_config_path(chosen, environment), chosen)
        self.assertEqual(_notify_config_path(None, environment), named)
        # Nothing named and nothing at the default path: notices stay off.
        self.assertIsNone(_notify_config_path(None, {"XDG_CONFIG_HOME": str(self.root)}))
        default = self.root / "ubertooth-harness" / "notify.yaml"
        default.parent.mkdir(parents=True)
        default.write_text("enabled: false\n", encoding="utf-8")
        self.assertEqual(
            _notify_config_path(None, {"XDG_CONFIG_HOME": str(self.root)}), default
        )

    def test_the_controller_refuses_to_start_on_a_bad_configuration(self):
        bad = self.write("enabled: true\nrecipients: [not-an-address]\n")
        with self.assertRaisesRegex(HarnessError, "completion notices"):
            _notifier(bad, self.root)

    def test_a_disabled_configuration_gives_the_controller_no_notifier(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertIsNone(_notifier(self.write("enabled: false\n"), self.root))
        self.assertIn("no notice will be sent", stderr.getvalue())

    def test_an_enabled_configuration_gives_one_with_a_ledger(self):
        notifier = _notifier(self.enabled(), self.root)
        self.assertTrue(notifier.enabled)
        self.assertEqual(notifier.ledger_dir, self.root / LEDGER_DIR)

    def test_notify_test_dry_run_prints_the_message_and_no_credential(self):
        secret = self.write("hunter2\n", "smtp-password")
        path = self.write(f"""\
            enabled: true
            sender: {SENDER}
            recipients: [{RECIPIENT}]
            smtp:
              host: relay.example.invalid
              tls: starttls
              username: {SENDER}
              password_file: {secret}
            """)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                main(["notify-test", "--notify-config", str(path), "--dry-run"]), 0
            )
        text = out.getvalue()
        self.assertIn("dry run: nothing was sent", text)
        self.assertIn("TEST:", text)
        self.assertIn(f"To: {RECIPIENT}", text)
        self.assertIn(str(secret), text)  # which source, not what is in it
        self.assertNotIn("hunter2", text)

    def test_notify_test_reports_a_configuration_that_is_not_there(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(
                main(["notify-test", "--notify-config", str(self.root / "absent.yaml")]),
                2,
            )
        self.assertIn("absent.yaml", stderr.getvalue())

    def test_notify_test_refuses_a_disabled_configuration(self):
        path = self.write("enabled: false\n")
        out, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(stderr):
            self.assertEqual(
                main(["notify-test", "--notify-config", str(path)]), 2
            )
        self.assertIn("enabled: false", stderr.getvalue())
        self.assertIn("notices: disabled", out.getvalue())

    def test_notify_test_sends_one_notice_through_the_relay(self):
        with FakeSmtpServer() as server:
            path = self.write(f"""\
                enabled: true
                sender: {SENDER}
                recipients: [{RECIPIENT}]
                smtp:
                  host: {server.host}
                  port: {server.port}
                  tls: none
                attempts: 1
                retry_delay_s: 0
                """)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    main(["notify-test", "--notify-config", str(path)]), 0
                )
        self.assertEqual(len(server.messages), 1)
        self.assertIn("is not a real run", parsed(server.messages[0]["data"]).get_content())
        # No ledger: a test notice may be sent as often as somebody likes.
        self.assertFalse((self.root / LEDGER_DIR).exists())

    def test_the_controller_option_is_not_a_bind_option(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            main(["controller", "--notify-host", "relay.example.invalid"])
        self.assertIn("unrecognized arguments", stderr.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
