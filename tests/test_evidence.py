"""The public-safe release evidence export (#370).

Every run here is a synthetic run directory built in a temporary directory:
no hardware, no network, and no clock, so the archive's bytes are the same on
every run of the suite and on every platform.
"""

import datetime
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from ubertooth_harness import evidence
from ubertooth_harness.evidence import (
    EvidenceError,
    LOGS_NAME,
    MANIFEST_NAME,
    PRIVATE_LOGS_NAME,
    PRIVATE_SUMMARY_NAME,
    SUMMARY_NAME,
    SUMS_NAME,
    Export,
    build_bundle,
    gate_of,
    gate_results,
    missing_gates,
    phase_of_release,
    private_names,
    public_labels,
    read_run,
    summary,
)
from ubertooth_harness.sanitize import HOST, PATH, SERIAL, USER, Unsafe


SERIAL_0 = "aaaa0000bbbb1111cccc2222dddd3333"
SERIAL_1 = "bbbb0000bbbb1111cccc2222dddd4444"
JLINK = "001050210121"
OPERATOR = "ada"
MACHINE = "bench-linux-7"
COMMIT = "9903b4251f0e4c0c8b1f2a3d4e5f60718293a4b5"
LIBBTBB_COMMIT = "d356e9da7fb03969371aa14d603a8aad2f0ef198"
START = datetime.datetime(2026, 11, 5, 9, 12, 33)
EXPORTED = datetime.datetime(2026, 11, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _event(kind: str, status: str, details: dict, second: int,
           run_id: str) -> dict:
    moment = START + datetime.timedelta(seconds=second)
    return {
        "run_id": run_id, "event_type": kind, "status": status,
        "timestamp": moment.isoformat(timespec="milliseconds"),
        "details": details,
    }


def write_run(
    root: Path,
    *,
    run_id: str = "20261105T091233Z-abcdef123456",
    plan: str = "fast",
    suite: str = "fast",
    status: str = "passed",
    home: str = "/Users/ada",
    source_path: str | None = None,
    bootloader_mode: str = "record",
    failures: tuple = (),
    blocked: tuple = (),
    not_applicable: tuple = (),
    extra_transcript: str = "",
) -> Path:
    """One finished run directory, with everything an export has to take out.

    The records carry what a real run carries: the paths the commands ran
    with, the registry's aliases, each device's serial, and the lease
    holder's user and machine name.
    """
    directory = root / run_id
    (directory / "output").mkdir(parents=True)
    (directory / "captures").mkdir(parents=True)
    checkout = source_path or f"{home}/src/ubertooth"
    events = [
        _event("run_started", "running", {
            "plan_name": plan, "suite": suite, "host": "Host_Linux",
        }, 0, run_id),
        _event("manifest", "passed", {"manifest": {
            "plan_name": plan, "suite": suite, "host": "Host_Linux",
            "sources": [
                {
                    "name": "Ubertooth", "source_path": checkout,
                    "repository": "https://github.com/SqueakyFloors/ubertooth.git",
                    "requested_ref": COMMIT, "commit": COMMIT,
                    "short_commit": COMMIT[:7], "ref_name": "2026-11-R1",
                    "build_version": "2026-11-R1",
                },
                {
                    "name": "libbtbb", "source_path": f"{home}/src/libbtbb",
                    "repository": "https://github.com/SqueakyFloors/libbtbb.git",
                    "requested_ref": LIBBTBB_COMMIT, "commit": LIBBTBB_COMMIT,
                    "short_commit": LIBBTBB_COMMIT[:7], "ref_name": "fork-pin",
                    "build_version": "2020-12-R1+79",
                },
            ],
            "invocations": [{
                "host": "Host_Linux",
                "resources": {
                    "Ubertooth": "UT_0", "guard Ubertooth 1": "UT_1",
                    "DK advertiser": "DK_0",
                },
                "resource_identities": {
                    "Ubertooth": {"serial": SERIAL_0},
                    "guard Ubertooth 1": {"serial": SERIAL_1},
                    "DK advertiser": {"jlink_serial": JLINK},
                },
            }],
            "environment": {"os": {
                "system": "Linux", "distribution": "24.04",
                "release": "6.8.0", "architecture": "x86_64",
            }},
        }}, 1, run_id),
        _event("lease_takeover", "warning", {"resource": "UT_0", "holder": {
            "owner": OPERATOR, "user": OPERATOR, "hostname": MACHINE,
            "pid": 4242, "run_id": "20261104T100000Z-000000000000",
        }}, 2, run_id),
        _event("bootloader_stage_finished", "passed",
               {"mode": bootloader_mode}, 3, run_id),
        _event("test_started", "running", {
            "invocation_id": "001-Host CLI surface", "test_name": "Host CLI surface",
            "host": "Host_Linux",
        }, 4, run_id),
        _event("output_checks", "passed", {
            "invocation_id": "001-Host CLI surface",
            "checks": [
                {"kind": "observed", "name": "channel", "value": "2441"},
                {"kind": "expected", "name": "usage line", "found": True},
            ],
        }, 5, run_id),
        _event("command_finished", "passed", {
            "command_id": "c1", "invocation_id": "001-Host CLI surface",
            "returncode": 0, "duration_s": 1.0,
            "output_files": [
                {"path": "output/c1.stderr.log", "stream": "stderr",
                 "bytes": 64, "lines": 2, "sha256": "0" * 64},
                {"path": "captures/advertising.pcapng", "stream": "",
                 "bytes": 2048, "lines": 0, "sha256": "1" * 64},
            ],
        }, 6, run_id),
        _event("test_finished", "passed", {
            "invocation_id": "001-Host CLI surface", "skipped_cases": 1,
        }, 7, run_id),
    ]
    second = 8
    for name, reason in failures:
        events.append(_event("test_started", "running", {
            "invocation_id": name, "test_name": name, "host": "Host_Linux",
        }, second, run_id))
        events.append(_event("test_finished", "failed", {
            "invocation_id": name, "reason": reason, "failed_cases": 1,
        }, second + 1, run_id))
        second += 2
    for name, reason in blocked:
        events.append(_event("blocked", "failed", {
            "test_name": name, "host": "Host_Linux", "reason": reason,
        }, second, run_id))
        second += 1
    for name, reason in not_applicable:
        events.append(_event("not_applicable", "skipped", {
            "invocation_id": name, "host": "Host_Linux", "reason": reason,
        }, second, run_id))
        second += 1
    events.append(_event("watchdog_summary", "passed", {
        "boards": [{"resource": "UT_0", "serial": SERIAL_0, "resets": 0}],
    }, second, run_id))
    events.append(_event("run_finished", status, {
        "status": status, "requested_tests": 1 + len(failures),
        "completed_tests": 1, "skipped_cases": 1,
        "failure_reason": "" if status == "passed" else "a test failed",
        "watchdog_resets": 0,
    }, second + 1, run_id))
    with (directory / "run.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
    (directory / "report.md").write_text(
        "# Ubertooth test run\n\n"
        f"| host | Host_Linux (Linux 24.04 x86_64) |\n"
        f"| device UT_0 | serial {SERIAL_0} |\n"
        f"| built in | {checkout}/host |\n"
        f"| port | /dev/serial/by-id/usb-SEGGER_J-Link_{JLINK}-if00 |\n\n"
        "```text\n"
        f"00:00:02.000 [M] lease taken over from {OPERATOR} on {MACHINE}\n"
        f"00:00:03.000 [C] ubertooth-util -U {SERIAL_0} -V\n"
        "00:00:03.100 [O] Host tools version: 2026-11-R1 (libbtbb 2020-12-R1+79)\n"
        f"{extra_transcript}"
        "```\n",
        encoding="utf-8",
    )
    (directory / "output" / "c1.stderr.log").write_text(
        f"opened {SERIAL_0} from {checkout}/build\n"
        f"holder {OPERATOR}@{MACHINE}\n",
        encoding="utf-8",
    )
    (directory / "captures" / "advertising.pcapng").write_bytes(b"\xa1\xb2\xc3\xd4")
    return directory


def members_of(path: Path) -> dict[str, bytes]:
    """Every member of the archive, by name."""
    import zstandard

    with path.open("rb") as handle:
        data = zstandard.ZstdDecompressor().stream_reader(handle).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
        return {
            item.name: archive.extractfile(item).read()
            for item in archive.getmembers()
        }


class GateTests(unittest.TestCase):
    def test_a_suite_belongs_to_the_gate_that_names_it(self):
        for suite, gate in (
            ("fast", "fast"), ("release", "release"),
            ("btbr-comprehensive", "alternate-image"),
            ("ble-comprehensive", "ble"), ("classic-long", "classic"),
            ("hci-comprehensive", "hci"), ("classic-weak-link", "weak-link"),
            ("firmware", "firmware"), ("slow", "firmware"),
            ("address-soak-72h", "soak-72h"), ("mac", "other"),
        ):
            with self.subTest(suite=suite):
                self.assertEqual(gate_of(suite), gate)

    def test_a_release_name_says_which_phase_it_is(self):
        for name, phase in (
            ("2026-11-DEV2", "snapshot"), ("2026-11-RC1", "rc"),
            ("2026-11-R1", "release"), ("main", "snapshot"),
        ):
            with self.subTest(name=name):
                self.assertEqual(phase_of_release(name), phase)

    def test_no_soak_is_ever_required(self):
        for gate in evidence.GATES:
            if gate.key.startswith("soak-"):
                self.assertEqual(gate.required_in, ())

    def test_a_failed_run_does_not_meet_its_gate(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            run = read_run(write_run(root, status="failed"))
            results = gate_results((run,))
            fast = next(item for item in results if item.gate.key == "fast")
            self.assertEqual(fast.runs, (run,))
            self.assertFalse(fast.met)
            self.assertIn(fast, missing_gates(results, "snapshot"))

    def test_the_bootloader_gate_wants_a_comparison_not_a_record(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            recorded = read_run(write_run(root, bootloader_mode="record"))
            checked = read_run(write_run(
                root, run_id="20261105T101233Z-abcdef123457",
                bootloader_mode="check"))
            for run, met in ((recorded, False), (checked, True)):
                result = next(item for item in gate_results((run,))
                              if item.gate.key == "bootloader")
                self.assertEqual(result.met, met)

    def test_an_attestation_meets_a_gate_no_suite_measures(self):
        results = gate_results((), {"installer": "install.sh on a clean prefix"})
        installer = next(item for item in results if item.gate.key == "installer")
        self.assertTrue(installer.met)
        self.assertIn("attested by hand", installer.describe())

    def test_a_gate_a_suite_measures_cannot_be_attested(self):
        with self.assertRaises(EvidenceError) as refused:
            evidence.check_attestations({"fast": "it went fine"})
        self.assertIn("cannot be attested", str(refused.exception))

    def test_an_unknown_gate_is_refused(self):
        with self.assertRaises(EvidenceError):
            evidence.check_attestations({"nonsense": "x"})

    def test_a_snapshot_requires_less_than_a_release(self):
        required = {
            phase: {
                gate.key for gate in evidence.GATES if gate.required_for(phase)
            }
            for phase in evidence.PHASES
        }
        self.assertEqual(required["snapshot"], {"fast"})
        self.assertLess(required["snapshot"], required["rc"])
        self.assertLess(required["rc"], required["release"])
        self.assertIn("installer", required["release"])


class ReadRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._clean)
        self.run = read_run(write_run(
            self.root,
            failures=(("004-Capture", "no advertisement was captured"),),
            blocked=(("BLE connection", "no second DK is registered"),),
            not_applicable=(("008-HCI", "this host has no BlueZ tools"),),
        ))

    def _clean(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def test_it_reads_the_identities_the_run_recorded(self):
        self.assertEqual(self.run.suite, "fast")
        self.assertEqual(self.run.gate, "fast")
        self.assertEqual(self.run.source("Ubertooth")["commit"], COMMIT)
        self.assertEqual(self.run.source("libbtbb")["commit"], LIBBTBB_COMMIT)
        self.assertEqual(self.run.platform, "Linux (24.04, x86_64)")
        self.assertEqual(self.run.started, "2026-11-05T09:12:33.000")

    def test_it_reads_the_deviations_rather_than_only_the_totals(self):
        self.assertEqual(
            self.run.failures,
            (("004-Capture", "failed: no advertisement was captured"),))
        self.assertEqual(
            self.run.blocked, (("BLE connection", "no second DK is registered"),))
        self.assertEqual(
            self.run.not_applicable,
            (("008-HCI", "this host has no BlueZ tools"),))
        self.assertEqual(self.run.skipped_cases, 1)

    def test_it_reads_the_values_the_run_observed(self):
        self.assertEqual(
            [(item.name, item.value) for item in self.run.measurements],
            [("channel", "2441")])

    def test_it_reads_the_files_and_knows_which_are_text(self):
        self.assertEqual(
            [(item.path, item.is_text) for item in self.run.files],
            [("captures/advertising.pcapng", False),
             ("output/c1.stderr.log", True)])

    def test_it_collects_the_names_the_export_has_to_remove(self):
        private = self.run.private
        self.assertIn(SERIAL_0, private.serials)
        self.assertIn(SERIAL_1, private.serials)
        self.assertIn(OPERATOR, private.users)
        self.assertIn(MACHINE, private.hosts)
        self.assertIn(str(self.run.directory.resolve()), private.paths)
        self.assertIn("/Users/ada/src/ubertooth", private.paths)

    def test_a_directory_with_no_records_is_refused(self):
        with self.assertRaises(EvidenceError):
            read_run(self.root / "nothing")

    def test_the_bench_aliases_become_public_labels(self):
        labels = public_labels((self.run,))
        self.assertEqual(labels, {
            "Host_Linux": "host 1",
            "DK_0": "DK advertiser 1",
            "UT_0": "Ubertooth 1",
            "UT_1": "guard Ubertooth 1",
        })

    def test_the_labels_are_the_same_whichever_order_the_roles_arrive(self):
        self.assertEqual(public_labels((self.run,)), public_labels((self.run,)))

    def test_every_run_is_masked_with_every_run_s_names(self):
        other = read_run(write_run(
            self.root, run_id="20261105T101233Z-abcdef123457", suite="release"))
        merged = private_names((self.run, other))
        self.assertIn(SERIAL_0, merged.serials)
        self.assertIn("UT_0", merged.aliases)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._clean)
        self.run = write_run(self.root)
        self.output = self.root / "evidence"

    def _clean(self):
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, **options):
        options.setdefault("release", "2026-11-R1")
        options.setdefault("phase", "snapshot")
        options.setdefault("harness", "f" * 40)
        options.setdefault("now", EXPORTED)
        runs = options.pop("runs", [self.run])
        output = options.pop("output", self.output)
        return build_bundle(runs, output, **options)

    def test_it_writes_the_three_assets_and_nothing_else(self):
        bundle = self.build()
        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()),
            sorted([SUMMARY_NAME, LOGS_NAME, SUMS_NAME]))
        self.assertTrue(bundle.complete)

    def test_the_archive_carries_the_text_the_run_recorded(self):
        bundle = self.build()
        members = members_of(bundle.logs)
        self.assertEqual(sorted(members), [
            MANIFEST_NAME,
            f"runs/{self.run.name}/output/c1.stderr.log",
            f"runs/{self.run.name}/report.md",
            f"runs/{self.run.name}/run.jsonl",
        ])

    def test_a_capture_stays_on_the_test_host_and_is_named_in_the_summary(self):
        bundle = self.build()
        members = members_of(bundle.logs)
        self.assertNotIn(f"runs/{self.run.name}/captures/advertising.pcapng",
                         members)
        text = bundle.summary.read_text(encoding="utf-8")
        self.assertIn("## Files kept on the test host", text)
        self.assertIn("captures/advertising.pcapng", text)
        self.assertIn("1" * 64, text)

    def test_the_manifest_hashes_every_other_member(self):
        bundle = self.build()
        members = members_of(bundle.logs)
        manifest = members.pop(MANIFEST_NAME).decode()
        listed = dict(
            (line.split("  ", 1)[1], line.split("  ", 1)[0])
            for line in manifest.splitlines()
        )
        self.assertEqual(sorted(listed), sorted(members))
        for name, body in members.items():
            self.assertEqual(listed[name], hashlib.sha256(body).hexdigest())

    def test_the_sums_file_covers_the_two_published_assets(self):
        bundle = self.build()
        listed = dict(
            (line.split("  ", 1)[1], line.split("  ", 1)[0])
            for line in bundle.sums.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(sorted(listed), sorted([SUMMARY_NAME, LOGS_NAME]))
        for name, digest in listed.items():
            body = (self.output / name).read_bytes()
            self.assertEqual(digest, hashlib.sha256(body).hexdigest())

    def test_the_same_runs_produce_the_same_archive_bytes(self):
        first = self.build().logs.read_bytes()
        second = self.build(output=self.root / "again").logs.read_bytes()
        self.assertEqual(first, second)

    def test_the_archive_headers_carry_nothing_of_this_host(self):
        import zstandard

        bundle = self.build()
        with bundle.logs.open("rb") as handle:
            data = zstandard.ZstdDecompressor().stream_reader(handle).read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as archive:
            for item in archive.getmembers():
                self.assertEqual((item.mtime, item.uid, item.gid), (0, 0, 0))
                self.assertEqual((item.uname, item.gname), ("", ""))
                self.assertEqual(item.mode, 0o644)

    def test_nothing_private_reaches_any_asset(self):
        bundle = self.build()
        published = [bundle.summary.read_text(encoding="utf-8")]
        published += [
            body.decode("utf-8") for body in members_of(bundle.logs).values()
        ]
        for text in published:
            for secret in (SERIAL_0, SERIAL_1, JLINK, OPERATOR, MACHINE,
                           "/Users/ada", "UT_0", "UT_1", "Host_Linux"):
                self.assertNotIn(secret, text)

    def test_the_placeholders_say_what_was_taken_out(self):
        bundle = self.build()
        report = members_of(bundle.logs)[f"runs/{self.run.name}/report.md"]
        text = report.decode("utf-8")
        self.assertIn(f"serial {SERIAL}", text)
        # The source checkout a run records is stripped as the prefix it is,
        # so the path below it is still readable.
        self.assertIn(f"{PATH}/host", text)
        self.assertIn(f"lease taken over from {USER} on {HOST}", text)
        self.assertIn("Ubertooth 1", text)
        self.assertIn("host 1", text)

    def test_a_bench_path_it_does_not_recognize_fails_closed(self):
        run = write_run(
            self.root, run_id="20261105T111233Z-abcdef123458",
            extra_transcript="00:00:05.000 [M] notes in /srv/bench/notes.txt\n")
        with self.assertRaises(Unsafe) as refused:
            self.build(runs=[run], output=self.root / "unsafe")
        self.assertIn("/srv/bench/notes.txt", str(refused.exception))
        self.assertFalse((self.root / "unsafe" / SUMMARY_NAME).exists())
        self.assertFalse((self.root / "unsafe" / LOGS_NAME).exists())

    def test_a_serial_no_record_registers_fails_closed(self):
        # The gate is read over the finished text, so a serial the harness's
        # own masking never knew about is refused rather than published.
        run = write_run(
            self.root, run_id="20261105T131233Z-abcdef123460",
            extra_transcript="00:00:04.000 [O] port /dev/serial/by-id/"
                             "usb-SEGGER_J-Link_000987654321-if00\n")
        with self.assertRaises(Unsafe) as refused:
            self.build(runs=[run], output=self.root / "stranger")
        self.assertIn("a device serial", str(refused.exception))

    def test_naming_that_prefix_makes_the_same_run_exportable(self):
        run = write_run(
            self.root, run_id="20261105T111233Z-abcdef123459",
            extra_transcript="00:00:05.000 [M] notes in /srv/bench/notes.txt\n")
        bundle = self.build(runs=[run], output=self.root / "stripped",
                            strip_paths=["/srv/bench"])
        text = bundle.summary.read_text(encoding="utf-8")
        self.assertNotIn("/srv/bench", text)
        for body in members_of(bundle.logs).values():
            self.assertNotIn(b"/srv/bench", body)

    def test_what_a_release_is_judged_on_is_kept(self):
        text = self.build().summary.read_text(encoding="utf-8")
        for kept in (COMMIT, LIBBTBB_COMMIT, "2026-11-R1", "fork-pin",
                     "Linux (24.04, x86_64)", "2026-11-05T09:12:33.000",
                     "f" * 40, "channel", "2441"):
            self.assertIn(kept, text)

    def test_a_missing_gate_is_listed_and_the_upload_withheld(self):
        bundle = self.build(phase="release")
        text = bundle.summary.read_text(encoding="utf-8")
        self.assertIn("Incomplete", text)
        self.assertIn("## Missing required evidence", text)
        for title in ("Canonical non-soak release suite", "Installer acceptance",
                      "Bootloader acceptance", "Focused BLE suites"):
            self.assertIn(title, text)
        self.assertFalse(bundle.complete)
        self.assertEqual(bundle.upload, "")

    def test_a_complete_bundle_says_so_and_offers_the_command(self):
        bundle = self.build()
        text = bundle.summary.read_text(encoding="utf-8")
        self.assertNotIn("Incomplete", text)
        self.assertIn("None: every gate this phase requires", text)
        self.assertEqual(bundle.upload, (
            "gh release upload 2026-11-R1 \\\n"
            f"  {SUMMARY_NAME} {LOGS_NAME} {SUMS_NAME} \\\n"
            "  --repo SqueakyFloors/ubertooth"
        ))

    def test_an_acknowledged_gap_gets_the_command_back(self):
        bundle = self.build(phase="release", allow_incomplete=True)
        self.assertIn("## Missing required evidence",
                      bundle.summary.read_text(encoding="utf-8"))
        self.assertIn("gh release upload", bundle.upload)

    def test_a_private_bundle_keeps_serials_under_its_own_names(self):
        bundle = self.build(private_bundle=True, output=self.root / "private")
        self.assertEqual(bundle.summary.name, PRIVATE_SUMMARY_NAME)
        self.assertEqual(bundle.logs.name, PRIVATE_LOGS_NAME)
        self.assertEqual(bundle.upload, "")
        text = bundle.summary.read_text(encoding="utf-8")
        self.assertIn("Private bundle", text)
        report = members_of(bundle.logs)[f"runs/{self.run.name}/report.md"]
        self.assertIn(SERIAL_0, report.decode("utf-8"))
        # Only the serials: a private bundle still has no paths, no machine
        # name and no operator in it.
        for secret in (OPERATOR, MACHINE, "/Users/ada"):
            self.assertNotIn(secret, report.decode("utf-8"))

    def test_a_rerun_of_one_suite_is_reported_as_a_deviation(self):
        again = write_run(self.root, run_id="20261105T121233Z-abcdef123459")
        bundle = self.build(runs=[self.run, again], output=self.root / "rerun")
        self.assertIn("was run 2 times in this evidence",
                      bundle.summary.read_text(encoding="utf-8"))

    def test_runs_that_disagree_on_a_commit_say_so(self):
        with tempfile.TemporaryDirectory() as other:
            directory = write_run(Path(other))
            events = [
                json.loads(line) for line in
                (directory / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            for event in events:
                sources = (event.get("details", {}).get("manifest") or {}).get("sources")
                for source in sources or ():
                    if source["name"] == "Ubertooth":
                        source["commit"] = "0" * 40
            with (directory / "run.jsonl").open("w", encoding="utf-8") as stream:
                for event in events:
                    stream.write(json.dumps(event, sort_keys=True) + "\n")
            bundle = self.build(runs=[self.run, directory],
                                output=self.root / "disagree")
            self.assertIn("the runs do not agree on this commit",
                          bundle.summary.read_text(encoding="utf-8"))

    def test_it_refuses_an_export_it_cannot_identify(self):
        with self.assertRaises(EvidenceError):
            build_bundle([], self.output, release="2026-11-R1")
        with self.assertRaises(EvidenceError):
            build_bundle([self.run], self.output, release="")
        with self.assertRaises(EvidenceError):
            build_bundle([self.run], self.output, release="x", phase="nonsense")

    def test_the_phase_comes_from_the_release_name_when_it_is_not_given(self):
        bundle = build_bundle([self.run], self.root / "inferred",
                              release="2026-11-DEV2", harness="", now=EXPORTED)
        self.assertIn("development snapshot",
                      bundle.summary.read_text(encoding="utf-8"))
        self.assertTrue(bundle.complete)

    def test_a_summary_with_no_members_still_renders(self):
        export = Export(runs=(), release="2026-11-R1", phase="release",
                        now=EXPORTED)
        text = summary(export)
        self.assertIn("## Missing required evidence", text)
        self.assertIn("None.", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
