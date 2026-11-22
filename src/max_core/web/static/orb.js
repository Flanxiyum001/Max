"use strict";
// MAX web orb. Same behaviour as the desktop orb, in a browser.
const $ = (id) => document.getElementById(id);
const canvas = $("orb"), ctx = canvas.getContext("2d");
const answerEl = $("answer"), input = $("input"), hint = $("hint");
const loginForm = $("login"), passcode = $("passcode"), choices = $("choices");
const githubAuth = $("github-auth");
const sandboxLink = $("sandbox-link");
const appsToggle = $("connections-toggle"), appsPanel = $("connections-panel");
const appsList = $("connections-list");
const MCP_APPS = Object.freeze({
  github: ["GitHub", ["github.com"]],
  cloudflare: ["Cloudflare", ["cloudflare.com"]],
  railway: ["Railway", ["railway.com"]],
  notion: ["Notion", ["notion.com", "makenotion.com"]],
  linear: ["Linear", ["linear.app"]],
  sentry: ["Sentry", ["sentry.dev", "sentry.io"]],
  figma: ["Figma", ["figma.com"]],
});
let pendingConnectService = null;

let state = "idle";            // idle | thinking | speaking | listening | error
let energy = 0, t = 0;
let busy = false, authed = false, pendingJob = null, hideTimer = null;
let audio = null, recognition = null, listening = false, sttReady = false;

// ---------------------------------------------------------------- drawing
function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = innerWidth * dpr; canvas.height = innerHeight * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
addEventListener("resize", resize); resize();

