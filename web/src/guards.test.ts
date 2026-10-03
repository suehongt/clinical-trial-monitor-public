/**
 * Contract tests for the runtime guards in ./guards.
 *
 * Two layers:
 *  1. REAL-DATA CONTRACT — every guard must accept the actual JSON published
 *     under public/data/ (read with node:fs; missing files are skipped with a
 *     clear message instead of failing).
 *  2. NEGATIVE CONTRACT — malformed fixtures (missing required fields, wrong
 *     array element types, non-object roots, empty object roots) must be
 *     rejected with `false`, never thrown on.
 *
 * Plain asserts via node:assert/strict — no test framework, no new deps.
 * Run with: npm run test (esbuild-bundles this file, then executes it).
 */

// node builtins resolve at runtime (esbuild --platform=node keeps them
// external); types come from the @types/node devDependency.
import { existsSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";

import {
  isDailyGroup,
  isDetailsMap,
  isDiseaseIndex,
  isNctStudyCheck,
  isWatchListData,
  isEndpointRow,
  isLiveSearchData,
  isEventsData,
  isMirrorsData,
  isReportData,
  isTrial,
  isTrialDetailData,
  isTrialDetailResponse,
} from "./guards";

/** `process` is a node global; declared here so the file type-checks. */
declare const process: { cwd(): string };

/* ------------------------------------------------------------- fixtures */

function trial(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "NCT0001",
    source: "NCT",
    title: "A trial",
    status: "Recruiting",
    conditions: ["heart failure"],
    sponsors: ["Some Sponsor"],
    countries: ["United States"],
    secondaryEndpoints: ["e1"],
    interventions: ["drug"],
    locations: ["site 1"],
    ...overrides,
  };
}

function report(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    profile: "hf",
    label: "心力衰竭",
    label_en: "Heart Failure",
    total: 1,
    trials: [trial()],
    ...overrides,
  };
}

function mirrors(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    total: 1,
    groups: [
      {
        masterId: "000d8960-847a-40c0-aa40-7409f760dec4",
        sources: ["ICTRP", "NCT"],
        trials: [{ source: "NCT", id: "NCT0001", title: "A trial", status: null, enrollment: 5, url: null }],
      },
    ],
    ...overrides,
  };
}

function events(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    total: 1,
    events: [
      {
        detected_at: "2026-01-01 00:00:00",
        field_name: "status",
        old_value: null,
        new_value: "Recruiting",
        id: "NCT0001",
        source: "NCT",
        title: "A trial",
      },
    ],
    ...overrides,
  };
}

function details(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "NCT:NCT0001": {
      summary: null,
      investigator: null,
      inclusion: "criteria",
      exclusion: null,
      endpointTable: {
        primary: [{ no: "1", indicator: "indicator", time: "week 12", type: "primary" }],
        secondary: [],
      },
    },
    ...overrides,
  };
}

/* -------------------------------------------------------- mini runner */

let passed = 0;

function test(name: string, fn: () => void): void {
  fn();
  passed += 1;
  console.log("ok - " + name);
}

/* --------------------------------------------- 1. real-data contract */

const DATA_CANDIDATES = [
  // bundled to node_modules/.cache/guards.test.mjs (npm run test)
  new URL("../../public/data", import.meta.url).pathname,
  // executed straight from src/ (tsx / ts-node style)
  new URL("../public/data", import.meta.url).pathname,
  // run with cwd = web
  process.cwd() + "/public/data",
];

const dataDir = DATA_CANDIDATES.find((d) => existsSync(d + "/index.json")) ?? null;

interface RealFile {
  file: string;
  guard: (v: unknown) => boolean;
  label: string;
}

const REAL_FILES: RealFile[] = [
  { file: "index.json", guard: isDiseaseIndex, label: "isDiseaseIndex" },
  { file: "hf.json", guard: isReportData, label: "isReportData" },
  { file: "mirrors.json", guard: isMirrorsData, label: "isMirrorsData" },
  { file: "events.json", guard: isEventsData, label: "isEventsData" },
  { file: "hf_details.json", guard: isDetailsMap, label: "isDetailsMap" },
];

let realChecked = 0;
let realSkipped = 0;

if (dataDir === null) {
  realSkipped = REAL_FILES.length;
  console.warn(
    `SKIP: real-data contract checks skipped — public/data/index.json not found in any of: ${DATA_CANDIDATES.join(", ")}`,
  );
} else {
  for (const { file, guard, label } of REAL_FILES) {
    const path = dataDir + "/" + file;
    if (!existsSync(path)) {
      realSkipped += 1;
      console.warn(`SKIP: ${file} not found at ${path} — ${label} real-data check skipped`);
      continue;
    }
    test(`real data: ${file} satisfies ${label}`, () => {
      let parsed: unknown;
      assert.doesNotThrow(() => {
        parsed = JSON.parse(readFileSync(path, "utf8"));
      }, `${file} is not valid JSON`);
      assert.equal(guard(parsed), true, `${file} rejected by ${label}`);
    });
    realChecked += 1;
  }
}

/* -------------------------------------------- 2. acceptance fixtures */

