import guideJson from "./content/user-guide.json";
import releasesJson from "./content/release-notes.json";
import type { Lang } from "./i18n";

export type LocalizedText = Record<Lang, string>;
export type GuideLink = { path: string; label: LocalizedText };
export type GuideItem = { title: LocalizedText; body: LocalizedText; link?: GuideLink };
export type GuideSection = { id: string; title: LocalizedText; description: LocalizedText; items: GuideItem[] };
export type UserGuide = { schemaVersion: number; updatedAt: string; title: LocalizedText; summary: LocalizedText; sections: GuideSection[] };
export type ChangeType = "feature" | "improvement" | "fix";
export type ReleaseNote = { version: string; date: string; status: "current" | "previous"; title: LocalizedText; changes: Array<{ type: ChangeType; text: LocalizedText }> };
export type ReleaseNotes = { schemaVersion: number; updatedAt: string; title: LocalizedText; summary: LocalizedText; releases: ReleaseNote[] };

export const userGuide = guideJson as UserGuide;
export const releaseNotes = releasesJson as ReleaseNotes;

export const startHere = {
  title: { en: "Start here", zh: "从这里开始" },
  summary: { en: "A practical path from discovery to a reusable research briefing.", zh: "从发现试验到形成可复用研究简报的实际路径。" },
  steps: [
    { title: { en: "Search trials", zh: "检索试验" }, body: { en: "Search the indexed registries and refine results with source-aware filters.", zh: "检索已入库登记平台，并使用保留来源身份的筛选条件缩小结果。" }, path: "/trials" },
    { title: { en: "Save or track a search", zh: "保存或跟踪搜索" }, body: { en: "A Saved Search preserves criteria. Tracking creates a Monitor that can run again.", zh: "保存搜索会保留条件；跟踪搜索会创建可重复运行的监测器。" }, path: "/trials?view=saved" },
    { title: { en: "Create a Monitor", zh: "创建监测器" }, body: { en: "Choose a topic and optional UTC-backed schedule. Enabled and scheduled are separate states.", zh: "设置研究主题及可选的 UTC 调度。启用状态与调度状态相互独立。" }, path: "/monitors" },
    { title: { en: "Review Updates", zh: "审阅变化" }, body: { en: "Triage exact field changes by severity, source and category.", zh: "按严重程度、来源和类别分诊准确的字段变化。" }, path: "/updates" },
    { title: { en: "Organize evidence", zh: "整理研究证据" }, body: { en: "Curate trials, link monitors, write observations and bookmark stable events in a Project.", zh: "在项目中收录试验、关联监测器、撰写观察并收藏稳定事件。" }, path: "/projects" },
    { title: { en: "Read or print a Briefing", zh: "阅读或打印简报" }, body: { en: "Use the deterministic briefing to summarize a selected scope without invented conclusions.", zh: "使用确定性简报总结选定范围，不生成臆测结论。" }, path: "/briefing" },
  ],
  concepts: [
    ["Watch", "Track one trial globally; this is independent of Project membership.", "全局跟踪单项试验；与项目归属相互独立。"],
    ["Saved Search", "Reusable search criteria; it does not create Project events by itself.", "可复用的搜索条件；自身不会直接产生项目事件。"],
    ["Monitor", "A reusable rule set that records entered, left, reentered and changed activity.", "可复用规则集，记录进入、退出、重新进入和变化活动。"],
    ["Project", "A local research container for assets, curated trials, notes and evidence.", "用于整理资产、收录试验、笔记和证据的本地研究容器。"],
    ["Evidence", "A Project bookmark to a stable trial event ID, optionally annotated.", "指向稳定试验事件 ID 的项目书签，可附注释。"],
    ["Scope", "The current set selected for intelligence; it is not a historical ownership reconstruction.", "情报分析当前选定的范围，并非历史归属重建。"],
    ["Severity", "Critical, important, normal or minor attention level; text always accompanies color.", "关键、重要、一般或轻微的关注等级；颜色始终配有文字。"],
    ["Freshness", "Backend-provided fresh, delayed, stale, unknown or disabled source state.", "后端提供的新鲜、延迟、过期、未知或停用来源状态。"],
  ].map(([term, en, zh]) => ({ term, body: { en, zh } })),
  statuses: [
    { title: { en: "Enabled vs scheduled", zh: "启用与已调度" }, body: { en: "An enabled Monitor may still be manual. A schedule controls automatic execution only.", zh: "启用的监测器仍可能是手动运行；调度只控制自动执行。" } },
    { title: { en: "Freshness states", zh: "新鲜度状态" }, body: { en: "Fresh, delayed, stale, unknown and disabled come from the backend threshold and configuration.", zh: "新鲜、延迟、过期、未知和停用均来自后端阈值与配置。" } },
    { title: { en: "Watch vs Add to Project", zh: "监测与加入项目" }, body: { en: "Watch follows a trial globally. Add to Project curates it only inside that Project.", zh: "监测会全局跟踪试验；加入项目仅在该项目内收录。" } },
    { title: { en: "Archive vs delete", zh: "归档与删除" }, body: { en: "Archive keeps all Project data and does not pause monitors. Delete removes that Project's links, notes and evidence.", zh: "归档会保留全部项目数据且不暂停监测器；删除会移除该项目的关联、笔记和证据。" } },
  ],
};