const rgba = (r, g, b, a) => `rgba(${r},${g},${b},${a})`;
function frame() {
  const target = { idle: 0, thinking: 1, speaking: 0.6, error: 0.2, listening: 0.75 }[state];
  energy += (target - energy) * 0.05;
  t += 0.016 * (1 + 2.2 * energy);
  const w = innerWidth, h = innerHeight, cx = w / 2, cy = h * 0.42;
  const err = state === "error";
  const rim = err ? [255, 90, 90] : [90, 230, 255];
  const breathe = 1 + 0.012 * Math.sin(t * 1.3) + 0.02 * energy * Math.sin(t * 6) + 0.09 * level;
  const r = Math.min(w, h) * 0.24 * breathe;
  ctx.clearRect(0, 0, w, h);

  // halo
  let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 1.28);
  const a = Math.min(1, (110 + 90 * energy + 120 * level) / 255);
  g.addColorStop(0.74, rgba(...rim, a)); g.addColorStop(0.84, rgba(...rim, a / 3));
  g.addColorStop(1, rgba(0, 0, 0, 0));
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * 1.28, 0, 7); ctx.fill();

  // body
  g = ctx.createRadialGradient(cx - r * .08, cy + r * .05, 0, cx - r * .08, cy + r * .05, r);
  g.addColorStop(0, "#000"); g.addColorStop(0.6, "rgb(0,1,8)");
  g.addColorStop(0.84, err ? "rgb(60,6,12)" : "rgb(3,14,90)");
  g.addColorStop(0.95, err ? "rgb(160,40,40)" : "rgb(30,150,220)");
  g.addColorStop(1, err ? "rgb(255,150,150)" : "rgb(150,245,255)");
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();

  // inner currents
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.clip(); ctx.translate(cx, cy);
  const ribbons = [[.21, [20, 50, 255], 150, 1], [-.15, [10, 90, 255], 110, .9],
                   [.11, [40, 20, 200], 120, .8], [-.27, [30, 200, 255], 60, .95]];
  ribbons.forEach(([speed, hue, alpha, scale], i) => {
    if (err) hue = [200, 30, 50];
    ctx.save(); ctx.rotate(t * speed + i * 1.22);
    const wob = 0.18 * Math.sin(t * (0.7 + 0.2 * i) + i);
    ctx.beginPath();
    ctx.moveTo(-r * .95, r * (.1 + wob));
    ctx.bezierCurveTo(-r * .35, -r * (.95 - wob) * scale, r * .35, -r * (.7 + wob) * scale, r * .95, -r * .05);
    ctx.bezierCurveTo(r * .45, -r * (.25 - wob) * scale, -r * .25, -r * (.15 + wob) * scale, -r * .95, r * (.1 + wob));
    const rg = ctx.createRadialGradient(0, -r * .4, 0, 0, -r * .4, r * 1.1);
    rg.addColorStop(0, rgba(...hue, alpha * (0.75 + 0.35 * energy) / 255));
    rg.addColorStop(1, rgba(...hue, 0));
    ctx.fillStyle = rg; ctx.fill();
    ctx.strokeStyle = rgba(90, 160, 255, (25 + 70 * energy) / 255); ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
  });
  g = ctx.createRadialGradient(0, r * .05, 0, 0, r * .05, r * .7);
  g.addColorStop(0, "rgba(0,0,0,.92)"); g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, r * .05, r * .72, 0, 7); ctx.fill();
  ctx.restore();

  // rim
  ctx.lineWidth = Math.max(2, r * 0.045);
  g = ctx.createConicGradient ? ctx.createConicGradient(t * 0.25, cx, cy) : null;
  if (g) {
    [[0, "rgb(230,255,255)"], [.18, rgba(...rim, 1)], [.42, err ? rgba(...rim, 1) : "rgb(60,200,170)"],
     [.62, rgba(...rim, 1)], [.82, "rgb(170,250,255)"], [1, "rgb(230,255,255)"]]
      .forEach(([p, c]) => g.addColorStop(p, c));
    ctx.strokeStyle = g;
  } else ctx.strokeStyle = rgba(...rim, 1);
  ctx.beginPath(); ctx.arc(cx, cy, r * .975, 0, 7); ctx.stroke();
  ctx.lineWidth = 1; ctx.strokeStyle = "rgba(200,255,255,.27)";
  ctx.beginPath(); ctx.arc(cx, cy, r * .9, 0, 7); ctx.stroke();

  // listening ripples
  if (state === "listening") {
    for (let k = 0; k < 3; k++) {
      const ph = (t * 0.9 + k / 3) % 1;
      ctx.strokeStyle = rgba(150, 240, 255, 0.6 * (1 - ph)); ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(cx, cy, r * (1 + 0.35 * ph), 0, 7); ctx.stroke();
    }
  }

  // highlights
  ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.clip();
  g = ctx.createRadialGradient(cx + r * .5, cy - r * .6, 0, cx + r * .5, cy - r * .6, r * .9);
  g.addColorStop(0, "rgba(255,255,255,.94)"); g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * .96, 0, 7);
  ctx.arc(cx - r * .09, cy + r * .09, r * .93, 0, 7, true); ctx.fill();
  g = ctx.createRadialGradient(cx - r * .15, cy + r * .95, 0, cx - r * .15, cy + r * .95, r * .55);
  g.addColorStop(0, rgba(...rim, .78)); g.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(cx - r * .15, cy + r * .95, r * .6, r * .35, 0, 0, 7); ctx.fill();
  ctx.restore();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---------------------------------------------------------------- ui helpers
function show(text, ms) {
  clearTimeout(hideTimer);
  if (!text) { answerEl.classList.remove("show"); return; }
  answerEl.textContent = text; answerEl.classList.add("show");
  if (ms !== 0) hideTimer = setTimeout(() => answerEl.classList.remove("show"),
                                       ms || Math.max(12000, Math.min(60000, text.length * 90)));
}
function setState(s, backAfter) {
  state = s;
  if (backAfter) setTimeout(() => { if (state === s && !busy) state = "idle"; }, backAfter);
}
async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  if (res.status === 401) { showLogin(); throw new Error("login_required"); }
  if (!res.ok) { let d = "error"; try { d = (await res.json()).detail; } catch {} throw new Error(d); }
  return res;
}
const ERRORS = {
  model_not_configured: "No model connected yet. Set the MAX_TRIAL_* settings on the server.",
  model_unavailable: "I couldn't reach my model just now.",
  busy: "One moment, I'm still on the last one.",
  sandbox_failed: "The PR sandbox could not be started. Check the PR and Actions permissions.",
  slow_down: "Too many requests, give me a second.",
};
// Why the model call failed (sanitized gateway codes from the server).
const DETAILS = {
  auth_failure: "My model key was rejected. Check MAX_TRIAL_API_KEY.",
  provider_unavailable: "I can't reach the model server. Is FreeLLMAPI online at a public address?",
  timeout: "The model took too long to answer. Try again.",
  rate_limited: "All my free models are rate-limited right now. Try again in a minute.",
  invalid_request: "The model server rejected the request. Check MAX_TRIAL_MODEL.",
  model_mismatch: "The model server answered with an unexpected model.",
  malformed_provider_response: "The model server sent a reply I couldn't read.",
  cloud_sensitive_content_blocked: "I held that back: it looked like private data for a cloud model.",
};

