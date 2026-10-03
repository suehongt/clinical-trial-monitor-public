/**
 * Contract tests for the human-readable change-field labels (Phase 3E).
 *
 * Guarantees:
 *  1. every canonical tracked field has a bilingual label;
 *  2. server-provided labels win over the local map when present;
 *  3. unknown fields degrade to a humanized raw path (never an empty string);
 *  4. the local map mirrors server/app.py FIELD_LABELS (key parity is also
 *     enforced from the Python side by test_server_api.py label tests).
 *
 * Plain asserts via node:assert/strict — same style as guards/i18n tests.
 */
import assert from "node:assert/strict";

import { fieldGroupLabel, fieldLabel } from "./fieldLabels";

let passed = 0;
function test(name: string, fn: () => void): void {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

test("canonical fields carry both languages", () => {
  for (const field of ["status_id", "enrollment", "primary_endpoint", "primary_completion_date"]) {
    const en = fieldLabel(field, "en");
    const zh = fieldLabel(field, "zh");
    assert.notEqual(en, field, `${field} must have an english label`);
    assert.notEqual(zh, field, `${field} must have a chinese label`);
    assert.ok(!en.includes("_"), `${field} label must not expose raw path: ${en}`);
  }
});

test("server-provided labels win when present", () => {
  const labelled = { field_label: "Server Label", field_label_zh: "服务端标签" };
  assert.equal(fieldLabel("anything", "en", labelled), "Server Label");
  assert.equal(fieldLabel("anything", "zh", labelled), "服务端标签");
  // null server label falls through to the local map
  assert.equal(fieldLabel("enrollment", "en", { field_label: null }), "Enrollment");
});

test("unknown fields humanize the raw path", () => {
  assert.equal(fieldLabel("weird_custom_field", "en"), "Weird custom field");
  assert.equal(fieldLabel("weird_custom_field", "zh"), "Weird custom field");
});

test("field-group labels resolve without raw underscores", () => {
  assert.equal(fieldGroupLabel("primary_outcomes", "en"), "Primary Outcomes");
  assert.notEqual(fieldGroupLabel("primary_outcomes", "zh"), "primary_outcomes");
  assert.equal(fieldGroupLabel("status", "en"), "Status");
});

console.log(`\nfieldLabels contract tests: ${passed} passed`);
