"""Block Deep Agent shortcuts that would bypass required planning or subagents."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, SystemMessage, ToolCall, ToolMessage
from langgraph.types import Command

from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import (
    InsightRuntimeContext,
    ResearchPlan,
    ResearchTopic,
)
from stakeholder_intelligence_agent.contracts.common import utc_now
from stakeholder_intelligence_agent.errors import (
    AccessDeniedError,
    ArtifactStateError,
    CourseFidelityError,
    EvidencePolicyError,
    RepeatedToolFailureError,
    RuntimeScopeError,
    TodoPlanAlignmentError,
)
from stakeholder_intelligence_agent.ingestion.identity import stable_id

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.insight.repository import InsightRunRepository

_RESEARCHER = "topic-researcher"
_EDITOR = "report-editor"
_MAX_COURSE_RETRIEVALS = 5
_MIN_DIVERSE_SOURCES = 3
_MIN_COMPLEMENTARY_SOURCES = 2
_MIN_INDEPENDENT_STAKEHOLDERS = 2
_REPETITION_WINDOW = 2
_IDENTICAL_FAILURE_LIMIT = 2
_EDITOR_EVIDENCE_CORRECTION_LIMIT = 2
_DOCUMENT_SOURCE_TYPES = frozenset({"stakeholder_document", "engagement_document"})
_TODO_STATUS_ORDER = {"pending": 0, "in_progress": 1, "completed": 2}
_EDITING_TODO_FRAGMENTS = ("edit", "synthes", "draft", "compose")


class _TodoAdvanceRequiredError(CourseFidelityError):
    """Signal a safe, model-correctable dispatch attempted before explicit TODO advance."""


def _tool_name(tool: Any) -> str | None:
    """Resolve a bound tool name from either a BaseTool or provider schema."""
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    if not isinstance(tool, dict):
        return None
    direct_name = tool.get("name")
    if isinstance(direct_name, str):
        return direct_name
    function = tool.get("function")
    if isinstance(function, dict):
        function_name = function.get("name")
        if isinstance(function_name, str):
            return function_name
    return None


def _role_locked_system_message(
    request: ModelRequest[InsightRuntimeContext],
    allowed_tools: tuple[str, ...],
) -> SystemMessage:
    """Append the final role-tool instruction after generic harness middleware text."""
    existing = "" if request.system_message is None else str(request.system_message.text)
    if allowed_tools:
        action = "Call exactly one of these tools now: " + ", ".join(allowed_tools) + "."
    else:
        action = "No tool is available now; return only the short final role completion message."
    return SystemMessage(
        content=(
            f"{existing}\n\nROLE-SCOPED TOOL OVERRIDE (highest priority for this subagent): "
            "write_todos, task, and generic filesystem tools are root-only even if generic "
            f"harness text mentions them. Never call them. {action}"
        )
    )


def _tool_correction_system_message(
    request: ModelRequest[InsightRuntimeContext],
    allowed_tools: tuple[str, ...],
) -> SystemMessage:
    """Require one bounded retry when the model answered instead of taking the next action."""
    existing = "" if request.system_message is None else str(request.system_message.text)
    return SystemMessage(
        content=(
            f"{existing}\n\nTOOL-CALL CORRECTION (highest priority): the previous response was "
            "invalid because it did not take a currently permitted action. Return no explanation "
            "or completion text. Call exactly one of these tools now with valid arguments: "
            f"{', '.join(allowed_tools)}."
        )
    )


def validate_researcher_wave(messages: list[Any], *, maximum: int) -> None:
    """Reject a model turn that exceeds the approved researcher concurrency bound."""
    last_ai = next(
        (message for message in reversed(messages) if isinstance(message, AIMessage)),
        None,
    )
    if last_ai is None:
        raise CourseFidelityError
    researcher_calls = [
        call
        for call in last_ai.tool_calls
        if call["name"] == "task" and call.get("args", {}).get("subagent_type") == _RESEARCHER
    ]
    if len(researcher_calls) > maximum:
        raise CourseFidelityError


class OrderedSubagentToolMiddleware(AgentMiddleware[AgentState[Any], InsightRuntimeContext, Any]):
    """Expose only role tools and accept exactly the next required phase."""

    def __init__(self, ordered_tools: tuple[str, ...]) -> None:
        super().__init__()
        if not ordered_tools or len(set(ordered_tools)) != len(ordered_tools):
            raise CourseFidelityError
        self._ordered_tools = ordered_tools

    def wrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[[ModelRequest[InsightRuntimeContext]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Restrict a synchronous subagent call to its next required tool."""
        restricted, expected_tool = self._restrict_model_tools(request)
        return self._validate_response(handler(restricted), expected_tool)

    async def awrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[
            [ModelRequest[InsightRuntimeContext]],
            Awaitable[ModelResponse[Any]],
        ],
    ) -> ModelResponse[Any]:
        """Restrict an asynchronous subagent call to its next required tool."""
        restricted, expected_tool = self._restrict_model_tools(request)
        return self._validate_response(await handler(restricted), expected_tool)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Return a bounded, model-correctable editor evidence rejection."""
        try:
            return handler(request)
        except EvidencePolicyError:
            return self._editor_evidence_rejection(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Return a bounded async editor evidence rejection without weakening validation."""
        try:
            return await handler(request)
        except EvidencePolicyError:
            return self._editor_evidence_rejection(request)

    @staticmethod
    def _editor_evidence_rejection(request: ToolCallRequest) -> ToolMessage:
        if request.tool_call["name"] != "save_final_report":
            raise EvidencePolicyError
        state = cast("dict[str, Any]", request.state)
        messages = state.get("messages", [])
        prior_rejections = (
            sum(
                1
                for message in messages
                if isinstance(message, ToolMessage)
                and message.name == "save_final_report"
                and message.status == "error"
            )
            if isinstance(messages, list)
            else 0
        )
        if prior_rejections >= _EDITOR_EVIDENCE_CORRECTION_LIMIT:
            raise EvidencePolicyError
        return ToolMessage(
            content=(
                "The report draft was rejected by deterministic evidence validation. "
                "Correct the draft using only evidence IDs from the loaded manifests; "
                "evidence used for contradictions must also support a finding, "
                "responsibility, or operational-risk claim. Retry save_final_report."
            ),
            tool_call_id=request.tool_call["id"],
            name="save_final_report",
            status="error",
        )

    def _restrict_model_tools(
        self,
        request: ModelRequest[InsightRuntimeContext],
    ) -> tuple[ModelRequest[InsightRuntimeContext], str | None]:
        expected_tool = self._next_tool(request.messages)
        if expected_tool is None:
            return (
                request.override(
                    tools=[],
                    system_message=_role_locked_system_message(request, ()),
                ),
                None,
            )
        tools = [tool for tool in request.tools if _tool_name(tool) in self._ordered_tools]
        tool_names = [_tool_name(tool) for tool in tools]
        if len(tools) != len(self._ordered_tools) or set(tool_names) != set(self._ordered_tools):
            raise CourseFidelityError
        return (
            request.override(
                tools=tools,
                system_message=_role_locked_system_message(request, (expected_tool,)),
            ),
            expected_tool,
        )

    def _next_tool(self, messages: list[Any]) -> str | None:
        phase = 0
        for message in messages:
            if (
                not isinstance(message, ToolMessage)
                or message.status == "error"
                or message.name not in self._ordered_tools
            ):
                continue
            message_phase = self._ordered_tools.index(message.name)
            if message_phase > phase:
                raise CourseFidelityError
            if message_phase == phase:
                phase += 1
        return self._ordered_tools[phase] if phase < len(self._ordered_tools) else None

    @staticmethod
    def _validate_response(
        response: ModelResponse[Any],
        expected_tool: str | None,
    ) -> ModelResponse[Any]:
        result: list[Any] = []
        for message in response.result:
            if not isinstance(message, AIMessage):
                result.append(message)
                continue
            if expected_tool is None:
                if message.tool_calls:
                    raise CourseFidelityError
                result.append(message)
                continue
            if not message.tool_calls:
                result.append(message)
                continue
            matching = [call for call in message.tool_calls if call["name"] == expected_tool]
            if not matching:
                raise CourseFidelityError
            result.append(message.model_copy(update={"tool_calls": matching[:1]}))
        return ModelResponse(
            result=result,
            structured_response=response.structured_response,
        )


