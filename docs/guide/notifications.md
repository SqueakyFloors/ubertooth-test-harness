# Completion notices by email

A release suite runs for the better part of an hour, a soak for days. Nobody
watches it: the browser is closed, the tunnel is down, the laptop is asleep.
Turn these on and the controller sends one short email when a run or a run
group has finished, so whoever looks next already knows the verdict.

They are **off** until a private configuration file turns them on. Nothing is
attached, no credential reaches a log, a run record or the message, and a
notice that cannot be sent is a warning in the controller's log and nothing
else: it never changes a verdict.

| | |
|---|---|
| Who sends | the controller, after the run's `report.md` is on disk |
| How often | once per run ID, or once per group ID |
| What is in it | verdict, IDs, commits, UTC times and duration, plan/suite, totals, result directory, loopback GUI link |
| What is never in it | logs, captures, `report.md`, credentials |
| Where it is configured | a private file outside this repository |
| Default | disabled |

## Set it up

1. **Write the configuration** outside the repository, readable only by you.
   [`notify.example.yaml`](../../notify.example.yaml) is the full list of
   settings; the shortest useful file is:

   ```sh
   mkdir -p ~/.config/ubertooth-harness
   cat > ~/.config/ubertooth-harness/notify.yaml <<'EOF'
   enabled: true
   sender: harness-notices@example.com
   recipients:
     - you@example.com
   smtp:
     host: smtp.example.com
     port: 587
     tls: starttls
     username: harness-notices@example.com
     password_env: UBERTOOTH_HARNESS_SMTP_PASSWORD
   EOF
   chmod 600 ~/.config/ubertooth-harness/notify.yaml
   ```

   That path is the default. `--notify-config FILE` and
   `UBERTOOTH_HARNESS_NOTIFY_CONFIG` name another one, in that order of
   precedence; with none of the three, and no file at the default path,
   notices stay off.

2. **Give the password its own environment variable**, never the YAML. For
   the systemd user service that [deploy](deploy.md) installs, put it in a
   private environment file and point the unit at it:

   ```sh
   install -m 600 /dev/null ~/.config/ubertooth-harness/smtp.env
   printf 'UBERTOOTH_HARNESS_SMTP_PASSWORD=%s\n' "$password" \
     > ~/.config/ubertooth-harness/smtp.env
   systemctl --user edit ubertooth-harness-controller.service
   # [Service]
   # EnvironmentFile=%h/.config/ubertooth-harness/smtp.env
   systemctl --user restart ubertooth-harness-controller.service
   ```

   A `smtp.password_file` holding only the password works too, and must be
   owned by you with mode `600`; a file anyone else can read is refused. The
   variable name must start with `UBERTOOTH_HARNESS_`, so a notice cannot
   pick up whatever a generic `PASSWORD` in the service environment happens
   to hold.

3. **Check it** without waiting for a run. `notify-test` sends one fixed
   notice that names no real run, and prints where it went and which source
   the password came from, never the password:

   ```sh
   python -m ubertooth_harness notify-test            # sends one test notice
   python -m ubertooth_harness notify-test --dry-run  # prints it, sends nothing
   ```

4. **Restart the controller.** It logs the recipients and the relay at
   startup (`completion notices: smtp …`), and refuses to start if the
   configuration is unusable, so a bench that asked for notices does not run
   for hours believing it will get one.

## What a notice says

One plain-text message, nothing attached:

```
Subject: [ubertooth-harness] FAILED: release/release run 20260101T120000Z-0123456789ab

Ubertooth harness run 20260101T120000Z-0123456789ab: failed

Verdict: failed
Run: 20260101T120000Z-0123456789ab
Target host: Host_Linux
Plan / suite: release / release
Tests: 23 passed, 1 failed, 2 skipped (26 of 27 planned tests reached)
Started (UTC): 2026-01-01T12:00:00Z
Finished (UTC): 2026-01-01T13:12:05Z
Duration: 1h 12m 05.000s
Reason: Raw receive: no packets

Source (the commit is the identity; a ref can move):
  ubertooth: <40-character commit> (ref main, builds as 2026-01-DEV1+7)
    from https://github.com/SqueakyFloors/ubertooth.git
  libbtbb: <40-character commit> (ref fork-pin)

Where to look:
  results on the test host: <the run directory on that host>
  controller results: <the run directory in the controller's results store>
  GUI (through the usual SSH tunnel): http://127.0.0.1:8765/ui/#run=20260101T120000Z-0123456789ab

No logs, captures or reports are attached. Open the GUI link above through
the usual SSH tunnel, or read report.md in the result directory on the test
host.
```

