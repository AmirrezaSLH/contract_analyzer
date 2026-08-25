export type MonitorWindow = "30m" | "1h";

/** One chart point. `null` is a gap: the line breaks, it is not drawn as 0. */
export interface Sample {
  bucket: string;
  value: number | null;
}

export interface MonitorSnapshot {
  upstream: {
    retriesPer100: number;
    exhaustedRate: number;
    topReason: string;
  };
  series: {
    retries: Sample[];
    exhausted: Sample[];
  };
}
