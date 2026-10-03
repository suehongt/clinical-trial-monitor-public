import { fetchJson, mutateJson } from "./api";

export type Project = {
  id: number; name: string; description: string; pinned: number; archived_at: string | null;
  created_at: string; updated_at: string;
  counts: { saved_searches: number; monitors: number; trials: number; notes: number; evidence: number };
  recent_important_changes?: number; recent_critical_changes?: number; last_activity?: string | null;
  freshness?: { sources: Array<{ full_name: string; freshness: string }>; affected: unknown[] };
  saved_searches: Array<{ id: number; name: string; description?: string; state_json: string }>;
  monitors: Array<{ id: number; name: string; enabled: number; current_trials: number; last_run_at?: string }>;
  trials: Array<{ source: string; trial_id: string; title: string | null; status: string | null; phase: string | null; last_change: string | null; recent_severity: string | null; watching: number }>;
  notes: Array<{ id: number; body: string; updated_at: string }>;
  evidence: Array<{ id: number; event_id: number; note: string; field_name: string; severity: string; trial_id: string; source: string; title: string; old_value: string | null; new_value: string | null }>;
  trial_page: number; trial_page_size: number; watched_trial_count: number;
};
export type ProjectListItem = Pick<Project, "id" | "name" | "description" | "pinned" | "archived_at" | "created_at" | "updated_at" | "counts" | "recent_important_changes" | "recent_critical_changes" | "last_activity" | "freshness"> & { active_monitors: number };
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
export const isProjectList = (v: unknown): v is { projects: ProjectListItem[] } => object(v) && Array.isArray(v.projects);
export const isProjectDetail = (v: unknown): v is { project: Project } => object(v) && object(v.project) && Array.isArray(v.project.trials);
const ok = (v: unknown): v is Record<string, unknown> => object(v);
export const projectList = () => fetchJson("/api/projects", isProjectList);
export const projectDetail = (id: number) => fetchJson(`/api/projects/${id}`, isProjectDetail);
export const createProject = (name: string, description = "") => mutateJson("/api/projects", "POST", { name, description }, isProjectDetail);
export const updateProject = (id: number, values: Record<string, unknown>) => mutateJson(`/api/projects/${id}`, "PATCH", values, isProjectDetail);
export const deleteProject = (id: number) => mutateJson(`/api/projects/${id}`, "DELETE", undefined, ok);
export const addAsset = (id: number, kind: "searches" | "monitors" | "trials", payload: Record<string, unknown>) =>
  mutateJson(`/api/projects/${id}/assets/${kind}`, "POST", payload, isProjectDetail);
export const removeAsset = (id: number, kind: "searches" | "monitors" | "trials", asset: string | number, source?: string) =>
  mutateJson(`/api/projects/${id}/assets/${kind}/${encodeURIComponent(asset)}${source ? `?source=${encodeURIComponent(source)}` : ""}`, "DELETE", undefined, isProjectDetail);
export const addNote = (id: number, body: string) => mutateJson(`/api/projects/${id}/notes`, "POST", { body }, ok);
export const updateNote = (id: number, noteId: number, body: string) => mutateJson(`/api/projects/${id}/notes/${noteId}`, "PATCH", { body }, ok);
export const deleteNote = (id: number, noteId: number) => mutateJson(`/api/projects/${id}/notes/${noteId}`, "DELETE", undefined, ok);
export const addEvidence = (id: number, eventId: number, note = "") => mutateJson(`/api/projects/${id}/evidence`, "POST", { event_id: eventId, note }, ok);
export const deleteEvidence = (id: number, eventId: number) => mutateJson(`/api/projects/${id}/evidence/${eventId}`, "DELETE", undefined, ok);
