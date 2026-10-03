import assert from "node:assert/strict";

import { lastRoute, rememberRoute } from "./router";

const values = new Map<string, string>();
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: {
    getItem(key: string) { return values.get(key) ?? null; },
    setItem(key: string, value: string) { values.set(key, value); },
  },
});

assert.equal(lastRoute(), null, "a first visit has no route to restore");

rememberRoute("/trials?q=myocarditis&page=2");
assert.equal(lastRoute(), "/trials?q=myocarditis&page=2",
  "the complete search URL is restored");

rememberRoute("/");
assert.equal(lastRoute(), "/trials?q=myocarditis&page=2",
  "the bare root does not erase the last useful screen");

rememberRoute("//example.com/escape");
assert.equal(lastRoute(), "/trials?q=myocarditis&page=2",
  "a protocol-relative external URL is never persisted");

rememberRoute("/api/trials/search?q=hidden");
assert.equal(lastRoute(), "/trials?q=myocarditis&page=2",
  "an API URL is never restored as an application screen");

console.log("router tests: all passed");
