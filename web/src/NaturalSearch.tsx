import { useEffect, useState } from "react";
import { mutateJson } from "./api";
import { navigate } from "./router";
import { emptySearchState, searchStateToQuery, type SearchState } from "./searchState";
import type { Lang, Translate } from "./i18n";

type Proposal = {
  original_query: string;
  rule: Record<string, string[]>;
  version: string;
  provider: string;
  confidence: string;
  ambiguities: { term: string; suggestion: string }[];
  unsupported: string[];
  explanations: string[];
};
const fields: Record<string, keyof SearchState> = {
  query: "q", registries: "registries", statuses: "statuses",
  study_types: "studyTypes", phase: "phase", country: "country",
  condition: "condition", intervention: "intervention", sponsor: "sponsor",
};
const isProposal = (v: unknown): v is Proposal => {
  if (!v || typeof v !== "object") return false;
  const p = v as Partial<Proposal>;
  return typeof p.original_query === "string" && !!p.rule && typeof p.rule === "object"
    && Array.isArray(p.ambiguities) && Array.isArray(p.unsupported)
    && Array.isArray(p.explanations);
};

const zhMessages: Record<string, string> = {
  "China is a country filter, not the ChiCTR registry.": "中国被解析为国家/地区，不是 ChiCTR 注册平台。",
  "Phase 2 or Phase 3 (including combined-phase records).": "解析为 II 期或 III 期，包含联合分期记录。",
  "II/III is treated as Phase 2 or Phase 3, including combined-phase records.": "II/III 被解析为 II 期或 III 期，包含联合分期记录。",
  "Recent has no fixed time window.": "“近期”没有固定的时间范围。",
  "“active” may mean Recruiting or Active, not recruiting.": "“active”可能指招募中，也可能指活跃但不再招募。",
  "“open” may describe recruitment or trial availability.": "“open”可能描述招募状态或试验可用性。",
  "A Chinese registry may refer to ChiCTR or CTR.": "“中国注册平台”可能指 ChiCTR 或 CTR。",
  "Europe is a region, not a supported country rule.": "欧洲是区域，目前不支持区域筛选。",
  "Specify a time window; update-date filtering is currently unavailable.": "请明确时间范围；当前尚不支持更新时间筛选。",
  "Choose a recruitment status explicitly.": "请明确选择招募状态。",
  "Specify Recruiting if recruitment is intended.": "如果指招募，请明确选择“Recruiting”。",
  "Choose ChiCTR or CTR explicitly.": "请明确选择 ChiCTR 或 CTR。",
};
function displayMessage(value: string, lang: Lang): string {
  if (lang === "en") return value;
  if (zhMessages[value]) return zhMessages[value];
  return value.replace("(update-date filtering is unavailable)", "（当前不支持更新时间筛选）")
    .replace("(no deterministic filter or ranking)", "（无对应的确定性筛选或排序）")
    .replace("(regional country grouping is unavailable)", "（当前不支持区域筛选）");
}

export default function NaturalSearch({ text, t, lang }: { text: string; t: Translate; lang: Lang }) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [rule, setRule] = useState<Record<string, string[]>>({});
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  useEffect(() => {
    let alive = true;
    setBusy(true); setError(""); setProposal(null); setAcknowledged(false);
    mutateJson<Proposal>("/api/search/interpret", "POST", { text }, isProposal)
      .then((p) => { if (alive) { setProposal(p); setRule(p.rule); } })
      .catch(() => { if (alive) setError(t("nl.error")); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [text, t]);
  const keyword = () => navigate(`/trials?${searchStateToQuery({ ...emptySearchState(), q: text })}`);
  const run = () => {
    const state = emptySearchState();
    for (const [key, values] of Object.entries(rule)) {
      const field = fields[key];
      if (!field) continue;
      if (field === "q") state.q = values.join(" ");
      else if (field !== "sort" && field !== "page") (state[field] as string[]) = values;
    }
    const p = new URLSearchParams(searchStateToQuery(state));
    p.set("nl", text);
    if (proposal?.version) p.set("iv", proposal.version);
    navigate(`/trials?${p}`);
  };
  const remove = (key: string, value: string) => setRule((current) => ({
    ...current, [key]: (current[key] ?? []).filter((x) => x !== value),
  }));
  const edit = (key: string, index: number, value: string) => setRule((current) => ({
    ...current, [key]: current[key].map((x, i) => i === index ? value : x),
  }));
  return <section className="nl-panel" aria-label={t("nl.title")}>
    <h1>{t("nl.title")}</h1>
    <p>{t("nl.original")}: <q>{text}</q></p>
    <p className="form-hint">{t("nl.provenance")}</p>
    {busy && <p role="status">{t("nl.interpreting")}</p>}
    {error && <p role="alert">{error}</p>}
    {proposal && <>
      <div className="nl-rule" role="group" aria-label={t("nl.filters")}>
        {Object.entries(rule).map(([key, values]) => values.map((value, index) => <label className="nl-chip" key={`${key}-${index}`}>
          <span>{t(`search.rule.${key}` as never)}</span>
          <input aria-label={`${key} ${index + 1}`} value={value} onChange={(e) => edit(key, index, e.target.value)} />
          <button type="button" aria-label={`${t("nl.remove")} ${value}`} onClick={() => remove(key, value)}>×</button>
        </label>))}
      </div>
      {proposal.explanations.map((x, i) => <p className="form-hint" key={i}>{displayMessage(x, lang)}</p>)}
      {proposal.ambiguities.length > 0 && <div role="alert"><h2>{t("nl.ambiguous")}</h2>
        {proposal.ambiguities.map((x, i) => <p key={i}>{x.term}: {displayMessage(x.suggestion, lang)}</p>)}
      </div>}
      {proposal.unsupported.length > 0 && <div role="status"><h2>{t("nl.unsupported")}</h2>
        {proposal.unsupported.map((x, i) => <p key={i}>{displayMessage(x, lang)}</p>)}
      </div>}
      {(proposal.ambiguities.length > 0 || proposal.unsupported.length > 0) &&
        <label className="form-hint"><input type="checkbox" checked={acknowledged}
          onChange={(e) => setAcknowledged(e.target.checked)} />{t("nl.acknowledge")}</label>}
      <p className="form-hint">{t("nl.localData")}</p>
      <button type="button" className="chip chip-on"
        disabled={(proposal.ambiguities.length > 0 || proposal.unsupported.length > 0) && !acknowledged}
        onClick={run}>{t("nl.run")}</button>
    </>}
    <button type="button" className="chip" onClick={keyword}>{t("nl.keywords")}</button>
  </section>;
}