The commit is the identity: a ref can move, so the notice names both and says
which is which. The GUI link is loopback, because that is the only place the
GUI listens: open the usual tunnel
([Quick start](../../README.md#quick-start-use-the-web-gui-from-your-laptop))
and the link works in the browser on your laptop.

A **run group** gets one message, not one per host: the same block at the
top for the group, then one block per host with its verdict, totals,
duration, GUI link and result directory, or the reason that host never ran.
A single-host run is a group of one and gets the run message above.

### A public URL, only when one has been published

A notice carries a public link only when a sanitized validation bundle
([#370](https://github.com/SqueakyFloors/ubertooth-test-harness/issues/370))
has been published for exactly that run or group and recorded beside the
results:

```
<results dir>/publications/<run or group id>.json
{"sanitized": true, "url": "https://github.com/…/releases/tag/…"}
```

Everything else leaves the link out: no file, `sanitized` not exactly `true`,
a record naming another run, a URL that is not a plain `https://` one. The
last three are logged, so a record that was meant to publish something says
why it did not.

## What is sent, and when, and only once

- **After the report.** The controller builds a notice only once the run has
  a terminal status and its `report.md` is on disk, so a message never points
  at results that are not there.
- **Once per run or group ID.** Each notice is claimed in a ledger beside the
  run directories (`<results dir>/notifications/<key>.json`) with an atomic
  exclusive create. A controller restart, a runner that reports one run's end
  twice after reconnecting, and two hosts of one group finishing in the same
  moment all end in one message.
- **A claim left by a crash is not sent again.** A ledger entry still reading
  `sending` means the controller died mid-send; nobody can tell from here
  whether that message went out, so it is logged and left. A duplicate flood
  is worse than one missing notice.
- **A run that ended while the controller was down** is not gone back over on
  startup: enabling notices would otherwise mail out the whole history. Such
  a run does get one notice, as `lost`, when its target's runner next
  registers without it, which is when the controller learns the run can no
  longer finish. A run still in flight across a restart gets its real verdict,
  because its runner reports the end to the controller that comes back.
- **A group settles on its last word, whichever that is.** Usually that is
  its last host ending. It can also be a host whose runner rejected the run,
  went away, or never answered, which is written on the group instead; the
  notice goes out then, naming each host that did run and the reason the
  others did not.
- **Bounded retries.** `attempts` (default 3, at most 5) deliveries,
  `retry_delay_s` apart, each with `timeout_s`. Then the notice is recorded
  as failed, with a warning naming the last error. The run's own result is
  untouched: a failure here is never a failed test, and a notice is never
  part of a verdict.

## Security

- **No listener.** Notices are outbound only. The controller still binds
  `127.0.0.1` and nothing else ([services.md](services.md#controller)).
- **Verified TLS.** `starttls` upgrades the connection, `tls` connects with
  TLS from the first byte, and both verify the certificate chain and the host
  name with the system trust store. There is no setting that turns either
  off. Authenticated SMTP requires one of them: a password never crosses an
  unencrypted connection. `tls: none` is allowed only for a relay on this
  machine's own loopback address, where there is no network to listen on.
- **Credentials from one named source.** `smtp.password_env` (a variable
  whose name starts with `UBERTOOTH_HARNESS_`) or `smtp.password_file` (yours,
  mode `600`), read at send time, never stored on the configuration object,
  never logged, never in a run record, and never in the message. A password
  in the YAML is not a setting: the loader refuses unknown keys.
- **No header injection.** Addresses must be plain `name@example.com`: a
  display name, a comma, a bracket or a newline is refused when the
  configuration loads. Everything a run record carries (a failure reason, a
  plan name, a host) is flattened to one line before it reaches a header, and
  Python's SMTP policy refuses a header with a newline as well.
- **No arbitrary command.** There is no "run this on completion" hook. The
  only local transport is a sendmail-compatible binary named by absolute
  path, which must be a regular executable file that no one else can write;
  it is run with the addresses as arguments and the message on standard
  input, never through a shell.
- **The configuration file is yours.** It says where notices go, so a file
  that is group- or world-writable is refused.
- **Nothing asks this host its own name.** The EHLO name is the sender's
  domain, or `smtp.ehlo_name`. Looking it up would depend on whatever DNS the
  host has (it stalled for a minute on a CI runner, long enough for the relay
  to give up), and would put the lab host's own name into the conversation.

## Settings

| Setting | Meaning |
|---|---|
| `enabled` | `false` by default; nothing is sent until it is `true` |
| `sender` | the `From` address, one plain address |
| `recipients` | up to 10 plain addresses |
| `transport` | `smtp` (default) or `sendmail` |
| `smtp.host`, `smtp.port` | the relay (port 587 by default) |
| `smtp.tls` | `starttls` (default), `tls`, or `none` for a loopback relay |
| `smtp.username` | set it to authenticate; then TLS and one password source are required |
| `smtp.password_env` | an environment variable named `UBERTOOTH_HARNESS_…` |
| `smtp.password_file` | an absolute path to a file only you can read |
| `smtp.ehlo_name` | what the client calls itself in EHLO; by default the sender's domain |
| `sendmail.path` | absolute path to a sendmail-compatible binary |
| `attempts` | deliveries per notice, 1 to 5 (default 3) |
| `retry_delay_s` | seconds between attempts (default 5, at most 60) |
| `timeout_s` | seconds per attempt (default 20, at most 120) |
| `subject_prefix` | default `[ubertooth-harness]` |
| `gui_base_url` | the loopback GUI a link points at; by default the controller's own port |

An unknown setting is refused rather than ignored, so a typo in a key that
would have turned encryption on cannot pass as a default.

## When a notice does not arrive

| What the log says | What it means |
|---|---|
| nothing about notices at startup | no configuration was found, so notices are off |
| `<path> has enabled: false; no notice will be sent` | the file was read and says not to |
| `warning: notice … attempt 1 of 3 failed: SMTPNotSupportedError` | the relay does not offer STARTTLS; use `tls: tls`, or the right port |
| `… SMTPAuthenticationError` | the relay rejected the user name or password; check the named source |
| `… was not sent after 3 attempt(s)` | every attempt failed; the run's result stands |
| `notice … was claimed by a run of this controller that did not finish sending it` | a crash mid-send; it is not retried |
| `publication record for … is not marked sanitized` | the public link was left out on purpose |

Nothing here can fail a run. If a notice is missing, the run's record,
`report.md` and the GUI are unchanged and complete.
