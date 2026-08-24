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
};
