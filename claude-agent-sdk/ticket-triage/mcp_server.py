"""The helpdesk MCP server the ticket runner's agent calls over HTTP.

Four lookups over canned customer, order, shipment and charge records, a policy
search over a small knowledge base, and one write: the shape of a small internal
tools service. It runs as its own process and exports its own spans to Lumoz
under its own service name.

With its telemetry on, the Claude Code CLI sends a W3C traceparent header on
every HTTP request it makes to an MCP server. The middleware below reads that
header and starts one span per tool call on it, so the server's work shows under
the job that called it, as a second service in the same trace. The policy search
is a RETRIEVER span carrying the documents it returned; the rest are TOOL spans.
The MCP SDK's own tracing middleware is replaced: it parents on request metadata
(_meta) that the CLI does not send, so its spans would be roots of their own.

    python mcp_server.py --port 8765

Reads LUMOZ_API_KEY, OTEL_ENDPOINT and LUMOZ_ENVIRONMENT from .env or the
environment. Without the first two, the server runs and exports nothing.
"""

import argparse
import atexit
import base64
import json
import os
import pathlib
from typing import Any

from dotenv import load_dotenv
from mcp.server._otel import OpenTelemetryMiddleware
from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk import trace as trace_sdk
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import StatusCode

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")

SERVICE_NAME = "claude-agent-sdk-helpdesk-mcp"
ENVIRONMENT = os.environ.get("LUMOZ_ENVIRONMENT", "development")

# Canned records, so every ticket has facts to check against.
CUSTOMERS = {
    "maya.lindqvist@example.com": {"customer_id": "C-2201", "name": "Maya Lindqvist", "tier": "gold", "orders_last_year": 14, "open_tickets": 0},
    "devon.okafor@example.com": {"customer_id": "C-3178", "name": "Devon Okafor", "tier": "standard", "orders_last_year": 2, "open_tickets": 0},
}
ORDERS = {
    "A-10422": {"customer_id": "C-2201", "placed_at": "2026-11-14T16:20:00Z", "items": [{"sku": "MON-27-4K", "name": "27-inch 4K monitor", "qty": 1, "price_usd": 349.00}], "total_usd": 349.00, "status": "delivered", "return_window_days": 30},
    "A-10457": {"customer_id": "C-3178", "placed_at": "2026-11-18T19:05:00Z", "items": [{"sku": "KB-MECH-75", "name": "75% mechanical keyboard", "qty": 1, "price_usd": 89.00}], "total_usd": 89.00, "status": "processing", "return_window_days": 30},
}
SHIPMENTS = {
    "A-10422": {"carrier": "UPS", "tracking": "1Z999AA10123456784", "shipped_at": "2026-11-15T11:00:00Z", "delivered_at": "2026-11-18T14:32:00Z", "carrier_notes": ["package scanned with visible crush damage at destination hub"], "replacement_stock": {"MON-27-4K": 12, "ships_within_days": 2}},
    "A-10457": {"carrier": "USPS", "tracking": "", "shipped_at": "", "delivered_at": "", "carrier_notes": [], "replacement_stock": {}},
}
CHARGES = {
    "A-10422": [{"charge_id": "ch_7a1", "amount_usd": 349.00, "status": "captured", "at": "2026-11-14T16:20:05Z"}],
    "A-10457": [
        {"charge_id": "ch_9c4", "amount_usd": 89.00, "status": "captured", "at": "2026-11-18T19:05:08Z"},
        {"charge_id": "ch_9c5", "amount_usd": 89.00, "status": "captured", "at": "2026-11-18T19:05:31Z", "note": "gateway retry after a timeout on ch_9c4; duplicate capture"},
    ],
}
POLICIES = [
    {"id": "pol-damaged-01", "title": "Damaged on arrival",
     "content": "If the carrier's scans or the customer's photos show transit damage, replace the item at no cost when stock allows, otherwise refund in full. No return is needed for items under $500."},
    {"id": "pol-duplicate-charge-02", "title": "Duplicate charges",
     "content": "When an order shows more captured charges than its total, refund every duplicate capture to the original card within one business day and tell the customer the refund takes 5 to 10 days to appear."},
    {"id": "pol-returns-03", "title": "Returns and return window",
     "content": "Unopened items may be returned within 30 days of delivery for a full refund. Opened items are refunded minus a 15 percent restocking fee unless they are defective."},
    {"id": "pol-escalation-04", "title": "When to escalate",
     "content": "Escalate to a human agent when the records contradict the customer's claim, when the amount exceeds $1,000, or when the customer threatens a chargeback."},
    {"id": "pol-gold-05", "title": "Gold tier",
     "content": "Gold tier customers get expedited replacement shipping at no cost and may keep a damaged item instead of returning it."},
]
RETRIEVER_TOOLS = {"search_policies"}
RESOLUTIONS: dict[str, dict] = {}

server = MCPServer("helpdesk", instructions="Customer, order, shipment, charge and policy lookups for support tickets.")


