// Pure-function tests for api.ts url helpers (the hook layer needs a DOM).
// esbuild bundles ./api including its react import — fine under node.
import assert from "node:assert/strict";
import { refreshableUrl } from "./api";

// tick 0 keeps the plain url: persistable in localStorage and shared across
// pages (this is what makes "no new data → paint instantly" work)
assert.equal(refreshableUrl("/api/watches", 0), "/api/watches");

// later ticks append ?refresh=N (or &… for urls that already carry params):
// a distinct url that forces the fetch, memory-only via STORE_SKIP
assert.equal(refreshableUrl("/api/watches", 3), "/api/watches?refresh=3");
assert.equal(refreshableUrl("/api/updates?scope=all", 1), "/api/updates?scope=all&refresh=1");

console.log("api.test.ts OK");
import "./intelligenceState.test";
