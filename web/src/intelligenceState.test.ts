import assert from "node:assert/strict";
import { clearUpdateFilters, formatChangeValue, groupUpdates, notificationRows, parseIntelligenceFilters, serializeIntelligenceFilters, sortBySeverity, sortCategories, sourceTrialKey, summarizeFreshness, toggleCategory, updateNotificationRead } from "./intelligenceState";

const valid = parseIntelligenceFilters(new URLSearchParams("scope=monitor&monitor_id=7&window=30d&registry=NCT&severity=critical&category=timeline&page=3"));
assert.equal(valid.scope, "monitor"); assert.equal(valid.monitorId, "7"); assert.equal(valid.page, 3);
assert.equal(serializeIntelligenceFilters(valid).get("monitor"), "7");
const invalid = parseIntelligenceFilters(new URLSearchParams("scope=nope&window=forever&severity=urgent&page=-2"));
assert.equal(invalid.scope, "monitored"); assert.equal(invalid.window, "7d");
assert.equal(sourceTrialKey("NCT", "1"), "NCT:1"); assert.notEqual(sourceTrialKey("NCT", "1"), sourceTrialKey("ChiCTR", "1"));
const items: any[] = [
  { event_id: 1, source: "NCT", trial_id: "1", title: "A", detected_at: "2026-01-02 10:00", severity: "normal" },
  { event_id: 2, source: "NCT", trial_id: "1", title: "A", detected_at: "2026-01-02 09:00", severity: "critical" },
  { event_id: 3, source: "ChiCTR", trial_id: "1", title: "B", detected_at: "2026-01-01 09:00", severity: "important" },
];
const grouped = groupUpdates(items); assert.equal(grouped.length, 2); assert.equal(grouped[0].trials[0].items.length, 2); assert.equal(grouped[1].trials[0].key, "ChiCTR:1");
assert.deepEqual(sortBySeverity(items).map((x) => x.severity), ["critical", "important", "normal"]);
assert.deepEqual(sortCategories({ z: 1, a: 3, b: 3 }), [["a", 3], ["b", 3], ["z", 1]]);
assert.equal(formatChangeValue(null, "Not reported"), "Not reported"); assert.equal(formatChangeValue('["a","b"]', "missing"), "a, b");
assert.equal(formatChangeValue('', "missing"), "missing"); assert.equal(formatChangeValue(5, "missing"), "5");
const sponsors = '[{"name": "University of Aarhus", "role": "lead"}, {"name": "Aarhus University Hospital", "role": "collaborator"}]';
assert.equal(formatChangeValue(sponsors, "missing"), "University of Aarhus (lead), Aarhus University Hospital (collaborator)");
assert.equal(formatChangeValue([{ name: "Roche" }], "missing"), "Roche");
assert.equal(formatChangeValue([{ city: "Aarhus" }], "missing"), '{"city":"Aarhus"}');
assert.equal(formatChangeValue([{ name: "X" }, null, "", " Y "], "missing"), "X, Y");
assert.equal(formatChangeValue([[], []], "missing"), "missing"); assert.equal(formatChangeValue("[]", "missing"), "missing");
assert.deepEqual(summarizeFreshness([{ freshness: "fresh" }, { state: "stale" }, {}]), { fresh: 1, stale: 1, unknown: 1 });
const notices: any[] = [{ id: 1, read_at: null }, { id: 2, read_at: "x" }]; assert.equal(notificationRows(notices, true).length, 1); assert.equal(updateNotificationRead(notices, 1, true)[0].read_at, "now");
const cleared = clearUpdateFilters(new URLSearchParams("scope=monitor&monitor=7&window=30d&registry=NCT&severity=critical&category=x&page=4")); assert.equal(cleared.toString(), "scope=monitor&window=30d&monitor=7");
assert.equal(toggleCategory(null, "x"), "x"); assert.equal(toggleCategory("x", "x"), null);
console.log("intelligenceState tests: all passed");
