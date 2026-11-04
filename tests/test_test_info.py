"""The committed test information file, its schema, and what /api/plans makes
of it.

No hardware and no server: the file is read from the repository, the schema
is driven with crafted files in a temporary directory, and the API shape is
read from ``list_plans``, which is the code the endpoint runs. Nothing here
runs a test, builds anything, or touches a board -- which is itself one of
the things asserted, since a missing or malformed file must never stop a run.
"""

import inspect
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import unittest
import unittest.mock

import yaml

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.gui import list_plans, test_information_view
from ubertooth_harness.testinfo import (
    FIELDS,
    MAX_FIELD_CHARS,
    MAX_RELATED,
    PROSE_FIELDS,
    REPOSITORIES,
    TEST_INFO_NAME,
    TEST_INFO_PATH,
    TestInfoError,
    TestInformation,
    anchor,
    documentation,
    load_test_info,
    test_information,
)

REPOSITORY = Path(__file__).resolve().parents[1]
PLANS = REPOSITORY / "testplans"
DOCUMENT = REPOSITORY / "docs" / "tests.md"

# One well-formed entry, as the crafted files below start from.
ENTRY = {
    "purpose": "What this catches.",
    "theory": "Why the answer is knowable.",
    "hardware": "One Ubertooth. Nothing transmits.",
    "expected": "One case: it passes.",
    "limitations": "Not established.",
    "related": ["ubertooth-test-harness#315"],
}


def write(directory, mapping=None, text=None) -> Path:
    """A test information file in ``directory``, from a mapping or raw text."""
    path = Path(directory) / "test_info.yaml"
    path.write_text(
        text if text is not None else yaml.safe_dump(mapping, sort_keys=False),
        encoding="utf-8",
    )
    return path


