import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type MetricsWindow } from "../api/client";
import { keys } from "./keys";

/**
 * The KPI page's three queries, and the three cadences they run at.
 *
 * The split is not arbitrary. `live.running` is a fact about *this process* --
 * it comes from `JobRunner`, not from a table -- so summary is the one that
 * has to be fresh, and it polls. The trend and the runs table are history:
 * they change when a run completes, which the summary will have already shown.
 *
 * **Every one of them keeps the previous window's data on screen while the new
 * one loads.** `02_kpi_page.md` §4: refetching shows in the "refreshed Ns ago"
 * line and nowhere else. A dashboard that blanks itself every five seconds is
 * unreadable, and one that blanks itself on a window change looks broken.
 */

/** Five seconds, the design's line. */
const SUMMARY_POLL = 5_000;

export function useMetricsSummary(window: MetricsWindow) {
  return useQuery({
    queryKey: keys.metricsSummary(window),
    queryFn: () => api.metricsSummary(window),
    refetchInterval: SUMMARY_POLL,
    // A dashboard is worth polling while it is not the focused tab: it is the
    // one page in this product somebody leaves open on a second monitor.
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}

export function useMetricsTimeseries(window: MetricsWindow) {
  return useQuery({
    queryKey: keys.metricsTimeseries(window),
    queryFn: () => api.metricsTimeseries(window),
    // A bucket is a minute on the short windows and an hour on 24h. Poll the
    // minute grain often enough that the in-progress bucket can move.
    refetchInterval: window === "30m" || window === "1h" ? 15_000 : 60_000,
    staleTime: window === "30m" || window === "1h" ? 5_000 : 30_000,
    placeholderData: keepPreviousData,
  });
}

export function useMetricsRuns(limit = 50) {
  return useQuery({
    queryKey: keys.metricsRuns(limit),
    queryFn: () => api.metricsRuns(limit),
    refetchInterval: 30_000,
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  });
}
