// Run with Node: test actual browser speech code with fake audio and network.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../src/max_core/web/static/orb.js"), "utf8");
new vm.Script(source); // Entire browser script must parse.
const block = source.slice(source.indexOf("function speechChunks("), source.indexOf('replayBtn.addEventListener("click"'));
const calls = [], spoken = [], shown = [];
let releaseOld;
const ctx = vm.createContext({
  AbortController, URL, Blob, setTimeout, clearTimeout,
  window: {}, player: null, audio: null,
  busy: false, listening: false, transcribing: false,
  stopWake() {}, stopMicLevel() {}, resumeWake() {}, setState() {}, setActivity() {},
  show(text) { shown.push(text); },
  api: async (_path, {text}) => {
    calls.push(text);
    if (text === "old") return new Promise(resolve => { releaseOld = resolve; });
    return {blob: async () => new Blob([text])};
  },
  Audio: class {
    pause() {}
    play() {
      spoken.push(this.src);
      queueMicrotask(() => { if (this.onplaying) this.onplaying(); if (this.onended) this.onended(); });
      return Promise.resolve();
    }
  },
});
vm.runInContext(block, ctx);
(async () => {
  const original = "A short sentence. ".repeat(160) + "😀".repeat(1000);
  const chunks = ctx.speechChunks(original);
  assert.equal(chunks.join(""), original);
  assert.ok(Array.from(chunks[0]).length <= 140);
  assert.ok(chunks.every(x => Array.from(x).length <= 600));
  await ctx.say(original);
  assert.equal(calls.join(""), original);
  assert.equal(spoken.length, chunks.length);
  assert.equal(shown.at(-1), original);
  const old = ctx.say("old");
  await ctx.say("new");
  const count = spoken.length;
  releaseOld({blob: async () => new Blob(["old"])});
  await old;
  assert.equal(spoken.length, count, "stale synthesis must not play");
  assert.equal(shown.at(-1), "new");
  // Media may accept play() but never emit playing/ended. The watchdog must
  // release it and use the same exact text with the fallback voice.
  const fallback = [];
  ctx.window.speechSynthesis = ctx.speechSynthesis = {
    getVoices: () => [{name: "Samantha", lang: "en-US", localService: true}],
    cancel() {},
    speak(u) { fallback.push(u.text); queueMicrotask(() => { u.onstart(); u.onend(); }); },
  };
  ctx.window.SpeechSynthesisUtterance = ctx.SpeechSynthesisUtterance = class {
    constructor(text) { this.text = text; }
  };
  ctx.Audio = class { pause() {} play() { return Promise.resolve(); } };
  vm.runInContext("VOICE_TIMEOUTS.start = 10", ctx);
  await ctx.say("No silent playback");
  assert.deepEqual(fallback, ["No silent playback"]);
  // Verify clean silence ends a turn rapidly and resumed speech extends it.
  const gate = source.slice(source.indexOf("function speechGate("), source.indexOf("async function transcribeClip("));
  let now = 0, tick = null, completed = [];
  const vad = vm.createContext({
    Date: {now: () => now}, voiceLevel: 0, level: 0, voiceAnalyser: true, noiseFloor: 0.025,
    setInterval(fn) { tick = fn; return 1; }, clearInterval() { tick = null; },
  });
  vm.runInContext(gate, vad);
  const advance = (ms, volume) => {
    vad.voiceLevel = volume;
    for (let i = 0; i < ms / 50; i++) { now += 50; if (tick) tick(); }
  };
  vad.speechGate(heard => completed.push(heard));
  advance(200, 0.2); advance(350, 0);
  assert.equal(completed.length, 0);
  advance(50, 0);
  assert.deepEqual(completed, [true]);
  completed = [];
  vad.speechGate(heard => completed.push(heard));
  advance(200, 0.2); advance(200, 0); advance(200, 0.2);
  advance(400, 0); assert.equal(completed.length, 0);
  advance(200, 0); assert.deepEqual(completed, [true]);
})().catch(err => { console.error(err); process.exitCode = 1; });