test("valid fixtures are accepted", () => {
  assert.equal(isTrial(trial()), true);
  assert.equal(isReportData(report()), true);
  assert.equal(isMirrorsData(mirrors()), true);
  assert.equal(isEventsData(events()), true);
  assert.equal(isDetailsMap(details()), true);
  assert.equal(isTrialDetailData({}), true); // every TrialDetailData field is optional
  assert.equal(isDetailsMap({}), true); // an empty Record<string, TrialDetailData> is valid
  assert.equal(isEndpointRow({ no: "1", indicator: "i", time: "t", type: "primary" }), true);
});

test("optional fields accept absent / null / typed values", () => {
  assert.equal(
    isTrial(trial({ enrollment: null, phase: null, scientificTitle: undefined, lastUpdated: "2026-01-01" })),
    true,
  );
  assert.equal(isTrial(trial({ enrollment: "60" })), false); // string where number|null allowed
  assert.equal(isTrialDetailData({ summary: null, investigator: null, endpointTable: null }), true);
});

function changeEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    detected_at: "2026-01-01 00:00:00",
    field_name: "status_id",
    old_value: "Not yet recruiting",
    new_value: "Recruiting",
    change_category: "status_change",
    id: "NCT0001",
    source: "NCT",
    title: "A trial",
    source_url: null,
    ...overrides,
  };
}

test("API trial-detail responses are accepted (TrialDetailData + changeHistory)", () => {
  assert.equal(isTrialDetailResponse({}), true); // bare TrialDetailData is valid
  assert.equal(
    isTrialDetailResponse({
      summary: "text",
      endpointTable: null,
      changeHistory: [changeEvent()],
    }),
    true,
  );
  assert.equal(
    isTrialDetailResponse({
      changeHistory: [], // empty history is still an array
    }),
    true,
  );
});

function dailyGroup(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    date: "2026-01-01",
    change_count: 1,
    new_count: 1,
    trials: [
      {
        id: "NCT0001",
        source: "NCT",
        title: "A trial",
        url: null,
        changes: [
          { field_name: "status_id", old_value: "2", new_value: "Recruiting", detected_at: "2026-01-01 08:00:00" },
        ],
      },
    ],
    new_trials: [
      { id: "CTR1", source: "CTR", title: "New trial", url: null, enrollment: 36, phase: null, first_crawled_at: "2026-01-01 09:00:00" },
    ],
    ...overrides,
  };
}

test("events payloads with an optional daily view are accepted", () => {
  assert.equal(isEventsData(events()), true); // static shape, no daily
  const withDaily = events();
  (withDaily as Record<string, unknown>).daily = [dailyGroup()];
  assert.equal(isEventsData(withDaily), true);
  assert.equal(isDailyGroup(dailyGroup()), true);
});

function liveSearch(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    keywords: "cardiomyopathy",
    live: true,
    cached: false,
    total: 1,
    trials: [trial()],
    ...overrides,
  };
}

test("LIVE search payloads are accepted (Trial-shaped results)", () => {
  assert.equal(isLiveSearchData(liveSearch()), true);
  assert.equal(isLiveSearchData(liveSearch({ trials: [], total: 0 })), true);
});

function watchList(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    watches: [
      {
        watch_id: 1,
        keyword: "CAR-T",
        created_at: "2026-01-01 00:00:00",
        total_matches: 3,
        recent_hits: 1,
        recent_matches: [
          { id: "NCT0009", source: "NCT", title: "CAR-T trial", url: null, first_crawled_at: null },
        ],
      },
    ],
    ...overrides,
  };
}

test("watch subscription payloads are accepted", () => {
  assert.equal(isWatchListData(watchList()), true);
  assert.equal(isWatchListData(watchList({ watches: [] })), true);
  assert.equal(isWatchListData(watchList({ watches: [{ watch_id: "x" }] })), false);
});

function studyCheck(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    generated_at: "2026-01-01 00:00:00 UTC",
    nct_id: "NCT0001",
    in_library: true,
    live_trial: trial(),
    diff: [{ field: "enrollment", local: 100, remote: 120 }],
    ...overrides,
  };
}

test("live single-trial check payloads are accepted", () => {
  assert.equal(isNctStudyCheck(studyCheck()), true);
  assert.equal(isNctStudyCheck(studyCheck({ diff: [] })), true);
  assert.equal(isNctStudyCheck(studyCheck({ in_library: "yes" })), false);
  assert.equal(isNctStudyCheck(studyCheck({ diff: [{ field: 7 }] })), false);
});

test("malformed LIVE search payloads are rejected", () => {
  assert.equal(isLiveSearchData(liveSearch({ total: "1" })), false);
  assert.equal(isLiveSearchData(liveSearch({ trials: [trial({ id: 7 })] })), false);
  assert.equal(isLiveSearchData(liveSearch({ generated_at: null })), false);
});

test("malformed daily groups are rejected", () => {
  assert.equal(isDailyGroup(dailyGroup({ date: 7 })), false);
  assert.equal(isDailyGroup(dailyGroup({ trials: "nope" })), false);
  assert.equal(isDailyGroup(dailyGroup({ new_trials: [{ id: "x" }] })), false);
  const bad = events();
  (bad as Record<string, unknown>).daily = [{ nope: true }];
  assert.equal(isEventsData(bad), false);
});