// ---------------------------------------------------------------- login
function showLogin() {
  authed = false; loginForm.classList.remove("hidden"); input.classList.add("hidden");
  githubAuth.classList.add("hidden");
  sandboxLink.classList.add("hidden");
  appsToggle.classList.add("hidden"); appsPanel.classList.add("hidden");
  hint.textContent = "enter passcode"; passcode.focus();
}
loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/login", { passcode: passcode.value });
    passcode.value = ""; loginForm.classList.add("hidden"); await boot();
  } catch (err) {
    setState("error", 1500); show(err.message === "too_many_attempts" ? "Too many tries. Wait a few minutes." : "That's not it.", 3000);
  }
});

async function boot() {
  const s = await (await fetch("/api/state", { credentials: "same-origin" })).json();
  if (!s.authed) return showLogin();
  authed = true; sttReady = !!s.stt_ready;
  appsToggle.classList.remove("hidden");
  hint.textContent = "type to talk · space or tap the orb to speak";
  if (!s.model_ready) { setState("error", 3000); show(ERRORS.model_not_configured); }
  else show("Hello. I'm here.", 4000);
  const outcome = new URLSearchParams(location.search).get("github");
  if (outcome === "connected") show(
    "GitHub authorized. MAX discovered available MCP tools. Each tool call still needs your approval.",
    11000
  );
  if (outcome === "failed") show(
    "GitHub authorization returned, but MCP discovery failed. Ask MAX to connect again.",
    10000
  );
  if (outcome === "denied") show("GitHub authorization cancelled.", 6000);
  const mcp = new URLSearchParams(location.search).get("mcp");
  if (mcp === "connected") show(
    "App connected to MAX. Compatible MCP tools are ready. I will ask before every action.",
    11500
  );
  if (mcp === "failed") show(
    "App authorization or tool discovery failed. You can retry from Apps.",
    9000
  );
  if (mcp === "denied") show("App authorization cancelled.", 6000);
  if (outcome || mcp) history.replaceState({}, "", "/");
  saveWake();
}

// ---------------------------------------------------------------- conversation
async function ask(text, spoken) {
  if (busy || !text.trim()) return;
  githubAuth.classList.add("hidden");
  sandboxLink.classList.add("hidden");
  pendingConnectService = null;
  busy = true; setState("thinking"); show("");
  try {
    const { job } = await (await api("/api/ask", { text, voice: spoken })).json();
    pendingJob = job;
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const j = await (await api(`/api/job/${job}`)).json();
      if (j.status === "approval" && j.approval) { askApproval(j.approval); continue; }
      choices.classList.add("hidden");
      if (j.status === "running") {
        const last = j.steps[j.steps.length - 1];
        if (last) show("⋯ " + last.split(":")[0], 0);
        continue;
      }
      busy = false; pendingJob = null;
      if (j.status === "done") {
        if (j.sandbox?.run_url) {
          try {
            const link = new URL(j.sandbox.run_url);
            if (link.protocol === "https:" && link.hostname === "github.com" &&
                /^\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9_.-]{1,100}\/actions\/runs\/[0-9]+$/.test(link.pathname)) {
              sandboxLink.href = link.href;
              sandboxLink.classList.remove("hidden");
            }
          } catch {}
        }
        if (j.connect?.action === "authorize" && MCP_APPS[j.connect.service]) {
          pendingConnectService = j.connect.service;
          githubAuth.textContent = "Authorize " + MCP_APPS[pendingConnectService][0];
          githubAuth.classList.remove("hidden");
          stopWake();
        }
        setState("speaking"); show(j.answer);
        if (spoken) { followUp = true; await say(j.answer); } else setState("speaking", 2500);
        if (!followUp) resumeWake();
      } else { setState("error", 2500); show(DETAILS[j.detail] || ERRORS[j.error] || ERRORS.model_unavailable); resumeWake(); }
      return;
    }
  } catch (err) {
    busy = false; pendingJob = null; choices.classList.add("hidden");
    if (err.message !== "login_required") { setState("error", 2500); show(ERRORS[err.message] || ERRORS.model_unavailable); }
  }
}

