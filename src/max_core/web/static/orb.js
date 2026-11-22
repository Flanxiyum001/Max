"use strict";
// MAX web orb. Same behaviour as the desktop orb, in a browser.
const $ = (id) => document.getElementById(id);
const canvas = $("orb"), ctx = canvas.getContext("2d");
const answerEl = $("answer"), input = $("input"), hint = $("hint");
const loginForm = $("login"), passcode = $("passcode"), choices = $("choices");

let state = "idle";            // idle | thinking | speaking | listening | error
let energy = 0, t = 0;
let busy = false, authed = false, pendingJob = null, hideTimer = null;
let audio = null, recognition = null, listening = false;

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
  const breathe = 1 + 0.012 * Math.sin(t * 1.3) + 0.02 * energy * Math.sin(t * 6);
  const r = Math.min(w, h) * 0.24 * breathe;
  ctx.clearRect(0, 0, w, h);

  // halo
  let g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r * 1.28);
  const a = (110 + 90 * energy) / 255;
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
  slow_down: "Too many requests, give me a second.",
};

// ---------------------------------------------------------------- login
function showLogin() {
  authed = false; loginForm.classList.remove("hidden"); input.classList.add("hidden");
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
  authed = true;
  hint.textContent = "type to talk · space or tap the orb to speak";
  if (!s.model_ready) { setState("error", 3000); show(ERRORS.model_not_configured); }
  else show("Hello. I'm here.", 4000);
}

// ---------------------------------------------------------------- conversation
async function ask(text, spoken) {
  if (busy || !text.trim()) return;
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
        setState("speaking"); show(j.answer);
        if (spoken) await say(j.answer); else setState("speaking", 2500);
      } else { setState("error", 2500); show(ERRORS[j.error] || ERRORS.model_unavailable); }
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
$("allow").onclick = () => answerApproval(true);
$("deny").onclick = () => answerApproval(false);

// ---------------------------------------------------------------- voice out
async function say(text) {
  try {
    const res = await api("/api/speak", { text: text.slice(0, 2000) });
    const url = URL.createObjectURL(await res.blob());
    stopSpeaking(); audio = new Audio(url); setState("speaking");
    audio.onended = () => { URL.revokeObjectURL(url); audio = null; if (!busy) setState("idle"); };
    await audio.play();
  } catch { setState("speaking", 2500); }
}
function stopSpeaking() { if (audio) { audio.pause(); audio = null; if (!busy) setState("idle"); } }

// ---------------------------------------------------------------- voice in (browser speech recognition)
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
function toggleListening() {
  if (!authed || busy) return;
  if (!SR) { setState("error", 2500); show("This browser can't listen. Try Chrome or Edge.", 4000); return; }
  if (listening) { recognition && recognition.stop(); return; }
  stopSpeaking();
  recognition = new SR(); recognition.lang = navigator.language || "en-US";
  recognition.interimResults = true; recognition.continuous = false;
  let finalText = "";
  recognition.onresult = (e) => {
    let interim = "";
    for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim += r[0].transcript));
    show(`“${finalText || interim}”`, 0);
  };
  recognition.onerror = (e) => { listening = false; setState("error", 2500);
    show(e.error === "not-allowed" ? "Microphone permission was denied." : "I didn't catch that.", 3500); };
  recognition.onend = () => {
    listening = false;
    if (finalText.trim()) ask(finalText.trim(), true);
    else if (state === "listening") { setState("idle"); show("I didn't hear anything.", 3000); }
  };
  listening = true; setState("listening"); show("Listening…", 0); recognition.start();
}
canvas.addEventListener("click", toggleListening);

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
