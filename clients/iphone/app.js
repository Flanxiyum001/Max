"use strict";

(() => {
  const viewNames = new Set(["voice", "memory", "approvals"]);
  const tabs = document.querySelectorAll("[data-tab]");
  const panels = document.querySelectorAll("[data-view]");
  const connection = document.getElementById("connection");
  const memoryCard = document.getElementById("memory-card");
  const approvalsList = document.getElementById("approvals-list");
  const count = document.getElementById("queue-count");
  const previous = document.getElementById("previous");
  const next = document.getElementById("next");
  const ptt = document.getElementById("ptt");
  const micState = document.getElementById("mic-state");
  const voiceStage = document.querySelector(".voice-stage");
  let suggestions = [];
  let pending = [];
  let index = 0;
  let stream = null;
  let activePointer = null;
  let captureGeneration = 0;
  let loading = false;
  let swipeStart = null;

  const stringField = (value, max = 400) =>
    typeof value === "string" ? value.slice(0, max) : "";
  const node = (tag, className, value) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  };
  function message(target, text) {
    target.replaceChildren(node("div", "empty", text));
  }
  function setConnection(connected) {
    connection.classList.toggle("connected", connected);
    connection.textContent = connected ? "Core connected" : "Not connected";
  }
  function renderMemory() {
    const item = suggestions[index];
    previous.disabled = index <= 0;
    next.disabled = index >= suggestions.length - 1;
    count.textContent = suggestions.length ? `${index + 1} / ${suggestions.length}` : "0 / 0";
    if (!item) {
      message(memoryCard, "No authenticated pending memory suggestions available.");
      return;
    }
    const box = node("article", "memory-item");
    box.append(
      node("small", "", "Suggestion " + (index + 1)),
      node("div", "", stringField(item.summary))
    );
    memoryCard.replaceChildren(box);
  }
  function renderApprovals() {
    approvalsList.replaceChildren();
    if (!pending.length) {
      message(approvalsList, "No authenticated pending actions available.");
      return;
    }
    for (const item of pending) {
      const card = node("article", "approval-card");
      card.append(
        node("strong", "", stringField(item.action, 90) || "Pending review"),
        node("p", "", stringField(item.summary, 250)),
        node("small", "", "Open MAX Trust in an authenticated client to decide")
      );
      approvalsList.append(card);
    }
  }
  function clearPrivateData() {
    suggestions = [];
    pending = [];
    index = 0;
    setConnection(false);
    renderMemory();
    renderApprovals();
  }
  function show(tab) {
    if (!viewNames.has(tab)) return;
    for (const panel of panels) panel.hidden = panel.dataset.view !== tab;
    for (const button of tabs) {
      const selected = button.dataset.tab === tab;
      button.classList.toggle("selected", selected);
      if (selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
  }
  for (const tab of tabs) {
    tab.addEventListener("click", () => show(tab.dataset.tab));
  }
  function shift(amount) {
    index = Math.min(Math.max(index + amount, 0), Math.max(suggestions.length - 1, 0));
    renderMemory();
  }
  previous.addEventListener("click", () => shift(-1));
  next.addEventListener("click", () => shift(1));
  memoryCard.addEventListener("touchstart", (event) => {
    swipeStart = event.touches.length === 1 ? event.touches[0].clientX : null;
  }, { passive: true });
  memoryCard.addEventListener("touchend", (event) => {
    if (swipeStart === null || event.changedTouches.length !== 1) return;
    const movement = event.changedTouches[0].clientX - swipeStart;
    swipeStart = null;
    if (Math.abs(movement) > 55) shift(movement < 0 ? 1 : -1);
  }, { passive: true });

  // The future backend must authenticate the HTTPS session, enforce CORS/CSRF,
  // classify/redact data, and send ONLY owner-scoped pending summaries. No
  // mutation is made from this prototype, and no tokens are cached.
  async function refresh() {
    if (loading || document.hidden) return;
    loading = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 6000);
    try {
      const response = await fetch("/api/mobile/v1/snapshot", {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
        signal: controller.signal
      });
      if (!response.ok || !(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("backend_unavailable");
      }
      const data = await response.json();
      if (data?.authenticated !== true ||
          !Array.isArray(data?.memory_suggestions) ||
          !Array.isArray(data?.pending_approvals) ||
          data.memory_suggestions.length > 100 || data.pending_approvals.length > 100) {
        throw new Error("invalid_backend_snapshot");
      }
      if (document.hidden || !document.hasFocus()) throw new Error("background_snapshot");
      // Whitelist display-only fields. Never render arbitrary HTML or links.
      suggestions = data.memory_suggestions.filter(x => x && typeof x.summary === "string");
      pending = data.pending_approvals.filter(x => x && typeof x.action === "string");
      index = 0;
      setConnection(true);
      renderMemory();
      renderApprovals();
    } catch (_) {
      clearPrivateData();
    } finally {
      clearTimeout(timeout);
      loading = false;
    }
  }

  function stopCapture() {
    captureGeneration++;
    activePointer = null;
    if (stream) {
      for (const track of stream.getTracks()) track.stop();
      stream = null;
    }
    voiceStage.classList.remove("recording");
    micState.textContent = "Microphone is off";
    ptt.textContent = "Hold to test microphone";
  }
  async function startCapture() {
    if (!navigator.mediaDevices?.getUserMedia || document.hidden || !window.isSecureContext) {
      micState.textContent = "Microphone requires a secure foreground browser";
      return;
    }
    const request = ++captureGeneration;
    try {
      // Invoked directly from pointerdown / keydown, without an intervening await.
      const captured = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
        video: false
      });
      if (request !== captureGeneration || document.hidden || !document.hasFocus()) {
        captured.getTracks().forEach(track => track.stop());
        return;
      }
      stream = captured;
      for (const track of captured.getTracks()) track.addEventListener("ended", stopCapture);
      voiceStage.classList.add("recording");
      micState.textContent = "Local microphone active — release to stop";
      ptt.textContent = "Release to stop";
    } catch (_) {
      if (request === captureGeneration) {
        micState.textContent = "Microphone unavailable or permission denied";
      }
    }
  }
  ptt.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || activePointer !== null) return;
    activePointer = event.pointerId;
    startCapture();
  });
  window.addEventListener("pointerup", (event) => {
    if (event.pointerId === activePointer) stopCapture();
  });
  window.addEventListener("pointercancel", (event) => {
    if (event.pointerId === activePointer) stopCapture();
  });
  ptt.addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat && activePointer === null) {
      event.preventDefault();
      activePointer = "keyboard";
      startCapture();
    }
  });
  ptt.addEventListener("keyup", (event) => {
    if ((event.key === " " || event.key === "Enter") && activePointer === "keyboard") {
      event.preventDefault();
      stopCapture();
    }
  });
  window.addEventListener("blur", () => {
    stopCapture();
    clearPrivateData();
  });
  window.addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopCapture();
      clearPrivateData();
    } else refresh();
  });
  window.addEventListener("offline", () => {
    stopCapture();
    clearPrivateData();
  });
  window.addEventListener("pagehide", () => {
    stopCapture();
    clearPrivateData();
  });
  show("voice");
  clearPrivateData();
  refresh();
  // Only static shell assets are offline-capable; no private API responses.
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("./service-worker.js").catch(() => {});
  }
})();
