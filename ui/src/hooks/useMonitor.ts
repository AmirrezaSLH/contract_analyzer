import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type MonitorWindow } from "../api/client";
import { keys } from "./keys";

export function useMonitorStages(window: MonitorWindow) {
  return useQuery({
    queryKey: keys.monitorStages(window),
    queryFn: () => api.monitorStages(window),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}

export function useMonitorHost(window: MonitorWindow) {
  return useQuery({
    queryKey: keys.monitorHost(window),
    queryFn: () => api.monitorHost(window),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}

export function useMonitorUpstream(window: MonitorWindow) {
  return useQuery({
    queryKey: keys.monitorUpstream(window),
    queryFn: () => api.monitorUpstream(window),
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    placeholderData: keepPreviousData,
  });
}
