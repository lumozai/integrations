# Claude Agent SDK — Ticket Triage

A backend job on the Claude Agent SDK, traced to Lumoz: a runner takes support tickets from a queue, a Claude agent checks each claim against the helpdesk records served by an MCP server over HTTP, looks up the applicable policy, and records the resolution. No person is in the loop.

Three processes: `runner.py`, the Claude Code CLI the SDK starts, and `mcp_server.py`. Two things are switched on for tracing, and the app writes one span of its own.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys
```

The Claude Agent SDK runs the Claude Code CLI, which needs Node.js 18 or later. The MCP server uses the `mcp` 2.x server API.

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | billed by the Claude Code CLI |
| `LUMOZ_API_KEY` | no | enables Lumoz tracing |
| `OTEL_ENDPOINT` | no | Lumoz ingest endpoint |
| `LUMOZ_ENVIRONMENT` | no | `deployment.environment` on every span; default `development` |
| `HELPDESK_MCP_URL` | no | an MCP server you run yourself; otherwise the runner starts one on port 8765 |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | no | set to `0` to run with the instrumentor alone |

Without the Lumoz variables the job still runs; tracing is skipped.

## Run

```bash
python runner.py
```

```
[lumoz] claude-agent-sdk-ticket-triage -> https://api.lumoz.ai/proxy/v1/traces (instrumentor and Claude Code CLI telemetry)
[helpdesk] claude-agent-sdk-helpdesk-mcp -> https://api.lumoz.ai/proxy/v1/traces

=== tkt-4821 Order arrived damaged (email) ===
  turns=8 cost=$0.27 error=False
  note: I recorded tkt-4821 as a replace. ...

=== tkt-4833 Charged twice (chat) ===
  turns=7 cost=$0.20 error=False
  note: I'm refunding the duplicate $89.00 charge ...
```

Two tickets cost under half a dollar of API credit and take about a minute.

## Who calls whom

```
ticket queue ──▶ runner.py ──▶ Claude Code CLI ──▶ Anthropic API        (one request per model turn)
                     ▲              │
                     │              └──▶ helpdesk MCP server              (tools/call over HTTP)
                 messages, result
```

The runner never calls a model or a tool itself. It hands the CLI the prompt and reads back the messages; the CLI does the rest, including every call to the MCP server.

## Who records what

| Recorder | Where it runs | What it records |
|---|---|---|
| App's root span | runner.py | one root span per ticket |
| OpenInference instrumentor | runner.py | the run, tool calls and sub-agents |
| CLI's tracer | Claude Code CLI | model calls, tool calls, sub-agents |
| MCP server's tracer | mcp_server.py | each call it serves |

Each process exports its own spans to Lumoz over OTLP. Lumoz joins them by trace id: one trace per ticket, the runner and the CLI under one service name, the MCP server as a second service in the same trace.

**App's root span** (`runner.py`, `triage()`). One root span per ticket, kind `CHAIN`, with the instruction and the ticket as `input.value` and the agent's note as `output.value`. It marks the job's boundary: when a job is more than one `query()` call, or has work around the agent that belongs to the same unit, this root makes it one trace and one business task. A single-call job works without it; the instrumentor's root carries the ask and the answer, and the task is named from that just the same. No tag is needed either way.

**OpenInference instrumentor** (`lumoz_tracing.py`). `ClaudeAgentSDKInstrumentor().instrument()`, called before `claude_agent_sdk` is imported. It records the run as one AGENT span with the ask, the answer, every reply and the run's token totals, and one TOOL span per tool call through the SDK's hooks, with the arguments, the result, the call id and any error. A sub-agent gets its own span, and the call that delegates to it carries the sub-agent's type in its name, such as `Agent (code-reviewer)`.

**CLI's tracer** (`lumoz_tracing.py`, `enable_cli_telemetry()`). The Claude Code CLI's own telemetry. It records one span per model call with its tokens and model, and its own record of each tool call. The SDK copies the active trace context into the CLI it starts, so these spans nest under the run. The helper switches it on once, through the CLI's environment, with the same endpoint, key and service name as the app:

- `CLAUDE_CODE_ENABLE_TELEMETRY=1`
- `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`
- `OTEL_TRACES_EXPORTER=otlp`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_SERVICE_NAME`

A value already in the environment, or passed through `ClaudeAgentOptions.env`, wins. One small helper call per session arrives as a one-span trace of its own; it counts toward usage and is never a task.

**MCP server's tracer** (`mcp_server.py`, `LumozToolSpans`). With its telemetry on, the CLI sends a `traceparent` header on every HTTP request to the server. The middleware starts one span per call on that context and exports it under the server's own service name: a TOOL span for a lookup, and a RETRIEVER span for the policy search, carrying the query and the documents it returned. The MCP SDK's built-in tracing middleware is replaced, because it reads request metadata the CLI does not send.

## What you see in Lumoz

One trace per ticket. Its tree, as recorded:

```
CHAIN  triage_ticket                       the job: instruction + ticket in, the note out
  AGENT  ClaudeAgentSDK.query              the run: ask, answer, replies, run totals
    claude_code.interaction                the CLI's turn
      LLM  claude_code.llm_request         model call 1, with its tokens
      claude_code.tool ToolSearch          the CLI loading the MCP tool definitions
      LLM  claude_code.llm_request         model call 2
      claude_code.tool lookup_customer     the CLI's record of the call
        claude_code.tool.execution
          TOOL  lookup_customer            served by the MCP server, second service
      ...  get_order, get_shipment, list_charges, the same way
      claude_code.tool search_policies
        claude_code.tool.execution
          RETRIEVER  search_policies       the query and three documents, with scores
      LLM  claude_code.llm_request         model call 3
      claude_code.tool record_resolution
        claude_code.tool.execution
          TOOL  record_resolution
      LLM  claude_code.llm_request         model call 4, the note
    TOOL  ToolSearch                       the hook's record
    TOOL  mcp__helpdesk__lookup_customer   the hook's record: arguments, result, call id
    TOOL  mcp__helpdesk__get_order
    TOOL  mcp__helpdesk__search_policies
    TOOL  mcp__helpdesk__list_charges
    TOOL  mcp__helpdesk__get_shipment
    TOOL  mcp__helpdesk__record_resolution
```

- A business task per ticket, named from the instruction: `Triage Support Ticket`, with the note as its answer and an outcome.
- The trajectory follows the model calls and the `mcp__helpdesk__` tool calls; each tool counts once, from the hook's span. `ToolSearch` stays in the trace and out of the task.
- The trace's token counts and the Models tab come from the CLI's model calls.
- Two services on the trace: `claude-agent-sdk-ticket-triage` and `claude-agent-sdk-helpdesk-mcp`.
- The policy search shows as a retrieval with its query and documents.

## Limits

- The messages the model received and the tool definitions it was offered never leave the CLI, so no span carries them. Signals that need them do not apply.
- Without the CLI's telemetry there are no model-call spans: the run's token totals sit on the agent span, and the Models tab stays empty.
- On `ClaudeSDKClient`, the CLI's spans arrive as separate traces that share the session id with the instrumentor's; they count toward usage and are never a task of their own.