let approvalShown = false;
function askApproval(a) {
  if (approvalShown) return;
  approvalShown = true; setState("speaking");
  const detail = Object.entries(a.arguments).map(([k, v]) => `${k}: ${v}`).join(", ");
  show(`MAX wants to run ${a.tool}\n${detail}`, 0);
  choices.classList.remove("hidden");
}
async function answerApproval(allow) {
  if (!approvalShown || !pendingJob) return;
  approvalShown = false; choices.classList.add("hidden");
  setState("thinking"); show(allow ? "⋯ allowed" : "⋯ denied", 0);
  try { await api(`/api/job/${pendingJob}/approve`, { allow }); } catch {}
}
function appAuthorizationURL(service, url) {
  const item = MCP_APPS[service];
  if (!item) throw new Error("unknown_mcp_service");
  const destination = new URL(url);
  const host = destination.hostname.toLowerCase();
  if (destination.protocol !== "https:" || destination.username || destination.password ||
      !item[1].some(domain => host === domain || host.endsWith("." + domain))) {
    throw new Error("invalid_mcp_authorization_url");
  }
  return destination.href;
}

async function authorizeApp(service) {
  if (!MCP_APPS[service]) return;
  githubAuth.disabled = true;
  try {
    const { url } = await (await api("/api/connect/mcp/" + service + "/start", {})).json();
    window.location.assign(appAuthorizationURL(service, url));
  } catch (err) {
    githubAuth.disabled = false;
    const message = err.message === "github_oauth_not_configured"
      ? "GitHub requires a one-time OAuth App registration for MAX."
      : err.message === "mcp_public_url_missing"
      ? "MAX needs a stable HTTPS URL for OAuth."
      : err.message === "mcp_oauth_registration_not_supported"
      ? "This provider requires its own OAuth App registration. MAX can't register automatically."
      : "Connection setup failed. Check the provider or try again.";
    show(message, 8500);
  }
}

githubAuth.addEventListener("click", () => {
  if (pendingConnectService) authorizeApp(pendingConnectService);
});

async function renderApps() {
  if (!authed) return;
  let info;
  try { info = (await (await api("/api/state")).json()).connections; }
  catch { return; }
  appsList.replaceChildren();
  for (const [service, config] of Object.entries(MCP_APPS)) {
    const current = info?.services?.[service] || {};
    const row = document.createElement("div");
    row.className = "connection-row";
    const label = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = config[0];
    const description = document.createElement("small");
    description.textContent = current.connected
      ? "Connected · " + (current.tools || 0) + " tools"
      : current.configured ? "Not connected" : "Needs one-time app setup";
    label.append(title, description);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = current.connected ? "Disconnect" : "Connect";
    button.onclick = async () => {
      if (current.connected) {
        if (!confirm("Disconnect " + config[0] + " from MAX?")) return;
        try {
          await api("/api/connect/mcp/" + service + "/disconnect", {});
          await renderApps(); show(config[0] + " disconnected.", 4500);
        } catch { show("Couldn't disconnect " + config[0] + ".", 5000); }
      } else {
        pendingConnectService = service;
        await authorizeApp(service);
      }
    };
    row.append(label, button);
    appsList.append(row);
  }
}

appsToggle.addEventListener("click", async () => {
  appsPanel.classList.toggle("hidden");
  if (!appsPanel.classList.contains("hidden")) await renderApps();
});
$("connections-close").addEventListener("click", () => appsPanel.classList.add("hidden"));
$("allow").onclick = () => answerApproval(true);
$("deny").onclick = () => answerApproval(false);

