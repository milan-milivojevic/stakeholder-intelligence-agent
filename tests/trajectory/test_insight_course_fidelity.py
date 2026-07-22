"""Execute the required Deep Agent planning, research, and editor trajectory offline."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from blockbuster import blockbuster_ctx
from langchain.agents.middleware.types import (
    AgentState,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain.tools import ToolRuntime
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    ToolCall,
    ToolMessage,
)

from stakeholder_intelligence_agent.artifacts import ScopedArtifactStore
from stakeholder_intelligence_agent.contracts import InsightReport, ResearchPlan, ResearchTopic
from stakeholder_intelligence_agent.errors import (
    ArtifactStateError,
    CourseFidelityError,
    EvidencePolicyError,
    RepeatedToolFailureError,
)
from stakeholder_intelligence_agent.insight.editor import EditorReportDraft
from stakeholder_intelligence_agent.insight.graph import (
    InsightGraphDependencies,
    build_insight_graph,
)
from stakeholder_intelligence_agent.insight.tools import (
    build_editor_tools,
    build_orchestrator_tools,
    build_researcher_tools,
)
from stakeholder_intelligence_agent.middleware import (
    CourseFidelityGuardMiddleware,
    OrderedSubagentToolMiddleware,
    ResearcherLoopMiddleware,
)
from stakeholder_intelligence_agent.persistence.checkpointer import (
    open_sqlite_checkpointer,
)
from tests.fakes import ToolCallingFakeModel
from tests.helpers import insight_context

if TYPE_CHECKING:
    from pathlib import Path

    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import BaseTool

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.access import InsightRuntimeContext


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> AIMessage:
    call: ToolCall = {
        "name": name,
        "args": arguments,
        "id": call_id,
        "type": "tool_call",
    }
    return AIMessage(content="", tool_calls=[call])


def _combined_tool_calls(*messages: AIMessage) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[call for message in messages for call in message.tool_calls],
    )


@pytest.mark.trajectory
async def test_artifact_tools_reuse_identical_writes_after_lost_responses(
    settings: Settings,
    tmp_path: Path,
) -> None:
    context = insight_context(
        run_id="run-response-loss",
        thread_id="thread-response-loss",
        question="What is supported after a bounded response loss?",
    )
    artifacts = ScopedArtifactStore(tmp_path / "artifacts")
    topic = ResearchTopic(
        topic_id="topic-response-loss",
        title="Response Loss",
        objective="Determine whether current authorized evidence supports a conclusion.",
        questions=("Which conclusion is supported?",),
        required_source_types=("document",),
        dependencies=(),
        priority=1,
    )
    todos = [
        {"content": "Research Response Loss", "status": "in_progress"},
        {"content": "Edit the structured report", "status": "pending"},
        {"content": "Validate the report", "status": "pending"},
    ]
    plan_runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={"todos": todos},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="plan-response-loss",
        store=None,
        tools=[],
    )
    plan_tool = build_orchestrator_tools(artifacts, max_topics=5)[0]
    plan_call = cast("Any", plan_tool).coroutine
    first_plan = await plan_call(
        topics=[topic],
        source_strategy=["document"],
        completion_criteria=["Record an explicit evidence gap when evidence is absent."],
        runtime=plan_runtime,
    )
    saved_created_at = artifacts.read_json(context.access, "/research_plan.json")["created_at"]
    second_plan = await plan_call(
        topics=[topic],
        source_strategy=["document"],
        completion_criteria=["Record an explicit evidence gap when evidence is absent."],
        runtime=plan_runtime,
    )

    assigned_message = HumanMessage(
        content="topic_id=topic-response-loss Research only this topic."
    )
    researcher_runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={"messages": [assigned_message]},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="research-response-loss",
        store=None,
        tools=[],
    )
    save_research = next(
        tool for tool in build_researcher_tools(artifacts) if tool.name == "save_research_artifacts"
    )
    research_call = cast("Any", save_research).coroutine
    first_research = await research_call(
        topic_id=topic.topic_id,
        findings_markdown="# Findings\n\nNo authorized evidence was available.",
        evidence_ids=[],
        runtime=researcher_runtime,
    )
    second_research = await research_call(
        topic_id=topic.topic_id,
        findings_markdown="# Findings\n\nNo authorized evidence was available.",
        evidence_ids=[],
        runtime=researcher_runtime,
    )
    with pytest.raises(ArtifactStateError):
        await research_call(
            topic_id=topic.topic_id,
            findings_markdown=(
                "# Findings\n\nA changed replay must not replace the persisted result."
            ),
            evidence_ids=[],
            runtime=researcher_runtime,
        )

    editor_runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="editor-response-loss",
        store=None,
        tools=[],
    )
    draft = EditorReportDraft.model_validate(
        {
            "status": "insufficient_evidence",
            "executive_summary": "No authorized evidence supports a business conclusion.",
            "researched_topics": [
                {
                    "topic_id": topic.topic_id,
                    "status": "insufficient_evidence",
                    "summary": "No authorized evidence was available.",
                }
            ],
            "findings": [],
            "responsibilities": [],
            "operational_risks": [],
            "buy_in_signals": [],
            "contradictions": [],
            "evidence_gaps": [
                {
                    "topic": topic.title,
                    "description": "No authorized evidence was available.",
                    "impact": "The planned question remains unsupported.",
                }
            ],
            "open_questions": ["Which authorized source could answer the question?"],
            "follow_up_recommendations": [],
        }
    )
    save_report = next(
        tool
        for tool in build_editor_tools(artifacts, settings=settings)
        if tool.name == "save_final_report"
    )
    report_call = cast("Any", save_report).coroutine
    first_report = await report_call(report=draft, runtime=editor_runtime)
    report_payload = artifacts.read_json(context.access, "/report/insight_report.json")
    second_report = await report_call(report=draft, runtime=editor_runtime)

    assert first_plan.update["research_plan"]["created_at"] == saved_created_at
    assert second_plan.update["research_plan"]["created_at"] == saved_created_at
    assert first_research == "Research artifacts saved for topic topic-response-loss."
    assert second_research == "Research artifacts already exist for topic topic-response-loss."
    assert first_report == "The strict InsightReport was validated and saved."
    assert second_report == "The existing strict InsightReport was validated and reused."
    assert report_payload["status"] == "insufficient_evidence"


@pytest.mark.trajectory
def test_planning_middleware_serializes_tool_batches_before_parallel_execution(
    tmp_path: Path,
) -> None:
    guard = CourseFidelityGuardMiddleware(ScopedArtifactStore(tmp_path / "artifacts"))
    tool_schemas: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Test schema for {name}.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("write_todos", "create_research_plan", "task", "ls", "grep")
    ]
    request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=[],
            tools=tool_schemas,
            state={"messages": []},
        ),
    )
    forwarded_tools: list[list[str]] = []
    response = ModelResponse(
        result=[
            _combined_tool_calls(
                _tool_call("write_todos", {"todos": []}, "todos"),
                _tool_call("create_research_plan", {"topics": []}, "plan"),
            )
        ]
    )

    def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
        forwarded_tools.append(
            [str(tool["function"]["name"]) for tool in received.tools if isinstance(tool, dict)]
        )
        return response

    def capture_and_return(
        received: ModelRequest[InsightRuntimeContext],
        model_response: ModelResponse[Any],
    ) -> ModelResponse[Any]:
        forwarded_tools.append(
            [str(tool["function"]["name"]) for tool in received.tools if isinstance(tool, dict)]
        )
        return model_response

    serialized = guard.wrap_model_call(request, handler)
    message = cast("AIMessage", serialized.result[0])
    assert [call["name"] for call in message.tool_calls] == ["write_todos"]
    assert forwarded_tools.pop() == ["write_todos"]

    plan_response = ModelResponse(
        result=[
            _combined_tool_calls(
                _tool_call("create_research_plan", {"topics": []}, "plan"),
                _tool_call(
                    "task",
                    {"description": "topic_id=a", "subagent_type": "topic-researcher"},
                    "research",
                ),
            )
        ]
    )
    request = request.override(
        state=cast(
            "AgentState[Any]",
            {"messages": [], "todos": [{"content": "Topic"}]},
        )
    )
    serialized = guard.wrap_model_call(
        request,
        handler=lambda received: capture_and_return(received, plan_response),
    )
    message = cast("AIMessage", serialized.result[0])
    assert [call["name"] for call in message.tool_calls] == ["create_research_plan"]
    assert forwarded_tools.pop() == ["write_todos", "create_research_plan"]

    researcher_editor_response = ModelResponse(
        result=[
            _combined_tool_calls(
                _tool_call(
                    "task",
                    {"description": "topic_id=a", "subagent_type": "topic-researcher"},
                    "research",
                ),
                _tool_call(
                    "task",
                    {"description": "edit", "subagent_type": "report-editor"},
                    "editor",
                ),
            )
        ]
    )
    request = request.override(
        state=cast(
            "AgentState[Any]",
            {
                "messages": [],
                "todos": [{"content": "Topic", "status": "in_progress"}],
                "research_plan": {
                    "plan_id": "plan-a",
                    "run_id": "run-a",
                    "engagement_id": "engagement-a",
                    "question": "What are the stakeholder risks?",
                    "topics": [
                        {
                            "topic_id": "a",
                            "title": "Topic",
                            "objective": "Research the topic.",
                            "questions": ["What evidence exists?"],
                            "required_source_types": ["document"],
                            "dependencies": [],
                            "priority": 1,
                        }
                    ],
                    "source_strategy": ["document"],
                    "completion_criteria": ["Use authorized evidence."],
                    "created_at": "2026-07-21T00:00:00Z",
                },
            },
        )
    )
    serialized = guard.wrap_model_call(
        request,
        handler=lambda received: capture_and_return(received, researcher_editor_response),
    )
    message = cast("AIMessage", serialized.result[0])
    assert [call["args"]["subagent_type"] for call in message.tool_calls] == ["topic-researcher"]
    assert forwarded_tools.pop() == ["write_todos", "task"]

    request = request.override(
        state=cast(
            "AgentState[Any]",
            {
                **cast("dict[str, Any]", request.state),
                "todos": [{"content": "Topic", "status": "pending"}],
            },
        )
    )
    guard.wrap_model_call(
        request,
        handler=lambda received: capture_and_return(received, researcher_editor_response),
    )
    assert forwarded_tools.pop() == ["write_todos"]


@pytest.mark.trajectory
def test_orchestrator_model_input_keeps_only_latest_todo_snapshot(tmp_path: Path) -> None:
    guard = CourseFidelityGuardMiddleware(ScopedArtifactStore(tmp_path / "artifacts"))
    first = _tool_call(
        "write_todos",
        {"todos": [{"content": "Old topic", "status": "in_progress"}]},
        "todo-old",
    )
    latest = _tool_call(
        "write_todos",
        {"todos": [{"content": "Current topic", "status": "in_progress"}]},
        "todo-current",
    )
    messages: list[AnyMessage] = [
        HumanMessage(content="What is the current risk?"),
        first,
        ToolMessage(
            "Updated todo list with an obsolete verbose snapshot.",
            tool_call_id="todo-old",
            name="write_todos",
        ),
        latest,
        ToolMessage(
            "Updated todo list with the full current verbose snapshot.",
            tool_call_id="todo-current",
            name="write_todos",
        ),
    ]
    tool_schemas: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("write_todos", "create_research_plan")
    ]
    request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=messages,
            tools=tool_schemas,
            state=cast(
                "AgentState[Any]",
                {
                    "messages": messages,
                    "todos": [{"content": "Current topic", "status": "in_progress"}],
                },
            ),
        ),
    )
    received_messages: list[Any] = []

    def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
        received_messages.extend(received.messages)
        return ModelResponse(result=[_tool_call("create_research_plan", {"topics": []}, "plan")])

    guard.wrap_model_call(request, handler)

    visible_tool_ids = {
        call["id"]
        for message in received_messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    visible_todo_results = [
        message
        for message in received_messages
        if isinstance(message, ToolMessage) and message.name == "write_todos"
    ]
    assert "todo-old" not in visible_tool_ids
    assert "todo-current" in visible_tool_ids
    assert len(visible_todo_results) == 1
    assert visible_todo_results[0].content == "Current TODO state saved."
    assert len(cast("dict[str, Any]", request.state)["messages"]) == len(messages)


@pytest.mark.trajectory
def test_subagent_middleware_exposes_only_role_tools_and_accepts_next_phase() -> None:
    guard = OrderedSubagentToolMiddleware(
        ("think_tool", "scoped_retrieve", "save_research_artifacts")
    )
    tool_schemas: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Test schema for {name}.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in (
            "write_todos",
            "ls",
            "grep",
            "read_file",
            "think_tool",
            "scoped_retrieve",
            "save_research_artifacts",
        )
    ]
    request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=[],
            tools=tool_schemas,
            state={"messages": []},
        ),
    )
    forwarded_tools: list[list[str]] = []

    def invoke(
        current: ModelRequest[InsightRuntimeContext],
        response: ModelResponse[Any],
    ) -> ModelResponse[Any]:
        def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
            forwarded_tools.append(
                [str(tool["function"]["name"]) for tool in received.tools if isinstance(tool, dict)]
            )
            return response

        return guard.wrap_model_call(current, handler)

    first = invoke(
        request,
        ModelResponse(
            result=[
                _combined_tool_calls(
                    _tool_call("think_tool", {"reflection": "bounded"}, "think-1"),
                    _tool_call("think_tool", {"reflection": "duplicate"}, "think-2"),
                    _tool_call("ls", {"path": "/"}, "generic"),
                )
            ]
        ),
    )
    first_message = cast("AIMessage", first.result[0])
    assert [call["name"] for call in first_message.tool_calls] == ["think_tool"]

    think_result = ToolMessage(
        "Private pause completed.",
        tool_call_id="think-1",
        name="think_tool",
    )
    request = request.override(
        messages=[think_result],
        state=cast("AgentState[Any]", {"messages": [think_result]}),
    )
    invoke(
        request,
        ModelResponse(
            result=[
                _tool_call(
                    "scoped_retrieve",
                    {"topic_id": "topic-a", "query": "bounded query"},
                    "retrieve",
                )
            ]
        ),
    )

    retrieve_result = ToolMessage(
        '{"status":"no_authorized_sources"}',
        tool_call_id="retrieve",
        name="scoped_retrieve",
    )
    request = request.override(
        messages=[think_result, retrieve_result],
        state=cast(
            "AgentState[Any]",
            {"messages": [think_result, retrieve_result]},
        ),
    )
    invoke(
        request,
        ModelResponse(
            result=[
                _tool_call(
                    "save_research_artifacts",
                    {
                        "topic_id": "topic-a",
                        "findings_markdown": "# Findings\n\nNo evidence.",
                        "evidence_ids": [],
                    },
                    "save",
                )
            ]
        ),
    )

    save_result = ToolMessage(
        "Research artifacts saved.",
        tool_call_id="save",
        name="save_research_artifacts",
    )
    request = request.override(
        messages=[think_result, retrieve_result, save_result],
        state=cast(
            "AgentState[Any]",
            {"messages": [think_result, retrieve_result, save_result]},
        ),
    )
    final = invoke(
        request,
        ModelResponse(result=[AIMessage(content="The bounded topic is complete.")]),
    )

    assert cast("AIMessage", final.result[0]).tool_calls == []
    role_tools = ["think_tool", "scoped_retrieve", "save_research_artifacts"]
    assert forwarded_tools == [role_tools, role_tools, role_tools, []]

    out_of_order = ToolMessage(
        "Unexpected retrieval.",
        tool_call_id="bad",
        name="scoped_retrieve",
    )
    with pytest.raises(CourseFidelityError):
        invoke(
            request.override(
                messages=[out_of_order],
                state=cast("AgentState[Any]", {"messages": [out_of_order]}),
            ),
            ModelResponse(result=[AIMessage(content="invalid")]),
        )


@pytest.mark.trajectory
async def test_editor_evidence_rejection_allows_only_bounded_corrections() -> None:
    guard = OrderedSubagentToolMiddleware(("load_research_package", "save_final_report"))
    context = insight_context()
    runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="save-1",
        store=None,
        tools=[],
    )

    async def rejected(_request: ToolCallRequest) -> ToolMessage:
        raise EvidencePolicyError

    messages: list[AnyMessage] = []
    for attempt in range(1, 3):
        call = _tool_call(
            "save_final_report",
            {"report": {"attempt": attempt}},
            f"save-{attempt}",
        )
        messages.append(call)
        request = ToolCallRequest(
            tool_call=call.tool_calls[0],
            tool=None,
            state={"messages": list(messages)},
            runtime=cast("Any", runtime),
        )
        result = await guard.awrap_tool_call(request, rejected)
        assert isinstance(result, ToolMessage)
        assert result.status == "error"
        assert "deterministic evidence validation" in str(result.content)
        messages.append(result)

    final_call = _tool_call(
        "save_final_report",
        {"report": {"attempt": 3}},
        "save-3",
    )
    messages.append(final_call)
    final_request = ToolCallRequest(
        tool_call=final_call.tool_calls[0],
        tool=None,
        state={"messages": messages},
        runtime=cast("Any", runtime),
    )
    with pytest.raises(EvidencePolicyError):
        await guard.awrap_tool_call(final_request, rejected)


@pytest.mark.trajectory
def test_researcher_loop_supports_query_refinement_and_early_stop() -> None:
    role_names = ("scoped_retrieve", "think_tool", "save_research_artifacts")
    tool_schemas: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Test schema for {name}.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in (*role_names, "read_file")
    ]
    base_request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=[],
            tools=tool_schemas,
            state={"messages": []},
        ),
    )

    def invoke(
        guard: ResearcherLoopMiddleware,
        messages: list[AnyMessage],
        response: AIMessage,
    ) -> tuple[ModelResponse[Any], list[str]]:
        forwarded: list[str] = []
        request = base_request.override(
            messages=messages,
            state=cast("AgentState[Any]", {"messages": messages}),
        )

        def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
            forwarded.extend(
                str(tool["function"]["name"]) for tool in received.tools if isinstance(tool, dict)
            )
            return ModelResponse(result=[response])

        return guard.wrap_model_call(request, handler), forwarded

    retrieve_one = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-1","source_id":"source-1"}]}',
        tool_call_id="retrieve-1",
        name="scoped_retrieve",
    )
    think_one = ToolMessage("reviewed", tool_call_id="think-1", name="think_tool")
    retrieve_two = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-2","source_id":"source-2"}]}',
        tool_call_id="retrieve-2",
        name="scoped_retrieve",
    )
    think_two = ToolMessage("reviewed", tool_call_id="think-2", name="think_tool")

    refinement_guard = ResearcherLoopMiddleware(max_retrievals=2)
    _, initial_tools = invoke(
        refinement_guard,
        [],
        _tool_call("scoped_retrieve", {"query": "initial"}, "retrieve-1"),
    )
    _, after_retrieve_tools = invoke(
        refinement_guard,
        [retrieve_one],
        _tool_call("think_tool", {"reflection": "gap found"}, "think-1"),
    )
    refined, after_think_tools = invoke(
        refinement_guard,
        [retrieve_one, think_one],
        _tool_call("scoped_retrieve", {"query": "refined"}, "retrieve-2"),
    )
    _, after_refined_retrieve_tools = invoke(
        refinement_guard,
        [retrieve_one, think_one, retrieve_two],
        _tool_call("think_tool", {"reflection": "sufficient"}, "think-2"),
    )
    saved, exhausted_tools = invoke(
        refinement_guard,
        [retrieve_one, think_one, retrieve_two, think_two],
        _tool_call("save_research_artifacts", {}, "save"),
    )

    assert initial_tools == ["scoped_retrieve"]
    assert after_retrieve_tools == ["think_tool"]
    assert after_think_tools == ["scoped_retrieve"]
    assert after_refined_retrieve_tools == ["think_tool"]
    assert exhausted_tools == ["save_research_artifacts"]
    assert cast("AIMessage", refined.result[0]).tool_calls[0]["args"]["query"] == "refined"
    assert cast("AIMessage", saved.result[0]).tool_calls[0]["name"] == "save_research_artifacts"

    no_sources = ToolMessage(
        '{"status":"no_authorized_sources","results":[]}',
        tool_call_id="retrieve-empty",
        name="scoped_retrieve",
    )
    early_stop_guard = ResearcherLoopMiddleware(max_retrievals=3)
    early_stop, available_after_one = invoke(
        early_stop_guard,
        [no_sources, think_one],
        _tool_call("save_research_artifacts", {}, "save-early"),
    )
    assert available_after_one == ["scoped_retrieve", "save_research_artifacts"]
    assert cast("AIMessage", early_stop.result[0]).tool_calls[0]["name"] == (
        "save_research_artifacts"
    )

    diverse_guard = ResearcherLoopMiddleware(max_retrievals=4)
    _, two_source_tools = invoke(
        diverse_guard,
        [retrieve_one, think_one, retrieve_two, think_two],
        _tool_call("scoped_retrieve", {"query": "third angle"}, "retrieve-3"),
    )
    assert two_source_tools == ["scoped_retrieve"]
    retrieve_three = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-3","source_id":"source-3"}]}',
        tool_call_id="retrieve-3",
        name="scoped_retrieve",
    )
    _, after_third_retrieve = invoke(
        diverse_guard,
        [retrieve_one, think_one, retrieve_two, think_two, retrieve_three],
        _tool_call("think_tool", {"reflection": "three sources"}, "think-3"),
    )
    _, three_source_tools = invoke(
        diverse_guard,
        [
            retrieve_one,
            think_one,
            retrieve_two,
            think_two,
            retrieve_three,
            ToolMessage("reviewed", tool_call_id="think-3", name="think_tool"),
        ],
        _tool_call("save_research_artifacts", {}, "save-three"),
    )
    assert after_third_retrieve == ["think_tool"]
    assert three_source_tools == ["scoped_retrieve", "save_research_artifacts"]

    complementary = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-doc","source_id":"source-doc",'
        '"source_type":"engagement_document","stakeholder_id":null},'
        '{"evidence_id":"evidence-interview","source_id":"source-interview",'
        '"source_type":"interview","stakeholder_id":"stakeholder-1"}]}',
        tool_call_id="retrieve-complementary",
        name="scoped_retrieve",
    )
    _, complementary_tools = invoke(
        diverse_guard,
        [complementary, think_one],
        _tool_call("save_research_artifacts", {}, "save-complementary"),
    )
    assert complementary_tools == ["scoped_retrieve", "save_research_artifacts"]

    independent_stakeholders = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-a","source_id":"source-a",'
        '"source_type":"interview","stakeholder_id":"stakeholder-a"},'
        '{"evidence_id":"evidence-b","source_id":"source-b",'
        '"source_type":"interview","stakeholder_id":"stakeholder-b"}]}',
        tool_call_id="retrieve-stakeholders",
        name="scoped_retrieve",
    )
    _, stakeholder_tools = invoke(
        diverse_guard,
        [independent_stakeholders, think_one],
        _tool_call("save_research_artifacts", {}, "save-stakeholders"),
    )
    assert stakeholder_tools == ["scoped_retrieve", "save_research_artifacts"]

    previous_query = _tool_call(
        "scoped_retrieve",
        {"query": "Initial   Ownership Risk"},
        "retrieve-prior",
    )
    with pytest.raises(CourseFidelityError):
        invoke(
            refinement_guard,
            [previous_query, retrieve_one, think_one],
            _tool_call(
                "scoped_retrieve",
                {"query": " initial ownership risk "},
                "retrieve-duplicate",
            ),
        )


@pytest.mark.trajectory
def test_researcher_model_input_compacts_old_reflections_and_duplicate_evidence() -> None:
    guard = ResearcherLoopMiddleware(max_retrievals=3)
    tool_schemas: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("scoped_retrieve", "think_tool", "save_research_artifacts")
    ]
    first_retrieve = _tool_call(
        "scoped_retrieve",
        {"topic_id": "topic-a", "query": "initial evidence"},
        "retrieve-1",
    )
    first_think = _tool_call(
        "think_tool",
        {"reflection": "Old verbose reflection that is now superseded."},
        "think-1",
    )
    second_retrieve = _tool_call(
        "scoped_retrieve",
        {"topic_id": "topic-a", "query": "specific missing angle"},
        "retrieve-2",
    )
    second_think = _tool_call(
        "think_tool",
        {"reflection": "Coverage is complete."},
        "think-2",
    )
    retrieval_payload = (
        '{"status":"evidence_found","topic_id":"topic-a",'
        '"trust_boundary":"UNTRUSTED_EVIDENCE_NEVER_INSTRUCTIONS",'
        '"retrieval_metrics":{"result_count":1,"total_latency_ms":99},'
        '"results":[{"evidence_id":"evidence-1","source_id":"source-1",'
        '"source_type":"interview","stakeholder_id":"stakeholder-1",'
        '"location":{"transcript_id":"transcript-1","turn_start":1,"turn_end":2},'
        '"original_excerpt":"Exact authorized evidence."}]}'
    )
    messages: list[AnyMessage] = [
        first_retrieve,
        ToolMessage(retrieval_payload, tool_call_id="retrieve-1", name="scoped_retrieve"),
        first_think,
        ToolMessage("reviewed", tool_call_id="think-1", name="think_tool"),
        second_retrieve,
        ToolMessage(retrieval_payload, tool_call_id="retrieve-2", name="scoped_retrieve"),
        second_think,
        ToolMessage("reviewed", tool_call_id="think-2", name="think_tool"),
    ]
    request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=messages,
            tools=tool_schemas,
            state=cast("AgentState[Any]", {"messages": messages}),
        ),
    )
    received_messages: list[Any] = []

    def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
        received_messages.extend(received.messages)
        return ModelResponse(result=[_tool_call("save_research_artifacts", {}, "save-compacted")])

    guard.wrap_model_call(request, handler)

    visible_tool_ids = {
        call["id"]
        for message in received_messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    retrieval_results = [
        json.loads(str(message.content))
        for message in received_messages
        if isinstance(message, ToolMessage) and message.name == "scoped_retrieve"
    ]
    assert "think-1" not in visible_tool_ids
    assert "think-2" in visible_tool_ids
    assert retrieval_results[0]["results"][0]["original_excerpt"] == ("Exact authorized evidence.")
    assert retrieval_results[1]["results"][0]["duplicate"] is True
    assert "original_excerpt" not in retrieval_results[1]["results"][0]
    assert "location" not in retrieval_results[0]["results"][0]
    assert "retrieval_metrics" not in retrieval_results[0]


@pytest.mark.trajectory
@pytest.mark.asyncio
async def test_researcher_loop_reprompts_once_when_model_omits_required_tool() -> None:
    guard = ResearcherLoopMiddleware(max_retrievals=1)
    retrieve = ToolMessage(
        '{"status":"evidence_found","results":['
        '{"evidence_id":"evidence-1","source_id":"source-1"}]}',
        tool_call_id="retrieve-1",
        name="scoped_retrieve",
    )
    think = ToolMessage("reviewed", tool_call_id="think-1", name="think_tool")
    messages: list[AnyMessage] = [retrieve, think]
    tools: list[BaseTool | dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Test schema for {name}.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("scoped_retrieve", "think_tool", "save_research_artifacts")
    ]
    request = cast(
        "ModelRequest[InsightRuntimeContext]",
        ModelRequest(
            model=ToolCallingFakeModel(responses=[AIMessage(content="unused")]),
            messages=messages,
            tools=tools,
            state=cast("AgentState[Any]", {"messages": messages}),
        ),
    )
    received_system_messages: list[str] = []
    received_tool_choices: list[Any] = []

    async def handler(received: ModelRequest[InsightRuntimeContext]) -> ModelResponse[Any]:
        assert received.system_message is not None
        received_system_messages.append(str(received.system_message.text))
        received_tool_choices.append(received.tool_choice)
        if len(received_system_messages) == 1:
            return ModelResponse(result=[AIMessage(content="Research is complete.")])
        return ModelResponse(
            result=[_tool_call("save_research_artifacts", {}, "save-after-correction")]
        )

    response = await guard.awrap_model_call(request, handler)

    assert len(received_system_messages) == 2
    assert received_tool_choices == ["any", "any"]
    assert "TOOL-CALL CORRECTION" in received_system_messages[1]
    assert cast("AIMessage", response.result[0]).tool_calls[0]["name"] == (
        "save_research_artifacts"
    )


def test_researcher_loop_never_allows_more_than_five_retrievals() -> None:
    with pytest.raises(CourseFidelityError):
        ResearcherLoopMiddleware(max_retrievals=6)


def test_dependent_researcher_waits_for_declared_artifacts(tmp_path: Path) -> None:
    context = insight_context()
    artifacts = ScopedArtifactStore(tmp_path / "artifacts")
    guard = CourseFidelityGuardMiddleware(artifacts)
    plan = ResearchPlan.model_validate(
        {
            "plan_id": "plan-dependencies",
            "run_id": context.run_id,
            "engagement_id": context.access.engagement_id,
            "question": context.question,
            "topics": [
                {
                    "topic_id": "foundation",
                    "title": "Foundation",
                    "objective": "Establish the underlying facts.",
                    "questions": ["What facts are established?"],
                    "dependencies": [],
                    "priority": 1,
                },
                {
                    "topic_id": "dependent",
                    "title": "Dependent analysis",
                    "objective": "Analyze the established facts.",
                    "questions": ["What follows from the facts?"],
                    "dependencies": ["foundation"],
                    "priority": 2,
                },
            ],
            "source_strategy": ["document"],
            "completion_criteria": ["Complete dependencies before dependent work."],
            "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
    )
    artifacts.write_json(
        context.access,
        "/research_plan.json",
        plan.model_dump(mode="json"),
    )
    tool_message = _tool_call(
        "task",
        {
            "description": "topic_id=dependent Analyze only the dependent topic.",
            "subagent_type": "topic-researcher",
        },
        "dependent-task",
    )
    runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="dependent-task",
        store=None,
        tools=[],
    )
    request = ToolCallRequest(
        tool_call=tool_message.tool_calls[0],
        tool=None,
        state={
            "messages": [tool_message],
            "todos": [
                {"content": "Research Foundation", "status": "completed"},
                {"content": "Research Dependent analysis", "status": "pending"},
                {"content": "Edit the report", "status": "pending"},
                {"content": "Validate the report", "status": "pending"},
            ],
        },
        runtime=cast("Any", runtime),
    )

    called = False

    def premature_handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage("should not run", tool_call_id="dependent-task", name="task")

    rejected = guard.wrap_tool_call(request, premature_handler)
    assert isinstance(rejected, ToolMessage)
    assert rejected.status == "error"
    assert called is False

    request = request.override(
        state={
            **cast("dict[str, Any]", request.state),
            "todos": [
                {"content": "Research Foundation", "status": "completed"},
                {"content": "Research Dependent analysis", "status": "in_progress"},
                {"content": "Edit the report", "status": "pending"},
                {"content": "Validate the report", "status": "pending"},
            ],
        }
    )
    with pytest.raises(CourseFidelityError):
        guard.wrap_tool_call(
            request,
            lambda _request: ToolMessage(
                "should not run",
                tool_call_id="dependent-task",
                name="task",
            ),
        )

    artifacts.write_text(context.access, "/research/foundation/findings.md", "# Findings\n")
    artifacts.write_json(
        context.access,
        "/research/foundation/sources.json",
        {"status": "completed"},
    )
    with pytest.raises(EvidencePolicyError):
        guard.wrap_tool_call(
            request,
            lambda _request: (_ for _ in ()).throw(EvidencePolicyError()),
        )
    assert not artifacts.exists(context.access, "/research/dependent/sources.json")
    result = guard.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            "started",
            tool_call_id="dependent-task",
            name="task",
        ),
    )

    assert isinstance(result, ToolMessage)
    assert result.content == "started"


@pytest.mark.trajectory
async def test_async_researcher_topic_resolution_stays_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = insight_context()
    artifacts = ScopedArtifactStore(tmp_path / "artifacts")
    guard = CourseFidelityGuardMiddleware(artifacts)
    plan = ResearchPlan.model_validate(
        {
            "plan_id": "plan-async-boundary",
            "run_id": context.run_id,
            "engagement_id": context.access.engagement_id,
            "question": context.question,
            "topics": [
                {
                    "topic_id": "operations",
                    "title": "Operations",
                    "objective": "Establish the supported operating process.",
                    "questions": ["How does the process operate?"],
                    "dependencies": [],
                    "priority": 1,
                }
            ],
            "source_strategy": ["document"],
            "completion_criteria": ["Use only authorized evidence."],
            "created_at": datetime(2026, 7, 20, tzinfo=UTC),
        }
    )
    artifacts.write_json(
        context.access,
        "/research_plan.json",
        plan.model_dump(mode="json"),
    )
    task_call = _tool_call(
        "task",
        {
            "description": "topic_id=operations Research only the operations topic.",
            "subagent_type": "topic-researcher",
        },
        "async-researcher-task",
    )
    runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="async-researcher-task",
        store=None,
        tools=[],
    )
    request = ToolCallRequest(
        tool_call=task_call.tool_calls[0],
        tool=None,
        state={
            "messages": [task_call],
            "todos": [
                {"content": "Research Operations", "status": "in_progress"},
                {"content": "Edit the report", "status": "pending"},
                {"content": "Validate the report", "status": "pending"},
            ],
        },
        runtime=cast("Any", runtime),
    )
    event_loop_thread = threading.get_ident()
    resolution_threads: list[int] = []
    original_researcher_topic = guard._researcher_topic  # noqa: SLF001

    def checked_researcher_topic(received: ToolCallRequest) -> ResearchTopic | None:
        resolution_threads.append(threading.get_ident())
        return original_researcher_topic(received)

    monkeypatch.setattr(guard, "_researcher_topic", checked_researcher_topic)

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            "started",
            tool_call_id="async-researcher-task",
            name="task",
        )

    with blockbuster_ctx(
        scanned_modules=["stakeholder_intelligence_agent.middleware.course_fidelity"]
    ):
        result = await guard.awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage)
    assert result.content == "started"
    assert resolution_threads
    assert all(thread_id != event_loop_thread for thread_id in resolution_threads)


def test_third_identical_rejected_root_action_is_stopped(tmp_path: Path) -> None:
    context = insight_context()
    guard = CourseFidelityGuardMiddleware(ScopedArtifactStore(tmp_path / "artifacts"))
    arguments = {
        "topics": [{"topic_id": "topic-a", "title": "Missing TODO title"}],
        "source_strategy": ["document"],
        "completion_criteria": ["Use authorized evidence."],
    }
    first = _tool_call("create_research_plan", arguments, "plan-failure-1")
    second = _tool_call("create_research_plan", arguments, "plan-failure-2")
    current = _tool_call("create_research_plan", arguments, "plan-failure-3")
    todos = [{"content": "Different TODO title", "status": "in_progress"}]
    first_todos = _tool_call("write_todos", {"todos": todos}, "todos-1")
    second_todos = _tool_call("write_todos", {"todos": todos}, "todos-2")
    messages: list[AnyMessage] = [
        first_todos,
        ToolMessage("TODO saved.", tool_call_id="todos-1", name="write_todos"),
        first,
        ToolMessage(
            "Plan rejected.",
            tool_call_id="plan-failure-1",
            name="create_research_plan",
            status="error",
        ),
        second_todos,
        ToolMessage("TODO saved.", tool_call_id="todos-2", name="write_todos"),
        second,
        ToolMessage(
            "Plan rejected.",
            tool_call_id="plan-failure-2",
            name="create_research_plan",
            status="error",
        ),
        current,
    ]
    runtime: ToolRuntime[InsightRuntimeContext, dict[str, Any]] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _event: None,
        tool_call_id="plan-failure-3",
        store=None,
        tools=[],
    )
    request = ToolCallRequest(
        tool_call=current.tool_calls[0],
        tool=None,
        state={
            "messages": messages,
            "todos": todos,
        },
        runtime=cast("Any", runtime),
    )

    with pytest.raises(RepeatedToolFailureError):
        guard.wrap_tool_call(
            request,
            lambda _request: ToolMessage(
                "must not execute",
                tool_call_id="plan-failure-3",
                name="create_research_plan",
            ),
        )

    corrected_todos = [
        {"content": "Research Missing TODO title", "status": "in_progress"},
        {"content": "Edit the report", "status": "pending"},
        {"content": "Validate the report", "status": "pending"},
    ]
    corrected_write = _tool_call(
        "write_todos",
        {"todos": corrected_todos},
        "todos-corrected",
    )
    corrected_plan = _tool_call(
        "create_research_plan",
        arguments,
        "plan-after-correction",
    )
    corrected_messages = [
        *messages,
        corrected_write,
        ToolMessage(
            "TODO saved.",
            tool_call_id="todos-corrected",
            name="write_todos",
        ),
        corrected_plan,
    ]
    corrected_request = request.override(
        tool_call=corrected_plan.tool_calls[0],
        state={"messages": corrected_messages, "todos": corrected_todos},
    )
    corrected_result = guard.wrap_tool_call(
        corrected_request,
        lambda _request: ToolMessage(
            "executed after correction",
            tool_call_id="plan-after-correction",
            name="create_research_plan",
        ),
    )
    assert isinstance(corrected_result, ToolMessage)
    assert corrected_result.content == "executed after correction"


def _report_payload(question: str) -> dict[str, Any]:
    return {
        "report_id": "report-a",
        "engagement_id": "engagement-a",
        "question": question,
        "status": "insufficient_evidence",
        "executive_summary": "No READY authorized source supports a responsible conclusion.",
        "researched_topics": [
            {
                "topic_id": "topic-a",
                "title": "Operational Risk",
                "status": "insufficient_evidence",
                "summary": "The authorized retrieval scope contained no READY source.",
                "evidence_ids": [],
            }
        ],
        "findings": [],
        "responsibilities": [],
        "operational_risks": [],
        "buy_in_signals": [
            {
                "topic": "Operational Risk",
                "stakeholder_id": None,
                "role": None,
                "department": None,
                "category": "insufficient_evidence",
                "explanation": "No permitted evidence supports a qualitative signal.",
                "evidence_ids": [],
            }
        ],
        "contradictions": [],
        "evidence_gaps": [
            {
                "topic": "Operational Risk",
                "description": "No READY authorized source was indexed.",
                "impact": "Responsibilities and operational risks cannot be supported.",
            }
        ],
        "open_questions": ["Which current source establishes the operating process?"],
        "follow_up_recommendations": [],
        "evidence_ids": [],
        "citations": [],
        "run_metadata": {
            "run_id": "run-a",
            "started_at": "2026-07-15T00:00:00Z",
            "completed_at": "2026-07-15T00:00:01Z",
            "primary_model_id": "gemini-test-primary",
            "fallback_model_id": "gemini-test-fallback",
            "topic_count": 1,
            "status_detail": "The full workflow completed without authorized evidence.",
        },
    }


def _multi_topic_report_payload(question: str) -> dict[str, Any]:
    payload = _report_payload(question)
    payload["report_id"] = "report-multi"
    payload["researched_topics"] = [
        {
            "topic_id": "topic-risk",
            "title": "Operational Risk",
            "status": "insufficient_evidence",
            "summary": "No READY evidence supported the risk topic.",
            "evidence_ids": [],
        },
        {
            "topic_id": "topic-ownership",
            "title": "Process Ownership",
            "status": "insufficient_evidence",
            "summary": "No READY evidence supported the ownership topic.",
            "evidence_ids": [],
        },
    ]
    payload["evidence_gaps"] = [
        {
            "topic": "Operational Risk",
            "description": "No current risk source was available.",
            "impact": "Risk conclusions cannot be supported.",
        },
        {
            "topic": "Process Ownership",
            "description": "No current ownership source was available.",
            "impact": "Ownership conclusions cannot be supported.",
        },
    ]
    payload["run_metadata"] = dict(payload["run_metadata"])
    payload["run_metadata"].update(
        {
            "run_id": "run-multi",
            "topic_count": 2,
        }
    )
    return payload


@pytest.mark.integration
@pytest.mark.trajectory
async def test_real_deep_agent_uses_planning_researcher_and_editor_in_order(
    settings: Settings,
    tmp_path: Path,
) -> None:
    context = insight_context()
    rejected_report = _report_payload(context.question)
    rejected_report["contradictions"] = [
        {
            "topic": "Operational Risk",
            "side_a": {
                "statement": "One unsupported side.",
                "evidence_ids": ["unlinked-evidence-a"],
            },
            "side_b": {
                "statement": "A second unsupported side.",
                "evidence_ids": ["unlinked-evidence-b"],
            },
            "interpretation": "The draft must not retain uncitable contradiction evidence.",
            "evidence_ids": ["unlinked-evidence-a", "unlinked-evidence-b"],
        }
    ]
    primary = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "in_progress"},
                        {
                            "content": "Synthesize findings into the structured report",
                            "status": "pending",
                        },
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "main-todos",
            ),
            _tool_call(
                "create_research_plan",
                {
                    "topics": [
                        {
                            "topic_id": "topic-a",
                            "title": "Operational Risk",
                            "objective": "Identify supported operational risks and evidence gaps.",
                            "questions": ["Which current evidence supports an operational risk?"],
                            "required_source_types": ["document", "interview"],
                            "dependencies": [],
                            "priority": 1,
                        }
                    ],
                    "source_strategy": ["document", "interview"],
                    "completion_criteria": [
                        "Use only registered evidence or report insufficient evidence."
                    ],
                },
                "main-plan",
            ),
            _tool_call(
                "task",
                {
                    "description": (
                        "topic_id=topic-a Research only the Operational Risk topic and save "
                        "its required artifacts."
                    ),
                    "subagent_type": "topic-researcher",
                },
                "main-researcher",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "completed"},
                        {
                            "content": "Synthesize findings into the structured report",
                            "status": "in_progress",
                        },
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "main-research-complete",
            ),
            _tool_call(
                "task",
                {
                    "description": "Load all completed research artifacts and create the report.",
                    "subagent_type": "report-editor",
                },
                "main-editor",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "completed"},
                        {
                            "content": "Synthesize findings into the structured report",
                            "status": "completed",
                        },
                        {"content": "Validate the report", "status": "completed"},
                    ]
                },
                "main-workflow-complete",
            ),
            AIMessage(content="The validated report artifact is available."),
        ]
    )
    researcher = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "scoped_retrieve",
                {
                    "topic_id": "topic-a",
                    "query": "current operational risk evidence",
                },
                "research-retrieve",
            ),
            _tool_call(
                "think_tool",
                {"reflection": "Check scope, plan, and evidence availability."},
                "research-think",
            ),
            _tool_call(
                "save_research_artifacts",
                {
                    "topic_id": "topic-a",
                    "findings_markdown": (
                        "# Findings\n\nNo READY authorized source supports a risk conclusion."
                    ),
                    "evidence_ids": [],
                },
                "research-save",
            ),
            AIMessage(content="The topic artifacts were saved with insufficient evidence."),
        ]
    )
    editor = ToolCallingFakeModel(
        responses=[
            _tool_call("load_research_package", {}, "editor-load"),
            _tool_call(
                "save_final_report",
                {"report": rejected_report},
                "editor-rejected-save",
            ),
            _tool_call(
                "save_final_report",
                {"report": _report_payload(context.question)},
                "editor-save",
            ),
            AIMessage(content="The strict report was validated and saved."),
        ]
    )
    fallback = ToolCallingFakeModel(responses=[AIMessage(content="Fallback response.")])
    thread_id = context.access.thread_id
    assert thread_id is not None
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

    async with open_sqlite_checkpointer(tmp_path / "insight.sqlite3") as saver:
        graph = build_insight_graph(
            settings,
            dependencies=InsightGraphDependencies(
                primary_model=primary,
                fallback_model=fallback,
                researcher_model=researcher,
                editor_model=editor,
                checkpointer=saver,
                harness_provider="toolcallingfakemodel",
            ),
        )
        with blockbuster_ctx():
            raw_result = await graph.ainvoke(
                {"messages": [HumanMessage(content=context.question)]},
                config=config,
                context=context,
            )

    result = cast("dict[str, Any]", raw_result)
    messages = cast("list[BaseMessage]", result["messages"])
    parent_calls = [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    assert [call["name"] for call in parent_calls] == [
        "write_todos",
        "create_research_plan",
        "task",
        "write_todos",
        "task",
        "write_todos",
    ]
    assert [call["args"]["subagent_type"] for call in parent_calls if call["name"] == "task"] == [
        "topic-researcher",
        "report-editor",
    ]
    assert "general-purpose" not in str(parent_calls)

    plan = ResearchPlan.model_validate(result["research_plan"])
    assert plan.topics[0].topic_id == "topic-a"
    scope_root = settings.agent_artifacts_root / context.access.engagement_id / thread_id
    assert (scope_root / "research_plan.md").is_file()
    assert (scope_root / "research" / "topic-a" / "findings.md").is_file()
    assert (scope_root / "research" / "topic-a" / "sources.json").is_file()
    report_path = scope_root / "report" / "insight_report.json"
    report = InsightReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    assert report.status == "insufficient_evidence"

    assert primary.call_count == 7
    assert researcher.call_count == 4
    assert editor.call_count == 4
    assert set(primary.bound_tool_names) == {"write_todos", "create_research_plan", "task"}
    assert fallback.call_count == 0
    assert {"write_todos", "create_research_plan", "task"} <= set(primary.bound_tool_names)
    assert {"think_tool", "scoped_retrieve", "save_research_artifacts"} <= set(
        researcher.bound_tool_names
    )
    assert {"load_research_package", "save_final_report"} <= set(editor.bound_tool_names)
    generic_tools = {"write_todos", "ls", "glob", "grep", "read_file", "write_file", "edit_file"}
    assert generic_tools.isdisjoint(researcher.bound_tool_names)
    assert generic_tools.isdisjoint(editor.bound_tool_names)


@pytest.mark.integration
@pytest.mark.trajectory
async def test_plan_alignment_requires_explicit_write_todos_correction(
    settings: Settings,
) -> None:
    context = insight_context(thread_id="thread-plan-alignment")
    topic_title = "Operational Risks and Responsibilities Documentation"
    plan_arguments = {
        "topics": [
            {
                "topic_id": "topic-a",
                "title": topic_title,
                "objective": "Identify supported operational risks and responsibilities.",
                "questions": ["Which current evidence supports risks and responsibilities?"],
                "required_source_types": ["document", "interview"],
                "dependencies": [],
                "priority": 1,
            }
        ],
        "source_strategy": ["document", "interview"],
        "completion_criteria": ["Use only registered evidence or report insufficient evidence."],
    }
    primary = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {
                            "content": "Identify documentation about risks and responsibilities.",
                            "status": "in_progress",
                        },
                        {"content": "Research the documentation.", "status": "pending"},
                        {"content": "Synthesize the report.", "status": "pending"},
                    ]
                },
                "alignment-initial-todos",
            ),
            _tool_call(
                "create_research_plan",
                plan_arguments,
                "alignment-rejected-plan",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": f"Research {topic_title}", "status": "in_progress"},
                        {"content": "Edit the structured report", "status": "pending"},
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "alignment-corrected-todos",
            ),
            _tool_call(
                "create_research_plan",
                plan_arguments,
                "alignment-accepted-plan",
            ),
            _tool_call(
                "task",
                {
                    "description": "topic_id=topic-a Research only the assigned plan topic.",
                    "subagent_type": "topic-researcher",
                },
                "alignment-researcher",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": f"Research {topic_title}", "status": "completed"},
                        {"content": "Edit the structured report", "status": "in_progress"},
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "alignment-research-complete",
            ),
            _tool_call(
                "task",
                {
                    "description": "Load completed research and create the strict report.",
                    "subagent_type": "report-editor",
                },
                "alignment-editor",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": f"Research {topic_title}", "status": "completed"},
                        {"content": "Edit the structured report", "status": "completed"},
                        {"content": "Validate the report", "status": "completed"},
                    ]
                },
                "alignment-workflow-complete",
            ),
            AIMessage(content="The validated report artifact is available."),
        ]
    )
    researcher = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "scoped_retrieve",
                {"topic_id": "topic-a", "query": "current risk and responsibility evidence"},
                "alignment-retrieve",
            ),
            _tool_call(
                "think_tool",
                {"reflection": "Check scope and evidence availability."},
                "alignment-think",
            ),
            _tool_call(
                "save_research_artifacts",
                {
                    "topic_id": "topic-a",
                    "findings_markdown": "# Findings\n\nNo READY authorized source was available.",
                    "evidence_ids": [],
                },
                "alignment-save",
            ),
            AIMessage(content="The bounded research artifacts were saved."),
        ]
    )
    alignment_report = _report_payload(context.question)
    alignment_report["researched_topics"][0]["title"] = topic_title
    alignment_report["buy_in_signals"][0]["topic"] = topic_title
    alignment_report["evidence_gaps"][0]["topic"] = topic_title
    editor = ToolCallingFakeModel(
        responses=[
            _tool_call("load_research_package", {}, "alignment-load"),
            _tool_call(
                "save_final_report",
                {"report": alignment_report},
                "alignment-report",
            ),
            AIMessage(content="The strict report was validated and saved."),
        ]
    )
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=primary,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Fallback response.")]
            ),
            researcher_model=researcher,
            editor_model=editor,
            harness_provider="toolcallingfakemodel",
        ),
    )

    raw = await graph.ainvoke(
        {"messages": [HumanMessage(content=context.question)]},
        config={"configurable": {"thread_id": "thread-plan-alignment"}},
        context=context,
    )

    result = cast("dict[str, Any]", raw)
    messages = cast("list[BaseMessage]", result["messages"])
    parent_calls = [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    assert [call["name"] for call in parent_calls] == [
        "write_todos",
        "create_research_plan",
        "write_todos",
        "create_research_plan",
        "task",
        "write_todos",
        "task",
        "write_todos",
    ]
    plan_results = [
        message
        for message in messages
        if isinstance(message, ToolMessage) and message.name == "create_research_plan"
    ]
    assert [message.status for message in plan_results] == ["error", "success"]
    assert "Call write_todos" in str(plan_results[0].content)
    assert topic_title in "\n".join(str(item["content"]) for item in result["todos"])
    assert result["research_plan"]["topics"][0]["title"] == topic_title
    assert researcher.call_count == 4
    assert editor.call_count == 3
    assert primary.call_count == 9


@pytest.mark.integration
@pytest.mark.trajectory
async def test_multiple_topics_use_separate_researcher_tasks_before_editor(
    settings: Settings,
) -> None:
    context = insight_context(
        thread_id="thread-multi",
        run_id="run-multi",
        question="Which risks and process owners are supported?",
    )
    topics = [
        {
            "topic_id": "topic-risk",
            "title": "Operational Risk",
            "objective": "Identify supported risk evidence.",
            "questions": ["Which risk is supported?"],
            "required_source_types": ["document"],
            "dependencies": [],
            "priority": 1,
        },
        {
            "topic_id": "topic-ownership",
            "title": "Process Ownership",
            "objective": "Identify supported ownership evidence.",
            "questions": ["Which owner is supported?"],
            "required_source_types": ["interview"],
            "dependencies": [],
            "priority": 1,
        },
    ]
    primary = ToolCallingFakeModel(
        responses=[
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "in_progress"},
                        {"content": "Research Process Ownership", "status": "pending"},
                        {"content": "Edit the report", "status": "pending"},
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "multi-todos",
            ),
            _tool_call(
                "create_research_plan",
                {
                    "topics": topics,
                    "source_strategy": ["document", "interview"],
                    "completion_criteria": ["Record explicit gaps when evidence is absent."],
                },
                "multi-plan",
            ),
            _tool_call(
                "task",
                {
                    "description": "topic_id=topic-risk Research only operational risk.",
                    "subagent_type": "topic-researcher",
                },
                "multi-risk",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "completed"},
                        {"content": "Research Process Ownership", "status": "in_progress"},
                        {"content": "Edit the report", "status": "pending"},
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "multi-risk-complete",
            ),
            _tool_call(
                "task",
                {
                    "description": "topic_id=topic-ownership Research only process ownership.",
                    "subagent_type": "topic-researcher",
                },
                "multi-ownership",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "completed"},
                        {"content": "Research Process Ownership", "status": "completed"},
                        {"content": "Edit the report", "status": "in_progress"},
                        {"content": "Validate the report", "status": "pending"},
                    ]
                },
                "multi-research-complete",
            ),
            _tool_call(
                "task",
                {
                    "description": "Load both completed topic artifacts and edit the report.",
                    "subagent_type": "report-editor",
                },
                "multi-editor",
            ),
            _tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "Research Operational Risk", "status": "completed"},
                        {"content": "Research Process Ownership", "status": "completed"},
                        {"content": "Edit the report", "status": "completed"},
                        {"content": "Validate the report", "status": "completed"},
                    ]
                },
                "multi-workflow-complete",
            ),
            AIMessage(content="The validated report artifact is available."),
        ]
    )
    researcher_responses: list[BaseMessage] = []
    for topic_id in ("topic-risk", "topic-ownership"):
        researcher_responses.extend(
            [
                _tool_call(
                    "scoped_retrieve",
                    {"topic_id": topic_id, "query": f"current evidence for {topic_id}"},
                    f"{topic_id}-retrieve",
                ),
                _tool_call(
                    "think_tool",
                    {"reflection": f"Review only {topic_id}."},
                    f"{topic_id}-think",
                ),
                _tool_call(
                    "save_research_artifacts",
                    {
                        "topic_id": topic_id,
                        "findings_markdown": (
                            f"# Findings\n\nNo READY evidence was available for {topic_id}."
                        ),
                        "evidence_ids": [],
                    },
                    f"{topic_id}-save",
                ),
                AIMessage(content=f"Artifacts for {topic_id} were saved."),
            ]
        )
    researcher = ToolCallingFakeModel(responses=researcher_responses)
    editor = ToolCallingFakeModel(
        responses=[
            _tool_call("load_research_package", {}, "multi-load"),
            _tool_call(
                "save_final_report",
                {"report": _multi_topic_report_payload(context.question)},
                "multi-report",
            ),
            AIMessage(content="The strict multi-topic report was saved."),
        ]
    )
    graph = build_insight_graph(
        settings,
        dependencies=InsightGraphDependencies(
            primary_model=primary,
            fallback_model=ToolCallingFakeModel(
                responses=[AIMessage(content="Fallback response.")]
            ),
            researcher_model=researcher,
            editor_model=editor,
            harness_provider="toolcallingfakemodel",
        ),
    )
    thread_id = context.access.thread_id
    assert thread_id is not None
    raw = await graph.ainvoke(
        {"messages": [HumanMessage(content=context.question)]},
        config={"configurable": {"thread_id": thread_id}},
        context=context,
    )

    result = cast("dict[str, Any]", raw)
    calls = [
        call
        for message in cast("list[BaseMessage]", result["messages"])
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "task"
    ]
    assert [call["args"]["subagent_type"] for call in calls] == [
        "topic-researcher",
        "topic-researcher",
        "report-editor",
    ]
    assert "topic_id=topic-risk" in calls[0]["args"]["description"]
    assert "topic_id=topic-ownership" in calls[1]["args"]["description"]
    scope_root = settings.agent_artifacts_root / context.access.engagement_id / thread_id
    assert (scope_root / "research" / "topic-risk" / "sources.json").is_file()
    assert (scope_root / "research" / "topic-ownership" / "sources.json").is_file()
    assert (scope_root / "report" / "insight_report.json").is_file()
    assert researcher.call_count == 8
    assert editor.call_count == 3
