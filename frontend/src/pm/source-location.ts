import type { SourceLocation } from "./contracts";

export function describeSourceLocation(location: SourceLocation): string {
  switch (location.kind) {
    case "pdf_page":
      return `${location.filename}, page ${String(location.page)}`;
    case "docx_rendered_page":
      return `${location.filename}, rendered page ${String(location.rendered_page)}${
        location.section === null ? "" : `, section ${location.section}`
      }${location.paragraph === null ? "" : `, paragraph ${String(location.paragraph)}`}`;
    case "pptx_slide":
      return `${location.filename}, slide ${String(location.slide)}${
        location.shape_identifier === null ? "" : `, shape ${location.shape_identifier}`
      }`;
    case "xlsx_range":
      return `${location.filename}, ${location.sheet}!${location.cell_range}`;
    case "image_region":
      return `${location.filename}${
        location.region === null ? "" : `, region ${location.region}`
      }${location.image_index === null ? "" : `, image ${String(location.image_index)}`}`;
    case "transcript_turns":
      return `Interview transcript, turns ${String(location.turn_start)}–${String(location.turn_end)}`;
  }
}