test("malformed changeHistory arrays are rejected", () => {
  assert.equal(isTrialDetailResponse({ changeHistory: "nope" }), false);
  assert.equal(isTrialDetailResponse({ changeHistory: [42] }), false);
  assert.equal(
    isTrialDetailResponse({ changeHistory: [changeEvent({ detected_at: null })] }),
    false,
  ); // required string field is null
  assert.equal(
    isTrialDetailResponse({ changeHistory: [changeEvent({ field_name: 7 })] }),
    false,
  );
});

/* -------------------------------------------- 3. negative: root shapes */

test("string / null / number / array roots are rejected", () => {
  assert.equal(isReportData("not an object"), false);
  assert.equal(isDiseaseIndex(null), false);
  assert.equal(isMirrorsData(42), false);
  assert.equal(isEventsData([1, 2, 3]), false); // arrays are not objects here
  assert.equal(isDetailsMap("x"), false);
  assert.equal(isTrial(undefined), false);
});

test("empty object roots are rejected (required fields missing)", () => {
  assert.equal(isReportData({}), false);
  assert.equal(isDiseaseIndex({}), false);
  assert.equal(isMirrorsData({}), false);
  assert.equal(isEventsData({}), false);
});

/* ---------------------------------- 4. negative: missing required fields */

test("missing required root fields are rejected", () => {
  const noProfile: Record<string, unknown> = report();
  delete noProfile.profile;
  assert.equal(isReportData(noProfile), false);

  const noTotal: Record<string, unknown> = report();
  delete noTotal.total;
  assert.equal(isReportData(noTotal), false);

  const noDiseases: Record<string, unknown> = { generated_at: "2026-01-01" };
  assert.equal(isDiseaseIndex(noDiseases), false);

  const noGroups: Record<string, unknown> = mirrors();
  delete noGroups.groups;
  assert.equal(isMirrorsData(noGroups), false);

  const noEvents: Record<string, unknown> = events();
  delete noEvents.events;
  assert.equal(isEventsData(noEvents), false);
});

test("missing required nested fields are rejected", () => {
  const noId: Record<string, unknown> = trial();
  delete noId.id;
  assert.equal(isReportData(report({ trials: [noId] })), false);

  const noFile: Record<string, unknown> = {
    key: "hf", label: "心力衰竭", label_en: "Heart Failure", total: 1,
  };
  assert.equal(isDiseaseIndex({ generated_at: "2026-01-01", diseases: [noFile] }), false);

  const noSources: Record<string, unknown> = {
    masterId: "m1",
    trials: [{ source: "NCT", id: "NCT0001", title: "A trial" }],
  };
  assert.equal(isMirrorsData(mirrors({ groups: [noSources] })), false);

  const noFieldName: Record<string, unknown> = {
    detected_at: "2026-01-01", old_value: null, new_value: null,
    id: "NCT0001", source: "NCT", title: "A trial",
  };
  assert.equal(isEventsData(events({ events: [noFieldName] })), false);

  const endpointMissingTime: Record<string, unknown> = { no: "1", indicator: "i", type: "primary" };
  assert.equal(isEndpointRow(endpointMissingTime), false);
});

/* --------------------------------- 5. negative: wrong array element types */

test("wrong array element types are rejected", () => {
  assert.equal(isTrial(trial({ conditions: ["ok", 5] })), false);
  assert.equal(isReportData(report({ trials: ["not a trial"] })), false);
  assert.equal(isReportData(report({ trials: [1, 2, 3] })), false);

  const badDisease: Record<string, unknown> = {
    key: "hf", label: "l", label_en: "en", total: "many", file: "hf.json", // total: string
  };
  assert.equal(isDiseaseIndex({ generated_at: "2026-01-01", diseases: [badDisease] }), false);

  const badSources = mirrors();
  (badSources.groups as Array<Record<string, unknown>>)[0].sources = [1, "NCT"];
  assert.equal(isMirrorsData(badSources), false);

  const badMirrorTrial = mirrors();
  (badMirrorTrial.groups as Array<Record<string, unknown>>)[0].trials = [{ source: "NCT", id: 7, title: "t" }];
  assert.equal(isMirrorsData(badMirrorTrial), false);

  const badDetails = details();
  const entry = badDetails["NCT:NCT0001"] as Record<string, unknown>;
  const table = entry.endpointTable as Record<string, unknown[]>;
  table.primary = [{ no: 1, indicator: "i", time: "t", type: "primary" }]; // no: number
  assert.equal(isDetailsMap(badDetails), false);

  const badValue: Record<string, unknown> = { "NCT:NCT0001": "just a string" };
  assert.equal(isDetailsMap(badValue), false);
});

/* ------------------------------------------------------------- summary */

console.log(
  `\nguards contract tests: ${passed} passed` +
    (dataDir ? ` · real-data files checked: ${realChecked}` : "") +
    (realSkipped ? ` · skipped (missing files): ${realSkipped}` : ""),
);