// ---------------------------------------------------------------- sound level → orb
// level (0..1) comes from the mic while listening and from MAX's voice while speaking.
// Clean speech for the recogniser: mono, echo-cancelled, noise-suppressed, levelled.
const MIC = { audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } };
let noiseFloor = 0.03;
let level = 0, audioCtx = null, analyser = null, levelSource = null, micStream = null;
function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioCtx.createAnalyser(); analyser.fftSize = 512;
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
}
function connectLevel(node) {
  try { if (levelSource) levelSource.disconnect(); } catch {}
  levelSource = node; node.connect(analyser);
}
const buf = new Uint8Array(512);
function sampleLevel() {
  if (analyser && (state === "listening" || state === "speaking")) {
    analyser.getByteTimeDomainData(buf);
    let sum = 0; for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
    level += (Math.min(1, Math.sqrt(sum / buf.length) * 4) - level) * 0.35;
  } else level *= 0.85;
  requestAnimationFrame(sampleLevel);
}
requestAnimationFrame(sampleLevel);
async function startMicLevel() {
  try {
    ensureAudio();
    if (!micStream) micStream = await navigator.mediaDevices.getUserMedia(MIC);
    connectLevel(audioCtx.createMediaStreamSource(micStream));
  } catch {}
}
function stopMicLevel() {
  if (micStream) { micStream.getTracks().forEach((tr) => tr.stop()); micStream = null; }
}
function chime(up) {
  try {
    ensureAudio();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain(), now = audioCtx.currentTime;
    o.type = "sine";
    o.frequency.setValueAtTime(up ? 660 : 880, now);
    o.frequency.exponentialRampToValueAtTime(up ? 990 : 520, now + 0.16);
    g.gain.setValueAtTime(0.0001, now); g.gain.exponentialRampToValueAtTime(0.12, now + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, now + 0.22);
    o.connect(g).connect(audioCtx.destination); o.start(now); o.stop(now + 0.25);
  } catch {}
}

// ---------------------------------------------------------------- voice out
let followUp = false;
// Browsers keep page audio locked until the first tap/keypress. Unlock once, on
// that gesture, and reuse ONE player element so later replies may play by themselves.
let player = null, playerSrc = null;
function silentWav() {
  const b = new Uint8Array(44 + 800), v = new DataView(b.buffer);
  const w = (o, t) => [...t].forEach((c, i) => b[o + i] = c.charCodeAt(0));
  w(0, "RIFF"); v.setUint32(4, 36 + 800, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 8000, true);
  v.setUint32(28, 8000, true); v.setUint16(32, 1, true); v.setUint16(34, 8, true);
  w(36, "data"); v.setUint32(40, 800, true); b.fill(128, 44);
  return URL.createObjectURL(new Blob([b], { type: "audio/wav" }));
}
function unlockAudio() {
  try { ensureAudio(); } catch {}
  if (!player) {
    player = new Audio(); player.preload = "auto";
    player.src = silentWav(); player.play().catch(() => {});
  }
  if (window.speechSynthesis && !unlockAudio.tts) {   // also prime the browser voice
    unlockAudio.tts = true;
    try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); } catch {}
  }
}
["pointerdown", "keydown", "touchend"].forEach((ev) =>
  document.addEventListener(ev, unlockAudio, { capture: true, passive: true }));
