# Instrumenting LangGraph with Lumoz

This guide covers how this example integrates LangGraph with Lumoz observability, including the parts that are specific to a graph with `interrupt_before` (human-in-the-loop approval) and tool errors. For the base exporter setup shared by every LangGraph example here, see [research-writer/INSTRUMENTATION.md](../research-writer/INSTRUMENTATION.md) — this guide only covers what's different.

## 1. Configure the Exporter and Initialize First

Identical to the base pattern — see [`travel_agent.py`](travel_agent.py) → `configure_lumoz_tracing()`. Called at module level, before the graph, tools, or `ChatOpenAI` are constructed:

```python
tracer_provider = configure_lumoz_tracing()
tracing_enabled = tracer_provider is not None
```

## 2. One Root Span Per Scenario, Not Per Turn

A booking conversation spans many turns and, often, a pause for approval in the middle of one. Wrapping the *whole* scripted scenario in a single root span — rather than one span per user message — keeps the full conversation in one trace:

```python
tracer = trace.get_tracer("travel_agent")

with tracer.start_as_current_span(
    f"travel_agent_scenario_{name}",
    attributes={"session.id": session_id, "user.id": user_id, "scenario": name},
):
    with using_attributes(user_id=user_id, session_id=session_id):
        for turn in scenario["turns"]:
            run_turn(app, config, turn["user"], ...)
```

The interactive REPL does the opposite — one root span per turn — since a live session has no natural end. Pick whichever grouping matches how you want to review the trace afterward.

## 3. Human-in-the-Loop Approval Doesn't Need Special Span Handling

`interrupt_before=["sensitive_tools"]` pauses graph execution, but the OpenTelemetry span for the *user's turn* is still open the whole time — the pause happens inside your `with` block, not around it. Resuming with `app.stream(None, config, ...)` (approve) or `app.stream({"messages": [...]}, config, ...)` (deny, with a `ToolMessage` explaining why) both continue emitting spans as children of the same root span:

```python
snapshot = app.get_state(config)
while snapshot.next:
    tool_calls = snapshot.values["messages"][-1].tool_calls
    if approve(tool_calls):
        events = app.stream(None, config, stream_mode="values")
    else:
        events = app.stream(
            {"messages": [ToolMessage(tool_call_id=tc["id"], content="Denied by reviewer.")
                          for tc in tool_calls]},
            config, stream_mode="values",
        )
    snapshot = app.get_state(config)
```

The result: one trace shows the assistant's proposed action, the pause, the reviewer's decision, and what the assistant did next — all in sequence.

## 4. Recording a Tool Exception on the Current Span

When a tool raises (see `book_car_rental`'s simulated outage), attach the exception to the currently active span before re-raising so it shows up on the tool's own span rather than only in the fallback `ToolMessage`:

```python
from opentelemetry import trace

current_span = trace.get_current_span()
if current_span and current_span.is_recording():
    current_span.record_exception(error)
    current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
raise error
```

`ToolNode(...).with_fallbacks(...)` (see `create_tool_node_with_fallback`) then converts the exception into a `ToolMessage` so the assistant can tell the traveler what happened, but the span itself keeps the real exception for anyone debugging the trace later.

## 5. Flush Before Exit

```python
if tracing_enabled and tracer_provider:
    tracer_provider.force_flush()
```

Called after every scripted scenario and on `/quit` in interactive mode — `BatchSpanProcessor` buffers spans, so a script that exits immediately can drop its last batch.

## Trace Structure

A `book_trip` scenario run produces a trace like:

```
travel_agent_scenario_book_trip           (root — manual span)
├── fetch_user_info                       (NODE)
├── assistant                             (NODE)
│   └── ChatOpenAI                        (LLM)
├── safe_tools                            (NODE)
│   └── search_hotels                     (TOOL)
├── assistant                             (NODE)
│   └── ChatOpenAI                        (LLM)
│                                         [ interrupt_before: sensitive_tools ]
├── sensitive_tools                       (NODE, resumed after approval)
│   └── book_hotel                        (TOOL)
├── assistant                             (NODE)
│   └── ChatOpenAI                        (LLM)
└── ... (repeats for the car rental)
```

For the `car_rental_outage` scenario, `book_car_rental`'s span carries an `ERROR` status and the recorded exception instead of a normal tool result.
