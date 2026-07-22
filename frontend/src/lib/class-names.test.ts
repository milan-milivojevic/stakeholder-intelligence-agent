import { describe, expect, it } from "vitest";

import { classNames } from "./class-names";

describe("classNames", () => {
  it("keeps only defined class strings in order", () => {
    expect(classNames("base", false, null, undefined, "active")).toBe("base active");
  });
});
