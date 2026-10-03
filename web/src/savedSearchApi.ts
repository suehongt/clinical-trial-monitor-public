import { fetchJson, mutateJson } from "./api";

export type SavedSearch = {
  id: number; name: string; description: string | null;
  state: Record<string, unknown>; search_schema_version: number;
  original_nl_query: string | null; interpreter_version: string | null;
  pinned: boolean; created_at: string; updated_at: string; last_opened_at: string | null;
};
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const isSaved = (v: unknown): v is SavedSearch =>
  isObject(v) && typeof v.id === "number" && typeof v.name === "string"
  && isObject(v.state) && typeof v.pinned === "boolean";
export const isSavedSearchResponse = (v: unknown): v is { saved_search: SavedSearch } =>
  isObject(v) && isSaved(v.saved_search);
export const isSavedSearchList = (v: unknown): v is { items: SavedSearch[]; total: number; page: number } =>
  isObject(v) && Array.isArray(v.items) && v.items.every(isSaved) && typeof v.total === "number";

export const listSavedSearches = (params: URLSearchParams) =>
  fetchJson(`/api/saved-searches?${params}`, isSavedSearchList);
export const getSavedSearch = (id: number) =>
  fetchJson(`/api/saved-searches/${id}`, isSavedSearchResponse);
export const openSavedSearch = (id: number) =>
  mutateJson(`/api/saved-searches/${id}/open`, "POST", undefined, isSavedSearchResponse);
export const createSavedSearch = (payload: Record<string, unknown>) =>
  mutateJson("/api/saved-searches", "POST", payload, isSavedSearchResponse);
export const updateSavedSearch = (id: number, payload: Record<string, unknown>) =>
  mutateJson(`/api/saved-searches/${id}`, "PATCH", payload, isSavedSearchResponse);
export const deleteSavedSearch = (id: number) =>
  mutateJson(`/api/saved-searches/${id}`, "DELETE", undefined,
    (v): v is { deleted: number } => isObject(v) && typeof v.deleted === "number");