class CommittedFileTests(unittest.TestCase):
    """The file in this repository: complete, valid, and in catalog order."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.info = load_test_info(catalog=cls.catalog)

    def test_every_catalog_test_has_an_entry(self):
        # The AGENTS.md rule, enforced: a test added without its entry fails
        # here rather than quietly losing its information in the GUI.
        self.assertEqual([], [n for n in self.catalog.names if n not in self.info])
        self.assertEqual(len(self.catalog.names), len(self.info))

    def test_the_entries_are_in_catalog_order(self):
        # Same order as catalog.py and docs/tests.md, so the three read
        # alongside each other.
        self.assertEqual(list(self.catalog.names), list(self.info))

    def test_every_field_is_present_and_says_something(self):
        for name, entry in self.info.items():
            for field in PROSE_FIELDS:
                value = getattr(entry, field)
                with self.subTest(test=name, field=field):
                    self.assertTrue(value.strip())
                    self.assertLessEqual(len(value), MAX_FIELD_CHARS)
                    # One block of prose, not a heading or a list pasted in.
                    self.assertFalse(value.startswith(("#", "* ")))

    def test_a_test_with_no_implementation_says_so_rather_than_guessing(self):
        # A name with no cases behind it says so where a reader looks for the
        # result, rather than describing an outcome nobody has seen (#315).
        for name in self.catalog.names:
            if self.catalog.get(name).implemented:
                continue
            with self.subTest(test=name):
                self.assertIn("no implementation", self.info[name].expected.lower())

    def test_the_weak_link_tests_explain_what_attenuation_is_for(self):
        # The entries a weak-link plan points at have to say what the
        # attenuated link buys and what it does not (#315). The shared
        # account is in docs/tests.md; each entry names its own figures, or
        # says what it is waiting for and why.
        from ubertooth_harness import weaklink

        for check in weaklink.CHECKS:
            entry = self.info[check.test]
            text = " ".join(
                getattr(entry, field) for field in PROSE_FIELDS
            ).lower()
            with self.subTest(test=check.test):
                self.assertIn("weak", text)
                self.assertRegex(text, r"attenuat|\bdb\b")

    def test_no_entry_carries_an_argument_default_or_a_required_flag(self):
        # The catalog owns which arguments a test takes, which are required,
        # and what they default to, and /api/plans reports all three beside
        # the prose; an entry that wrote one down would be a second source to
        # drift (#315). What an entry may say is what the argument is for.
        #
        # Built from the catalog, so an argument added later is covered
        # without editing this test: for every argument of the test, the
        # prose may not say it is required, may not give it a default, and
        # may not carry a parenthesized default at all. A boot default, a
        # tool's own default, and an RF placement default are not the
        # catalog's, so the word "default" on its own stays allowed.
        for name, entry in self.info.items():
            definition = self.catalog.get(name)
            prose = " ".join(getattr(entry, field) for field in PROSE_FIELDS)
            with self.subTest(test=name):
                self.assertNotRegex(prose, r"\(default[s]?\b")
                for argument in definition.arguments:
                    if argument in (definition.hardware_roles or {}):
                        # A role's argument is the board selector, and the
                        # prose says what the role does and what placement
                        # the test needs of it, which is the point of the
                        # field. "the Ubertooth defaults to needing an
                        # ordinary link" is about the bench, not about the
                        # selector's default value.
                        continue
                    quoted = re.escape(argument)
                    for pattern in (
                        rf"\brequired {quoted}\b",
                        rf"\b{quoted} (?:is|are) required\b",
                        rf"\b{quoted}\W{{0,3}} defaults? (?:to|of)\b",
                    ):
                        self.assertNotRegex(
                            prose, pattern,
                            f"{argument} is the catalog's to describe",
                        )

    def test_related_references_resolve_to_a_known_repository(self):
        for name, entry in self.info.items():
            for item in entry.related:
                with self.subTest(test=name, reference=item.reference):
                    repository = item.reference.split("#")[0]
                    self.assertIn(repository, REPOSITORIES)
                    self.assertTrue(item.url.startswith("https://github.com/"))
                    self.assertIn(REPOSITORIES[repository], item.url)

    def test_every_entry_points_at_a_real_documentation_section(self):
        # The anchors are derived from the catalog name, and docs/tests.md
        # headings are the catalog names, so a heading that drifted fails
        # here rather than giving the GUI a dead link.
        headings = {
            anchor(line.removeprefix("## ").strip())
            for line in DOCUMENT.read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        }
        for name in self.info:
            with self.subTest(test=name):
                self.assertIn(documentation(name)["anchor"], headings)

    def test_the_file_carries_no_lab_detail(self):
        # This repository is public: no IP addresses, no `user@host`, no
        # home-directory paths of a lab machine (AGENTS.md, "Public text").
        text = TEST_INFO_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotRegex(text, r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+")
        self.assertNotRegex(text, r"/(home|Users)/")


class SchemaTests(unittest.TestCase):
    """What the loader refuses, and why a reader would want it refused."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()

    def refused(self, mapping=None, text=None) -> str:
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, mapping, text)
            with self.assertRaises(TestInfoError) as caught:
                load_test_info(path, self.catalog)
            return str(caught.exception)

    def loaded(self, mapping=None, text=None):
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, mapping, text)
            return load_test_info(path, self.catalog)

    def test_a_well_formed_entry_loads(self):
        info = self.loaded({"Host version": dict(ENTRY)})
        self.assertEqual(["Host version"], list(info))
        entry = info["Host version"]
        self.assertEqual("What this catches.", entry.purpose)
        self.assertEqual(
            [("ubertooth-test-harness#315",
              "https://github.com/SqueakyFloors/ubertooth-test-harness/issues/315")],
            [(item.reference, item.url) for item in entry.related],
        )

    def test_an_unknown_test_name_is_refused(self):
        reason = self.refused({"Host versions": dict(ENTRY)})
        self.assertIn("no catalog test is named 'Host versions'", reason)

    def test_a_duplicate_entry_is_refused(self):
        # PyYAML would keep the last of two, so the first test's information
        # would be lost while the file looked complete.
        text = (
            "Host version:\n"
            + "".join(f"  {k}: x\n" for k in PROSE_FIELDS)
            + "  related: []\n"
            "Host version:\n"
            + "".join(f"  {k}: y\n" for k in PROSE_FIELDS)
            + "  related: []\n"
        )
        self.assertIn("duplicate entry for 'Host version'", self.refused(text=text))

    def test_malformed_yaml_is_refused_with_the_parser_s_reason(self):
        reason = self.refused(text="Host version: [unclosed\n")
        self.assertIn("unable to load", reason)

    def test_a_key_that_is_not_a_test_name_is_refused_as_such(self):
        # A YAML complex key builds a list, which is unhashable: the loader
        # has to say so rather than raising TypeError out of PyYAML.
        reason = self.refused(text="? [a, b]\n: x\n")
        self.assertIn("a key must be a test name, not list", reason)

    def test_a_byte_in_another_encoding_is_refused_as_unreadable(self):
        # What an editor leaves when it saves a cp1252 quote or accent.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test_info.yaml"
            path.write_bytes(b"Host version:\n  purpose: caf\xe9\n")
            with self.assertRaises(TestInfoError) as caught:
                load_test_info(path, self.catalog)
        self.assertIn("unable to load", str(caught.exception))

    def test_a_file_that_is_not_a_mapping_is_refused(self):
        self.assertIn("non-empty mapping", self.refused(text="- Host version\n"))
        self.assertIn("non-empty mapping", self.refused(text="{}\n"))

    def test_a_missing_field_is_refused(self):
        for field in FIELDS:
            entry = {k: v for k, v in ENTRY.items() if k != field}
            with self.subTest(field=field):
                reason = self.refused({"Host version": entry})
                self.assertIn(f"missing field(s) {field}", reason)

    def test_an_unknown_field_is_refused(self):
        reason = self.refused({"Host version": {**ENTRY, "runtime": "fast"}})
        self.assertIn("unknown field(s) runtime", reason)

    def test_an_empty_field_is_refused_and_says_what_to_write(self):
        reason = self.refused({"Host version": {**ENTRY, "theory": "   "}})
        self.assertIn("theory must not be empty", reason)
        self.assertIn("not established", reason)

    def test_a_field_that_is_not_a_string_is_refused(self):
        self.assertIn(
            "purpose must be a string",
            self.refused({"Host version": {**ENTRY, "purpose": 7}}),
        )

    def test_an_overlong_field_is_refused_and_points_at_the_document(self):
        long = "x" * (MAX_FIELD_CHARS + 1)
        reason = self.refused({"Host version": {**ENTRY, "expected": long}})
        self.assertIn("docs/tests.md", reason)

    def test_an_entry_that_is_not_a_mapping_is_refused(self):
        self.assertIn(
            "an entry must be a mapping",
            self.refused({"Host version": "see the docs"}),
        )

    def test_a_malformed_reference_is_refused(self):
        for value in ("315", "#315", "ubertooth 315", "ubertooth#0",
                      "ubertooth#1234567", "https://example.invalid/315"):
            with self.subTest(reference=value):
                reason = self.refused({"Host version": {**ENTRY, "related": [value]}})
                self.assertIn("related reference", reason)

    def test_a_reference_to_an_unknown_repository_is_refused(self):
        reason = self.refused({"Host version": {**ENTRY, "related": ["kernel#315"]}})
        self.assertIn("names no known repository", reason)

    def test_related_must_be_a_list_of_strings(self):
        self.assertIn(
            "related must be a list",
            self.refused({"Host version": {**ENTRY, "related": "ubertooth#1"}}),
        )
        self.assertIn(
            "non-empty string",
            self.refused({"Host version": {**ENTRY, "related": [None]}}),
        )

    def test_a_repeated_reference_is_refused(self):
        reason = self.refused(
            {"Host version": {**ENTRY, "related": ["ubertooth#1", "ubertooth#1"]}}
        )
        self.assertIn("related repeats ubertooth#1", reason)

    def test_too_many_references_are_refused(self):
        many = [f"ubertooth#{n}" for n in range(1, MAX_RELATED + 2)]
        reason = self.refused({"Host version": {**ENTRY, "related": many}})
        self.assertIn(f"at most {MAX_RELATED}", reason)

    def test_an_empty_related_list_is_accepted(self):
        info = self.loaded({"Host version": {**ENTRY, "related": []}})
        self.assertEqual((), info["Host version"].related)

    def test_the_anchor_of_a_name_is_its_heading_slug(self):
        self.assertEqual("host-version", anchor("Host version"))
        self.assertEqual("ble-extcap-in-tshark", anchor("BLE extcap in tshark"))
        self.assertEqual(
            "follow-a-ble-connection-across-its-end",
            anchor("Follow a BLE connection across its end"),
        )


