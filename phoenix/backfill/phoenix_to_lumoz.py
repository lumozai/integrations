#!/usr/bin/env python3
"""Backfill spans from a Phoenix project into Lumoz.

Reads spans from Phoenix (REST: GET /v1/projects/{project}/spans), restores
them to the OTLP form that was originally ingested, and POSTs them to the Lumoz
OTLP/HTTP endpoint. A watermark is kept per (phoenix project, app, env), so
re-runs only move new spans.

Restoring the raw span reuses Phoenix's own code:
  * Phoenix stores attributes nested (unflatten + load_json_strings at ingest);
    the REST listing flattens them again and drops openinference.span.kind.
    `unflatten` + `encode_span_to_otlp` (phoenix.trace.otel) is the inverse of
    Phoenix's `decode_otlp_span`: nested -> flat OTLP KeyValues, with
    metadata / tool.parameters / document.metadata / prompt template variables
    re-serialized as JSON strings.

Usage:
  python phoenix_to_lumoz.py            # keys from .env (or the environment); prompts for the rest
  python phoenix_to_lumoz.py --project my-project --app my-app --env prod --days 7
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans

from phoenix.trace.attributes import load_json_strings, unflatten
from phoenix.trace.otel import encode_span_to_otlp
from phoenix.trace.schemas import Span, SpanContext, SpanEvent, SpanKind, SpanStatusCode

DEFAULT_PHOENIX_URL = "http://localhost:6006"
DEFAULT_LUMOZ_ENDPOINT = "https://api.lumoz.ai/proxy/v1/traces"
DEFAULT_STATE_FILE = Path.home() / ".lumoz" / "phoenix_backfill_state.json"
DEFAULT_DAYS = 30
DEFAULT_ENV = "phoenix"  # Lumoz environment when none is given
PAGE_SIZE = 500  # spans per Phoenix request (API max is 1000)
BATCH_SIZE = 500  # max spans per Lumoz request
MAX_BATCH_BYTES = 3 * 1024 * 1024  # starting size; a 413 ("Request Too Long") halves the batch and retries
DEFAULT_SCOPE = "phoenix-backfill"
# Frameworks whose instrumentation scope isn't "openinference.instrumentation.<label>".
_DIRECT_SCOPES = {
    "vercel_ai": "ai",
    "mastra": "@mastra/otel-exporter",
    "google_adk": "gcp.vertex.agent",
    "crewai": "crewai.telemetry",
    "livekit": "livekit-agents",
    "mcp": "mcp-python-sdk",
    "a2a": "a2a-python-sdk",
    "fastapi": "fastapi",
}
# Instrumentation packages in github.com/Arize-ai/openinference (python/instrumentation and
# js/packages), named with underscores, plus the frameworks above.
OPENINFERENCE_FRAMEWORKS = frozenset(
    """ag2 agent_framework agentspec agno anthropic autogen autogen_agentchat bedrock
    bedrock_agent_runtime beeai claude_agent_sdk cohere crewai dspy google_adk google_genai groq
    guardrails haystack instructor langchain langchain_v0 litellm llama_index mcp mistralai ollama
    openai openai_agents openlit openllmetry pipecat portkey promptflow pydantic_ai smolagents
    strands_agents together typesafe vertexai""".split()
)
KNOWN_FRAMEWORKS = sorted(OPENINFERENCE_FRAMEWORKS | _DIRECT_SCOPES.keys())
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Phoenix side
# --------------------------------------------------------------------------- #
def phoenix_client(url: str) -> httpx.Client:
    headers = {}
    if key := os.environ.get("PHOENIX_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    return httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=60)


def list_projects(client: httpx.Client) -> list[str]:
    names, cursor = [], None
    while True:
        r = client.get("/v1/projects", params={"cursor": cursor} if cursor else None)
        r.raise_for_status()
        body = r.json()
        names += [p["name"] for p in body["data"]]
        cursor = body.get("next_cursor")
        if not cursor:
            return names


def server_sorts_spans(client: httpx.Client) -> bool:
    """True if this Phoenix's getSpans accepts sort/order. Older servers ignore them silently."""
    try:
        spec = client.get("/openapi.json").json()
        params = spec["paths"]["/v1/projects/{project_identifier}/spans"]["get"]["parameters"]
        return {"sort", "order"} <= {p["name"] for p in params}
    except Exception:
        return False


