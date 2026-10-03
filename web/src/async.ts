/**
 * Discriminated union for asynchronous resources. Every fetch in the app is
 * represented by one of these four states, replacing parallel
 * `error`/`loading`/`data` useState pairs.
 */
export type AsyncState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; message: string };
