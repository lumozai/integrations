"""A ticket runner: a backend job with a runner, the Claude Code CLI and an MCP server over HTTP.

No person asks anything. Support tickets arrive from a queue (here, tickets.json),
and for each one the runner has a Claude Agent SDK agent check the claim against
the helpdesk records, look up the applicable policy, decide the resolution and
record it. Three processes: this runner, the Claude Code CLI the SDK starts, and
the helpdesk MCP server (mcp_server.py), which the runner starts on port 8765
unless HELPDESK_MCP_URL names one already running.

Lumoz shows one trace per ticket. The runner opens one root span per ticket with
the instruction and the ticket as input.value and the agent's note as
output.value; the business task is named from that instruction. The root marks
the job's boundary: it matters when a job is more than one query() call or has
work around the agent, and a single-call job works without it, since the
instrumentor's own root span carries the ask and the answer. The agent's run, its
model calls and its tool calls nest under the root. The MCP server receives the
trace context on each request and exports its own spans under its own service
name, so the trace spans two services.

    python runner.py
"""

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

import anyio
from opentelemetry import trace

import lumoz_tracing

lumoz_tracing.setup("claude-agent-sdk-ticket-triage")   # before claude_agent_sdk is imported

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
MCP_URL = os.environ.get("HELPDESK_MCP_URL", "http://127.0.0.1:8765/mcp")
TOOLS = [
    "mcp__helpdesk__lookup_customer",
    "mcp__helpdesk__get_order",
    "mcp__helpdesk__get_shipment",
    "mcp__helpdesk__list_charges",
    "mcp__helpdesk__search_policies",
    "mcp__helpdesk__record_resolution",
]
INSTRUCTION = (
    "Triage this support ticket with the helpdesk tools: identify the customer and the order, verify "
    "the claim against the records, look up the applicable policy with search_policies, decide the "
    "resolution (replace, refund, escalate or no_action), record it with record_resolution, then reply "
    "with a note to the customer of at most 80 words."
)
tracer = trace.get_tracer("ticket-triage")


async def triage(ticket: dict) -> str:
    """One job: one root span carrying the ask and the note, the agent's run nested under it."""
    prompt = f"{INSTRUCTION}\n\nTicket:\n{json.dumps(ticket, indent=2)}"
    with tracer.start_as_current_span("triage_ticket") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", prompt)
        options = ClaudeAgentOptions(
            mcp_servers={"helpdesk": {"type": "http", "url": MCP_URL}},
            allowed_tools=TOOLS,
            max_turns=12,
        )
        note = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        print(f"  agent: {block.text.strip()[:160]}")
            elif isinstance(message, ResultMessage):
                note = message.result or ""
                print(f"  turns={message.num_turns} cost=${message.total_cost_usd or 0:.4f} error={message.is_error}")
        root.set_attribute("output.value", note)
        return note


def start_helpdesk_server() -> subprocess.Popen | None:
    """Start the helpdesk MCP server unless HELPDESK_MCP_URL points at one already running."""
    if "HELPDESK_MCP_URL" in os.environ:
        return None
    port = MCP_URL.rsplit(":", 1)[1].split("/", 1)[0]
    # The server exports under its own service name; keep the CLI's telemetry settings out of it.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDE_CODE_", "OTEL_EXPORTER_", "TRACEPARENT", "TRACESTATE"))
           and k not in ("OTEL_SERVICE_NAME", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_TRACES_EXPORTER", "OTEL_METRICS_EXPORTER", "OTEL_LOGS_EXPORTER")}
    proc = subprocess.Popen([sys.executable, str(HERE / "mcp_server.py"), "--port", port], env=env)
    for _ in range(50):
        try:
            urllib.request.urlopen(MCP_URL, timeout=1)
        except urllib.error.HTTPError:
            return proc          # any HTTP answer means the server is up (GET is not an MCP method)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.3)
    proc.terminate()
    raise RuntimeError(f"helpdesk MCP server did not come up at {MCP_URL}")


async def main() -> None:
    tickets = json.loads((HERE / "tickets.json").read_text())
    server = start_helpdesk_server()
    try:
        for ticket in tickets:                     # the queue; a real runner polls one
            print(f"\n=== {ticket['id']} {ticket['subject']} ({ticket['channel']}) ===")
            note = await triage(ticket)
            print(f"  note: {note[:500]}")
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    anyio.run(main)
