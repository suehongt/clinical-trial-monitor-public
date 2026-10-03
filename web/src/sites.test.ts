import { parseSites } from "./sites";

const assert = (condition: unknown, message: string) => { if (!condition) throw new Error(message); };

// JSON array of objects (NCT / CTIS / ISRCTN; fields may be null)
const structured = parseSites([
  '[{"facility": "NIH Clinical Center", "city": "Bethesda", "state": "Maryland", "country": "United States", "status": ""}]',
  '[{"facility": "-", "city": null, "state": null, "country": "United Kingdom"}]',
]);
assert(structured.length === 2, "one row per object entry");
assert(structured[0].facility === "NIH Clinical Center" && structured[0].city === "Bethesda", "structured fields map");
assert(structured[1].city === "" && structured[1].state === "", "null fields become empty strings");

// JSON array of plain facility names (CTR)
const ctr = parseSites(['["中国人民解放军第302医院", "北京大学第一医院"]']);
assert(ctr.length === 2 && ctr[0].facility === "中国人民解放军第302医院", "plain facility names stay one per row");
assert(ctr[0].city === "" && ctr[0].country === "", "no fabricated fields for facility-only rows");

// ChiCTR: several sites concatenated into one labeled blob, no separator
const chictrBlob = "国家： 中国 省(直辖市)： 山东省 市(区县)： Country： China Province： Shandong City： " +
  "单位(医院)： 山东省立医院（山东省儿童医院） 单位级别： 三级甲等 Institution hospital： Shandong Provincial Hospital " +
  "Level of the institution： Tertiary A " +
  "国家： 中国 省(直辖市)： 贵州省 市(区县)： 凯里市 Country： China Province： Guizhou City： Kaili " +
  "单位(医院)： 黔东南州人民医院 单位级别： 三甲 Institution hospital： Qiandongnan People's Hospital";
const chictr = parseSites([JSON.stringify([chictrBlob])]);
assert(chictr.length === 2, `ChiCTR blob splits per site, got ${chictr.length}`);
assert(chictr[0].facility === "山东省立医院（山东省儿童医院）", "facility from 单位(医院) label");
assert(chictr[0].state === "山东省" && chictr[0].country === "中国", "province/country from Chinese labels");
assert(chictr[0].city === "", "empty 市(区县) does not swallow the next label");
assert(chictr[1].city === "凯里市", "city from 市(区县) when present");
assert(chictr[1].facility === "黔东南州人民医院", "second site parsed independently");

// "Facility, City, Country" plain string keeps facility as the first part
const csv = parseSites(["Mass General Hospital, Boston, United States"]);
assert(csv[0].facility === "Mass General Hospital" && csv[0].city === "Boston" && csv[0].country === "United States",
  "comma string maps facility/city/country without duplicating into facility");

// degenerate input degrades instead of throwing
const bare = parseSites(["国家： 中国 单位(医院)： 北京协和医院"]);
assert(bare.length === 1 && bare[0].facility === "北京协和医院" && bare[0].country === "中国", "labeled blob without English labels");
assert(parseSites(["", "   "]).length === 0, "blank entries are dropped");

console.log("sites tests: all passed");
