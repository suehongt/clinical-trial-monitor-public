import assert from "node:assert/strict";
import {
  buildGlobalSearchUrl, globalSearchFromQuery, navKeyFor,
  persistedLang, persistedSidebar, persistedTheme,
} from "./shellState";

assert.equal(navKeyFor("/dashboard"), "dashboard");
assert.equal(navKeyFor("/trials"), "trials");
assert.equal(navKeyFor("/trials/NCT/NCT1"), "trials");
assert.equal(navKeyFor("/trials", "?view=saved"), "saved");
assert.equal(navKeyFor("/trials", "?view=watched"), "trials");
assert.equal(navKeyFor("/updates"), "updates");
assert.equal(navKeyFor("/updates", "?view=notifications"), "updates");
assert.equal(navKeyFor("/projects/1"), "projects");
assert.equal(navKeyFor("/monitors/1"), "monitors");
assert.equal(navKeyFor("/briefing"), "briefing");
assert.equal(navKeyFor("/data-sources"), "sources");
assert.equal(navKeyFor("/help"), "help");

assert.equal(buildGlobalSearchUrl("heart failure", "keyword"), "/trials?q=heart%20failure");
assert.equal(buildGlobalSearchUrl("heart & lung", "natural"), "/trials?ask=heart%20%26%20lung");
assert.equal(buildGlobalSearchUrl("  ", "natural"), "/trials");
assert.deepEqual(globalSearchFromQuery(new URLSearchParams("q=abc")), { mode: "keyword", value: "abc" });
assert.deepEqual(globalSearchFromQuery(new URLSearchParams("ask=why")), { mode: "natural", value: "why" });

assert.equal(persistedTheme("bogus"), "light");
assert.equal(persistedLang("bogus"), "en");
assert.equal(persistedSidebar("bogus"), "expanded");
console.log("shellState tests: all passed");