export function searchHelp(lang: Lang, query: string) {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return userGuide.sections;
  return userGuide.sections.map((section) => ({ ...section, items: section.items.filter((item) => [localize(item.title, lang), localize(item.body, lang), section.id].join(" ").toLocaleLowerCase().includes(q)) })).filter((section) => section.items.length);
}

export function localize(value: LocalizedText, lang: Lang): string {
  return value[lang] || value.en;
}

export function validateHelpContent(guide: UserGuide, notes: ReleaseNotes): string[] {
  const errors: string[] = [];
  const textOk = (value: LocalizedText | undefined) => Boolean(value?.zh?.trim() && value?.en?.trim());
  if (guide.schemaVersion !== 1) errors.push("unsupported user-guide schemaVersion");
  if (!textOk(guide.title) || !textOk(guide.summary)) errors.push("user-guide title/summary must be bilingual");
  const sectionIds = new Set<string>();
  for (const section of guide.sections) {
    if (!section.id || sectionIds.has(section.id)) errors.push(`invalid or duplicate guide section id: ${section.id}`);
    sectionIds.add(section.id);
    if (!textOk(section.title) || !textOk(section.description) || !section.items.length) errors.push(`invalid guide section: ${section.id}`);
    for (const item of section.items) {
      if (!textOk(item.title) || !textOk(item.body)) errors.push(`invalid guide item in: ${section.id}`);
      if (item.link && (!item.link.path.startsWith("/") || item.link.path.startsWith("//") || !textOk(item.link.label))) errors.push(`unsafe or invalid guide link in: ${section.id}`);
    }
  }
  if (notes.schemaVersion !== 1) errors.push("unsupported release-notes schemaVersion");
  if (!textOk(notes.title) || !textOk(notes.summary)) errors.push("release-notes title/summary must be bilingual");
  let previousDate = "9999-99-99";
  const versions = new Set<string>();
  let currentReleases = 0;
  for (const release of notes.releases) {
    if (!release.version || versions.has(release.version)) errors.push(`invalid or duplicate release version: ${release.version}`);
    versions.add(release.version);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(release.date) || release.date > previousDate) errors.push(`releases are not newest-first at: ${release.version}`);
    previousDate = release.date;
    if (!textOk(release.title) || !release.changes.length) errors.push(`invalid release: ${release.version}`);
    if (release.status === "current") currentReleases += 1;
    for (const change of release.changes) if (!["feature", "improvement", "fix"].includes(change.type) || !textOk(change.text)) errors.push(`invalid change in: ${release.version}`);
  }
  if (currentReleases !== 1 || notes.releases[0]?.status !== "current") errors.push("release notes must have exactly one current release and it must be first");
  return errors;
}
