import { describe, expect, it } from "vitest";
import { ApiError } from "../src/api/client";
import { surfaceFor } from "../src/api/errors";

/**
 * Every code the spec names has exactly one place it renders.
 *
 * The point of the table is that a view never switches on a code. These tests
 * are what stop it drifting back: if a code loses its row, the first list here
 * fails rather than a screen silently falling back to a generic message.
 */

const error = (code: string, message = "m", hint?: string) => new ApiError(400, code, message, hint);

describe("the codes the UI spec names", () => {
  it.each([
    ["unsupported_media_type", "inline"],
    ["payload_too_large", "inline"],
    ["embedder_unavailable", "replaces-card"],
    ["ingest_failed", "replaces-card"],
    ["no_api_key", "banner"],
    ["document_not_found", "full-pane"],
    ["analysis_running", "inline"],
    ["validation", "inline"],
    ["unreachable", "replaces-card"],
  ])("puts %s on the %s surface", (code, placement) => {
    expect(surfaceFor(error(code)).placement).toBe(placement);
  });
});

describe("the codes the API can return", () => {
  // Lifted from `api/errors.py` and the routes: every code this service is
  // capable of emitting. None of them may reach the fallback, because the
  // fallback prints an internal message at a user.
  const FROM_THE_API = [
    "document_not_found",
    "analysis_not_found",
    "no_api_key",
    "answer_unavailable",
    "unauthorized",
    "embedder_unavailable",
    "ingest_failed",
    "model_mismatch",
    "upstream_failure",
    "validation",
    "analysis_running",
    "not_running",
    "not_live_here",
    "metrics_unavailable",
    "logs_unavailable",
    "unknown_route",
    "internal",
  ];

  it.each(FROM_THE_API)("has a row for %s", (code) => {
    const surface = surfaceFor(error(code));
    expect(surface.title).toBeTruthy();
    expect(["inline", "replaces-card", "banner", "full-pane"]).toContain(surface.placement);
  });
});

describe("the three rules", () => {
  it("makes the hint the second line, always", () => {
    const surface = surfaceFor(error("no_api_key", "ANTHROPIC_API_KEY is not set.", "Set it in .env."));
    expect(surface.hint).toBe("Set it in .env.");
  });

  it("falls back rather than throwing on a code it has never seen", () => {
    // The API may grow a code before this table knows about it. A generic
    // inline error is a better outcome than a white screen.
    const surface = surfaceFor(error("a_code_from_the_future", "Something specific happened."));
    expect(surface.placement).toBe("inline");
    expect(surface.title).toBe("Something specific happened.");
  });

  it("never renders an empty title, even for an error with no message", () => {
    expect(surfaceFor(error("also_unknown", "")).title).toBeTruthy();
  });

  it("offers no retry where retrying cannot help", () => {
    // A .txt does not become a PDF on a second attempt.
    expect(surfaceFor(error("unsupported_media_type")).retry).toBeNull();
    expect(surfaceFor(error("no_api_key")).retry).toBeNull();
  });

  it("offers one where it can", () => {
    expect(surfaceFor(error("unreachable")).retry).toBeTruthy();
    expect(surfaceFor(error("upstream_failure")).retry).toBeTruthy();
  });

  it("echoes the API's message only where the title does not already say it", () => {
    // payload_too_large's message carries the filename and the size, which the
    // title cannot.
    expect(surfaceFor(error("payload_too_large", "big.pdf is 40 MB.")).body).toBe("big.pdf is 40 MB.");
    // no_api_key's message names an environment variable at a reviewer.
    expect(surfaceFor(error("no_api_key", "ANTHROPIC_API_KEY is not set.")).body).toBeUndefined();
  });

  it("carries the trace id through when the response had one", () => {
    const withTrace = new ApiError(500, "internal", "m", undefined, "4f2a9c1e-7b30");
    expect(surfaceFor(withTrace).traceId).toBe("4f2a9c1e-7b30");
  });
});
