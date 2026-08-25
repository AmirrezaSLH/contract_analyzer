import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type MetricsWindow } from "../api/client";
import { keys } from "./keys";

export function useMonitorStages(window: MetricsWindow) {
  return useQuery({
    queryKey: keys.monitorStages(window),
    queryFn: () => api.monitorStages(window),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}

export function useMonitorHost(window: MetricsWindow) {
  return useQuery({
    queryKey: keys.monitorHost(window),
    queryFn: () => api.monitorHost(window),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}
