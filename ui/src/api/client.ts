/**
 * The API client: one base URL, one error shape, one trace id per action.
 *
 * Everything the front end knows about HTTP is here. Views call hooks, hooks
 * call this, and nothing else in `ui/` mentions `fetch`.
 */

import type { components, paths } from "./types.gen";

/** Always `/api`. In development Vite proxies it to the API; in production
 *  this bundle is served *by* the API. There is no configurable base URL in a
 *  browser bundle, and there is no second origin to configure one for. */
export const BASE = "/api";

type Schemas = components["schemas"];

export type Health = Schemas["Health"];
export type CriterionOut = Schemas["CriterionOut"];
export type DocumentOut = Schemas["DocumentOut"];
export type DocumentDetail = Schemas["DocumentDetail"];
export type UploadOut = Schemas["UploadOut"];
export type SectionOut = Schemas["SectionOut"];
export type Analysis = Schemas["Analysis"];
export type AnalysisSummary = Schemas["AnalysisSummary"];
export type AnalysisReport = Schemas["AnalysisReport"];
export type ComplianceResult = Schemas["ComplianceResult"];
export type CriterionProgress = Schemas["CriterionProgress"];
export type ResolvedQuote = Schemas["ResolvedQuote"];
export type CitationOut = Schemas["CitationOut"];
export type SubRequirementResult = Schemas["SubRequirementResult"];
export type Answer = Schemas["Answer"];
export type ChatRequest = Schemas["ChatRequest"];
export type LastAnalysisOut = Schemas["LastAnalysisOut"];
export type JobStatus = AnalysisSummary["status"];
export type ComplianceState = ComplianceResult["compliance_state"];
export type SubRequirementStatus = SubRequirementResult["status"];
export type RetrievalMode = NonNullable<ChatRequest["retrieval_mode"]>;

// -- the KPI page ----------------------------------------------------------
// Typed like everything else, from the schema: the four `/metrics` handlers
// carry pydantic response models precisely so this page is not the one part of
// the front end typed by hand.
export type MetricsSummary = Schemas["MetricsSummary"];
export type MetricsBucket = Schemas["MetricsBucket"];
export type RunRow = Schemas["RunRow"];
export type SpanNode = Schemas["SpanNode"];
export type EvaluatorSlot = Schemas["EvaluatorSlot"];

/** The window selector's three options, and the only windows this UI asks for.
 *  The **bucket is deliberately not a parameter here**: the server pairs them
 *  (`windows.DEFAULT_BUCKETS` -- 24h/1h, 7d/6h, 30d/1d) so the API and the
 *  design cannot drift, and thirty days of one-hour bars is 720 marks on a
 *  900-pixel axis. */
export type MetricsWindow = "24h" | "7d" | "30d";

export type MonitorWindow = "30m" | "1h";

export interface StageBucket {
  bucket: string;
  n: number;
  error_rate: number | null;
  errors_total: number | null;
}

export interface MonitorStages {
  window: string;
  live_window: string;
  since: string;
  generated_at: string;
  name: string | null;
  n: number;
  errors: number;
  error_rate: number | null;
  errors_total: number | null;
  min_samples: number;
  series: StageBucket[];
}

export interface HostBucket {
  bucket: string;
  rss_pct: number | null;
  disk_used_pct: number | null;
}

export interface MonitorHost {
  window: string;
  bucket: string;
  since: string;
  generated_at: string;
  ts: string | null;
  rss_mb: number | null;
  rss_pct: number | null;
  disk_used_pct: number | null;
  disk_used_gb: number | null;
  disk_total_gb: number | null;
  series: HostBucket[];
}

export interface UpstreamBucket {
  bucket: string;
  calls: number;
  retries: number;
  failed: number;
  retries_per_100: number | null;
  exhausted_rate: number | null;
}

export interface MonitorUpstream {
  window: string;
  live_window: string;
  since: string;
  generated_at: string;
  calls: number;
  retries: number;
  failed: number;
  retries_per_100: number | null;
  exhausted_rate: number | null;
  top_reason: string | null;
  top_reason_share: number | null;
  series: UpstreamBucket[];
}

/** `AnalysisReport` types `results` as `ComplianceResult[] | undefined`, and
 *  `relevant_quotes` likewise. These narrow the optionality away once, here,
 *  rather than in every component that reads a report. */
export type Report = AnalysisReport;

/** A failure, in this API's own envelope.
 *
 *  `code` is the stable string every error surface switches on. A response
 *  that is not this shape -- a proxy error page, a 502 from nowhere -- becomes
 *  `unreachable` so the rendering layer never has to handle two shapes. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly hint?: string,
    readonly traceId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** A user-initiated action's trace id: an upload, a submission, a question.
 *
 *  One per *action*, not per request, which is what makes the log walkthrough
 *  work -- a reviewer reads an id off the analysis card and greps
 *  `.run/app.jsonl` for it, and finds the submission, the five criterion runs
 *  and every tool call they made under one id. */