class ResearcherLoopMiddleware(AgentMiddleware[AgentState[Any], InsightRuntimeContext, Any]):
    """Enforce a bounded retrieve-think loop before research artifacts are saved."""

    _role_tools = ("scoped_retrieve", "think_tool", "save_research_artifacts")

    def __init__(self, *, max_retrievals: int) -> None:
        super().__init__()
        if not 1 <= max_retrievals <= _MAX_COURSE_RETRIEVALS:
            raise CourseFidelityError
        self._max_retrievals = max_retrievals

    def wrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[[ModelRequest[InsightRuntimeContext]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Restrict a synchronous researcher turn to its valid next action."""
        restricted, allowed_tools = self._restrict_model_tools(request)
        prior_queries = self._prior_queries(request.messages)
        try:
            return self._validate_response(
                handler(restricted),
                allowed_tools,
                prior_queries=prior_queries,
            )
        except CourseFidelityError:
            if not allowed_tools:
                raise
        corrected = restricted.override(
            system_message=_tool_correction_system_message(restricted, allowed_tools)
        )
        return self._validate_response(
            handler(corrected),
            allowed_tools,
            prior_queries=prior_queries,
        )

    async def awrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[
            [ModelRequest[InsightRuntimeContext]],
            Awaitable[ModelResponse[Any]],
        ],
    ) -> ModelResponse[Any]:
        """Restrict an asynchronous researcher turn to its valid next action."""
        restricted, allowed_tools = self._restrict_model_tools(request)
        prior_queries = self._prior_queries(request.messages)
        try:
            return self._validate_response(
                await handler(restricted),
                allowed_tools,
                prior_queries=prior_queries,
            )
        except CourseFidelityError:
            if not allowed_tools:
                raise
        corrected = restricted.override(
            system_message=_tool_correction_system_message(restricted, allowed_tools)
        )
        return self._validate_response(
            await handler(corrected),
            allowed_tools,
            prior_queries=prior_queries,
        )

    def _restrict_model_tools(
        self,
        request: ModelRequest[InsightRuntimeContext],
    ) -> tuple[ModelRequest[InsightRuntimeContext], tuple[str, ...]]:
        allowed_tools = self._allowed_tools(request.messages)
        role_tools = [tool for tool in request.tools if _tool_name(tool) in self._role_tools]
        role_tool_names = {_tool_name(tool) for tool in role_tools}
        if len(role_tools) != len(self._role_tools) or role_tool_names != set(self._role_tools):
            raise CourseFidelityError
        tools = [tool for tool in role_tools if _tool_name(tool) in allowed_tools]
        return (
            request.override(
                messages=self._compact_researcher_messages(request.messages),
                tools=tools,
                tool_choice="any" if tools else None,
                system_message=_role_locked_system_message(request, allowed_tools),
            ),
            allowed_tools,
        )

    def _allowed_tools(self, messages: list[Any]) -> tuple[str, ...]:
        phase = "retrieve"
        retrievals = 0
        for message in messages:
            if (
                not isinstance(message, ToolMessage)
                or message.status == "error"
                or message.name not in self._role_tools
            ):
                continue
            if phase in {"retrieve", "choose"} and message.name == "scoped_retrieve":
                retrievals += 1
                if retrievals > self._max_retrievals:
                    raise CourseFidelityError
                phase = "think"
            elif phase == "think" and message.name == "think_tool":
                phase = "choose"
            elif phase == "choose" and message.name == "save_research_artifacts":
                phase = "complete"
            else:
                raise CourseFidelityError

        if phase == "retrieve":
            return ("scoped_retrieve",)
        if phase == "think":
            return ("think_tool",)
        if phase == "choose" and retrievals < self._max_retrievals:
            if self._evidence_sufficient(messages):
                return ("scoped_retrieve", "save_research_artifacts")
            return ("scoped_retrieve",)
        if phase == "choose":
            return ("save_research_artifacts",)
        return ()

    @classmethod
    def _validate_response(
        cls,
        response: ModelResponse[Any],
        allowed_tools: tuple[str, ...],
        *,
        prior_queries: frozenset[str],
    ) -> ModelResponse[Any]:
        result: list[Any] = []
        for message in response.result:
            if not isinstance(message, AIMessage):
                result.append(message)
                continue
            if not allowed_tools:
                if message.tool_calls:
                    raise CourseFidelityError
                result.append(message)
                continue
            matching = [call for call in message.tool_calls if call["name"] in allowed_tools]
            if not matching:
                raise CourseFidelityError
            selected = matching[0]
            if selected["name"] == "scoped_retrieve":
                query = selected.get("args", {}).get("query")
                if not isinstance(query, str) or cls._normalize_query(query) in prior_queries:
                    raise CourseFidelityError
            result.append(message.model_copy(update={"tool_calls": [selected]}))
        return ModelResponse(
            result=result,
            structured_response=response.structured_response,
        )

    @classmethod
    def _prior_queries(cls, messages: list[Any]) -> frozenset[str]:
        queries: set[str] = set()
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                if call["name"] != "scoped_retrieve":
                    continue
                query = call.get("args", {}).get("query")
                if isinstance(query, str) and query.strip():
                    queries.add(cls._normalize_query(query))
        return frozenset(queries)

    @staticmethod
    def _normalize_query(query: str) -> str:
        return " ".join(query.casefold().split())

    @classmethod
    def _compact_researcher_messages(cls, messages: list[Any]) -> list[Any]:
        """Trim superseded reflections and duplicate evidence only in the next model input."""
        latest_think_id = next(
            (
                message.tool_call_id
                for message in reversed(messages)
                if isinstance(message, ToolMessage)
                and message.name == "think_tool"
                and message.status != "error"
            ),
            None,
        )
        seen_evidence: set[str] = set()
        compacted: list[Any] = []
        for message in messages:
            if isinstance(message, AIMessage) and message.tool_calls:
                think_calls = [call for call in message.tool_calls if call["name"] == "think_tool"]
                if len(think_calls) == len(message.tool_calls) and all(
                    call.get("id") != latest_think_id for call in think_calls
                ):
                    continue
            if isinstance(message, ToolMessage) and message.name == "think_tool":
                if message.tool_call_id != latest_think_id:
                    continue
                compacted.append(
                    message.model_copy(update={"content": "Latest coverage reflection recorded."})
                )
                continue
            if isinstance(message, ToolMessage) and message.name == "scoped_retrieve":
                compacted.append(cls._compact_retrieval_message(message, seen_evidence))
                continue
            compacted.append(message)
        return compacted

    @staticmethod
    def _compact_retrieval_message(
        message: ToolMessage,
        seen_evidence: set[str],
    ) -> ToolMessage:
        """Keep evidence text once while removing model-irrelevant retrieval bookkeeping."""
        try:
            payload = json.loads(str(message.content))
        except (json.JSONDecodeError, TypeError):
            return message
        if not isinstance(payload, dict):
            return message
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return message
        results: list[dict[str, Any]] = []
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            evidence_id = raw.get("evidence_id")
            if not isinstance(evidence_id, str):
                continue
            duplicate = evidence_id in seen_evidence
            seen_evidence.add(evidence_id)
            item = {
                key: raw[key]
                for key in (
                    "evidence_id",
                    "source_id",
                    "source_type",
                    "stakeholder_id",
                    "original_excerpt",
                )
                if key in raw and (key != "original_excerpt" or not duplicate)
            }
            if duplicate:
                item["duplicate"] = True
            results.append(item)
        metrics = payload.get("retrieval_metrics")
        compact_payload: dict[str, Any] = {
            key: payload[key] for key in ("status", "topic_id", "trust_boundary") if key in payload
        }
        if isinstance(metrics, dict) and isinstance(metrics.get("result_count"), int):
            compact_payload["result_count"] = metrics["result_count"]
        compact_payload["results"] = results
        return message.model_copy(
            update={
                "content": json.dumps(
                    compact_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            }
        )

    @classmethod
    def _evidence_sufficient(cls, messages: list[Any]) -> bool:
        observations: list[
            tuple[
                str,
                frozenset[str],
                frozenset[str],
                frozenset[str],
                tuple[str, ...],
            ]
        ] = []
        for message in messages:
            if not isinstance(message, ToolMessage) or message.name != "scoped_retrieve":
                continue
            try:
                payload = json.loads(str(message.content))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            results = payload.get("results", [])
            if not isinstance(results, list):
                continue
            source_ids = frozenset(
                item["source_id"]
                for item in results
                if isinstance(item, dict) and isinstance(item.get("source_id"), str)
            )
            source_types = frozenset(
                item["source_type"]
                for item in results
                if isinstance(item, dict) and isinstance(item.get("source_type"), str)
            )
            stakeholder_ids = frozenset(
                item["stakeholder_id"]
                for item in results
                if isinstance(item, dict) and isinstance(item.get("stakeholder_id"), str)
            )
            signature = tuple(
                sorted(
                    item["evidence_id"]
                    for item in results
                    if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
                )
            )
            observations.append(
                (
                    str(payload.get("status", "")),
                    source_ids,
                    source_types,
                    stakeholder_ids,
                    signature,
                )
            )
        if not observations:
            return False
        if observations[-1][0] == "no_authorized_sources":
            return True
        all_sources = set().union(*(item[1] for item in observations))
        all_source_types = set().union(*(item[2] for item in observations))
        all_stakeholders = set().union(*(item[3] for item in observations))
        complementary_channels = (
            len(all_sources) >= _MIN_COMPLEMENTARY_SOURCES
            and "interview" in all_source_types
            and bool(all_source_types & _DOCUMENT_SOURCE_TYPES)
        )
        independent_stakeholders = (
            len(all_sources) >= _MIN_COMPLEMENTARY_SOURCES
            and len(all_stakeholders) >= _MIN_INDEPENDENT_STAKEHOLDERS
        )
        if (
            len(all_sources) >= _MIN_DIVERSE_SOURCES
            or complementary_channels
            or independent_stakeholders
        ):
            return True
        return (
            len(observations) >= _REPETITION_WINDOW
            and bool(observations[-1][4])
            and observations[-1][4] == observations[-2][4]
        )


class CourseFidelityGuardMiddleware(AgentMiddleware[AgentState[Any], InsightRuntimeContext, Any]):
    """Enforce TODO -> plan -> researchers -> editor ordering around tool calls."""

    def __init__(
        self,
        artifacts: ScopedArtifactStore,
        *,
        max_parallel_researchers: int = 3,
        run_repository: InsightRunRepository | None = None,
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._max_parallel_researchers = max_parallel_researchers
        self._run_repository = run_repository

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        """Validate then execute a synchronous tool call."""
        try:
            self._validate(request)
        except _TodoAdvanceRequiredError:
            return self._todo_advance_error(request)
        except TodoPlanAlignmentError:
            return self._todo_plan_alignment_error(request)
        topic = self._researcher_topic(request)
        if topic is not None and self._research_artifacts_exist(request, topic):
            return self._researcher_recovered_message(request, topic)
        try:
            result = handler(request)
        except Exception as error:
            if isinstance(error, _TodoAdvanceRequiredError):
                recovery = self._todo_advance_error(request)
            elif isinstance(error, TodoPlanAlignmentError):
                recovery = self._todo_plan_alignment_error(request)
            elif (
                isinstance(
                    error,
                    (AccessDeniedError, EvidencePolicyError, RuntimeScopeError),
                )
                or topic is None
            ):
                raise
            elif self._research_artifacts_exist(request, topic):
                recovery = self._researcher_recovered_message(request, topic)
            else:
                self._write_failed_research_artifacts(request, topic)
                recovery = self._researcher_failed_message(request, topic)
            return recovery
        if topic is not None and not self._research_artifacts_exist(request, topic):
            self._write_failed_research_artifacts(request, topic)
        return result

    def wrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[[ModelRequest[InsightRuntimeContext]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        """Serialize planning phases before LangGraph schedules tool calls in parallel."""
        return self._serialize_planning_response(handler(self._restrict_model_tools(request)))

    async def awrap_model_call(
        self,
        request: ModelRequest[InsightRuntimeContext],
        handler: Callable[
            [ModelRequest[InsightRuntimeContext]],
            Awaitable[ModelResponse[Any]],
        ],
    ) -> ModelResponse[Any]:
        """Serialize async planning phases before parallel tool execution."""
        return self._serialize_planning_response(await handler(self._restrict_model_tools(request)))

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Validate then execute an asynchronous tool call."""
        try:
            await asyncio.to_thread(self._validate, request)
        except _TodoAdvanceRequiredError:
            return self._todo_advance_error(request)
        except TodoPlanAlignmentError:
            return self._todo_plan_alignment_error(request)
        # Resolving the researcher topic reloads the scoped research plan.  The
        # Deep Agents filesystem backend resolves its root during construction,
        # so keep that synchronous filesystem work off the Agent Server event
        # loop just as we do for the validation pass above.
        topic = await asyncio.to_thread(self._researcher_topic, request)
        if topic is not None and await self._aresearch_artifacts_exist(request, topic):
            return self._researcher_recovered_message(request, topic)
        try:
            result = await handler(request)
        except Exception as error:
            if isinstance(error, _TodoAdvanceRequiredError):
                recovery = self._todo_advance_error(request)
            elif isinstance(error, TodoPlanAlignmentError):
                recovery = self._todo_plan_alignment_error(request)
            elif (
                isinstance(
                    error,
                    (AccessDeniedError, EvidencePolicyError, RuntimeScopeError),
                )
                or topic is None
            ):
                raise
            elif await self._aresearch_artifacts_exist(request, topic):
                recovery = self._researcher_recovered_message(request, topic)
            else:
                await self._awrite_failed_research_artifacts(request, topic)
                recovery = self._researcher_failed_message(request, topic)
            return recovery
        if topic is not None and not await self._aresearch_artifacts_exist(request, topic):
            await self._awrite_failed_research_artifacts(request, topic)
        return result

    def _researcher_topic(self, request: ToolCallRequest) -> ResearchTopic | None:
        if request.tool_call["name"] != "task":
            return None
        arguments = request.tool_call.get("args", {})
        if arguments.get("subagent_type") != _RESEARCHER:
            return None
        description = arguments.get("description")
        if not isinstance(description, str):
            return None
        raw_context = cast("Any", request.runtime.context)
        if not isinstance(raw_context, InsightRuntimeContext):
            return None
        return self._validate_researcher_task(self._load_plan(raw_context), description)

    def _research_artifacts_exist(
        self,
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> bool:
        context = cast("InsightRuntimeContext", request.runtime.context)
        base = f"/research/{topic.topic_id}"
        if not self._artifacts.exists(
            context.access, f"{base}/findings.md"
        ) or not self._artifacts.exists(context.access, f"{base}/sources.json"):
            return False
        try:
            findings = self._artifacts.read_text(context.access, f"{base}/findings.md")
            manifest = self._artifacts.read_json(context.access, f"{base}/sources.json")
        except (ArtifactStateError, OSError, TypeError, ValueError):
            return False
        if not findings.strip() or not isinstance(manifest, dict):
            return False
        expected_identity = (
            context.access.engagement_id,
            context.run_id,
            topic.topic_id,
            stable_id("researcher", context.run_id, topic.topic_id),
        )
        actual_identity = (
            manifest.get("engagement_id"),
            manifest.get("run_id"),
            manifest.get("topic_id"),
            manifest.get("researcher_id"),
        )
        evidence_ids = manifest.get("evidence_ids")
        sources = manifest.get("sources")
        return (
            actual_identity == expected_identity
            and manifest.get("status") in {"completed", "failed", "insufficient_evidence"}
            and isinstance(evidence_ids, list)
            and all(isinstance(item, str) for item in evidence_ids)
            and isinstance(sources, list)
        )

    async def _aresearch_artifacts_exist(
        self,
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> bool:
        return await asyncio.to_thread(self._research_artifacts_exist, request, topic)

    def _write_failed_research_artifacts(
        self,
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> None:
        context = cast("InsightRuntimeContext", request.runtime.context)
        base = f"/research/{topic.topic_id}"
        self._artifacts.write_text(
            context.access,
            f"{base}/findings.md",
            (
                "# Research outcome\n\n"
                "The assigned researcher reached an explicit operational failure before it "
                "could save a supported result. No business conclusion is asserted for "
                "this topic.\n"
            ),
        )
        self._artifacts.write_json(
            context.access,
            f"{base}/sources.json",
            {
                "engagement_id": context.access.engagement_id,
                "run_id": context.run_id,
                "topic_id": topic.topic_id,
                "researcher_id": stable_id("researcher", context.run_id, topic.topic_id),
                "evidence_ids": [],
                "sources": [],
                "status": "failed",
            },
        )

    async def _awrite_failed_research_artifacts(
        self,
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> None:
        await asyncio.to_thread(self._write_failed_research_artifacts, request, topic)
        if self._run_repository is None:
            return
        context = cast("InsightRuntimeContext", request.runtime.context)
        await self._run_repository.record_activity(
            context,
            actor="topic-researcher",
            action="research_topic_failed",
            topic_id=topic.topic_id,
            artifact_name=f"research/{topic.topic_id}/findings.md",
            now=utc_now(),
        )

    @staticmethod
    def _researcher_failed_message(
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> ToolMessage:
        return ToolMessage(
            (
                f"Research topic {topic.topic_id} reached an explicit failed state. "
                "Failure artifacts were saved; continue the bounded workflow."
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
        )

    @staticmethod
    def _researcher_recovered_message(
        request: ToolCallRequest,
        topic: ResearchTopic,
    ) -> ToolMessage:
        return ToolMessage(
            (
                f"Research topic {topic.topic_id} already saved its terminal artifact boundary. "
                "The persisted result was reused after a lost response."
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
        )

    @staticmethod
    def _todo_advance_error(request: ToolCallRequest) -> ToolMessage:
        """Reject a premature task without changing TODO state or ending the graph run."""
        return ToolMessage(
            (
                "Task rejected: its exact planned topic TODO is not in_progress. "
                "Call write_todos explicitly, preserve monotonic statuses, then retry the task."
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )

    @staticmethod
    def _todo_plan_alignment_error(request: ToolCallRequest) -> ToolMessage:
        """Keep a mismatched plan recoverable while requiring an explicit TODO correction."""
        return ToolMessage(
            (
                "Plan rejected: every proposed topic title must appear exactly in the TODO "
                "content, together with explicit editing and validation TODOs. Call write_todos "
                "to correct the list, then retry create_research_plan without changing scope."
            ),
            tool_call_id=request.tool_call["id"],
            name=request.tool_call["name"],
            status="error",
        )

    @classmethod
    def _restrict_model_tools(
        cls,
        request: ModelRequest[InsightRuntimeContext],
    ) -> ModelRequest[InsightRuntimeContext]:
        """Expose only the root tools allowed in the current orchestration phase."""
        state = cast("dict[str, Any]", request.state)
        if not state.get("todos"):
            allowed = {"write_todos"}
        elif not state.get("research_plan"):
            allowed = {"write_todos", "create_research_plan"}
        elif cls._task_requires_todo_refresh(request.messages):
            allowed = {"write_todos"}
        elif cls._editor_completed(request.messages):
            if cls._all_todos_completed(state.get("todos")):
                return request.override(
                    messages=cls._compact_orchestrator_messages(request.messages),
                    tools=[],
                )
            allowed = {"write_todos"}
        elif not cls._has_dispatchable_todo(state):
            allowed = {"write_todos"}
        else:
            allowed = {"write_todos", "task"}
        tools = [tool for tool in request.tools if _tool_name(tool) in allowed]
        if not tools:
            raise CourseFidelityError
        return request.override(
            messages=cls._compact_orchestrator_messages(request.messages),
            tools=tools,
        )

    @staticmethod
    def _compact_orchestrator_messages(messages: list[Any]) -> list[Any]:
        """Drop superseded TODO snapshots from model input while preserving graph state."""
        latest_todo_result = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, ToolMessage) and message.name == "write_todos"
            ),
            None,
        )
        if latest_todo_result is None:
            return list(messages)
        kept_todo_ids = {latest_todo_result.tool_call_id}
        if latest_todo_result.status == "error":
            latest_success_id = next(
                (
                    message.tool_call_id
                    for message in reversed(messages)
                    if isinstance(message, ToolMessage)
                    and message.name == "write_todos"
                    and message.status != "error"
                ),
                None,
            )
            if latest_success_id is not None:
                kept_todo_ids.add(latest_success_id)
        compacted: list[Any] = []
        for message in messages:
            if isinstance(message, AIMessage) and message.tool_calls:
                todo_calls = [call for call in message.tool_calls if call["name"] == "write_todos"]
                if len(todo_calls) == len(message.tool_calls) and all(
                    call.get("id") not in kept_todo_ids for call in todo_calls
                ):
                    continue
            if isinstance(message, ToolMessage) and message.name == "write_todos":
                if message.tool_call_id not in kept_todo_ids:
                    continue
                output_message = message
                if message.status != "error":
                    output_message = message.model_copy(
                        update={"content": "Current TODO state saved."}
                    )
                compacted.append(output_message)
                continue
            compacted.append(message)
        return compacted

    @staticmethod
    def _tool_result_positions(messages: list[Any], tool_name: str) -> tuple[int, ...]:
        return tuple(
            index
            for index, message in enumerate(messages)
            if isinstance(message, ToolMessage)
            and message.name == tool_name
            and message.status != "error"
        )

    @classmethod
    def _task_requires_todo_refresh(cls, messages: list[Any]) -> bool:
        task_positions = cls._tool_result_positions(messages, "task")
        if not task_positions:
            return False
        todo_positions = cls._tool_result_positions(messages, "write_todos")
        return not todo_positions or task_positions[-1] > todo_positions[-1]

    @classmethod
    def _editor_completed(cls, messages: list[Any]) -> bool:
        completed_call_ids = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolMessage)
            and message.name == "task"
            and message.status != "error"
        }
        return any(
            call.get("id") in completed_call_ids
            and call.get("args", {}).get("subagent_type") == _EDITOR
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if call.get("name") == "task"
        )

    @staticmethod
    def _all_todos_completed(raw_todos: object) -> bool:
        return (
            isinstance(raw_todos, list)
            and bool(raw_todos)
            and all(
                isinstance(item, dict) and item.get("status") == "completed" for item in raw_todos
            )
        )

    @classmethod
    def _has_dispatchable_todo(cls, state: dict[str, Any]) -> bool:
        """Expose task only after an explicit TODO advance matches the saved plan."""
        raw_todos = state.get("todos")
        raw_plan = state.get("research_plan")
        if not isinstance(raw_todos, list):
            return False
        try:
            plan = ResearchPlan.model_validate(raw_plan)
        except (TypeError, ValueError):
            return False
        if any(
            cls._todo_has_status(raw_todos, topic.title, "in_progress") for topic in plan.topics
        ):
            return True
        return all(
            cls._todo_has_status(raw_todos, topic.title, "completed") for topic in plan.topics
        ) and cls._todo_has_any_fragment_status(
            raw_todos,
            _EDITING_TODO_FRAGMENTS,
            "in_progress",
        )

    @classmethod
    def _serialize_planning_response(cls, response: ModelResponse[Any]) -> ModelResponse[Any]:
        result = [cls._serialize_message(message) for message in response.result]
        return ModelResponse(
            result=result,
            structured_response=response.structured_response,
        )

    @staticmethod
    def _serialize_message(message: Any) -> Any:
        if not isinstance(message, AIMessage) or len(message.tool_calls) <= 1:
            return message
        calls = message.tool_calls
        todo_calls = [call for call in calls if call["name"] == "write_todos"]
        if todo_calls:
            return message.model_copy(update={"tool_calls": todo_calls[:1]})
        plan_calls = [call for call in calls if call["name"] == "create_research_plan"]
        if plan_calls:
            return message.model_copy(update={"tool_calls": plan_calls[:1]})
        researcher_calls = [
            call
            for call in calls
            if call["name"] == "task" and call.get("args", {}).get("subagent_type") == _RESEARCHER
        ]
        editor_calls = [
            call
            for call in calls
            if call["name"] == "task" and call.get("args", {}).get("subagent_type") == _EDITOR
        ]
        if researcher_calls and editor_calls:
            return message.model_copy(
                update={"tool_calls": [call for call in calls if call not in editor_calls]}
            )
        return message

    def _validate(self, request: ToolCallRequest) -> None:
        self._reject_repeated_failed_call(request)
        name = request.tool_call["name"]
        state = cast("dict[str, Any]", request.state)
        raw_context = cast("Any", request.runtime.context)
        if not isinstance(raw_context, InsightRuntimeContext):
            raise CourseFidelityError
        context = raw_context
        if name == "write_todos":
            self._validate_todo_update(request, context)
            return
        if name == "create_research_plan":
            if not state.get("todos"):
                raise CourseFidelityError
            return
        if name != "task":
            return

        plan = self._load_plan(context)
        arguments = request.tool_call.get("args", {})
        subagent_type = arguments.get("subagent_type")
        description = arguments.get("description")
        if not isinstance(description, str):
            raise CourseFidelityError
        if subagent_type == _RESEARCHER:
            self._validate_researcher_dispatch(context, state, plan, description)
            return
        if subagent_type == _EDITOR:
            self._validate_editor_dispatch(context, state, plan)
            return
        raise CourseFidelityError

    @classmethod
    def _reject_repeated_failed_call(cls, request: ToolCallRequest) -> None:
        """Stop before a third identical root action repeats the same recoverable failure."""
        state = cast("dict[str, Any]", request.state)
        messages = state.get("messages", [])
        if not isinstance(messages, list):
            return
        current_signature = (
            cls._tool_call_signature(request.tool_call),
            cls._stable_json(state.get("todos")),
        )
        failed_call_ids = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolMessage)
            and message.status == "error"
            and isinstance(message.tool_call_id, str)
        }
        pending_todos: dict[str, Any] = {}
        active_todos: Any = None
        prior_signatures: dict[str, tuple[tuple[str, str], str]] = {}
        for message in messages:
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    call_id = call.get("id")
                    if not isinstance(call_id, str):
                        continue
                    prior_signatures[call_id] = (
                        cls._tool_call_signature(call),
                        cls._stable_json(active_todos),
                    )
                    if call.get("name") == "write_todos":
                        pending_todos[call_id] = call.get("args", {}).get("todos")
            elif (
                isinstance(message, ToolMessage)
                and message.name == "write_todos"
                and message.status != "error"
                and isinstance(message.tool_call_id, str)
                and message.tool_call_id in pending_todos
            ):
                active_todos = pending_todos[message.tool_call_id]
        previous_failures = sum(
            1 for call_id in failed_call_ids if prior_signatures.get(call_id) == current_signature
        )
        if previous_failures >= _IDENTICAL_FAILURE_LIMIT:
            raise RepeatedToolFailureError

    @staticmethod
    def _tool_call_signature(call: ToolCall) -> tuple[str, str]:
        return str(call.get("name", "")), CourseFidelityGuardMiddleware._stable_json(
            call.get("args", {})
        )

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )

    def _validate_researcher_dispatch(
        self,
        context: InsightRuntimeContext,
        state: dict[str, Any],
        plan: ResearchPlan,
        description: str,
    ) -> None:
        topic = self._validate_researcher_task(plan, description)
        if not self._has_todo_status(state, topic.title, "in_progress"):
            raise _TodoAdvanceRequiredError
        for dependency in topic.dependencies:
            base = f"/research/{dependency}"
            if not self._artifacts.exists(context.access, f"{base}/findings.md"):
                raise CourseFidelityError
            if not self._artifacts.exists(context.access, f"{base}/sources.json"):
                raise CourseFidelityError
        self._validate_parallel_wave(state)

    def _validate_editor_dispatch(
        self,
        context: InsightRuntimeContext,
        state: dict[str, Any],
        plan: ResearchPlan,
    ) -> None:
        for topic in plan.topics:
            self._require_todo_status(state, topic.title, "completed")
        if not self._has_todo_any_status(
            state,
            _EDITING_TODO_FRAGMENTS,
            "in_progress",
        ):
            raise _TodoAdvanceRequiredError
        self._validate_editor_task(context, plan)

    def _validate_todo_update(
        self,
        request: ToolCallRequest,
        context: InsightRuntimeContext,
    ) -> None:
        proposed = self._validated_todo_items(request)
        state = cast("dict[str, Any]", request.state)
        current = state.get("todos")
        if not state.get("research_plan"):
            return
        plan = self._load_plan(context)
        if not isinstance(current, list) or not current:
            raise CourseFidelityError
        self._validate_monotonic_todos(current, proposed)

        completed = self._completed_task_calls(request.state.get("messages", []))
        self._validate_completed_work_todos(plan, proposed, completed)

    @staticmethod
    def _validated_todo_items(request: ToolCallRequest) -> list[Any]:
        proposed = request.tool_call.get("args", {}).get("todos")
        if not isinstance(proposed, list) or not proposed:
            raise CourseFidelityError
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("content"), str)
            or item.get("status") not in _TODO_STATUS_ORDER
            for item in proposed
        ):
            raise CourseFidelityError
        return proposed

    @staticmethod
    def _validate_monotonic_todos(current: list[Any], proposed: list[Any]) -> None:
        current_by_content = {
            str(item.get("content")): str(item.get("status"))
            for item in current
            if isinstance(item, dict)
        }
        proposed_by_content = {
            str(item["content"]): str(item["status"]) for item in proposed if isinstance(item, dict)
        }
        if current_by_content.keys() != proposed_by_content.keys():
            raise CourseFidelityError
        advances = 0
        for content, old_status in current_by_content.items():
            new_status = proposed_by_content[content]
            if _TODO_STATUS_ORDER[new_status] < _TODO_STATUS_ORDER[old_status]:
                raise CourseFidelityError
            advances += int(new_status != old_status)
        if advances == 0:
            raise CourseFidelityError

    @classmethod
    def _validate_completed_work_todos(
        cls,
        plan: ResearchPlan,
        proposed: list[Any],
        completed: tuple[ToolCall, ...],
    ) -> None:
        completed_research_titles = {
            topic.title
            for topic in plan.topics
            if any(
                call.get("args", {}).get("subagent_type") == _RESEARCHER
                and f"topic_id={topic.topic_id}" in str(call.get("args", {}).get("description", ""))
                for call in completed
            )
        }
        for topic in plan.topics:
            marked_complete = cls._todo_has_status(proposed, topic.title, "completed")
            if marked_complete != (topic.title in completed_research_titles):
                raise CourseFidelityError
        editor_completed = any(
            call.get("args", {}).get("subagent_type") == _EDITOR for call in completed
        )
        if editor_completed:
            if not cls._all_todos_completed(proposed):
                raise CourseFidelityError
        elif (
            cls._todo_has_any_fragment_status(
                proposed,
                _EDITING_TODO_FRAGMENTS,
                "completed",
            )
            or cls._todo_has_fragment_status(
                proposed,
                "validat",
                "completed",
            )
            or (
                len(completed_research_titles) == len(plan.topics)
                and not cls._todo_has_any_fragment_status(
                    proposed,
                    _EDITING_TODO_FRAGMENTS,
                    "in_progress",
                )
            )
        ):
            raise CourseFidelityError

    @classmethod
    def _completed_task_calls(cls, raw_messages: object) -> tuple[ToolCall, ...]:
        if not isinstance(raw_messages, list):
            return ()
        completed_ids = {
            message.tool_call_id
            for message in raw_messages
            if isinstance(message, ToolMessage)
            and message.name == "task"
            and message.status != "error"
        }
        return tuple(
            call
            for message in raw_messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
            if call.get("name") == "task" and call.get("id") in completed_ids
        )

    @staticmethod
    def _todo_has_status(todos: list[Any], content: str, status: str) -> bool:
        return any(
            isinstance(item, dict)
            and content.casefold() in str(item.get("content", "")).casefold()
            and item.get("status") == status
            for item in todos
        )

    @staticmethod
    def _todo_has_fragment_status(todos: list[Any], fragment: str, status: str) -> bool:
        return any(
            isinstance(item, dict)
            and fragment.casefold() in str(item.get("content", "")).casefold()
            and item.get("status") == status
            for item in todos
        )

    @classmethod
    def _todo_has_any_fragment_status(
        cls,
        todos: list[Any],
        fragments: tuple[str, ...],
        status: str,
    ) -> bool:
        return any(cls._todo_has_fragment_status(todos, fragment, status) for fragment in fragments)

    @classmethod
    def _require_todo_status(
        cls,
        state: dict[str, Any],
        content: str,
        status: str,
    ) -> None:
        todos = state.get("todos")
        if not isinstance(todos, list) or not cls._todo_has_status(todos, content, status):
            raise CourseFidelityError

    @classmethod
    def _has_todo_status(cls, state: dict[str, Any], fragment: str, status: str) -> bool:
        todos = state.get("todos")
        return isinstance(todos, list) and cls._todo_has_fragment_status(
            todos,
            fragment,
            status,
        )

    @classmethod
    def _has_todo_any_status(
        cls,
        state: dict[str, Any],
        fragments: tuple[str, ...],
        status: str,
    ) -> bool:
        todos = state.get("todos")
        return isinstance(todos, list) and cls._todo_has_any_fragment_status(
            todos,
            fragments,
            status,
        )

    def _load_plan(self, context: InsightRuntimeContext) -> ResearchPlan:
        payload = self._artifacts.read_json(context.access, "/research_plan.json")
        plan = ResearchPlan.model_validate(payload)
        state_scope = (context.run_id, context.access.engagement_id, context.question)
        if (plan.run_id, plan.engagement_id, plan.question) != state_scope:
            raise CourseFidelityError
        return plan

    @staticmethod
    def _validate_researcher_task(plan: ResearchPlan, description: str) -> ResearchTopic:
        matching = [topic for topic in plan.topics if f"topic_id={topic.topic_id}" in description]
        if len(matching) != 1:
            raise CourseFidelityError
        if description.count("topic_id=") != 1:
            raise CourseFidelityError
        return matching[0]

    def _validate_parallel_wave(self, state: dict[str, Any]) -> None:
        validate_researcher_wave(
            state.get("messages", []),
            maximum=self._max_parallel_researchers,
        )

    def _validate_editor_task(
        self,
        context: InsightRuntimeContext,
        plan: ResearchPlan,
    ) -> None:
        for topic in plan.topics:
            base = f"/research/{topic.topic_id}"
            if not self._artifacts.exists(context.access, f"{base}/findings.md"):
                raise CourseFidelityError
            if not self._artifacts.exists(context.access, f"{base}/sources.json"):
                raise CourseFidelityError
