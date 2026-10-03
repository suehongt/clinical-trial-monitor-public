/**
 * Contract tests for the bilingual UI strings in ./i18n.
 *
 * Guarantees:
 *  1. STRINGS.en and STRINGS.zh define EXACTLY the same key set — a key added
 *     to one language without the other fails here (and also fails to compile
 *     thanks to `const zh: Record<StringKey, string>` in i18n.ts).
 *  2. No empty / whitespace-only values on either side.
 *  3. Placeholder tokens ({n}, {name}, ...) match between en and zh per key,
 *     so interpolation cannot silently break one language.
 *  4. Spot checks of known chrome strings.
 *
 * Plain asserts via node:assert/strict — no test framework, no new deps.
 * Run with: npm run test (esbuild-bundles this file, then executes it).
 */

// node builtins resolve at runtime (esbuild --platform=node keeps them
// external); types come from the @types/node devDependency.
import assert from "node:assert/strict";

import { STRINGS } from "./i18n";

/* -------------------------------------------------------- mini runner */

let passed = 0;

function test(name: string, fn: () => void): void {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

/* ----------------------------------------------------------- contract */

test("en and zh dictionaries have exactly the same key set", () => {
  const enKeys = Object.keys(STRINGS.en).sort();
  const zhKeys = Object.keys(STRINGS.zh).sort();
  assert.equal(enKeys.length > 0, true, "en dictionary must not be empty");
  assert.deepEqual(zhKeys, enKeys);
});

test("no empty or whitespace-only string values", () => {
  for (const [key, value] of Object.entries(STRINGS.en)) {
    assert.notEqual(value.trim(), "", `en.${key} is empty`);
  }
  for (const [key, value] of Object.entries(STRINGS.zh)) {
    assert.notEqual(value.trim(), "", `zh.${key} is empty`);
  }
});

test("placeholder tokens match between en and zh for every key", () => {
  const placeholders = (s: string): string[] => (s.match(/\{[a-z]+\}/g) ?? []).sort();
  for (const key of Object.keys(STRINGS.en)) {
    assert.deepEqual(placeholders(STRINGS.zh[key]), placeholders(STRINGS.en[key]), `placeholder drift at ${key}`);
  }
});

test("spot checks: known chrome keys", () => {
  assert.equal(STRINGS.en["app.title"], "Clinical Trial Monitor");
  assert.equal(STRINGS.en["stat.total"], "Total trials");
  assert.equal(STRINGS.en["search.placeholder"], "Search title, condition, sponsor, ID…");
  assert.equal(STRINGS.en["more.load"], "Load more ({n} remaining)");
  assert.equal(STRINGS.zh["status.recruiting"], "招募中");
  assert.equal(STRINGS.zh["card.details"], "查看详情 →");
});

/* ------------------------------------------------------------- summary */

console.log(`\ni18n contract tests: ${passed} passed · keys: ${Object.keys(STRINGS.en).length}`);
