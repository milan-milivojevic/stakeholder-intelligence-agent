"""Application service joining raw-turn persistence with the checkpointed agent."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from stakeholder_intelligence_agent.errors import (
    InterviewCompletionNotReadyError,
    InterviewLifecycleError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id
from stakeholder_intelligence_agent.interview.prompts import (
    completion_is_recommended,
    opening_interview_question,
)
from stakeholder_intelligence_agent.interview.types import (
    FinalizationResult,
    InterviewStartResult,
    InterviewTokenChunk,
    InterviewTurnResult,
    StoredTranscriptTurn,
    TranscriptIngestionResult,
)

_MESSAGE_STREAM_ITEM_SIZE = 2
_SAFE_STREAM_CHUNK_CHARACTERS = 96

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel

    from stakeholder_intelligence_agent.contracts import InterviewRuntimeContext
    from stakeholder_intelligence_agent.interview.ingestion import TranscriptIngestionService
    from stakeholder_intelligence_agent.interview.repository import TranscriptRepository


class InterviewConversationService:
    """Persist exact input/output while LangGraph owns resumable conversation state."""

    def __init__(
        self,
        *,
        repository: TranscriptRepository,
        graph: Pregel[Any, Any, Any, Any],
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        ingestion: TranscriptIngestionService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._graph = graph
        self._checkpointer = checkpointer or getattr(graph, "checkpointer", None)
        self._ingestion = ingestion
        self._clock = clock or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def initialize(self) -> None:
        """Prepare raw-transcript persistence and optional indexing dependencies."""
        await self._repository.initialize()
        if self._ingestion is not None:
            await self._ingestion.initialize()

    async def start(self, context: InterviewRuntimeContext) -> InterviewStartResult:
        """Persist one deterministic opening question and seed the checkpoint once."""
        lock = await self._lock_for(context)
        async with lock:
            return await self._start_unlocked(context)

    async def _start_unlocked(self, context: InterviewRuntimeContext) -> InterviewStartResult:
        existing = await self._repository.list_turns(context.access, now=self._clock())
        if existing:
            return InterviewStartResult(opening_turn=existing[0], idempotent=True)
        thread_id = context.access.thread_id
        interview_session_id = context.access.interview_session_id
        if thread_id is None or interview_session_id is None:
            raise InterviewLifecycleError
        opening_text = opening_interview_question(
            role=context.role,
            department=context.department,
        )
        opening_message_id = stable_id("interview-opening", interview_session_id)
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        await self._graph.aupdate_state(
            config,
            {"messages": [AIMessage(content=opening_text, id=opening_message_id)]},
        )
        opening_turn = await self._repository.append_turn(
            context.access,
            speaker="assistant",
            original_text=opening_text,
            checkpoint_message_id=opening_message_id,
            now=self._clock(),
        )
        return InterviewStartResult(opening_turn=opening_turn, idempotent=False)

    async def submit_turn(
        self,
        context: InterviewRuntimeContext,
        *,
        original_text: str,
        request_message_id: str,
    ) -> InterviewTurnResult:
        """Store exact input before PII middleware, invoke, then append the safe output."""
        lock = await self._lock_for(context)
        async with lock:
            return await self._submit_turn_unlocked(
                context,
                original_text=original_text,
                request_message_id=request_message_id,
            )

    async def stream_turn(
        self,
        context: InterviewRuntimeContext,
        *,
        original_text: str,
        request_message_id: str,
    ) -> AsyncIterator[InterviewTokenChunk | InterviewTurnResult]:
        """Buffer model deltas until PII middleware finalizes the safe graph state."""
        lock = await self._lock_for(context)
        async with lock:
            async for item in self._stream_turn_unlocked(
                context,
                original_text=original_text,
                request_message_id=request_message_id,
            ):
                yield item

    async def _stream_turn_unlocked(
        self,
        context: InterviewRuntimeContext,
        *,
        original_text: str,
        request_message_id: str,
    ) -> AsyncIterator[InterviewTokenChunk | InterviewTurnResult]:
        stakeholder_turn = await self._repository.append_turn(
            context.access,
            speaker="stakeholder",
            original_text=original_text,
            checkpoint_message_id=request_message_id,
            now=self._clock(),
        )
        thread_id = context.access.thread_id
        if thread_id is None:
            raise InterviewLifecycleError
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        final_state: dict[str, object] | None = None
        async for mode, update in self._graph.astream(
            {"messages": [HumanMessage(content=original_text, id=request_message_id)]},
            config=config,
            context=context,
            stream_mode=["messages", "values"],
        ):
            if mode == "values":
                if isinstance(update, dict):
                    final_state = cast("dict[str, object]", update)
                continue
            if (
                mode != "messages"
                or not isinstance(update, tuple)
                or len(update) != _MESSAGE_STREAM_ITEM_SIZE
            ):
                continue
            message, _metadata = update
            if (
                not isinstance(message, AIMessageChunk)
                or message.tool_call_chunks
                or not message.text
            ):
                continue
            # Raw provider deltas are deliberately not emitted. LangChain's PII
            # middleware applies the authoritative output policy to graph state
            # after the model node completes, so forwarding this pre-policy chunk
            # would create a wire-only disclosure even though persistence is safe.
        if final_state is None:
            raise InterviewLifecycleError
        result = await self._persist_assistant_result(
            context,
            stakeholder_turn=stakeholder_turn,
            request_message_id=request_message_id,
            state=final_state,
        )
        for sequence, offset in enumerate(
            range(0, len(result.assistant_text), _SAFE_STREAM_CHUNK_CHARACTERS),
            start=1,
        ):
            yield InterviewTokenChunk(
                sequence=sequence,
                delta=result.assistant_text[offset : offset + _SAFE_STREAM_CHUNK_CHARACTERS],
            )
        yield result

    async def _submit_turn_unlocked(
        self,
        context: InterviewRuntimeContext,
        *,
        original_text: str,
        request_message_id: str,
    ) -> InterviewTurnResult:
        stakeholder_turn = await self._repository.append_turn(
            context.access,
            speaker="stakeholder",
            original_text=original_text,
            checkpoint_message_id=request_message_id,
            now=self._clock(),
        )
        thread_id = context.access.thread_id
        if thread_id is None:
            raise InterviewLifecycleError
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        result = await self._graph.ainvoke(
            {"messages": [HumanMessage(content=original_text, id=request_message_id)]},
            config=config,
            context=context,
        )
        state = cast("dict[str, object]", result)
        return await self._persist_assistant_result(
            context,
            stakeholder_turn=stakeholder_turn,
            request_message_id=request_message_id,
            state=state,
        )

    async def _persist_assistant_result(
        self,
        context: InterviewRuntimeContext,
        *,
        stakeholder_turn: StoredTranscriptTurn,
        request_message_id: str,
        state: dict[str, object],
    ) -> InterviewTurnResult:
        raw_messages = state.get("messages")
        if not isinstance(raw_messages, list):
            raise InterviewLifecycleError
        assistant_message = next(
            (
                message
                for message in reversed(raw_messages)
                if isinstance(message, AIMessage)
                and not message.tool_calls
                and message.text.strip()
            ),
            None,
        )
        if assistant_message is None:
            raise InterviewLifecycleError
        assistant_text = assistant_message.text
        assistant_id = assistant_message.id or stable_id(
            "assistant-message",
            request_message_id,
            sha256(assistant_text.encode("utf-8")).hexdigest(),
        )
        assistant_turn = await self._repository.append_turn(
            context.access,
            speaker="assistant",
            original_text=assistant_text,
            checkpoint_message_id=assistant_id,
            now=self._clock(),
        )
        return InterviewTurnResult(
            stakeholder_turn=stakeholder_turn,
            assistant_turn=assistant_turn,
            assistant_text=assistant_text,
            graph_state=state,
        )

    async def delete_answer(
        self,
        context: InterviewRuntimeContext,
        *,
        turn_index: int,
    ) -> tuple[StoredTranscriptTurn, ...]:
        """Remove one draft answer, its downstream turns, and their checkpoint state."""
        lock = await self._lock_for(context)
        async with lock:
            existing = await self._repository.list_turns(context.access, now=self._clock())
            target = next(
                (item for item in existing if item.value.turn_index == turn_index),
                None,
            )
            if target is None or target.value.speaker != "stakeholder":
                raise InterviewLifecycleError
            retained = tuple(item for item in existing if item.value.turn_index < turn_index)
            try:
                await self._replace_checkpoint(context, retained)
            except Exception as error:
                with contextlib.suppress(Exception):
                    await self._replace_checkpoint(context, existing)
                raise InterviewLifecycleError from error
            try:
                return await self._repository.truncate_from_stakeholder_turn(
                    context.access,
                    turn_index=turn_index,
                    now=self._clock(),
                )
            except Exception:
                with contextlib.suppress(Exception):
                    await self._replace_checkpoint(context, existing)
                raise

    async def finish(
        self,
        context: InterviewRuntimeContext,
    ) -> FinalizationResult | TranscriptIngestionResult:
        """Run the explicit atomic Finish transition, then its idempotent indexing step."""
        lock = await self._lock_for(context)
        async with lock:
            return await self._finish_unlocked(context)

    async def _finish_unlocked(
        self,
        context: InterviewRuntimeContext,
    ) -> FinalizationResult | TranscriptIngestionResult:
        snapshot = await self._repository.snapshot(context.access, now=self._clock())
        if snapshot.transcript.status == "draft" and not completion_is_recommended(
            item.value.original_text for item in snapshot.turns if item.value.speaker == "assistant"
        ):
            raise InterviewCompletionNotReadyError
        finalized = await self._repository.finalize(context.access, now=self._clock())
        if self._ingestion is None:
            return finalized
        return await self._ingestion.ingest(context.access)

    async def _replace_checkpoint(
        self,
        context: InterviewRuntimeContext,
        turns: tuple[StoredTranscriptTurn, ...],
    ) -> None:
        thread_id = context.access.thread_id
        if thread_id is None or self._checkpointer is None:
            raise InterviewLifecycleError
        await self._checkpointer.adelete_thread(thread_id)
        if not turns:
            return
        messages = [
            (
                HumanMessage(
                    content=item.value.original_text,
                    id=item.value.checkpoint_message_id or item.turn_id,
                )
                if item.value.speaker == "stakeholder"
                else AIMessage(
                    content=item.value.original_text,
                    id=item.value.checkpoint_message_id or item.turn_id,
                )
            )
            for item in turns
        ]
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        await self._graph.aupdate_state(config, {"messages": messages})

    async def _lock_for(self, context: InterviewRuntimeContext) -> asyncio.Lock:
        interview_session_id = context.access.interview_session_id
        if interview_session_id is None:
            raise InterviewLifecycleError
        async with self._locks_guard:
            return self._locks.setdefault(interview_session_id, asyncio.Lock())
