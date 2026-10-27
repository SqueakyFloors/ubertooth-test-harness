// Ubertooth harness GUI. Vanilla JavaScript, no build step, no other origin.
//
// Every string from the controller or a runner reaches the page through
// textContent or a fixed-prefix attribute; nothing is parsed as HTML.
"use strict";

(function () {
  const LOG_LINES = 200;
  const LOG_LINE_CHARS = 500;
  const STATUS_TEXT = {
    pending: ["○", "pending"],
    running: ["…", "running"],
    passed: ["✓", "passed"],
    failed: ["✗", "failed"],
    canceled: ["■", "canceled"],
    skipped: ["–", "skipped"],
    "not applicable": ["–", "not applicable"],
    error: ["✗", "error"],
    lost: ["?", "lost"],
    "not run": ["○", "not run"],
    "not reached": ["○", "not reached"],
    "did not finish": ["✗", "did not finish"],
    "not requested": ["–", "not requested"],
    online: ["●", "online"],
    offline: ["○", "offline"],
    busy: ["◆", "busy"],
  };

  const state = {
    historySort: { key: "created_at", dir: -1 },  // dir 1 ascending, -1 descending
    openFailures: null,       // run ID whose failure summary row is open
    failureNodes: new Map(),  // run ID -> rendered failure summary
    targets: new Map(),
    runs: new Map(),
    plans: [],
    // ref option value ("<kind>:<name>") -> {name, kind, commit} as listed
    refOptions: new Map(),
    selected: null, // run_id shown in the live view
    live: null, // {run_id, seen:Set, events:[], items:[], byInvocation:Map, log:[], partial, stage}
    socket: null,
    retry: 1000,
  };

  const $ = (id) => document.getElementById(id);

  // Messages and reasons are shown in sentence case ("error: x" -> "Error: x");
  // the text is otherwise as the tool or runner wrote it.
  function sentence(text) {
    const value = String(text ?? "");
    return value ? value[0].toUpperCase() + value.slice(1) : value;
  }

  // Display name of a target: its registered ID with spaces for underscores
  // (Host_Linux -> "Host Linux"); the ID itself is unchanged everywhere else.
  function targetName(host) {
    return String(host ?? "–").replace(/_/g, " ");
  }

  function targetTitle(target) {
    const name = targetName(target.host);
    return target.hostname ? `${name} (${target.hostname})` : name;
  }

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
    // A step a finished run never reached keeps its pill, empty (screen
    // readers still hear "not reached").
    if (status === "not reached") {
      return el("span", { className: "status status-not-reached" },
        el("span", { "aria-hidden": "true", text: "\u00a0" }),
        el("span", { className: "visually-hidden", text: "not reached" }));
    }
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

  // Compact duration: only the non-zero parts, no leading zeros
  // (4 -> "4s", 130 -> "2m 10s", 3900 -> "1h 5m"); 0 -> "0s".
  function formatSeconds(total) {
    const seconds = Math.max(0, Math.round(total));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const parts = [];
    if (h) parts.push(`${h}h`);
    if (m) parts.push(`${m}m`);
    if (s || !parts.length) parts.push(`${s}s`);
    return parts.join(" ");
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

  // One small box per piece of equipment the target reports, grouped by kind.
  // Ubertooths (the devices under test) first; hubs and Bluetooth devices
  // join here once the runner reports them.
  function attachedEquipment(target) {
    const serials = target.ubertooth_serials;
    let boards;
    if (!Array.isArray(serials)) {
      boards = [el("p", { className: "hint",
        text: `Unknown: ${target.enumeration_error || "the runner could not list its USB devices"}` })];
    } else if (!serials.length) {
      boards = [el("p", { className: "hint", text: "No Ubertooth attached." })];
    } else {
      // The runner lists its boards only when it registers, so say when;
      // unknown while it is offline.
      const when = target.registered_at
        ? new Date(target.registered_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : "";
      const state = target.status === "online"
        ? ["ok", when ? `Detected on USB ${when}` : "Detected on USB"]
        : ["warn", "Unknown (runner offline)"];
      // Named by the alias the runs use (learned from the latest run's plan);
      // until a run has started, by "Ubertooth" and the serial below.
      const aliases = target.board_aliases || {};
      boards = [el("ul", { className: "devices" }, ...serials.map((serial) =>
        el("li", { className: "device" },
          el("div", { className: "device-head" },
            el("span", { className: `dot dot-${state[0]}`, "aria-hidden": "true" }),
            el("span", { className: "device-name",
              text: aliases[String(serial).toLowerCase()] ? targetName(aliases[String(serial).toLowerCase()]) : "Ubertooth" }),
            el("span", { className: "device-state", text: state[1] })),
          el("code", { className: "device-serial", title: "USB serial number", text: serial }))))];
    }
    return el("section", { className: "equipment", "aria-label": `Attached equipment on ${targetName(target.host)}` },
      el("h4", { text: "Attached equipment" }),
      el("div", { className: "equipment-group" },
        el("h5", { text: "Devices under test" }), ...boards));
  }

  function renderTargets() {
    const list = $("targets");
    const targets = [...state.targets.values()].sort((a, b) => a.host.localeCompare(b.host));
    $("targets-empty").hidden = targets.length > 0;
    list.replaceChildren(...targets.map((target) => {
      const os = target.os || {};
      const osText = [os.system, os.macos_version || os.distribution, os.release, os.architecture]
        .filter(Boolean).join(" ");
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
          el("h3", { id: `target-${target.host}`, text: targetTitle(target) }),
          el("dl", { className: "summary summary-compact" },
            el("dt", { text: "Status" }), el("dd", { className: "badges" }, statusBadge(target.status),
              target.active_run_id ? statusBadge("busy") : null),
            el("dt", { text: "OS" }), el("dd", { text: osText || "unknown" }),
            el("dt", { text: "Python" }), el("dd", { text: target.python }),
            el("dt", { text: "Last seen" }), el("dd", {}, timeElement(target.last_seen_at)),
            el("dt", { text: "Active run" }), el("dd", {}, active)),
          attachedEquipment(target)));
    }));
    renderTargetSelect();
  }

  function renderTargetSelect() {
    const select = $("target");
    const current = select.value;
    const targets = [...state.targets.values()].sort((a, b) => a.host.localeCompare(b.host));
    if (!targets.length) {
      select.replaceChildren(el("option", { value: "", text: "(no targets registered)" }));
      return;
    }
    select.replaceChildren(...targets.map((target) => {
      const notes = [target.status];
      if (target.active_run_id) notes.push("busy");
      return el("option", { value: target.host, text: `${targetName(target.host)} (${notes.join(", ")})` });
    }));
    if (targets.some((target) => target.host === current)) select.value = current;
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
      // Branches first (main at the top, as the controller orders them),
      // then tags; the remote's HEAD is the same commit as its default branch
      // and is listed last.
      const groups = { branch: "Branches", tag: "Tags", default: "Remote HEAD" };
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
      status.textContent = `${data.refs.length} refs from ${data.repository}, listed ${data.fetched_at}`;
    } catch (error) {
      state.refOptions = new Map();
      select.replaceChildren(el("option", { value: "", text: "(refs unavailable)" }));
      status.textContent = `Cannot list refs: ${error.message}`;
    }
  }

  async function loadPlans() {
    try {
      const data = await api("GET", "/api/plans");
      state.plans = data.plans;
    } catch (error) {
      state.plans = [];
      $("plan-status").textContent = `Cannot list plans: ${error.message}`;
    }
    const select = $("plan");
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
    if (usable.some((plan) => plan.name === "fast")) select.value = "fast";
    renderSuites();
    if (state.live) rebuildChecklist();
  }

  function findSuite(planName, suiteName) {
    const plan = state.plans.find((item) => item.name === planName && !item.error);
    return plan ? plan.suites.find((suite) => suite.name === suiteName) || null : null;
  }

  function renderSuites() {
    const plan = state.plans.find((item) => item.name === $("plan").value);
    const select = $("suite");
    const suites = plan ? plan.suites : [];
    select.replaceChildren(...suites.map((suite) =>
      el("option", { value: suite.name, text: `${suite.name} (${suite.tests.length} tests)` })));
    const same = suites.find((suite) => suite.name === plan?.name);
    if (same) select.value = same.name;
    renderSuiteTests();
  }

  function renderSuiteTests() {
    const suite = findSuite($("plan").value, $("suite").value);
    $("suite-tests").textContent = suite ? suite.tests.map((test) => test.name).join(", ") : "";
    const box = $("suite-estimate");
    box.hidden = !suite;
    if (!suite) return;
    const known = suite.estimated_s !== null && suite.estimated_s !== undefined;
    box.classList.toggle("estimate-unknown", !known);
    $("suite-estimate-value").textContent = known ? formatSeconds(suite.estimated_s) : "no estimate";
    $("suite-estimate-note").textContent = estimateText(suite);
  }

  // The note under the estimate box; an indicator, so no closing period.
  function estimateText(suite) {
    if (suite.estimated_s !== null && suite.estimated_s !== undefined) {
      const perBoard = suite.per_board_tests
        ? `; ${suite.per_board_tests} test(s) run once per selected Ubertooth and are counted once here (the live countdown counts every board)`
        : "";
      return `From the catalog's per-test estimates; build and preflight not included${perBoard}`;
    }
    return `${suite.unestimated_tests} of ${suite.tests.length} tests have no estimated duration, so the live view shows elapsed time only`;
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
      // Flashing applies only to a run that builds.
      flash_build: $("build").checked && $("flash-build").checked,
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
      button.disabled = false;
    }
  }

  // ----- live run ----------------------------------------------------------

  function newLive(runId) {
    return {
      run_id: runId, seen: new Set(), events: [], items: [],
      byInvocation: new Map(), log: [], partial: "", stage: "waiting",
      invocations: null,
      // invocation ID -> transcript lines ({kind, text}) of that test.
      transcripts: new Map(), partials: new Map(),
      shown: null,  // invocation ID whose transcript is open
    };
  }

  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  // past: opened from History (a grey "Past run" card). A run started here or
  // followed from its target stays a "Live run" card after it ends.
  async function selectRun(runId, scroll = true, past = false) {
    if (!RUN_ID.test(String(runId))) return;
    state.selected = runId;
    state.selectedPast = past;
    // A link to this view: /ui/#run=<run_id>.
    window.history.replaceState(null, "", `#run=${runId}`);
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
    } catch (error) {
      $("cancel-status").textContent = `Cannot load run: ${error.message}`;
    }
    if (scroll) $("live-heading").scrollIntoView({ block: "start" });
  }

  // The boards an invocation uses, e.g. "UT Linux0" or "sender: UT Linux0".
  function boardLabels(resources) {
    if (!resources || typeof resources !== "object") return [];
    return Object.entries(resources).map(([role, alias]) =>
      role === "Ubertooth" ? targetName(alias) : `${role}: ${targetName(alias)}`);
  }

  function rebuildChecklist() {
    const live = state.live;
    if (!live) return;
    const run = state.runs.get(live.run_id);
    const suite = run ? findSuite(run.plan, run.suite) : null;
    let tests = suite ? suite.tests : [];
    if (run && Array.isArray(run.tests) && run.tests.length) {
      tests = tests.filter((test) => run.tests.includes(test.name));
    }
    if (Array.isArray(live.invocations)) {
      // The run's manifest: one item per invocation on the target, so a test
      // with Ubertooth: each counts every board it selected.
      live.items = live.invocations.map((invocation) => ({
        name: invocation.test_name, invocation_id: invocation.invocation_id,
        estimated_s: invocation.estimated_s, status: "pending", reason: "",
        boards: boardLabels(invocation.resources),
      }));
    } else {
      live.items = tests.map((test) => ({
        name: test.name, estimated_s: test.estimated_s, status: "pending", reason: "",
      }));
    }
    live.planned = Array.isArray(live.invocations) || Boolean(suite);
    live.byInvocation = new Map();
    // Records from the socket and from REST may interleave; replay in order.
    const events = [...live.events].sort((a, b) => a.sequence - b.sequence);
    live.events = [];
    live.seen = new Set();
    live.log = [];
    live.partial = "";
    live.stage = "waiting";
    // Phases before the tests, shown above them; status follows their events.
    const runInfo = state.runs.get(live.run_id);
    // "Boards" is the wait for boards another session holds: the runner
    // leases them before the run's first record, so it passes on that record.
    live.phases = [
      { key: "boards", name: "Boards", status: "running", reason: "Waiting for the run's boards (another session may hold them)" },
      { key: "build", name: "Build", status: runInfo && runInfo.build === false ? "not requested" : "pending", reason: "" },
      { key: "preflight", name: "Preflight", status: "pending", reason: "" },
    ];
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
    live.log.push(String(line).slice(0, LOG_LINE_CHARS));
    if (live.log.length > LOG_LINES) live.log.splice(0, live.log.length - LOG_LINES);
  }

  function flushPartial() {
    if (state.live.partial) {
      pushLog(state.live.partial);
      state.live.partial = "";
    }
  }

  // One test's transcript: each command sent, its output as it arrives
  // (stderr marked), and how it ended. Keyed by invocation ID.
  function recordTranscript(live, kind, status, details) {
    // Tests by invocation ID; build and preflight output (no invocation ID)
    // under "phase:build" / "phase:preflight" while that step runs.
    const phaseKey = kind.startsWith("build") || live.stage === "building" ? "phase:build"
      : kind.startsWith("preflight") || live.stage === "preflight" ? "phase:preflight" : null;
    const id = details.invocation_id || phaseKey;
    if (!id) return;
    const key = String(id);
    if (!live.transcripts.has(key)) live.transcripts.set(key, []);
    const lines = live.transcripts.get(key);
    const flush = (stream) => {
      const pkey = `${key}\u0000${stream}`;
      const rest = live.partials.get(pkey);
      if (rest) { lines.push({ kind: stream, text: rest }); live.partials.delete(pkey); }
    };
    if (kind === "command_started") {
      const argv = Array.isArray(details.argv) ? details.argv.map(String).join(" ") : "";
      lines.push({ kind: "command", text: `$ ${argv}` });
    } else if (kind === "command_output") {
      const stream = details.stream === "stderr" ? "stderr" : "stdout";
      const pkey = `${key}\u0000${stream}`;
      const parts = ((live.partials.get(pkey) || "") + String(details.text ?? "")).split("\n");
      live.partials.set(pkey, parts.pop());
      for (const text of parts) lines.push({ kind: stream, text });
    } else if (kind === "command_output_omitted") {
      lines.push({ kind: "note", text: "… output left out here (the full output is in the run's output/ directory)" });
    } else if (kind === "command_finished") {
      flush("stdout"); flush("stderr");
      const rc = details.returncode === null || details.returncode === undefined ? "–" : details.returncode;
      const took = typeof details.duration_s === "number" ? `, ${details.duration_s.toFixed(2)} s` : "";
      const extra = details.timed_out ? ", timed out" : details.cancelled ? ", cancelled" : "";
      lines.push({ kind: status === "passed" || status === "stopped" ? "result" : "result-bad",
        text: `→ exit ${rc}${took}${extra} (${status})` });
    } else if (kind === "build_step_started") {
      lines.push({ kind: "note", text: `+++ ${details.step_name ?? details.step_id ?? ""} +++` });
    } else if (kind === "test_finished" || kind === "build_finished" || kind === "preflight_finished") {
      flush("stdout"); flush("stderr");
      lines.push({ kind: status === "passed" ? "result" : "result-bad",
        text: `=== ${status}${details.reason ? `: ${details.reason}` : ""} ===` });
    }
    if (lines.length > 4000) lines.splice(0, lines.length - 4000);
  }

  // Keep the running step near the top of the test list, unless the user
  // has scrolled the list themselves ("Follow the current test" resumes).
  function followCurrent(live) {
    const box = $("checklist-scroll");
    const current = box.querySelector('[aria-current="step"]');
    // Following resumes by itself when the next step starts.
    const step = current ? current.textContent : null;
    if (step !== live.followedStep) { live.followedStep = step; live.follow = true; }
    if (live.follow === false || !current) return;
    const top = Math.max(0, current.offsetTop - box.offsetTop - current.offsetHeight);
    if (Math.abs(box.scrollTop - top) > 2) {
      box.scrollTop = top;
    }
  }

  function showTranscript(id) {
    const live = state.live;
    if (!live) return;
    live.shown = live.shown === id ? null : id;
    renderLive();
    if (live.shown) $("transcript").focus();
  }

  function renderTranscript(live) {
    const box = $("transcript-box");
    const id = live.shown;
    box.hidden = !id;
    if (!id) return;
    const item = live.items.find((entry) => entry.invocation_id === id) ||
      [...live.byInvocation.entries()].filter(([key]) => key === id).map(([, value]) => value)[0];
    const phase = (live.phases || []).find((entry) => `phase:${entry.key}` === id);
    $("transcript-title").textContent = `Transcript: ${phase ? phase.name : item ? item.name : id}`;
    const lines = live.transcripts.get(id) || [];
    const node = $("transcript");
    const atBottom = node.scrollTop + node.clientHeight >= node.scrollHeight - 4;
    node.replaceChildren(...(lines.length ? lines : [{ kind: "note", text: "Nothing recorded yet" }])
      .map((line) => el("span", { className: `t-${line.kind}`, text: `${line.text}\n` })));
    if (atBottom) node.scrollTop = node.scrollHeight;
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
    recordTranscript(live, kind, status, details);
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
      pushLog(`[... ${count} output lines left out; full output in ${String(details.output_file ?? "")} ...]`);
    }
    const boardsPhase = (live.phases || []).find((entry) => entry.key === "boards");
    if (boardsPhase && boardsPhase.status === "running") {
      boardsPhase.status = "passed";
      boardsPhase.reason = "";
    }
    if (kind === "build_started" || kind === "preflight_started") {
      const phase = (live.phases || []).find((entry) => `${entry.key}_started` === kind);
      if (phase) phase.status = "running";
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
    } else if (kind === "not_applicable") {
      // Left out before the run: the host cannot run it (not a skip or failure).
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
    } else if (kind === "test_finished") {
      const item = live.byInvocation.get(String(details.invocation_id));
      if (item) {
        item.status = status === "not_applicable" ? "not applicable" : status;
        item.reason = String(details.reason || "");
      }
      pushLog(`=== TEST ${details.test_name ?? ""}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "build_step_started") {
      pushLog(`+++ ${details.step_name ?? details.step_id ?? ""} +++`);
    } else if (kind === "build_finished" || kind === "preflight_finished") {
      const phase = (live.phases || []).find((entry) => `${entry.key}_finished` === kind);
      if (phase) { phase.status = status || "passed"; phase.reason = String(details.reason || ""); }
      pushLog(`=== ${kind.replace("_finished", "").toUpperCase()}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "run_started") {
      pushLog(`=== RUN ${record.run_id}: ${details.plan_name ?? ""} / ${details.suite ?? ""} on ${details.host ?? ""} ===`);
    } else if (kind === "run_finished") {
      pushLog(`=== RUN ${status}${details.failure_reason ? ": " + details.failure_reason : ""} ===`);
    }
    if (render) renderLive();
  }

  // A run has ended only once it has a final status; until then a test that
  // has not started is "pending", and only after the end "not reached".
  const FINAL = new Set(["passed", "failed", "error", "canceled", "lost"]);
  function runEnded(run) {
    return Boolean(run) && FINAL.has(run.status);
  }

  function itemStatus(item, run) {
    if (!runEnded(run)) return item.status;
    if (item.status === "pending") return "not reached";
    if (item.status === "running") return "did not finish";
    return item.status;
  }

  // Every board the run's plan uses (from its manifest), or why it is unknown.
  function runBoards(live) {
    if (!Array.isArray(live.invocations)) return "Known once the run's plan arrives";
    const aliases = new Set();
    for (const invocation of live.invocations) {
      for (const alias of Object.values(invocation.resources || {})) aliases.add(targetName(alias));
    }
    return aliases.size ? [...aliases].sort().join(", ") : "None (host only)";
  }

  function renderLive() {
    const live = state.live;
    if (!live) return;
    const run = state.runs.get(live.run_id) || { run_id: live.run_id, status: "running" };
    // A run opened from History is a past run (grey card); one started or
    // followed here stays "Live run" when it ends.
    const isLive = !state.selectedPast;
    $("live-heading").textContent = isLive ? "Live run" : "Past run";
    $("live-heading").closest(".panel").classList.toggle("panel-past", !isLive);
    // Column groups across the top of the card; each group stacks its
    // label/value pairs vertically. "–" until a value is known.
    const groups = [
      [["Run", el("code", { text: run.run_id })],
        ["Started", timeElement(run.created_at)], ["Finished", timeElement(run.finished_at)]],
      [["Status", statusBadge(run.status)], ["Stage", live.stage]],
      [["Ref", refLabel(run)],
        ["Plan / suite", `${run.plan ?? "–"} / ${run.suite ?? "–"}`],
        ["Target", run.target ? targetName(run.target) : "–"]],
      [["Boards", runBoards(live)]],
    ];
    const items = groups.map((pairs) => el("div", { className: "summary-item" },
      ...pairs.flatMap(([term, value]) => [el("dt", { text: term }), el("dd", {}, value)])));
    if (run.reason) {
      items.push(el("div", { className: "summary-item summary-item-wide" },
        el("dt", { text: "Reason" }), el("dd", {}, sentence(run.reason))));
    }
    $("live-summary").replaceChildren(...items);
    $("cancel-button").hidden = runEnded(run);

    // A phase still pending or running when the run ended did not finish.
    const phaseStatus = (phase) => (!runEnded(run) ? phase.status
      : phase.status === "pending" ? "not reached" : phase.status === "running" ? "did not finish" : phase.status);
    // A run that ended in a phase with no failure of its own (for example a
    // ref that did not resolve) shows the run's reason on that phase.
    const phases = live.phases || [];
    let blamed = null;
    if (runEnded(run) && run.status !== "passed" && run.reason
        && !phases.some((phase) => phase.status === "failed" || phase.status === "error")
        && !live.items.some((item) => item.status !== "pending")) {
      blamed = phases.find((phase) => phase.status === "running" || phase.status === "pending") || null;
    }
    // A small "Transcript" button under each step's name opens its commands
    // and output above the log; greyed until the step has output.
    const transcriptButton = (tid, enabled, label) => el("button", {
      type: "button", className: "transcript-toggle", text: "Transcript",
      disabled: !enabled,
      "aria-expanded": enabled ? (live.shown === tid ? "true" : "false") : null,
      "aria-label": `Transcript of ${label}`,
      title: enabled ? null : "Available once this step starts",
      onclick: enabled ? () => showTranscript(tid) : null,
    });
    const nameCell = (label, button) => el("div", { className: "check-title" },
      el("span", { className: "check-name", text: label }), button);
    const estimateCell = (seconds) => el("span", {
      className: "hint check-estimate",
      text: seconds !== null && seconds !== undefined ? formatSeconds(seconds) : "",
      "aria-label": seconds !== null && seconds !== undefined ? `estimated runtime ${formatSeconds(seconds)}` : null,
    });
    const phaseRows = phases.map((phase) => {
      const shown = phase === blamed ? "failed" : phaseStatus(phase);
      const reason = phase === blamed ? run.reason : phase.reason;
      const tid = `phase:${phase.key}`;
      const open = live.shown === tid;
      return el("li", {
        className: `check check-phase check-${shown.replace(/ /g, "-")}${open ? " check-open" : ""}`,
        "aria-current": shown === "running" ? "step" : null,
      },
        statusBadge(shown),
        nameCell(phase.name, phase.key === "boards" ? null
          : transcriptButton(tid, live.transcripts.has(tid), phase.name)),
        estimateCell(null),
        reason ? el("span", { className: "reason", text: sentence(reason) }) : null);
    });
    $("checklist").replaceChildren(...phaseRows, ...live.items.map((item) => {
      const shown = itemStatus(item, run);
      const known = item.invocation_id
        || [...live.byInvocation.entries()].find(([, value]) => value === item)?.[0] || null;
      const started = item.status !== "pending";
      const id = known && started ? known : null;
      const open = id && live.shown === id;
      return el("li", {
        className: `check check-${shown.replace(/ /g, "-")}${started ? "" : " check-locked"}${open ? " check-open" : ""}`,
        "aria-current": shown === "running" ? "step" : null,
      },
        statusBadge(shown),
        nameCell(item.name, transcriptButton(id || `pending:${item.name}`, Boolean(id), item.name)),
        estimateCell(item.estimated_s),
        item.reason ? el("span", { className: "reason", text: sentence(item.reason) }) : null);
    }));
    if (!live.items.length) {
      $("checklist").append(el("li", {
        className: "check-note",
        text: "Tests appear here as they start (this run's plan is not in the controller's plans directory).",
      }));
    }
    followCurrent(live);
    renderTranscript(live);
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
    const finished = runEnded(run);
    const finishedText = () => {
      return `Took ${formatSeconds(elapsed)}`;
    };
    if (!known) {
      $("live-clock").textContent = finished ? finishedText() : `Elapsed ${formatSeconds(elapsed)}`;
      const missing = estimates.filter((value) => value === null || value === undefined).length;
      $("live-estimate").textContent = live.planned
        ? `No countdown: ${missing} of ${estimates.length} tests have no estimated duration, so only elapsed time is shown`
        : "No countdown: this run's plan is unknown here, so only elapsed time is shown";
      return;
    }
    const total = estimates.reduce((sum, value) => sum + value, 0);
    $("live-estimate").textContent = `Estimate ${formatSeconds(total)} from the catalog's per-test estimates; build and preflight not included`;
    const testsStart = live.testsStarted ? Date.parse(live.testsStarted) : NaN;
    const testsElapsed = Number.isNaN(testsStart) ? null : Math.max(0, (end - testsStart) / 1000);
    if (finished) {
      $("live-clock").textContent = finishedText();
    } else if (testsElapsed === null) {
      $("live-clock").textContent = `Elapsed ${formatSeconds(elapsed)}`;
    } else if (testsElapsed <= total) {
      $("live-clock").textContent = `${formatSeconds(total - testsElapsed)} remaining, estimated (elapsed ${formatSeconds(elapsed)})`;
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
    // Sorted by the chosen column (Started, newest first, by default); ties
    // fall back to newest first. Missing values sort last either way.
    const { key, dir } = state.historySort;
    const value = (run) => (key === "ref" ? refLabel(run) : key === "plan"
      ? `${run.plan ?? ""} / ${run.suite ?? ""}` : String(run[key] ?? ""));
    const runs = [...state.runs.values()].sort((a, b) => {
      const va = value(a); const vb = value(b);
      if (va !== vb) {
        if (!va) return 1;
        if (!vb) return -1;
        return dir * va.localeCompare(vb, undefined, { numeric: true });
      }
      return String(b.created_at).localeCompare(String(a.created_at)) || b.run_id.localeCompare(a.run_id);
    });
    for (const th of document.querySelectorAll("table.history th[data-sort]")) {
      th.setAttribute("aria-sort", th.dataset.sort === key ? (dir > 0 ? "ascending" : "descending") : "none");
    }
    const label = document.querySelector(`table.history th[data-sort="${key}"] .sort`);
    $("history-caption").textContent =
      `Runs, sorted by ${label ? label.textContent : key} (${dir > 0 ? "ascending" : "descending"})`;
    $("history-empty").hidden = runs.length > 0;
    $("history").replaceChildren(...runs.map((run) => {
      const actions = el("td", { className: "actions" },
        el("button", {
          type: "button", text: "View", "aria-label": `View run ${run.run_id}`,
          onclick: () => selectRun(run.run_id, true, runEnded(run)),
        }));
      if (run.report) {
        // Rendered in a new tab by report.html; the raw file is linked there.
        actions.append(" ", el("a", {
          className: "button-link",
          href: `report.html?run=${encodeURIComponent(run.run_id)}`, text: "Report",
          target: "_blank", rel: "noopener",
          "aria-label": `Report of run ${run.run_id} (opens in a new tab)`,
        }));
      }
      const open = state.openFailures === run.run_id;
      if (runEnded(run) && run.status !== "passed") {
        actions.append(" ", el("button", {
          type: "button", text: open ? "Hide failures" : "Failures",
          "aria-expanded": open ? "true" : "false",
          "aria-controls": `failures-${run.run_id}`,
          "aria-label": `${open ? "Hide the" : "Show the"} failure summary of run ${run.run_id}`,
          onclick: () => toggleFailures(run.run_id),
        }));
      }
      // The run ID is long; it is in the row's tooltip and the Watch label.
      const row = el("tr", { title: `Run ${run.run_id}` },
        el("td", { className: "time" }, timeElement(run.created_at)),
        el("td", { className: "time" }, timeElement(run.finished_at)),
        el("td", { text: refLabel(run) }),
        el("td", { text: `${run.plan ?? "–"} / ${run.suite ?? "–"}` }),
        el("td", { text: targetName(run.target) }),
        el("td", {}, statusBadge(run.status)),
        actions);
      if (!open) return [row];
      // The failure summary opens in a row of its own right under the run.
      const cell = el("td", { colSpan: "7", className: "failures-cell" });
      cell.append(state.failureNodes.get(run.run_id) || el("p", { className: "hint", text: "Loading…" }));
      return [row, el("tr", { id: `failures-${run.run_id}`, className: "failures-row" }, cell)];
    }).flat());
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

  async function toggleFailures(runId) {
    if (state.openFailures === runId) {
      state.openFailures = null;
      renderHistory();
      return;
    }
    state.openFailures = runId;
    state.failureNodes.delete(runId);
    renderHistory();
    let node;
    try {
      const data = await api("GET", runPath(runId));
      node = !data.failures.length
        ? el("p", { className: "hint", text: "No failed step recorded" })
        : el("ul", { className: "failures" }, ...data.failures.map((failure) =>
          el("li", { className: "failure" },
            statusBadge(failure.status), " ", el("strong", { text: failure.step }),
            failure.reason ? el("span", { className: "reason", text: sentence(failure.reason) }) : null,
            failure.log && failure.log.length
              ? el("pre", { className: "log failure-log", text: failure.log.join("\n") }) : null)));
    } catch (error) {
      node = el("p", { className: "hint", text: `Cannot load the run: ${error.message}` });
    }
    state.failureNodes.set(runId, node);
    if (state.openFailures === runId) renderHistory();
  }

  // ----- live socket -------------------------------------------------------

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    const socket = new WebSocket(`${scheme}${window.location.host}/ws/live`);
    state.socket = socket;
    socket.addEventListener("open", () => {
      state.retry = 1000;
      $("connection").className = "conn conn-up";
      $("connection").textContent = "Connected to the controller (live updates on)";
      loadRuns();
      if (state.selected) selectRun(state.selected, false, state.selectedPast);
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
      } else if (data.type === "target" && data.target) {
        state.targets.set(data.target.host, data.target);
        renderTargets();
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
      $("connection").textContent = `Disconnected from the controller; retrying in ${Math.round(wait / 1000)} s`;
      window.setTimeout(connect, wait);
    });
  }

  // Flash needs Build: disabled, with a note, while Build is unticked.
  function syncFlashBuild() {
    const build = $("build").checked;
    $("flash-build").disabled = !build;
    $("flash-build-note").hidden = build;
  }

  function start() {
    $("run-form").addEventListener("submit", submitRun);
    $("build").addEventListener("change", syncFlashBuild);
    syncFlashBuild();
    $("refs-reload").addEventListener("click", loadRefs);
    $("plan").addEventListener("change", renderSuites);
    $("suite").addEventListener("change", renderSuiteTests);
    $("cancel-button").addEventListener("click", cancelRun);
    renderTargetSelect();
    loadRefs();
    loadPlans();
    const linked = /^#run=(.+)$/.exec(window.location.hash);
    if (linked && RUN_ID.test(linked[1])) state.selected = linked[1];
    connect();
    window.setInterval(renderClock, 1000);
    // Only the user's own scrolling (wheel, touch, drag, keys) stops the
    // list following the running test; scrolls the page makes do not.
    const stopFollowing = () => {
      if (state.live) state.live.follow = false;
    };
    for (const type of ["wheel", "touchmove", "mousedown"]) {
      $("checklist-scroll").addEventListener(type, stopFollowing, { passive: true });
    }
    $("checklist-scroll").addEventListener("keydown", (event) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) stopFollowing();
    });
    $("transcript-close").addEventListener("click", () => {
      if (state.live) { state.live.shown = null; renderLive(); }
    });
    // Click a column header to sort by it; click again to reverse.
    for (const th of document.querySelectorAll("table.history th[data-sort]")) {
      th.querySelector(".sort").addEventListener("click", () => {
        const sortKey = th.dataset.sort;
        const current = state.historySort;
        const times = sortKey === "created_at" || sortKey === "finished_at";
        state.historySort = current.key === sortKey
          ? { key: sortKey, dir: -current.dir }
          : { key: sortKey, dir: times ? -1 : 1 };
        renderHistory();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", start);
})();