def _iter_pages(
    client: httpx.Client, project: str, start: datetime, end: datetime, sorted_by_server: bool
) -> Iterator[dict[str, Any]]:
    cursor = None
    while True:
        params: dict[str, Any] = {
            "limit": PAGE_SIZE,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }
        if sorted_by_server:
            params.update(sort="start_time", order="asc")
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"/v1/projects/{quote(project, safe='')}/spans", params=params)
        r.raise_for_status()
        body = r.json()
        yield from body["data"]
        cursor = body.get("next_cursor")
        if not cursor:
            return


def iter_phoenix_spans(
    client: httpx.Client, project: str, start: datetime, end: datetime
) -> Iterator[dict[str, Any]]:
    """Spans with start <= start_time < end, oldest first (start_time, then span_id).

    The watermark needs this order. A Phoenix without sort/order support returns newest-first, so
    the window is read fully and sorted here (holds the window's spans in memory).
    """
    if server_sorts_spans(client):
        yield from _iter_pages(client, project, start, end, True)
        return
    print("  note: this Phoenix can't sort spans server-side; reading the window into memory first")
    spans = list(_iter_pages(client, project, start, end, False))
    spans.sort(key=lambda s: (_dt(s["start_time"]), s["context"]["span_id"]))
    yield from spans


# --------------------------------------------------------------------------- #
# Restore raw OTLP span
# --------------------------------------------------------------------------- #
def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _unix_nano(dt: datetime) -> int:
    # exact integer math; Span.timestamp() * 1e9 in encode_span_to_otlp is float
    return ((dt - _EPOCH) // timedelta(microseconds=1)) * 1000


def to_otlp_span(api_span: dict[str, Any]):
    """REST span -> OTLP Span proto, undoing the REST flattening."""
    attributes = unflatten(load_json_strings(api_span["attributes"].items()))
    start, end = _dt(api_span["start_time"]), _dt(api_span["end_time"])
    span = Span(
        name=api_span["name"],
        context=SpanContext(
            trace_id=api_span["context"]["trace_id"], span_id=api_span["context"]["span_id"]
        ),
        span_kind=SpanKind(api_span["span_kind"]),
        parent_id=api_span.get("parent_id"),
        start_time=start,
        end_time=end,
        status_code=SpanStatusCode(api_span["status_code"]),
        status_message=api_span.get("status_message") or "",
        attributes=attributes,
        events=[
            SpanEvent(
                name=e["name"], timestamp=_dt(e["timestamp"]), attributes=e.get("attributes") or {}
            )
            for e in api_span.get("events", [])
        ],
        conversation=None,
    )
    otlp = encode_span_to_otlp(span)
    otlp.start_time_unix_nano = _unix_nano(start)
    otlp.end_time_unix_nano = _unix_nano(end)
    for pb, ev in zip(otlp.events, span.events):
        pb.time_unix_nano = _unix_nano(ev.timestamp)
    return otlp


def _kv(key: str, value: str) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(string_value=value))