export function newTrace(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8)}`;
}

export interface Options {
  method?: string;
  body?: unknown;
  traceId?: string;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { method = "GET", body, traceId, signal, headers = {} } = options;
  const response = await fetch(`${BASE}${path}`, {
    method,
    signal,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(traceId ? { "X-Trace-Id": traceId } : {}),
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  }).catch((cause) => {
    // A network failure is not an HTTP status, and a view that only knows how
    // to render an `ApiError` would otherwise get a raw `TypeError`.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(0, "unreachable", "Could not reach the analyzer.", RETRY_HINT);
  });
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const RETRY_HINT = "Check that the service is running, then try again.";

/** Every non-2xx, as one type. */
export async function toApiError(response: Response): Promise<ApiError> {
  const traceId = response.headers.get("X-Trace-Id") ?? undefined;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const error = (body as { error?: Schemas["ErrorBody"] } | null)?.error;
  if (error?.code) {
    return new ApiError(response.status, error.code, error.message, error.hint ?? undefined, traceId);
  }
  // Not this API's envelope: a proxy, a gateway, a load balancer. One shape
  // reaches the rendering layer, so it is given the one it knows.
  return new ApiError(
    response.status,
    "unreachable",
    "Could not reach the analyzer.",
    RETRY_HINT,
    traceId,
  );
}

// --------------------------------------------------------------------------
// The calls
// --------------------------------------------------------------------------

export const api = {
  health: () => request<Health>("/health"),

  criteria: () => request<CriterionOut[]>("/criteria"),

  documents: () => request<DocumentOut[]>("/documents"),

  document: (id: number) => request<DocumentDetail>(`/documents/${id}`),

  sections: (id: number) => request<SectionOut[]>(`/documents/${id}/sections`),

  deleteDocument: (id: number, traceId?: string) =>
    request<void>(`/documents/${id}`, { method: "DELETE", traceId }),

  analysis: (id: string) => request<Analysis>(`/analyses/${id}`),

  createAnalysis: (documentId: number, options: { traceId?: string; rerun?: boolean } = {}) =>
    request<AnalysisSummary>("/analyses", {
      method: "POST",
      body: { document_id: documentId } satisfies Schemas["AnalyzeRequest"],
      traceId: options.traceId,
      // What makes the API queue a *second* job rather than handing back the
      // one already in flight. Without it a re-run is a duplicate submission,
      // which the API correctly refuses with a 200 and the existing id.
      headers: options.rerun ? { "Idempotency-Key": newTrace() } : {},
    }),

  cancelAnalysis: (id: string, traceId?: string) =>
    request<AnalysisSummary>(`/analyses/${id}/cancel`, { method: "POST", traceId }),

  metricsSummary: (window: MetricsWindow) =>
    request<MetricsSummary>(`/metrics/summary?window=${window}`),

  // No `bucket`: the server chooses it from the window. See `MetricsWindow`.
  metricsTimeseries: (window: MetricsWindow) =>
    request<MetricsBucket[]>(`/metrics/timeseries?window=${window}`),

  metricsRuns: (limit = 50) => request<RunRow[]>(`/metrics/runs?limit=${limit}`),

  monitorStages: (window: MonitorWindow) =>
    request<MonitorStages>(`/monitor/stages?window=${window}`),

  monitorHost: (window: MonitorWindow) =>
    request<MonitorHost>(`/monitor/host?window=${window}`),

  monitorUpstream: (window: MonitorWindow) =>
    request<MonitorUpstream>(`/monitor/upstream?window=${window}`),
};

// --------------------------------------------------------------------------
// Upload
// --------------------------------------------------------------------------

/**
 * The one `XMLHttpRequest` in this codebase, and it carries this comment
 * saying why: `fetch` has no upload progress event. A 25 MB PDF over a slow
 * link is fifteen seconds of nothing, and the drop zone is specified to show a
 * determinate bar. Everything else uses `fetch`.
 */
export function upload(
  file: File,
  options: { traceId?: string; onProgress?: (fraction: number) => void } = {},
): Promise<UploadOut> {
  const { traceId, onProgress } = options;
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/documents`);
    if (traceId) xhr.setRequestHeader("X-Trace-Id", traceId);
    xhr.responseType = "text";

    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress?.(event.loaded / event.total);
    });

    xhr.addEventListener("load", () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText) as UploadOut);
        return;
      }
      reject(xhrError(xhr));
    });
    xhr.addEventListener("error", () =>
      reject(new ApiError(0, "unreachable", "Could not reach the analyzer.", RETRY_HINT)),
    );
    xhr.addEventListener("abort", () =>
      reject(new ApiError(0, "upload_aborted", "The upload was cancelled.")),
    );

    xhr.send(form);
  });
}

function xhrError(xhr: XMLHttpRequest): ApiError {
  const traceId = xhr.getResponseHeader("X-Trace-Id") ?? undefined;
  try {
    const body = JSON.parse(xhr.responseText) as { error?: Schemas["ErrorBody"] };
    if (body.error?.code) {
      return new ApiError(
        xhr.status,
        body.error.code,
        body.error.message,
        body.error.hint ?? undefined,
        traceId,
      );
    }
  } catch {
    /* falls through to the generic shape below */
  }
  return new ApiError(xhr.status, "unreachable", "Could not reach the analyzer.", RETRY_HINT, traceId);
}

export type { paths };
