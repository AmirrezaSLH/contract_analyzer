import { describe, expect, it } from "vitest";
import { analysisPhase, type PhaseInput } from "../src/views/Analysis/phase";

/**
 * The regression this file exists for.
 *
 * TanStack Query v5 reports `isPending: true` for a *disabled* query -- one
 * whose `enabled` is false because there is nothing to fetch. The view's guard
 * was written as `!analysis.isPending`, which is therefore never true on a
 * document that has never been analysed. The empty state carrying the Run
 * button never rendered: the page showed a header, a tab bar, and nothing
 * else, and the Analyse action looked like a dead button.
 *
 * Nothing threw and nothing logged, which is what makes this worth a test
 * rather than a fix: the failure mode of a render guard is *absence*.
 */

const base: PhaseInput = {
  documentLoading: false,
  analysisId: null,
  analysisFetching: false,
  status: undefined,
  submitting: false,
};

const phase = (over: Partial<PhaseInput> = {}) => analysisPhase({ ...base, ...over });

describe("analysisPhase", () => {
  it("offers the Run button on a document that has never been analysed", () => {
    // A disabled query is idle, not loading. This is the regression.
    expect(phase({ analysisId: null, analysisFetching: false })).toBe("none");
  });

  it("waits while the document itself is still loading", () => {
    // Otherwise "has not been analysed yet" flashes before the report it has.
    expect(phase({ documentLoading: true })).toBe("loading");
    expect(phase({ documentLoading: true, analysisId: "17" })).toBe("loading");
  });

  it("waits while a submission is in flight", () => {
    expect(phase({ submitting: true })).toBe("loading");
  });

  it("waits while an existing analysis is being fetched for the first time", () => {
    expect(phase({ analysisId: "17", analysisFetching: true, status: undefined })).toBe("loading");
  });

  it.each(["queued", "running"])("shows the progress card while %s", (status) => {
    expect(phase({ analysisId: "17", status })).toBe("live");
  });

  it.each(["done", "failed", "cancelled", "interrupted"])("settles on %s", (status) => {
    expect(phase({ analysisId: "17", status })).toBe("settled");
  });

  it("never returns loading forever when there is nothing to load", () => {
    // The shape of the original bug, stated as an invariant: with no id and
    // nothing in flight, the view must always reach a state a user can act on.
    expect(phase()).not.toBe("loading");
  });

  it("falls back to the empty state if a fetch ended with no status", () => {
    // A cache entry that was removed, or a query that errored. Better to offer
    // the Run button than to render nothing.
    expect(phase({ analysisId: "17", analysisFetching: false, status: undefined })).toBe("none");
  });
});
