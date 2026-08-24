/** The query keys, in one place.
 *
 *  Invalidation is targeted, never blanket: an upload invalidates `documents`;
 *  a completed analysis invalidates `documents` (the library's "last analysis"
 *  chip is now stale) and nothing else. `queryClient.invalidateQueries()` with
 *  no argument is a bug waiting to be a performance problem.
 */
export const keys = {
  health: ["health"] as const,
  criteria: ["criteria"] as const,
  documents: ["documents"] as const,
  document: (id: number) => ["documents", id] as const,
  sections: (id: number) => ["documents", id, "sections"] as const,
  analysis: (id: string) => ["analyses", id] as const,
  // The window is in the key, so switching it is a new query rather than a
  // refetch of the old one -- which is what lets the previous window's numbers
  // stay on screen while the new ones arrive.
  metricsSummary: (window: string) => ["metrics", "summary", window] as const,
  metricsTimeseries: (window: string) => ["metrics", "timeseries", window] as const,
  metricsRuns: (limit: number) => ["metrics", "runs", limit] as const,
};
