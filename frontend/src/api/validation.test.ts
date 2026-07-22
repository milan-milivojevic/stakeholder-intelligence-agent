import { describe, expect, it } from "vitest";

import {
  asRecord,
  ContractError,
  nullableString,
  parseApiErrorResponse,
  parseBrowserSessionView,
  parseOperationResponse,
  parseSafeFailureEvent,
  parseSafeRunEvent,
  requiredNumber,
  requiredString,
  requiredStringArray,
  requireExactKeys,
} from "./validation";

const safePmSession = {
  principal_type: "pm",
  access_session_id: "access_session_safe",
  expires_at: "2026-07-16T08:00:00Z",
  engagement_id: "engagement_alpha",
  stakeholder_id: null,
  interview_session_id: null,
  thread_id: null,
};

describe("browser contract validation", () => {
  it("accepts a safe PM session view", () => {
    expect(parseBrowserSessionView(safePmSession)).toEqual(safePmSession);
  });

  it("requires the complete immutable stakeholder mapping", () => {
    expect(() =>
      parseBrowserSessionView({
        ...safePmSession,
        principal_type: "stakeholder",
        stakeholder_id: "stakeholder_alpha",
      }),
    ).toThrow(ContractError);
  });

  it("accepts a complete immutable stakeholder mapping", () => {
    expect(
      parseBrowserSessionView({
        ...safePmSession,
        principal_type: "stakeholder",
        stakeholder_id: "stakeholder_alpha",
        interview_session_id: "interview_alpha",
        thread_id: "thread_alpha",
      }),
    ).toMatchObject({
      principal_type: "stakeholder",
      stakeholder_id: "stakeholder_alpha",
      thread_id: "thread_alpha",
    });
  });

  it("rejects PM sessions carrying stakeholder scope", () => {
    expect(() =>
      parseBrowserSessionView({ ...safePmSession, stakeholder_id: "stakeholder_alpha" }),
    ).toThrow(ContractError);
  });

  it("rejects unexpected fields and accepts only the fixed operation response", () => {
    expect(() => parseBrowserSessionView({ ...safePmSession, extra: "field" })).toThrow(
      ContractError,
    );
    expect(parseOperationResponse({ status: "ok" })).toEqual({ status: "ok" });
    expect(() => parseOperationResponse({ status: "failed" })).toThrow(ContractError);
    expect(() => requireExactKeys({ one: 1 }, ["two"])).toThrow(ContractError);
  });

  it("rejects invalid principals and timestamps", () => {
    expect(() => parseBrowserSessionView({ ...safePmSession, principal_type: "admin" })).toThrow(
      ContractError,
    );
    expect(() => parseBrowserSessionView({ ...safePmSession, expires_at: "not-a-time" })).toThrow(
      ContractError,
    );
  });

  it.each(["access_token", "invitation_token", "bootstrap_token", "token_hash", "cookie"])(
    "rejects the prohibited browser field %s",
    (field) => {
      expect(() => parseBrowserSessionView({ ...safePmSession, [field]: "prohibited" })).toThrow(
        /prohibited secret field/u,
      );
    },
  );

  it("parses only the closed safe error envelope", () => {
    expect(
      parseApiErrorResponse({
        error: {
          code: "ACCESS_DENIED",
          message: "Access is not authorized.",
          correlation_id: "correlation_safe",
        },
      }),
    ).toEqual({
      error: {
        code: "ACCESS_DENIED",
        message: "Access is not authorized.",
        correlation_id: "correlation_safe",
      },
    });
  });

  it("validates primitive contract fields defensively", () => {
    const record = { text: "value", optional: null, count: 2, ids: ["one", "two"] };
    expect(asRecord(record)).toBe(record);
    expect(requiredString(record, "text")).toBe("value");
    expect(nullableString(record, "optional")).toBeNull();
    expect(nullableString({ optional: "value" }, "optional")).toBe("value");
    expect(requiredNumber(record, "count")).toBe(2);
    expect(requiredStringArray(record, "ids")).toEqual(["one", "two"]);

    expect(() => asRecord(null)).toThrow(ContractError);
    expect(() => asRecord([])).toThrow(ContractError);
    expect(() => requiredString({ text: "" }, "text")).toThrow(ContractError);
    expect(() => nullableString({ optional: 1 }, "optional")).toThrow(ContractError);
    expect(() => requiredNumber({ count: Number.NaN }, "count")).toThrow(ContractError);
    expect(() => requiredStringArray({ ids: ["one", 2] }, "ids")).toThrow(ContractError);
  });

  it("parses safe failure and progress projections", () => {
    expect(
      parseSafeFailureEvent({
        status: "failed",
        failure_code: "EVENT_STREAM_FAILED",
        failure_message: "The progress stream failed.",
        correlation_id: "correlation_1",
      }),
    ).toMatchObject({ status: "failed", failure_code: "EVENT_STREAM_FAILED" });
    expect(() => parseSafeFailureEvent({ status: "complete" })).toThrow(ContractError);

    expect(
      parseSafeRunEvent({
        event_id: "event_1",
        occurred_at: "2026-07-15T20:00:00Z",
        actor: "report-editor",
        action: "report_completed",
        from_status: null,
        to_status: "complete",
        topic_id: null,
        source_ids: [],
        evidence_ids: ["evidence_1"],
        artifact_name: null,
        failure_code: null,
        correlation_id: "correlation_1",
      }),
    ).toMatchObject({ event_id: "event_1", to_status: "complete" });
  });
});
