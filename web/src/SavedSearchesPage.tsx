import { useState } from "react";
import { useJson } from "./api";
import { navigate } from "./router";
import type { Translate } from "./i18n";
import { savedStateToSearchState, searchStateToQuery } from "./searchState";
import {
  createSavedSearch, deleteSavedSearch, isSavedSearchList, openSavedSearch,
  updateSavedSearch, type SavedSearch,
} from "./savedSearchApi";
import { EmptyState, Loading } from "./ui";
import AddToProject from "./AddToProject";

export default function SavedSearchesPage({ t }: { t: Translate }) {
  const [q, setQ] = useState("");
  const [sort, setSort] = useState("recent");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [tick, setTick] = useState(0);
  const [editing, setEditing] = useState<SavedSearch | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [deleting, setDeleting] = useState<SavedSearch | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const params = new URLSearchParams({ q, sort, page: String(page), page_size: "25", refresh: String(tick) });
  if (pinnedOnly) params.set("pinned", "true");
  const data = useJson(`/api/saved-searches?${params}`, isSavedSearchList);
  const refresh = () => setTick((x) => x + 1);
  const action = (job: Promise<unknown>, after?: () => void) => {
    setBusy(true); setError("");
    job.then(() => { after?.(); refresh(); })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(false));
  };
  const open = (item: SavedSearch, track = false) => action(openSavedSearch(item.id).then(({ saved_search }) => {
    const query = new URLSearchParams(searchStateToQuery(savedStateToSearchState(saved_search.state)));
    query.set("saved_search", String(saved_search.id));
    if (track) query.set("track", "1");
    navigate(`/trials?${query}`);
  }));
  const edit = (item: SavedSearch) => {
    setEditing(item); setName(item.name); setDescription(item.description ?? ""); setError("");
  };

  return <section className="saved-library" aria-label={t("saved.title")}>
    <div className="page-tabs" role="tablist" aria-label={t("a11y.trialViews")}>
      <button type="button" role="tab" aria-selected={false} className="ptab" onClick={() => navigate("/trials")}>{t("trials.allTab")}</button>
      <button type="button" role="tab" aria-selected={false} className="ptab" onClick={() => navigate("/trials?view=watched")}>{t("nav.watched")}</button>
      <button type="button" role="tab" aria-selected={true} className="ptab ptab-on">{t("saved.title")}</button>
    </div>
    <div className="page-head">
      <div><h1>{t("saved.title")}</h1><p className="page-sub">{t("saved.passive")}</p></div>
      <div className="search-actions">
        <button type="button" className="chip chip-on" onClick={() => navigate("/trials")}>
          {t("saved.create")}
        </button>
      </div>
    </div>
    <div className="saved-toolbar">
      <input aria-label={t("saved.find")} placeholder={t("saved.find")} value={q}
        onChange={(e) => { setQ(e.target.value); setPage(1); }} />
      <select aria-label={t("saved.sort")} value={sort} onChange={(e) => { setSort(e.target.value); setPage(1); }}>
        <option value="recent">{t("saved.recent")}</option>
        <option value="updated">{t("saved.updated")}</option>
        <option value="name">{t("saved.nameSort")}</option>
      </select>
      <label><input type="checkbox" checked={pinnedOnly} onChange={(e) => { setPinnedOnly(e.target.checked); setPage(1); }} />{t("saved.pinnedOnly")}</label>
    </div>
    {error && <p className="error-note" role="alert">{error}</p>}
    {data.status === "loading" && <Loading label={t("saved.loading")} />}
    {data.status === "error" && <EmptyState title={t("saved.loadError")} hint={data.message} />}
    {data.status === "ready" && data.data.items.length === 0 && <EmptyState title={t("saved.empty")} hint={t("saved.emptyHint")}>
      <button type="button" className="chip chip-on" onClick={() => navigate("/trials")}>
        {t("saved.create")}
      </button>
    </EmptyState>}
    {data.status === "ready" && data.data.items.length > 0 && <>
      <div className="saved-list">
        {data.data.items.map((item) => <article className="saved-card" key={item.id}>
          <div><h2>{item.pinned ? "★ " : ""}{item.name}</h2>
            {item.description && <p>{item.description}</p>}
            {item.original_nl_query && <p><strong>{t("saved.original")}: </strong>{item.original_nl_query}</p>}
            <p className="form-hint">{Object.entries(item.state).filter(([k]) => k !== "sort")
              .map(([k, v]) => `${t(`search.rule.${k === "q" ? "query" : k}` as never)}: ${Array.isArray(v) ? v.join(", ") : String(v)}`).join(" · ")}</p>
            <p className="form-hint">{t("saved.updated")}: {item.updated_at} · {t("saved.lastOpened")}: {item.last_opened_at ?? "—"}</p>
            <p className="form-hint">{t("saved.openForCount")}</p>
          </div>
          <div className="saved-actions">
            <AddToProject kind="searches" id={item.id} />
            <button type="button" className="chip chip-on" disabled={busy} onClick={() => open(item)} aria-label={`${t("saved.open")} ${item.name}`}>{t("saved.open")}</button>
            <button type="button" className="chip" disabled={busy} onClick={() => open(item, true)} aria-label={`${t("saved.track")} ${item.name}`}>{t("saved.track")}</button>
            <button type="button" className="chip" disabled={busy} onClick={() => action(updateSavedSearch(item.id, { pinned: !item.pinned }))}
              aria-label={`${item.pinned ? t("saved.unpin") : t("saved.pin")} ${item.name}`} aria-pressed={item.pinned}>{item.pinned ? t("saved.unpin") : t("saved.pin")}</button>
            <button type="button" className="chip" disabled={busy} onClick={() => edit(item)} aria-label={`${t("saved.rename")} ${item.name}`}>{t("saved.rename")}</button>
            <button type="button" className="chip" disabled={busy} onClick={() => action(createSavedSearch({ name: `${item.name} (${t("saved.copy")})`, state: item.state,
              description: item.description, original_nl_query: item.original_nl_query, interpreter_version: item.interpreter_version }))}
              aria-label={`${t("saved.duplicate")} ${item.name}`}>{t("saved.duplicate")}</button>
            <button type="button" className="chip" disabled={busy} onClick={() => setDeleting(item)} aria-label={`${t("saved.delete")} ${item.name}`}>{t("saved.delete")}</button>
          </div>
        </article>)}
      </div>
      {data.data.total > 25 && <div className="pagination">
          <button type="button" disabled={page === 1} onClick={() => setPage(page - 1)}>{t("search.prevPage")}</button>
          <span>{t("search.pageOf", { page, total: Math.ceil(data.data.total / 25) })}</span>
          <button type="button" disabled={page * 25 >= data.data.total} onClick={() => setPage(page + 1)}>{t("search.nextPage")}</button>
        </div>}
    </>}
    {editing && <div className="modal-backdrop"><div className="modal track-modal" role="dialog" aria-modal="true" aria-label={t("saved.rename")}>
      <h2>{t("saved.rename")}: {editing.name}</h2>
      <label className="form-row">{t("mon.name")}<input autoFocus value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label className="form-row">{t("saved.description")}<input value={description} onChange={(e) => setDescription(e.target.value)} /></label>
      {error && <p role="alert">{error}</p>}
      <button type="button" disabled={busy} onClick={() => action(updateSavedSearch(editing.id, { name, description }), () => setEditing(null))}>{t("saved.updateMetadata")}</button>
      <button type="button" onClick={() => setEditing(null)}>{t("a11y.close")}</button>
    </div></div>}
    {deleting && <div className="modal-backdrop"><div className="modal track-modal" role="alertdialog" aria-modal="true" aria-label={t("saved.delete")}>
      <h2>{t("saved.delete")}: {deleting.name}</h2><p>{t("saved.deleteSafety")}</p>
      {error && <p role="alert">{error}</p>}
      <button type="button" autoFocus onClick={() => setDeleting(null)}>{t("a11y.close")}</button>
      <button type="button" disabled={busy} onClick={() => action(deleteSavedSearch(deleting.id), () => setDeleting(null))}>{t("saved.confirmDelete")}</button>
    </div></div>}
  </section>;
}
