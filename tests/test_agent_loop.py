import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    text_delta,
    thinking_delta,
    tool_call_end,
    transport_error,
)
from tau_agent import (
    AgentEvent,
    AgentMessage,
    AgentTool,
    AgentToolResult,
    AssistantMessage,
    MessageEndEvent,
    MessageUpdateEvent,
    SimpleCancellationToken,
    TextContent,
    ToolCall,
    ToolExecutionEndEvent,
    ToolExecutionUpdateEvent,
    ToolResultMessage,
    TurnRetryStartEvent,
    UserMessage,
)
from tau_agent.loop import run_agent_loop
from tau_agent.provider_events import ThinkingDeltaEvent
from tau_agent.types import JSONValue
from tau_ai import CancellationToken, FakeProvider


async def _collect(stream: AsyncIterator[AgentEvent]) -> list[AgentEvent]:
    return [event async for event in stream]


def _tool(
    name: str,
    execute_fn,
) -> AgentTool:  # noqa: ANN001
    return AgentTool(
        name=name,
        label=name.title(),
        description=f"Run {name}.",
        parameters={"type": "object"},
        execute_fn=execute_fn,
    )


@pytest.mark.anyio
async def test_agent_loop_streams_canonical_nested_events() -> None:
    messages: list[AgentMessage] = [UserMessage(content="Say hello")]
    assistant = AssistantMessage(content="Hello", model="fake")
    provider = FakeProvider(
        [[assistant_start(), text_delta("Hel"), text_delta("lo"), assistant_done(assistant)]]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
        )
    )

    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_update",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    updates = [event for event in events if isinstance(event, MessageUpdateEvent)]
    assert [event.assistant_message_event.delta for event in updates] == ["Hel", "lo"]  # type: ignore[union-attr]
    assert messages == [messages[0], assistant]


@pytest.mark.anyio
async def test_agent_loop_nests_thinking_events_without_losing_final_message() -> None:
    messages: list[AgentMessage] = [UserMessage(content="Think briefly")]
    assistant = AssistantMessage(content="Done", model="fake")
    provider = FakeProvider(
        [
            [
                assistant_start(),
                thinking_delta("hidden "),
                thinking_delta("reasoning"),
                text_delta("Done"),
                assistant_done(assistant),
            ]
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
        )
    )

    nested = [
        event.assistant_message_event
        for event in events
        if isinstance(event, MessageUpdateEvent)
        and isinstance(event.assistant_message_event, ThinkingDeltaEvent)
    ]
    assert [event.delta for event in nested] == ["hidden ", "reasoning"]
    assert messages[-1] == assistant
    # The final provider message is the canonical persistence boundary.
    assert isinstance(messages[-1], AssistantMessage)


@pytest.mark.anyio
async def test_agent_loop_executes_tool_and_emits_tool_result_message_lifecycle() -> None:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: CancellationToken | None = None,
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        return AgentToolResult(
            content=[TextContent(text=f"contents of {arguments['path']}")],
            details={"path": arguments["path"]},
        )

    tool_call = ToolCall(id="call-1", name="read", arguments={"path": "README.md"})
    first = AssistantMessage(content=[TextContent(text="Reading."), tool_call], model="fake")
    final = AssistantMessage(content="Done.", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), tool_call_end(tool_call), assistant_done(first, "toolUse")],
            [assistant_start(), text_delta("Done."), assistant_done(final)],
        ]
    )
    messages: list[AgentMessage] = [UserMessage(content="Read README.md")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[_tool("read", execute)],
        )
    )

    result = next(message for message in messages if isinstance(message, ToolResultMessage))
    assert result.role == "toolResult"
    assert result.tool_name == "read"
    assert result.text == "contents of README.md"
    assert result.details == {"path": "README.md"}
    result_lifecycle = [
        event.type
        for event in events
        if isinstance(event, (MessageEndEvent,)) and event.message is result
    ]
    assert result_lifecycle == ["message_end"]
    assert [event.type for event in events].count("message_start") == 3
    assert provider.calls[1][2] == messages[:3]


@pytest.mark.anyio
async def test_agent_loop_passes_call_id_signal_and_progress_to_tool() -> None:
    observed: list[tuple[str, CancellationToken | None]] = []

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: CancellationToken | None = None,
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del arguments
        observed.append((tool_call_id, signal))
        assert on_update is not None
        on_update(AgentToolResult(content="working"))
        await asyncio.sleep(0)
        return AgentToolResult(content="done")

    call = ToolCall(id="call-1", name="work", arguments={})
    first = AssistantMessage(content=[call], model="fake")
    final = AssistantMessage(content="finished", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), tool_call_end(call), assistant_done(first, "toolUse")],
            [assistant_start(), assistant_done(final)],
        ]
    )
    signal = SimpleCancellationToken()

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=[UserMessage(content="work")],
            tools=[_tool("work", execute)],
            signal=signal,
        )
    )

    assert observed == [("call-1", signal)]
    updates = [event for event in events if isinstance(event, ToolExecutionUpdateEvent)]
    assert [event.partial_result.text for event in updates] == ["working"]


