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
    "did not finish": ["✗", "did not finish"],
    online: ["●", "online"],
    offline: ["○", "offline"],
    busy: ["◆", "busy"],
  };

  const state = {
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
        ? ["ok", when ? `on USB at registration, ${when}` : "on USB at registration"]
        : ["warn", "unknown (runner offline)"];
      boards = [el("ul", { className: "devices" }, ...serials.map((serial, index) =>
        el("li", { className: "device" },
          el("div", { className: "device-head" },
            el("span", { className: `dot dot-${state[0]}`, "aria-hidden": "true" }),
            el("span", { className: "device-name", text: `Ubertooth ${index + 1}` }),
            el("span", { className: "device-state", text: state[1] })),
          el("code", { className: "device-serial", title: "USB serial number", text: serial }))))];
    }
    return el("section", { className: "equipment", "aria-label": `Attached equipment on ${targetName(target.host)}` },
      el("h4", { text: "Attached equipment" }),
      el("div", { className: "equipment-group" },
        el("h5", { text: "Ubertooths (devices under test)" }), ...boards));
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
    };
  }

  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  async function selectRun(runId, scroll = true) {
    if (!RUN_ID.test(String(runId))) return;
    state.selected = runId;
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
      pushLog(`[... ${count} output lines left out; full output in ${String(details.output_file ?? "")} ...]`);
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
      pushLog(`=== ${kind.replace("_finished", "").toUpperCase()}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "run_started") {
      pushLog(`=== RUN ${record.run_id}: ${details.plan_name ?? ""} / ${details.suite ?? ""} on ${details.host ?? ""} ===`);
    } else if (kind === "run_finished") {
      pushLog(`=== RUN ${status}${details.failure_reason ? ": " + details.failure_reason : ""} ===`);
    }
    if (render) renderLive();
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
      ["Target", run.target ? targetName(run.target) : "–"],
      ["Started", timeElement(run.created_at)],
      ["Finished", timeElement(run.finished_at)],
    ];
    if (run.reason) fields.push(["Reason", run.reason, "summary-item summary-item-wide"]);
    // One <div> per term/description pair (valid in a <dl>), so the summary
    // can flow across the top of the card.
    $("live-summary").replaceChildren(...fields.map(([term, value, className]) =>
      el("div", { className: className || "summary-item" },
        el("dt", { text: term }), el("dd", {}, value))));
    $("cancel-button").hidden = run.status !== "running";

    $("checklist").replaceChildren(...live.items.map((item) => {
      const shown = itemStatus(item, run);
      return el("li", {
        className: `check check-${shown.replace(/ /g, "-")}`,
        "aria-current": shown === "running" ? "step" : null,
      },
        statusBadge(shown), " ", el("span", { className: "check-name", text: item.name }),
        item.estimated_s !== null && item.estimated_s !== undefined
          ? el("span", { className: "hint check-estimate", text: `~${formatSeconds(item.estimated_s)}`, title: "estimated duration" }) : null,
        item.reason ? el("span", { className: "reason", text: ` — ${item.reason}` }) : null);
    }));
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
    $("live-estimate").textContent = `Estimate ~${formatSeconds(total)} from the catalog's per-test estimates; build and preflight not included`;
    const testsStart = live.testsStarted ? Date.parse(live.testsStarted) : NaN;
    const testsElapsed = Number.isNaN(testsStart) ? null : Math.max(0, (end - testsStart) / 1000);
    if (run.status !== "running") {
      $("live-clock").textContent = `Finished after ${formatSeconds(elapsed)}`;
    } else if (testsElapsed === null) {
      $("live-clock").textContent = `Countdown starts with the first test (elapsed ${formatSeconds(elapsed)})`;
    } else if (testsElapsed <= total) {
      $("live-clock").textContent = `~${formatSeconds(total - testsElapsed)} remaining (elapsed ${formatSeconds(elapsed)})`;
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
      // The run ID is long; it is in the row's tooltip and the Watch label.
      return el("tr", { title: `Run ${run.run_id}` },
        el("td", { className: "time" }, timeElement(run.created_at)),
        el("td", { className: "time" }, timeElement(run.finished_at)),
        el("td", { text: refLabel(run) }),
        el("td", { text: `${run.plan ?? "–"} / ${run.suite ?? "–"}` }),
        el("td", { text: targetName(run.target) }),
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
        el("dt", { text: "Target" }), el("dd", { text: targetName(run.target) }),
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
      $("connection").textContent = "Connected to the controller (live updates on)";
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

  function start() {
    $("run-form").addEventListener("submit", submitRun);
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
  }

  document.addEventListener("DOMContentLoaded", start);
})();