@server.tool()
def lookup_customer(email: str) -> dict:
    """The customer record behind an email address."""
    return CUSTOMERS.get(email.lower(), {"error": "no customer with that email"})


@server.tool()
def get_order(order_id: str) -> dict:
    """An order: items, total, status and return window."""
    return ORDERS.get(order_id, {"error": "no such order"})


@server.tool()
def get_shipment(order_id: str) -> dict:
    """An order's shipment: carrier, scans, delivery time and replacement stock."""
    return SHIPMENTS.get(order_id, {"error": "no shipment for that order"})


@server.tool()
def list_charges(order_id: str) -> list[dict]:
    """Every card charge taken for an order."""
    return CHARGES.get(order_id, [])


@server.tool()
def search_policies(query: str, top_k: int = 3) -> list[dict]:
    """The support policies most relevant to a query, best match first, with a score."""
    words = {w for w in query.lower().split() if len(w) > 3}
    scored = []
    for policy in POLICIES:
        text = f"{policy['title']} {policy['content']}".lower()
        score = sum(1 for w in words if w in text) / (len(words) or 1)
        if score > 0:
            scored.append({**policy, "score": round(score, 3)})
    scored.sort(key=lambda p: p["score"], reverse=True)
    return scored[:top_k]


@server.tool()
def record_resolution(ticket_id: str, action: str, note: str) -> dict:
    """File the resolution for a ticket: action is one of replace, refund, escalate, no_action."""
    RESOLUTIONS[ticket_id] = {"action": action, "note": note}
    return {"recorded": True, "ticket_id": ticket_id, "action": action}


def _tool_result(payload: Any) -> Any:
    """The value a tool returned, out of the MCP result envelope."""
    if isinstance(payload, dict):
        structured = payload.get("structuredContent")
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        for item in payload.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    return json.loads(item["text"])
                except (ValueError, TypeError):
                    return item["text"]
    return payload


class LumozToolSpans(ServerMiddleware):
    """One span per tools/call, parented on the caller's traceparent header when it sends one:
    a RETRIEVER span carrying its documents for a search, a TOOL span for everything else."""

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer

    async def __call__(self, ctx: ServerRequestContext, call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call" or not ctx.params:
            return await call_next(ctx)
        headers = {k.lower(): v for k, v in (getattr(ctx.request, "headers", None) or {}).items()}
        parent = propagate.extract(headers) if "traceparent" in headers else None
        name = str(ctx.params.get("name") or "tools/call")
        arguments = ctx.params.get("arguments") or {}
        retriever = name in RETRIEVER_TOOLS
        attributes = {
            "openinference.span.kind": "RETRIEVER" if retriever else "TOOL",
            "tool.name": name,
            "tool.parameters": json.dumps(arguments),
            "input.value": str(arguments.get("query", "")) if retriever else json.dumps(arguments),
            "input.mime_type": "text/plain" if retriever else "application/json",
        }
        with self._tracer.start_as_current_span(name, context=parent, attributes=attributes) as span:
            try:
                result = await call_next(ctx)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise
            payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            value = _tool_result(payload)
            if retriever and isinstance(value, list):
                for i, doc in enumerate(d for d in value if isinstance(d, dict)):
                    span.set_attribute(f"retrieval.documents.{i}.document.id", str(doc.get("id", i)))
                    span.set_attribute(f"retrieval.documents.{i}.document.content", str(doc.get("content", "")))
                    span.set_attribute(f"retrieval.documents.{i}.document.score", float(doc.get("score", 0.0)))
                    span.set_attribute(f"retrieval.documents.{i}.document.metadata", json.dumps({"title": doc.get("title", "")}))
            span.set_attribute("output.value", json.dumps(value)[:4000])
            span.set_attribute("output.mime_type", "application/json")
            return result


def setup_tracing() -> trace.Tracer:
    provider = trace_sdk.TracerProvider(
        resource=Resource.create({"service.name": SERVICE_NAME, "deployment.environment": ENVIRONMENT})
    )
    endpoint, api_key = os.environ.get("OTEL_ENDPOINT"), os.environ.get("LUMOZ_API_KEY")
    if endpoint and api_key:
        token = base64.b64encode(api_key.encode()).decode()
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers={"authorization": f"Basic {token}"})))
        print(f"[helpdesk] {SERVICE_NAME} -> {endpoint}", flush=True)
    else:
        print("[helpdesk] LUMOZ_API_KEY or OTEL_ENDPOINT not set: tracing skipped", flush=True)
    atexit.register(provider.shutdown)
    return provider.get_tracer("helpdesk-mcp")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    tracer = setup_tracing()
    server.middleware[:] = [m for m in server.middleware if not isinstance(m, OpenTelemetryMiddleware)]
    server.middleware.insert(0, LumozToolSpans(tracer))
    server.run(transport="streamable-http", host="127.0.0.1", port=args.port, stateless_http=True)


if __name__ == "__main__":
    main()
