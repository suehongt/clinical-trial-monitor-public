import { statusKey } from "./labels";

function equal(actual: string, expected: string, label: string): void {
  if (actual !== expected) {
    throw new Error(`${label}: expected ${expected}, got ${actual}`);
  }
}

const cases: Array<[string, string]> = [
  ["Recruiting", "recruiting"],
  ["正在招募", "recruiting"],
  ["Not yet recruiting", "other"],
  ["Active, not recruiting", "other"],
  ["尚未招募", "other"],
  ["招募暂停", "other"],
  ["Completed", "completed"],
  ["Terminated", "terminated"],
];

for (const [status, expected] of cases) {
  equal(statusKey(status), expected, status);
}

console.log(`labels tests: ${cases.length} passed`);
