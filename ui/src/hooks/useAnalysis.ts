import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { api, newTrace, type Analysis, type AnalysisSummary } from "../api/client";
import { keys } from "./keys";

/** The statuses that mean "nothing more will happen to this analysis". */
const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);

export function isTerminal(status: string | undefined): boolean {
  return status !== undefined && TERMINAL.has(status);
}

/**
 * The polling machine, and the one piece of real machinery in this front end.
 *
 * `refetchInterval` returning `false` on a terminal status is what stops the
 * poll: there is no interval to leak and no cleanup to get wrong. The
 * predicate takes the whole query object in v5 -- it took the data in v4 --
 * which is why `@tanstack/react-query` is pinned to an exact version rather
 * than a caret range, and why `test/poll.test.ts` asserts the predicate
 * directly rather than trusting the signature.
 *
 * `GET /analyses/{id}` is all the running view needs: `criteria` is exactly
 * the progress table it draws. The SSE endpoint exists and buys this UI
 * nothing.
 */
export function useAnalysis(analysisId: string | null) {
  const client = useQueryClient();

  const query = useQuery({
    queryKey: keys.analysis(analysisId ?? ""),
    queryFn: () => api.analysis(analysisId!),
    enabled: analysisId !== null,
    refetchInterval: (q) => pollInterval(q.state.data?.status),
    // Two seconds of staleness on a job that takes a minute is not a problem;
    // a refetch on every mount of the view is.
    staleTime: 1_000,
  });

  const status = query.data?.status;
  useEffect(() => {
    // The library's "last analysis" chip is stale the moment a run finishes.
    // Targeted, not a blanket invalidate: nothing else changed.
    if (isTerminal(status)) void client.invalidateQueries({ queryKey: keys.documents });
  }, [status, client]);

  return query;
}

/** Extracted so it can be tested without React: this is the assertion that the
 *  poll stops, and it is the one behaviour a version bump could silently
 *  break. */
export function pollInterval(status: string | undefined): number | false {
  return status === "queued" || status === "running" ? 2000 : false;
}

export function useCreateAnalysis() {
  const client = useQueryClient();
  return useMutation<AnalysisSummary & { traceId: string }, Error, { documentId: number; rerun?: boolean }>({
    mutationFn: async ({ documentId, rerun }) => {
      const traceId = newTrace();
      // A `200` here is a success, not an error: it means the duplicate-submit
      // guard matched a run already in flight, and its `analysis_id` is as good
      // as a `202`'s. Rendering it as a failure would make a double-clicked
      // button look broken while working correctly.
      const summary = await api.createAnalysis(documentId, { traceId, rerun });
      return { ...summary, traceId };
    },
    onSuccess: (summary) => {
      client.setQueryData(keys.analysis(summary.analysis_id), summary as Analysis);
      void client.invalidateQueries({ queryKey: keys.documents });
      void client.invalidateQueries({ queryKey: keys.document(summary.document_id) });
    },
  });
}

export function useCancelAnalysis() {
  const client = useQueryClient();
  return useMutation<AnalysisSummary, Error, string>({
    mutationFn: (id) => api.cancelAnalysis(id, newTrace()),
    onSuccess: (summary) => {
      void client.invalidateQueries({ queryKey: keys.analysis(summary.analysis_id) });
    },
  });
}