@pytest.mark.anyio
async def test_agent_loop_records_unknown_tool_as_canonical_error_result() -> None:
    call = ToolCall(id="call-1", name="missing", arguments={})
    assistant = AssistantMessage(content=[call], model="fake")
    messages: list[AgentMessage] = [UserMessage(content="Use it")]
    provider = FakeProvider(
        [[assistant_start(), tool_call_end(call), assistant_done(assistant, "toolUse")]]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turns=1,
        )
    )

    end = next(event for event in events if isinstance(event, ToolExecutionEndEvent))
    assert end.is_error is True
    assert end.result.text == "Tool missing not found"
    result = next(message for message in messages if isinstance(message, ToolResultMessage))
    assert result.is_error is True
    assert result.text == "Tool missing not found"


@pytest.mark.anyio
async def test_agent_loop_converts_provider_error_to_assistant_error_message() -> None:
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider([[assistant_error("provider failed")]])

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
        )
    )

    assert [event.type for event in events] == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "turn_end",
        "agent_end",
    ]
    error = messages[-1]
    assert isinstance(error, AssistantMessage)
    assert error.stop_reason == "error"
    assert error.error_message == "provider failed"


@pytest.mark.anyio
async def test_agent_loop_excludes_empty_failed_assistant_from_next_provider_call() -> None:
    messages: list[AgentMessage] = []
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_error("provider failed")],
            [assistant_start(), assistant_done(recovered)],
        ]
    )

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="hello")],
        )
    )
    failed = messages[-1]
    assert isinstance(failed, AssistantMessage)
    assert failed.stop_reason == "error"

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            prompts=[UserMessage(content="continue")],
        )
    )

    assert failed in messages
    replayed = provider.calls[1][2]
    assert [message.text for message in replayed] == ["hello", "continue"]
    assert failed not in replayed
    assert messages[-1] is recovered


@pytest.mark.anyio
async def test_agent_loop_repairs_malformed_tool_history_before_provider_call() -> None:
    call = ToolCall(id="call-1", name="read", arguments={"path": "README.md"})
    assistant = AssistantMessage(content=[call])
    late_user = UserMessage(content="continue")
    orphan = ToolResultMessage(tool_call_id="call-missing", tool_name="bash", content="orphan")
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider([[assistant_start(), assistant_done(recovered)]])
    messages: list[AgentMessage] = [assistant, late_user, orphan]

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
        )
    )

    replayed = provider.calls[0][2]
    assert replayed[0] is assistant
    repair = replayed[1]
    assert isinstance(repair, ToolResultMessage)
    assert repair.tool_call_id == "call-1"
    assert repair.is_error is True
    assert replayed[2] is late_user
    assert orphan not in replayed
    assert orphan in messages


@pytest.mark.anyio
async def test_agent_loop_injects_steering_and_follow_up_messages() -> None:
    call = ToolCall(id="call-1", name="work", arguments={})

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="ok")

    first = AssistantMessage(content=[call], model="fake")
    second = AssistantMessage(content="second", model="fake")
    third = AssistantMessage(content="third", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), tool_call_end(call), assistant_done(first, "toolUse")],
            [assistant_start(), assistant_done(second)],
            [assistant_start(), assistant_done(third)],
        ]
    )
    steering = [UserMessage(content="steer")]
    follow_up = [UserMessage(content="follow up")]

    def pop(queue: list[UserMessage]) -> tuple[UserMessage, ...]:
        return (queue.pop(0),) if queue else ()

    messages: list[AgentMessage] = [UserMessage(content="start")]
    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[_tool("work", execute)],
            get_steering_messages=lambda: pop(steering),
            get_follow_up_messages=lambda: pop(follow_up),
        )
    )

    assert [message.text for message in messages if isinstance(message, UserMessage)] == [
        "start",
        "steer",
        "follow up",
    ]
    assert len(provider.calls) == 3


