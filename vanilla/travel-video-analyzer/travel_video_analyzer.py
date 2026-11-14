"""
Travel Video Analyzer - Framework-less Multi-Agent

Uses OpenInference decorators (@tracer.chain, @tracer.tool) with direct Anthropic API.
No LangGraph, LangChain, or other agent frameworks.

Pipeline: handle_request (root) → analyze_video → extract_frames → analyze_frames → generate_tags

The complete custom-instrumentation example for the Lumoz docs (Instrument > Custom
instrumentation): one root span per run carrying the request, the answer, the session and the
user; typed CHAIN / TOOL / LLM spans; failures recorded as an error status plus exception
attributes; the last batch flushed before exit.
"""

import base64
import json
import os
import re
import sys
import tempfile
import traceback
import uuid

import anthropic
import cv2
from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from openinference.instrumentation import OITracer
from openinference.instrumentation.config import TraceConfig
from openinference.instrumentation.anthropic import AnthropicInstrumentor

load_dotenv()

# ============================================================================
# Image Stripping Span Processor
# ============================================================================

# Patterns to match base64 image data in span attributes
BASE64_IMAGE_PATTERNS = [
    re.compile(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}'),  # data URI
    re.compile(r'/9j/[A-Za-z0-9+/=]{100,}'),  # JPEG magic bytes
    re.compile(r'iVBOR[A-Za-z0-9+/=]{100,}'),  # PNG magic bytes
]


class ImageStrippingSpanProcessor(BatchSpanProcessor):
    """Strips base64 image data from spans before export to prevent large payloads."""

    def __init__(self, span_exporter: SpanExporter, max_image_chars: int = 100):
        super().__init__(span_exporter)
        self.max_image_chars = max_image_chars

    def _strip_images_from_value(self, value):
        """Recursively strip base64 image data from a value."""
        if isinstance(value, str):
            result = value
            for pattern in BASE64_IMAGE_PATTERNS:
                result = pattern.sub(
                    lambda m: m.group(0)[:self.max_image_chars] + '...[IMAGE_TRUNCATED]',
                    result
                )
            return result
        elif isinstance(value, (list, tuple)):
            return type(value)(self._strip_images_from_value(v) for v in value)
        elif isinstance(value, dict):
            return {k: self._strip_images_from_value(v) for k, v in value.items()}
        return value

    def on_end(self, span: ReadableSpan) -> None:
        """Process span on end, stripping image data from attributes."""
        if span.attributes:
            modified_attrs = {}
            for key, value in span.attributes.items():
                modified_attrs[key] = self._strip_images_from_value(value)
            # Modify the internal attributes (this is a private API but necessary)
            if hasattr(span, '_attributes'):
                span._attributes = modified_attrs
        super().on_end(span)


# ============================================================================
# Tracing Setup
# ============================================================================

def _init_tracing():
    endpoint = os.environ.get("OTEL_ENDPOINT")
    api_key = os.environ.get("LUMOZ_API_KEY")
    if not endpoint or not api_key:
        raise ValueError("OTEL_ENDPOINT and LUMOZ_API_KEY required")

    # service.name is the application card in the Lumoz console; deployment.environment
    # is the environment the traces are filed under.
    resource = Resource.create({
        "service.name": "travel-video-analyzer-vanilla",
        "deployment.environment": os.environ.get("DEPLOYMENT_ENVIRONMENT", "development"),
    })
    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    headers = {"authorization": f"Basic {base64.b64encode(api_key.encode()).decode()}"}
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
    # Use ImageStrippingSpanProcessor to truncate base64 images before export
    # This prevents large payloads that cause timeouts/retries and duplicate spans
    provider.add_span_processor(ImageStrippingSpanProcessor(exporter))

    # Auto-instrument Anthropic client (must be before client creation). It records each model
    # call as an LLM span with llm.input_messages, llm.output_messages, llm.model_name and the
    # token counts. To record those by hand instead, see the Model calls section of the guide.
    AnthropicInstrumentor().instrument(tracer_provider=provider)

    print(f"Tracing → {endpoint}")
    return OITracer(trace.get_tracer(__name__), TraceConfig()), provider


