import { useState } from "react";
import { useJson } from "./api";
import { addAsset, addEvidence, createProject, isProjectList } from "./projectApi";

type Props = { kind: "searches" | "monitors" | "trials" | "evidence"; id?: number; source?: string; trialId?: string; eventId?: number };

/** Explicit organizational action: never changes Watch or Monitor state. */
export default function AddToProject({ kind, id, source, trialId, eventId }: Props) {
  const [open, setOpen] = useState(false);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const projects = useJson(open ? `/api/projects?refresh=${tick}` : null, isProjectList);
  const add = async (projectId: number) => {
    setBusy(true); setMessage("");
    try {
      if (kind === "evidence") await addEvidence(projectId, eventId!);
      else if (kind === "trials") await addAsset(projectId, kind, { source, trial_id: trialId });
      else await addAsset(projectId, kind, { id });
      setMessage("Added to project"); setOpen(false);
    } catch (e) { setMessage(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };
  const create = async () => {
    const name = window.prompt("New project name");
    if (!name?.trim()) return;
    setBusy(true);
    try { const { project } = await createProject(name.trim()); await add(project.id); setTick((v) => v + 1); }
    catch (e) { setMessage(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  return <span className="project-picker" onClick={(e) => e.stopPropagation()}>
    <button type="button" className="chip" onClick={() => { setOpen(!open); setMessage(""); }}>{kind === "evidence" ? "Save evidence" : "Add to Project"}</button>
    {open && <span className="project-picker-menu" role="group" aria-label="Choose project">
      {projects.status === "ready" && projects.data.projects.map((p) => <button type="button" key={p.id} disabled={busy} onClick={() => add(p.id)}>{p.name}</button>)}
      {projects.status === "ready" && projects.data.projects.length === 0 && <small>No projects yet.</small>}
      <button type="button" disabled={busy} onClick={create}>Create new project…</button>
    </span>}
    {message && <small role="status">{message}</small>}
  </span>;
}
