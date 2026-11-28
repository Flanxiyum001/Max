// Exercise the connection UI in isolation without network or OAuth credentials.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/max_core/web/static/orb.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "../src/max_core/web/static/index.html"), "utf8");
new vm.Script(source);
const elements = {};
function element(id) {
  if (elements[id]) return elements[id];
  const classes = new Set(["hidden"]);
  return elements[id] = {textContent: "", disabled: false, classList: {
    add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x),
    toggle: (x, force) => { if (force === undefined ? !classes.has(x) : force) classes.add(x); else classes.delete(x); }
  }};
}
const context = vm.createContext({
  MCP_APPS: {github: ["GitHub", ["github.com"]], figma: ["Figma", ["figma.com"]]},
  pendingConnectAction: null, pendingConnectService: null,
  githubAuth: element("github-auth"), githubSetup: element("github-setup"),
  githubCallback: element("github-callback"), githubSetupStatus: element("github-setup-status"),
  location: {origin: "https://max.example.test"}, $: element, stopWake() {}, resumeWake() {},
  show() {}, api: async () => ({json: async () => ({github: {configured: true, connected: false}})}),
});
vm.runInContext(source.slice(source.indexOf("function clearConnectionAction()"), source.indexOf("async function authorizeApp(")), context);
(async () => {
  assert.ok(context.presentConnectionAction({service: "github", action: "needs_configuration"}));
  assert.equal(context.githubAuth.textContent, "Set up GitHub");
  assert.equal(context.githubAuth.classList.contains("hidden"), false);
  assert.equal(context.githubSetup.classList.contains("hidden"), false);
  assert.equal(context.githubCallback.textContent, "https://max.example.test/api/connect/github/callback");
  await context.refreshGithubSetup();
  assert.equal(context.githubAuth.textContent, "Authorize GitHub");
  assert.equal(context.githubSetup.classList.contains("hidden"), true);
  context.clearConnectionAction();
  assert.equal(context.githubAuth.classList.contains("hidden"), true);
  assert.equal(context.presentConnectionAction({service: "unknown", action: "authorize"}), false);
  assert.ok(context.presentConnectionAction({service: "figma", action: "authorize"}));
  assert.equal(context.githubAuth.textContent, "Authorize Figma");
  assert.ok(html.includes('href="https://github.com/settings/applications/new"'));
  assert.ok(html.includes('id="github-check-setup"'));
})().catch(error => { console.error(error); process.exitCode = 1; });
