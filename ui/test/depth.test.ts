import { describe, expect, it } from "vitest";
import { DEPTHS, topKFor } from "../src/views/Chat/depth";

/**
 * Depth hides `retrieval_top_k`, and the one thing that must hold is that
 * `medium` *is* the configured default -- otherwise the control silently
 * changes retrieval the moment someone opens the chat.
 */

describe("the depth mapping", () => {
  it("makes medium exactly settings.retrieval_top_k", () => {
    for (const configured of [1, 3, 4, 6, 8, 12, 20]) {
      expect(topKFor("medium", configured)).toBe(configured);
    }
  });

  it("is total over the three options", () => {
    for (const depth of DEPTHS) {
      expect(Number.isInteger(topKFor(depth, 6))).toBe(true);
    }
  });

  it("orders shallow below medium below deep", () => {
    expect(topKFor("shallow", 6)).toBeLessThan(topKFor("medium", 6));
    expect(topKFor("deep", 6)).toBeGreaterThan(topKFor("medium", 6));
  });

  it("clamps to the 1..20 the API accepts", () => {
    // `POST /chat` validates `top_k` at ge=1 le=20. A mapping that could
    // produce 0 or 40 would turn a dropdown into a 422.
    for (const configured of [1, 2, 15, 20]) {
      for (const depth of DEPTHS) {
        const k = topKFor(depth, configured);
        expect(k).toBeGreaterThanOrEqual(1);
        expect(k).toBeLessThanOrEqual(20);
      }
    }
  });

  it("does not collapse shallow to zero at the smallest configured value", () => {
    expect(topKFor("shallow", 1)).toBe(1);
  });
});
