"""Lumoz tracing for a Claude Agent SDK app: two things, switched on once.

1. The OpenInference Claude Agent SDK instrumentor, in this process. It records
   each run (the ask, the answer, every reply), each tool call through the SDK's
   hooks, and sub-agents.
2. The Claude Code CLI's own telemetry, pointed at the same Lumoz endpoint. The
   SDK starts the CLI as a subprocess and copies this process's environment into
   it, so setting the CLI's variables here is enough. Its spans carry what the
   instrumentor cannot see: one span per model call, with that call's tokens.

Call setup() before anything imports claude_agent_sdk: the instrumentor patches
the SDK at import time.

Reads from .env (or the environment):
    LUMOZ_API_KEY       client_id:client_secret for the Lumoz OTLP proxy
    OTEL_ENDPOINT       the proxy's traces endpoint
    LUMOZ_ENVIRONMENT   deployment.environment on every span (default: development)
Without LUMOZ_API_KEY and OTEL_ENDPOINT, tracing is skipped and the app runs as is.
To run with the instrumentor alone, set CLAUDE_CODE_ENABLE_TELEMETRY=0.
"""

import atexit
import base64
import os
import pathlib

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")

ENVIRONMENT = os.environ.get("LUMOZ_ENVIRONMENT", "development")
_PROVIDER = None


def enable_cli_telemetry(service_name: str, endpoint: str, token: str) -> None:
    """Switch on the Claude Code CLI's tracer through the environment the SDK hands it.

    Every setting is a default: a value already in the environment, or passed through
    ClaudeAgentOptions.env, wins. Metrics and logs stay off, and so do the CLI's content
    flags, since the instrumentor's spans already carry the prompt and the replies.
    """
    for key, value in {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_HEADERS": f"authorization=Basic {token}",
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_RESOURCE_ATTRIBUTES": f"deployment.environment={ENVIRONMENT}",
    }.items():
        os.environ.setdefault(key, value)


def setup(service_name: str):
    """Install the instrumentor and switch on the CLI's telemetry. Returns the provider, or None.

    Safe to call more than once: the second call returns what the first one made.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    endpoint, api_key = os.environ.get("OTEL_ENDPOINT"), os.environ.get("LUMOZ_API_KEY")
    if not endpoint or not api_key:
        print("[lumoz] LUMOZ_API_KEY or OTEL_ENDPOINT not set: tracing skipped")
        return None

    from openinference.instrumentation.claude_agent_sdk import ClaudeAgentSDKInstrumentor
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk import trace as trace_sdk
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    token = base64.b64encode(api_key.encode()).decode()
    provider = trace_sdk.TracerProvider(
        resource=Resource.create({"service.name": service_name, "deployment.environment": ENVIRONMENT})
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers={"authorization": f"Basic {token}"}))
    )
    trace.set_tracer_provider(provider)
    ClaudeAgentSDKInstrumentor().instrument(tracer_provider=provider)
    enable_cli_telemetry(service_name, endpoint, token)
    atexit.register(provider.shutdown)
    print(f"[lumoz] {service_name} -> {endpoint} (instrumentor and Claude Code CLI telemetry)")
    _PROVIDER = provider
    return provider
