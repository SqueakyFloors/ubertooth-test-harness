# Release evidence: the public-safe summary, logs, and sums

What a release of the Ubertooth fork is published with, how it is built, what
is taken out of it, and the one command that is never run for you.

A run is recorded for the bench it ran on: the paths the commands ran with,
the lease holder's user and machine name, the registry's aliases for the host
and every board, every device serial, and whatever traffic was on the air
([run records](run-records.md)). None of that may go out with a release. The
export turns a fixed set of finished run directories into three assets that
may:

| Asset | What it holds |
|---|---|
| `validation-summary.md` | the release and the exact commits, which gates were required and which were met, every run with its platform class, UTC start and end, totals and verdict, the measurements the runs recorded, every deviation, and the names of the logs beside it |
| `validation-logs.tar.zst` | each run's `report.md`, its `run.jsonl`, and the text output files it saved, each sanitized, with `MANIFEST-SHA256` giving the SHA-256 of every member |
| `validation-SHA256SUMS` | the SHA-256 of the two assets above, in the format `sha256sum -c` reads |

## Build it

```sh
python -m ubertooth_harness release-evidence \
  runs/20261105T091233Z-... runs/20261105T104512Z-... \
  --release 2026-11-R1 --output evidence/2026-11-R1
```

| Option | What it does |
|---|---|
| `--release NAME` | the release or tag this evidence is for; required |
| `--output DIR` | where the three assets go; required |
| `--phase snapshot\|rc\|release` | which gates are required (default: read from the release name) |
| `--attest GATE=NOTE` | evidence for a gate run by hand; repeatable |
| `--strip-path PREFIX` | an absolute path prefix to replace before publication; repeatable |
| `--harness-commit COMMIT` | the harness commit to record |
| `--private` | keep device serials, write the private names, print no upload command |
| `--allow-incomplete` | print the upload command although a required gate has no evidence |

It reads the run directories and writes those three files. No hardware, no
network, no run directory touched, and nothing uploaded. It exits 0 when
every required gate is met, 1 when one is not (and `--allow-incomplete` was
not passed), and 2 when it refuses to publish something.

## Which gates a phase requires

A release name says which phase it is — `2026-11-DEV2` a development
snapshot, `2026-11-RC1` a release candidate, `2026-11-R1` a final release —
and `--phase` overrides it. A run is grouped by the suite it recorded, so
nothing has to be declared per run.

| Gate | Suites | Snapshot | Candidate | Release |
|---|---|---|---|---|
| Fast gate | `fast` | required | required | required |
| Canonical non-soak release suite | `release` | | required | required |
| Alternate `btbr` image | `btbr`, `btbr-comprehensive`, `btbr-long` | | required | required |
| Focused BLE suites | `ble`, `ble-comprehensive`, `connection` | | required | required |
| Focused Classic suites | `classic`, `classic-comprehensive`, `classic-long` | | required | required |
| Focused HCI suites | `hci`, `hci-comprehensive` | | required | required |
| Weak-link checks | the four `*-weak-link` suites | | required | required |
| Firmware and DFU acceptance | `firmware`, `slow` | | required | required |
| Installer acceptance | by hand | | | required |
| Bootloader acceptance | a run with `--bootloader check` or `update` | | | required |
| Staged 4-, 24-, and 72-hour soaks | the `address-soak-*` suites | | | |