def normalize_framework(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def validate_framework(value: str) -> str:
    """Normalized label if known; otherwise exit with close matches."""
    label = normalize_framework(value)
    if label in KNOWN_FRAMEWORKS:
        return label
    close = difflib.get_close_matches(label, KNOWN_FRAMEWORKS, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    sys.exit(
        f"Unknown framework '{value}'.{hint}\n"
        "Run with --list-frameworks to see valid names, or --allow-unknown-framework to use it as-is."
    )


def print_frameworks() -> None:
    print("Valid --framework names (OpenInference instrumentations + Lumoz-recognised libraries):")
    width = max(map(len, KNOWN_FRAMEWORKS)) + 2
    cols = 4
    for i in range(0, len(KNOWN_FRAMEWORKS), cols):
        print("  " + "".join(n.ljust(width) for n in KNOWN_FRAMEWORKS[i : i + cols]))


def choose_framework() -> Optional[str]:
    print_frameworks()
    while True:
        choice = input("Framework that emitted these spans (name, or Enter to skip): ").strip()
        if not choice:
            return None
        label = normalize_framework(choice)
        if label in KNOWN_FRAMEWORKS:
            return label
        close = difflib.get_close_matches(label, KNOWN_FRAMEWORKS, n=3, cutoff=0.5)
        print(f"  '{choice}' is not a known framework." + (f" Did you mean: {', '.join(close)}?" if close else ""))


def framework_scope(framework: Optional[str]) -> str:
    """Instrumentation scope name that identifies `framework` (e.g. langchain) in Lumoz."""
    if not framework:
        return DEFAULT_SCOPE
    label = normalize_framework(framework)
    return _DIRECT_SCOPES.get(label, f"openinference.instrumentation.{label}")


def build_request(
    otlp_spans: list, project: str, app: str, env: str, scope: str = DEFAULT_SCOPE
) -> ExportTraceServiceRequest:
    resource_spans = ResourceSpans()
    resource_spans.resource.attributes.extend(
        [
            _kv("service.name", app),
            _kv("deployment.environment", env),
            _kv("project.name", project),
            _kv("openinference.project.name", project),
            _kv("backfill.source", "phoenix"),
        ]
    )
    scope_spans = ScopeSpans()
    scope_spans.scope.name = scope
    scope_spans.spans.extend(otlp_spans)
    resource_spans.scope_spans.append(scope_spans)
    return ExportTraceServiceRequest(resource_spans=[resource_spans])


# --------------------------------------------------------------------------- #
# Lumoz side
# --------------------------------------------------------------------------- #
class PayloadTooLarge(Exception):
    pass


def send_to_lumoz(req: ExportTraceServiceRequest, endpoint: str, api_key: str) -> None:
    body = req.SerializeToString()  # send uncompressed: the ingest endpoint does not accept gzip
    headers = {
        "Content-Type": "application/x-protobuf",
        "Authorization": "Basic " + base64.b64encode(api_key.encode()).decode(),
    }
    for attempt in range(5):
        try:
            r = httpx.post(endpoint, content=body, headers=headers, timeout=60)
            if r.status_code < 300:
                return
            if r.status_code == 413:
                raise PayloadTooLarge(r.text[:200])
            if r.status_code < 500 and r.status_code != 429:
                raise SystemExit(f"Lumoz rejected batch: {r.status_code} {r.text[:300]}")
        except httpx.TransportError:
            pass
        time.sleep(2**attempt)
    raise SystemExit("Lumoz unreachable / failing after 5 attempts; watermark not advanced")


def send_batch(spans: list, project: str, app: str, env: str, scope: str, endpoint: str, api_key: str) -> None:
    """Send spans; on 413 split the batch in half and send each half."""
    try:
        send_to_lumoz(build_request(spans, project, app, env, scope), endpoint, api_key)
    except PayloadTooLarge as e:
        if len(spans) == 1:
            raise SystemExit(f"Lumoz rejects even a single span ({spans[0].ByteSize()} bytes): {e}")
        mid = len(spans) // 2
        print(f"  413 on {len(spans)} spans ({sum(x.ByteSize() for x in spans)} bytes); splitting")
        send_batch(spans[:mid], project, app, env, scope, endpoint, api_key)
        send_batch(spans[mid:], project, app, env, scope, endpoint, api_key)


# --------------------------------------------------------------------------- #
# Watermark: per (phoenix project, app, env)
# --------------------------------------------------------------------------- #
def _key(project: str, app: str, env: str) -> str:
    return json.dumps([project, app, env])


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def prompt(label: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip() or (default or "")
        if value:
            return value


def confirm(question: str, default: bool = True) -> bool:
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def choose_days(default: float) -> float:
    while True:
        raw = prompt("Days of history to read", f"{default:g}")
        try:
            if (days := float(raw)) > 0:
                return days
        except ValueError:
            pass
        print("  enter a positive number")


def choose_project(client: httpx.Client) -> str:
    names = list_projects(client)
    print("Phoenix projects:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    choice = prompt("Phoenix project (number or name)")
    return names[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(names) else choice


def main() -> None:
    # .env next to this script; variables already set in the environment take precedence
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--phoenix-url", default=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", DEFAULT_PHOENIX_URL))
    ap.add_argument("--project", help="Phoenix project name (prompted if omitted)")
    ap.add_argument("--app", help="Lumoz app / service.name (prompted if omitted; default: the Phoenix project name)")
    ap.add_argument("--env", help=f"Lumoz environment (prompted if omitted; default {DEFAULT_ENV})")
    ap.add_argument("--days", type=float, help=f"how far back to read (prompted if omitted; default {DEFAULT_DAYS})")
    ap.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--framework", help="library that emitted the spans (e.g. langchain, openai, google_adk); "
                    "sets the instrumentation scope so Lumoz shows the framework. Validated; see --list-frameworks. "
                    "Default: prompted when interactive, else left blank")
    ap.add_argument("--list-frameworks", action="store_true", help="print valid --framework names and exit")
    ap.add_argument("--allow-unknown-framework", action="store_true", help="accept a --framework not in the list")
    ap.add_argument("--endpoint", default=os.environ.get("OTEL_ENDPOINT", DEFAULT_LUMOZ_ENDPOINT))
    ap.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    ap.add_argument("--ignore-watermark", action="store_true", help="re-read the full --days window")
    ap.add_argument("--dry-run", action="store_true", help="read and convert, send nothing, keep watermark")
    args = ap.parse_args()
    if args.list_frameworks:
        print_frameworks()
        return
    if args.framework and not args.allow_unknown_framework:
        args.framework = validate_framework(args.framework)

    api_key = os.environ.get("LUMOZ_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("Set LUMOZ_API_KEY")

    if not (args.project and args.app and args.env) and not sys.stdin.isatty():
        sys.exit("Not an interactive terminal: pass --project, --app and --env.")

    client = phoenix_client(args.phoenix_url)
    project = args.project or choose_project(client)
    app = args.app or prompt("Lumoz app", project)  # defaults to the Phoenix project name
    env = args.env or prompt("Lumoz env", DEFAULT_ENV)

    interactive = not (args.project and args.app and args.env)  # anything prompted for => ask the rest
    if args.framework is None and interactive:
        args.framework = choose_framework()  # offer the list

    now = datetime.now(timezone.utc)
    state = load_state(args.state_file)
    entry = state.get(_key(project, app, env), {})
    if args.days is None:
        args.days = choose_days(DEFAULT_DAYS) if interactive else DEFAULT_DAYS
    if entry and not args.ignore_watermark and interactive:
        print(f"Last run for this project/app/env reached {entry['watermark']}.")
        args.ignore_watermark = not confirm("Resume from there?")
    window_start = now - timedelta(days=args.days)
    start, seen = window_start, set()
    if entry and not args.ignore_watermark:
        wm = _dt(entry["watermark"])
        if wm > window_start:
            # start_time is inclusive; spans already sent at exactly wm are skipped by id
            start, seen = wm, set(entry.get("span_ids_at_watermark", []))
    print(f"{project} -> app={app} env={env}: reading {start.isoformat()} .. {now.isoformat()}")

    def new_spans() -> Iterator[dict[str, Any]]:
        for api_span in iter_phoenix_spans(client, project, start, now):
            if _dt(api_span["start_time"]) == start and api_span["context"]["span_id"] in seen:
                continue
            yield api_span

    if interactive and not args.yes and not args.dry_run:
        total = sum(1 for _ in new_spans())
        if total == 0:
            print("Nothing new to send.")
            return
        if not confirm(f"Send {total} spans from '{project}' to Lumoz app={app} env={env}?"):
            print("Cancelled.")
            return

    scope = framework_scope(args.framework)
    sent = 0
    batch: list = []
    batch_bytes = 0
    last_ts: Optional[str] = None
    ids_at_last: set[str] = set()

    def flush() -> None:
        nonlocal sent, batch, batch_bytes
        if not batch:
            return
        if not args.dry_run:
            send_batch(batch, project, app, env, scope, args.endpoint, api_key)  # type: ignore[arg-type]
            state[_key(project, app, env)] = {
                "watermark": last_ts,
                "span_ids_at_watermark": sorted(ids_at_last),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(args.state_file, state)
        sent += len(batch)
        batch, batch_bytes = [], 0
        print(f"  {'would send' if args.dry_run else 'sent'} {sent} spans (through {last_ts})")

    for api_span in new_spans():
        otlp_span = to_otlp_span(api_span)
        size = otlp_span.ByteSize()
        if batch and batch_bytes + size > MAX_BATCH_BYTES:
            flush()  # flush the spans before this one; last_ts/ids_at_last still describe them
        # a span bigger than the cap still goes (alone); send_batch fails clearly if Lumoz refuses it
        ts = api_span["start_time"]
        if ts != last_ts:
            last_ts, ids_at_last = ts, set()
        ids_at_last.add(api_span["context"]["span_id"])
        # a tie group may straddle a flush; carry the earlier ids of this ts forward
        if entry and last_ts == entry.get("watermark") and not args.ignore_watermark:
            ids_at_last |= seen
        batch.append(otlp_span)
        batch_bytes += size
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()
    print(f"Done: {sent} spans {'(dry run)' if args.dry_run else 'sent'}.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as e:
        sys.exit(f"HTTP {e.response.status_code} from {e.request.url.copy_with(query=None)}: {e.response.text[:200]}")
    except httpx.TransportError as e:
        sys.exit(f"Cannot reach {e.request.url.copy_with(query=None)}: {e}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Progress up to the last sent batch is saved.")
