// Ubertooth harness GUI. Vanilla JavaScript, no build step, no other origin.
//
// Every string from the controller or a runner reaches the page through
// textContent or a fixed-prefix attribute; nothing is parsed as HTML.
"use strict";

(function () {
  const LOG_LINES = 200;
  const LOG_LINE_CHARS = 500;
  // The three build options beyond Build itself: the run's firmware image,
  // the alternate btbr image, and the Python tools. `need` and `require` are
  // the flags /api/plans sets on a test that leaves cases out without the
  // option, and on one that cannot run without it at all. `cost` is the
  // page's own note for the option, read once at start and kept as the first
  // sentence of the note; `chosen` is the operator's own tick, which a
  // suite's need never sets.
  const BUILD_OPTIONS = [
    {
      id: "with-firmware", what: "the run's firmware image",
      need: "needs_firmware_build", require: "requires_firmware_build",
      // run-suite builds this anyway when the run flashes a board.
      builtByFlash: true,
    },
    {
      id: "with-btbr", what: "the alternate btbr image",
      need: "needs_btbr_build", require: "requires_btbr_build",
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
    "not reached": ["–", "not reached"],
    // A host of a group whose run has been asked for and not yet accepted.
    starting: ["…", "starting"],
    "did not finish": ["✗", "did not finish"],
    online: ["●", "online"],
    offline: ["○", "offline"],
    busy: ["◆", "busy"],
  };
  // The statuses the group API reports with an underscore, as a reader reads
  // them. Everything else arrives as one word already.
  const STATUS_WORDS = {
    not_reached: "not reached",
    not_applicable: "not applicable",
  };
  // How often the group's own step rows are fetched again while it runs.
  // The pills' fill is animated from each cell's start time between
  // fetches, so this is about statuses changing, not about smoothness.
  const GROUP_POLL_MS = 1500;

  // The symbol beside a registered board's status word.
  const BOARD_ICON = {
    ready: ["●"],
    busy: ["◆"],
    unknown: ["?"],
    offline: ["○"],
  };

  const state = {
    targets: new Map(),
    // The Manage boards panel, when it is open: see manageState().
    manage: null,
    runs: new Map(),
    // group_id -> the group view the history row is drawn from.
    groups: new Map(),
    // The plan list fetched for no target at all: what the plan and suite
    // selects are filled from, so they work before a runner registers.
    // The history: the group IDs the loaded pages hold (the rows), the
    // filters those pages were asked for, the cursor the next page starts
    // after (null once the oldest group is on the page), the counts the last
    // page reported, and a token so a slower answer for filters already
    // left, or an older page asked for before them, cannot replace or be
    // appended to a newer one.
    historyIds: new Set(),
    historyFilters: { target: "", status: "", ref: "" },
    historyCursor: null,
    historyMatching: 0,
    historyTotal: 0,
    historyToken: 0,
    // Rows added since that page was fetched, and rows it counted that no
    // longer match: what the count line adds to and takes off the page's
    // own figure. The next page's figure is current, so both are cleared
    // whenever one arrives.
    historyAdded: new Set(),
    historyGone: new Set(),
    plans: [],
    // What each listed test is for, keyed by catalog name, as /api/plans
    // sends it once per response (#315). Empty until the plans load.
    testInfo: {},
    // The info button the open dialog was opened from, so Escape and Close
    // put focus back where it was.
    infoInvoker: null,
    // host -> that host's own plan list. The estimates are what that host's
    // finished runs measured, and `runs_on_target` says which steps it runs
    // at all, which is what makes a host eligible for the chosen suite.
    plansByHost: new Map(),
    // host -> why its list could not be read, so its row says that instead
    // of going on claiming to be loading.
    plansErrors: new Map(),
    // The hosts a load is under way for, and their platforms, so an update
    // that changes neither does not start another load; and a token, so a
    // slower answer for hosts already left cannot replace a newer list.
    plansWanted: "",
    plansPlatforms: "",
    plansToken: 0,
    // The hosts the operator has ticked, whether or not they are registered
    // now: a host that drops out and comes back keeps its tick.
    chosen: new Set(),
    // True while the only tick is the one the page made itself (the first
    // host to register), so it may be moved to a host that can run the
    // chosen suite. The operator's own tick is never moved.
    autoChosen: false,
    // Whether that first tick has been made at all. Without it a host the
    // operator unticked would be ticked again by the next heartbeat, so
    // they could never leave the set empty.
    autoTicked: false,
    // ref option value ("<kind>:<name>") -> {name, kind, commit} as listed
    refOptions: new Map(),
    selected: null, // group_id shown in the live view
    // Whether that ID came from a /ui/#run= fragment, which names one host's
    // run rather than its group.
    selectedIsRun: false,
    // run_id -> [{path, stream, bytes, lines, sha256, owner, checks}] as the
    // run recorded them, and the page of the file open in the viewer.
    files: new Map(),
    openFile: null, // {run_id, path, page}
    wantedFile: null, // a file named in the URL fragment, opened once loaded
    // The group on display: its id, the last detail fetched for it, one
    // transcript per host, and which host's transcript is on show.
    group: null,
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
    const known = STATUS_TEXT[statusWord(status)];
    const [icon, label] = known || ["•", String(status || "unknown")];
    const key = known ? statusWord(status).replace(/ /g, "-") : "unknown";
    return el("span", { className: `status status-${key}` },
      el("span", { "aria-hidden": "true", text: icon + " " }), label);
  }

  // One status as a reader reads it: the group API writes two of them with
  // an underscore, and nothing else here has to know that.
  function statusWord(status) {
    const text = String(status || "");
    return STATUS_WORDS[text] || text;
  }

  // The short name a per-host pill carries. A registry host is usually
  // `Host_Linux` or `Host_Mac`, and the pill is read beside its row, so the
  // prefix every host shares is dropped; the full name is in the pill's
  // own label and title.
  function hostLabel(host) {
    const name = String(host || "");
    return name.replace(/^Host[_-]/, "") || name;
  }

  // A board's status as the controller derived it (boardstatus.py): the word
  // it sent, a symbol, and a color from its level. The word is always
  // there, so the color only repeats what the text says.
  function boardBadge(board) {
    const status = String(board.status || "unknown");
    const [icon] = BOARD_ICON[status] || ["•"];
    const level = String(board.level || "attention");
    return el("span", { className: `status level-${level}` },
      el("span", { "aria-hidden": "true", text: icon + " " }), status);
  }

  function timeElement(iso) {
    if (!iso) return el("span", { text: "–" });
    const date = new Date(iso);
    const shown = Number.isNaN(date.getTime()) ? String(iso) : date.toLocaleString();
    return el("time", { datetime: String(iso), text: shown });
  }

  // How long a finished run or group took, in the words the controller gives
  // it (durations.human_duration), so the page, report.md, the email, and a
  // release's evidence all say the same thing. A running one has none: its
  // clock is live, and renderClock counts it against the estimate instead.
  function durationElement(record) {
    const text = record && record.duration_hms;
    if (!text) return el("span", { text: "–" });
    return el("span", { text: String(text) });
  }

  // What a finished group's clock says. The controller's own words for how
  // long it took, and this page's arithmetic only for a group whose record
  // carries none.
  function finishedAfter(group, elapsed) {
    return `Finished after ${(group && group.duration_hms) || formatSeconds(elapsed)}`;
  }

  // An estimate or a live clock, rounded to the second: a figure that is a
  // guess, or one that moves while you read it, has no business claiming
  // milliseconds. A measured duration that has ended says them, in the
  // controller's own words (durationElement).
  function formatSeconds(total) {
    const seconds = Math.max(0, Math.round(total));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const pad = (n) => String(n).padStart(2, "0");
    if (h) return `${h}h ${pad(m)}m ${pad(s)}s`;
    return m ? `${m}m ${pad(s)}s` : `${s}s`;
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
  const groupPath = (groupId) => `/api/run-groups/${encodeURIComponent(groupId)}`;

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
      const boards = Array.isArray(target.boards) ? target.boards : [];
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
            el("dt", { text: "Active run" }), el("dd", {}, active)),
          el("div", { className: "target-boards" },
            el("h4", { id: `boards-${target.host}`, text: "Boards" }),
            el("p", { className: "hint", text: target.boards_summary || "" }),
            boards.length
              ? el("ul", {
                  className: "boards",
                  "aria-labelledby": `boards-${target.host}`,
                }, ...boards.map(boardLine))
              : null,
            el("button", {
              type: "button", text: "Manage boards",
              "aria-label": `Manage the boards of ${target.host}`,
              onclick: () => openManage(target.host),
            }))));
    }));
    // The first runner to register is ticked once, so the page is ready to
    // run on one host without a choice; after that the ticks are the
    // operator's, including a deliberately empty set (autoTicked).
    if (!state.autoTicked && !state.chosen.size && targets.length) {
      state.chosen.add(targets[0].host);
      state.autoTicked = true;
      state.autoChosen = true;
    }
    renderHostChoices();
    maybeLoadPlans();
    // A newly registered target is a history filter choice too.
    renderHistoryChoices();
    if (state.manage) renderManage();
  }

  // One registered board: its status, its alias and serial, where its power
  // comes from, and the sentence saying why the status is what it is.
  function boardLine(board) {
    const power = board.power_controller
      ? `${board.power_controller} port ${board.power_port}`
      : "no switchable port";
    return el("li", { className: "board reg-board" },
      boardBadge(board),
      el("span", { className: "board-name", text: String(board.alias || "") }),
      el("code", { className: "board-serial", text: String(board.serial || "") }),
      el("span", { className: "board-power", text: power }),
      el("p", { className: "reason", text: String(board.status_detail || "") }));
  }

  // The hosts a plan listing belongs to, and the platforms it was judged
  // by: a change in either is a reload, and both are compared with what a
  // load is under way for, so an update while one is in flight does not
  // start another.
  const plansKey = () => groupPlanHosts().join(" ");
  const plansPlatformsKey = () =>
    groupPlanHosts().map((host) => String(targetPlatform(host))).join(" ");

  // Every host that is registered: a plan listing is fetched for each, so
  // the row of a host says what the chosen suite would do there before it
  // is ticked, and a host the suite runs nothing on can be held out of the
  // group rather than refused after Run.
  function groupPlanHosts() {
    return [...state.targets.keys()].sort();
  }

  function maybeLoadPlans() {
    if (plansKey() !== state.plansWanted
        || plansPlatformsKey() !== state.plansPlatforms) {
      loadPlans();
    }
  }

  function renderHostChoices() {
    const list = $("host-choices");
    const targets = [...state.targets.values()].sort((a, b) => a.host.localeCompare(b.host));
    // A host that is not registered now but is ticked (it dropped out while
    // chosen) keeps its row, so the tick is visible rather than silently
    // dropped; starting a group on it is refused by the controller.
    const names = [...new Set([...targets.map((t) => t.host), ...state.chosen])].sort();
    if (!names.length) {
      list.replaceChildren(el("li", { className: "empty", text: "No target runner has registered yet." }));
      $("hosts-status").textContent = "";
      syncRunButton();
      return;
    }
    list.replaceChildren(...names.map((host) => {
      const target = state.targets.get(host);
      const id = `host-${host}`;
      const blocked = hostRefusal(host);
      const box = el("input", {
        type: "checkbox", id, name: "hosts", value: host,
        checked: state.chosen.has(host),
        // A host the chosen suite does not run anything on cannot be part
        // of this group: the row says so instead of the run finding out.
        disabled: Boolean(blocked),
        "aria-describedby": `${id}-note`,
        onchange: () => {
          // The operator's own choice from here on: nothing ticks or
          // unticks a box for them again.
          state.autoChosen = false;
          if (box.checked) state.chosen.add(host);
          else state.chosen.delete(host);
          // Another host is another plan listing: its own estimates, and
          // its own answer to what the suite leaves out and must build.
          renderHostChoices();
          renderSuiteTests();
        },
      });
      return el("li", { className: blocked ? "host-choice host-choice-out" : "host-choice" },
        box,
        el("label", { for: id },
          el("span", { className: "host-choice-name", text: host }),
          target
            ? el("span", { className: "badges" }, statusBadge(target.status),
              target.active_run_id ? statusBadge("busy") : null,
              target.platform
                ? el("span", { className: "status status-platform", text: target.platform })
                : null)
            : el("span", { className: "badges" }, statusBadge("offline"))),
        el("p", { id: `${id}-note`, className: blocked ? "hint warn" : "hint", text: hostNote(host) }));
    }));
    $("hosts-status").textContent = chosenText();
    syncRunButton();
  }

  // The hosts the group would be started on: ticked, registered, and able to
  // run something of the chosen suite.
  function groupHosts() {
    return [...state.chosen].filter(
      (host) => state.targets.has(host) && !hostRefusal(host)).sort();
  }

  // Why this host cannot be part of a group of the chosen suite, or "". The
  // controller's own sentence where it has one, so pressing Run is never
  // how the operator finds out (#285).
  function hostRefusal(host) {
    const suite = suiteFor(host);
    if (!suite) return "";
    return suite.runnable_on_target === false
      ? String(suite.not_runnable_reason || "This host cannot run this suite.")
      : "";
  }

  // What this host would do with the chosen suite: how many of its tests
  // run there, what they are estimated to take, and what is left out.
  function hostNote(host) {
    if (!state.targets.has(host)) {
      return "Not registered now; its runner has to be connected to be part of a group.";
    }
    const failed = state.plansErrors.get(host);
    if (failed) {
      // Nothing is known about what the suite would do here, so the row
      // says that rather than a figure or a reason it has not got. The
      // next target update tries again (loadPlans).
      return `Could not read this host's plans: ${failed}. It is read again`
        + " as soon as the host reports in.";
    }
    const blocked = hostRefusal(host);
    if (blocked) return blocked;
    const suite = suiteFor(host);
    if (!suite) {
      return state.plansByHost.has(host)
        ? "" : "Reading this host's plans…";
    }
    const running = suite.tests.filter((test) => test.runs_on_target !== false);
    const left = (suite.omitted_tests || []).length;
    return `${running.length} of ${suite.tests.length} tests run here`
      + (suite.estimated_s === null || suite.estimated_s === undefined
        ? ", with no time estimate"
        : `, about ${formatSeconds(suite.estimated_s)}`)
      + (left ? `; ${left} not applicable here.` : ".");
  }

  function chosenText() {
    const hosts = groupHosts();
    if (!hosts.length) return "Tick at least one host to run on.";
    if (hosts.length === 1) {
      return `One host: ${hosts[0]}. A single-host run is a group of one.`;
    }
    return `${hosts.length} hosts: ${hosts.join(", ")}. Each gets its own run,`
      + " started together; the group passes only if all of them pass.";
  }

  // The registry platform a target registered with, or null for one that
  // has not registered (which is what /api/plans reports for it).
  function targetPlatform(host) {
    const target = host ? state.targets.get(host) : null;
    return target && target.platform ? target.platform : null;
  }

  // ----- manage boards -----------------------------------------------------

  // The panel's own state: which target it is open for, the last autodetect
  // answer, the power-port choices that answer offered, and the device being
  // registered. `returnFocus` is the button that opened it, so closing gives
  // the keyboard back where it came from.
  function manageState(host) {
    return {
      host, devices: null, error: "", controllers: [], suggested: "",
      ports: [], adding: null, busy: false, returnFocus: null,
    };
  }

  function openManage(host) {
    const reopened = state.manage && state.manage.host === host;
    state.manage = reopened ? state.manage : manageState(host);
    state.manage.returnFocus = document.activeElement;
    $("manage").hidden = false;
    renderManage();
    $("manage-heading").focus();
    if (!reopened) detectBoards();
  }

  function closeManage() {
    const back = state.manage && state.manage.returnFocus;
    state.manage = null;
    $("manage").hidden = true;
    $("add-board").hidden = true;
    // Back to the button that opened it, when that button is still on the
    // page; a target that deregistered meanwhile takes its button with it,
    // and then the focus is left where the browser has it.
    if (back && document.contains(back) && typeof back.focus === "function") {
      back.focus();
    }
  }

  function manageStatus(text, warn) {
    const node = $("manage-status");
    node.textContent = text;
    node.classList.toggle("warn", Boolean(warn));
  }

  function renderManage() {
    const manage = state.manage;
    if (!manage) return;
    const target = state.targets.get(manage.host);
    $("manage-about").textContent = target
      ? `${manage.host}: boards are held in that target's own registry, and `
        + "its runner is what reads and changes it. A change is refused while "
        + "the target has a run going, or while the board is leased."
      : `${manage.host} is no longer registered with the controller.`;
    const boards = target && Array.isArray(target.boards) ? target.boards : [];
    $("manage-registered-empty").hidden = boards.length > 0;
    $("manage-registered").replaceChildren(...boards.map((board) =>
      el("li", { className: "board reg-board" },
        boardBadge(board),
        el("span", { className: "board-name", text: String(board.alias || "") }),
        el("code", { className: "board-serial", text: String(board.serial || "") }),
        el("button", {
          type: "button", text: "Unregister",
          disabled: manage.busy,
          "aria-label": `Unregister ${board.alias} from ${manage.host}`,
          onclick: () => removeBoard(board.alias),
        }),
        el("p", { className: "reason", text: String(board.status_detail || "") })
      )));
    renderDetected();
  }

  function renderDetected() {
    const manage = state.manage;
    const devices = manage && Array.isArray(manage.devices) ? manage.devices : null;
    $("manage-detect").disabled = Boolean(manage && manage.busy);
    $("manage-detected-empty").hidden = devices !== null && devices.length > 0;
    if (devices === null) {
      $("manage-detected-empty").textContent =
        "Press Autodetect boards to list what is attached.";
    } else if (!devices.length) {
      $("manage-detected-empty").textContent = manage.error
        ? `No board could be listed: ${manage.error}`
        : "No Ubertooth is attached to this target.";
    }
    $("manage-detected-wrap").hidden = !devices || !devices.length;
    $("manage-detected").replaceChildren(...(devices || []).map((device) => {
      const serial = String(device.serial || "");
      const registered = device.alias
        ? el("span", {}, "registered as ", el("code", { text: String(device.alias) }))
        : el("span", { text: "new" });
      return el("tr", {},
        el("td", {}, el("code", { text: serial || "none reported" })),
        el("td", {}, el("code", { text: String(device.path || "") })),
        el("td", {}, el("code", {
          text: `${String(device.vendor || "")}:${String(device.product || "")}`,
        })),
        el("td", {}, registered),
        el("td", { className: "actions" }, device.alias
          ? null
          : el("button", {
              type: "button", text: "Add",
              disabled: manage.busy || !serial,
              "aria-label": serial
                ? `Register the board with serial ${serial}`
                : `The board at ${device.path} reports no serial and cannot be registered`,
              onclick: () => startAdd(device),
            })));
    }));
    if (devices && devices.some((device) => !device.serial)) {
      // A board sitting in its DFU bootloader reports no serial at all, so
      // there is nothing to register it by; say so rather than leaving a
      // disabled button to be puzzled over. Only when nothing else is being
      // said, because this runs on every render and a refusal must not be
      // written over by a note.
      if (!$("manage-status").textContent) {
        manageStatus(
          "A listed board reports no serial: it is sitting in its DFU "
          + "bootloader. Bring it back to its application firmware and "
          + "autodetect again.",
          true,
        );
      }
    }
  }

  async function detectBoards() {
    const manage = state.manage;
    if (!manage) return;
    manage.busy = true;
    manageStatus("Reading the target's USB bus…");
    renderDetected();
    try {
      const data = await api(
        "GET", `/api/targets/${encodeURIComponent(manage.host)}/devices`);
      if (!state.manage || state.manage.host !== manage.host) return;
      manage.devices = Array.isArray(data.devices) ? data.devices : [];
      manage.error = typeof data.error === "string" ? data.error : "";
      manage.controllers = Array.isArray(data.power_controllers)
        ? data.power_controllers : [];
      manage.suggested = data.suggested_alias || "";
      manageStatus(manage.error ? `Autodetect: ${manage.error}` : "", manage.error);
    } catch (error) {
      if (!state.manage || state.manage.host !== manage.host) return;
      manage.devices = manage.devices || [];
      manageStatus(`Autodetect failed: ${error.message}`, true);
    } finally {
      manage.busy = false;
      renderManage();
    }
  }

  function startAdd(device) {
    const manage = state.manage;
    if (!manage) return;
    manage.adding = device;
    const serial = String(device.serial || "");
    $("add-board-device").textContent =
      `Serial ${serial}, on ${device.path}, reporting `
      + `${device.vendor}:${device.product}.`;
    $("add-alias").value = manage.suggested || "";
    // One option per free port, plus no port at all: a port another board
    // already has is not offered, because the registry refuses two boards
    // on one port.
    manage.ports = [];
    for (const controller of manage.controllers) {
      for (const port of controller.ports || []) {
        manage.ports.push({ controller: controller.alias, port });
      }
    }
    $("add-power").replaceChildren(
      el("option", { value: "", text: "No switchable port" }),
      ...manage.ports.map((entry, index) => el("option", {
        value: String(index),
        text: `${entry.controller} port ${entry.port}`,
      })));
    $("add-board").hidden = false;
    manageStatus("");
    $("add-alias").focus();
  }

  function cancelAdd() {
    if (state.manage) state.manage.adding = null;
    $("add-board").hidden = true;
    $("manage-detect").focus();
  }

  async function submitAdd(submitted) {
    submitted.preventDefault();
    const manage = state.manage;
    if (!manage || !manage.adding || manage.busy) return;
    const chosen = $("add-power").value;
    const mapping = chosen === "" ? null : manage.ports[Number(chosen)];
    const body = {
      action: "add",
      alias: $("add-alias").value.trim(),
      serial: String(manage.adding.serial || ""),
      power_controller: mapping ? mapping.controller : null,
      power_port: mapping ? mapping.port : null,
    };
    await changeRegistry(body, `Registered ${body.alias}.`);
  }

  async function removeBoard(alias) {
    const manage = state.manage;
    if (!manage || manage.busy) return;
    await changeRegistry(
      { action: "remove", alias },
      `Unregistered ${alias}.`,
    );
  }

  async function changeRegistry(body, done) {
    const manage = state.manage;
    manage.busy = true;
    manageStatus("Asking the target to change its registry…");
    renderManage();
    try {
      await api(
        "POST", `/api/targets/${encodeURIComponent(manage.host)}/boards`, body);
      if (!state.manage || state.manage.host !== manage.host) return;
      manage.adding = null;
      $("add-board").hidden = true;
      manage.busy = false;
      // The target's board list came back with the change; what is attached
      // has changed too, so the listing is read again. The outcome is said
      // after it, so the listing's own message does not replace it.
      await detectBoards();
      if (state.manage === manage && !manage.error) manageStatus(done);
    } catch (error) {
      if (!state.manage || state.manage.host !== manage.host) return;
      manageStatus(error.message, true);
    } finally {
      if (state.manage === manage) {
        manage.busy = false;
        renderManage();
      }
    }
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
      // The listed refs are the history ref filter's suggestions too.
      renderHistoryChoices();
    } catch (error) {
      state.refOptions = new Map();
      select.replaceChildren(el("option", { value: "", text: "(refs unavailable)" }));
      status.textContent = `Cannot list refs: ${error.message}`;
    }
  }

  // The plan lists belong to the hosts the group would run on: the
  // estimates are what each host's own runs measured, and a step that
  // names other hosts, or another platform, is not run there, so it is
  // left out of that host's estimate and of the build options its suite
  // asks for. One list per ticked host, and one for no host at all, which
  // is what the plan and suite selects are filled from before any runner
  // has registered. Called at start and whenever the ticks change.
  async function loadPlans() {
    const hosts = groupPlanHosts();
    const token = ++state.plansToken;
    state.plansWanted = hosts.join(" ");
    state.plansPlatforms = hosts.map((host) => String(targetPlatform(host))).join(" ");
    const problems = [];
    const lists = await Promise.all([
      api("GET", "/api/plans").catch((error) => ({ error })),
      ...hosts.map((host) => api(
        "GET", `/api/plans?target=${encodeURIComponent(host)}`,
      ).then((data) => ({ host, data })).catch((error) => ({ host, error }))),
    ]);
    if (token !== state.plansToken) return;  // a later set of hosts owns these
    const [any, ...perHost] = lists;
    if (any.error) {
      state.plans = [];
      state.testInfo = {};
      problems.push(`Cannot list plans: ${any.error.message}`);
    } else {
      state.plans = any.plans;
      // The prose comes down with the plans, keyed by the catalog name the
      // rows carry, so the info dialog needs no request of its own.
      state.testInfo = any.test_information || {};
    }
    state.plansByHost = new Map();
    state.plansErrors = new Map();
    for (const answer of perHost) {
      if (answer.error) {
        // Kept per host as well as in the status line: that host's own row
        // is where a reader looks, and it must not go on saying it is still
        // reading the plans.
        state.plansErrors.set(answer.host, answer.error.message);
        problems.push(`Cannot list plans for ${answer.host}: ${answer.error.message}`);
        continue;
      }
      state.plansByHost.set(answer.host, answer.data.plans);
    }
    const select = $("plan");
    // What the operator chose is kept across a reload for other hosts.
    const current = select.value;
    // Only plans that load and are runnable can be started; a plan of
    // planned tests (runnable: false, e.g. backlog) is listed, not offered.
    const usable = state.plans.filter((plan) => !plan.error && plan.runnable === true);
    select.replaceChildren(...usable.map((plan) =>
      el("option", { value: plan.name, text: `${plan.name} — ${plan.title}` })));
    const broken = state.plans.filter((plan) => plan.error);
    const planned = state.plans.filter((plan) => !plan.error && plan.runnable !== true);
    if (broken.length) {
      problems.push(`Not loadable: ${broken.map((plan) => `${plan.name} (${plan.error})`).join("; ")}`);
    }
    if (planned.length) {
      problems.push(`Not runnable (planned tests): ${planned.map((plan) => plan.name).join(", ")}`);
    }
    // A failure from an earlier load is not this list's status.
    $("plan-status").textContent = problems.join(". ");
    // A host whose own list could not be read is tried again on the next
    // target update (a heartbeat is enough), by forgetting what this load
    // was for: nothing else would ask for that host again.
    if (state.plansErrors.size) state.plansWanted = "";
    const keep = [current, "fast"].find(
      (name) => name && usable.some((plan) => plan.name === name));
    if (keep) select.value = keep;
    // A reload for other hosts keeps the chosen suite as well.
    renderSuites(true);
    // A tick nobody chose, on a host that cannot run this suite, moves to
    // one that can: the page is ready to run without a choice, and the
    // operator's own ticks are never moved (autoChosen).
    if (state.autoChosen && !groupHosts().length) {
      const usable = groupPlanHosts().find((host) => !hostRefusal(host));
      if (usable) {
        state.chosen = new Set([usable]);
      }
    }
    renderHostChoices();
    if (state.group) renderChecklist();
  }

  // The chosen suite out of the plan list with no host, which is the same
  // plan file every host reads: the test names and what the suite is.
  function findSuite(planName, suiteName, plans = state.plans) {
    const plan = plans.find((item) => item.name === planName && !item.error);
    return plan ? plan.suites.find((suite) => suite.name === suiteName) || null : null;
  }

  // The chosen suite as one host's own plan list reports it: that host's
  // estimates, and which of the suite's steps it runs at all.
  function suiteFor(host) {
    const plans = state.plansByHost.get(host);
    return plans ? findSuite($("plan").value, $("suite").value, plans) : null;
  }

  // `keepChoice` is for a reload of the same plan (other hosts): the
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
    // One row per test, each with its own info button. The names used to be
    // one sentence of text, which left nowhere to put one (#315); the
    // estimate and the artifact note stay in the hint below them, which is
    // what the suite select describes itself by.
    $("suite-test-list").replaceChildren(...(suite ? suite.tests : []).map(
      (test) => el("li", {},
        el("span", { className: "test-name", text: test.name }),
        infoButton(test.name))));
    $("suite-tests").textContent = suite
      ? estimateText(suite) + artifactWarning(suite)
      : "";
    // This suite may need build options the last one did not, and some
    // ticked host may not be able to run it at all.
    syncBuildOptions();
    renderHostChoices();
  }

  // A group the controller would refuse is not offered: no host ticked, and
  // a suite none of the ticked hosts can run (each host's own reason is in
  // its row). Everything else about the bench is the run's to report:
  // hardware it has not got blocks that step and fails the run, and the
  // other tests still run (#265, #269).
  function syncRunButton() {
    const hosts = groupHosts();
    const ticked = [...state.chosen];
    let reason = "";
    if (!hosts.length) {
      reason = !ticked.length
        ? "Tick the host, or hosts, to run the plan on."
        : "No ticked host can run this suite: see each host's reason above.";
    }
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
    // The steps the group's hosts run, as each host's own needs_artifacts
    // counts them: a step no host of the group runs needs it no image. With
    // no host chosen yet, every step of the suite counts.
    const pairs = groupSuites();
    const views = pairs.length ? pairs.map(([, view]) => view) : [suite];
    const tests = [...new Set(views.flatMap((view) => view.tests
      .filter((test) => (test.needs_artifacts || []).length
        && test.runs_on_target !== false)
      .map((test) => test.name)))];
    if (!tests.length) return "";
    const artifacts = wanted.map((label) => `--artifact "${label}=PATH"`).join(" ");
    return ` This suite also needs ${wanted.join(", ")},`
      + ` ${wanted.length > 1 ? "images" : "an image"} no build makes,`
      + ` for ${tests.join(", ")}: run it on the target with run-suite`
      + ` ${artifacts}. A run started here builds first, and flashes whatever`
      + " it would flash, before it is refused without running a test.";
  }

  // The chosen suite as each host of the group reports it: that host's own
  // estimates and its own answer to what it runs.
  function groupSuites() {
    return groupHosts()
      .map((host) => [host, suiteFor(host)])
      .filter(([, suite]) => Boolean(suite));
  }

  // The time the group is expected to take: one line per host, counting only
  // the steps that host runs, and then the group's own figure, which is the
  // longest host's rather than the sum, because the hosts run at the same
  // time.
  function estimateText(suite) {
    const pairs = groupSuites();
    if (!pairs.length) {
      return suite.estimated_s === null || suite.estimated_s === undefined
        ? "No time estimate: no host is chosen yet."
        : `Estimated test time ${formatSeconds(suite.estimated_s)} from the`
          + " catalog's per-test estimates, before a host is chosen.";
    }
    const parts = pairs.map(([host, view]) => hostEstimateText(host, view));
    const seconds = pairs.map(([, view]) => view.estimated_s);
    if (pairs.length > 1) {
      parts.push(seconds.every((value) => value !== null && value !== undefined)
        ? `The group takes the longest host's time, about`
          + ` ${formatSeconds(Math.max(...seconds))}: its hosts run at the same time.`
        : "No countdown for the group: a host of it has no estimated duration.");
    }
    return parts.join(" ");
  }

  // What one host is expected to spend on the chosen suite, which counts
  // only the steps that host runs, and then what it does not run.
  // `learned` says the figure came from that host's own finished runs
  // rather than the catalog's hand-calibrated estimates.
  function hostEstimateText(host, suite) {
    const running = suite.tests.filter((test) => test.runs_on_target !== false);
    const parts = [];
    if (suite.estimated_s !== null && suite.estimated_s !== undefined) {
      const learned = running.filter((test) => test.estimate_source === "learned").length;
      const source = learned
        ? `${learned} of ${running.length} from this host's own finished runs, the rest from the catalog`
        : "from the catalog's per-test estimates";
      parts.push(`Estimated test time on ${host} ${formatSeconds(suite.estimated_s)}`
        + ` (${source}; build and preflight not included).`);
      if (suite.per_board_tests) {
        parts.push(`${suite.per_board_tests} test(s) run once per selected Ubertooth`
          + " and are counted once here; the live countdown counts every board the run selects.");
      }
    } else if (!running.length) {
      parts.push(`No test of this suite runs on ${host}, so there is nothing for it to run.`);
    } else {
      parts.push(`No time estimate on ${host}: ${suite.unestimated_tests} of the`
        + ` ${running.length} tests it would run have no estimated duration,`
        + " so the live view shows elapsed time only.");
    }
    const omittedNote = omittedText(host, suite);
    if (omittedNote) parts.push(omittedNote);
    return parts.join(" ");
  }

  // The steps a host does not run, because the plan names other hosts for
  // them or its platform does not run them. Named with the time they take
  // where they do run, so an estimate smaller than the whole suite's is
  // accounted for rather than just smaller.
  function omittedText(host, suite) {
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
    return `${omitted.length} test(s) of this suite are not applicable on ${host}`
      + `${adds} and are not counted above: ${named}.`;
  }

  async function submitRun(event) {
    event.preventDefault();
    const status = $("run-status");
    const hosts = groupHosts();
    const chosen = state.refOptions.get($("ref").value);
    const body = {
      // Every host of the group gets this same request, each as its own
      // child run (POST /api/run-groups).
      hosts,
      ref: chosen ? chosen.name : "",
      // The commit this page listed: the controller refuses the run if the
      // ref has moved since, and the runner runs exactly this commit.
      commit: chosen ? chosen.commit : null,
      plan: $("plan").value,
      suite: $("suite").value,
      tests: [],
      build: $("build").checked,
      // Flashing and the three extra build options apply only to a run that
      // builds; the controller refuses any extra without Build.
      flash_build: $("build").checked && $("flash-build").checked,
      with_firmware: $("build").checked && $("with-firmware").checked,
      with_btbr: $("build").checked && $("with-btbr").checked,
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
    if (!hosts.length || !body.ref || !body.plan || !body.suite) {
      status.textContent = "Choose a ref, plan, suite and at least one host first.";
      return;
    }
    const button = $("run-button");
    button.disabled = true;
    status.textContent = `Starting ${body.plan}/${body.suite} at ${refLabel(body)}`
      + ` on ${hosts.join(", ")}…`;
    try {
      const data = await api("POST", "/api/run-groups", body);
      upsertGroup(data.group);
      status.textContent = hosts.length > 1
        ? `Started ${data.group.hosts_started} of ${hosts.length} runs as group`
          + ` ${data.group.group_id}.`
        : `Started run ${data.group.members[0].run_id || data.group.group_id}.`;
      selectGroup(data.group.group_id);
    } catch (error) {
      status.textContent = `Not started: ${error.message}`;
      if (error.status === 409 && !error.message.startsWith("runner rejected the run")) {
        // The ref may have moved since this page listed it: list the refs
        // again so the next Run sends the current commit.
        await loadRefs();
        status.textContent = `Not started: ${error.message}. The ref list has been reloaded; check the ref and press Run again.`;
      }
    } finally {
      // Not simply enabled again: the ticked hosts may not be able to run
      // the suite, which holds the button.
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

  // ----- test information --------------------------------------------------
  //
  // Why a test exists, how it produces a known answer, what it drives, and
  // what a pass does not prove, beside the test's name wherever the page
  // lists one: Run setup, the live checklist, and a failure summary
  // (#315). The prose is ubertooth_harness/test_info.yaml, which
  // /api/plans sends once per response keyed by the catalog name the rows
  // carry; the facts the catalog owns -- the hardware roles, the arguments,
  // the estimate and the profile it was read at -- stay on the test row, so
  // the dialog shows both and neither has two sources.
  //
  // Nothing here can stop a run. A file the controller could not read, a
  // test with no entry, a plan listing that failed to load, and a name no
  // listing has all end at a dialog that says so; the Run button never
  // reads any of it.

  // The entry for a test, or one that says why there is none. The server
  // sends `available` false with its own reason for a missing or malformed
  // file; this adds the two cases only the page can be in.
  function testInfoFor(name) {
    const entry = state.testInfo[name];
    if (entry) return entry;
    return {
      available: false,
      unavailable_reason: state.plans.length
        ? "No plan the controller lists holds this test, so there is no"
          + " information for it here."
        : "The plan listing has not loaded, so no test information is"
          + " available yet.",
      purpose: "", theory: "", hardware: "", expected: "", limitations: "",
      related: [], documentation: null,
    };
  }

  // The catalog's own facts for this test, and the listing they were read
  // from. The roles the catalog binds and the arguments it accepts are the
  // same wherever a test is listed, but its estimate and the coverage
  // profile that estimate was read at are the *step's*: `fast` lists
  // Argument validation at smoke coverage and `release` at comprehensive,
  // so a figure from the wrong listing would be the wrong figure.
  //
  // So the caller says which listing it is asking about: the run's plan and
  // suite in the live checklist and in a failure summary, and Run setup's
  // own choice there. The chosen suite is the fallback, and then any
  // listing that holds the test, which is all there is for a run whose plan
  // the controller does not have; the Runtime line names whichever it used.
  function catalogRow(name, context) {
    const wanted = [];
    if (context && context.plan) wanted.push([context.plan, context.suite]);
    wanted.push([$("plan").value, $("suite").value]);
    for (const [plan, suite] of wanted) {
      const listing = findSuite(plan, suite);
      const row = listing && listing.tests.find((test) => test.name === name);
      if (row) return { row, plan, suite };
    }
    for (const plan of state.plans) {
      for (const suite of plan.suites || []) {
        const row = suite.tests.find((test) => test.name === name);
        if (row) return { row, plan: plan.name, suite: suite.name };
      }
    }
    return { row: null, plan: "", suite: "" };
  }

  // Every plan and suite that lists this test, so a longer or soak form of
  // it is visible from the dialog rather than only from the plan files. A
  // plan the GUI cannot start (runnable false, such as backlog) is marked,
  // because listing it there is not the same as being able to run it.
  function testAppearances(name) {
    const places = [];
    for (const plan of state.plans) {
      for (const suite of plan.suites || []) {
        if (!suite.tests.some((test) => test.name === name)) continue;
        places.push(`${plan.name} / ${suite.name}`
          + (plan.runnable === true ? "" : " (planned, not runnable)"));
      }
    }
    return places;
  }

  // What the catalog times this test at, and what this target has measured.
  // The catalog estimate and its profile are what the issue asks for; a
  // learned figure is said beside it rather than instead of it, so the two
  // are never confused. Both belong to one listing of the test, which the
  // line names, because another plan may list the same test at another
  // coverage and so another estimate (see `catalogRow`).
  function runtimeText(found) {
    const row = found.row;
    if (!row) {
      return "No plan the controller lists holds this test, so its estimate"
        + " and profile are not known here.";
    }
    const listing = `as ${found.plan} / ${found.suite} lists it`;
    const parts = [];
    const catalogSeconds = row.catalog_estimated_s;
    const profile = row.estimate_profile
      ? `, profile "${row.estimate_profile}"` : "";
    if (catalogSeconds === null || catalogSeconds === undefined) {
      parts.push(`The catalog gives it no duration estimate (${listing}), so`
        + " a run of it shows elapsed time only.");
    } else {
      parts.push(`Catalog estimate about ${formatSeconds(catalogSeconds)} per`
        + ` invocation (${listing}${profile}).`);
    }
    if (row.estimate_source === "learned"
        && row.estimated_s !== null && row.estimated_s !== undefined) {
      const samples = row.estimate_samples;
      parts.push(`Finished runs measure ${formatSeconds(row.estimated_s)}`
        + (typeof samples === "number" ? ` (${samples} sample(s)` : " (")
        + (row.estimate_scaled ? ", from its per-unit rate" : "")
        + "), which is what this page counts.");
    }
    if (row.per_board) {
      parts.push("It runs once for every Ubertooth the run selects, and the"
        + " estimate above is one of those invocations.");
    }
    return parts.join(" ");
  }

  // A link only where the server gave an https URL. The entries and the
  // documentation link are built by the controller from fixed text, so this
  // can only ever refuse something that is not what it says it is -- and
  // then the reference is still shown, as text.
  function safeHref(url) {
    const text = String(url || "");
    return text.startsWith("https://") ? text : "";
  }

  function infoSection(heading, ...children) {
    return el("section", { className: "info-section" },
      el("h3", { text: heading }), ...children);
  }

  // The hardware prose with the roles the catalog binds and the arguments
  // it accepts under it, named rather than described: what each role does
  // is the prose's job.
  function hardwareSection(info, row) {
    const roles = row && Array.isArray(row.hardware_roles) ? row.hardware_roles : [];
    const args = row && Array.isArray(row.arguments) ? row.arguments : [];
    return infoSection("Hardware and traffic",
      info.hardware ? el("p", { text: info.hardware }) : null,
      roles.length
        ? el("p", {
            className: "info-facts",
            text: "Catalog roles: " + roles
              .map((item) => `${item.role} (${item.device})`).join(", ") + ".",
          })
        : el("p", { className: "info-facts", text: "The catalog binds it no device." }),
      args.length
        ? el("p", {
            className: "info-facts",
            text: "Arguments a plan may set: " + args
              .map((item) => item.name + (item.required ? " (required)" : ""))
              .join(", ") + ".",
          })
        : null);
  }

  function relatedSection(info) {
    const related = Array.isArray(info.related) ? info.related : [];
    if (!related.length) return null;
    return infoSection("Related issues", el("ul", { className: "info-links-list" },
      ...related.map((item) => {
        const href = safeHref(item && item.url);
        const reference = String((item && item.reference) || "");
        return el("li", {}, href
          ? el("a", { href, target: "_blank", rel: "noreferrer noopener", text: reference })
          : el("span", { text: reference }));
      })));
  }

  // The button that opens it. A real button, so it is reachable by Tab and
  // works with a touch or a click; nothing here is behind a right-click.
  // `context` is the plan and suite whose listing of the test the dialog
  // should read its estimate from (`catalogRow`): the run's, where the
  // button sits beside a run's own step.
  function infoButton(name, context) {
    const label = `What the test ${name} is for`;
    return el("button", {
      type: "button", className: "info-button",
      "aria-label": label, title: label,
      onclick: (event) => openTestInfo(name, event.currentTarget, context),
    }, el("span", { "aria-hidden": "true", text: "i" }));
  }

  function openTestInfo(name, invoker, context) {
    const info = testInfoFor(name);
    const found = catalogRow(name, context);
    const row = found.row;
    const dialog = $("test-info");
    // Only a script can ask for a second one (a modal dialog blocks the
    // page), and showModal() on an open dialog throws.
    if (dialog.open) dialog.close();
    state.infoInvoker = invoker || null;
    $("test-info-title").textContent = name;
    // The dialog's description: the purpose when there is one, and
    // otherwise why there is none, which is a reason to read and not an
    // empty dialog.
    const purpose = $("test-info-purpose");
    purpose.className = info.available ? "info-purpose" : "info-purpose hint warn";
    purpose.textContent = info.available
      ? String(info.purpose || "")
      // One sentence, whether or not the reason came with a full stop.
      : String(info.unavailable_reason || "No information for this test")
        .replace(/[.\s]*$/, ".")
        + " What the catalog itself says about it is below.";
    const places = testAppearances(name);
    $("test-info-body").replaceChildren(...[
      info.available ? infoSection("Theory of operation",
        el("p", { text: info.theory })) : null,
      hardwareSection(info, row),
      info.available ? infoSection("Expected outcome",
        el("p", { text: info.expected })) : null,
      info.available ? infoSection("What a pass does not prove",
        el("p", { text: info.limitations })) : null,
      infoSection("Runtime", el("p", { text: runtimeText(found) })),
      places.length
        ? infoSection("Plans that list it",
            el("p", { text: places.join(", ") + "." }))
        : null,
      relatedSection(info),
    ].filter(Boolean));
    const documentation = info.documentation || null;
    const href = documentation ? safeHref(documentation.url) : "";
    const where = documentation
      ? `${String(documentation.path)}#${String(documentation.anchor || "")}`
      : "docs/tests.md";
    $("test-info-links").replaceChildren(
      href
        ? el("a", {
            className: "button-link", href,
            target: "_blank", rel: "noreferrer noopener",
            text: "Open full documentation",
          })
        : null,
      el("span", { className: "hint", text: href ? where : `The full account is ${where}.` }),
    );
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");  // no <dialog> support: shown in place
    // The heading, so what was opened is announced with the dialog's own
    // description; the page's failure summary heading is focused the same way.
    $("test-info-title").focus();
  }

  function closeTestInfo() {
    const dialog = $("test-info");
    if (typeof dialog.close === "function") dialog.close();  // fires `close`
    else {
      dialog.removeAttribute("open");
      restoreInfoFocus();
    }
  }

  // Focus back to the button the dialog was opened from, for Escape and
  // Close alike. A button that is no longer on the page (the checklist was
  // redrawn while the dialog was open) leaves focus where the browser puts
  // it rather than moving it somewhere unrelated.
  function restoreInfoFocus() {
    const invoker = state.infoInvoker;
    state.infoInvoker = null;
    if (invoker && invoker.isConnected) invoker.focus();
  }

  // ----- the live run group ------------------------------------------------
  //
  // A run on several hosts is a run group: one child run per host, started
  // together, each with its own runner, boards and leases (rungroups.py).
  // The controller presents every run as a group, a run of its own being a
  // group of one, so this view always follows a group.
  //
  // GET /api/run-groups/{id} is where the rows come from: one row per
  // build, flash, preflight and test step, with one cell per host that runs
  // that step and only those, which is what each per-host pill is drawn
  // from. The rows are never worked out here, so the pills cannot drift
  // from the run records; what is worked out here is each pill's fill,
  // from the cell's own start time and estimate, so it moves between
  // fetches. Each host's transcript is built from that host's records as
  // they arrive on the live socket.

  function newTranscript(host, runId) {
    return {
      host, run_id: runId, seen: new Set(), log: [], partial: "",
      stage: "waiting", boards: new Map(), queued: [], pairings: [],
      bootloaderExpected: "", targets: new Map(),
    };
  }

  function newGroupView(groupId) {
    return {
      group_id: groupId, detail: null, group: null, tab: null,
      // One fetch of the rows at a time, with a trailing one when records
      // arrived while it was in flight.
      fetching: false, again: false, fetchedAt: 0, timer: null,
      transcripts: new Map(),
      // The pills the clock still moves: one per cell that is running with
      // an estimate, as the last render drew them (tickFills).
      fills: [],
    };
  }

  const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

  // The group a run belongs to: the one it is a child of, or itself (the
  // controller presents such a run as a group of one whose ID is the run's).
  function groupIdOf(run) {
    return (run && (run.group_id || run.run_id)) || null;
  }

  // Watch the group a run is a host of, with that host's transcript open.
  // Target cards and older /ui/#run= links name a run, not a group.
  async function selectRun(runId, scroll = true) {
    if (!RUN_ID.test(String(runId))) return;
    let run = state.runs.get(runId);
    if (!run) {
      try {
        run = (await api("GET", runPath(runId))).run;
        upsertRun(run);
      } catch (error) {
        $("live-empty").hidden = false;
        $("live-empty").textContent = `Cannot load run ${runId}: ${error.message}`;
        return;
      }
    }
    selectGroup(groupIdOf(run), { tab: run.target, scroll });
  }

  async function selectGroup(groupId, { tab = null, scroll = true } = {}) {
    if (!RUN_ID.test(String(groupId))) return;
    state.selected = groupId;
    // The file open in the viewer belonged to the group being left.
    state.openFile = null;
    renderOutputFile();
    // A link to this view: /ui/#group=<group_id>.
    window.history.replaceState(null, "", `#group=${groupId}`);
    const wanted = state.wantedFile;
    state.wantedFile = null;
    if (state.group && state.group.timer) window.clearTimeout(state.group.timer);
    const view = state.group = newGroupView(groupId);
    view.tab = tab;
    $("live-empty").hidden = true;
    $("live").hidden = false;
    $("cancel-status").textContent = "";
    renderGroup();
    // Every host's own records come with the first fetch of the group, which
    // backfills a transcript for each host that has a run (and for a host
    // that gets one later).
    await refreshGroup();
    if (state.selected !== groupId) return;
    if (wanted && view.tab) {
      const member = memberOf(view, view.tab);
      if (member && member.run_id) await openOutputFile(member.run_id, wanted, 0);
    }
    if (scroll) $("live-heading").scrollIntoView({ block: "start" });
  }

  function memberOf(view, host) {
    const members = (view.detail && view.detail.members) || [];
    return members.find((member) => member.host === host) || null;
  }

  // The group's rows, members and progress, at most one fetch at a time and
  // at most one every GROUP_POLL_MS; a record that arrives during a fetch
  // leaves a trailing one behind it.
  //
  // A fetch never asks for another by itself: it stores what it read without
  // scheduling (``storeGroup``), so only a record or an update from the
  // stream makes the page ask again and a group nothing is happening to is
  // left alone.
  async function refreshGroup() {
    const view = state.group;
    if (!view) return;
    if (view.fetching) {
      view.again = true;
      return;
    }
    view.fetching = true;
    try {
      const detail = await api("GET", groupPath(view.group_id));
      if (state.group !== view) return;
      view.detail = detail;
      view.group = detail.group;
      storeGroup(detail.group);
      for (const member of detail.members) {
        if (member.run) storeRun(member.run);
        if (member.run_id && !view.transcripts.has(member.host)) {
          // This host's transcript, and its own records from the start: a
          // child accepted after an earlier fetch would otherwise have the
          // records that arrived before this moment in no transcript at all.
          view.transcripts.set(member.host, newTranscript(member.host, member.run_id));
          loadTranscript(view.group_id, member.host, member.run_id);
        }
      }
      if (!view.tab || !memberOf(view, view.tab)) {
        // The first host that is not getting on with it, so a failure is
        // the transcript on show; otherwise the first host of the group.
        const members = detail.members;
        const wrong = members.find((member) => !["passed", "running", "pending",
          "starting"].includes(statusWord(member.status)));
        view.tab = (wrong || members[0] || {}).host || null;
      }
      renderGroup();
    } catch (error) {
      if (state.group !== view) return;
      $("cancel-status").textContent = `Cannot load this run group: ${error.message}`;
    } finally {
      view.fetchedAt = Date.now();
      view.fetching = false;
      if (view.again) {
        view.again = false;
        scheduleRefresh();
      }
    }
  }

  // A record or a run update changed something a row may show: fetch the
  // rows again, no more often than GROUP_POLL_MS.
  function scheduleRefresh() {
    const view = state.group;
    if (!view || view.timer) return;
    const wait = Math.max(0, GROUP_POLL_MS - (Date.now() - view.fetchedAt));
    view.timer = window.setTimeout(() => {
      view.timer = null;
      if (state.group === view) refreshGroup();
    }, wait);
  }

  async function loadTranscript(groupId, host, runId) {
    try {
      const data = await api("GET", runPath(runId));
      const view = state.group;
      if (!view || view.group_id !== groupId) return;
      upsertRun(data.run);
      const transcript = view.transcripts.get(host) || newTranscript(host, runId);
      view.transcripts.set(host, transcript);
      // Records from the socket and from REST may interleave; the sequence
      // each record carries is what keeps one of them out twice.
      for (const record of data.events) addEvent(runId, record, false);
      renderGroup();
      await loadOutputFiles(runId);
    } catch (error) {
      // The transcript stays as the socket has it; the next select retries.
    }
  }

  // The transcript a run's records belong to, or null for a run this group
  // has nothing to do with (the live socket carries every run's records).
  function transcriptOf(runId) {
    const view = state.group;
    if (!view) return null;
    for (const transcript of view.transcripts.values()) {
      if (transcript.run_id === runId) return transcript;
    }
    return null;
  }

  function pushLog(transcript, line) {
    // Long lines are cut, and say so, so a reader is never left thinking a
    // sentence ended where the log stopped.
    const text = String(line);
    transcript.log.push(text.length > LOG_LINE_CHARS
      ? `${text.slice(0, LOG_LINE_CHARS - 4)} ...`
      : text);
    if (transcript.log.length > LOG_LINES) {
      transcript.log.splice(0, transcript.log.length - LOG_LINES);
    }
  }

  function flushPartial(transcript) {
    if (transcript.partial) {
      pushLog(transcript, transcript.partial);
      transcript.partial = "";
    }
  }

  // One record of one host's run. The rows and their pills come from the
  // group's own answer, so nothing here decides a status: this builds that
  // host's transcript, and asks for the rows again.
  function addEvent(runId, record, render = true) {
    const live = transcriptOf(runId);
    if (!live || !record || typeof record !== "object") return;
    if (live.seen.has(record.sequence)) return;
    live.seen.add(record.sequence);
    const details = record.details && typeof record.details === "object" ? record.details : {};
    const kind = String(record.event_type);
    const status = String(record.status || "");
    if (kind === "command_output") {
      const text = live.partial + String(details.text ?? "");
      const lines = text.split("\n");
      live.partial = lines.pop();
      for (const line of lines) pushLog(live, line);
    } else {
      flushPartial(live);
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
      pushLog(live, `[... ${count} output lines left out; full output in ${names} ...]`);
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

    if (kind === "libbtbb_pin" && status !== "passed") {
      // The libbtbb differs from the fork's install.sh pin, or was not compared.
      pushLog(live, `warning: ${String(details.message ?? "")}`);
    }
    if (kind === "dfu_part_table" && (status === "warning" || status === "unchecked")) {
      // The flash sizes a DFU read-back is checked against differ from the
      // fork's own part table, or the comparison stopped working. A match,
      // and a commit without a table, leave the report row to say so, as
      // the terminal stream does (events.py).
      pushLog(live, `warning: ${String(details.message ?? "")}`);
    }
    if (kind === "lease_queued") {
      // A device was free while this run waited, and still not its turn:
      // why a run can start late with the bench looking idle. The harness
      // sends the line the report shows.
      const text = String(details.message ?? "");
      if (text) {
        if (!live.queued.includes(text)) live.queued.push(text);
        pushLog(live, `note: ${text}`);
      }
    }
    if (kind === "lease_pairing") {
      // A device this run never named, held because the registry says it is
      // permanently wired to one the run did: why a programmer appears
      // among the leases of a run that asked for a board. The harness sends
      // the line the report shows.
      const text = String(details.message ?? "");
      if (text) {
        if (!live.pairings.includes(text)) live.pairings.push(text);
        pushLog(live, `note: ${text}`);
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
      if (status === "failed" && details.reason) {
        pushLog(live, `error: ${String(details.reason)}`);
      }
    }
    if (kind === "bootloader_stage_finished" && status === "skipped" && details.reason) {
      pushLog(live, `note: bootloader stage skipped: ${String(details.reason)}`);
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
      for (const line of warnings) pushLog(live, `warning: ${String(line)}`);
      for (const line of Array.isArray(details.notes) ? details.notes : []) {
        pushLog(live, `note: ${String(line)}`);
      }
      if (status === "failed" && details.reason) {
        pushLog(live, `error: ${String(details.reason)}`);
      }
    }
    if (kind === "test_started") {
      pushLog(live, `=== TEST ${details.test_name ?? ""} on ${details.host ?? ""} ===`);
    } else if (kind === "known_target") {
      // Which device the harness drove for this test, or that it had none;
      // the controller sends the summary the report shows. Kept by
      // invocation, so a row's cell can name it beside that part.
      const found = Array.isArray(details.targets) && details.targets.length > 0;
      live.targets.set(String(details.invocation_id ?? ""),
                       found ? String(details.summary ?? "") : "no known target");
    } else if (kind === "not_applicable") {
      // Left out before the run: the host named cannot run it, its platform
      // does not, or this bench has not got the hardware it binds (not a
      // skip and not a failure). Such a test has no cell for this host in
      // its row, which is what the pills say; the line records it.
      const name = String(details.test_name ?? "");
      const what = details.case_name ? `${name} / ${details.case_name}` : name;
      pushLog(live, `[not applicable on ${details.host ?? ""}] ${what}: ${details.reason ?? ""}`);
    } else if (kind === "blocked") {
      // Also left out before the run, but the hardware it is about is what
      // is missing, so the run fails: the group's own answer gives such a
      // test a step of its own, whose cell for this host reads blocked.
      pushLog(live, `[blocked on ${details.host ?? ""}] ${details.test_name ?? ""}: ${details.reason ?? ""}`);
    } else if (kind === "test_finished") {
      pushLog(live, `=== TEST ${details.test_name ?? ""}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "build_step_started") {
      pushLog(live, `+++ ${details.step_name || details.step_id || ""} +++`);
    } else if (kind === "build_step_finished") {
      // The step's own outcome: it may have accepted a command that exited
      // non-zero, so this is not the status of its last command.
      pushLog(live, `--- ${details.step_name || details.step_id || ""} EXIT: ${status}${details.reason ? ": " + details.reason : ""} ---`);
    } else if (kind === "build_finished" || kind === "preflight_finished") {
      pushLog(live, `=== ${kind.replace("_finished", "").toUpperCase()}: ${status}${details.reason ? ": " + details.reason : ""} ===`);
    } else if (kind === "run_started") {
      pushLog(live, `=== RUN ${record.run_id}: ${details.plan_name ?? ""} / ${details.suite ?? ""} on ${details.host ?? ""} ===`);
    } else if (kind === "run_finished") {
      pushLog(live, `=== RUN ${status}${details.failure_reason ? ": " + details.failure_reason : ""} ===`);
    }
    if (render) {
      renderTranscript();
      // The rows are the group's own answer, so a record that may have
      // moved a step asks for them again rather than being read here.
      if (STEP_RECORDS.has(kind)) scheduleRefresh();
    }
  }

  // The records that can move a step, and so a pill: every stage boundary,
  // the manifest the test rows come from, a test starting or ending, and the
  // two planning records that leave a test out or block it.
  const STEP_RECORDS = new Set([
    "manifest", "build_started", "build_finished", "build_flash_started",
    "build_flash_finished", "build_flash_board_started",
    "build_flash_board_finished", "preflight_started", "preflight_finished",
    "test_started", "test_finished", "not_applicable", "blocked",
    "run_started", "run_finished",
  ]);

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
      renderGroup();
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

  // One link per file this invocation saved. A file a check failed on is
  // marked, so a reader looking at a failed test knows which one to open.
  function fileList(runId, invocationId) {
    const files = invocationId ? filesOf(runId, invocationId) : [];
    if (!files.length) return null;
    return fileLinks(runId, files);
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

  // ----- drawing the group -------------------------------------------------

  function renderGroup() {
    const view = state.group;
    if (!view) return;
    const group = view.group;
    const progress = (view.detail && view.detail.progress) || null;
    const fields = [
      ["Group", el("code", { text: group && group.implicit === false
        ? group.group_id : (groupRunId(group) || view.group_id) })],
      ["Status", statusBadge(group ? group.status : "running")],
      ["Hosts", group ? hostsText(group) : "–"],
      ["Ref", group ? refLabel(group) : "–"],
      ["Plan / suite", group ? `${group.plan ?? "–"} / ${group.suite ?? "–"}` : "–"],
      ["Started", timeElement(group && group.created_at)],
      ["Finished", timeElement(group && group.finished_at)],
      ["Duration", durationElement(group)],
      ["Steps done", progress ? `${progress.completed} of ${progress.planned}` : "–"],
    ];
    if (group && group.cancel_requested) {
      fields.push(["Cancel asked for", group.cancel_reason || "yes"]);
    }
    $("live-summary").replaceChildren(...fields.flatMap(([term, value]) =>
      [el("dt", { text: term }), el("dd", {}, typeof value === "string"
        ? document.createTextNode(value) : value)]));
    $("cancel-button").hidden = !group || group.status !== "running";
    renderChecklist();
    renderHostTabs();
    renderTranscript();
    renderClock();
  }

  // The run of a group of one, which is what its own ID is.
  function groupRunId(group) {
    const member = group && group.members && group.members[0];
    return member ? member.run_id : null;
  }

  function hostsText(group) {
    const hosts = (group.members || []).map((member) => member.host);
    return hosts.length === 1 ? hosts[0]
      : `${hosts.join(", ")} (${group.hosts_started} of ${group.hosts_asked} started)`;
  }

  // One row per step, with one pill per host that runs that step and only
  // those: the rows and cells of GET /api/run-groups/{id}, drawn in the
  // order a run reaches them (build, flash, preflight, then the tests).
  function renderChecklist() {
    const view = state.group;
    const steps = (view && view.detail && view.detail.steps) || [];
    const note = $("checklist-note");
    const hosts = ((view && view.group && view.group.members) || [])
      .map((member) => member.host);
    // The fills of the pills this render draws, so the clock can move them
    // without the rows being rebuilt: a checklist rebuilt every second
    // would announce itself to a screen reader and take the focus out of
    // whatever pill or link a reader was on.
    if (view) view.fills = [];
    note.textContent = steps.length
      ? `One pill per host that runs the step, and only those: ${hosts.join(", ")}.`
        + " A test no host has reached yet has pending pills; a test a host"
        + " does not run has no pill for it."
      : "Nothing to show yet: each host's steps appear as its run starts and"
        + " its plan arrives.";
    $("checklist").replaceChildren(...steps.map((step) => {
      const shown = statusWord(step.status);
      // The row's cells in the group's own host order, which is the order
      // the hosts were asked for, rather than whatever order the answer's
      // own object keys come in.
      const cells = hosts.map((host) => step.hosts[host])
        .filter(Boolean)
        .concat(Object.values(step.hosts).filter((cell) => !hosts.includes(cell.host)));
      return el("li", {
        className: `check check-${shown.replace(/ /g, "-")}`,
        "aria-current": shown === "running" ? "step" : null,
      },
        // The name and its info button together, so the button stays beside
        // the name rather than drifting to the pills at the far right. Only
        // a test has information to show; build, flash and preflight are
        // stages, not tests. The dialog reads this group's own listing of
        // the test, which is the one whose coverage and estimate it uses.
        el("span", { className: "check-name" },
          el("span", { className: "check-name-text", text: step.name }),
          step.kind === "test"
            ? infoButton(step.name, {
                plan: view.group && view.group.plan,
                suite: view.group && view.group.suite,
              })
            : null),
        el("span", { className: "pills" },
          ...cells.map((cell) => stepPill(step, cell))),
        ...cells.filter((cell) => cell.reason).map((cell) => el("span", {
          className: "reason",
          text: `${cell.host}: ${cell.reason}`,
        })),
        ...cells.flatMap((cell) => cellFiles(cell)));
    }));
    const extra = [];
    for (const member of (view && view.detail ? view.detail.members : [])) {
      const owned = member.run_id ? runFiles(member.run_id) : [];
      if (owned.length) {
        extra.push(el("li", { className: "check-note" },
          `Build and preflight output on ${member.host}: `,
          fileLinks(member.run_id, owned)));
      }
      if (!member.run_id && member.error) {
        extra.push(el("li", { className: "check-note" },
          `No run on ${member.host}: ${member.error}`));
      }
    }
    if (extra.length) $("checklist").append(...extra);
  }

  // One per-host pill: the host, its status, and, while the step runs there,
  // a fill from the time it has taken against what it is estimated to take.
  // The fill is decorative, so the pill's own text still says the status and
  // the fill carries a progressbar role for a screen reader.
  function stepPill(step, cell) {
    const fill = cellFill(cell);
    const about = `${step.name} on ${cell.host}: ${statusWord(cell.status)}`
      + (cell.elapsed_hms ? `, took ${cell.elapsed_hms}` : "")
      + (cell.estimated_s ? `, estimated ${formatSeconds(cell.estimated_s)}` : "")
      + (cell.reason ? ` — ${cell.reason}` : "");
    const pill = hostPill(cell.host, cell.status, {
      title: about,
      // Each pill's own button opens that host's transcript of this step.
      label: `Transcript of ${cell.host}: ${about}`,
      onclick: () => showHost(cell.host),
      fill: fill ? { ...fill, what: `${step.name} on ${cell.host}` } : null,
    });
    if (fill && state.group) {
      // Followed for as long as this render stands, so the clock can move
      // this one pill (see tickFills).
      state.group.fills.push({ pill, cell, what: `${step.name} on ${cell.host}` });
    }
    return pill;
  }

  // Move the fill of every pill that is still running, from the clock
  // rather than the network, without touching anything else on the page.
  function tickFills() {
    const view = state.group;
    if (!view || !view.fills || !view.fills.length) return;
    for (const followed of view.fills) {
      const fill = cellFill(followed.cell);
      const node = followed.pill.querySelector(".pill-fill");
      if (!fill || !node) continue;
      setFill(followed.pill, node, { ...fill, what: followed.what });
    }
  }

  // The one place a fill's width and its value for a screen reader are set.
  function setFill(pill, node, fill) {
    node.setAttribute("aria-label", fill.what);
    node.setAttribute("aria-valuenow", String(fill.percent));
    node.setAttribute("aria-valuetext", fill.over
      ? "over the estimate" : `${fill.percent}% of the estimate`);
    // A width is never set from script: the page sets no style attribute at
    // all, so the fill is one of the stylesheet's own steps, named in a
    // data attribute.
    node.setAttribute("data-fill", String(Math.round(fill.percent / 5) * 5));
    pill.classList.toggle("pill-over", Boolean(fill.over));
  }

  // The pill both the checklist and the history list are drawn with: the
  // host's short name, the status as a symbol and a word, and a button that
  // opens that host's transcript.
  function hostPill(host, status, { title, label, onclick, fill = null } = {}) {
    const shown = statusWord(status);
    const known = STATUS_TEXT[shown];
    const [icon, word] = known || ["•", shown];
    // A status this page has no entry for is styled as unknown rather than
    // put into a class of its own, as statusBadge does with one.
    const kind = known ? shown.replace(/ /g, "-") : "unknown";
    const node = fill ? el("span", {
      className: "pill-fill", role: "progressbar",
      "aria-valuemin": "0", "aria-valuemax": "100",
    }) : null;
    const pill = el("span", {
      className: `pill pill-${kind}`,
      title: title || `${host}: ${shown}`,
    },
      node,
      el("button", {
        type: "button", className: "pill-text",
        "aria-label": label || `${host}: ${shown}`,
        onclick,
      },
        el("span", { className: "pill-host", text: hostLabel(host) }),
        el("span", { "aria-hidden": "true", text: icon }),
        el("span", { className: "pill-status", text: word })));
    if (node) setFill(pill, node, fill);
    return pill;
  }

  // How far a running step is through its estimate, or null when nothing can
  // be said: a step that has not started, has finished, or has no estimate.
  function cellFill(cell) {
    if (statusWord(cell.status) !== "running" || !cell.started_at) return null;
    const estimate = cell.estimated_s;
    if (!estimate) return null;
    const elapsed = Math.max(0, (Date.now() - Date.parse(cell.started_at)) / 1000);
    if (Number.isNaN(elapsed)) return null;
    return {
      percent: Math.min(100, Math.round((elapsed / estimate) * 100)),
      over: elapsed > estimate,
    };
  }

  // The output files each invocation of this cell saved, named with the host
  // and the invocation they belong to.
  function cellFiles(cell) {
    const view = state.group;
    const member = view ? memberOf(view, cell.host) : null;
    if (!member || !member.run_id) return [];
    const nodes = [];
    for (const item of cell.items || []) {
      const links = fileList(member.run_id, item.id);
      if (links) {
        nodes.push(el("span", { className: "reason", text: `${cell.host}:` }), links);
      }
      const known = transcriptTargets(cell.host).get(item.id);
      if (known) {
        nodes.push(el("span", {
          className: "reason check-target", text: `${cell.host}: ${known}`,
        }));
      }
    }
    return nodes;
  }

  function transcriptTargets(host) {
    const view = state.group;
    const transcript = view ? view.transcripts.get(host) : null;
    return transcript ? transcript.targets : new Map();
  }

  // One tab per host of the group, each opening that host's own transcript.
  // The tabs follow the ARIA tabs pattern: one stop in the tab order, and
  // the arrow keys move between hosts (onTabKey).
  function renderHostTabs() {
    const view = state.group;
    const members = (view && view.detail && view.detail.members) || [];
    $("host-tabs").replaceChildren(...members.map((member) => {
      const chosen = member.host === view.tab;
      const transcript = view.transcripts.get(member.host);
      return el("button", {
        type: "button", role: "tab", id: `host-tab-${member.host}`,
        className: chosen ? "host-tab host-tab-current" : "host-tab",
        "aria-selected": chosen ? "true" : "false",
        "aria-controls": "log",
        tabindex: chosen ? "0" : "-1",
        onclick: () => showHost(member.host),
      },
        el("span", { className: "host-tab-name", text: member.host }),
        " ", statusBadge(member.status),
        transcript && transcript.stage
          ? el("span", { className: "hint", text: transcript.stage }) : null);
    }));
  }

  // Arrow keys along the host tabs, as the tabs pattern asks: the tab that
  // becomes current is the one focus moves to, so a keyboard reader can
  // reach every host's transcript.
  function onTabKey(event) {
    const view = state.group;
    const members = (view && view.detail && view.detail.members) || [];
    if (!members.length) return;
    const hosts = members.map((member) => member.host);
    const at = Math.max(0, hosts.indexOf(view.tab));
    const steps = {
      ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1,
      Home: 0, End: hosts.length - 1,
    };
    if (!(event.key in steps)) return;
    event.preventDefault();
    const wanted = (steps[event.key] + hosts.length) % hosts.length;
    showHost(hosts[wanted], { focus: true });
  }

  function showHost(host, { focus = false } = {}) {
    const view = state.group;
    if (!view || !memberOf(view, host)) return;
    view.tab = host;
    renderGroup();
    if (focus) {
      const tab = $(`host-tab-${host}`);
      if (tab) tab.focus();
    } else {
      $("log-heading").scrollIntoView({ block: "nearest" });
    }
  }

  // The host whose transcript is on show: its log tail, its boards, and the
  // leases it waited for or held.
  function renderTranscript() {
    const view = state.group;
    if (!view) return;
    const transcript = view.tab ? view.transcripts.get(view.tab) : null;
    const member = view.tab ? memberOf(view, view.tab) : null;
    $("log").setAttribute("aria-labelledby",
      view.tab ? `host-tab-${view.tab}` : "log-heading");
    const boards = transcript ? [...transcript.boards.values()] : [];
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
    const expected = transcript ? transcript.bootloaderExpected : "";
    expectedNote.hidden = !expected || boards.length === 0;
    expectedNote.textContent = expected
      ? `This run expects bootloader ${expected}.` : "";

    // Devices that were free while this run waited for its turn, and the
    // command that had the turn: why a run can start late with the bench
    // looking idle. Hidden unless the run waited behind somebody.
    const queued = transcript ? transcript.queued : [];
    $("live-queued").hidden = queued.length === 0;
    $("queued").replaceChildren(...queued.map((text) => el("li", { text })));

    // Devices the run never named and holds because they are wired to ones
    // it did. Hidden on every bench that registers no such pairing.
    const pairings = transcript ? transcript.pairings : [];
    $("live-pairings").hidden = pairings.length === 0;
    $("pairings").replaceChildren(...pairings.map((text) => el("li", { text })));

    const logNode = $("log");
    const lines = transcript
      ? (transcript.partial ? [...transcript.log, transcript.partial] : transcript.log)
      : [];
    // Pinned to the end only for a reader who is already there, so a render
    // for something else (another host's record, a heartbeat) does not pull
    // somebody reading further up back to the bottom.
    const atEnd = logNode.scrollHeight - logNode.scrollTop - logNode.clientHeight < 40;
    logNode.textContent = lines.length ? lines.join("\n")
      : member && !member.run_id
        ? `No run on ${member.host}: ${member.error || "it was never started"}.`
        : "Nothing recorded yet.";
    if (atEnd) logNode.scrollTop = logNode.scrollHeight;
    $("host-files").textContent = member && member.run_id
      ? `Run ${member.run_id} on ${member.host}.` : "";
  }

  // The whole group's clock: elapsed against the longest host's estimate,
  // because the hosts run at the same time. Every cell of every host has to
  // have an estimate for there to be a countdown, as the group's own
  // progress figure requires (group_progress).
  function renderClock() {
    const view = state.group;
    if (!view) return;
    const group = view.group;
    if (!group || !group.created_at) {
      $("live-clock").textContent = "";
      renderTitleProgress(null);
      return;
    }
    const start = Date.parse(group.created_at);
    const end = group.finished_at ? Date.parse(group.finished_at) : Date.now();
    const elapsed = Math.max(0, (end - start) / 1000);
    const steps = (view.detail && view.detail.steps) || [];
    const cells = steps.flatMap((step) => Object.values(step.hosts));
    const perHost = new Map();
    let missing = 0;
    for (const cell of cells) {
      if (cell.estimated_s === null || cell.estimated_s === undefined) missing += 1;
      perHost.set(cell.host, (perHost.get(cell.host) || 0) + (cell.estimated_s || 0));
    }
    const total = perHost.size ? Math.max(...perHost.values()) : 0;
    renderTitleProgress(view.detail ? view.detail.progress : null);
    if (!cells.length || missing) {
      $("live-clock").textContent = group.status === "running"
        ? `Elapsed ${formatSeconds(elapsed)}`
        : finishedAfter(group, elapsed);
      $("live-estimate").textContent = !cells.length
        ? "No countdown yet: the hosts have not reported their steps."
        : `No countdown: ${missing} of ${cells.length} steps across the group`
          + " have no estimated duration, so only elapsed time is shown.";
      return;
    }
    $("live-estimate").textContent = `Estimate ${formatSeconds(total)}: the`
      + ` longest of the group's ${perHost.size} host(s), which run at the`
      + " same time.";
    if (group.status !== "running") {
      $("live-clock").textContent = finishedAfter(group, elapsed);
    } else if (elapsed <= total) {
      $("live-clock").textContent = `About ${formatSeconds(total - elapsed)} remaining`
        + ` (elapsed ${formatSeconds(elapsed)})`;
    } else {
      $("live-clock").textContent = `Over the estimate by ${formatSeconds(elapsed - total)}`
        + ` (elapsed ${formatSeconds(elapsed)})`;
    }
  }

  // The card's own title bar is the group's progress: the steps its hosts
  // have finished against the steps they are planned to take.
  function renderTitleProgress(progress) {
    const bar = $("group-progress");
    const text = $("group-progress-text");
    if (!progress || !progress.planned) {
      bar.hidden = true;
      text.textContent = "";
      return;
    }
    const percent = Math.min(100, Math.round(
      (progress.completed / progress.planned) * 100));
    bar.hidden = false;
    bar.setAttribute("aria-valuenow", String(percent));
    bar.setAttribute("aria-valuetext",
      `${progress.completed} of ${progress.planned} steps done`);
    bar.setAttribute("data-fill", String(Math.round(percent / 5) * 5));
    text.textContent = `${progress.completed} of ${progress.planned} steps done`;
  }

  async function cancelGroup() {
    const view = state.group;
    if (!view) return;
    const status = $("cancel-status");
    status.textContent = "Asking every host of the group to cancel…";
    try {
      const data = await api("POST", `${groupPath(view.group_id)}/cancel`,
                             { reason: "canceled from the web GUI" });
      upsertGroup(data.group);
      const hosts = (data.canceled || []).join(", ");
      status.textContent = `Cancel accepted for ${hosts || "no host"};`
        + " each run stops at its next safe point.";
      scheduleRefresh();
    } catch (error) {
      status.textContent = `Cancel refused: ${error.message}`;
    }
  }

  // ----- history -----------------------------------------------------------

  // The history is paged newest first: the latest HISTORY_PAGE groups, then a
  // page at a time behind the cursor the controller returns, so a bench with
  // months of runs neither sends nor draws all of them.
  const HISTORY_PAGE = 50;

  function chosenFilters() {
    return {
      target: $("history-target").value,
      status: $("history-status").value,
      ref: $("history-ref").value.trim(),
    };
  }

  function filtering() {
    const filters = state.historyFilters;
    return Boolean(filters.target || filters.status || filters.ref);
  }

  // The same three comparisons the controller makes, so a live update lands
  // in the rows exactly where the next page would have put it. A group
  // matches a target when it asked that host to run, which is the host the
  // row draws a pill for.
  function matchesFilters(group) {
    const filters = state.historyFilters;
    const hosts = group.hosts || [];
    if (filters.target && !hosts.includes(filters.target)) return false;
    if (filters.status && group.status !== filters.status) return false;
    if (filters.ref && group.ref !== filters.ref) return false;
    return true;
  }

  function historyQuery(cursor) {
    const query = new URLSearchParams({ limit: String(HISTORY_PAGE) });
    const filters = state.historyFilters;
    for (const name of ["target", "status", "ref"]) {
      if (filters[name]) query.set(name, filters[name]);
    }
    if (cursor) {
      query.set("before", cursor.created_at);
      // The group of that instant the page ended on: two groups stamped
      // alike are then neither repeated nor lost at a page boundary.
      if (cursor.group_id) query.set("before_group_id", cursor.group_id);
    }
    return `/api/run-groups?${query.toString()}`;
  }

  // One page into the rows. state.groups is the page's cache of group views
  // and state.runs of the member runs, which the live view reads too, so
  // nothing is dropped from either; the rows are state.historyIds, the
  // groups the loaded pages hold.
  function takeGroupPage(data, append) {
    if (!append) state.historyIds = new Set();
    for (const group of data.groups || []) {
      storeGroup(group);
      state.historyIds.add(group.group_id);
    }
    state.historyCursor = data.has_more
      ? { created_at: data.next_before, group_id: data.next_before_group_id }
      : null;
    state.historyMatching = Number.isInteger(data.matching)
      ? data.matching : state.historyIds.size;
    state.historyTotal = Number.isInteger(data.total)
      ? data.total : state.historyMatching;
    // This figure counts every stored group the filters match now, so it
    // already counts the rows added since the last page and leaves out the
    // ones that stopped matching.
    state.historyAdded = new Set();
    state.historyGone = new Set();
  }

  // A group the live stream reports, or one this page has just started: it
  // joins the rows when the filters match it, and leaves them when they no
  // longer do. A group read by ID (watching one, a #group= link) goes to the
  // cache through storeGroup and never makes a row of its own: the rows are
  // the pages the controller served.
  function joinOrLeaveRows(group) {
    const known = state.groups.has(group.group_id);
    if (matchesFilters(group)) {
      state.historyGone.delete(group.group_id);
      if (!state.historyIds.has(group.group_id)) {
        state.historyIds.add(group.group_id);
        // A group no page has held: it has just started, so it is newer than
        // any page, and it belongs at the top of the rows and in the count.
        if (!known) state.historyAdded.add(group.group_id);
      }
    } else if (state.historyIds.delete(group.group_id)) {
      // A group the filters no longer match (one that was running and has
      // finished, with "running" chosen) leaves the rows; its view stays in
      // the cache, so watching it still works.
      if (!state.historyAdded.delete(group.group_id)) {
        state.historyGone.add(group.group_id);
      }
    }
  }

  function upsertRun(run) {
    storeRun(run);
    // A run moved, so a cell of it may have: the rows are the group's own
    // answer, so they are asked for again rather than worked out here.
    if (state.group && transcriptOf((run || {}).run_id)) scheduleRefresh();
  }

  // What a run update changes on the page, without asking for anything.
  function storeRun(run) {
    if (!run || typeof run.run_id !== "string") return;
    state.runs.set(run.run_id, run);
    const view = state.group;
    if (view && transcriptOf(run.run_id)) renderGroup();
  }

  function upsertGroup(group) {
    if (group && typeof group.group_id === "string") joinOrLeaveRows(group);
    storeGroup(group);
    const view = state.group;
    if (view && group && view.group_id === group.group_id) {
      // A host of it may have gained a run, or ended one, which is a cell in
      // every row.
      scheduleRefresh();
    }
  }

  // What a group update changes on the page, without asking for anything:
  // this is also how a fetch stores what it just read, so reading the group
  // can never ask for it again (refreshGroup).
  function storeGroup(group) {
    if (!group || typeof group.group_id !== "string") return;
    state.groups.set(group.group_id, group);
    for (const member of group.members || []) {
      if (member.run) state.runs.set(member.run.run_id, member.run);
    }
    renderHistory();
    const view = state.group;
    if (view && view.group_id === group.group_id) {
      view.group = group;
      renderGroup();
    }
  }

  function renderHistory() {
    const groups = [...state.historyIds]
      .map((groupId) => state.groups.get(groupId))
      .filter(Boolean)
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))
        || b.group_id.localeCompare(a.group_id));
    $("history-empty").hidden = groups.length > 0;
    $("history-empty").textContent = filtering()
      ? "No run group matches these filters." : "No runs yet.";
    $("history").replaceChildren(...groups.map((group) => {
      const members = group.members || [];
      const actions = el("td", { className: "actions" },
        el("button", {
          type: "button", text: "Watch",
          "aria-label": `Watch run group ${group.group_id}`,
          onclick: () => selectGroup(group.group_id),
        }));
      for (const member of members) {
        if (member.run && member.run.report) {
          actions.append(" ", el("a", {
            className: "button-link",
            href: `${runPath(member.run_id)}/report.md`,
            text: members.length > 1 ? `report.md (${hostLabel(member.host)})` : "report.md",
            "aria-label": `report.md of ${member.host} in group ${group.group_id}`,
          }));
        }
      }
      if (group.status !== "running" && group.status !== "passed") {
        actions.append(" ", el("button", {
          type: "button", text: "Failures",
          "aria-label": `Failure summary of run group ${group.group_id}`,
          onclick: () => showFailures(group.group_id),
        }));
      }
      // One pill per host, each opening that host's own transcript, so a
      // group of two says which host did what without being opened.
      const hosts = el("td", { className: "host-cell" }, ...members.map((member) =>
        hostPill(member.host, member.status, {
          title: member.error || `${member.host}: ${statusWord(member.status)}`,
          label: `Watch ${member.host} in group ${group.group_id}`
            + (member.error ? ` — ${member.error}` : ""),
          onclick: () => selectGroup(group.group_id, { tab: member.host }),
        })));
      return el("tr", {},
        el("td", { className: "time" }, timeElement(group.created_at)),
        el("td", { className: "time" }, timeElement(group.finished_at)),
        el("td", { className: "time" }, durationElement(group)),
        el("td", {}, el("code", {
          text: group.implicit ? (groupRunId(group) || group.group_id)
            : `${group.group_id.slice(0, 12)}…`,
          title: group.group_id,
        })),
        el("td", { text: refLabel(group) }),
        el("td", { text: `${group.plan ?? "–"} / ${group.suite ?? "–"}` }),
        hosts,
        el("td", {}, statusBadge(group.status)),
        actions);
    }));
    renderHistoryCount(groups.length);
    renderHistoryChoices();
  }

  function renderHistoryCount(shown) {
    // The page's own figure, with the rows added since taken on and the
    // rows that stopped matching taken off, and never fewer groups than are
    // on display.
    const matching = Math.max(
      state.historyMatching + state.historyAdded.size - state.historyGone.size,
      shown,
    );
    const stored = Math.max(state.historyTotal + state.historyAdded.size, matching);
    $("history-older").hidden = !state.historyCursor;
    if (!shown) {
      $("history-count").textContent = "";
      return;
    }
    const sentences = [
      `Showing ${shown} of ${matching} run group${matching === 1 ? "" : "s"}`
      + (filtering() ? ` that match these filters, of ${stored} stored.` : "."),
    ];
    if (state.historyCursor) {
      sentences.push(`Load older adds up to ${HISTORY_PAGE} more, oldest last.`);
    }
    $("history-count").textContent = sentences.join(" ");
  }

  // The filter choices: every registered target and every host a loaded row
  // asked to run, and the listed refs beside the refs those rows name. Both
  // are rebuilt only when they change, since this runs on every render.
  function renderHistoryChoices() {
    const targets = new Set(state.targets.keys());
    const refs = new Set([...state.refOptions.values()].map((ref) => ref.name));
    for (const group of state.groups.values()) {
      for (const host of group.hosts || []) targets.add(host);
      if (group.ref) refs.add(group.ref);
    }
    const select = $("history-target");
    // A chosen target stays a choice even when no row or target names it,
    // or choosing it would clear the filter it is there to apply.
    if (select.value) targets.add(select.value);
    const hosts = [...targets].sort((a, b) => a.localeCompare(b));
    if (select.dataset.hosts !== hosts.join("\n")) {
      select.dataset.hosts = hosts.join("\n");
      const chosen = select.value;
      select.replaceChildren(
        el("option", { value: "", text: "Any target" }),
        ...hosts.map((host) => el("option", { value: host, text: host })));
      select.value = chosen;
    }
    const names = [...refs].sort((a, b) => a.localeCompare(b));
    const list = $("history-refs");
    if (list.dataset.refs !== names.join("\n")) {
      list.dataset.refs = names.join("\n");
      list.replaceChildren(...names.map((name) => el("option", { value: name })));
    }
  }

  // The newest page, for the filters now chosen: on connect, on reconnect,
  // and whenever the filters change.
  async function loadGroups() {
    state.historyFilters = chosenFilters();
    const token = ++state.historyToken;
    try {
      const data = await api("GET", historyQuery(null));
      // Filters have changed, or an older page has been asked for, since
      // this was sent: that request's answer is the page now.
      if (token !== state.historyToken) return;
      takeGroupPage(data, false);
      renderHistory();
    } catch (error) {
      if (token !== state.historyToken) return;
      $("history-empty").hidden = false;
      $("history-empty").textContent = `Cannot list run groups: ${error.message}`;
    }
  }

  async function loadOlderGroups() {
    const cursor = state.historyCursor;
    if (!cursor) return;
    const button = $("history-older");
    const hadFocus = document.activeElement === button;
    const token = ++state.historyToken;
    button.disabled = true;
    $("history-count").textContent = "Loading older run groups…";
    try {
      const data = await api("GET", historyQuery(cursor));
      // Never appended to rows the filters have changed under: that page
      // belongs to a query nobody is showing any more.
      if (token !== state.historyToken) return;
      takeGroupPage(data, true);
      renderHistory();
    } catch (error) {
      if (token === state.historyToken) {
        $("history-count").textContent = `Cannot load older run groups: ${error.message}`;
      }
    } finally {
      button.disabled = false;
    }
    // The button goes once the oldest group is on the page, so the keyboard
    // moves to the line that says so rather than back to the document.
    if (hadFocus) (state.historyCursor ? button : $("history-count")).focus();
  }

  // The failed steps of a whole group, host by host: each member's own
  // summary, exactly as a run on its own reports it.
  async function showFailures(groupId) {
    const box = $("failures");
    const list = $("failures-list");
    box.hidden = false;
    $("failures-heading").textContent = `Failure summary: ${groupId}`;
    list.replaceChildren(el("li", { text: "Loading…" }));
    try {
      const data = await api("GET", groupPath(groupId));
      const group = data.group;
      $("failures-run").replaceChildren(
        el("dt", { text: "Hosts" }), el("dd", { text: hostsText(group) }),
        el("dt", { text: "Ref" }), el("dd", { text: refLabel(group) }),
        el("dt", { text: "Plan / suite" }), el("dd", { text: `${group.plan ?? "–"} / ${group.suite ?? "–"}` }),
        el("dt", { text: "Status" }), el("dd", {}, statusBadge(group.status)));
      const items = [];
      for (const member of data.members) {
        if (!member.run_id) {
          items.push(el("li", { className: "failure" },
            statusBadge(member.status), " ",
            el("strong", { text: member.host }),
            el("span", { text: `: ${member.error || "no run was started"}` })));
          continue;
        }
        for (const failure of member.failures) {
          items.push(el("li", { className: "failure" },
            statusBadge(failure.status), " ",
            el("strong", { text: `${member.host} · ${failure.step}` }),
            // A test gets its information button here too, reading this
            // group's own listing of it; a build, flash or preflight step
            // is not a test and has none to show.
            state.testInfo[failure.step]
              ? infoButton(failure.step, { plan: group.plan, suite: group.suite })
              : null,
            failure.reason ? el("span", { text: `: ${failure.reason}` }) : null,
            failure.log && failure.log.length
              ? el("pre", { className: "log", text: failure.log.join("\n") }) : null));
        }
      }
      list.replaceChildren(...(items.length ? items
        : [el("li", { text: "No failed step recorded." })]));
    } catch (error) {
      list.replaceChildren(el("li", { text: `Cannot load the run group: ${error.message}` }));
    }
    $("failures-heading").focus();
  }

  // ----- live socket -------------------------------------------------------
  //
  // One stream, unfiltered: every target, run and group update, and every
  // run's own records. The controller also offers ?group_id=ID, which
  // carries one group and nothing else, but the history list is live too,
  // so this page wants the lot and keeps the records of runs that are not
  // the group on show out itself (transcriptOf).

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    const socket = new WebSocket(`${scheme}${window.location.host}/ws/live`);
    state.socket = socket;
    socket.addEventListener("open", () => {
      state.retry = 1000;
      $("connection").className = "conn conn-up";
      $("connection").textContent = "Connected to the controller (live updates on).";
      loadGroups();
      if (state.selected) {
        // A fragment may name a run rather than its group; a reconnect
        // re-reads the group on show without scrolling the page again.
        const wanted = state.selected;
        if (state.selectedIsRun) {
          state.selectedIsRun = false;
          selectRun(wanted, false);
        } else {
          selectGroup(wanted, { scroll: false });
        }
      }
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
        renderGroup();
      } else if (data.type === "target" && data.target) {
        state.targets.set(data.target.host, data.target);
        renderTargets();
        renderGroup();
      } else if (data.type === "run") {
        upsertRun(data.run);
      } else if (data.type === "group") {
        upsertGroup(data.group);
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
  // /api/plans counts the suite's own flags, over every host of the group:
  // a step no host of it runs must not make it build, and a step one host
  // runs must.
  function neededBy(suite, need) {
    if (!suite) return [];
    const pairs = groupSuites();
    const views = pairs.length ? pairs.map(([, view]) => view) : [suite];
    return [...new Set(views.flatMap((view) => view.tests
      .filter((test) => test[need] && test.runs_on_target !== false)
      .map((test) => test.name)))];
  }

  // Flash and the three build options need Build: each is disabled, with a
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
    $("build").addEventListener("change", syncBuildOptions);
    // Flashing builds the firmware image anyway, which the note says.
    $("flash-build").addEventListener("change", syncBuildOptions);
    $("bootloader").addEventListener("change", syncBuildOptions);
    syncBuildOptions();
    $("refs-reload").addEventListener("click", loadRefs);
    $("plan").addEventListener("change", () => renderSuites());
    $("suite").addEventListener("change", renderSuiteTests);
    $("cancel-button").addEventListener("click", cancelGroup);
    // The history filters: a change applies at once, and the form's own
    // submit (Enter in the ref box, or Apply) does the same rather than
    // navigating away.
    $("history-filters").addEventListener("submit", (submit) => {
      submit.preventDefault();
      loadGroups();
    });
    $("history-target").addEventListener("change", loadGroups);
    $("history-status").addEventListener("change", loadGroups);
    // A ref picked from the suggestions, or typed and left, applies too.
    $("history-ref").addEventListener("change", loadGroups);
    $("history-clear").addEventListener("click", () => {
      $("history-target").value = "";
      $("history-status").value = "";
      $("history-ref").value = "";
      loadGroups();
      $("history-target").focus();
    });
    $("history-older").addEventListener("click", loadOlderGroups);
    // The information dialog: Close and Escape both close it, and focus
    // goes back to the button it was opened from however it closed.
    $("test-info-close").addEventListener("click", closeTestInfo);
    $("test-info").addEventListener("close", restoreInfoFocus);
    $("test-info").addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeTestInfo();
      }
    });
    $("manage-close").addEventListener("click", closeManage);
    $("manage-detect").addEventListener("click", detectBoards);
    $("add-board").addEventListener("submit", submitAdd);
    $("add-cancel").addEventListener("click", cancelAdd);
    // Escape closes the panel, as it does any other transient one.
    $("manage").addEventListener("keydown", (pressed) => {
      if (pressed.key === "Escape") closeManage();
    });
    $("file-previous").addEventListener("click", () => pageOutputFile(-1));
    $("file-next").addEventListener("click", () => pageOutputFile(1));
    $("file-close").addEventListener("click", () => {
      state.openFile = null;
      renderOutputFile();
    });
    // Each host of the group gets its own tick; ticking one is another plan
    // listing, with that host's own estimates and its own answer to what
    // the suite leaves out and what it must build (renderHostChoices).
    renderHostChoices();
    loadRefs();
    loadPlans();
    // /ui/#group=<group_id> shows that group, /ui/#run=<run_id> the group
    // that run is a host of; &file=<path> also opens that saved file in the
    // viewer, so a file view can be linked to and reopened.
    const linked = /^#(group|run)=([^&]+)(?:&file=(.+))?$/.exec(window.location.hash);
    if (linked && RUN_ID.test(linked[2])) {
      state.selected = linked[2];
      state.selectedIsRun = linked[1] === "run";
      if (linked[3]) state.wantedFile = decodeURIComponent(linked[3]);
    }
    connect();
    window.setInterval(renderClock, 1000);
    // The pills' fill moves with the clock: each running cell's share of
    // its estimate is read from the time it started, so nothing is fetched
    // and no row is rebuilt (which would announce the checklist to a screen
    // reader, and take the focus off a pill, once a second).
    window.setInterval(tickFills, 1000);
    $("host-tabs").addEventListener("keydown", onTabKey);
  }

  document.addEventListener("DOMContentLoaded", start);
})();
