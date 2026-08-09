#!/usr/bin/env node

import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const manifest = JSON.parse(fs.readFileSync(new URL("./manifest.json", import.meta.url), "utf8"));
const command = manifest.commands["search-selected-text"];
assert.equal(manifest.version, "2.10");
assert.equal(command.suggested_key.mac, "Command+Shift+L");
assert.equal(command.suggested_key.default, undefined);
for (const site of ["enterjoy", "tcafe21", "pastebin.com"]) {
  assert.ok(manifest.content_scripts[0].include_globs.some((glob) => glob.includes(site)));
}

const listeners = { messages: [] };
class MockElement {}
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
  fs.readFileSync(new URL("./content.js", import.meta.url), "utf8"),
  contentContext,
);

let shownQuery = "";
contentContext.showSelectionSearch = (query) => { shownQuery = query; };
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
assert.equal(typeof commandListener, "function");
commandListener("search-selected-text", { id: 42 });
assert.deepEqual(sentMessage, {
  id: 42,
  message: { action: "showShortcutSelectionSearch" },
});

console.log("context search check ok: sites=3 shortcut=Command+Shift+L");