async function audioRunning() {
  if (!audioCtx) return false;
  if (audioCtx.state !== "running") {
    try { await Promise.race([audioCtx.resume(), new Promise((r) => setTimeout(r, 400))]); } catch {}
  }
  return audioCtx.state === "running";
}
function say(text) {
  return new Promise(async (done) => {
    try {
      const res = await api("/api/speak", { text: text.slice(0, 2000) });
      const url = URL.createObjectURL(await res.blob());
      const wantFollowUp = followUp;
      stopSpeaking(); followUp = wantFollowUp;
      // Route through the visualiser only while it is running; a suspended
      // AudioContext would otherwise swallow the sound without any error.
      const running = await audioRunning();
      let el;
      if (player && (playerSrc || running)) {
        el = player;
        if (!playerSrc && running) {
          try { playerSrc = audioCtx.createMediaElementSource(player); playerSrc.connect(audioCtx.destination); } catch {}
        }
        if (playerSrc && !running) el = new Audio();   // routed but muted: play directly instead
      } else el = new Audio();
      if (el === player && playerSrc) connectLevel(playerSrc);
      el.src = url; audio = el; setState("speaking");
      el.onended = () => {
        URL.revokeObjectURL(url); el.onended = null; audio = null;
        if (!busy) setState("idle");
        done();
        if (followUp) { followUp = false; setTimeout(() => startListening({ followUp: true }), 250); }
        else resumeWake();
      };
      await el.play();
    } catch {
      // Server voices failed: speak with the browser's built-in voice instead.
      const wantFollowUp = followUp;
      const after = () => {
        if (!busy) setState("idle");
        done();
        if (wantFollowUp) { followUp = false; setTimeout(() => startListening({ followUp: true }), 400); }
        else resumeWake();
      };
      if (!browserSay(text, after)) { setState("speaking", 2500); after(); }
    }
  });
}
function pickVoice() {
  const vs = window.speechSynthesis ? speechSynthesis.getVoices() : [];
  const en = vs.filter((v) => /^en/i.test(v.lang));
  const prefer = /(natural|neural|online).*(female|emily|aria|jenny|sonia|libby|natasha)|emily|aria|jenny|sonia|libby|samantha|karen|moira|tessa|serena|google uk english female|google us english|zira/i;
  return en.find((v) => prefer.test(v.name)) || en[0] || vs[0] || null;
}
function browserSay(text, after) {
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return false;
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text.slice(0, 2000));
    const v = pickVoice(); if (v) { u.voice = v; u.lang = v.lang; }
    u.rate = 1.02; u.pitch = 1.05;
    u.onstart = () => setState("speaking");
    u.onend = after; u.onerror = after;
    speechSynthesis.speak(u);
    return true;
  } catch { return false; }
}
if (window.speechSynthesis) { speechSynthesis.getVoices(); speechSynthesis.onvoiceschanged = () => speechSynthesis.getVoices(); }
function stopSpeaking() { if (window.speechSynthesis) speechSynthesis.cancel(); followUp = false; if (audio) { audio.pause(); audio = null; if (!busy) setState("idle"); } }

// ---------------------------------------------------------------- voice in (Siri-style)
// • tap / Space: listen once; sends by itself when you stop talking
// • "Hey MAX" (opt-in): always listening for the wake phrase while this tab is open
// • after a spoken answer MAX keeps listening ~6 s for a follow-up
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const WAKE = /\b(?:hey|hi|ok|okay)[\s,]+max\b[\s,.!?]*/i;
let wakeOn = localStorage.getItem("max_wake") === "on";
let wakeRec = null, wakeWanted = false;

// High-accuracy path: record → server Whisper-class transcription. Ends by itself
// when you stop talking (voice-activity detection on the mic level).
let recorder = null;
function recOptions() {
  const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"]
    .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
  return { mime, opts: mime ? { mimeType: mime, audioBitsPerSecond: 128000 } : { audioBitsPerSecond: 128000 } };
}
// Adaptive end-of-speech: thresholds follow the room's noise floor, and a
// natural mid-sentence pause (< 1.6 s) does not cut you off.
function speechGate(onDone, { spoke = false, maxWaitMs = 8000, maxMs = 30000 } = {}) {
  const started = Date.now(); let quietSince = null;
  const timer = setInterval(() => {
    const now = Date.now();
    if (!spoke) noiseFloor += (Math.min(level, 0.2) - noiseFloor) * 0.05;
    const on = Math.max(0.09, noiseFloor * 3), off = Math.max(0.05, noiseFloor * 1.8);
    if (level > on) { spoke = true; quietSince = null; }
    else if (spoke && level < off) quietSince = quietSince || now;
    if (!spoke && now - started > maxWaitMs) { clearInterval(timer); onDone(false); }
    else if ((spoke && quietSince && now - quietSince > 1600) || now - started > maxMs) {
      clearInterval(timer); onDone(true);
    }
  }, 100);
  return () => clearInterval(timer);
}
async function transcribeClip(blob) {
  const res = await fetch("/api/transcribe", { method: "POST", body: blob,
    headers: { "Content-Type": blob.type }, credentials: "same-origin" });
  if (!res.ok) throw new Error("stt");
  const { text } = await res.json();
  return (text || "").trim();
}
async function startRecording(opts = {}) {
  stopSpeaking(); stopWake();
  listening = true; setState("listening"); chime(true);
  show(opts.followUp ? "…" : "Listening…", 0);
  await startMicLevel();
  if (!micStream) { listening = false; setState("error", 2500); show("Microphone permission was denied.", 3500); return; }
  const { mime, opts: ropts } = recOptions();
  const chunks = [];
  recorder = new MediaRecorder(micStream, ropts);
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  let cancelled = false;
  const stopGate = speechGate((heard) => {
    cancelled = !heard;
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }, { maxWaitMs: opts.followUp ? 6000 : 8000 });
  recorder.onstop = async () => {
    stopGate(); listening = false; recorder = null;
    const blob = new Blob(chunks, { type: (mime || "audio/webm").split(";")[0] });
    stopMicLevel();
    if (cancelled || blob.size < 1500) {
      setState("idle"); if (!opts.followUp) show("I didn't hear anything.", 2500); else show("");
      resumeWake(); return;
    }
    chime(false); setState("thinking"); show("⋯", 0);
    try {
      const text = await transcribeClip(blob);
      if (text) { show(`“${text}”`, 0); ask(text, true); }
      else { setState("idle"); show("I didn't catch that.", 2500); resumeWake(); }
    } catch { setState("error", 2500); show("I couldn't transcribe that. Try again?", 3000); resumeWake(); }
  };
  recorder.start(250);
}