No soak is ever a gate ([release testing, section
11](../release-testing.md#11-optional-endurance-work)): a soak that was run
is reported, and a soak that was not is not missing.

Two gates no suite measures are the installer and bootloader acceptance of
[release testing section 5](../release-testing.md#5-test-cases). The
bootloader gate is met by a run that compared or installed one, which that
run's own records say. The installer is run by a person, so it is attested:

```sh
--attest "installer=install.sh on a clean prefix; ubertooth-util -V reported 2026-11-R1"
```

The note is quoted in the summary and marked as attested by hand rather than
measured. A gate a suite does measure cannot be attested; the export refuses
it rather than letting a sentence stand in for a run.

**A gap is reported, never dropped.** A required gate with no passing run
behind it is listed in the summary under **Missing required evidence**, the
summary opens with an **Incomplete** banner, the command exits 1, and no
upload command is printed. A run that failed is evidence of a failure and not
of a gate: its gate reads as missing and the run appears under
**Deviations**.

## What is taken out, and what is kept

Two things happen to every byte, in `ubertooth_harness/sanitize.py`.

**It is rewritten.** The names to remove are read off the runs' own records
rather than typed in, so a bench that registers its host under its host name
is covered like any other:

| Taken out | Becomes | Where it is found |
|---|---|---|
| device serials, in every width a run writes one | `<serial>` | the registry identities, preflight, the flash guards, the isolation enumeration (the rules `report --redact-serials` uses) |
| the lease holder's user name | `<user>` | the `user` and `owner` of every lease record |
| a machine name | `<host>` | the `hostname` of every lease record, and any name in a private domain (`.local`, `.internal`, `.lan`, `.corp`, ...) |
| the bench's aliases for its hosts and boards | `host 1`, `Ubertooth 1`, `guard Ubertooth 2`, ... | the manifest's own role-to-alias bindings; the label is the catalog's name for that role, which is already public |
| a home directory | `<home>` | `/Users/<name>`, `/home/<name>`, `/root` |
| a temporary or per-user runtime directory | `<tmp>` | `/tmp`, `/var/folders/...`, `/run/user/<id>` |
| the run directory and the source checkouts, and any `--strip-path` prefix | `<path>` | recorded in the run's own manifest |
| an electronic mail address | `<email>` | anywhere |
| an IP address other than loopback | `<address>` | v4 by its form; v6 by parsing a token of hex and colons, which is what keeps the rule off a transcript's `HH:MM:SS` stamp and off a Bluetooth address |

Kept, because it is the evidence: public repository URLs, release names,
commits, tool and firmware versions, plan and suite names, timestamps,
durations, verdicts, measurements, and run IDs (a UTC timestamp and a random
suffix, which name the run on the test host without naming the host).

A member's own name goes through the same two steps, because it becomes a tar
header and a line of `MANIFEST-SHA256`: a command ID can carry a board's
serial into the name of a spill file, and the archive would otherwise publish
in a file name what the summary masked in its table.

**Then it is read again, and that scan is the gate.** It is a denylist over
the finished text, so a rule the rewriting does not implement reaches a
reader as a refusal rather than as a leak:

```
error: runs/20261105T091233Z-abcdef123456/report.md cannot be published:
runs/.../report.md:112: an absolute path this export does not recognize: /srv/bench/notes.txt

Sanitize the source, or name the prefix with --strip-path; the unredacted
records stay on the test host.
```

Nothing is written once it refuses. The rule to know about is the last one:
every absolute path a host tool, the operating system, or a package manager
legitimately names is allowed (`/usr`, `/dev`, `/opt`, `/etc`, `/Library`,
and the rest), and **any other absolute path is a refusal**, on the view
that a path this does not recognize is more likely a bench's own directory
than something a reader needs. `--strip-path` is how you answer one. A slash
begins a path only when what precedes it says so: one inside a relative
token a build wrote (`-I../common`, `-Wa,-alhms=./bluetooth_rxtx.lst`), or
one straight after a placeholder the rewriting left, belongs to that token
and names no directory on any host. A root is judged by its first component,
as written and as the `..` in the path leave it, so a toolchain path that
climbs about inside an allowed root is kept and one that climbs out of it is
refused; a bench that keeps its runs under an allowed root (`/opt/<bench
name>`) has to name that prefix too, since judging further would refuse the
library paths a build legitimately records.

The two halves read a private name with one function, deliberately: a gate
that read a name more widely than the rewriting would refuse a line the
rewriting had decided to leave, and no option could clear it. So `ada` the
lease holder is not found inside `adapter`, and the alias `UT_0` is not found
inside `UT_01`.

## The aliases a run records beside the ones it binds

An exclusive-Ubertooth test switches off the boards it did **not** bind, so
those aliases, the hub's alias, and the hub's own serial appear under
`power_isolation` and `power_cycle` and nowhere else. They are collected and
relabeled with the rest; the isolated board reads as `isolated board 1` and
the hub as `power controller 1`.

## What travels, and what stays on the test host

A member is a file the run's own records name, and the run's own record says
which travel: `stdout`, `stderr`, and a target device's shell traffic are
text the harness decoded itself, so they go; a file a tool wrote itself is
labeled `capture` by the harness whatever its name says — a PCAP, a `-d`
dump, a tool's own report — so it stays. A record with some other stream, or
none, stays too: an export does not guess at what it cannot classify.

Everything that stays is listed in the summary under **Files kept on the test
host** with the size and SHA-256 the run recorded and the reason it stayed,
so a reader knows it exists, can ask for it, and can check they were given
it. A file the run directory no longer holds, or whose recorded path does not
name a file inside it, is listed there too rather than dropped quietly.

## A private bundle

`--private` writes `validation-summary-private.md`,
`validation-logs-private.tar.zst`, and `validation-private-SHA256SUMS`, and
keeps the device serials — the only thing it keeps. The paths, the machine
and operator names, and the aliases go out of it too, because none of them is
ever wanted, and every other scan rule still applies. It gets no upload
command: it is not for a public release.

## Publishing it

The release owner uploads the reviewed bundle; a harness run never publishes
it. Read the summary first, then:

```sh
cd evidence/2026-11-R1
gh release upload 2026-11-R1 \
  validation-summary.md validation-logs.tar.zst validation-SHA256SUMS \
  --repo SqueakyFloors/ubertooth
```

The command the export prints is that one, with the release you named. It is
printed and not run, and it is withheld for an incomplete or private bundle.

## Checking a published bundle

```sh
sha256sum -c validation-SHA256SUMS
tar --zstd -xf validation-logs.tar.zst
sha256sum -c MANIFEST-SHA256
```

The archive's members carry no modification time, owner, or group, and the
compression level is pinned, so the same run directories produce the same
archive bytes for a given `zstandard` version (pinned in
`requirements.txt`): rebuilding the bundle from the retained records with the
same pins reproduces the hash the release published.
