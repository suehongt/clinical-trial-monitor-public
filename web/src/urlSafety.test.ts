import { safeExternalUrl } from "./urlSafety";

const assert = (condition: boolean) => { if (!condition) throw new Error("unsafe URL accepted"); };
assert(safeExternalUrl("https://clinicaltrials.gov/study/NCT00000001") !== null);
assert(safeExternalUrl("http://example.test/study") !== null);
for (const value of ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
  "//evil.example", "not a url", null, 42]) {
  assert(safeExternalUrl(value) === null);
}
console.log("url safety tests: passed");