function startListening(opts = {}) {
  if (!authed || busy || listening) return;
  if (sttReady && window.MediaRecorder && navigator.mediaDevices) { startRecording(opts); return; }
  if (!SR) { setState("error", 2500); show("This browser can't listen. Try Chrome or Edge.", 4000); return; }
  stopSpeaking(); stopWake();
  recognition = new SR(); recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true; recognition.continuous = false;
  let finalText = "", heardAnything = false;
  const silenceTimer = opts.followUp ? setTimeout(() => { if (!heardAnything) recognition.abort(); }, 6000) : null;
  recognition.onresult = (e) => {
    heardAnything = true;
    let interim = "";
    for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim += r[0].transcript));
    show(`“${finalText || interim}”`, 0);
  };
  recognition.onerror = (e) => {
    if (e.error === "not-allowed") { setState("error", 2500); show("Microphone permission was denied.", 3500); wakeOn = false; saveWake(); }
  };
  recognition.onend = () => {
    clearTimeout(silenceTimer); listening = false; stopMicLevel();
    const text = finalText.trim();
    if (text) { chime(false); ask(text, true); }
    else { if (state === "listening") setState("idle"); if (!opts.followUp) show("I didn't hear anything.", 2500); else show(""); resumeWake(); }
  };
  listening = true; setState("listening"); chime(true);
  show(opts.followUp ? "…" : "Listening…", 0);
  startMicLevel();
  try { recognition.start(); } catch { listening = false; setState("idle"); }
}
function toggleListening() {
  if (listening) {
    if (recorder && recorder.state !== "inactive") recorder.stop();
    else recognition && recognition.stop();
    return;
  }
  startListening();
}
canvas.addEventListener("click", toggleListening);

