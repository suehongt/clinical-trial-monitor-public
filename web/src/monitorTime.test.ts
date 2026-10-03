/**
 * Round-trip tests for monitorTime — no DOM, no timezone assumptions:
 * conversions are checked by comparing parsed instants, so the suite passes
 * in any TZ the CI/dev machine happens to run in.
 */
import { strict as assert } from "node:assert";
import { utcStampToInputValue, inputValueToUtcStamp } from "./monitorTime";

// datetime-local shape, minute precision
const local = utcStampToInputValue("2026-09-29 14:19:00");
assert.match(local, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);

// round trip preserves the instant; the helper returns a naive UTC stamp,
// so it must be re-parsed with an explicit Z (Date.parse would otherwise
// read it as viewer-local and shift by the TZ offset)
const back = inputValueToUtcStamp(local);
assert.ok(back);
assert.equal(Date.parse(back + "Z"), Date.parse("2026-09-29T14:19:00Z"));

// invalid / absent input degrades to "" / null rather than throwing
assert.equal(utcStampToInputValue(null), "");
assert.equal(utcStampToInputValue(undefined), "");
assert.equal(utcStampToInputValue("garbage"), "");
assert.equal(inputValueToUtcStamp("garbage"), null);
assert.equal(inputValueToUtcStamp(""), null);

console.log("monitorTime.test.ts OK");
