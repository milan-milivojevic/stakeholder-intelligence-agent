import { describe, expect, it } from "vitest";

import type { SourceLocation } from "./contracts";
import { describeSourceLocation } from "./source-location";

describe("describeSourceLocation", () => {
  it.each<[SourceLocation, string]>([
    [
      { kind: "pdf_page", filename: "source.pdf", page: 2, bounding_box: null },
      "source.pdf, page 2",
    ],
    [
      {
        kind: "docx_rendered_page",
        filename: "source.docx",
        rendered_page: 3,
        section: "Scope",
        paragraph: 4,
        bounding_box: null,
      },
      "source.docx, rendered page 3, section Scope, paragraph 4",
    ],
    [
      {
        kind: "pptx_slide",
        filename: "source.pptx",
        slide: 5,
        shape_identifier: "Title 1",
        bounding_box: null,
      },
      "source.pptx, slide 5, shape Title 1",
    ],
    [
      {
        kind: "xlsx_range",
        filename: "source.xlsx",
        sheet: "Ownership",
        cell_range: "A1:C4",
        chart_identifier: null,
        image_identifier: null,
      },
      "source.xlsx, Ownership!A1:C4",
    ],
    [
      {
        kind: "image_region",
        filename: "source.png",
        image_index: 2,
        region: "upper-right",
        bounding_box: null,
      },
      "source.png, region upper-right, image 2",
    ],
    [
      {
        kind: "transcript_turns",
        stakeholder_id: "stakeholder-alpha",
        transcript_id: "transcript-alpha",
        turn_start: 0,
        turn_end: 4,
      },
      "Interview transcript, turns 0–4",
    ],
  ])("formats a closed location without host paths", (location, expected) => {
    expect(describeSourceLocation(location)).toBe(expected);
  });

  it("omits absent optional locator labels", () => {
    expect(
      describeSourceLocation({
        kind: "docx_rendered_page",
        filename: "source.docx",
        rendered_page: 1,
        section: null,
        paragraph: null,
        bounding_box: null,
      }),
    ).toBe("source.docx, rendered page 1");
    expect(
      describeSourceLocation({
        kind: "pptx_slide",
        filename: "source.pptx",
        slide: 1,
        shape_identifier: null,
        bounding_box: null,
      }),
    ).toBe("source.pptx, slide 1");
    expect(
      describeSourceLocation({
        kind: "image_region",
        filename: "source.jpeg",
        image_index: null,
        region: "full image",
        bounding_box: null,
      }),
    ).toBe("source.jpeg, region full image");
  });
});
