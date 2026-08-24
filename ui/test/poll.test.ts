import { describe, expect, it } from "vitest";
import { isTerminal, pollInterval } from "../src/hooks/useAnalysis";

/**
 * The poll stops on a terminal status.
 *
 * This is the one behaviour a dependency bump could break silently: the
 * `refetchInterval` predicate's signature changed between majors, and a poll
 * that never stops is invisible until someone opens the network panel. The
 * predicate is a plain function so it can be asserted without React.
 */

describe("pollInterval", () => {
  it("polls while a run is in flight", () => {
    expect(pollInterval("queued")).toBe(2000);
    expect(pollInterval("running")).toBe(2000);
  });

  it.each(["done", "failed", "cancelled", "interrupted"])("stops on %s", (status) => {
    expect(pollInterval(status)).toBe(false);
  });

  it("does not poll before there is anything to poll", () => {
    expect(pollInterval(undefined)).toBe(false);
  });
});

describe("isTerminal", () => {
  it("agrees with the poll about which statuses are over", () => {
    for (const status of ["queued", "running", "done", "failed", "cancelled", "interrupted"]) {
      expect(isTerminal(status)).toBe(pollInterval(status) === false);
    }
  });
});
