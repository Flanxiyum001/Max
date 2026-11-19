"use strict";
(() => {
  const get = (id) => document.getElementById(id);
  const pages = [...document.querySelectorAll("[data-page]")];
  const navButtons = [...document.querySelectorAll("[data-nav]")];
  const names = { home: "Overview", tasks: "Task queue", trust: "Trust centre", system: "System" };
  const validTaskId = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  let token = null;
  let generation = 0;
  let tasks = [];
  let approvals = [];
  let selectedTask = null;
  let pendingCreate = null;
  let busy = false;
  let micStream = null;
  let micPointer = null;
  let micGeneration = 0;

  const clean = (value, length = 180) => typeof value === "string" ? value.slice(0, length) : "";
  function element(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = value;
    return result;
  }
  function empty(target, message) {
    target.replaceChildren(element("div", "empty-state", message));
  }
  function timeLabel(value) {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString() : "Time unavailable";
  }
  function flash(message) {
    get("command-hint").textContent = message;
  }
  function show(name) {
    if (!Object.hasOwn(names, name)) return;
    for (const page of pages) page.hidden = page.dataset.page !== name;
    for (const button of navButtons) {
      const active = button.dataset.nav === name;
      button.classList.toggle("selected", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    get("breadcrumb").textContent = names[name];
  }
  for (const button of document.querySelectorAll("[data-goto]")) {
    button.addEventListener("click", () => show(button.dataset.goto));
  }
  for (const button of navButtons) {
    button.addEventListener("click", () => show(button.dataset.nav));
  }

  function disconnect(message = "Session disconnected.") {
    generation += 1;
    token = null;
    tasks = [];
    approvals = [];
    selectedTask = null;
    pendingCreate = null;
    busy = false;
    get("owner-token").value = "";
    get("connect-panel").hidden = false;
    get("connect-toggle").textContent = "Connect Core";
    get("core-indicator").classList.remove("connected");
    get("core-indicator").lastChild.textContent = " Disconnected";
    get("stat-connection").textContent = "OFFLINE";
    get("stat-connection-note").textContent = "Authentication required";
    get("stat-tasks").textContent = "—";
    get("stat-approvals").textContent = "—";
    get("task-submit").disabled = true;
    get("connection-message").textContent = message;
    get("task-message").textContent = "";
    get("timeline-hint").textContent = "Select a task to inspect read-only event metadata.";
    empty(get("home-tasks"), "Connect Core to view tracked tasks.");
    empty(get("task-list"), "Connect Core to load tasks.");
    empty(get("home-trust"), "Pending requests appear only after authentication.");
    empty(get("trust-list"), "Connect Core to view pending Trust requests.");
    empty(get("task-timeline"), "No task selected.");
  }
  async function api(path, options = {}) {
    const current = token;
    if (!current) throw new Error("not_connected");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(path, {
        method: options.method || "GET",
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        headers: {
          Accept: "application/json",
          Authorization: "Bearer " + current,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...(options.key ? { "Idempotency-Key": options.key } : {})
        },
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal
      });
      if (response.status === 401) {
        if (current === token) disconnect("Local owner token rejected or expired.");
        throw new Error("unauthorized");
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      if (!(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("invalid_response");
      }
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }
  function errorLabel(error) {
    if (error instanceof Error && error.message === "unauthorized") return "Session ended. Reconnect.";
    if (error instanceof Error && error.message === "not_connected") return "Connect Core first.";
    if (error instanceof Error && /^HTTP \d{3}$/.test(error.message)) {
      return error.message === "HTTP 503" ? "Core storage/service unavailable." : "Core request failed (" + error.message + ").";
    }
    return "Core unreachable or response invalid. Check local service.";
  }
  function renderTasks(target, values, limit) {
    target.replaceChildren();
    if (!values.length) {
      empty(target, "No tracked tasks available.");
      return;
    }
    for (const task of values.slice(0, limit)) {
      if (!task || !validTaskId.test(clean(task.id, 40))) continue;
      const button = element("button", "task-item");
      button.type = "button";
      const left = element("div");
      left.append(element("strong", "", clean(task.objective, 250) || "Untitled task"));
      left.append(element("small", "", timeLabel(task.created_at) + " • " + clean(task.id, 36).slice(0, 8)));
      const state = element("span", "status-pill", clean(task.state, 36) || "unknown");
      button.append(left, state);
      button.addEventListener("click", () => selectTask(task.id));
      target.append(button);
    }
    if (!target.childElementCount) empty(target, "No valid task records returned.");
  }
  function renderApprovals(target, values, limit) {
    target.replaceChildren();
    if (!values.length) {
      empty(target, "No pending Trust requests.");
      return;
    }
    for (const entry of values.slice(0, limit)) {
      if (!entry || typeof entry.tool_name !== "string") continue;
      const box = element("article", "trust-item");
      box.append(element("small", "", "WAITING FOR SEPARATE OWNER REVIEW"));
      box.append(element("strong", "", clean(entry.tool_name, 80)));
      box.append(element("p", "", "State: " + clean(entry.state, 32) + " • Expires " + timeLabel(entry.expires_at)));
      target.append(box);
    }
    if (!target.childElementCount) empty(target, "No valid pending Trust records.");
  }
  async function loadTasks() {
    const epoch = generation;
    try {
      const data = await api("/v1/tasks");
      if (epoch !== generation) return;
      if (!Array.isArray(data) || data.length > 100) throw new Error("invalid_response");
      tasks = data.filter((x) => x && typeof x.id === "string" && typeof x.objective === "string");
      get("stat-tasks").textContent = String(tasks.length);
      renderTasks(get("home-tasks"), tasks, 3);
      renderTasks(get("task-list"), tasks, 30);
    } catch (error) {
      if (epoch !== generation) return;
      tasks = [];
      get("stat-tasks").textContent = "—";
      empty(get("home-tasks"), errorLabel(error));
      empty(get("task-list"), errorLabel(error));
    }
  }
  async function loadApprovals() {
    const epoch = generation;
    try {
      const data = await api("/v1/approvals");
      if (epoch !== generation) return;
      if (!Array.isArray(data) || data.length > 100) throw new Error("invalid_response");
      approvals = data.filter((x) => x && typeof x.tool_name === "string");
      get("stat-approvals").textContent = String(approvals.length);
      renderApprovals(get("home-trust"), approvals, 3);
      renderApprovals(get("trust-list"), approvals, 50);
    } catch (error) {
      if (epoch !== generation) return;
      approvals = [];
      get("stat-approvals").textContent = "—";
      empty(get("home-trust"), errorLabel(error));
      empty(get("trust-list"), errorLabel(error));
    }
  }
  async function health() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/health/ready", {
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        headers: { Accept: "application/json" },
        signal: controller.signal
      });
      if (!response.ok) throw new Error("not_ready");
      const body = await response.json();
      if (body.status !== "ready") throw new Error("not_ready");
      const scope = body.scope === "durable-task-store" ? "Durable store ready" : "Bootstrap only";
      get("system-health").textContent = scope;
      get("system-detail").textContent = body.scope === "durable-task-store"
        ? "Core and task database are responding. This does not establish model, tool or device readiness."
        : "The local Core responds but PostgreSQL-backed task and Trust features are not configured.";
    } catch (_) {
      get("system-health").textContent = "Core not ready";
      get("system-detail").textContent = "Local service unavailable or required storage is not ready.";
    } finally {
      clearTimeout(timeout);
    }
  }
  async function refresh() {
    if (!token || document.hidden) return;
    await Promise.all([loadTasks(), loadApprovals(), health()]);
  }
  async function selectTask(id) {
    if (!token || !validTaskId.test(id)) return;
    selectedTask = id;
    show("tasks");
    const epoch = generation;
    get("timeline-hint").textContent = "Task " + id.slice(0, 8) + " • Journal metadata only; not external-effect proof.";
    empty(get("task-timeline"), "Loading task timeline…");
    try {
      const data = await api("/v1/tasks/" + id + "/events?limit=20");
      if (epoch !== generation || selectedTask !== id) return;
      if (!data || !Array.isArray(data.events) || data.events.length > 20) {
        throw new Error("invalid_response");
      }
      const target = get("task-timeline");
      target.replaceChildren();
      for (const event of data.events) {
        const box = element("article", "event-item");
        box.append(element("strong", "", clean(event.kind, 90) || "Event"));
        box.append(element("small", "", "Revision " + String(Number.isInteger(event.task_revision) ? event.task_revision : "—") + " • " + timeLabel(event.created_at)));
        target.append(box);
      }
      if (!target.childElementCount) empty(target, "No journal entries for this snapshot.");
      if (data.next_cursor) {
        target.append(element("p", "field-note", "Showing first 20 events. Additional history remains on Core."));
      }
    } catch (error) {
      if (epoch === generation && selectedTask === id) empty(get("task-timeline"), errorLabel(error));
    }
  }
  get("connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = get("owner-token").value.trim();
    get("owner-token").value = "";
    if (value.length < 32 || /\s/.test(value)) {
      get("connection-message").textContent = "Enter a valid local bootstrap token.";
      return;
    }
    disconnect("Connecting to owner session…");
    token = value;
    const epoch = generation;
    try {
      const session = await api("/v1/session");
      if (epoch !== generation) return;
      if (session.principal !== "owner") throw new Error("invalid_response");
      get("connect-panel").hidden = true;
      get("core-indicator").classList.add("connected");
      get("core-indicator").lastChild.textContent = " Core connected";
      get("connect-toggle").textContent = "Disconnect";
      get("stat-connection").textContent = "ONLINE";
      get("stat-connection-note").textContent = "Local owner session";
      get("task-submit").disabled = false;
      flash("Core connected. Use /tasks, /trust, /status or /task to stage a task.");
      await refresh();
    } catch (error) {
      if (epoch === generation) disconnect(errorLabel(error));
    }
  });
  get("connect-toggle").addEventListener("click", () => {
    if (token) {
      disconnect("Disconnected. No credentials were saved.");
    } else {
      get("connect-panel").hidden = false;
      get("owner-token").focus();
    }
  });
  get("task-objective").addEventListener("input", () => {
    const value = get("task-objective").value.trim();
    if (pendingCreate && pendingCreate.objective !== value) pendingCreate = null;
  });
  get("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !token) return;
    const objective = get("task-objective").value.trim();
    if (!objective || objective.length > 500) {
      get("task-message").textContent = "Provide a task description (1–500 characters).";
      return;
    }
    if (!pendingCreate || pendingCreate.objective !== objective) {
      if (!globalThis.crypto?.randomUUID) {
        get("task-message").textContent = "A secure local browser is required to queue tasks.";
        return;
      }
      pendingCreate = { objective, key: globalThis.crypto.randomUUID() };
    }
    const epoch = generation;
    busy = true;
    get("task-submit").disabled = true;
    get("task-message").textContent = "Recording task in Core…";
    try {
      const record = await api("/v1/tasks", { method: "POST", body: { objective }, key: pendingCreate.key });
      if (epoch !== generation) return;
      get("task-message").textContent = "Task recorded: " + clean(record.id, 36).slice(0, 8) + ". Execution was not started.";
      get("task-objective").value = "";
      pendingCreate = null;
      await loadTasks();
    } catch (error) {
      if (epoch === generation) {
        get("task-message").textContent = errorLabel(error) + " Retry uses the same request key.";
      }
    } finally {
      busy = false;
      if (epoch === generation) get("task-submit").disabled = !token;
    }
  });
  get("refresh-tasks").addEventListener("click", loadTasks);
  get("refresh-trust").addEventListener("click", loadApprovals);
  get("refresh-system").addEventListener("click", health);
  get("command-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const raw = get("command-input").value.trim();
    get("command-input").value = "";
    const lower = raw.toLowerCase();
    if (!raw) return;
    if (lower === "/status") {
      show("system");
      health();
      flash("Showing local Core readiness. Provider and PC execution remain gated.");
    } else if (lower === "/tasks") {
      show("tasks");
      if (token) loadTasks();
      flash("Task queue opened.");
    } else if (lower === "/trust") {
      show("trust");
      if (token) loadApprovals();
      flash("Trust centre is read-only. No decisions are submitted here.");
    } else if (lower === "/help") {
      flash("Commands: /status · /tasks · /trust · /help · /task <objective>. No AI chat in this build.");
    } else if (lower.startsWith("/task ")) {
      const objective = raw.slice(6).trim();
      if (!objective || objective.length > 500) {
        flash("Task objective must contain 1–500 characters.");
        return;
      }
      show("tasks");
      get("task-objective").value = objective;
      pendingCreate = null;
      get("task-objective").focus();
      flash("Review your objective and press Queue task. Nothing runs automatically.");
    } else {
      flash("This command deck has no live AI chat. Type /help for available local commands.");
    }
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      get("command-input").focus();
    } else if (event.altKey && "1234".includes(event.key) && event.key.length === 1) {
      event.preventDefault();
      show(Object.keys(names)[Number(event.key) - 1]);
    }
  });
  function stopMic() {
    micGeneration++;
    micPointer = null;
    if (micStream) {
      for (const track of micStream.getTracks()) track.stop();
      micStream = null;
    }
    get("mic-test").classList.remove("recording");
    get("mic-test").setAttribute("aria-label", "Hold to test microphone");
  }
  async function startMic() {
    if (!navigator.mediaDevices?.getUserMedia || !window.isSecureContext || document.hidden) {
      flash("Microphone requires a secure foreground browser and explicit permission.");
      return;
    }
    const request = ++micGeneration;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      if (request !== micGeneration || document.hidden || !document.hasFocus()) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      micStream = stream;
      for (const track of stream.getTracks()) track.addEventListener("ended", stopMic);
      get("mic-test").classList.add("recording");
      get("mic-test").setAttribute("aria-label", "Local microphone active. Release to stop");
      flash("Microphone LOCAL TEST active. No speech recognition, storage or transmission.");
    } catch (_) {
      if (request === micGeneration) flash("Microphone blocked or unavailable.");
    }
  }
  get("mic-test").addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || micPointer !== null) return;
    micPointer = event.pointerId;
    if (typeof event.currentTarget.setPointerCapture === "function") {
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    startMic();
  });
  window.addEventListener("pointerup", (event) => {
    if (event.pointerId === micPointer) stopMic();
  });
  window.addEventListener("pointercancel", (event) => {
    if (event.pointerId === micPointer) stopMic();
  });
  get("mic-test").addEventListener("lostpointercapture", () => {
    if (micPointer !== null && micPointer !== "keyboard") stopMic();
  });
  get("mic-test").addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat && micPointer === null) {
      event.preventDefault();
      micPointer = "keyboard";
      startMic();
    }
  });
  get("mic-test").addEventListener("keyup", (event) => {
    if ((event.key === " " || event.key === "Enter") && micPointer === "keyboard") {
      event.preventDefault();
      stopMic();
    }
  });
  window.addEventListener("blur", stopMic);
  window.addEventListener("pagehide", () => {
    stopMic();
    disconnect("Page closed; session cleared.");
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopMic();
    else refresh();
  });
  window.addEventListener("offline", stopMic);
  setInterval(() => {
    if (token && !document.hidden) refresh();
  }, 20000);
  disconnect("Enter your local Core token to begin.");
  show("home");
  health();
})();