// While "Hey MAX" is on, a rolling recording runs alongside the browser's wake
// detector, so a command said in the same breath is transcribed by the server's
// accurate recogniser instead of the browser's.
let tape = null;
function startTape() {
  if (!sttReady || !window.MediaRecorder || !micStream || tape) return;
  const { mime, opts } = recOptions();
  const t = { chunks: [], mime, started: Date.now(), wake: false, rec: new MediaRecorder(micStream, opts) };
  t.rec.ondataavailable = (e) => { if (e.data.size) t.chunks.push(e.data); };
  t.rec.start(250); tape = t;
}
function dropTape() { if (tape) { const t = tape; tape = null; try { t.rec.onstop = null; t.rec.stop(); } catch {} } }
setInterval(() => {   // keep the tape short: restart it during silence
  if (tape && !tape.wake && Date.now() - tape.started > 20000 && level < Math.max(0.05, noiseFloor * 1.8)) { dropTape(); startTape(); }
}, 1000);
function finishTape() {
  const t = tape; t.wake = true;
  listening = true; setState("listening"); chime(true); show("Listening…", 0);
  speechGate(() => { try { t.rec.stop(); } catch {} }, { spoke: true, maxMs: 20000 });
  t.rec.onstop = async () => {
    tape = null; listening = false; stopMicLevel();
    const blob = new Blob(t.chunks, { type: (t.mime || "audio/webm").split(";")[0] });
    chime(false); setState("thinking"); show("⋯", 0);
    try {
      const said = await transcribeClip(blob);
      const m = said.match(WAKE);
      const rest = (m ? said.slice(m.index + m[0].length) : said).trim();
      if (rest.length > 2) { show(`“${rest}”`, 0); ask(rest, true); }
      else { setState("idle"); startListening(); }   // just "Hey MAX": listen for the command
    } catch { setState("error", 2500); show("I couldn't transcribe that. Try again?", 3000); resumeWake(); }
  };
}
async function startWake() {
  if (!SR || !wakeOn || !authed || listening || busy || audio || wakeRec) return;
  wakeWanted = true;
  if (sttReady && window.MediaRecorder) { await startMicLevel(); startTape(); }
  if (!wakeWanted || wakeRec) return;
  wakeRec = new SR(); wakeRec.lang = navigator.language || "en-US";
  wakeRec.continuous = true; wakeRec.interimResults = true;
  wakeRec.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const said = e.results[i][0].transcript;
      const m = said.match(WAKE);
      if (!m) continue;
      if (tape && !tape.wake) {   // accurate path: let the recording finish, then transcribe it
        wakeWanted = false; try { wakeRec.abort(); } catch {} wakeRec = null;
        finishTape(); return;
      }
      const rest = said.slice(m.index + m[0].length).trim();
      if (e.results[i].isFinal && rest.length > 2) {   // "Hey MAX, what's the weather?"
        stopWake(); chime(true); chime(false); show(`“${rest}”`, 0); ask(rest, true); return;
      }
      if (!rest) { stopWake(); setTimeout(() => startListening(), 150); return; }  // "Hey MAX" … pause
    }
  };
  wakeRec.onerror = (e) => { if (e.error === "not-allowed") { wakeOn = false; saveWake(); } };
  wakeRec.onend = () => { wakeRec = null; if (wakeWanted) setTimeout(startWake, 400); };  // browsers stop it periodically
  try { wakeRec.start(); } catch { wakeRec = null; }
}
function stopWake() {
  wakeWanted = false; if (tape && !tape.wake) dropTape();
  if (wakeRec) { try { wakeRec.abort(); } catch {} wakeRec = null; }
}
function resumeWake() { if (wakeOn) setTimeout(startWake, 300); }
function saveWake() {
  localStorage.setItem("max_wake", wakeOn ? "on" : "off");
  wakeBtn.textContent = wakeOn ? "“Hey MAX”: on" : "“Hey MAX”: off";
  wakeBtn.classList.toggle("on", wakeOn);
  if (wakeOn) startWake(); else stopWake();
}
const wakeBtn = $("wake");
wakeBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!SR) { show("This browser can't listen. Try Chrome or Edge.", 4000); return; }
  wakeOn = !wakeOn; saveWake();
  if (wakeOn) show("Say “Hey MAX” any time while this tab is open.", 4000);
});
document.addEventListener("visibilitychange", () => { if (document.hidden) stopWake(); else resumeWake(); });

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  if (!authed) return;
  if (approvalShown) {
    if (e.key === "Enter") { e.preventDefault(); answerApproval(true); }
    else if (e.key === "Escape") { e.preventDefault(); answerApproval(false); }
    return;
  }
  if (document.activeElement === input) {
    if (e.key === "Enter") { e.preventDefault(); const v = input.value; input.value = ""; input.classList.add("hidden"); ask(v, false); }
    else if (e.key === "Escape") { input.value = ""; input.classList.add("hidden"); input.blur(); }
    return;
  }
  if (e.key === " " && !e.repeat) { e.preventDefault(); toggleListening(); return; }
  if (e.key === "Escape") { if (audio) stopSpeaking(); else show(""); return; }
  if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
    input.classList.remove("hidden"); input.focus();
  }
});
input.addEventListener("input", () => { if (!input.value) input.classList.add("hidden"); });

// ---------------------------------------------------------------- reminders
setInterval(async () => {
  if (!authed || busy) return;
  try {
    const { due } = await (await api("/api/reminders/due")).json();
    if (due.length) { const text = "Reminder: " + due.join(" · "); setState("speaking"); show(text); say(text); }
  } catch {}
}, 30000);

boot();
