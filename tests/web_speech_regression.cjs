// Run with Node: test actual browser speech code with fake audio and network.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../src/max_core/web/static/orb.js"), "utf8");
new vm.Script(source); // Entire browser script must parse.
const block = source.slice(source.indexOf("function speechChunks("), source.indexOf("// ---------------------------------------------------------------- voice in"));
const calls = [], spoken = [], shown = [];
let releaseOld;
const ctx = vm.createContext({
  AbortController, URL, Blob, setTimeout,
  window: {}, player: null, playerSrc: null, audio: null,
  audioRunning: async () => false,
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
  assert.ok(Array.from(chunks[0]).length <= 240);
  assert.ok(chunks.every(x => Array.from(x).length <= 900));
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
})().catch(err => { console.error(err); process.exitCode = 1; });
