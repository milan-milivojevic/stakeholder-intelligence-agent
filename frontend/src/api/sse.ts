import type {
  InsightStreamEvent,
  InterviewMessageEvent,
  InterviewStatusEvent,
  InterviewStreamEvent,
  InterviewTokenEvent,
} from "./contracts";
import {
  asRecord,
  ContractError,
  parseSafeFailureEvent,
  parseSafeRunEvent,
  requiredNumber,
  requiredString,
} from "./validation";

const maximumBufferedCharacters = 262_144;
const allowedEventNames = new Set(["failure", "message", "progress", "status", "token"]);

export interface DecodedSseEvent {
  event: string;
  data: unknown;
  id: string | null;
}

function decodeFrame(frame: string): DecodedSseEvent | null {
  let event = "message";
  let id: string | null = null;
  const dataLines: string[] = [];

  for (const line of frame.split(/\r?\n/u)) {
    if (line.length === 0 || line.startsWith(":")) {
      continue;
    }
    const separator = line.indexOf(":");
    const field = separator === -1 ? line : line.slice(0, separator);
    const rawValue = separator === -1 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "event") {
      event = value;
    } else if (field === "data") {
      dataLines.push(value);
    } else if (field === "id" && !value.includes("\u0000")) {
      id = value;
    }
  }

  if (dataLines.length === 0) {
    return null;
  }
  if (!allowedEventNames.has(event)) {
    throw new ContractError("The server emitted an unapproved event type.");
  }
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) as unknown, id };
  } catch {
    throw new ContractError("The server emitted malformed event data.");
  }
}

export async function* decodeSseStream(
  stream: ReadableStream<Uint8Array>,
): AsyncGenerator<DecodedSseEvent> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      if (buffer.length > maximumBufferedCharacters) {
        throw new ContractError("The event stream exceeded its safe buffer limit.");
      }

      let boundary = buffer.search(/\r?\n\r?\n/u);
      while (boundary !== -1) {
        const delimiterLength = buffer.startsWith("\r\n\r\n", boundary) ? 4 : 2;
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + delimiterLength);
        const decoded = decodeFrame(frame);
        if (decoded !== null) {
          yield decoded;
        }
        boundary = buffer.search(/\r?\n\r?\n/u);
      }

      if (done) {
        const decoded = decodeFrame(buffer);
        if (decoded !== null) {
          yield decoded;
        }
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseInterviewStatus(value: unknown): InterviewStatusEvent {
  const record = asRecord(value);
  if (
    record.stage !== "interview" ||
    (record.status !== "started" && record.status !== "succeeded")
  ) {
    throw new ContractError();
  }
  return {
    stage: "interview",
    status: record.status,
    message_id: requiredString(record, "message_id"),
    correlation_id: requiredString(record, "correlation_id"),
  };
}

function parseInterviewMessage(value: unknown): InterviewMessageEvent {
  const record = asRecord(value);
  return {
    message_id: requiredString(record, "message_id"),
    stakeholder_turn_index: requiredNumber(record, "stakeholder_turn_index"),
    assistant_turn_index: requiredNumber(record, "assistant_turn_index"),
    assistant_text: requiredString(record, "assistant_text"),
    correlation_id: requiredString(record, "correlation_id"),
  };
}

function parseInterviewToken(value: unknown): InterviewTokenEvent {
  const record = asRecord(value);
  const sequence = requiredNumber(record, "sequence");
  if (!Number.isInteger(sequence) || sequence < 1) {
    throw new ContractError();
  }
  return {
    message_id: requiredString(record, "message_id"),
    sequence,
    delta: requiredString(record, "delta"),
    correlation_id: requiredString(record, "correlation_id"),
  };
}

export function parseInterviewStreamEvent(decoded: DecodedSseEvent): InterviewStreamEvent {
  if (decoded.event === "status") {
    return { event: "status", data: parseInterviewStatus(decoded.data) };
  }
  if (decoded.event === "message") {
    return { event: "message", data: parseInterviewMessage(decoded.data) };
  }
  if (decoded.event === "token") {
    return { event: "token", data: parseInterviewToken(decoded.data) };
  }
  if (decoded.event === "failure") {
    const record = asRecord(decoded.data);
    if (record.stage !== "interview") {
      throw new ContractError();
    }
    return {
      event: "failure",
      data: { stage: "interview", ...parseSafeFailureEvent(record) },
    };
  }
  throw new ContractError("The interview stream emitted an unapproved event type.");
}

export function parseInsightStreamEvent(decoded: DecodedSseEvent): InsightStreamEvent {
  if (decoded.event === "progress") {
    return { event: "progress", data: parseSafeRunEvent(decoded.data) };
  }
  if (decoded.event === "failure") {
    return { event: "failure", data: parseSafeFailureEvent(decoded.data) };
  }
  throw new ContractError("The insight stream emitted an unapproved event type.");
}
