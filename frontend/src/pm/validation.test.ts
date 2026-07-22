import { describe, expect, it } from "vitest";

import {
  completeReport,
  documentProcessingDetails,
  documentSummary,
  engagement,
  evidenceResponse,
  interviewPreview,
  interviewSession,
  invitation,
  reportResponse,
  stakeholder,
  terminalRun,
  uploadResponse,
} from "../test/pm-fixtures";
import type { SourceLocation } from "./contracts";
import {
  parseDocumentListResponse,
  parseDocumentProcessingDetailsResponse,
  parseEngagementContextResponse,
  parseEngagementListResponse,
  parseEvidenceDrillDownResponse,
  parseInsightReportResponse,
  parseInsightRunListResponse,
  parseInsightStatusResponse,
  parseInterviewPreviewResponse,
  parseInterviewSessionListResponse,
  parseInvitationIssuedResponse,
  parseInvitationListResponse,
  parseInvitationSummary,
  parseStakeholderListResponse,
  parseStakeholderResponse,
  parseUploadResponse,
} from "./validation";

describe("PM response validation", () => {
  it("parses every PM setup, upload, interview, and status envelope", () => {
    expect(parseEngagementListResponse({ engagements: [engagement] }).engagements).toEqual([
      engagement,
    ]);
    expect(parseEngagementContextResponse({ engagement }).engagement).toEqual(engagement);
    expect(parseStakeholderListResponse({ stakeholders: [stakeholder] }).stakeholders).toEqual([
      stakeholder,
    ]);
    expect(parseStakeholderResponse({ stakeholder }).stakeholder).toEqual(stakeholder);
    expect(parseInvitationListResponse({ invitations: [invitation] }).invitations).toEqual([
      invitation,
    ]);
    expect(parseInvitationSummary(invitation)).toEqual(invitation);
    expect(parseInvitationIssuedResponse({ invitation, invitation_token: "A".repeat(48) })).toEqual(
      { invitation, invitation_token: "A".repeat(48) },
    );
    expect(parseDocumentListResponse({ documents: [documentSummary] }).documents).toEqual([
      documentSummary,
    ]);
    expect(parseUploadResponse(uploadResponse)).toEqual(uploadResponse);
    expect(parseDocumentProcessingDetailsResponse(documentProcessingDetails)).toEqual(
      documentProcessingDetails,
    );
    expect(
      parseInterviewSessionListResponse({ interview_sessions: [interviewSession] })
        .interview_sessions,
    ).toEqual([interviewSession]);
    expect(parseInterviewPreviewResponse(interviewPreview)).toEqual(interviewPreview);
    expect(parseInsightStatusResponse({ run: terminalRun("complete") }).run.status).toBe(
      "complete",
    );
    expect(parseInsightRunListResponse({ runs: [terminalRun("complete")] }).runs).toEqual([
      terminalRun("complete"),
    ]);
  });

  it("parses the complete report and bound evidence drill-down", () => {
    expect(parseInsightReportResponse(reportResponse())).toEqual(reportResponse());
    expect(parseEvidenceDrillDownResponse(evidenceResponse)).toEqual(evidenceResponse);
  });

  it("rejects execution metrics that contradict configured runtime bounds", () => {
    const response = reportResponse();
    response.metrics.topic_count = response.metrics.configured_topic_limit + 1;

    expect(() => parseInsightReportResponse(response)).toThrow(/contract/u);
  });

  it("accepts all six closed source-location variants", () => {
    const locations: SourceLocation[] = [
      {
        kind: "pdf_page",
        filename: "source.pdf",
        page: 2,
        bounding_box: {
          x0: 0,
          y0: 1,
          x1: 10,
          y1: 20,
          coordinate_space: "points",
        },
      },
      {
        kind: "docx_rendered_page",
        filename: "source.docx",
        rendered_page: 3,
        section: "Responsibilities",
        paragraph: 4,
        bounding_box: null,
      },
      {
        kind: "pptx_slide",
        filename: "source.pptx",
        slide: 5,
        shape_identifier: "Title 1",
        bounding_box: null,
      },
      {
        kind: "xlsx_range",
        filename: "source.xlsx",
        sheet: "Ownership",
        cell_range: "A1:C4",
        chart_identifier: null,
        image_identifier: null,
      },
      {
        kind: "image_region",
        filename: "source.png",
        image_index: 1,
        region: "upper-right",
        bounding_box: {
          x0: 0,
          y0: 0,
          x1: 1,
          y1: 1,
          coordinate_space: "normalized",
        },
      },
      {
        kind: "transcript_turns",
        stakeholder_id: "stakeholder-alpha",
        transcript_id: "transcript-alpha",
        turn_start: 0,
        turn_end: 2,
      },
    ];

    for (const location of locations) {
      const response = reportResponse();
      const citation = response.report.citations.at(0);
      if (citation === undefined) {
        throw new Error("The report fixture requires one citation.");
      }
      citation.source_location = location;
      expect(parseInsightReportResponse(response).report.citations[0]?.source_location).toEqual(
        location,
      );
    }
  });

  it("rejects unknown fields, malformed timestamps, short invitations, and non-integral values", () => {
    expect(() =>
      parseEngagementListResponse({ engagements: [{ ...engagement, access_token: "prohibited" }] }),
    ).toThrow(/contract/u);
    expect(() =>
      parseEngagementContextResponse({
        engagement: { ...engagement, created_at: "not-a-timestamp" },
      }),
    ).toThrow(/contract/u);
    expect(() =>
      parseInvitationIssuedResponse({ invitation, invitation_token: "too-short" }),
    ).toThrow(/contract/u);
    expect(() => parseUploadResponse({ ...uploadResponse, chunk_count: 1.5 })).toThrow(/contract/u);
    expect(() =>
      parseDocumentProcessingDetailsResponse({
        ...documentProcessingDetails,
        element_count: -1,
      }),
    ).toThrow(/contract/u);
    expect(() =>
      parseInterviewPreviewResponse({
        ...interviewPreview,
        transcript: {
          ...interviewPreview.transcript,
          status: "draft",
          finalized_at: null,
          content_hash: null,
        },
      }),
    ).toThrow(/finalized transcript/u);
  });

  it("rejects report identity, status, citation, source-kind, and artifact-integrity mismatches", () => {
    const wrongRun = reportResponse();
    wrongRun.run = { ...wrongRun.run, report_id: "different-report" };
    expect(() => parseInsightReportResponse(wrongRun)).toThrow(/contract/u);

    const wrongStatus = reportResponse();
    wrongStatus.run = { ...wrongStatus.run, status: "partial" };
    expect(() => parseInsightReportResponse(wrongStatus)).toThrow(/contract/u);

    const missingCitation = reportResponse();
    const firstCitation = completeReport().citations.at(0);
    if (firstCitation === undefined) {
      throw new Error("The report fixture requires one citation.");
    }
    missingCitation.report = { ...completeReport(), citations: [firstCitation] };
    expect(() => parseInsightReportResponse(missingCitation)).toThrow(/contract/u);

    const unknownLocation = reportResponse();
    const unknownCitation = unknownLocation.report.citations.at(0);
    if (unknownCitation === undefined) {
      throw new Error("The report fixture requires one citation.");
    }
    unknownCitation.source_location = {
      kind: "pdf_page",
      filename: "source.pdf",
      page: 1,
      bounding_box: null,
    };
    const rawUnknown = structuredClone(unknownLocation) as unknown as {
      report: { citations: { source_location: Record<string, unknown> }[] };
    };
    const rawCitation = rawUnknown.report.citations.at(0);
    if (rawCitation === undefined) {
      throw new Error("The report fixture requires one citation.");
    }
    rawCitation.source_location.kind = "host_path";
    expect(() => parseInsightReportResponse(rawUnknown)).toThrow(/contract/u);

    const wrongHash = structuredClone(evidenceResponse);
    wrongHash.original.content_hash = "not-a-content-hash";
    expect(() => parseEvidenceDrillDownResponse(wrongHash)).toThrow(/contract/u);
  });
});
