"""The ticket runner on ClaudeSDKClient: one session, each ticket a turn.

The same job as runner.py, on the SDK's other path. One ClaudeSDKClient is
connected once; every ticket from the queue is sent as a turn of that session,
and the agent answers each with the same helpdesk tools over HTTP.

Lumoz shows one trace per turn, rooted on the runner's span for that ticket,
and the turns share a session id. The Claude Code CLI starts once, at
connect(), before any turn's span exists, so with its telemetry on its spans
arrive as traces of their own that share the session id; the MCP server's
spans join those, since the CLI is the caller.

    python runner_client.py
"""

import json
import os
import pathlib

import anyio
from opentelemetry import trace

import lumoz_tracing

lumoz_tracing.setup("claude-agent-sdk-ticket-triage")   # before claude_agent_sdk is imported

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, TextBlock  # noqa: E402

from runner import INSTRUCTION, MCP_URL, TOOLS, start_helpdesk_server  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
tracer = trace.get_tracer("ticket-triage")


async def triage_turn(client: ClaudeSDKClient, ticket: dict) -> str:
    """One turn of the session: one root span carrying the ask and the note."""
    prompt = f"{INSTRUCTION}\n\nTicket:\n{json.dumps(ticket, indent=2)}"
    with tracer.start_as_current_span("triage_ticket") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", prompt)
        await client.query(prompt)
        note = ""
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(f"  agent: {block.text.strip()[:160]}")
            elif isinstance(message, ResultMessage):
                note = message.result or ""
                print(f"  turns={message.num_turns} cost=${message.total_cost_usd or 0:.4f} session={message.session_id[:8]}")
        root.set_attribute("output.value", note)
        return note


async def main() -> None:
    tickets = json.loads((HERE / "tickets.json").read_text())
    server = start_helpdesk_server()
    options = ClaudeAgentOptions(
        mcp_servers={"helpdesk": {"type": "http", "url": MCP_URL}},
        allowed_tools=TOOLS,
        max_turns=12,
    )
    try:
        async with ClaudeSDKClient(options=options) as client:      # one session for the queue
            for ticket in tickets:
                print(f"\n=== {ticket['id']} {ticket['subject']} ({ticket['channel']}) ===")
                note = await triage_turn(client, ticket)
                print(f"  note: {note[:300]}")
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    anyio.run(main)
