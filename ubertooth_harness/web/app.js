// Ubertooth harness GUI. Vanilla JavaScript, no build step, no other origin.
//
// Every string from the controller or a runner reaches the page through
// textContent or a fixed-prefix attribute; nothing is parsed as HTML.
"use strict";

(function () {
  const LOG_LINES = 200;
  const LOG_LINE_CHARS = 500;
  // The two build options beyond Build itself: the run's firmware image and
  // the Python tools. `need` and `require` are the flags /api/plans sets on
  // a test that leaves cases out without the option, and on one that cannot
  // run without it at all. `cost` is the page's own note for the option,
  // read once at start and kept as the first sentence of the note; `chosen`
  // is the operator's own tick, which a suite's need never sets.
  const BUILD_OPTIONS = [
    {
      id: "with-firmware", what: "the run's firmware image",
      need: "needs_firmware_build", require: "requires_firmware_build",
      // run-suite builds this anyway when the run flashes a board.
      builtByFlash: true,
    },
    {
      id: "with-python", what: "the Python tools build",
      need: "needs_python_tools", require: "requires_python_tools",
    },
  ];
  const STATUS_TEXT = {
    pending: ["○", "pending"],
    running: ["…", "running"],
    passed: ["✓", "passed"],
    failed: ["✗", "failed"],
    canceled: ["■", "canceled"],
    skipped: ["–", "skipped"],
    "not applicable": ["–", "not applicable"],
    blocked: ["✗", "blocked"],
    error: ["✗", "error"],
    lost: ["?", "lost"],
    "not run": ["○", "not run"],
    "did not finish": ["✗", "did not finish"],
    online: ["●", "online"],
    offline: ["○", "offline"],
    busy: ["◆", "busy"],
  };

  const state = {
    targets: new Map(),
    runs: new Map(),
    plans: [],
    // The target the plan list on display was loaded for, the one a load is
    // under way for, and a token so a slower answer for a target already
    // left cannot replace a newer list.
    plansTarget: "",
    plansWanted: "",
    // The platform that list was judged by, so a target that registers
    // after it was fetched is noticed.
    plansPlatform: null,
    plansToken: 0,
    // ref option value ("<kind>:<name>") -> {name, kind, commit} as listed
    refOptions: new Map(),
    selected: null, // run_id shown in the live view
    // run_id -> [{path, stream, bytes, lines, sha256, owner, checks}] as the
    // run recorded them, and the page of the file open in the viewer.
    files: new Map(),
    openFile: null, // {run_id, path, page}
    wantedFile: null, // a file named in the URL fragment, opened once loaded
    live: null, // {run_id, seen:Set, events:[], items:[], byInvocation:Map, log:[], partial, stage}
    socket: null,
    retry: 1000,
  };

  const $ = (id) => document.getElementById(id);

  // Build an element; children are nodes or strings (as text).
  function el(tag, attributes, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "text") node.textContent = String(value);
      else if (name === "className") node.className = value;
      else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  function statusBadge(status) {
    const known = STATUS_TEXT[status];
    const [icon, label] = known || ["•", String(status || "unknown")];
    const key = known ? status.replace(/ /g, "-") : "unknown";
    return el("span", { className: `status status-${key}` },
      el("span", { "aria-hidden": "true", text: icon + " " }), label);
  }

  function timeElement(iso) {
    if (!iso) return el("span", { text: "–" });
    const date = new Date(iso);
    const shown = Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
    return el("time", { datetime: String(iso), text: shown });
  }

  function formatSeconds(total) {
    const seconds = Math.max(0, Math.round(total));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}h ${pad(m)}m ${pad(s)}s` : `${m}m ${pad(s)}s`;
  }

  async function api(method, path, body) {
    const options = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const detail = payload && typeof payload.detail === "string"
        ? payload.detail : `HTTP ${response.status}`;
      const error = new Error(detail);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  const runPath = (runId) => `/api/runs/${encodeURIComponent(runId)}`;

  // ----- targets -----------------------------------------------------------

  function renderTargets() {
    const list = $("targets");
    const targets = [...state.targets.values()].sort((a, b) => a.host.localeCompare(b.host));
    $("targets-empty").hidden = targets.length > 0;
    list.replaceChildren(...targets.map((target) => {
      const os = target.os || {};
      const osText = [os.system, os.macos_version || os.distribution, os.release, os.architecture]
        .filter(Boolean).join(" ");
      const serials = Array.isArray(target.ubertooth_serials)
        ? (target.ubertooth_serials.length ? target.ubertooth_serials.join(", ") : "none attached")
        : `unknown (${target.enumeration_error || "enumeration unavailable"})`;
      const active = target.active_run_id
        ? el("span", {}, el("code", { text: target.active_run_id }), " ",
          el("button", {
            type: "button", text: "Watch",
            "aria-label": `Watch run ${target.active_run_id}`,
            onclick: () => selectRun(target.active_run_id),
          }))
        : "none";
      return el("li", {},
        el("article", { className: "card target-card", "aria-labelledby": `target-${target.host}` },
          el("h3", { id: `target-${target.host}`, text: target.host }),
          el("dl", { className: "summary summary-compact" },
            el("dt", { text: "Status" }), el("dd", { className: "badges" }, statusBadge(target.status),
              target.active_run_id ? statusBadge("busy") : null),
            el("dt", { text: "Host name" }), el("dd", { text: target.hostname }),
            el("dt", { text: "OS" }), el("dd", { text: osText || "unknown" }),
            el("dt", { text: "Python" }), el("dd", { text: target.python }),
            el("dt", { text: "Ubertooth serials" }), el("dd", { text: serials }),
            el("dt", { text: "Last seen" }), el("dd", {}, timeElement(target.last_seen_at)),
            el("dt", { text: "Active run" }), el("dd", {}, active))));
    }));
    renderTargetSelect();
  }

  function renderTargetSelect() {
    const select = $("target");
    const current = select.value;
    const targets = [...state.targets.values()].sort((a, b) => a.host.localeCompare(b.host));
    if (!targets.length) {
      select.replaceChildren(el("option", { value: "", text: "(no targets registered)" }));
    } else {
      select.replaceChildren(...targets.map((target) => {
        const notes = [target.status];
        if (target.active_run_id) notes.push("busy");
        return el("option", { value: target.host, text: `${target.host} (${notes.join(", ")})` });
      }));
      if (targets.some((target) => target.host === current)) select.value = current;
    }
    // The first target to register, or a selection this update replaced,
    // changes which host the plans belong to; the estimates, the build
    // options and whether the suite can be measured there are that host's.
    // A target that registers while already chosen gains a platform, which
    // changes the same answers, so that is a reload too. Both are compared
    // with what a load is under way for, so an update while one is in
    // flight does not start another.
    if (select.value !== state.plansWanted
        || targetPlatform(select.value) !== state.plansPlatform) {
      loadPlans();
    }
  }

  // The registry platform a target registered with, or null for one that
  // has not registered (which is what /api/plans reports for it).
  function targetPlatform(host) {
    const target = host ? state.targets.get(host) : null;
    return target && target.platform ? target.platform : null;
  }

  // ----- run setup ---------------------------------------------------------

  // "main (0123456789ab)": the ref and the commit that was run.
  function refLabel(run) {
    if (!run.ref) return "–";
    if (!run.commit || run.commit === run.ref) return run.ref;
    return `${run.ref} (${String(run.commit).slice(0, 12)})`;
  }

  async function loadRefs() {
    const select = $("ref");
    const status = $("refs-status");
    status.textContent = "Loading refs…";
    try {
      const data = await api("GET", "/api/refs");
      const groups = { default: "Default branch", branch: "Branches", tag: "Tags" };
      const current = select.value;
      // One entry per listed ref, keyed by kind and name, so a branch and a
      // tag of the same name are separate choices; each sends its own
      // commit, which the controller accepts for that name.
      const key = (ref) => `${ref.kind}:${ref.name}`;
      state.refOptions = new Map(data.refs.map((ref) => [key(ref), ref]));
      select.replaceChildren(...Object.entries(groups).map(([kind, label]) => {
        const refs = data.refs.filter((ref) => ref.kind === kind);
        if (!refs.length) return null;
        return el("optgroup", { label },
          ...refs.map((ref) => el("option", {
            value: key(ref),
            text: `${ref.name} (${kind === "tag" ? "tag" : "branch"}, ${String(ref.commit).slice(0, 12)})`,
          })));
      }).filter(Boolean));
      const preferred = [current, "branch:main", "branch:master", "default:HEAD"]
        .find((value) => value && state.refOptions.has(value));
      if (preferred) select.value = preferred;
      status.textContent = `${data.refs.length} refs from ${data.repository}, listed ${data.fetched_at}.`;
    } catch (error) {
      state.refOptions = new Map();
      select.replaceChildren(el("option", { value: "", text: "(refs unavailable)" }));
      status.textContent = `Cannot list refs: ${error.message}`;
    }
  }

  // The plan list belongs to the chosen target: the estimates are what that
  // host's own runs measured, and a step that names other hosts is not run
  // there, so it is left out of the suite's estimate and of the build
  // options the suite asks for. Called at start and whenever the target
  // changes.
  async function loadPlans() {
    const target = $("target").value;
    const token = ++state.plansToken;
    state.plansWanted = target;
    // The platform the controller would judge the steps by: a target that
    // registers after this list was fetched gains one, which is a reload.
    const platform = targetPlatform(target);
    const query = target ? `?target=${encodeURIComponent(target)}` : "";
    try {
      const data = await api("GET", `/api/plans${query}`);
      if (token !== state.plansToken) return;  // a later target owns the list
      state.plans = data.plans;
      state.plansTarget = target;
      state.plansPlatform = platform;
      // A failure from an earlier load is not this list's status.
      $("plan-status").textContent = "";
    } catch (error) {
      if (token !== state.plansToken) return;
      state.plans = [];
      state.plansTarget = target;
      state.plansPlatform = platform;
      $("plan-status").textContent = `Cannot list plans: ${error.message}`;
    }
    const select = $("plan");
    // What the operator chose is kept across a reload for another target.
    const current = select.value;
    // Only plans that load and are runnable can be started; a plan of
    // planned tests (runnable: false, e.g. backlog) is listed, not offered.
    const usable = state.plans.filter((plan) => !plan.error && plan.runnable === true);
    select.replaceChildren(...usable.map((plan) =>
      el("option", { value: plan.name, text: `${plan.name} — ${plan.title}` })));
    const broken = state.plans.filter((plan) => plan.error);
    const planned = state.plans.filter((plan) => !plan.error && plan.runnable !== true);
    const notes = [];
    if (broken.length) {
      notes.push(`Not loadable: ${broken.map((plan) => `${plan.name} (${plan.error})`).join("; ")}`);
    }
    if (planned.length) {
      notes.push(`Not runnable (planned tests): ${planned.map((plan) => plan.name).join(", ")}`);
    }
    if (notes.length) $("plan-status").textContent = notes.join(". ");
    const keep = [current, "fast"].find(
      (name) => name && usable.some((plan) => plan.name === name));
    if (keep) select.value = keep;
    // A reload for another target keeps the chosen suite as well.
    renderSuites(true);
    if (state.live) rebuildChecklist();
  }

  function findSuite(planName, suiteName) {
    const plan = state.plans.find((item) => item.name === planName && !item.error);
    return plan ? plan.suites.find((suite) => suite.name === suiteName) || null : null;
  }

  // `keepChoice` is for a reload of the same plan (another target): the
  // chosen suite stays chosen. When the operator picks a plan, the suite
  // named after it wins, as it did before.
  function renderSuites(keepChoice = false) {
    const plan = state.plans.find((item) => item.name === $("plan").value);
    const select = $("suite");
    const current = select.value;
    const suites = plan ? plan.suites : [];
    select.replaceChildren(...suites.map((suite) =>
      el("option", { value: suite.name, text: `${suite.name} (${suite.tests.length} tests)` })));
    const keep = (keepChoice && suites.find((suite) => suite.name === current))
      || suites.find((suite) => suite.name === plan?.name);
    if (keep) select.value = keep.name;
    renderSuiteTests();
  }

  function renderSuiteTests() {
    const suite = findSuite($("plan").value, $("suite").value);
    $("suite-tests").textContent = suite
      ? `${suite.tests.map((test) => test.name).join(", ")}. ${estimateText(suite)}`
        + artifactWarning(suite)
      : "";
    // This suite may need build options the last one did not, and this
    // target may not be able to measure it at all.
    syncBuildOptions();
    syncRunButton();
  }

  // A run the controller would refuse is not offered: a suite no step of
  // which runs on the chosen target, and one holding a step whose own
  // hardware is what it measures (`required hardware:`) that names no host
  // of this run. `/api/plans` answers both for the chosen target, in the
  // same sentence the controller refuses with, so pressing Run cannot be
  // the way to find out. Everything else about the bench is the run's to
  // report: hardware it has not got blocks that step and fails the run,
  // and the other tests still run (#265, #269).
  function syncRunButton() {
    const suite = findSuite($("plan").value, $("suite").value);
    const reason = suite && suite.runnable_on_target === false
      ? String(suite.not_runnable_reason || "This target cannot run this suite.")
      : "";
    const note = $("run-blocked");
    // Shown before it is filled: a status region that changes while it is
    // hidden announces nothing, and a disabled button cannot be focused to
    // reach its description.
    note.hidden = !reason;
    note.textContent = reason;
    $("run-button").disabled = Boolean(reason);
  }

  // A suite with a test that needs an image no build makes (a published
  // release image) cannot be run from here at all: only the command line
  // passes one, with --artifact. Such a run is refused where the cases are
  // built, which is after it has built the commits and flashed the boards,
  // so the warning says that too. Said here rather than hidden, because the
  // build options below now cover every other image a run needs.
  function artifactWarning(suite) {
    const wanted = Array.isArray(suite.needs_artifacts) ? suite.needs_artifacts : [];
    if (!wanted.length) return "";
    // The steps the chosen target runs, as `suite.needs_artifacts` itself
    // counts them: a step it does not run needs this run no image.
    const tests = suite.tests
      .filter((test) => (test.needs_artifacts || []).length
        && test.runs_on_target !== false)
      .map((test) => test.name);
    const artifacts = wanted.map((label) => `--artifact "${label}=PATH"`).join(" ");
    return ` This suite also needs ${wanted.join(", ")},`
      + ` ${wanted.length > 1 ? "images" : "an image"} no build makes,`
      + ` for ${tests.join(", ")}: run it on the target with run-suite`
      + ` ${artifacts}. A run started here builds first, and flashes whatever`
      + " it would flash, before it is refused without running a test.";
  }

  // The time the chosen target is expected to spend on the chosen suite,
  // which counts only the steps that target runs, and then what it does not
  // run. `learned` says the figure came from that host's own finished runs
  // rather than the catalog's hand-calibrated estimates.
  function estimateText(suite) {
    const target = state.plansTarget;
    const on = target ? ` on ${target}` : "";
    const running = suite.tests.filter((test) => test.runs_on_target !== false);
    const parts = [];
    if (suite.estimated_s !== null && suite.estimated_s !== undefined) {
      const learned = running.filter((test) => test.estimate_source === "learned").length;
      const source = learned
        ? `${learned} of ${running.length} from this target's own finished runs, the rest from the catalog`
        : "from the catalog's per-test estimates";
      parts.push(`Estimated test time${on} ${formatSeconds(suite.estimated_s)}`
        + ` (${source}; build and preflight not included).`);
      if (suite.per_board_tests) {
        parts.push(`${suite.per_board_tests} test(s) run once per selected Ubertooth`
          + " and are counted once here; the live countdown counts every board the run selects.");
      }
    } else if (!running.length) {
      parts.push(`No test of this suite runs${on}, so there is nothing for it to run.`);
    } else {
      parts.push(`No time estimate${on}: ${suite.unestimated_tests} of the`
        + ` ${running.length} tests it would run have no estimated duration,`
        + " so the live view shows elapsed time only.");
    }
    const omittedNote = omittedText(suite);
    if (omittedNote) parts.push(omittedNote);
    return parts.join(" ");
  }

  // The steps the chosen target does not run, because the plan names other
  // hosts for them. Named with the time they take where they do run, so an
  // estimate smaller than the whole suite's is accounted for rather than
  // just smaller.
  function omittedText(suite) {
    const omitted = Array.isArray(suite.omitted_tests) ? suite.omitted_tests : [];
    if (!omitted.length) return "";
    // The known estimates among them, so one step without an estimate does
    // not hide the time of the rest.
    const timed = omitted.filter((test) => typeof test.estimated_s === "number");
    const total = timed.reduce((sum, test) => sum + test.estimated_s, 0);
    const untimed = omitted.length - timed.length;
    const missing = untimed ? `${untimed} with no estimate` : "";
    const counted = timed.length
      ? `${formatSeconds(total)} of test time where they do run`
        + (missing ? `, ${missing}` : "")
      : missing;
    const adds = counted ? ` (${counted})` : "";
    // Grouped by reason: a whole suite for another host is one sentence,
    // not the same reason after every test name.
    const groups = new Map();
    for (const test of omitted) {
      const names = groups.get(test.reason) || [];
      names.push(test.name);
      groups.set(test.reason, names);
    }
    const named = [...groups.entries()]
      .map(([reason, names]) => `${names.join(", ")} — ${reason}`).join("; ");
    return `${omitted.length} test(s) of this suite are not run there${adds}`
      + ` and are not counted above: ${named}.`;
  }

  async function submitRun(event) {
    event.preventDefault();
    const status = $("run-status");
    const host = $("target").value;
    const chosen = state.refOptions.get($("ref").value);
    const body = {
      ref: chosen ? chosen.name : "",
      // The commit this page listed: the controller refuses the run if the
      // ref has moved since, and the runner runs exactly this commit.
      commit: chosen ? chosen.commit : null,
      plan: $("plan").value,
      suite: $("suite").value,
      tests: [],
      build: $("build").checked,
      // Flashing and the two extra build options apply only to a run that
      // builds; the controller refuses either extra without Build.
      flash_build: $("build").checked && $("flash-build").checked,
      with_firmware: $("build").checked && $("with-firmware").checked,
      with_python: $("build").checked && $("with-python").checked,
      // Check and Update compare the board against a bootloader built from
      // the ref, which run-suite refuses without a build; the controller
      // refuses it too, so Build off means Record.
      // Update writes flash before preflight, so the controller refuses it
      // unless the boards are flashed with the run's own build first.
      bootloader: $("build").checked && $("flash-build").checked
        ? $("bootloader").value
        : $("build").checked && $("bootloader").value !== "update"
          ? $("bootloader").value
          : "record",
    };
    if (!host || !body.ref || !body.plan || !body.suite) {
      status.textContent = "Choose a ref, plan, suite and target first.";
      return;
    }
    const button = $("run-button");
    button.disabled = true;
    status.textContent = `Starting ${body.plan}/${body.suite} at ${refLabel(body)} on ${host}…`;
    try {
      const data = await api("POST", `/api/targets/${encodeURIComponent(host)}/runs`, body);
      upsertRun(data.run);
      status.textContent = `Started run ${data.run.run_id}.`;
      selectRun(data.run.run_id);
    } catch (error) {
      status.textContent = `Not started: ${error.message}`;
      if (error.status === 409 && !error.message.startsWith("runner rejected the run")) {
        // The ref may have moved since this page listed it: list the refs
        // again so the next Run sends the current commit.
        await loadRefs();
        status.textContent = `Not started: ${error.message}. The ref list has been reloaded; check the ref and press Run again.`;
      }
    } finally {
      // Not simply enabled again: the suite may be one this target cannot
      // measure, which holds the button.
      syncRunButton();
    }
  }

  // One bootloader identity record as a reader reads it. A board whose
  // region carries no version block has only the hash to go on, which is what
  // the harness puts in `detail`.
  function bootloaderText(identity) {
    if (!identity) return "not reported";
    const crc = String(identity.region_crc32 ?? "");
    if (identity.has_version_block) {
      return `${String(identity.version ?? "")} ${String(identity.build ?? "")}, CRC32:${crc}`;
    }
    return `${String(identity.detail ?? identity.generation ?? "")}, CRC32:${crc}`;
  }

  // ----- live run ----------------------------------------------------------

  function newLive(runId) {
    return {
      run_id: runId, seen: new Set(), events: [], items: [],
      byInvocation: new Map(), log: [], partial: "", stage: "waiting",
      invocations: null, boards: new Map(), queued: [], bootloaderExpected: "",
    };
  }

  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  async function selectRun(runId, scroll = true) {
    if (!RUN_ID.test(String(runId))) return;
    state.selected = runId;
    // The file open in the viewer belonged to the run being left.
    state.openFile = null;
    // A link to this view: /ui/#run=<run_id>.
    window.history.replaceState(null, "", `#run=${runId}`);
    const wanted = state.wantedFile;
    state.wantedFile = null;
    state.live = newLive(runId);
    $("live-empty").hidden = true;
    $("live").hidden = false;
    $("cancel-status").textContent = "";
    rebuildChecklist();
    renderLive();
    try {
      const data = await api("GET", runPath(runId));
      if (state.selected !== runId) return;
      upsertRun(data.run);
      state.live.invocations = Array.isArray(data.invocations) ? data.invocations : null;
      for (const record of data.events) addEvent(runId, record, false);
      // Now that the run's plan is known, lay out its checklist and replay.
      rebuildChecklist();
      await loadOutputFiles(runId);
      if (wanted) await openOutputFile(runId, wanted, 0);
    } catch (error) {
      $("cancel-status").textContent = `Cannot load run: ${error.message}`;
    }
    if (scroll) $("live-heading").scrollIntoView({ block: "start" });
  }

  function rebuildChecklist() {
    const live = state.live;
    if (!live) return;
    const run = state.runs.get(live.run_id);
    const suite = run ? findSuite(run.plan, run.suite) : null;
    let tests = suite ? suite.tests : [];
    // The plans were listed for the run-setup target, and a step that names
    // hosts is run only there, so the flag is read only for a run on that
    // same target. The run's manifest replaces this list as soon as it
    // arrives, and a test that starts anyway is added when it starts.
    if (run && run.target === state.plansTarget) {
      tests = tests.filter((test) => test.runs_on_target !== false);
    }
    if (run && Array.isArray(run.tests) && run.tests.length) {
      tests = tests.filter((test) => run.tests.includes(test.name));
    }
    if (Array.isArray(live.invocations)) {
      // The run's manifest: one item per invocation on the target, so a test
      // with Ubertooth: each counts every board it selected.
      live.items = live.invocations.map((invocation) => ({
        name: invocation.test_name, invocation_id: invocation.invocation_id,
        estimated_s: invocation.estimated_s, status: "pending", reason: "",
      }));
    } else {
      live.items = tests.map((test) => ({
        name: test.name, estimated_s: test.estimated_s, status: "pending", reason: "",
      }));
    }
    live.planned = Array.isArray(live.invocations) || Boolean(suite);
    live.byInvocation = new Map();
    live.boards = new Map();
    live.queued = [];
    live.bootloaderExpected = "";
    // Records from the socket and from REST may interleave; replay in order.
    const events = [...live.events].sort((a, b) => a.sequence - b.sequence);
    live.events = [];
    live.seen = new Set();
    live.log = [];
    live.partial = "";
    live.stage = "waiting";
    // The estimate covers tests only, so the countdown starts with the first test.
    live.testsStarted = null;
    for (const record of events) addEvent(live.run_id, record, false);
    renderLive();
  }

  async function loadInvocations(runId) {
    try {
      const data = await api("GET", runPath(runId));
      const live = state.live;
      if (!live || live.run_id !== runId || !Array.isArray(data.invocations)) return;
      live.invocations = data.invocations;
      for (const record of data.events) addEvent(runId, record, false);
      rebuildChecklist();
    } catch (error) {
      // Keep the plan's checklist; the next full load retries.
    }
  }

  function pushLog(line) {
    const live = state.live;
    // Long lines are cut, and say so, so a reader is never left thinking a
    // sentence ended where the log stopped.
    const text = String(line);
    live.log.push(text.length > LOG_LINE_CHARS
      ? `${text.slice(0, LOG_LINE_CHARS - 4)} ...`
      : text);
    if (live.log.length > LOG_LINES) live.log.splice(0, live.log.length - LOG_LINES);
  }

  function flushPartial() {
    if (state.live.partial) {
      pushLog(state.live.partial);
      state.live.partial = "";
    }
  }

  function addEvent(runId, record, render = true) {
    const live = state.live;
    if (!live || live.run_id !== runId || !record || typeof record !== "object") return;
    if (live.seen.has(record.sequence)) return;
    live.seen.add(record.sequence);
    live.events.push(record);
    const details = record.details && typeof record.details === "object" ? record.details : {};
    const kind = String(record.event_type);
    const status = String(record.status || "");
    if (kind === "command_output") {
      const text = live.partial + String(details.text ?? "");
      const lines = text.split("\n");
      live.partial = lines.pop();
      for (const line of lines) pushLog(line);
    } else {
      flushPartial();
    }
    if (kind === "command_output_omitted") {
      // Output left out of run.jsonl (in output/); the command's last records follow.
      const counts = details.output_omitted && details.output_omitted.lines;
      let count = 0;
      if (counts && typeof counts === "object") {
        for (const value of Object.values(counts)) if (typeof value === "number") count += value;
      }
      const saved = Array.isArray(details.output_files) ? details.output_files : [];
      const names = saved.map((file) => String((file && file.path) ?? "")).join(", ");
      pushLog(`[... ${count} output lines left out; full output in ${names} ...]`);
    }
    if (kind === "command_finished" || kind === "output_files") {
      // A command just saved its output: refresh the list so the links
      // appear during the run, not only when it is selected again.
      if (details.output_files || kind === "output_files") loadOutputFiles(runId);
    }
    if (kind === "build_started") live.stage = "building";
    else if (kind === "preflight_started") live.stage = "preflight";
    else if (kind === "test_started") live.stage = "tests";
    else if (kind === "run_finished") live.stage = "finished";

    if (kind === "manifest" && render && !Array.isArray(live.invocations)) {
      // Live: fetch the planned invocations (with estimates) and re-lay out.
      loadInvocations(runId);
    }
    if (kind === "libbtbb_pin" && status !== "passed") {
      // The libbtbb differs from the fork's install.sh pin, or was not compared.
      pushLog(`warning: ${String(details.message ?? "")}`);
    }
    if (kind === "lease_queued") {
      // A device was free while this run waited, and still not its turn:
      // why a run can start late with the bench looking idle. The harness
      // sends the line the report shows.
      const text = String(details.message ?? "");
      if (text) {
        if (!live.queued.includes(text)) live.queued.push(text);
        pushLog(`note: ${text}`);
      }
    }
    if (kind === "bootloader_identity") {
      // Which bootloader is under the firmware on this board. A board keeps
      // it through every firmware update, so it is shown beside the board
      // rather than as a step of the run.
      const serial = String(details.serial ?? "");
      const board = live.boards.get(serial) || {
        resource: String(details.resource ?? ""), serial, resets: 0, state: "", warning: "",
      };
      board.bootloader = bootloaderText(details.installed);
      // In update mode the stage says it wrote one and preflight then reads
      // the board again; the later read must not take away the fact that
      // this run installed it, which is what report.md keeps too.
      board.bootloaderWritten = board.bootloaderWritten || Boolean(details.written);
      const read = String(details.status ?? "");
      board.bootloaderStatus =
        board.bootloaderWritten && read === "current" ? "updated" : read;
      live.boards.set(serial, board);
      if (details.expected) {
        live.bootloaderExpected = bootloaderText(details.expected);
      }
      if (status === "failed" && details.reason) pushLog(`error: ${String(details.reason)}`);
    }
    if (kind === "bootloader_stage_finished" && status === "skipped" && details.reason) {
      pushLog(`note: bootloader stage skipped: ${String(details.reason)}`);
    }
    if (kind === "watchdog") {
      // The firmware reset a board, or did not: a count, never a failure.
      const serial = String(details.serial ?? "");
      const board = live.boards.get(serial) || {
        resource: String(details.resource ?? ""), serial, resets: 0, state: "", warning: "",
      };
      board.resets += typeof details.new_resets === "number" ? details.new_resets : 0;
      board.state = String(details.state ?? "");
      const warnings = Array.isArray(details.warnings) ? details.warnings : [];
      if (warnings.length) board.warning = String(warnings[warnings.length - 1]);
      live.boards.set(serial, board);
      for (const line of warnings) pushLog(`warning: ${String(line)}`);
      for (const line of Array.isArray(details.notes) ? details.notes : []) {
        pushLog(`note: ${String(line)}`);
      }
      if (status === "failed" && details.reason) pushLog(`error: ${String(details.reason)}`);
    }
    if (kind === "test_started") {
      const name = String(details.test_name ?? "");
      const id = String(details.invocation_id ?? "");
      let item = live.items.find((entry) => entry.invocation_id === id && entry.status === "pending") ||
        live.items.find((entry) => entry.name === name && entry.status === "pending");
      if (!item) {
        item = { name, estimated_s: null, status: "pending", reason: "" };
        live.items.push(item);
      }
      item.status = "running";
      item.started = record.timestamp;
      if (!live.testsStarted && record.timestamp) live.testsStarted = record.timestamp;
      live.byInvocation.set(String(details.invocation_id), item);
      pushLog(`=== TEST ${name} on ${details.host ?? ""} ===`);
    } else if (kind === "known_target") {
      // Which device the harness drove for this test, or that it had none;
      // the controller sends the summary the report shows.
      // The full line where there is a target; a short one otherwise, so a
      // suite of plain tests does not repeat a sentence down the column.
      const found = Array.isArray(details.targets) && details.targets.length > 0;
      const item = live.byInvocation.get(String(details.invocation_id));
      if (item) item.target = found ? String(details.summary ?? "") : "no known target";
    } else if (kind === "not_applicable") {
      // Left out before the run: the host named cannot run it, or this bench
      // has not got the hardware it binds (not a skip or a failure).
      const name = String(details.test_name ?? "");
      if (!details.invocation_id) {
        let item = live.items.find((entry) => entry.name === name && entry.status === "pending");
        if (!item && Array.isArray(live.invocations)) {
          // Left out at planning: not in the manifest, and takes no time.
          item = { name, estimated_s: 0, status: "pending", reason: "" };
          live.items.push(item);
        }
        if (item) {
          item.status = "not applicable";
          item.reason = String(details.reason || "");
        }
      }
      const what = details.case_name ? `${name} / ${details.case_name}` : name;
      pushLog(`[not applicable on ${details.host ?? ""}] ${what}: ${details.reason ?? ""}`);
    } else if (kind === "blocked") {
      // Also left out before the run, but the hardware it is about is what
      // is missing, so the run fails: shown as blocked, not as a skip.
      const name = String(details.test_name ?? "");
      // A test this run also runs keeps its own row: with `Ubertooth: each`
      // on a partly switchable bench the step runs for some boards and is
      // blocked for the rest, and a row cannot say both. The log line and
      // the failure summary carry the record instead.
      const running = Array.isArray(live.invocations)
        && live.invocations.some((invocation) => invocation.test_name === name);
      if (!running) {
        let item = live.items.find((entry) => entry.name === name && entry.status === "pending");
        if (!item && Array.isArray(live.invocations)) {
          // Not in the manifest, and it takes no time.
          item = { name, estimated_s: 0, status: "pending", reason: "" };
          live.items.push(item);
        }
        if (item) {
          item.status = "blocked";
          item.reason = String(details.reason || "");
        }
      }
      pushLog(`[blocked on ${details.host ?? ""}] ${name}: ${details.reason ?? ""}`);
    } else if (kind === "test_finished") {
      const item = live.byInvocation.get(String(details.invocation_id));
      if (item) {
        item.status = status === "not_applicable" ? "not applicable" : status;
        item.reason = String(details.reason || "");
      }
      pushLog(`=== TEST ${details.test_name ?? ""}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "build_step_started") {
      pushLog(`+++ ${details.step_name || details.step_id || ""} +++`);
    } else if (kind === "build_step_finished") {
      // The step's own outcome: it may have accepted a command that exited
      // non-zero, so this is not the status of its last command.
      pushLog(`--- ${details.step_name || details.step_id || ""} EXIT: ${status}${details.reason ? ": " + details.reason : ""} ---`);
    } else if (kind === "build_finished" || kind === "preflight_finished") {
      pushLog(`=== ${kind.replace("_finished", "").toUpperCase()}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "run_started") {
      pushLog(`=== RUN ${record.run_id}: ${details.plan_name ?? ""} / ${details.suite ?? ""} on ${details.host ?? ""} ===`);
    } else if (kind === "run_finished") {
      pushLog(`=== RUN ${status}${details.failure_reason ? ": " + details.failure_reason : ""} ===`);
    }
    if (render) renderLive();
  }

  // ----- saved output files ----------------------------------------------
  //
  // The files the run saved are listed from its own records (so the list
  // costs nothing to build) and read a page at a time, because one file can
  // be a soak's whole output. The viewer is deliberately unlike the log
  // tail: black on white in a fixed-width font, because it shows a saved
  // file and not a live stream.

  function fileSize(count) {
    const units = [["GiB", 1 << 30], ["MiB", 1 << 20], ["KiB", 1 << 10]];
    for (const [name, step] of units) {
      if (count >= step) return `${(count / step).toFixed(1)} ${name}`;
    }
    return `${count} bytes`;
  }

  async function loadOutputFiles(runId) {
    try {
      const data = await api("GET", `${runPath(runId)}/files`);
      if (!Array.isArray(data.files)) return;
      state.files.set(runId, data.files);
      renderLive();
    } catch (error) {
      // An older run, or a controller that has no files for it: the
      // checklist simply shows no links.
    }
  }

  function filesOf(runId, invocationId) {
    const files = state.files.get(runId) || [];
    return files.filter((file) => {
      const owner = (file && file.owner) || {};
      return String(owner.invocation_id ?? "") === String(invocationId ?? "");
    });
  }

  // The files that belong to no test: the build's and the preflight's, which
  // is where a failed build's compiler output is.
  function runFiles(runId) {
    const files = state.files.get(runId) || [];
    return files.filter((file) => !((file && file.owner) || {}).invocation_id);
  }

  // One link per file this test saved. A file a check failed on is marked,
  // so a reader looking at a failed test knows which one to open.
  function fileList(item) {
    const live = state.live;
    if (!live) return null;
    const files = item.invocation_id
      ? filesOf(live.run_id, item.invocation_id) : [];
    if (!files.length) return null;
    return fileLinks(live.run_id, files);
  }

  // A run a target runner carried out keeps its output files in that
  // runner's own run directory; the controller asks the runner for a page,
  // so such a file opens like any other while its runner is connected.
  function runnerConnected(runId) {
    const run = state.runs.get(runId);
    const target = run && state.targets.get(run.target);
    return Boolean(target && target.connected && target.online);
  }

  function fileLinks(runId, files) {
    const onTarget = !runnerConnected(runId);
    return el("ul", { className: "check-files" }, ...files.map((file) => {
      const checks = Array.isArray(file.checks) ? file.checks : [];
      const failed = checks.filter((check) => check && check.reason).length;
      const name = String(file.path).replace(/^output\//, "");
      const about = `${file.stream} · ${fileSize(file.bytes)} · ${checks.length} check`
        + `${checks.length === 1 ? "" : "s"}`
        + (failed ? `, ${failed} failed` : "");
      // Only while its runner is away is there nothing to open.
      if (file.readable === false && onTarget) {
        return el("li", {}, el("span", {
          className: failed ? "hint file-failed" : "hint",
          title: `${about} · in the target's own run directory, and its runner `
            + "is not connected",
          text: `${name} (on the target)`,
        }));
      }
      return el("li", {},
        el("button", {
          type: "button",
          className: failed ? "link file-failed" : "link",
          title: file.readable === false
            ? `${about} · read from the target through its runner` : about,
          onclick: () => openOutputFile(runId, String(file.path), 0),
          text: name,
        }));
    }));
  }

  async function openOutputFile(runId, path, offset) {
    $("live-file").hidden = false;
    $("file-status").textContent = "Loading…";
    const query = `path=${encodeURIComponent(path)}&offset=${Math.max(0, offset)}`;
    try {
      const page = await api("GET", `${runPath(runId)}/file?${query}`);
      state.openFile = { run_id: runId, path, page };
      renderOutputFile();
    } catch (error) {
      state.openFile = null;
      $("file-view").textContent = "";
      $("file-about").textContent = path;
      $("file-status").textContent = `Cannot read this file: ${error.message}`;
    }
  }

  function renderOutputFile() {
    const open = state.openFile;
    const live = $("live");
    if (!open) {
      $("live-file").hidden = true;
      live.classList.remove("live-showing-file");
      return;
    }
    live.classList.add("live-showing-file");
    const page = open.page;
    const first = page.offset + 1;
    const last = page.offset + page.length;
    const changed = page.file_bytes !== page.bytes;
    $("file-about").textContent =
      `${open.path} · ${page.stream} · ${fileSize(page.bytes)}`
      + (page.lines ? ` · ${page.lines} lines` : "")
      + ` · SHA-256 ${page.sha256}`
      + (changed
        ? ` · WARNING: ${fileSize(page.file_bytes)} on disk now, so this is `
          + "not the file the run recorded"
        : "");
    $("file-status").textContent = page.length
      ? `bytes ${first}–${last} of ${page.file_bytes}`
      : "this file is empty";
    $("file-previous").disabled = page.offset <= 0;
    $("file-next").disabled = Boolean(page.eof);
    const view = $("file-view");
    view.textContent = String(page.text ?? "");
    view.scrollTop = 0;
  }

  function pageOutputFile(direction) {
    const open = state.openFile;
    if (!open) return;
    const step = open.page.page_bytes || 1;
    const offset = direction < 0
      ? Math.max(0, open.page.offset - step)
      : open.page.offset + (open.page.length || step);
    openOutputFile(open.run_id, open.path, offset);
  }

  function itemStatus(item, run) {
    if (!run || run.status === "running") return item.status;
    if (item.status === "pending") return "not run";
    if (item.status === "running") return "did not finish";
    return item.status;
  }

  function renderLive() {
    const live = state.live;
    if (!live) return;
    const run = state.runs.get(live.run_id) || { run_id: live.run_id, status: "running" };
    const fields = [
      ["Run", el("code", { text: run.run_id })],
      ["Status", statusBadge(run.status)],
      ["Stage", live.stage],
      ["Ref", refLabel(run)],
      ["Plan / suite", `${run.plan ?? "–"} / ${run.suite ?? "–"}`],
      ["Target", run.target ?? "–"],
      ["Started", timeElement(run.created_at)],
      ["Finished", timeElement(run.finished_at)],
    ];
    if (run.reason) fields.push(["Reason", run.reason]);
    $("live-summary").replaceChildren(...fields.flatMap(([term, value]) =>
      [el("dt", { text: term }), el("dd", {}, value)]));
    $("cancel-button").hidden = run.status !== "running";

    // Per board: how many times the firmware's watchdog reset it during this
    // run, and the last warning if there was one. Hidden until a board is read.
    const boards = [...live.boards.values()];
    $("live-boards").hidden = boards.length === 0;
    $("boards").replaceChildren(...boards.map((board) => el("li", {
      className: board.resets ? "board board-reset" : "board",
    },
      el("span", { className: "board-name", text: board.resource || board.serial }),
      el("span", {
        className: "board-resets",
        // The count always, since it matters most on a board whose last read
        // failed; then what that read found, when it was not a record.
        text: ` ${board.resets} self-reset${board.resets === 1 ? "" : "s"}`
          + (board.state === "read" || !board.state ? ""
            : board.state === "absent" ? "; did not answer"
            : `; watchdog ${board.state}`),
      }),
      board.bootloader ? el("span", {
        className: board.bootloaderStatus === "stale"
          || board.bootloaderStatus === "unreadable" ? "reason" : "board-bootloader",
        text: `; bootloader ${board.bootloader}`
          + (board.bootloaderWritten ? " (installed by this run)" : ""),
      }) : null,
      board.warning ? el("span", { className: "reason", text: ` — ${board.warning}` }) : null)));
    const expectedNote = $("bootloader-expected");
    expectedNote.hidden = !live.bootloaderExpected || boards.length === 0;
    expectedNote.textContent = live.bootloaderExpected
      ? `This run expects bootloader ${live.bootloaderExpected}.` : "";

    // Devices that were free while this run waited for its turn, and the
    // command that had the turn: why a run can start late with the bench
    // looking idle. Hidden unless the run waited behind somebody.
    $("live-queued").hidden = live.queued.length === 0;
    $("queued").replaceChildren(
      ...live.queued.map((text) => el("li", { text })));

    $("checklist").replaceChildren(...live.items.map((item) => {
      const shown = itemStatus(item, run);
      return el("li", {
        className: `check check-${shown.replace(/ /g, "-")}`,
        "aria-current": shown === "running" ? "step" : null,
      },
        statusBadge(shown), " ", el("span", { className: "check-name", text: item.name }),
        item.estimated_s !== null && item.estimated_s !== undefined
          ? el("span", { className: "hint check-estimate", text: ` (about ${formatSeconds(item.estimated_s)})` }) : null,
        item.target ? el("span", { className: "reason check-target", text: item.target }) : null,
        item.reason ? el("span", { className: "reason", text: ` — ${item.reason}` }) : null,
        fileList(item));
    }));
    const runOwned = runFiles(live.run_id);
    if (runOwned.length) {
      $("checklist").append(el("li", { className: "check-note" },
        "Build and preflight output: ", fileLinks(live.run_id, runOwned)));
    }
    if (!live.items.length) {
      $("checklist").replaceChildren(el("li", {
        className: "check-note",
        text: "Tests appear here as they start (this run's plan is not in the controller's plans directory).",
      }));
    }
    const logNode = $("log");
    const lines = live.partial ? [...live.log, live.partial] : live.log;
    logNode.textContent = lines.join("\n");
    logNode.scrollTop = logNode.scrollHeight;
    renderClock();
  }

  function renderClock() {
    const live = state.live;
    if (!live) return;
    const run = state.runs.get(live.run_id);
    if (!run || !run.created_at) {
      $("live-clock").textContent = "";
      return;
    }
    const start = Date.parse(run.created_at);
    const end = run.finished_at ? Date.parse(run.finished_at) : Date.now();
    const elapsed = Math.max(0, (end - start) / 1000);
    const estimates = live.items.map((item) => item.estimated_s);
    const known = live.planned && estimates.length > 0 &&
      estimates.every((value) => value !== null && value !== undefined);
    if (!known) {
      $("live-clock").textContent = `Elapsed ${formatSeconds(elapsed)}`;
      const missing = estimates.filter((value) => value === null || value === undefined).length;
      $("live-estimate").textContent = live.planned
        ? `No countdown: ${missing} of ${estimates.length} tests have no estimated duration, so only elapsed time is shown.`
        : "No countdown: this run's plan is unknown here, so only elapsed time is shown.";
      return;
    }
    const total = estimates.reduce((sum, value) => sum + value, 0);
    $("live-estimate").textContent = `Estimate ${formatSeconds(total)} from the catalog's per-test estimates; build and preflight are not included.`;
    const testsStart = live.testsStarted ? Date.parse(live.testsStarted) : NaN;
    const testsElapsed = Number.isNaN(testsStart) ? null : Math.max(0, (end - testsStart) / 1000);
    if (run.status !== "running") {
      $("live-clock").textContent = `Finished after ${formatSeconds(elapsed)}`;
    } else if (testsElapsed === null) {
      $("live-clock").textContent = `Countdown starts with the first test (elapsed ${formatSeconds(elapsed)})`;
    } else if (testsElapsed <= total) {
      $("live-clock").textContent = `About ${formatSeconds(total - testsElapsed)} remaining (elapsed ${formatSeconds(elapsed)})`;
    } else {
      $("live-clock").textContent = `Over the estimate by ${formatSeconds(testsElapsed - total)} (elapsed ${formatSeconds(elapsed)})`;
    }
  }

  async function cancelRun() {
    const live = state.live;
    if (!live) return;
    const status = $("cancel-status");
    status.textContent = "Asking the runner to cancel…";
    try {
      await api("POST", `${runPath(live.run_id)}/cancel`, { reason: "canceled from the web GUI" });
      status.textContent = "Cancel accepted; the run stops at its next safe point.";
    } catch (error) {
      status.textContent = `Cancel refused: ${error.message}`;
    }
  }

  // ----- history -----------------------------------------------------------

  function upsertRun(run) {
    if (!run || typeof run.run_id !== "string") return;
    state.runs.set(run.run_id, run);
    renderHistory();
    if (state.live && state.live.run_id === run.run_id) renderLive();
  }

  function renderHistory() {
    const runs = [...state.runs.values()].sort((a, b) =>
      String(b.created_at).localeCompare(String(a.created_at)) || b.run_id.localeCompare(a.run_id));
    $("history-empty").hidden = runs.length > 0;
    $("history").replaceChildren(...runs.map((run) => {
      const actions = el("td", { className: "actions" },
        el("button", {
          type: "button", text: "Watch", "aria-label": `Watch run ${run.run_id}`,
          onclick: () => selectRun(run.run_id),
        }));
      if (run.report) {
        actions.append(" ", el("a", {
          className: "button-link",
          href: `${runPath(run.run_id)}/report.md`, text: "report.md",
          "aria-label": `report.md of run ${run.run_id}`,
        }));
      }
      if (run.status !== "running" && run.status !== "passed") {
        actions.append(" ", el("button", {
          type: "button", text: "Failures", "aria-label": `Failure summary of run ${run.run_id}`,
          onclick: () => showFailures(run.run_id),
        }));
      }
      return el("tr", {},
        el("td", { className: "time" }, timeElement(run.created_at)),
        el("td", { className: "time" }, timeElement(run.finished_at)),
        el("td", {}, el("code", { text: run.run_id })),
        el("td", { text: refLabel(run) }),
        el("td", { text: `${run.plan ?? "–"} / ${run.suite ?? "–"}` }),
        el("td", { text: run.target }),
        el("td", {}, statusBadge(run.status)),
        actions);
    }));
  }

  async function loadRuns() {
    try {
      const data = await api("GET", "/api/runs");
      state.runs = new Map(data.runs.map((run) => [run.run_id, run]));
      renderHistory();
      if (state.live) rebuildChecklist();
    } catch (error) {
      $("history-empty").textContent = `Cannot list runs: ${error.message}`;
    }
  }

  async function showFailures(runId) {
    const box = $("failures");
    const list = $("failures-list");
    box.hidden = false;
    $("failures-heading").textContent = `Failure summary: ${runId}`;
    list.replaceChildren(el("li", { text: "Loading…" }));
    try {
      const data = await api("GET", runPath(runId));
      const run = data.run;
      $("failures-run").replaceChildren(
        el("dt", { text: "Target" }), el("dd", { text: run.target }),
        el("dt", { text: "Ref" }), el("dd", { text: refLabel(run) }),
        el("dt", { text: "Plan / suite" }), el("dd", { text: `${run.plan ?? "–"} / ${run.suite ?? "–"}` }),
        el("dt", { text: "Status" }), el("dd", {}, statusBadge(run.status)));
      if (!data.failures.length) {
        list.replaceChildren(el("li", { text: "No failed step recorded." }));
      } else {
        list.replaceChildren(...data.failures.map((failure) => el("li", { className: "failure" },
          statusBadge(failure.status), " ", el("strong", { text: failure.step }),
          failure.reason ? el("span", { text: `: ${failure.reason}` }) : null,
          failure.log && failure.log.length
            ? el("pre", { className: "log", text: failure.log.join("\n") }) : null)));
      }
    } catch (error) {
      list.replaceChildren(el("li", { text: `Cannot load the run: ${error.message}` }));
    }
    $("failures-heading").focus();
  }

  // ----- live socket -------------------------------------------------------

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    const socket = new WebSocket(`${scheme}${window.location.host}/ws/live`);
    state.socket = socket;
    socket.addEventListener("open", () => {
      state.retry = 1000;
      $("connection").className = "conn conn-up";
      $("connection").textContent = "Connected to the controller (live updates on).";
      loadRuns();
      if (state.selected) selectRun(state.selected, false);
    });
    socket.addEventListener("message", (message) => {
      let data;
      try {
        data = JSON.parse(message.data);
      } catch (error) {
        return;
      }
      if (data.type === "snapshot") {
        state.targets = new Map((data.targets || []).map((target) => [target.host, target]));
        renderTargets();
        // A file that stayed on the target is a link only while its runner
        // is connected, so the live view follows the target's status.
        renderLive();
      } else if (data.type === "target" && data.target) {
        state.targets.set(data.target.host, data.target);
        renderTargets();
        renderLive();
      } else if (data.type === "run") {
        upsertRun(data.run);
      } else if (data.type === "event") {
        addEvent(data.run_id, data.event);
      }
    });
    socket.addEventListener("close", () => {
      const wait = state.retry;
      state.retry = Math.min(state.retry * 2, 30000);
      $("connection").className = "conn conn-down";
      $("connection").textContent = `Disconnected from the controller; retrying in ${Math.round(wait / 1000)} s.`;
      window.setTimeout(connect, wait);
    });
  }

  // The tests of the suite that need a build option, counted the way
  // /api/plans counts the suite's own flags: only the steps the chosen
  // target runs, since a step it does not run must not make it build.
  function neededBy(suite, need) {
    if (!suite) return [];
    return suite.tests
      .filter((test) => test[need] && test.runs_on_target !== false)
      .map((test) => test.name);
  }

  // Flash and the two build options need Build: each is disabled, with a
  // note, while Build is unticked. Then, for each build option and the
  // chosen suite:
  //
  // - a suite with a test that cannot run without the option has it ticked
  //   and held there, since the run would be refused otherwise;
  // - a suite that would only leave cases out says which, and invites the
  //   tick, but does not tick it: an option ticked for the operator costs
  //   build time they did not ask for, and the firmware build also needs a
  //   toolchain the target may not have, which would turn a run that used
  //   to pass with those cases left out into one that fails;
  // - a box the operator has set keeps what they set.
  //
  // Called whenever Build, Flash, a box, the plan, the suite or the plan
  // list changes.
  // The note's own words, kept so syncBuildOptions can add a sentence to
  // them without reading back what it added last time.
  let bootloaderNoteText = "";

  function syncBuildOptions() {
    const build = $("build").checked;
    const flashing = build && $("flash-build").checked;
    $("flash-build").disabled = !build;
    $("flash-build-note").hidden = build;
    // Check and Update build a bootloader from the ref, so without Build the
    // only thing a run can do is record what each board has.
    const bootloaderChoice = $("bootloader");
    bootloaderChoice.disabled = !build;
    // Update writes flash before preflight, so it needs the boards flashed
    // with this run's build first.
    bootloaderChoice.querySelector('option[value="update"]').disabled = !flashing;
    if (!build || (!flashing && bootloaderChoice.value === "update")) {
      bootloaderChoice.value = "record";
    }
    const suite = findSuite($("plan").value, $("suite").value);
    const suiteName = $("suite").value;
    for (const option of BUILD_OPTIONS) {
      const box = $(option.id);
      const required = neededBy(suite, option.require);
      const needed = neededBy(suite, option.need);
      // What the operator set, remembered apart from the box: a tick this
      // suite forces must not follow them to the next suite, which would
      // build what they never asked for.
      box.checked = required.length > 0 || option.chosen;
      box.disabled = !build || required.length > 0;
      const sentences = [option.cost];
      if (required.length) {
        sentences.push(`The ${suiteName} suite cannot run without ${option.what}: `
          + `${required.join(", ")}, so it is ticked for this suite.`);
      } else if (needed.length && !box.checked) {
        sentences.push(`Without ${option.what} the ${suiteName} suite leaves cases `
          + `out: ${needed.join(", ")}. Tick it to run them.`);
        if (option.builtByFlash && flashing) {
          // run-suite builds the image anyway when the run flashes a board.
          sentences.push("A run that flashes a board builds the image for that"
            + " anyway, and records it, so those cases may run without this.");
        }
      }
      if (!build) sentences.push("Needs Build.");
      $(`${option.id}-note`).textContent = sentences.join(" ");
    }
    const bootloaderNote = $("bootloader-note");
    bootloaderNote.textContent = bootloaderNoteText
      + (build ? "" : " Without Build a run can only record what is installed.");
  }

  function start() {
    $("run-form").addEventListener("submit", submitRun);
    bootloaderNoteText = $("bootloader-note").textContent;
    for (const option of BUILD_OPTIONS) {
      option.cost = $(`${option.id}-note`).textContent;
      option.chosen = $(option.id).checked;
      // Only the operator fires this (a held box is disabled), so it is
      // their choice that is remembered, and the note says different things
      // for a ticked and an unticked box.
      $(option.id).addEventListener("change", () => {
        option.chosen = $(option.id).checked;
        syncBuildOptions();
      });
    }
    // Another target is another plan listing: its own estimates, and its own
    // answer to what the suite leaves out and what it must build.
    $("target").addEventListener("change", loadPlans);
    $("build").addEventListener("change", syncBuildOptions);
    // Flashing builds the firmware image anyway, which the note says.
    $("flash-build").addEventListener("change", syncBuildOptions);
    $("bootloader").addEventListener("change", syncBuildOptions);
    syncBuildOptions();
    $("refs-reload").addEventListener("click", loadRefs);
    $("plan").addEventListener("change", () => renderSuites());
    $("suite").addEventListener("change", renderSuiteTests);
    $("cancel-button").addEventListener("click", cancelRun);
    $("file-previous").addEventListener("click", () => pageOutputFile(-1));
    $("file-next").addEventListener("click", () => pageOutputFile(1));
    $("file-close").addEventListener("click", () => {
      state.openFile = null;
      renderOutputFile();
    });
    renderTargetSelect();
    loadRefs();
    loadPlans();
    // /ui/#run=<run_id> shows that run; &file=<path> also opens that saved
    // file in the viewer, so a file view can be linked to and reopened.
    const linked = /^#run=([^&]+)(?:&file=(.+))?$/.exec(window.location.hash);
    if (linked && RUN_ID.test(linked[1])) {
      state.selected = linked[1];
      if (linked[2]) state.wantedFile = decodeURIComponent(linked[2]);
    }
    connect();
    window.setInterval(renderClock, 1000);
  }

  document.addEventListener("DOMContentLoaded", start);
})();