@pytest.mark.anyio
async def test_loop_applies_seed_to_every_leg_of_the_prompt() -> None:
    """Prove a seeded prompt streams the same seed on every leg of that prompt.

    The loop must carry a per-prompt seed through the tool-call continuation
    leg (tool call -> result -> continuation) so a retried turn uses the same
    fresh random seed for every provider request it makes.
    """

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="ok")

    tool_call = ToolCall(id="call-1", name="read", arguments={"path": "README.md"})
    first = AssistantMessage(content=[TextContent(text="Reading."), tool_call], model="fake")
    final = AssistantMessage(content="Done.", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), tool_call_end(tool_call), assistant_done(first, "toolUse")],
            [assistant_start(), text_delta("Done."), assistant_done(final)],
        ]
    )
    messages: list[AgentMessage] = [UserMessage(content="Read README.md")]

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[_tool("read", execute)],
            seed=7,
        )
    )

    assert provider.seeds == [7, 7]


@pytest.mark.anyio
async def test_loop_applies_seed_across_steer_and_follow_up_injection() -> None:
    """Prove a seeded prompt keeps its seed on injected steer/follow-up legs.

    Messages queued mid-run drain into the running prompt stream, so their
    provider calls must stay under the original prompt's seed; only a new
    harness run starts a fresh (seedless or reseeded) stream.
    """
    call = ToolCall(id="call-1", name="work", arguments={})

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal=None,  # noqa: ANN001
        on_update=None,  # noqa: ANN001
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="ok")

    first = AssistantMessage(content=[call], model="fake")
    second = AssistantMessage(content="second", model="fake")
    third = AssistantMessage(content="third", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), tool_call_end(call), assistant_done(first, "toolUse")],
            [assistant_start(), assistant_done(second)],
            [assistant_start(), assistant_done(third)],
        ]
    )
    steering = [UserMessage(content="steer")]
    follow_up = [UserMessage(content="follow up")]

    def pop(queue: list[UserMessage]) -> tuple[UserMessage, ...]:
        return (queue.pop(0),) if queue else ()

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=[UserMessage(content="start")],
            tools=[_tool("work", execute)],
            get_steering_messages=lambda: pop(steering),
            get_follow_up_messages=lambda: pop(follow_up),
            seed=7,
        )
    )

    assert provider.seeds == [7, 7, 7]


@pytest.mark.anyio
async def test_agent_loop_stops_with_assistant_error_after_max_turns() -> None:
    call = ToolCall(id="call-1", name="missing", arguments={})
    assistant = AssistantMessage(content=[call], model="fake")
    provider = FakeProvider(
        [[assistant_start(), tool_call_end(call), assistant_done(assistant, "toolUse")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="loop")]

    await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turns=1,
        )
    )

    error = messages[-1]
    assert isinstance(error, AssistantMessage)
    assert error.stop_reason == "error"
    assert error.error_message == "Agent stopped after max_turns=1"
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_agent_loop_retries_transient_failure_then_succeeds() -> None:
    """Prove a retryable failure is retried invisibly and never touches history."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    recovered = AssistantMessage(content="recovered", model="fake")
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), transport_error("peer closed connection")],
            [assistant_start(), text_delta("recovered"), assistant_done(recovered)],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    retries = [event for event in events if isinstance(event, TurnRetryStartEvent)]
    assert len(retries) == 1
    assert retries[0].attempt == 2
    assert retries[0].max_attempts == 3
    assert retries[0].reason == "peer closed connection"
    assert retries[0].error_message == "peer closed connection"
    assert messages[-1] is recovered
    assert len(provider.calls) == 2
    assert provider.calls[0][2] == provider.calls[1][2]
    assert not any(
        isinstance(event, MessageEndEvent)
        and isinstance(event.message, AssistantMessage)
        and event.message.stop_reason == "error"
        for event in events
    )


@pytest.mark.anyio
async def test_agent_loop_exhausts_turn_retry_budget() -> None:
    """Prove two retries are allowed and the third failure ends the run as today."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("a"), transport_error("drop 1", partial="a")],
            [assistant_start(), text_delta("b"), transport_error("drop 2", partial="b")],
            [assistant_start(), text_delta("c"), transport_error("drop 3", partial="c")],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 2
    assert len(provider.calls) == 3
    final = messages[-1]
    assert isinstance(final, AssistantMessage)
    assert final.stop_reason == "error"
    assert final.error_message == "drop 3"
    assert final.text == "c"


