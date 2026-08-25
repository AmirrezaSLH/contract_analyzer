export type MonitorWindow = "30m" | "1h";

/** One chart point. `null` is a gap: the line breaks, it is not drawn as 0. */
export interface Sample {
  bucket: string;
  value: number | null;
}

export interface MonitorSnapshot {
  http: {
    rpm: number;
    fiveXx: number;
    p95Ms: number;
  };
  upstream: {
    retriesPer100: number;
    exhaustedRate: number;
    topReason: string;
  };
  stages: {
    name: string;
    errorRate: number;
    p95S: number;
    n: number;
  };
  host: {
    rssPct: number;
    rssMb: number;
    diskPct: number;
    diskGb: number;
    diskTotalGb: number;
  };
  series: {
    httpRpm: Sample[];
    httpFiveXx: Sample[];
    httpP95: Sample[];
    retries: Sample[];
    exhausted: Sample[];
    stageError: Sample[];
    stageP95: Sample[];
    rss: Sample[];
    disk: Sample[];
  };
}
