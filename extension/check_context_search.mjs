#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const manifest = JSON.parse(fs.readFileSync(new URL("./manifest.json", import.meta.url), "utf8"));
const contentSource = fs.readFileSync(new URL("./content.js", import.meta.url), "utf8");
const command = manifest.commands["search-selected-text"];
assert.equal(manifest.version, "2.11");
assert.equal(command.suggested_key.mac, "Command+Shift+L");
assert.equal(command.suggested_key.default, undefined);
const supportedSites = ["enterjoy", "tcafe21", "pastebin.com", "chating.wiki"];
for (const site of supportedSites) {
  assert.ok(manifest.content_scripts[0].include_globs.some((glob) => glob.includes(site)));
}
assert.match(contentSource, /createElement\("dialog"\)/);
assert.match(contentSource, /\.showModal\(\)/);
assert.match(contentSource, /\.group-material-copy > strong/);
assert.match(contentSource, /\.cw-board-item__title > strong/);

const listeners = { messages: [] };
class MockElement {
  closest() { return null; }
}
const contentContext = {
  console,
  Element: MockElement,
  Map,
  Promise,
  window: {
    getSelection() {
      return { isCollapsed: true, containsNode: () => false, toString: () => "" };
    },
  },
  document: {
    querySelectorAll: () => [],
    addEventListener(type, listener) {
      listeners[type] = listener;
    },
    createElement() {
      throw new Error("modal creation should be mocked before use");
    },
    body: { appendChild() {} },
    documentElement: { appendChild() {} },
  },
  chrome: {
    storage: { onChanged: { addListener() {} } },
    runtime: {
      lastError: null,
      onMessage: { addListener(listener) { listeners.messages.push(listener); } },
      sendMessage() {},
    },
  },
  setInterval() {},
};
vm.runInNewContext(
  contentSource,
  contentContext,
);

assert.equal(
  contentContext.normalizeSiteTitleText(
    { matches: (selector) => selector === ".group-material-copy > strong" },
    "철혈로정복하다+1-734+완.txt",
  ),
  "철혈로정복하다 1-734 완.txt",
);
assert.equal(
  contentContext.normalizeSiteTitleText({ matches: () => false }, "C++ 개발자 1-10.txt"),
  "C++ 개발자 1-10.txt",
);

let tooltipAttribute = null;
let tooltipAppended = null;
const tooltipMock = {
  className: "",
  style: {},
  popoverOpen: false,
  setAttribute(name, value) { tooltipAttribute = [name, value]; },
  removeAttribute() {},
  showPopover() { this.popoverOpen = true; },
  hidePopover() { this.popoverOpen = false; },
  matches(selector) { return selector === ":popover-open" && this.popoverOpen; },
};
contentContext.document.createElement = () => tooltipMock;
contentContext.document.documentElement.appendChild = (element) => {
  tooltipAppended = element;
};
const tooltip = contentContext.getSharedTooltip();
contentContext.showSharedTooltip({
  getBoundingClientRect: () => ({ left: 120, bottom: 80 }),
}, tooltip);
assert.deepEqual(tooltipAttribute, ["popover", "manual"]);
assert.equal(tooltipAppended, tooltipMock);
assert.equal(tooltip.style.left, "120px");
assert.equal(tooltip.style.top, "85px");
assert.equal(tooltip.popoverOpen, true);
contentContext.hideSharedTooltip(tooltip);
assert.equal(tooltip.popoverOpen, false);
assert.equal(tooltip.style.display, "none");

const modal = {
  root: { style: {} },
  closeBtn: { focus() {} },
  queryEl: { textContent: "" },
  resultsEl: { innerHTML: "" },
  webStatsWrap: { style: {} },
  webStatsEl: { innerHTML: "" },
};
let webStatsRequest = "";
let renderedWebStats = null;
contentContext.getSelectionSearchModal = () => modal;
contentContext.requestSearch = () => Promise.resolve({ results: [] });
contentContext.requestWebStats = (query) => {
  webStatsRequest = query;
  return Promise.resolve({
    query: { title: query },
    results: [{
      platform: "카카오",
      status: "ok",
      metrics: [
        { label: "조회", value: "1.2만" },
        { label: "추천", value: "37" },
      ],
    }],
  });
};
contentContext.renderTooltip = () => {};
contentContext.renderWebStatsTooltip = (_target, response) => {
  renderedWebStats = response;
};
contentContext.showSelectionSearch("  메타   제목  ", { includeWebStats: true });
await Promise.resolve();
await Promise.resolve();
assert.equal(webStatsRequest, "메타 제목");
assert.equal(modal.webStatsWrap.style.display, "block");
assert.equal(renderedWebStats.results[0].metrics[0].label, "조회");
assert.equal(renderedWebStats.results[0].metrics[1].label, "추천");

let shownQuery = "";
let shownOptions = null;
contentContext.showSelectionSearch = (query, options) => {
  shownQuery = query;
  shownOptions = options;
};
contentContext.window.getSelection = () => ({
  isCollapsed: false,
  containsNode: () => true,
  toString: () => "  선택한   제목  ",
});
let shortcutResponse = null;
for (const listener of listeners.messages) {
  listener({ action: "showShortcutSelectionSearch" }, {}, (value) => { shortcutResponse = value; });
}
assert.equal(shortcutResponse && shortcutResponse.ok, true);
assert.equal(shownQuery, "선택한 제목");
assert.equal(shownOptions, undefined);

let contextMenuPrevented = false;
let contextMenuStopped = false;
listeners.contextmenu({
  target: new MockElement(),
  metaKey: true,
  preventDefault() { contextMenuPrevented = true; },
  stopPropagation() { contextMenuStopped = true; },
});
assert.equal(contextMenuPrevented, true);
assert.equal(contextMenuStopped, true);
assert.equal(shownQuery, "선택한 제목");
assert.equal(shownOptions && shownOptions.includeWebStats, true);

let createdMenu = null;
let commandListener = null;
let sentMessage = null;
globalThis.chrome = {
  runtime: {
    lastError: null,
    onInstalled: { addListener() {} },
    onStartup: { addListener() {} },
    onMessage: { addListener() {} },
  },
  contextMenus: {
    remove(_id, callback) { callback(); },
    create(properties, callback) { createdMenu = properties; callback(); },
    onClicked: { addListener() {} },
  },
  commands: {
    onCommand: { addListener(listener) { commandListener = listener; } },
  },
  tabs: {
    sendMessage(id, message, callback) {
      sentMessage = { id, message };
      callback();
    },
    query(_query, callback) { callback([]); },
  },
  storage: { onChanged: { addListener() {} } },
};
await import(`./background.js?context-search-check=${Date.now()}`);
assert.equal(createdMenu.title, "이 제목으로 중복 확인");
assert.ok(createdMenu.documentUrlPatterns.includes("*://chating.wiki/*"));
assert.ok(createdMenu.documentUrlPatterns.includes("*://*.chating.wiki/*"));
assert.equal(typeof commandListener, "function");
commandListener("search-selected-text", { id: 42 });
assert.deepEqual(sentMessage, {
  id: 42,
  message: { action: "showShortcutSelectionSearch" },
});

console.log(`context search check ok: sites=${supportedSites.length} shortcut=Command+Shift+L`);