@pytest.mark.anyio
async def test_agent_loop_turn_retry_disabled_with_zero_budget() -> None:
    """Prove a zero budget keeps today's terminal behavior exactly."""
    provider = FakeProvider(
        [[assistant_start(), text_delta("partial"), transport_error("drop", partial="partial")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=0,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_non_transport_error() -> None:
    """Prove only centrally classified transient failures trigger a retry."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider([[assistant_error("invalid api key", status_code=401)]])

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_retry_backoff_delays_grow() -> None:
    """Prove retry delays are exponential and stop at the one-second cap."""
    provider = FakeProvider(
        [
            [assistant_start(), transport_error("1")],
            [assistant_start(), transport_error("2")],
            [assistant_start(), transport_error("3")],
            [assistant_start(), assistant_done(AssistantMessage(content="ok", model="fake"))],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=[UserMessage(content="hello")],
            tools=[],
            max_turn_retries=3,
        )
    )

    assert [event.delay_seconds for event in events if isinstance(event, TurnRetryStartEvent)] == [
        0.25,
        0.5,
        1.0,
    ]


@pytest.mark.anyio
async def test_agent_loop_cancel_during_retry_backoff_discards_partial() -> None:
    """Prove cancelling during backoff ends the run with the partial discarded."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [[assistant_start(), text_delta("partial"), transport_error("drop", partial="partial")]]
    )
    signal = SimpleCancellationToken()
    events: list[AgentEvent] = []

    async for event in run_agent_loop(
        provider=provider,
        model="fake",
        system="You are Tau.",
        messages=messages,
        tools=[],
        signal=signal,
        max_turn_retries=2,
    ):
        events.append(event)
        if isinstance(event, TurnRetryStartEvent):
            signal.cancel()

    assert len(provider.calls) == 1
    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    final = messages[-1]
    assert isinstance(final, AssistantMessage)
    assert final.stop_reason == "error"
    assert final.error_message == "drop"
    assert not final.content


@pytest.mark.anyio
async def test_agent_loop_cancel_during_reattempt_ends_run() -> None:
    """Prove cancelling a reattempt never triggers further attempts."""
    provider = FakeProvider(
        [
            [assistant_start(), text_delta("partial"), transport_error("drop")],
            [
                assistant_start(),
                text_delta("re"),
                assistant_done(AssistantMessage(content="done", model="fake")),
            ],
        ]
    )
    signal = SimpleCancellationToken()
    events: list[AgentEvent] = []

    async for event in run_agent_loop(
        provider=provider,
        model="fake",
        system="You are Tau.",
        messages=[UserMessage(content="hello")],
        tools=[],
        signal=signal,
        max_turn_retries=2,
    ):
        events.append(event)
        if isinstance(event, MessageUpdateEvent) and event.message.text == "re":
            signal.cancel()

    assert len(provider.calls) == 2
    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    assert events[-1].type == "agent_end"


@pytest.mark.anyio
async def test_agent_loop_retries_exhausted_transient_status() -> None:
    """Prove a 503 that outlives the adapter is retried at turn level with an HTTP reason."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_error("boom", status_code=503)],
            [assistant_error("boom", status_code=503)],
            [assistant_error("boom", status_code=503)],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    retries = [event for event in events if isinstance(event, TurnRetryStartEvent)]
    assert len(retries) == 2
    assert all(retry.reason == "HTTP 503" for retry in retries)
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_mixed_outcomes_end_the_retry_sequence() -> None:
    """Prove a non-retryable failure stops retrying and is projected terminally."""
    messages: list[AgentMessage] = [UserMessage(content="hello")]
    provider = FakeProvider(
        [
            [assistant_error("boom", status_code=503)],
            [assistant_error("unauthorized", status_code=401)],
        ]
    )

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert sum(isinstance(event, TurnRetryStartEvent) for event in events) == 1
    assert len(provider.calls) == 2
    assert messages[-1].error_message == "unauthorized"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_terminal_rate_limit() -> None:
    """Prove a quota 429 is terminal despite its transient status code."""
    provider = FakeProvider(
        [[assistant_error("quota", status_code=429, body="Your plan has insufficient_quota.")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"


@pytest.mark.anyio
async def test_agent_loop_does_not_retry_context_overflow() -> None:
    """Prove context-overflow failures bypass turn-level retry entirely."""
    provider = FakeProvider(
        [[assistant_error("overflow", status_code=400, body="maximum context length exceeded")]]
    )
    messages: list[AgentMessage] = [UserMessage(content="hello")]

    events = await _collect(
        run_agent_loop(
            provider=provider,
            model="fake",
            system="You are Tau.",
            messages=messages,
            tools=[],
            max_turn_retries=2,
        )
    )

    assert not any(isinstance(event, TurnRetryStartEvent) for event in events)
    assert len(provider.calls) == 1
    assert messages[-1].stop_reason == "error"
