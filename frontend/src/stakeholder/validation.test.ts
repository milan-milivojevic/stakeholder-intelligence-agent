import { describe, expect, it } from "vitest";

import { ContractError } from "../api/validation";
import {
  draftStatus,
  finishResponse,
  interviewContext,
  stakeholderDocument,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import {
  parseInterviewContextResponse,
  parseInterviewFinishResponse,
  parseInterviewStatusResponse,
  parseStakeholderDocumentListResponse,
  parseStakeholderUploadResponse,
} from "./validation";

describe("stakeholder response validation", () => {
  it("parses the closed context, document, status, and finish contracts", () => {
    expect(parseInterviewContextResponse(interviewContext)).toEqual(interviewContext);
    expect(parseStakeholderDocumentListResponse({ documents: [stakeholderDocument] })).toEqual({
      documents: [stakeholderDocument],
    });
    expect(parseStakeholderUploadResponse(stakeholderUpload)).toEqual(stakeholderUpload);
    expect(parseInterviewStatusResponse(draftStatus)).toEqual(draftStatus);
    expect(parseInterviewFinishResponse(finishResponse)).toEqual(finishResponse);
  });

  it("rejects unknown fields and invalid lifecycle values", () => {
    expect(() =>
      parseInterviewContextResponse({ ...interviewContext, access_token: "forbidden" }),
    ).toThrow(ContractError);
    expect(() =>
      parseInterviewStatusResponse({
        ...draftStatus,
        interview_session: { ...draftStatus.interview_session, status: "complete" },
      }),
    ).toThrow(ContractError);
    expect(() =>
      parseInterviewStatusResponse({
        ...draftStatus,
        transcript: { ...draftStatus.transcript, content_hash: "not-a-hash" },
      }),
    ).toThrow(ContractError);
  });

  it("rejects malformed arrays and negative counters", () => {
    expect(() => parseStakeholderDocumentListResponse({ documents: {} })).toThrow(ContractError);
    expect(() => parseInterviewStatusResponse({ ...draftStatus, turn_count: -1 })).toThrow(
      ContractError,
    );
    expect(() => parseInterviewStatusResponse({ ...draftStatus, turn_count: 0 })).toThrow(
      ContractError,
    );
    expect(() =>
      parseInterviewStatusResponse({
        ...draftStatus,
        turns: [{ ...draftStatus.turns[0], speaker: "system" }],
      }),
    ).toThrow(ContractError);
    expect(() =>
      parseInterviewStatusResponse({
        ...draftStatus,
        transcript: { ...draftStatus.transcript, language_observations: null },
      }),
    ).toThrow(ContractError);
    expect(() => parseInterviewFinishResponse({ ...finishResponse, chunk_count: -1 })).toThrow(
      ContractError,
    );
  });
});
