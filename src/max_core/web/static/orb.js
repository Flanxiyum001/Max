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
  render: ["Render", ["render.com"]],
});
const keybox = $("keybox"), keyLink = $("key-link"), keyInput = $("key-input");
let keyService = null;
keybox.addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = keyInput.value.trim(); if (!keyService || !key) return;
  keyInput.value = ""; keybox.classList.add("hidden");
  setState("thinking"); show("Connecting…", 0);
  try {
    const res = await api(`/api/connect/mcp/${keyService}/key`, { key });
    const { tools } = await res.json();
    const msg = `${MCP_APPS[keyService][0]} is connected. I can use ${tools} of its tools, and I'll ask before each action.`;
    setState("speaking"); show(msg); say(msg);
  } catch (err) {
    setState("error", 2500);
    show(err.message === "mcp_api_key_invalid" ? "That doesn't look like a valid key. Try again?" : "I couldn't connect with that key.", 3500);
  }
  keyService = null;
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
// Particle-sphere voice orb. Adapted from "Speaking Orb" by aqualang89 (21st.dev), MIT licence.
// Swells and sends lit rings on every syllable while speaking, reacts to mic level while listening.
const ORB_STATES = ["listening", "thinking", "speaking", "error"];
const ORB_COLORS = [[255, 185, 105], [178, 151, 255], [255, 150, 214], [255, 90, 90]];
const TAU = Math.PI * 2;
const ORB_N = 1400;
const SPHERE = Array.from({ length: ORB_N }, (_, i) => {
  const rnd = (v) => { const q = Math.sin(v * 12.9898) * 43758.5453; return q - Math.floor(q); };
  const j = (1 - 1000 / ORB_N) * 1.6 / Math.sqrt(ORB_N);
  const y = Math.max(-1, Math.min(1, 1 - 2 * (i + 0.5) / ORB_N + (rnd(i + 0.37) - 0.5) * j));
  const a = i * 2.399963229728653 + (rnd(i) - 0.5) * j * 6, s = Math.sqrt(1 - y * y);
  return { x: Math.cos(a) * s, y, z: Math.sin(a) * s, a, u: i / ORB_N, sa: Math.sin(a) };
});
let orbW = [1, 0, 0, 0], orbEnv = 0, orbOnsets = [], orbLast = 0, orbTime = 0, orbSprite = null, orbSpriteKey = "";
const orbSpark = makeSprite("255,255,255", 0.5);
function makeSprite(rgb, core) {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const x = c.getContext("2d"), g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
  g.addColorStop(0, `rgba(${rgb},1)`); g.addColorStop(core, `rgba(${rgb},1)`);
  g.addColorStop(core + 0.03, `rgba(${rgb},.065)`); g.addColorStop(0.94, `rgba(${rgb},.065)`);
  g.addColorStop(1, `rgba(${rgb},0)`); x.fillStyle = g; x.fillRect(0, 0, 64, 64); return c;
}
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");
function frame(now) {
  const dt = orbLast ? Math.min((now - orbLast) / 1000, 0.05) : 0; orbLast = now;
  const still = reduceMotion.matches;
  orbTime += dt;
  const key = state === "idle" ? "listening" : state;
  const k = 1 - Math.exp(-dt * 7);
  orbW = orbW.map((w, i) => w + ((ORB_STATES[i] === key ? 1 : 0) - w) * k);
  // speech envelope: fast attack, soft release; onsets (syllables) spawn rings
  // Browser speech has no measurable audio, so fall back to a syllable-like rhythm (~5/s).
  const synthPulse = 0.55 + 0.45 * Math.sin(Math.PI * ((orbTime * 4.6) % 1)) * (0.6 + 0.4 * Math.sin(orbTime * 1.7));
  const target = state === "speaking" ? (level > 0.04 ? level : synthPulse * ((orbTime * 4.6) % 1 < 0.7 ? 1 : 0.3)) : 0, prev = orbEnv;
  orbEnv += (target - orbEnv) * (target > orbEnv ? 0.55 : 0.18);
  if (orbEnv - prev > 0.06 && orbTime - (orbOnsets.at(-1) ?? -1) > 0.12) orbOnsets.push(orbTime);
  orbOnsets = orbOnsets.filter((o) => orbTime - o < 1.2);
  const h = still ? 0 : orbTime, env = still ? 0 : orbEnv, onsets = still ? [] : orbOnsets;
  const mic = state === "listening" ? level : 0;

  const w = innerWidth, hh = innerHeight, S = Math.min(w, hh) * 0.62 / 720;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr * S, 0, 0, dpr * S, dpr * (w / 2 - 360 * S), dpr * (hh * 0.42 - 360 * S));
  const col = [0, 1, 2].map((c) => Math.round(ORB_COLORS.reduce((s, rgb, i) => s + rgb[c] * orbW[i], 0)));
  const rgb = col.join(","), sw = orbW[2];
  if (rgb !== orbSpriteKey) { orbSpriteKey = rgb; orbSprite = makeSprite(rgb, 0.2); }
  const ga = 0.075 + 0.05 * env * sw, glow = ctx.createRadialGradient(360, 360, 10, 360, 360, 305 + 25 * env * sw);
  glow.addColorStop(0, `rgba(${rgb},${ga})`); glow.addColorStop(0.57, `rgba(${rgb},${ga * 0.47})`);
  glow.addColorStop(1, `rgba(${rgb},0)`); ctx.fillStyle = glow; ctx.fillRect(0, 0, 720, 720);

  const [wl, wt, wsp, we] = orbW, cos = Math.cos(h * 0.23), sin = Math.sin(h * 0.23);
  const amp = 8 + mic * 22, rings = onsets.map((o) => { const d = h - o; return { y: -1.15 + d * 2.1, g: Math.pow(1 - d / 1.2, 2) }; });
  ctx.globalCompositeOperation = "lighter";
  for (let i = 0; i < ORB_N; i++) {
    const p = SPHERE[i]; let x = 0, y = 0, z = 0, lit = 0;
    const rx = p.x * cos + p.z * sin, rz = p.z * cos - p.x * sin;
    const calm = wl + we;
    if (calm > 0.001) {
      const R = (192 + Math.sin(p.y * 11 - h * 3 + p.a * 0.03) * amp + Math.sin(p.a * 0.17 + h * 2) * 5) * calm;
      x += rx * R; y += p.y * R; z += rz * R;
    }
    if (wt > 0.001) {
      const B = p.u * TAU * 3 + h * 0.57, L = p.u * TAU * 8 - h * 0.48, Q = 137 + 41 * Math.cos(L) + 24 * p.sa;
      const ax = Q * Math.cos(B), ay = Q * Math.sin(B), d = 65 * Math.sin(L) + 17 * p.y;
      x += ax * wt; y += (d * 0.77 - ay * 0.52) * wt; z += (d * 0.52 + ay * 0.77) * wt;
    }
    if (wsp > 0.001) {
      const n = Math.sin(rx * 2.3 + h * 1.4) * Math.sin(p.y * 2.9 - h * 1.1) * Math.cos(rz * 2.1 + h * 0.8);
      let R = 184 + 16 * env + n * (5 + 19 * env) + Math.sin(p.y * 7 - h * 2.2) * 3 * env;
      for (const r of rings) { const q = (p.y - r.y) / 0.15, e = Math.exp(-q * q) * r.g; R += 7 * e; lit += e; }
      x += rx * R * wsp; y += p.y * R * wsp; z += rz * R * wsp; lit *= wsp;
    }
    const depth = (z + 240) / 480, persp = 850 / (850 - z);
    const alpha = Math.min(1, 0.22 + 0.72 * depth + 0.35 * env * sw + 1.2 * lit);
    const size = (0.8 + depth * 1.35) * (0.75 + 0.25 * Math.sin(p.a + h * 1.6)) * (1 + 0.5 * Math.min(1, lit)) * 9;
    const px = 360 + x * persp, py = 360 + y * persp;
    ctx.globalAlpha = alpha; ctx.drawImage(orbSprite, px - size / 2, py - size / 2, size, size);
    if (z > 75 || lit > 0.25) {
      const sa = (Math.max(alpha - 0.5, 0) + lit * 0.8) * 0.75;
      if (sa > 0) { const s2 = size * 0.2; ctx.globalAlpha = Math.min(1, sa); ctx.drawImage(orbSpark, px - s2 / 2, py - s2 / 2, s2, s2); }
    }
  }
  ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
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
  // Apps panel removed: connect apps by asking MAX ("MAX, connect to Cloudflare").
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
      approvalShown = false; activeApprovalId = null;
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
        if (j.connect?.action === "api_key" && MCP_APPS[j.connect.service]) {
          try {
            const link = new URL(j.connect.url);
            if (link.protocol === "https:" && MCP_APPS[j.connect.service][1].some((d) => link.hostname === d || link.hostname.endsWith("." + d))) {
              keyService = j.connect.service; keyLink.href = link.href;
              keyLink.textContent = "Create " + MCP_APPS[keyService][0] + " key";
              keybox.classList.remove("hidden"); stopWake();
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
let activeApprovalId = null, lastAnsweredApprovalId = null;
function askApproval(a) {
  const id = a.id || null;
  if (id && id === lastAnsweredApprovalId) return;
  if (approvalShown && activeApprovalId === id) return;
  activeApprovalId = id;
  approvalShown = true; setState("speaking");
  const detail = JSON.stringify(a.arguments, null, 2);
  show(`MAX wants to run ${a.tool}\n${detail}`, 0);
  choices.classList.remove("hidden");
}
async function answerApproval(allow) {
  if (!approvalShown || !pendingJob) return;
  const approval_id = activeApprovalId;
  lastAnsweredApprovalId = approval_id;
  approvalShown = false; activeApprovalId = null; choices.classList.add("hidden");
  setState("thinking"); show(allow ? "⋯ allowed" : "⋯ denied", 0);
  try { await api(`/api/job/${pendingJob}/approve`, { allow, approval_id }); }
  catch { lastAnsweredApprovalId = null; }
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
      ? "Connected · " + (current.tools || 0) +
        (current.requested_tools ? "/" + current.requested_tools : "") + " tools"
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

// ---------------------------------------------------------------- sound level & voice isolation → orb
// level (0..1) drives the orb animation; voiceLevel (0..1) measures speech-band (120–3800 Hz)
// energy after high-pass & presence filtering, isolating the user's voice from background noise.
const MIC = {
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    voiceIsolation: true,
  },
};
let noiseFloor = 0.025;
let level = 0, voiceLevel = 0;
let audioCtx = null, analyser = null, voiceAnalyser = null, levelSource = null, micStream = null;
let cleanStream = null, cleanDest = null, gateGain = null, gateHoldUntil = 0;
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
function buildVoiceChain(stream) {
  ensureAudio();
  const src = audioCtx.createMediaStreamSource(stream);
  connectLevel(src);

  // 1. Cut low-frequency room rumble, AC hum, desk bumps (< 95 Hz)
  const hp = audioCtx.createBiquadFilter();
  hp.type = "highpass"; hp.frequency.value = 95; hp.Q.value = 0.75;

  // 2. Boost speech consonant clarity (~2.4 kHz) and tame high-frequency hiss (> 7.2 kHz)
  const presence = audioCtx.createBiquadFilter();
  presence.type = "peaking"; presence.frequency.value = 2400; presence.Q.value = 1.1; presence.gain.value = 3.5;

  const lp = audioCtx.createBiquadFilter();
  lp.type = "lowpass"; lp.frequency.value = 7200; lp.Q.value = 0.7;

  // 3. Dedicated speech-band analyser (before the gate) so VAD sees clean vocal frequencies
  voiceAnalyser = audioCtx.createAnalyser();
  voiceAnalyser.fftSize = 512;
  voiceAnalyser.smoothingTimeConstant = 0.45;

  // 4. Adaptive noise-floor gate: ducks background voices/room noise between commands
  gateGain = audioCtx.createGain();
  gateGain.gain.value = 0.03;

  // 5. Broadcast-style voice compressor so soft and loud syllables transcribe evenly
  const comp = audioCtx.createDynamicsCompressor();
  comp.threshold.value = -20; comp.knee.value = 12; comp.ratio.value = 4;
  comp.attack.value = 0.003; comp.release.value = 0.15;

  src.connect(hp);
  hp.connect(presence);
  presence.connect(lp);
  lp.connect(voiceAnalyser);
  lp.connect(gateGain);
  gateGain.connect(comp);

  if (typeof audioCtx.createMediaStreamDestination === "function") {
    cleanDest = audioCtx.createMediaStreamDestination();
    comp.connect(cleanDest);
    cleanStream = cleanDest.stream;
  } else {
    cleanStream = stream;
  }
}
const buf = new Uint8Array(512);
const freqBuf = new Uint8Array(256);
function measureSpeechBand() {
  if (!voiceAnalyser || !audioCtx) return level;
  voiceAnalyser.getByteFrequencyData(freqBuf);
  const binHz = (audioCtx.sampleRate || 48000) / (voiceAnalyser.fftSize || 512);
  const lo = Math.max(1, Math.floor(120 / binHz));
  const hi = Math.min(freqBuf.length - 1, Math.ceil(3800 / binHz));
  let sum = 0, count = 0;
  for (let i = lo; i <= hi; i++) { sum += freqBuf[i]; count++; }
  return count ? Math.min(1, (sum / (count * 255)) * 2.2) : level;
}
function sampleLevel() {
  if (analyser && (state === "listening" || state === "speaking")) {
    analyser.getByteTimeDomainData(buf);
    let sum = 0; for (const v of buf) { const d = (v - 128) / 128; sum += d * d; }
    level += (Math.min(1, Math.sqrt(sum / buf.length) * 4) - level) * 0.35;
    if (state === "listening" && voiceAnalyser) {
      const sv = measureSpeechBand();
      voiceLevel += (sv - voiceLevel) * 0.45;
      if (gateGain && audioCtx) {
        const now = Date.now();
        const openAt = Math.max(0.08, noiseFloor * 2.6);
        if (voiceLevel >= openAt) gateHoldUntil = now + 220;
        const target = now <= gateHoldUntil ? 1.0 : 0.03;
        const tc = target > 0.5 ? 0.012 : 0.08;
        gateGain.gain.setTargetAtTime(target, audioCtx.currentTime, tc);
      }
    } else {
      voiceLevel = level;
    }
  } else {
    level *= 0.85;
    voiceLevel *= 0.85;
  }
  requestAnimationFrame(sampleLevel);
}
requestAnimationFrame(sampleLevel);
async function startMicLevel() {
  try {
    ensureAudio();
    if (!micStream) {
      micStream = await navigator.mediaDevices.getUserMedia(MIC);
      buildVoiceChain(micStream);
    }
  } catch {}
}
function stopMicLevel() {
  cleanStream = null; cleanDest = null; gateGain = null; voiceAnalyser = null;
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
// Adaptive end-of-speech: uses speech-band voiceLevel so background rumble/chatter
// doesn't hold the gate open, and requires sustained speech before locking on.
function speechGate(onDone, { spoke = false, maxWaitMs = 8000, maxMs = 30000 } = {}) {
  const started = Date.now(); let quietSince = null, speechMs = spoke ? 200 : 0;
  const timer = setInterval(() => {
    const now = Date.now();
    const v = voiceLevel || level;
    if (!spoke) noiseFloor += (Math.min(v, 0.18) - noiseFloor) * 0.05;
    const on = Math.max(0.085, noiseFloor * 2.8), off = Math.max(0.045, noiseFloor * 1.7);
    if (v > on) {
      speechMs += 100;
      if (speechMs >= 150) { spoke = true; quietSince = null; }
    } else {
      if (!spoke) speechMs = Math.max(0, speechMs - 50);
      if (spoke && v < off) quietSince = quietSince || now;
    }
    if (!spoke && now - started > maxWaitMs) { clearInterval(timer); onDone(false); }
    else if ((spoke && quietSince && now - quietSince > 1500) || now - started > maxMs) {
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
  recorder = new MediaRecorder(cleanStream || micStream, ropts);
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
  const t = { chunks: [], mime, started: Date.now(), wake: false, rec: new MediaRecorder(cleanStream || micStream, opts) };
  t.rec.ondataavailable = (e) => { if (e.data.size) t.chunks.push(e.data); };
  t.rec.start(250); tape = t;
}
function dropTape() { if (tape) { const t = tape; tape = null; try { t.rec.onstop = null; t.rec.stop(); } catch {} } }
setInterval(() => {   // keep the tape short: restart it during silence
  const v = voiceLevel || level;
  if (tape && !tape.wake && Date.now() - tape.started > 20000 && v < Math.max(0.045, noiseFloor * 1.7)) { dropTape(); startTape(); }
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
