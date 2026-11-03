"""Web GUI served by the controller: assets, headers, refs, plans, failures.

No browser and no hardware: the controller listens on 127.0.0.1 with an
ephemeral port, git ls-remote is scripted (one test runs the real git against
a local repository), and the page is checked statically for the endpoints it
wires to and for HTML injection sinks.
"""

import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from ubertooth_harness.catalog import builtin_catalog
from ubertooth_harness.gui import (
    ASSETS,
    WEB_DIR,
    GitRefSource,
    failure_summary,
    list_plans,
    omitted_reason,
    parse_ls_remote,
    run_git,
    step_hosts,
    test_hardware,
)
from ubertooth_harness.gui import _build_needs as build_needs
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
OTHER = "89abcdef" * 5
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

    async def accept(self, runner, post, message, run_id="run-1"):
        await runner.send("run_accepted", request_id=message["request_id"], run_id=run_id)
        status, payload, _ = await post
        self.assertEqual(201, status, payload)

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

    async def test_the_listed_commit_is_sent_and_recorded(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            message = await self.accepted_run(runner)
            self.assertEqual(("main", COMMIT), (message["ref"], message["commit"]))
        record = json.loads((self.results / "run-1" / "run.json").read_text())
        self.assertEqual(("main", COMMIT), (record["ref"], record["commit"]))
        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(COMMIT, payload["run"]["commit"])

    async def test_a_ref_that_moved_since_it_was_listed_is_refused(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "main", "commit": OTHER, "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(409, status, payload)
            self.assertIn("moved", payload["detail"])
            self.assertIn(OTHER, payload["detail"])
            # The commit the caller saw, still current: sent unchanged.
            post, message = await self.start_run(runner, commit=COMMIT)
            self.assertEqual(COMMIT, message["commit"])
            await self.accept(runner, post, message)

    async def test_commit_must_be_a_full_lower_case_id(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            for commit in (COMMIT[:12], COMMIT.upper(), "main", ""):
                with self.subTest(commit):
                    status, payload, _ = await self.http(
                        "POST", "/api/targets/Host_A/runs",
                        {"ref": "main", "commit": commit, "plan": "fast",
                         "suite": "fast"},
                    )
                    self.assertEqual(422, status, payload)
            await self.accepted_run(runner)

    async def test_a_branch_and_tag_of_one_name_are_told_apart_by_commit(self):
        self.controller.ref_source = fake_ref_source(
            f"{COMMIT}\trefs/heads/v1\n{OTHER}\trefs/tags/v1\n".encode()
        )
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "v1", "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(422, status, payload)
            # The message says how to choose, in the GUI or the API.
            self.assertIn("both a branch and a tag", payload["detail"])
            self.assertIn("pick the branch or the tag entry", payload["detail"])
            self.assertIn("`commit`", payload["detail"])
            # The GUI lists both entries; each sends its own commit, which
            # is run, and the run keeps the name.
            for commit in (COMMIT, OTHER):
                with self.subTest(commit):
                    self.assertEqual(commit, await self.controller.resolve_ref("v1", commit))
            # A commit neither entry lists is a moved ref.
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": "v1", "commit": "c" * 40, "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(409, status, payload)
            self.assertIn("moved", payload["detail"])
            post, message = await self.start_run(runner, ref="v1", commit=OTHER)
            self.assertEqual(("v1", OTHER), (message["ref"], message["commit"]))
            await self.accept(runner, post, message)

    async def test_a_full_commit_ref_is_its_own_commit(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            status, payload, _ = await self.http(
                "POST", "/api/targets/Host_A/runs",
                {"ref": COMMIT, "commit": OTHER, "plan": "fast", "suite": "fast"},
            )
            self.assertEqual(422, status, payload)
            self.assertIn("does not match", payload["detail"])

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
            self.assertEqual(("main", None), (message["ref"], message["commit"]))
            self.assertIn("not checked", stderr.getvalue())
            await runner.send("run_finished", request_id=message["request_id"],
                              run_id="run-1", status="passed", exit_code=0,
                              run_directory="/x/run-1")
        self.controller.ref_source = None
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                message = await self.accepted_run(runner, run_id="run-2")
            self.assertIn("no Ubertooth repository", stderr.getvalue())
            await runner.send("run_finished", request_id=message["request_id"],
                              run_id="run-2", status="passed", exit_code=0,
                              run_directory="/x/run-2")
            await wait_until(lambda: self.controller.runs["run-2"].finished_at)
            # A commit the caller saw is still sent when nothing can be listed.
            with contextlib.redirect_stderr(io.StringIO()):
                post, message = await self.start_run(runner, commit=OTHER)
            self.assertEqual(OTHER, message["commit"])
            await self.accept(runner, post, message, "run-3")

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
        # Plans of planned tests are listed but marked not runnable.
        self.assertEqual(
            {"backlog", "mac-polling"},
            {name for name, plan in plans.items() if plan["runnable"] is not True},
        )
        self.assertIs(True, fast["runnable"])
        (suite,) = fast["suites"]
        self.assertEqual("fast", suite["name"])
        plan = load_plan(REPOSITORY / "testplans" / "fast.yaml")
        self.assertEqual(
            [step.test_name for step in plan.steps("fast")],
            [test["name"] for test in suite["tests"]],
        )
        estimates = {test["name"]: test["estimated_s"] for test in suite["tests"]}
        # Runnable plans name only implemented tests: catalog estimates.
        self.assertEqual(0.4, estimates["Ubertooth identity"])
        self.assertEqual(6, estimates["Spectrum RSSI sanity"])
        self.assertEqual(4.3, estimates["BLE extcap capture"])  # smoke, 3s
        # Every test has an estimate: the suite total is their sum.
        self.assertNotIn(None, estimates.values())
        self.assertAlmostEqual(sum(estimates.values()), suite["estimated_s"], places=6)
        self.assertEqual(0, suite["unestimated_tests"])

    async def test_the_platform_a_target_registered_with_is_listed_for_it(self):
        # The platform the steps were judged against comes from the target's
        # own registration, so only a registered target has one.
        async with FakeRunner(self.port) as runner:
            await self.registered(runner, host="Host_A")  # registers Linux
            status, payload, _ = await self.http("GET", "/api/plans?target=Host_A")
            self.assertEqual((200, "Host_A", "linux"),
                             (status, payload["target"], payload["platform"]))
            self.assertEqual(
                "linux",
                (await self.http("GET", "/api/targets/Host_A"))[1]["target"]["platform"],
            )
        for query in ("?target=Host_B", ""):
            status, payload, _ = await self.http("GET", f"/api/plans{query}")
            self.assertEqual((200, None), (status, payload["platform"]))

    async def test_a_target_is_only_shown_the_steps_it_would_run(self):
        # The release suite holds one step that names the bench host its
        # ESP32 Classic peers are on (#282). Another target compiles no
        # invocation for it, so it is not that target's time and not in its
        # total; what it would add where it does run is reported beside it
        # (#284).
        named, other = "Host_Linux", "Host_Mac"
        async def release(target=None):
            query = f"?target={target}" if target else ""
            status, payload, _ = await self.http("GET", f"/api/plans{query}")
            self.assertEqual(200, status)
            plans = {plan["name"]: plan for plan in payload["plans"]}
            (suite,) = plans["release"]["suites"]
            return suite

        suite = await release(other)
        self.assertAlmostEqual(654.5, suite["estimated_s"], delta=0.2)
        self.assertEqual(
            [{"name": "Recover AFH map",
              "reason": f"runs on {named}, not on {other}",
              "estimated_s": 520.0}],
            suite["omitted_tests"],
        )
        self.assertEqual(520.0, suite["omitted_estimated_s"])
        # Every step is still listed, each saying whether it runs there.
        self.assertEqual(
            ["Recover AFH map"],
            [test["name"] for test in suite["tests"] if not test["runs_on_target"]],
        )
        self.assertEqual(0, suite["unestimated_tests"])
        # The host the step names, and no target at all, count every step.
        for suite in (await release(named), await release()):
            self.assertAlmostEqual(1174.5, suite["estimated_s"], delta=0.2)
            self.assertEqual([], suite["omitted_tests"])
            self.assertEqual(0.0, suite["omitted_estimated_s"])
            self.assertEqual(
                [], [t["name"] for t in suite["tests"] if not t["runs_on_target"]])

    def test_a_step_for_another_host_ticks_no_build_option_here(self):
        # The build options and the images only --artifact can pass are the
        # suite's as the chosen target would run it: a step the target does
        # not run must not tick a build option or warn about an image.
        plan = (
            "plan: Mixed\nsuites:\n  mixed:\n"
            "    - Host CLI surface:\n        hosts: each\n"
            "    - Flash reliability:\n"
            "        hosts: [Host_Linux]\n"
            "        platforms: [linux]\n"
            "        Ubertooth: default\n"
            "        flashes: 2\n"
            "        image sequence: [test build, KGV]\n"
            "        KGV sha256: " + "a" * 64 + "\n"
            "        KGV revision: 2020-12-R1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mixed.yaml").write_text(plan, encoding="utf-8")
            elsewhere = list_plans(root, target="Host_Mac")[0]["suites"][0]
            named = list_plans(root, target="Host_Linux")[0]["suites"][0]
            every = list_plans(root)[0]["suites"][0]
        self.assertEqual(
            (False, False, []),
            (elsewhere["needs_firmware_build"], elsewhere["requires_firmware_build"],
             elsewhere["needs_artifacts"]),
        )
        self.assertEqual(0, elsewhere["per_board_tests"])
        for suite in (named, every):
            self.assertEqual(
                (True, True, ["KGV"]),
                (suite["needs_firmware_build"], suite["requires_firmware_build"],
                 suite["needs_artifacts"]),
            )

    def test_a_suite_no_step_of_which_runs_on_the_target(self):
        # Nothing to run is no estimate rather than a total of zero: the
        # target would refuse such a run.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bench.yaml").write_text(
                "plan: Bench\nsuites:\n  bench:\n"
                "    - Host CLI surface:\n        hosts: [Host_Linux]\n"
                "    - Refused command lines:\n        hosts: [Host_Linux]\n"
                "        coverage: smoke\n",
                encoding="utf-8",
            )
            (suite,) = list_plans(root, target="Host_Mac")[0]["suites"]
        self.assertIsNone(suite["estimated_s"])
        self.assertEqual(0, suite["unestimated_tests"])
        self.assertEqual(2, len(suite["omitted_tests"]))
        self.assertGreater(suite["omitted_estimated_s"], 0)

    def test_an_omitted_step_with_no_estimate_leaves_their_total_unknown(self):
        # The omitted total follows the same rule as the suite's own: it is
        # the sum only when every one of them has an estimate. The steps
        # are still named, each with its own (null here).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bench.yaml").write_text(
                "plan: Bench\nsuites:\n  bench:\n"
                "    - Host CLI surface:\n        hosts: [Host_Linux]\n"
                "    - Receive-only firmware:\n        hosts: [Host_Linux]\n"
                "        Ubertooth: default\n",
                encoding="utf-8",
            )
            (suite,) = list_plans(root, target="Host_Mac")[0]["suites"]
        self.assertIsNone(suite["omitted_estimated_s"])
        self.assertEqual(
            [("Host CLI surface", 3.6), ("Receive-only firmware", None)],
            [(test["name"], test["estimated_s"]) for test in suite["omitted_tests"]],
        )

    async def test_a_suite_whose_required_step_runs_elsewhere_is_not_offered(self):
        # The release suite's required step names the bench host its ESP32
        # Classic peers are on, so a run on another target could not measure
        # it: run setup is told so, in the sentence the controller refuses
        # the run with (#285).
        status, payload, _ = await self.http("GET", "/api/plans?target=Host_Mac")
        self.assertEqual(200, status)
        plans = {plan["name"]: plan for plan in payload["plans"]}
        (suite,) = plans["release"]["suites"]
        self.assertIs(False, suite["runnable_on_target"])
        self.assertEqual(
            [{"name": "Recover AFH map", "hosts": ["Host_Linux"],
              "hardware": "Ubertooth, ESP32 Classic peer (acceptor), "
                          "ESP32 Classic peer (initiator)",
              "reason": "runs on Host_Linux, not on Host_Mac"}],
            suite["blocked_tests"],
        )
        reason = suite["not_runnable_reason"]
        for part in ("Recover AFH map is required on Host_Linux",
                     "ESP32 Classic peer (initiator)", "run group"):
            self.assertIn(part, reason)
        # A suite with no test for the target says that instead.
        (classic,) = [s for s in plans["classic"]["suites"] if s["name"] == "classic"]
        self.assertIs(False, classic["runnable_on_target"])
        self.assertEqual([], classic["blocked_tests"])
        self.assertIn("no test of this suite runs on Host_Mac",
                      classic["not_runnable_reason"])
        # The host it names, and no target at all, are offered the suite.
        for query in ("?target=Host_Linux", ""):
            status, payload, _ = await self.http("GET", f"/api/plans{query}")
            (suite,) = [
                plan for plan in payload["plans"] if plan["name"] == "release"
            ][0]["suites"]
            self.assertEqual((True, [], ""), (
                suite["runnable_on_target"], suite["blocked_tests"],
                suite["not_runnable_reason"]))

    def test_only_a_required_steps_own_host_naming_blocks_a_run(self):
        # `required hardware:` is about absence of the hardware the step
        # measures (#282): a platform a host cannot meet is not applicable,
        # which that argument deliberately does not change, and a step
        # without the argument is nobody's gate.
        entries = {
            "another host, required": (
                "    - Recover AFH map:\n        hosts: [Host_Linux]\n"
                "        required hardware: true\n"
                "        acceptor: default\n        initiator: default\n"
                "        coverage: smoke\n        duration: 2m\n"
            ),
            "another host, not required": (
                "    - Recover AFH map:\n        hosts: [Host_Linux]\n"
                "        acceptor: default\n        initiator: default\n"
                "        coverage: smoke\n        duration: 2m\n"
            ),
            "this platform cannot, required": (
                "    - Exclusive Ubertooth topology:\n        hosts: each\n"
                "        platforms: [linux]\n"
                "        required hardware: true\n"
                "        exclusive Ubertooth: true\n"
            ),
        }
        for label, entry in entries.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "gate.yaml").write_text(
                    "plan: Gate\nsuites:\n  gate:\n"
                    "    - Host CLI surface:\n        hosts: each\n" + entry,
                    encoding="utf-8",
                )
                (suite,) = list_plans(
                    root, target="Host_Mac", platform="macos"
                )[0]["suites"]
            # Every one of them is left out of this target's estimate.
            self.assertEqual(1, len(suite["omitted_tests"]), label)
            blocked = label == "another host, required"
            self.assertEqual(not blocked, suite["runnable_on_target"], label)
            self.assertEqual(1 if blocked else 0, len(suite["blocked_tests"]), label)

    def test_a_required_step_a_host_of_the_run_does_run_is_not_blocked(self):
        # Two steps of one test, one of them for this target: that run
        # measures the gate, so nothing is blocked (the planner keeps one
        # record per test and none for a test the host runs).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "gate.yaml").write_text(
                "plan: Gate\nsuites:\n  gate:\n"
                "    - Recover AFH map:\n        hosts: [Host_Linux]\n"
                "        required hardware: true\n"
                "        acceptor: default\n        initiator: default\n"
                "        coverage: smoke\n        duration: 2m\n"
                "    - Recover AFH map:\n        hosts: [Host_Mac]\n"
                "        required hardware: true\n"
                "        acceptor: default\n        initiator: default\n"
                "        coverage: smoke\n        duration: 2m\n",
                encoding="utf-8",
            )
            (suite,) = list_plans(root, target="Host_Mac")[0]["suites"]
        self.assertEqual((True, []), (suite["runnable_on_target"],
                                      suite["blocked_tests"]))

    def test_the_hardware_a_test_binds_is_named_role_by_role(self):
        catalog = builtin_catalog()
        self.assertEqual(
            "Ubertooth, ESP32 Classic peer (acceptor), "
            "ESP32 Classic peer (initiator)",
            test_hardware(catalog, "Recover AFH map"),
        )
        self.assertEqual("Ubertooth", test_hardware(catalog, "Ubertooth identity"))
        # A test that binds no hardware, and one the catalog does not know.
        self.assertEqual("", test_hardware(catalog, "Host CLI surface"))
        self.assertEqual("", test_hardware(catalog, "No such test"))

    def test_which_hosts_a_step_names(self):
        # `hosts: each` names none, so every target runs it; the planner
        # reads a singular `host` first, so this does too.
        self.assertEqual((), step_hosts({"hosts": "each"}))
        self.assertEqual((), step_hosts({}))
        self.assertEqual(("Host_Linux",), step_hosts({"hosts": ("Host_Linux",)}))
        self.assertEqual(("A", "B"), step_hosts({"hosts": ["A", "B"]}))
        self.assertEqual(("A",), step_hosts({"host": "A", "hosts": ("B",)}))
        catalog = builtin_catalog()

        def reason(arguments, target, platform=None, name="Host CLI surface"):
            return omitted_reason(catalog, name, arguments, target, platform)

        # No target: nothing is left out, whatever the step names.
        self.assertEqual("", reason({"hosts": ("Host_Linux",)}, None))
        self.assertEqual("", reason({"hosts": ("A", "B")}, "B"))
        self.assertEqual("runs on A, B, not on C", reason({"hosts": ("A", "B")}, "C"))
        # A test the catalog does not know is nobody's platform question.
        self.assertEqual("", reason({}, "C", "macos", name="No such test"))

    def test_a_step_the_targets_platform_cannot_run_is_left_out(self):
        # The planner leaves a host out of a step its platform does not run
        # (hostneeds), whichever hosts the step names, so the listing does
        # too once the target has registered the platform it is on.
        catalog = builtin_catalog()
        linux_only = catalog.get("Exclusive Ubertooth topology").normalize_arguments(
            {"platforms": ["linux"], "exclusive Ubertooth": True}
        )
        self.assertEqual(
            "runs only on Linux, and Host_Mac is macOS",
            omitted_reason(catalog, "Exclusive Ubertooth topology", linux_only,
                           "Host_Mac", "macos"),
        )
        for target, platform in (("Host_Linux", "linux"), ("Host_Mac", None)):
            self.assertEqual("", omitted_reason(
                catalog, "Exclusive Ubertooth topology", linux_only,
                target, platform))
        # And in a suite: the step is not that target's time, and its
        # `Ubertooth: each` no longer counts as a per-board test there.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "both.yaml").write_text(
                "plan: Both\nsuites:\n  both:\n"
                "    - Exclusive Ubertooth topology:\n        hosts: each\n"
                "        platforms: [linux]\n"
                "        exclusive Ubertooth: true\n        Ubertooth: each\n"
                "    - Host CLI surface:\n        hosts: each\n",
                encoding="utf-8",
            )
            mac = list_plans(root, target="Host_Mac", platform="macos")[0]["suites"][0]
            linux = list_plans(root, target="Host_Linux", platform="linux")[0]["suites"][0]
            unknown = list_plans(root, target="Host_Mac")[0]["suites"][0]
        self.assertEqual((3.6, 0), (mac["estimated_s"], mac["per_board_tests"]))
        self.assertEqual(
            [{"name": "Exclusive Ubertooth topology",
              "reason": "runs only on Linux, and Host_Mac is macOS",
              "estimated_s": 15.0}],
            mac["omitted_tests"],
        )
        for suite in (linux, unknown):
            self.assertEqual((18.6, 1), (suite["estimated_s"], suite["per_board_tests"]))
            self.assertEqual([], suite["omitted_tests"])

    async def test_plans_say_which_build_options_a_suite_needs(self):
        # So the GUI can ask for the image or the Python tools the chosen
        # suite's tests need, instead of the run being refused or those
        # tests left out (#257).
        status, payload, _ = await self.http("GET", "/api/plans")
        self.assertEqual(200, status)
        plans = {plan["name"]: plan for plan in payload["plans"]}
        suites = {
            (name, suite["name"]): suite
            for name, plan in plans.items() for suite in plan["suites"]
        }
        # slow holds DFU images and Flash reliability, which are refused
        # without the run's image, so the suite cannot run without it.
        slow = suites[("slow", "slow")]
        self.assertEqual((True, True, False, False), (
            slow["needs_firmware_build"], slow["requires_firmware_build"],
            slow["needs_python_tools"], slow["requires_python_tools"],
        ))
        self.assertEqual(
            {"DFU images", "Flash reliability"},
            {test["name"] for test in slow["tests"] if test["requires_firmware_build"]},
        )
        # host needs both, and is refused without neither: each option only
        # decides whether some cases run.
        host = suites[("host", "host")]
        self.assertEqual((True, False, True, False), (
            host["needs_firmware_build"], host["requires_firmware_build"],
            host["needs_python_tools"], host["requires_python_tools"],
        ))
        self.assertEqual(
            {"DFU image checks"},
            {test["name"] for test in host["tests"] if test["needs_firmware_build"]},
        )
        self.assertEqual(
            {"Python tools"},
            {test["name"] for test in host["tests"] if test["needs_python_tools"]},
        )
        # slow also names Flash reliability, which needs a published image no
        # build makes: the listing says which, so the GUI can say the run
        # would be refused (nothing can send an artifact over the protocol).
        self.assertEqual(["KGV"], slow["needs_artifacts"])
        self.assertEqual(
            {"Flash reliability"},
            {test["name"] for test in slow["tests"] if test["needs_artifacts"]},
        )
        self.assertEqual([], host["needs_artifacts"])
        # A suite of tests that need neither says so, test by test.
        ble = suites[("ble", "ble")]
        self.assertEqual((False, False),
                         (ble["needs_firmware_build"], ble["needs_python_tools"]))
        self.assertEqual(
            [],
            [test["name"] for test in ble["tests"]
             if test["needs_firmware_build"] or test["needs_python_tools"]],
        )
        # The firmware plan needs the image and not the Python tools: the two
        # options are reported apart.
        firmware = suites[("firmware", "firmware")]
        self.assertEqual((True, False), (firmware["needs_firmware_build"],
                                         firmware["needs_python_tools"]))

    def test_the_images_a_step_names_decide_what_its_suite_needs(self):
        # Flash reliability's plan entry lists the images it flashes, so a
        # suite needs the firmware build only where the entry names the run's
        # own image, and names every other image as one --artifact must pass.
        entry = (
            "    - Flash reliability:\n"
            "        hosts: [Host_Linux]\n"
            "        platforms: [linux]\n"
            "        Ubertooth: default\n"
            "        flashes: 2\n"
            "        image sequence: [{sequence}]\n"
            "        KGV sha256: " + "a" * 64 + "\n"
            "        KGV revision: 2020-12-R1\n"
        )
        cases = {
            "test build, KGV": (True, ["KGV"]),
            "2020-12-R1, KGV": (False, ["2020-12-R1", "KGV"]),
        }
        for sequence, (firmware, artifacts) in cases.items():
            with self.subTest(sequence=sequence), tempfile.TemporaryDirectory() as d:
                (Path(d) / "flash.yaml").write_text(
                    "plan: Flash\nsuites:\n  flash:\n"
                    + entry.format(sequence=sequence),
                    encoding="utf-8",
                )
                (suite,) = list_plans(Path(d))[0]["suites"]
                self.assertEqual(
                    (firmware, firmware, artifacts),
                    (suite["needs_firmware_build"], suite["requires_firmware_build"],
                     suite["needs_artifacts"]),
                )

    def test_a_suite_of_tests_that_need_nothing_built_says_so(self):
        fields = ("needs_firmware_build", "needs_python_tools",
                  "requires_firmware_build", "requires_python_tools")
        self.assertEqual([], build_needs(builtin_catalog(), "Host version")
                         ["needs_artifacts"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "elsewhere.yaml").write_text(
                "plan: Elsewhere\nsuites:\n  quick:\n    - Host version: {}\n",
                encoding="utf-8",
            )
            (suite,) = list_plans(root)[0]["suites"]
        self.assertEqual([False] * 4, [suite[field] for field in fields])
        self.assertEqual([False] * 4, [suite["tests"][0][field] for field in fields])
        # A test the catalog does not know needs nothing either, rather than
        # the listing failing over it.
        self.assertEqual(
            [False] * 4,
            [build_needs(builtin_catalog(), "No such test")[field] for field in fields],
        )

    def test_estimates_sum_and_broken_plans_are_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "timed.yaml").write_text(
                "plan: Timed\nsuites:\n  quick:\n"
                "    - BLE monitor:\n        duration: 10s\n"
                "    - Spectrum analyzer:\n        coverage: smoke\n        duration: 2m\n"
                "  partial:\n"
                "    - Host CLI surface: {}\n"
                "    - Receive-only firmware:\n        Ubertooth: default\n",
                encoding="utf-8",
            )
            (root / "broken.yaml").write_text("plan: Broken\nsuites: []\n", encoding="utf-8")
            (root / "bad name.yaml").write_text("plan: x\n", encoding="utf-8")
            plans = {plan["name"]: plan for plan in list_plans(root)}
        self.assertEqual({"timed", "broken"}, set(plans))
        # BLE monitor 10 s; smoke Spectrum analyzer 0.6 s plus one 2m sweep.
        self.assertEqual(130.6, plans["timed"]["suites"][0]["estimated_s"])
        self.assertEqual(0, plans["timed"]["suites"][0]["unestimated_tests"])
        # Not implemented and no duration argument: no estimate.
        partial = plans["timed"]["suites"][1]
        self.assertEqual([3.6, None], [t["estimated_s"] for t in partial["tests"]])
        self.assertIsNone(partial["estimated_s"])
        self.assertEqual(1, partial["unestimated_tests"])
        self.assertTrue(plans["broken"]["error"])
        self.assertEqual([], plans["broken"]["suites"])
        self.assertIs(False, plans["broken"]["runnable"])
        self.assertIs(True, plans["timed"]["runnable"])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "planned.yaml").write_text(
                "plan: Planned\nrunnable: false\nsuites:\n  later:\n"
                "    - BLE monitor:\n        duration: 10s\n",
                encoding="utf-8",
            )
            (planned,) = list_plans(Path(directory))
        self.assertEqual((None, False), (planned["error"], planned["runnable"]))


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
        # No manifest record: the live view lists the plan's tests.
        self.assertIsNone(payload["invocations"])

    async def test_run_detail_lists_the_planned_invocations(self):
        async with FakeRunner(self.port) as runner:
            await self.registered(runner)
            await self.accepted_run(runner)
            manifest = {"invocations": [
                {"id": f"00{n}-Exclusive Ubertooth topology",
                 "test_name": "Exclusive Ubertooth topology",
                 "arguments": {"platforms": ["linux"], "exclusive Ubertooth": True,
                               "capture duration": "5s"}}
                for n in (1, 2)
            ]}
            await runner.send("event", run_id="run-1", event=event(
                "run-1", 1, "manifest", "passed", manifest=manifest))
            await wait_until(lambda: self.controller.read_events(self.controller.runs["run-1"]))
        status, payload, _ = await self.http("GET", "/api/runs/run-1")
        self.assertEqual(200, status)
        # One per board the planner selected, each with its estimate. No run
        # has finished on this target, so the catalog estimate stands.
        self.assertEqual(
            [{"invocation_id": f"00{n}-Exclusive Ubertooth topology",
              "test_name": "Exclusive Ubertooth topology", "estimated_s": 15.0,
              "catalog_estimated_s": 15.0, "estimate_source": "catalog",
              "estimate_samples": 0, "estimate_scaled": False}
             for n in (1, 2)],
            payload["invocations"],
        )
        self.assertEqual([], payload["stages"])

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

    def test_the_live_view_closes_a_build_step_with_its_outcome(self):
        # The step's own status and reason, as report.md's transcript shows
        # them (`--- <step> EXIT: <status>: <reason> ---`).
        self.assertIn('kind === "build_step_finished"', self.js)
        self.assertRegex(self.js, r'pushLog\(`--- \$\{details\.step_name.*EXIT: \$\{status\}')

    def test_script_sends_the_commit_it_listed(self):
        self.assertIn("commit: chosen ? chosen.commit : null", self.js)

    def test_branch_and_tag_of_one_name_are_separate_entries(self):
        # Options are keyed by kind and name and labelled branch or tag.
        self.assertIn("const key = (ref) => `${ref.kind}:${ref.name}`;", self.js)
        self.assertIn('kind === "tag" ? "tag" : "branch"', self.js)
        self.assertIn("ref: chosen ? chosen.name : \"\"", self.js)

    def test_a_refused_start_reloads_the_ref_list(self):
        self.assertIn("error.status = response.status;", self.js)
        # A ref that moved (409) reloads the list; a runner's own refusal
        # (also 409, for example a Build it cannot do) is shown as it is.
        self.assertRegex(
            self.js,
            r'if \(error\.status === 409 && !error\.message\.startsWith\("runner rejected the run"\)\) \{[^}]*await loadRefs\(\);',
        )

    def test_script_wires_to_the_controller_endpoints(self):
        for endpoint in (
            '"/api/refs"', "`/api/plans${query}`", '"/api/runs"', "/api/targets/${",
            "/ws/live", "/report.md", "/cancel", "`/api/runs/${encodeURIComponent(runId)}`",
        ):
            self.assertIn(endpoint, self.js)
        self.assertIn('"Content-Type"] = "application/json"', self.js)

    def test_only_runnable_plans_are_offered_to_start(self):
        # The plan <select> is filled from plans that load and are runnable;
        # the others are only named in the status line.
        body = self.js[self.js.index("async function loadPlans()"):
                       self.js.index("function findSuite(")]
        self.assertIn(
            "const usable = state.plans.filter((plan) => !plan.error && plan.runnable === true);",
            body,
        )
        self.assertIn("select.replaceChildren(...usable.map(", body)
        self.assertEqual(1, body.count("select.replaceChildren("))
        self.assertIn("Not runnable (planned tests)", body)

    def test_the_plan_list_is_loaded_for_the_chosen_target(self):
        # Estimates and omitted steps belong to one target, so the list is
        # fetched for the chosen one and again whenever it changes, and a
        # slower answer for a target already left cannot replace it.
        body = self.js[self.js.index("async function loadPlans()"):
                       self.js.index("function findSuite(")]
        self.assertIn("const target = $(\"target\").value;", body)
        self.assertIn("?target=${encodeURIComponent(target)}", body)
        self.assertIn("const token = ++state.plansToken;", body)
        self.assertIn("if (token !== state.plansToken) return;", body)
        # A failed load's message does not survive the next good one.
        self.assertIn('$("plan-status").textContent = "";', body)
        # The operator's own target change reloads it, beside the plan and
        # suite selects; a target update while a load is in flight does not
        # start another.
        start = self.js[self.js.index("function start()"):]
        self.assertIn('$("target").addEventListener("change", loadPlans);', start)
        self.assertIn("state.plansWanted = target;", body)
        select = self.js[self.js.index("function renderTargetSelect()"):
                         self.js.index("// ----- run setup")]
        self.assertIn("if (select.value !== state.plansWanted) loadPlans();", select)
        # The operator's plan survives a reload for another target, and so
        # does the suite they chose; choosing a plan takes the suite named
        # after that plan, as before.
        self.assertIn('const keep = [current, "fast"].find(', body)
        self.assertIn("renderSuites(true);", body)
        suites = self.js[self.js.index("function renderSuites("):
                         self.js.index("function renderSuiteTests()")]
        self.assertIn("keepChoice && suites.find((suite) => suite.name === current)",
                      suites)
        self.assertIn('$("plan").addEventListener("change", () => renderSuites());',
                      start)

    def test_the_estimate_says_what_the_target_does_not_run(self):
        body = self.js[self.js.index("function estimateText(suite)"):
                       self.js.index("async function submitRun(")]
        # The target the figure is for, and the steps it leaves out with the
        # time they take where they run.
        self.assertIn("const target = state.plansTarget;", body)
        self.assertIn("Estimated test time${on}", body)
        omitted = self.js[self.js.index("function omittedText(suite)"):
                          self.js.index("async function submitRun(")]
        self.assertIn("suite.omitted_tests", omitted)
        self.assertIn("are not run there", omitted)
        self.assertIn("not counted above", omitted)
        # One sentence per reason, and a step without an estimate does not
        # hide the time of the rest.
        self.assertIn("const groups = new Map();", omitted)
        self.assertIn('typeof test.estimated_s === "number"', omitted)
        self.assertIn("with no estimate", omitted)
        self.assertIn("No test of this suite runs${on}", body)
        # The checklist a run starts with leaves out what its target does
        # not run, but only while the list belongs to that target.
        checklist = self.js[self.js.index("function rebuildChecklist()"):]
        self.assertIn("run.target === state.plansTarget", checklist)
        self.assertIn("test.runs_on_target !== false", checklist)

    def test_the_build_options_are_offered_explained_and_sent(self):
        # Both options are on the form, each with the note the operator reads
        # about what it costs, and each is sent only with Build (#257).
        for option, name in (("with-firmware", "with_firmware"),
                             ("with-python", "with_python")):
            self.assertRegex(
                self.html,
                rf'<input type="checkbox" id="{option}" name="{name}"'
                rf' aria-describedby="{option}-note">',
            )
            note = re.search(rf'<p id="{option}-note" class="hint">([^<]+)</p>', self.html)
            self.assertIsNotNone(note, option)
            self.assertIn("build time", note.group(1))
            self.assertIn(
                f'{name}: $("build").checked && $("{option}").checked,', self.js
            )
        # Neither is ticked by default: a suite that needs one ticks it.
        for option in ("with-firmware", "with-python"):
            self.assertNotRegex(self.html, rf'id="{option}"[^>]*\bchecked\b')
        for field in ("needs_firmware_build", "requires_firmware_build",
                      "needs_python_tools", "requires_python_tools"):
            self.assertIn(f'"{field}"', self.js)
        # A step the chosen target does not run must not tick, hold, or send
        # a build option: what the suite needs is counted the way
        # /api/plans counts its own flags.
        needed = self.js[self.js.index("function neededBy(suite, need)"):
                         self.js.index("function syncBuildOptions()")]
        self.assertIn("test.runs_on_target !== false", needed)

    def test_a_suite_that_needs_a_build_option_holds_it_ticked(self):
        body = self.js[self.js.index("function syncBuildOptions()"):
                       self.js.index("function start()")]
        # Ticked and held for a suite that cannot run without the option;
        # otherwise the box keeps what the operator set, so nothing is
        # built, or made to fail on a target without the toolchain, for a
        # suite that would only leave cases out.
        # The operator's own tick is remembered apart from the box, so a tick
        # one suite forces does not follow them to the next suite.
        self.assertIn("box.checked = required.length > 0 || option.chosen;", body)
        self.assertIn("option.chosen = $(option.id).checked;", self.js)
        self.assertIn("box.disabled = !build || required.length > 0;", body)
        self.assertIn("cannot run without ", body)
        # That suite's note names the cases and invites the tick instead, and
        # says when flashing builds the image anyway. Every note keeps the
        # page's own cost sentence and says when Build is what is missing.
        self.assertIn("needed.length && !box.checked", body)
        self.assertIn("Tick it to run them.", body)
        self.assertIn("option.builtByFlash && flashing", body)
        self.assertIn("const sentences = [option.cost];", body)
        self.assertIn('needed.join(", ")', body)
        self.assertIn("Needs Build.", body)
        # Every path that can change what is needed or what is ticked syncs
        # the options: the plan and suite selects (through renderSuiteTests),
        # Build, Flash, and the boxes themselves.
        for caller in ("function renderSuiteTests()", "function start()"):
            section = self.js[self.js.index(caller):]
            self.assertIn("syncBuildOptions", section[:section.index("\n  }")])
        start = self.js[self.js.index("function start()"):]
        for control in ('$("build")', '$("flash-build")'):
            self.assertIn(f'{control}.addEventListener("change", syncBuildOptions)',
                          start)
        # Each box records the operator's choice, then syncs.
        self.assertRegex(
            start,
            r'\$\(option\.id\)\.addEventListener\("change", \(\) => \{\s*'
            r'option\.chosen = \$\(option\.id\)\.checked;\s*syncBuildOptions\(\);',
        )
        self.assertNotIn("syncFlashBuild", self.js)

    def test_a_suite_the_target_cannot_measure_holds_run(self):
        # Run is held, with the reason the controller would refuse with, so
        # pressing it is not how the operator finds out (#285).
        body = self.js[self.js.index("function syncRunButton()"):
                       self.js.index("// A suite with a test that needs an image")]
        self.assertIn("suite.runnable_on_target === false", body)
        self.assertIn("suite.not_runnable_reason", body)
        self.assertIn('$("run-button").disabled = Boolean(reason);', body)
        self.assertIn("note.hidden = !reason;", body)
        # Called wherever the suite, the plan, or the target changes
        # (renderSuiteTests), and again after a start attempt, so a refusal
        # does not leave Run enabled for a suite that is still held.
        tests_body = self.js[self.js.index("function renderSuiteTests()"):
                             self.js.index("function syncRunButton()")]
        self.assertIn("syncRunButton();", tests_body)
        submit = self.js[self.js.index("async function submitRun("):
                         self.js.index("// ----- live run")]
        self.assertIn("syncRunButton();", submit)
        self.assertNotIn("button.disabled = false;", submit)
        # The reason is a status region the button points at.
        self.assertRegex(
            self.html, r'<p id="run-blocked" class="hint warn" role="status" hidden>')
        self.assertRegex(
            self.html, r'id="run-button"[^>]*aria-describedby="run-blocked"')

    def test_a_suite_needing_an_image_no_build_makes_says_so(self):
        # The build options cover every image a run can build; an image only
        # --artifact can pass is named instead of silently failing the run.
        body = self.js[self.js.index("function artifactWarning(suite)"):
                       self.js.index("function syncBuildOptions()")]
        self.assertIn("suite.needs_artifacts", body)
        self.assertIn("no build makes", body)
        self.assertIn('--artifact "${label}=PATH"', body)
        # Named for the steps the chosen target runs, as the suite's own
        # needs_artifacts counts them.
        self.assertIn("test.runs_on_target !== false", body)
        # What such a run costs before it is refused, so Run is not pressed
        # expecting a quick refusal.
        self.assertIn("builds first, and flashes whatever", body)
        self.assertIn("+ artifactWarning(suite)", self.js)

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
