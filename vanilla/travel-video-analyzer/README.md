# Travel Video Analyzer - Framework-less

A multi-agent app using **plain Python** with direct Anthropic API calls. No LangGraph, LangChain, or other agent frameworks - just OpenTelemetry spans with OpenInference attributes.

This is the runnable example behind the Lumoz guide **Instrument > Custom instrumentation**. Every element that guide asks for is here, marked in the code.

## Architecture

```
handle_request (root CHAIN)         request in, answer out, session + user
└── analyze_video (CHAIN)
    ├── extract_frames (TOOL)       OpenCV frame extraction
    ├── analyze_frames (TOOL)       Claude Vision analysis
    │   └── Claude call (LLM)
    └── generate_tags (TOOL)        Tag generation
        └── Claude call (LLM)
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env with your API keys
```

## Run

```bash
# Copy sample videos from LangGraph version
cp -r ../langgraph/travel-video-analyzer/videos .

# Run analysis
python travel_video_analyzer.py ./videos/travel-video-0.mp4
```

## The Lumoz contract, element by element

| Guide section | Where in the code | What it sets |
|---|---|---|
| The unit of work | `handle_request` | One root span per run with `input.value` (the request), `output.value` (the answer), `session.id`, `user.id` |
| Runs no person started | `WORKFLOW_TAG` | `[Workflow: Analyze Travel Video]` at the start of the request, the name Lumoz gives the business task |
| Span kinds | `@tracer.chain`, `@tracer.tool` | `openinference.span.kind` CHAIN and TOOL, with `tool.name`, `tool.description`, arguments as `input.value` and the result as `output.value` |
| Model calls | `AnthropicInstrumentor` | LLM spans with `llm.input_messages`, `llm.output_messages`, `llm.model_name` and token counts, recorded by the instrumentor. To record them by hand, see the guide's Model calls section |
| Errors | `fail`, and the `except` in `handle_request` | Error status plus `exception.type`, `exception.message`, `exception.stacktrace` as span attributes, on the failing span and on the root |
| Flush before exit | the `finally` in `__main__` | `tracer_provider.shutdown()` so the last batch is exported |

## View Traces in Lumoz

1. Run the analyzer on a video
2. Open [Lumoz Console](https://console.lumoz.ai)
3. Go to **Home** and click the **travel-video-analyzer-vanilla** app card
4. Open **Detect > Explore** and click the trace to see the root, the tool calls and the model calls
5. The run appears as the business task **Analyze Travel Video**

## Key Decorators

```python
from openinference.instrumentation import OITracer

tracer = OITracer(trace.get_tracer(__name__), TraceConfig())

@tracer.chain(name="workflow")      # For orchestration
@tracer.tool(name="...", desc="...") # For tools
@tracer.llm                          # For LLM calls
```

## Custom Span Attributes

Add your own attributes from within decorated functions. They are stored with the trace and shown in the span detail; only the keys in the guide are analyzed.

```python
from opentelemetry import trace

@tracer.tool(name="analyze_frames", description="Analyze frames with Vision")
def analyze_frames(frame_paths: list[str]) -> dict:
    span = trace.get_current_span()
    span.set_attribute("vision.frame_count", len(frame_paths))
    span.set_attribute("vision.model", MODEL)

    # ... processing ...

    # Add attributes after computation
    span.set_attribute("vision.detected_city", city)
    return result
```

## Span Processing

Use a span processor to batch and export spans:

```python
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider.add_span_processor(BatchSpanProcessor(exporter))
```

### Image Payload Handling (Vision API)

If your app sends images to Claude Vision, use `ImageStrippingSpanProcessor` instead. Large base64-encoded images in span attributes cause export timeouts and duplicate spans.

```python
# For apps WITHOUT Vision API - use standard BatchSpanProcessor
provider.add_span_processor(BatchSpanProcessor(exporter))

# For apps WITH Vision API - use ImageStrippingSpanProcessor
provider.add_span_processor(ImageStrippingSpanProcessor(exporter))
```

The `ImageStrippingSpanProcessor` (included in this example) extends `BatchSpanProcessor` and truncates base64 image data before export:

```python
class ImageStrippingSpanProcessor(BatchSpanProcessor):
    """Strips base64 image data from spans before export."""

    def on_end(self, span):
        # Truncate base64 patterns: data:image/..., /9j/..., iVBOR...
        # Keeps first 100 chars + "...[IMAGE_TRUNCATED]"
        super().on_end(span)
```