class UnavailableTests(unittest.TestCase):
    """A missing file, a malformed one, and a test with no entry."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()

    def test_a_missing_file_leaves_every_test_unavailable_and_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            information = test_information(
                Path(directory) / "absent.yaml", self.catalog
            )
            self.assertEqual({}, dict(information.entries))
            view = information.view("Host version")
            self.assertIs(False, view["available"])
            self.assertEqual(f"{TEST_INFO_NAME} is missing",
                             view["unavailable_reason"])
            # The full document is still linked: the anchor is derived from
            # the name, not read from the file.
            self.assertEqual(documentation("Host version"), view["documentation"])
            self.assertEqual(
                list(self.catalog.names), list(information.missing(self.catalog))
            )

    def test_a_malformed_file_names_the_reason_without_the_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, text="Host version: [unclosed\n")
            information = test_information(path, self.catalog)
            reason = information.view("Host version")["unavailable_reason"]
            self.assertTrue(reason.startswith(f"{TEST_INFO_NAME} could not be read"))
            # One line, and nothing about where this checkout happens to be.
            self.assertNotIn("\n", reason)
            self.assertNotIn(str(directory), reason)

    def test_nothing_about_the_file_reaches_the_caller_as_an_exception(self):
        # The controller serves the plans listing from here, so a file it
        # cannot read must cost one information block and not the response.
        # Each of these raised out of an earlier version of the loader.
        files = {
            "another encoding": b"Host version:\n  purpose: caf\xe9\n",
            "a complex key": b"? [a, b]\n: x\n",
            "a list at the root": b"- Host version\n",
            "a flow sequence left open": b"Host version: [unclosed\n",
            "nothing at all": b"",
            "a binary blob": bytes(range(256)),
        }
        for description, content in files.items():
            with self.subTest(file=description):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "test_info.yaml"
                    path.write_bytes(content)
                    information = test_information(path, self.catalog)
                    view = information.view("Host version")
                self.assertIs(False, view["available"])
                self.assertIn("could not be read", view["unavailable_reason"])
                self.assertNotIn("\n", view["unavailable_reason"])

    def test_a_directory_in_place_of_the_file_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test_info.yaml"
            path.mkdir()
            view = test_information(path, self.catalog).view("Host version")
        self.assertIs(False, view["available"])
        self.assertIn("could not be read", view["unavailable_reason"])

    def test_the_reason_names_the_entry_that_is_wrong(self):
        # One bad entry makes every test's information unavailable, which is
        # strict on purpose; the reason has to say which entry it was, or a
        # reader is told only that the file is broken.
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, {"Spectrum analyzer": {**ENTRY, "theory": " "}})
            reason = test_information(path, self.catalog).view(
                "Host version")["unavailable_reason"]
        self.assertIn("Spectrum analyzer", reason)
        self.assertIn("theory must not be empty", reason)

    def test_the_answer_is_held_while_the_file_on_disk_is_the_same_one(self):
        # /api/plans asks for this on every page load and change of target,
        # and a read is about 70 ms, so the answer is held. Each part of the
        # key is exercised on its own, because any one of them left out
        # would serve an answer from a file that is no longer there.
        other = {**ENTRY, "purpose": "A different purpose of the same length."}
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, {"Host version": dict(ENTRY)})
            first = test_information(path, self.catalog)
            self.assertIs(first, test_information(path, self.catalog))

            # Same size and same inode, older modification time: the
            # modification time alone has to make it read again.
            stat = path.stat()
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "What this catches.", "What this catches!"),
                encoding="utf-8",
            )
            self.assertEqual(stat.st_size, path.stat().st_size)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns - 1_000_000))
            by_mtime = test_information(path, self.catalog)
            self.assertIsNot(first, by_mtime)
            self.assertIn("catches!", by_mtime.entries["Host version"].purpose)

            # A file put in place by rename, carrying the modification time
            # and the size of the one it replaced: only the inode differs,
            # which is how a replacement is told from the original.
            held = path.stat()
            replacement = Path(directory) / "replacement.yaml"
            replacement.write_text(
                path.read_text(encoding="utf-8").replace(
                    "What this catches!", "What this catches?"),
                encoding="utf-8",
            )
            self.assertEqual(held.st_size, replacement.stat().st_size)
            self.assertNotEqual(held.st_ino, replacement.stat().st_ino)
            os.utime(replacement, ns=(held.st_atime_ns, held.st_mtime_ns))
            os.replace(replacement, path)
            self.assertEqual(held.st_mtime_ns, path.stat().st_mtime_ns)
            by_inode = test_information(path, self.catalog)
            self.assertIsNot(by_mtime, by_inode)
            self.assertIn("catches?", by_inode.entries["Host version"].purpose)

            # A file that is gone is not answered from the held copy.
            path.unlink()
            gone = test_information(path, self.catalog)
            self.assertEqual({}, dict(gone.entries))
            self.assertIn("is missing", gone.error)
            # Nor is a missing file itself held: the moment it is back, it is
            # read, although nothing about it has changed in the meantime.
            self.assertIsNot(gone, test_information(path, self.catalog))
            write(directory, {"Spectrum analyzer": dict(ENTRY)})
            back = test_information(path, self.catalog)
            self.assertEqual(["Spectrum analyzer"], list(back.entries))

    def test_a_different_catalog_is_not_answered_from_the_held_copy(self):
        # The entries are validated against the catalog's names, so an
        # answer belongs to the catalog it was read against.
        class Narrow:
            names = ("Spectrum analyzer",)
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, {"Host version": dict(ENTRY)})
            self.assertEqual(
                ["Host version"], list(test_information(path, self.catalog).entries)
            )
            narrowed = test_information(path, Narrow())
            self.assertEqual({}, dict(narrowed.entries))
            self.assertIn("no catalog test is named", narrowed.error)

    def test_a_test_with_no_entry_is_unavailable_on_its_own(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, {"Host version": dict(ENTRY)})
            information = test_information(path, self.catalog)
            self.assertIs(True, information.view("Host version")["available"])
            view = information.view("Spectrum analyzer")
            self.assertIs(False, view["available"])
            self.assertEqual(
                f"no {TEST_INFO_NAME} entry for this test",
                view["unavailable_reason"],
            )
            self.assertNotIn("Host version", information.missing(self.catalog))
            self.assertIn("Spectrum analyzer", information.missing(self.catalog))

    def test_an_unavailable_block_has_the_same_fields_as_an_entry(self):
        # A client reads the same shape either way and only looks at
        # `available`.
        available = test_information(catalog=self.catalog).view("Host version")
        missing = TestInformation({}, "").view("Host version")
        self.assertEqual(sorted(available), sorted(missing))
        for field in PROSE_FIELDS:
            self.assertEqual("", missing[field])
        self.assertEqual([], missing["related"])


class PlansApiTests(unittest.TestCase):
    """What ``/api/plans`` reports, which is what ``list_plans`` builds."""

    @classmethod
    def setUpClass(cls):
        cls.catalog = builtin_catalog()
        cls.plans = {plan["name"]: plan for plan in list_plans(PLANS)}

    def rows(self, plans=None):
        for plan in (plans or self.plans).values():
            for suite in plan["suites"]:
                for test in suite["tests"]:
                    yield test

    def test_every_listed_test_has_an_entry_in_the_response(self):
        # The prose is sent once per response, keyed by the catalog name the
        # rows carry, so every row can be looked up and nothing is repeated.
        view = test_information_view(list(self.plans.values()))
        rows = list(self.rows())
        self.assertGreater(len(rows), len(view))  # a test appears in several
        self.assertEqual(sorted({test["name"] for test in rows}), sorted(view))
        for name, info in view.items():
            with self.subTest(test=name):
                self.assertIs(True, info["available"], info["unavailable_reason"])
                self.assertEqual("", info["unavailable_reason"])
                for field in PROSE_FIELDS:
                    self.assertTrue(info[field].strip())
                self.assertEqual(documentation(name), info["documentation"])

    def test_the_prose_is_not_repeated_on_every_row(self):
        # What the per-response map is for: 122 steps of 53 tests in the
        # repository's own plans, so a row-level copy tripled the response.
        for test in self.rows():
            with self.subTest(test=test["name"]):
                for field in PROSE_FIELDS:
                    self.assertNotIn(field, test)
                self.assertNotIn("info", test)

    def test_the_hardware_and_arguments_come_from_the_catalog(self):
        for test in self.rows():
            definition = self.catalog.get(test["name"])
            with self.subTest(test=test["name"]):
                self.assertEqual(
                    [{"role": role, "device": device}
                     for role, device in (definition.hardware_roles or {}).items()],
                    test["hardware_roles"],
                )
                self.assertEqual(
                    [{"name": name, "required": rule.required}
                     for name, rule in definition.arguments.items()],
                    test["arguments"],
                )

    def test_no_entry_restates_the_catalog_estimate_of_a_planned_step(self):
        # The catalog owns the wall-time estimate and the row beside the
        # prose carries it, so an entry that wrote the figure down would be a
        # second source to drift (#315). Checked against the estimate of
        # every step a committed plan compiles for the test, which is the
        # figure a reader of that row sees: a duration a case runs for is
        # fine, the estimate for the invocation is not.
        pattern = re.compile(r"\b(\d+(?:\.\d+)?)\s?s\b")
        estimates = {}
        for test in self.rows():
            if test["catalog_estimated_s"] is not None:
                estimates.setdefault(test["name"], set()).add(
                    float(test["catalog_estimated_s"])
                )
        self.assertGreater(len(estimates), 20)
        information = test_information(catalog=self.catalog)
        for name, figures in estimates.items():
            prose = " ".join(
                getattr(information.entries[name], field)
                for field in PROSE_FIELDS
            )
            said = {float(found) for found in pattern.findall(prose)}
            with self.subTest(test=name):
                self.assertEqual(
                    set(), said & figures,
                    "names a figure in seconds that is this test's own "
                    "catalog estimate; the estimate belongs to catalog.py",
                )

    def test_the_estimate_profile_names_the_estimate_beside_it(self):
        # The estimate on the row is the catalog's for this profile, so the
        # dialog can say which profile the figure belongs to.
        found = set()
        for test in self.rows():
            definition = self.catalog.get(test["name"])
            profile = test["estimate_profile"]
            with self.subTest(test=test["name"]):
                if not definition.estimates:
                    self.assertIsNone(profile)
                    continue
                self.assertIn(profile, definition.estimates)
                found.add(profile)
        self.assertIn("smoke", found)
        self.assertIn("default", found)

    def test_a_board_test_names_the_roles_it_binds(self):
        rows = {test["name"]: test for test in self.rows()}
        advertiser = rows["Capture known BLE advertiser"]
        self.assertEqual(
            [{"role": "Ubertooth", "device": "Ubertooth"},
             {"role": "advertiser", "device": "nRF52840 DK"}],
            advertiser["hardware_roles"],
        )
        self.assertIn(
            {"name": "payload", "required": False}, advertiser["arguments"]
        )
        # A test with no board role says so with an empty list.
        self.assertEqual([], rows["Host CLI surface"]["hardware_roles"])

    def test_the_prose_is_passed_through_as_data_and_never_as_markup(self):
        # Escaping belongs to whoever renders it. What the API must not do is
        # build markup, so a crafted entry comes back as the string it is.
        payload = "<script>alert('x')</script> & \"quoted\" <b>"
        entry = {**ENTRY, "purpose": payload}
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, {"Host CLI surface": entry})
            view = test_information_view(
                list(self.plans.values()),
                test_information(path, self.catalog),
            )
        info = view["Host CLI surface"]
        self.assertEqual(payload, info["purpose"])
        self.assertNotIn("&amp;", info["purpose"])
        self.assertNotIn("&lt;", info["purpose"])

    def test_a_missing_file_does_not_stop_the_listing_or_a_run(self):
        plans = list(self.plans.values())
        with tempfile.TemporaryDirectory() as directory:
            view = test_information_view(
                plans,
                test_information(Path(directory) / "absent.yaml", self.catalog),
            )
        # The listing itself is untouched: the plans, their suites and every
        # estimate are what they are without the file.
        self.assertEqual(
            sorted({test["name"] for test in self.rows()}), sorted(view)
        )
        for name, info in view.items():
            with self.subTest(test=name):
                self.assertIs(False, info["available"])
                self.assertIn("is missing", info["unavailable_reason"])
                # The link to the full document is derived from the name.
                self.assertEqual(documentation(name), info["documentation"])
        for test in self.rows():
            # The catalog half does not come from the file at all.
            self.assertIsInstance(test["arguments"], list)
            self.assertIsInstance(test["hardware_roles"], list)

    def test_a_malformed_file_does_not_stop_the_listing_either(self):
        # The same guarantee as a missing file, for the file a reader is more
        # likely to produce: one byte saved in another encoding.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test_info.yaml"
            path.write_bytes(b"Host CLI surface:\n  purpose: caf\xe9\n")
            view = test_information_view(
                list(self.plans.values()), test_information(path, self.catalog)
            )
        for name, info in view.items():
            with self.subTest(test=name):
                self.assertIs(False, info["available"])
                self.assertIn("could not be read", info["unavailable_reason"])

    def test_the_listing_reads_one_committed_file_and_no_client_path(self):
        # What is asserted here: the file read is the one beside the package,
        # and the only way to read another is a parameter that is
        # keyword-only with a default, which no request value reaches. That
        # the endpoint takes nothing but `target` is asserted over HTTP in
        # tests.test_gui.
        self.assertEqual(
            REPOSITORY / "ubertooth_harness" / "test_info.yaml", TEST_INFO_PATH
        )
        parameters = inspect.signature(test_information_view).parameters
        self.assertEqual(["plans", "information"], list(parameters))
        self.assertIsNone(parameters["information"].default)
        self.assertNotIn("information", inspect.signature(list_plans).parameters)


class NoExecutionTests(unittest.TestCase):
    """Reading the information runs nothing: no command, no board, no build."""

    def test_reading_the_information_starts_no_process_and_opens_no_socket(self):
        # Driven, not grepped: every way of starting a process or opening a
        # socket raises, and reading the real file and building a view for
        # every catalog test still has to work.
        catalog = builtin_catalog()

        def refuse(*args, **kwargs):
            raise AssertionError("the information must not run anything")

        with unittest.mock.patch.multiple(
            subprocess, run=refuse, Popen=refuse, check_output=refuse,
            call=refuse, check_call=refuse,
        ), unittest.mock.patch.object(socket, "socket", refuse), \
                unittest.mock.patch.object(os, "system", refuse), \
                unittest.mock.patch.object(os, "fork", refuse, create=True):
            information = test_information(catalog=catalog)
            self.assertEqual((), information.missing(catalog))
            for name in catalog.names:
                self.assertIs(True, information.view(name)["available"])

    def test_a_test_with_no_information_is_still_a_test_a_plan_can_name(self):
        # The gate a run passes is the plan and the catalog, neither of which
        # reads this file: a suite whose tests have no entry still compiles.
        from ubertooth_harness.plan import load_plan

        with tempfile.TemporaryDirectory() as directory:
            information = test_information(Path(directory) / "absent.yaml")
            plan = load_plan(PLANS / "fast.yaml")
            names = [step.test_name for step in plan.steps("fast")]
            self.assertTrue(names)
            for name in names:
                self.assertIs(False, information.view(name)["available"])
            plans = {p["name"]: p for p in list_plans(PLANS)}
            (suite,) = plans["fast"]["suites"]
            self.assertEqual(names, [test["name"] for test in suite["tests"]])
            self.assertIs(True, suite["runnable_on_target"])


if __name__ == "__main__":
    unittest.main()
