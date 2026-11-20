# Claude Agent SDK — Ticket Triage

A backend job on the Claude Agent SDK, traced to Lumoz: a runner takes support tickets from a queue, a Claude agent checks each claim against the helpdesk records served by an MCP server over HTTP, looks up the applicable policy, and records the resolution. No person is in the loop.

The job runs on both of the SDK's paths, from the same tickets and the same MCP server:

| Runner | Path | Shape |
|---|---|---|
| `runner.py` | `query()` | one call per ticket, one trace per ticket |
| `runner_client.py` | `ClaudeSDKClient` | one session, each ticket a turn, two traces per turn |

Three processes either way: the runner, the Claude Code CLI the SDK starts, and `mcp_server.py`. Two things are switched on for tracing, and the app writes one span of its own.

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

## Who records what

| Recorder | Where it runs | What it records |
|---|---|---|
| App's root span | the runner | one root span per ticket |
| OpenInference instrumentor | the runner | the run, tool calls and sub-agents |
| CLI's tracer | Claude Code CLI | model calls, tool calls, sub-agents |
| MCP server's tracer | mcp_server.py | each call it serves |

Each process exports its own spans to Lumoz over OTLP, and Lumoz joins them by trace id.

**App's root span** (`triage_ticket`, in both runners). One root span per ticket, kind `CHAIN`, with the instruction and the ticket as `input.value` and the agent's note as `output.value`. It marks the job's boundary: when a job is more than one call, or has work around the agent that belongs to the same unit, this root makes it one trace and one business task. A single-call job works without it; the instrumentor's root carries the ask and the answer, and the task is named from that just the same. No tag is needed either way.

**OpenInference instrumentor** (`lumoz_tracing.py`). `ClaudeAgentSDKInstrumentor().instrument()`, called before `claude_agent_sdk` is imported. It records the run as one AGENT span with the ask, the answer, every reply and the run's token totals, and one TOOL span per tool call through the SDK's hooks, with the arguments, the result, the call id and any error. A sub-agent gets its own span, and the call that delegates to it carries the sub-agent's type in its name, such as `Agent (code-reviewer)`.

**CLI's tracer** (`lumoz_tracing.py`, `enable_cli_telemetry()`). The Claude Code CLI's own telemetry: one span per model call with its tokens and model, and its own record of each tool call. The helper switches it on once, through the CLI's environment, with the same endpoint, key and service name as the app:

- `CLAUDE_CODE_ENABLE_TELEMETRY=1`
- `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`
- `OTEL_TRACES_EXPORTER=otlp`
- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` and `OTEL_SERVICE_NAME`

A value already in the environment, or passed through `ClaudeAgentOptions.env`, wins.

**MCP server's tracer** (`mcp_server.py`, `LumozToolSpans`). With its telemetry on, the CLI sends a `traceparent` header on every HTTP request to the server. The middleware starts one span per call on that context and exports it under the server's own service name: a TOOL span for a lookup, and a RETRIEVER span for the policy search, carrying the query and the documents it returned. The MCP SDK's built-in tracing middleware is replaced, because it reads request metadata the CLI does not send.

## query(): one-off tasks

```bash
python runner.py
```

One `query()` call per ticket. The SDK starts the CLI inside the runner's root span and hands it the active trace context, so everything for a ticket lands in one trace across two services:

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
- One small helper call per ticket arrives as a one-span trace of its own; it counts toward usage and is never a task.

## ClaudeSDKClient: continuous conversations

```bash
python runner_client.py
```

One `ClaudeSDKClient` session; every ticket is a turn of it. The CLI starts once, at `connect()`, before any turn's span exists, so each ticket becomes two traces that share the session id:

```
trace 1, the turn
CHAIN  triage_ticket                                       the job: instruction + ticket in, the note out
  AGENT  ClaudeAgentSDK.ClaudeSDKClient.receive_response   the run: ask, answer, replies, run totals
    TOOL  ToolSearch                                       the hook's records, one per call
    TOOL  mcp__helpdesk__lookup_customer
    ...   get_order, get_shipment, list_charges, search_policies, record_resolution

trace 2, the CLI's turn, same session id
claude_code.interaction
  LLM  claude_code.llm_request                             model calls, with tokens
  claude_code.tool lookup_customer                         the CLI's record of each call
    claude_code.tool.execution
      TOOL  lookup_customer                                served by the MCP server, second service
  ...
  RETRIEVER  search_policies                               under its call, with the documents
```

- The business task comes from the turn's trace, one per ticket, named from the instruction.
- The CLI's trace carries the model calls and the MCP server's spans; it counts toward usage and is never a task of its own.
- One helper call per session, not per ticket, arrives as a one-span trace.
- The turns of one session share a session id in Lumoz.

## Limits

- The messages the model received and the tool definitions it was offered never leave the CLI, so no span carries them. Signals that need them do not apply.
- Without the CLI's telemetry there are no model-call spans: the run's token totals sit on the agent span, and the Models tab stays empty.
