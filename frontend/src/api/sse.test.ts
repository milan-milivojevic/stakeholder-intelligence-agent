import { describe, expect, it } from "vitest";

import { decodeSseStream, parseInsightStreamEvent, parseInterviewStreamEvent } from "./sse";
import { ContractError } from "./validation";

function streamChunks(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>) {
  const events = [];
  for await (const event of decodeSseStream(stream)) {
    events.push(event);
  }
  return events;
}

describe("safe SSE boundary", () => {
  it("decodes chunked ordered interview events", async () => {
    const events = await collect(
      streamChunks(
        'event: status\ndata: {"stage":"interview","status":"started",',
        '"message_id":"message_1","correlation_id":"correlation_1"}\n\n',
        'event: token\ndata: {"message_id":"message_1","sequence":1,',
        '"delta":"What ","correlation_id":"correlation_1"}\n\n',
        'event: message\ndata: {"message_id":"message_1","stakeholder_turn_index":0,',
        '"assistant_turn_index":1,"assistant_text":"What changed?","correlation_id":"correlation_1"}\n\n',
      ),
    );

    expect(events.map(parseInterviewStreamEvent)).toEqual([
      {
        event: "status",
        data: {
          stage: "interview",
          status: "started",
          message_id: "message_1",
          correlation_id: "correlation_1",
        },
      },
      {
        event: "token",
        data: {
          message_id: "message_1",
          sequence: 1,
          delta: "What ",
          correlation_id: "correlation_1",
        },
      },
      {
        event: "message",
        data: {
          message_id: "message_1",
          stakeholder_turn_index: 0,
          assistant_turn_index: 1,
          assistant_text: "What changed?",
          correlation_id: "correlation_1",
        },
      },
    ]);
  });

  it("parses allowlisted insight progress", () => {
    expect(
      parseInsightStreamEvent({
        event: "progress",
        id: null,
        data: {
          event_id: "event_1",
          occurred_at: "2026-07-15T20:00:00Z",
          actor: "topic-researcher",
          action: "research_completed",
          from_status: "researching",
          to_status: "editing",
          topic_id: "topic_1",
          source_ids: ["source_1"],
          evidence_ids: ["evidence_1"],
          artifact_name: "findings/topic_1.md",
          failure_code: null,
          correlation_id: "correlation_1",
        },
      }),
    ).toMatchObject({ event: "progress", data: { event_id: "event_1" } });
  });

  it("decodes CRLF frames, comments, identifiers, and final unterminated frames", async () => {
    await expect(
      collect(
        streamChunks(
          ': keepalive\r\nid: event-1\r\nevent: status\r\ndata: {"stage":"interview",\r\ndata: "status":"started","message_id":"message_1","correlation_id":"correlation_1"}\r\n\r\n',
          'event: failure\ndata: {"status":"failed","failure_code":"EVENT_STREAM_FAILED","failure_message":"The stream failed.","correlation_id":"correlation_1"}',
        ),
      ),
    ).resolves.toMatchObject([
      { event: "status", id: "event-1" },
      { event: "failure", id: null },
    ]);
  });

  it("ignores frames without data", async () => {
    await expect(collect(streamChunks("event: status\nid: event-1\n\n"))).resolves.toEqual([]);
  });

  it("parses safe failure projections for both streams", () => {
    const failure = {
      event: "failure",
      id: null,
      data: {
        status: "failed",
        failure_code: "EVENT_STREAM_FAILED",
        failure_message: "The stream failed.",
        correlation_id: "correlation_1",
      },
    };
    expect(parseInsightStreamEvent(failure)).toMatchObject({ event: "failure" });
    expect(
      parseInterviewStreamEvent({
        ...failure,
        data: { ...failure.data, stage: "interview" },
      }),
    ).toMatchObject({ event: "failure", data: { stage: "interview" } });
  });

  it("rejects stream events outside each feature contract", () => {
    expect(() => parseInterviewStreamEvent({ event: "progress", id: null, data: {} })).toThrow(
      ContractError,
    );
    expect(() => parseInsightStreamEvent({ event: "status", id: null, data: {} })).toThrow(
      ContractError,
    );
    expect(() =>
      parseInterviewStreamEvent({
        event: "failure",
        id: null,
        data: { status: "failed", stage: "insight" },
      }),
    ).toThrow(ContractError);
    expect(() =>
      parseInterviewStreamEvent({
        event: "status",
        id: null,
        data: { stage: "interview", status: "unknown" },
      }),
    ).toThrow(ContractError);
    expect(() =>
      parseInterviewStreamEvent({
        event: "token",
        id: null,
        data: { message_id: "message_1", sequence: 0, delta: "x", correlation_id: "c" },
      }),
    ).toThrow(ContractError);
  });

  it("rejects unknown event names and malformed JSON", async () => {
    await expect(collect(streamChunks("event: reasoning\ndata: {}\n\n"))).rejects.toThrow(
      ContractError,
    );
    await expect(collect(streamChunks("event: status\ndata: not-json\n\n"))).rejects.toThrow(
      ContractError,
    );
  });

  it("rejects an event stream that exceeds the bounded buffer", async () => {
    await expect(collect(streamChunks("x".repeat(262_145)))).rejects.toThrow(/buffer limit/u);
  });
});
