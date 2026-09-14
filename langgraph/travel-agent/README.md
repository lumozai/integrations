# Travel Agent - LangGraph

A booking assistant that searches and books flights, hotels, car rentals, and excursions, with sensitive actions gated behind human-in-the-loop approval. Instrumented with [Lumoz](https://lumoz.ai) for optional observability.

## Architecture

A single assistant node with two tool tiers. "Safe" tools (search, policy lookup, reading bookings) run immediately. "Sensitive" tools (booking, cancelling) pause the graph with `interrupt_before` so a reviewer can approve or deny before anything executes:

```
                +-------------------------------------------+
                |            LangGraph StateGraph            |
                |                                            |
 START -->  fetch_user_info  -->  assistant                  |
                |                     |                       |
                |          +----------+----------+            |
                |          |                     |             |
                |     safe_tools          sensitive_tools      |
                |    (search, lookup)   (book_*, cancel_*)     |
                |          |                     |             |
                |          |            [ interrupt_before ]   |
                |          |             reviewer approve/deny  |
                |          v                     v             |
                |        assistant <----------- assistant      |
                +-------------------------------------------+
```

## Tools

| Tier | Tools |
|------|-------|
| Safe | `search_flights`, `search_hotels`, `search_car_rentals`, `search_excursions`, `lookup_policy`, `get_my_bookings` |
| Sensitive (requires approval) | `book_flight`, `book_hotel`, `book_car_rental`, `book_excursion`, `cancel_booking` |

Flight, hotel, car rental, and excursion data live in an in-memory mock database (`travel_data.py`) — no external download or vector store needed to run this example.

## What Gets Traced

When running with Lumoz instrumentation, you'll see:

- **Assistant node**: the tool-calling loop deciding what to do next
- **Tool calls**: safe tools running immediately, sensitive tools pausing for approval and resuming after
- **A denied action**: the assistant receiving a `ToolMessage` explaining a reviewer declined the request, and how it responds
- **A tool exception**: what a downstream failure looks like in the trace (see the `car_rental_outage` scenario)
- **Root span**: one span per scenario (or per turn in interactive mode) with `user.id` and `session.id` attributes

## Setup

### 1. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` with your API keys:

```bash
# Required
OPENAI_API_KEY=sk-your-openai-key

# Optional — Lumoz observability
LUMOZ_API_KEY=your_client_id:your_client_secret
OTEL_ENDPOINT=https://api.lumoz.ai/proxy/v1/traces
```

**Get your Lumoz API key:**
1. Log in to [Lumoz Console](https://console.lumoz.ai)
2. Go to **Settings > API Keys**
3. Click **Create API Key**
4. Copy the key (format: `client_id:client_secret`)

Tracing is optional. The app works with only `OPENAI_API_KEY` set.

## Add Lumoz to Your Own LangGraph App

> **Just want the instrumentation code?** → [INSTRUMENTATION.md](INSTRUMENTATION.md)
>
> Covers: exporter setup, `LangChainInstrumentor`, user/session tracking with `using_attributes`, recording a tool exception on the current span, graceful shutdown.

## Run

### Scripted scenarios (no typing required)

```bash
python travel_agent.py --list-scenarios
python travel_agent.py --scenario book_trip
python travel_agent.py --scenario all
```

| Scenario | Demonstrates |
|----------|---------------|
| `flight_and_policy` | Read-only Q&A, no approvals |
| `book_trip` | Multi-step hotel + car booking, both approved |
| `denied_cancellation` | A reviewer denying a sensitive action |
| `adversarial_refund_request` | A traveler asking the assistant to bypass policy — the refund tool always returns the policy-correct amount regardless, so the trace can be checked against what the assistant tells the traveler |
| `car_rental_outage` | A simulated downstream tool failure and how it's recorded on the span |

#### One session vs. many

- `--scenario <name>` — one scenario, one session, one trace.
- `--scenario <name1>,<name2>,...` — **chains** the named scenarios' turns into a single session/thread, so they land in one continuous trace (e.g. `--scenario flight_and_policy,book_trip` reads as one traveler moving from questions straight into booking).
- `--scenario all` — runs every scenario, but each keeps its **own** separate session, so you get 5 independent traces in one process.

### Interactive mode

```bash
python travel_agent.py
```

```
=== LangGraph Travel Agent ===

Enter passenger ID (Enter for 'PASSENGER-100' demo account):
Enter your user ID (Enter for 'demo-traveler'):
Session: a1b2c3d4-...

Type /quit to exit. Booking or cancelling will ask you to approve as the reviewer.

You: Find me a flight from JFK to ZRH next week
...
You: Book that one
[tool_call] book_flight({'flight_id': 'FL101'})
Approve book_flight? [y/n]: y
...
```

## View Traces in Lumoz

1. Run a scenario or interactive session
2. Open [Lumoz Console](https://console.lumoz.ai)
3. Go to **Home** and click the **langgraph-travel-agent** app card
4. Navigate to the **Telemetry → Traces** tab
5. Click a trace to explore the assistant's tool-calling loop, the approval pause/resume, and any tool errors

## Requirements

- Python 3.11+
- OpenAI API key (for gpt-4o-mini)
- Lumoz API key (optional, for observability)

## Learn More

- [LangGraph Documentation](https://langchain-ai.github.io/langgraph/)
- [OpenInference](https://github.com/Arize-ai/openinference)
- Questions? [support@lumoz.ai](mailto:support@lumoz.ai)
