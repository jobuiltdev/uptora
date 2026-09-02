import { describe, expect, it } from "vitest";
import { duration } from "./format";

describe("duration", () => {
  it("formats operational durations without a large date dependency", () => {
    expect(duration(4692)).toBe("1h 18m");
    expect(duration(null)).toBe("Ongoing");
  });
});
