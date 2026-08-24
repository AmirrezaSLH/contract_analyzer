/**
 * Where a failure goes on the screen.
 *
 * `01_ui_spec.md` §4 is this module's specification: every `code` the API
 * defines has exactly one place it renders, and **there is no generic error
 * toast in this product**. Views switch on the *surface*, never on the code,
 * so a new code needs one row here and no view changes.
 *
 * Three rules, enforced here rather than remembered:
 *
 *   1. `hint` is always the second line. The API writes it for a person.
 *   2. An error never destroys work -- that is the views' half of the bargain:
 *      a failed question keeps its text in the input, a failed analysis keeps
 *      the previous report on screen.
 *   3. An unknown code falls back rather than throwing. The API may grow a
 *      code before this table knows about it, and a generic inline error is a
 *      better outcome than a white screen.
 */

import { ApiError } from "./client";

/** *Where* the failure renders. The views know these four; they do not know
 *  the forty-odd codes that map onto them. */
export type Placement =
  /** Under the control that caused it; the control stays usable. */
  | "inline"
  /** Replaces the card whose content failed -- the upload result, a report. */
  | "replaces-card"
  /** Above the tab bar, blocking the surfaces it names. */
  | "banner"
  /** The whole pane: the thing being addressed is not there. */
  | "full-pane";

export interface ErrorSurface {
  placement: Placement;
  /** The sentence a reviewer reads first. The API's `message` is accurate but
   *  often names an internal noun ("ANTHROPIC_API_KEY is not set"). */
  title: string;
  /** The API's own message, when it says something the title does not. */
  body?: string;
  /** Always the second line when the API sent one. */
  hint?: string;
  /** The label of the recovery action, or null when there is nothing to retry. */
  retry: string | null;
  traceId?: string;
}

interface Row {
  placement: Placement;
  title: string | ((error: ApiError) => string);
  retry: string | null;
  /** Whether to print the API's own message under the title. Off where the
   *  title already says it; on where the message carries the detail -- a
   *  filename, a size, the runner's failure. */
  echo?: boolean;
}

const TABLE: Record<string, Row> = {
  // -- upload ---------------------------------------------------------------
  unsupported_media_type: {
    placement: "inline",
    title: "That is not a PDF. This reads contracts as PDF only.",
    retry: null,
  },
  payload_too_large: {
    placement: "inline",
    title: "That file is over the upload limit.",
    retry: null,
    echo: true,
  },
  embedder_unavailable: {
    placement: "replaces-card",
    title: "The document could not be indexed: the embedding service is unavailable.",
    retry: "Try again",
  },
  ingest_failed: {
    // The API's message names the failure, so it *is* the title.
    placement: "replaces-card",
    title: (error) => error.message,
    retry: "Try again",
  },
  model_mismatch: {
    placement: "replaces-card",
    title: "This corpus was indexed with a different embedding model.",
    retry: null,
    echo: true,
  },

  // -- the answer key -------------------------------------------------------
  no_api_key: {
    placement: "banner",
    title: "No answer model is configured, so analysis and chat are unavailable.",
    retry: null,
  },
  answer_unavailable: {
    placement: "banner",
    title: "No answer model is configured, so analysis and chat are unavailable.",
    retry: null,
  },

  // -- scope ----------------------------------------------------------------
  document_not_found: {
    placement: "full-pane",
    title: "That document is no longer in the library.",
    retry: null,
  },
  analysis_not_found: {
    placement: "replaces-card",
    title: "That analysis is no longer here.",
    retry: null,
  },
  analysis_running: {
    placement: "inline",
    title: "An analysis of this contract is running. Cancel it first, or wait.",
    retry: null,
  },
  not_running: {
    placement: "inline",
    title: "That analysis is not running any more.",
    retry: null,
  },
  not_live_here: {
    placement: "inline",
    title: "That analysis is not running in the worker this request reached.",
    retry: null,
  },

  // -- the client's own mistakes -------------------------------------------
  validation: {
    // Should be unreachable from this UI. Seeing one is a bug in the UI, not
    // in the request the user made, so the API's message is shown verbatim.
    placement: "inline",
    title: (error) => error.message,
    retry: null,
  },
  unknown_route: {
    placement: "inline",
    title: (error) => error.message,
    retry: null,
  },
  unauthorized: {
    placement: "banner",
    title: "This deployment requires an API key that this page is not sending.",
    retry: null,
  },

  // -- codes this front end mints itself -----------------------------------
  // Not from the API. They describe things that happen to a *browser*, which
  // the API has no way to observe: a stream that stopped, an upload the user
  // navigated away from.
  stream_incomplete: {
    placement: "inline",
    title: "The answer stopped before it finished.",
    retry: "Ask again",
  },
  upload_aborted: {
    placement: "inline",
    title: "The upload was cancelled.",
    retry: null,
  },

  // -- upstream and infrastructure -----------------------------------------
  upstream_failure: {
    placement: "replaces-card",
    title: "The model provider did not answer.",
    retry: "Retry",
  },
  metrics_unavailable: {
    placement: "inline",
    title: "Metrics are not available yet.",
    retry: null,
  },
  internal: {
    placement: "replaces-card",
    title: "The analyzer failed to handle that.",
    retry: "Retry",
  },
  unreachable: {
    placement: "replaces-card",
    title: "Could not reach the analyzer.",
    retry: "Retry",
  },
};

/** The fallback. Never a blank pane, never a thrown error, never a spinner
 *  that outlives the request that started it. */
const FALLBACK: Row = {
  placement: "inline",
  title: (error) => error.message || "Something went wrong.",
  retry: "Retry",
};

export function surfaceFor(error: ApiError): ErrorSurface {
  const row = TABLE[error.code] ?? FALLBACK;
  const title = typeof row.title === "function" ? row.title(error) : row.title;
  return {
    placement: row.placement,
    title,
    body: row.echo && error.message !== title ? error.message : undefined,
    hint: error.hint,
    retry: row.retry,
    traceId: error.traceId,
  };
}

/** Every code this table knows. Exported for the test that asserts §4 is
 *  covered, not for runtime use. */
export const CODES = Object.keys(TABLE);