tracer, tracer_provider = _init_tracing()
client = anthropic.Anthropic()  # Created AFTER instrumentation
MODEL = "claude-sonnet-4-20250514"

# The name Lumoz gives this run's business task. Anywhere in the root span's input.value.
WORKFLOW_TAG = "[Workflow: Analyze Travel Video]"


def fail(message: str) -> None:
    """Record a failure the way Lumoz reads it: an error status on the current span plus the
    exception as span attributes (a span event alone is not read), then raise so the spans
    above fail too."""
    span = trace.get_current_span()
    span.set_status(Status(StatusCode.ERROR, message))
    span.set_attribute("exception.type", "RuntimeError")
    span.set_attribute("exception.message", message)
    raise RuntimeError(message)


# ============================================================================
# Tools
# ============================================================================

@tracer.tool(name="extract_frames", description="Extract key frames from video using OpenCV")
def extract_frames(video_path: str, num_frames: int = 2) -> dict:
    """Extract key frames from a video."""
    print(f"\n[extract_frames] {video_path}")

    # Add custom attributes to the current span
    span = trace.get_current_span()
    span.set_attribute("video.path", video_path)
    span.set_attribute("video.requested_frames", num_frames)

    if not os.path.exists(video_path):
        fail(f"Video not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        fail(f"Could not open: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    temp_dir = tempfile.mkdtemp(prefix="frames_")
    frames = []

    for i, idx in enumerate([int(i * total / num_frames) for i in range(num_frames)]):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if ok:
            path = os.path.join(temp_dir, f"frame_{i:03d}.jpg")
            cv2.imwrite(path, frame)
            frames.append(path)

    cap.release()
    print(f"[extract_frames] → {len(frames)} frames")

    # Add attributes after processing
    duration = round(total / fps, 2) if fps > 0 else 0
    span.set_attribute("video.duration_seconds", duration)
    span.set_attribute("video.extracted_frames", len(frames))

    return {
        "success": True,
        "frame_paths": frames,
        "metadata": {
            "filename": os.path.basename(video_path),
            "duration_seconds": duration,
        }
    }


@tracer.tool(name="analyze_frames", description="Analyze frames with Claude Vision")
def analyze_frames(frame_paths: list[str]) -> dict:
    """Analyze frames with Claude Vision to identify destination."""
    print(f"\n[analyze_frames] {len(frame_paths)} frames")

    span = trace.get_current_span()
    span.set_attribute("vision.frame_count", len(frame_paths))
    span.set_attribute("vision.model", MODEL)

    if not frame_paths:
        fail("No frames provided")

    # Build image content for Claude Vision
    images = []
    for path in frame_paths[:8]:
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
            images.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": data}
            })

    if not images:
        fail("Could not load images")

    # Call Claude Vision
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": images + [{
                "type": "text",
                "text": """Analyze these travel video frames and identify the destination.

Return JSON:
{
    "location": {"country": "", "city": "", "region": "", "specific_places": []},
    "landmarks": [],
    "activities": [],
    "landscape_type": [],
    "description": "2-3 sentences describing the destination"
}

Return ONLY the JSON object."""
            }]
        }]
    )

    text = response.content[0].text
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        destination = json.loads(text[start:end]) if start >= 0 and end > start else {"raw": text}
    except json.JSONDecodeError:
        destination = {"raw": text}

    loc = destination.get("location", {})
    print(f"[analyze_frames] → {loc.get('city', '?')}, {loc.get('country', '?')}")

    # Add detected location as attributes
    span.set_attribute("vision.detected_city", loc.get("city", "unknown"))
    span.set_attribute("vision.detected_country", loc.get("country", "unknown"))

    return {"success": True, "destination_info": destination}


