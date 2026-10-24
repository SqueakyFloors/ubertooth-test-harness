"""Web GUI served by the controller: assets, headers, refs, plans, failures.

No browser and no hardware: the controller listens on 127.0.0.1 with an
ephemeral port, git ls-remote is scripted (one test runs the real git against
a local repository), and the page is checked statically for the endpoints it
wires to and for HTML injection sinks.
"""

import contextlib
import io
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from ubertooth_harness.gui import (
    ASSETS,
    WEB_DIR,
    GitRefSource,
    failure_summary,
    list_plans,
    parse_ls_remote,
    run_git,
)
from ubertooth_harness.plan import load_plan

try:
    from test_controller import (
        COMMIT, ControllerTestCase, FakeRunner, event, fake_ref_source, wait_until,
    )
except ImportError:  # run as tests.test_gui
    from tests.test_controller import (
        COMMIT, ControllerTestCase, FakeRunner, event, fake_ref_source, wait_until,
    )

REPOSITORY = Path(__file__).resolve().parents[1]
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


class GuiServingTests(ControllerTestCase):

    def assert_security_headers(self, response, host=None):
        host = host or f"127.0.0.1:{self.port}"
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(value, response.getheader(name), name)
        csp = response.getheader("content-security-policy")
        directives = dict(
            (part.split(" ", 1) + [""])[:2] for part in (item.strip() for item in csp.split(";"))
        )
        self.assertEqual("'self'", directives["default-src"])
        self.assertEqual("'self'", directives["script-src"])  # no 'unsafe-inline'
        self.assertEqual("'self'", directives["style-src"])
        self.assertEqual(f"'self' ws://{host}", directives["connect-src"])
        self.assertEqual("'none'", directives["frame-ancestors"])
        self.assertEqual("'none'", directives["object-src"])
        self.assertNotIn("unsafe", csp)

    async def test_page_and_assets_are_served_with_security_headers(self):
        for path, media in (
            ("/ui/", "text/html"),
            ("/ui/index.html", "text/html"),
            ("/ui/app.js", "text/javascript"),
            ("/ui/app.css", "text/css"),
        ):
            with self.subTest(path):
                status, body, response = await self.http("GET", path)
                self.assertEqual(200, status)
                self.assertTrue(response.getheader("content-type").startswith(media))
                name = "index.html" if path == "/ui/" else path.removeprefix("/ui/")
                self.assertEqual((WEB_DIR / name).read_text(encoding="utf-8"), body)
                self.assert_security_headers(response)
        # The CSP names the origin the browser used.
        status, _, response = await self.http(
            "GET", "/ui/", headers={"Host": f"localhost:{self.port}"}
        )
        self.assertEqual(200, status)
        self.assert_security_headers(response, f"localhost:{self.port}")
        # API responses and refusals carry the headers too.
        for path, headers, expected in (
            ("/api/targets", {}, 200),
            ("/api/targets", {"Origin": "http://attacker.example"}, 403),
            ("/ui/nope.js", {}, 404),
        ):
            with self.subTest(path=path, headers=headers):
                status, _, response = await self.http("GET", path, headers=headers)
                self.assertEqual(expected, status)
                self.assert_security_headers(response)

    async def test_browsers_are_sent_to_the_gui_and_root_stays_json(self):
        status, _, response = await self.http("GET", "/", headers={"Accept": "text/html"})
        self.assertEqual(303, status)
        self.assertEqual("/ui/", response.getheader("location"))
        status, _, response = await self.http("GET", "/ui")
        self.assertEqual(308, status)
        self.assertEqual("/ui/", response.getheader("location"))
        status, payload, _ = await self.http("GET", "/")
        for endpoint in ("GET /ui/", "GET /api/refs", "GET /api/plans"):
            self.assertIn(endpoint, payload["endpoints"])

    async def test_static_paths_cannot_traverse(self):
        for path in (
            "/ui/../ubertooth_harness/gui.py",
            "/ui/..%2Fgui.py",
            "/ui/%2e%2e/gui.py",
            "/ui/%2e%2e%2fcontroller.py",
            "/ui/gui.py",
            "/ui/web/app.js",
            "/ui/app.js/",
            "/ui/.%2Fapp.js",
            "/ui/app.js%00",
            "/ui//etc/passwd",
            "/ui/%2Fetc%2Fpasswd",
            "/ui/....//app.js",
            "/ui/APP.JS",
        ):
            with self.subTest(path):
                status, body, response = await self.http("GET", path)
                if status == 307:
                    # The framework's trailing-slash redirect, to an asset.
                    self.assertEqual(
                        f"http://127.0.0.1:{self.port}/ui/app.js",
                        response.getheader("location"),
                    )
                    continue
                self.assertEqual(404, status, body)
                self.assertNotIn("import ", str(body))
                self.assertNotIn("root:", str(body))

    async def test_guard_applies_to_the_new_routes(self):
        port = self.port
        refused = (
            {"Host": f"attacker.example:{port}"},
            {"Origin": "http://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        )
        for path in ("/ui/", "/ui/app.js", "/api/refs", "/api/plans", "/api/runs/x"):
            for headers in refused:
                with self.subTest(path=path, headers=headers):
                    status, payload, _ = await self.http("GET", path, headers=headers)
                    self.assertEqual(403, status, payload)
            status, _, _ = await self.http(
                "GET", path, headers={"Origin": f"http://127.0.0.1:{port}",
                                      "Sec-Fetch-Site": "same-origin"},
            )
            self.assertNotEqual(403, status, path)


class RefTests(ControllerTestCase):

    def test_ls_remote_output_is_parsed(self):
        other = "f" * 40
        text = "\n".join((
            f"{COMMIT}\tHEAD",
            f"{COMMIT}\trefs/heads/main",
            f"{COMMIT}\trefs/heads/feature/a",
            f"{other}\trefs/tags/v2",
            f"{COMMIT}\trefs/tags/v2^{{}}",
            f"{COMMIT}\trefs/tags/v1",
            f"{COMMIT}\trefs/pull/1/head",
            f"{COMMIT}\trefs/heads/-oops",
            "not-a-sha\trefs/heads/bad",
            "garbage",
        ))
        refs = parse_ls_remote(text)
        self.assertEqual(
            [("HEAD", "default"), ("feature/a", "branch"), ("main", "branch"),
             ("v2", "tag"), ("v1", "tag")],
            [(ref.name, ref.kind) for ref in refs],
        )
        self.assertEqual(COMMIT, refs[3].commit)  # annotated tag: peeled commit

    async def test_refs_endpoint_lists_and_caches(self):
        calls = []
        self.controller.ref_source = fake_ref_source(calls=calls)
        status, payload, _ = await self.http("GET", "/api/refs")
        self.assertEqual(200, status, payload)
        self.assertEqual("https://example.invalid/ubertooth.git", payload["repository"])
        self.assertEqual(
            [{"name": "HEAD", "kind": "default", "commit": COMMIT},
             {"name": "main", "kind": "branch", "commit": COMMIT},
             {"name": "v1.2", "kind": "tag", "commit": COMMIT}],
            payload["refs"],
        )
        await self.http("GET", "/api/refs")
        self.assertEqual(1, len(calls))
        argv = calls[0]
        self.assertEqual(["git", "-c", "protocol.ext.allow=never", "ls-remote", "--",
                          "https://example.invalid/ubertooth.git"], argv[:6])

    async def test_cache_expires(self):
        calls, now = [], [0.0]

        async def git(argv, timeout_s):
            calls.append(argv)
            return 0, f"{COMMIT}\trefs/heads/main\n".encode(), b""

        source = GitRefSource("repo", ttl_s=60, git=git, clock=lambda: now[0])
        await source.refs()
        now[0] = 59
        await source.refs()
        self.assertEqual(1, len(calls))
        now[0] = 61
        await source.refs()
        self.assertEqual(2, len(calls))

    async def test_credentials_in_the_repository_url_are_not_shown(self):
        source = GitRefSource("https://user:secret@example.invalid/u.git",
                              git=fake_ref_source().git)
        data = await source.refs()
        self.assertNotIn("secret", data["repository"])
        self.assertNotIn("user", data["repository"])

    async def test_unlisted_refs_are_refused_before_dispatch(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            for ref in ("feature-x", "refs/heads/main", "main~1", COMMIT[:12], "MAIN"):
                with self.subTest(ref):
                    status, payload, _ = await self.http(
                        "POST", "/api/targets/Host_A/runs",
                        {"ref": ref, "plan": "fast", "suite": "fast"},
                    )
                    self.assertEqual(422, status, payload)
                    self.assertIn("not a branch or tag", payload["detail"])
            # Nothing reached the runner; a listed ref still goes through.
            message = await self.accepted_run(runner)
            self.assertEqual("main", message["ref"])

    async def test_full_commit_ids_are_accepted_without_listing(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            post, message = await self.start_run(runner, ref=COMMIT)
            self.assertEqual(COMMIT, message["ref"])
            await runner.send("run_accepted", request_id=message["request_id"],
                              run_id="run-1")
            status, payload, _ = await post
            self.assertEqual(201, status, payload)

    async def test_ref_listing_failure_does_not_block_runs(self):
        self.controller.ref_source = fake_ref_source(output=b"", code=128)
        status, payload, _ = await self.http("GET", "/api/refs")
        self.assertEqual(503, status)
        self.assertIn("repository not found", payload["detail"])
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            # Unlisted but well-formed: passed to the runner with a warning.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                message = await self.accepted_run(runner)
            self.assertEqual("main", message["ref"])
            self.assertIn("not checked", stderr.getvalue())
            await runner.send("run_finished", request_id=message["request_id"],
                              run_id="run-1", status="passed", exit_code=0,
                              run_directory="/x/run-1")
        self.controller.ref_source = None
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                await self.accepted_run(runner, run_id="run-2")
            self.assertIn("no Ubertooth repository", stderr.getvalue())

    async def test_real_git_ls_remote_of_a_local_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                subprocess.run(
                    ["git", "-C", directory, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                     *args], check=True, capture_output=True,
                )
            git("init", "-q", "-b", "main")
            git("commit", "-q", "--allow-empty", "-m", "first")
            git("branch", "dev")
            git("tag", "-a", "-m", "release", "v1.0")
            source = GitRefSource(directory, git=run_git)
            data = await source.refs()
            names = {(ref["name"], ref["kind"]) for ref in data["refs"]}
            self.assertEqual(
                {("HEAD", "default"), ("main", "branch"), ("dev", "branch"), ("v1.0", "tag")},
                names,
            )
            commits = {ref["commit"] for ref in data["refs"]}
            self.assertEqual(1, len(commits))  # the tag is peeled to the commit


class PlanTests(ControllerTestCase):

    async def test_plans_endpoint_lists_the_repository_plans(self):
        status, payload, _ = await self.http("GET", "/api/plans")
        self.assertEqual(200, status)
        plans = {plan["name"]: plan for plan in payload["plans"]}
        expected = sorted(path.stem for path in (REPOSITORY / "testplans").glob("*.yaml"))
        self.assertEqual(expected, sorted(plans))
        fast = plans["fast"]
        self.assertIsNone(fast["error"])
        (suite,) = fast["suites"]
        self.assertEqual("fast", suite["name"])
        plan = load_plan(REPOSITORY / "testplans" / "fast.yaml")
        self.assertEqual(
            [step.test_name for step in plan.steps("fast")],
            [test["name"] for test in suite["tests"]],
        )
        estimates = {test["name"]: test["estimated_s"] for test in suite["tests"]}
        self.assertEqual(10, estimates["BLE monitor"])
        self.assertIsNone(estimates["Ubertooth identity"])
        # Not every test has an estimate: no suite total.
        self.assertIsNone(suite["estimated_s"])
        self.assertEqual(list(estimates.values()).count(None), suite["unestimated_tests"])

    def test_estimates_sum_and_broken_plans_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "timed.yaml").write_text(
                "plan: Timed\nsuites:\n  quick:\n"
                "    - BLE monitor:\n        duration: 10s\n"
                "    - Spectrum analyzer:\n        coverage: smoke\n        duration: 2m\n",
                encoding="utf-8",
            )
            (root / "broken.yaml").write_text("plan: Broken\nsuites: []\n", encoding="utf-8")
            (root / "bad name.yaml").write_text("plan: x\n", encoding="utf-8")
            plans = {plan["name"]: plan for plan in list_plans(root)}
        self.assertEqual({"timed", "broken"}, set(plans))
        self.assertEqual(130, plans["timed"]["suites"][0]["estimated_s"])
        self.assertEqual(0, plans["timed"]["suites"][0]["unestimated_tests"])
        self.assertTrue(plans["broken"]["error"])
        self.assertEqual([], plans["broken"]["suites"])


class FailureSummaryTests(ControllerTestCase):

    async def test_run_detail_includes_the_failure_summary(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            records = [
                event("run-1", 1, "run_started"),
                event("run-1", 2, "test_started", name="Host version",
                      test_name="Ubertooth identity", invocation_id="001-id"),
                event("run-1", 3, "command_output", invocation_id="001-id",
                      stream="stdout", text="line one\n<script>x</script>\n"),
                event("run-1", 4, "test_finished", "failed", invocation_id="001-id",
                      test_name="Ubertooth identity", reason="expected pattern missing"),
                event("run-1", 5, "run_finished", "failed",
                      failure_reason="Ubertooth identity: expected pattern missing"),
            ]
            for record in records:
                await runner.send("event", run_id="run-1", event=record)
            await runner.send(
                "run_finished", request_id=message["request_id"], run_id="run-1",
                status="failed", exit_code=1, run_directory="/absent",
            )
            await wait_until(lambda: self.controller.runs["run-1"].status == "failed")
        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(200, status)
        self.assertEqual(
            [
                {"step": "Ubertooth identity", "status": "failed",
                 "reason": "expected pattern missing",
                 "log": ["line one", "<script>x</script>"]},
                {"step": "run", "status": "failed",
                 "reason": "Ubertooth identity: expected pattern missing", "log": []},
            ],
            payload["failures"],
        )

    def test_skipped_tests_are_not_failures_and_split_lines_are_joined(self):
        def record(kind, status=None, **details):
            return {"event_type": kind, "status": status, "details": details}

        events = [
            record("test_finished", "skipped", invocation_id="a", test_name="Skipped"),
            record("command_output", invocation_id="b", text="first par"),
            record("command_output", invocation_id="b", text="t\nsecond"),
            record("test_finished", "failed", invocation_id="b", test_name="Split",
                   reason="boom"),
        ]
        self.assertEqual(
            [{"step": "Split", "status": "failed", "reason": "boom",
              "log": ["first part", "second"]}],
            failure_summary({"status": "running"}, events),
        )

    def test_runs_without_a_finish_record(self):
        self.assertEqual(
            [{"step": "run", "status": "lost", "reason": "runner restarted", "log": []}],
            failure_summary({"status": "lost", "reason": "runner restarted"}, []),
        )
        self.assertEqual([], failure_summary({"status": "running"}, [{"bad": 1}]))
        build = failure_summary(
            {"status": "error"},
            [{"event_type": "build_finished", "status": "failed",
              "details": {"reason": "cmake failed"}}],
        )
        self.assertEqual("build", build[0]["step"])
        self.assertEqual("cmake failed", build[0]["reason"])


class PageWiringTests(unittest.TestCase):
    """A JS-free check that the page uses only its own assets and endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        cls.js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        cls.css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    def test_only_listed_assets_exist(self):
        self.assertEqual(set(ASSETS), {path.name for path in WEB_DIR.iterdir()})

    def test_page_loads_only_its_own_assets_and_has_no_inline_code(self):
        sources = re.findall(r'<script\b([^>]*)>', self.html)
        self.assertEqual(['src="/ui/app.js" defer'], [s.strip() for s in sources])
        self.assertIn('<link rel="stylesheet" href="/ui/app.css">', self.html)
        self.assertNotRegex(self.html, r"<style\b")
        self.assertNotRegex(self.html, r"\son[a-z]+\s*=")
        self.assertNotRegex(self.html, r"\sstyle\s*=")
        for text in (self.html, self.js, self.css):
            self.assertNotRegex(text, r"(?i)(https?:)?//[a-z0-9.-]+\.[a-z]{2,}/")
            self.assertNotIn("@import", text)

    def test_script_wires_to_the_controller_endpoints(self):
        for endpoint in (
            '"/api/refs"', '"/api/plans"', '"/api/runs"', "/api/targets/${",
            "/ws/live", "/report.md", "/cancel", "`/api/runs/${encodeURIComponent(runId)}`",
        ):
            self.assertIn(endpoint, self.js)
        self.assertIn('"Content-Type"] = "application/json"', self.js)

    def test_script_has_no_html_injection_sinks(self):
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                     "eval(", "new Function", "setTimeout(\"", "javascript:"):
            self.assertNotIn(sink, self.js)

    def test_every_element_the_script_uses_exists(self):
        ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        used = set(re.findall(r'\$\("([^"]+)"\)', self.js))
        self.assertTrue(used)
        self.assertEqual(set(), used - ids)

    def test_form_controls_are_labelled_and_the_checklist_is_live(self):
        labels = set(re.findall(r'<label for="([^"]+)"', self.html))
        controls = set(re.findall(r'<(?:select|input)\b[^>]*\bid="([^"]+)"', self.html))
        self.assertEqual(set(), controls - labels)
        self.assertRegex(self.html, r'id="checklist"[^>]*aria-live="polite"')
        self.assertIn('lang="en"', self.html)


if __name__ == "__main__":
    unittest.main()
