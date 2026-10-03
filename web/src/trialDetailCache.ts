import type { TrialDetailFull } from "./types";
import { getTrialFull } from "./monitoringApi";
import { trialSelectionKey } from "./trialViewState";

const MAX_ENTRIES = 24;
const resolved = new Map<string, TrialDetailFull>();
const pending = new Map<string, Promise<TrialDetailFull>>();

function touch(key: string, value: TrialDetailFull): void {
  resolved.delete(key);
  resolved.set(key, value);
  while (resolved.size > MAX_ENTRIES) resolved.delete(resolved.keys().next().value as string);
}

export function loadTrialDetail(source: string, id: string, retry = false): Promise<TrialDetailFull> {
  const key = trialSelectionKey({ source, id });
  if (retry) { resolved.delete(key); pending.delete(key); }
  const cached = resolved.get(key);
  if (cached) { touch(key, cached); return Promise.resolve(cached); }
  const running = pending.get(key);
  if (running) return running;
  const request = getTrialFull(source, id).then((value) => {
    pending.delete(key); touch(key, value); return value;
  }, (error) => { pending.delete(key); throw error; });
  pending.set(key, request);
  return request;
}

export function peekTrialDetail(source: string, id: string): TrialDetailFull | undefined {
  return resolved.get(trialSelectionKey({ source, id }));
}

export function trialDetailCacheKey(source: string, id: string): string {
  return trialSelectionKey({ source, id });
}