@tracer.tool(name="generate_tags", description="Generate searchable tags from destination info")
def generate_tags(destination_info: dict) -> dict:
    """Generate searchable tags from destination info."""
    print(f"\n[generate_tags]")

    span = trace.get_current_span()
    span.set_attribute("tags.model", MODEL)

    if not destination_info:
        fail("No destination info")

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Generate tags for this travel destination:

{json.dumps(destination_info, indent=2)}

Return JSON with lowercase, hyphenated tags:
{{
    "location_tags": [],
    "activity_tags": [],
    "landscape_tags": [],
    "all_tags": ["all unique tags combined"]
}}

Return ONLY the JSON object."""
        }]
    )

    text = response.content[0].text
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        tags = json.loads(text[start:end]) if start >= 0 and end > start else {"all_tags": []}
    except json.JSONDecodeError:
        tags = {"all_tags": []}

    all_tags = tags.get("all_tags", [])
    print(f"[generate_tags] → {len(all_tags)} tags")

    span.set_attribute("tags.count", len(all_tags))

    return {"success": True, "tags": tags}


# ============================================================================
# Orchestrator
# ============================================================================

@tracer.chain(name="analyze_video")
def analyze_video(video_path: str) -> dict:
    """Run the video analysis pipeline: extract → analyze → tag."""
    print(f"\n{'='*50}")
    print("TRAVEL VIDEO ANALYZER (Framework-less)")
    print(f"{'='*50}")
    print(f"Video: {video_path}")

    # Each step raises on failure (see fail), so the error status reaches the root span.
    extraction = extract_frames(video_path)
    analysis = analyze_frames(extraction["frame_paths"])
    tagging = generate_tags(analysis["destination_info"])

    return {
        "destination_info": analysis.get("destination_info", {}),
        "tags": tagging.get("tags", {}),
        "metadata": extraction.get("metadata", {}),
    }


# ============================================================================
# The unit of work
# ============================================================================

def handle_request(video_path: str, session_id: str, user_id: str) -> dict:
    """One run, one root span. The request goes in as input.value, the answer comes out as
    output.value, and session.id / user.id ride on the root. This is what Lumoz names a
    business task from and reads the outcome from."""
    request = f"{WORKFLOW_TAG}\nAnalyze this travel video and tag its destination: {video_path}"
    with tracer.start_as_current_span("handle_request") as root:
        root.set_attribute("openinference.span.kind", "CHAIN")
        root.set_attribute("input.value", request)
        root.set_attribute("session.id", session_id)
        root.set_attribute("user.id", user_id)
        try:
            result = analyze_video(video_path)
        except Exception as exc:
            root.set_status(Status(StatusCode.ERROR, str(exc)))
            root.set_attribute("exception.type", type(exc).__name__)
            root.set_attribute("exception.message", str(exc))
            root.set_attribute("exception.stacktrace", traceback.format_exc())
            raise
        root.set_attribute("output.value", json.dumps(result))
        return result


def run_analysis(input: str) -> dict:
    """Entry point for experiment harness."""
    path = input.rsplit(":", 1)[-1].strip() if ":" in input else input
    return handle_request(path, session_id=str(uuid.uuid4()), user_id="experiment-harness")


if __name__ == "__main__":
    video = sys.argv[1] if len(sys.argv) > 1 else "./videos/travel-video-0.mp4"
    try:
        result = handle_request(
            video,
            session_id=os.environ.get("SESSION_ID", str(uuid.uuid4())),
            user_id=os.environ.get("USER_ID", "demo-user"),
        )
    finally:
        tracer_provider.shutdown()  # flush the last batch before the process exits

    print("\n" + "="*50)
    print("RESULTS")
    print("="*50)

    if dest := result.get("destination_info"):
        loc = dest.get("location", {})
        print(f"Location: {loc.get('city', '?')}, {loc.get('country', '?')}")
        if desc := dest.get("description"):
            print(f"Description: {desc}")

    if tags := result.get("tags", {}).get("all_tags"):
        print(f"Tags: {', '.join(tags)}")
