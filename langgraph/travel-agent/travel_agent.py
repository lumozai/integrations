"""
LangGraph Travel Agent — booking assistant with human-in-the-loop approval

A single tool-calling assistant that helps travelers search and book flights,
hotels, car rentals, and excursions. Tools are split into two tiers:

  - "safe" tools (search, policy lookup, read bookings) run immediately
  - "sensitive" tools (book, cancel) pause the graph via `interrupt_before`
    so a reviewer can approve or deny the action before it executes

Runnable two ways:
  - Interactively: `python travel_agent.py` — you play the reviewer at the
    terminal prompt.
  - Scripted: `python travel_agent.py --scenario <name>` — a predefined
    conversation runs end to end with approvals resolved automatically, so
    the whole demo is reproducible without typing. See scenarios.py.

Instrumented with OpenInference for optional Lumoz observability.
"""

# Standard library
import argparse
import base64
import os
import sys
import uuid
from contextlib import nullcontext
from datetime import datetime
from typing import Annotated, Literal, Optional

# Third-party - Core
from dotenv import load_dotenv
from typing_extensions import TypedDict

# Third-party - LangGraph / LangChain
from langchain_core.messages import AnyMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

import travel_data as db

# Third-party - OpenTelemetry / OpenInference (optional)
try:
    from opentelemetry import trace
    from opentelemetry.sdk import trace as trace_sdk
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from openinference.instrumentation import using_attributes
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

if not _OTEL_AVAILABLE:
    from contextlib import contextmanager

    @contextmanager
    def using_attributes(**kwargs):
        yield

# Load environment variables
load_dotenv()

if not os.environ.get("OPENAI_API_KEY"):
    print("Error: OPENAI_API_KEY environment variable is required.")
    sys.exit(1)


# ============================================================================
# Lumoz Tracing
# ============================================================================

def configure_lumoz_tracing():
    """Configure OpenInference instrumentation for Lumoz observability.

    Returns a TracerProvider if both LUMOZ_API_KEY and OTEL_ENDPOINT are set,
    otherwise returns None. The app works fine without tracing.
    """
    if not _OTEL_AVAILABLE:
        print("[tracing] OpenTelemetry packages not installed — tracing disabled")
        return None

    otel_endpoint = os.environ.get("OTEL_ENDPOINT")
    api_key = os.environ.get("LUMOZ_API_KEY")

    if not otel_endpoint or not api_key:
        print("[tracing] LUMOZ_API_KEY or OTEL_ENDPOINT not set — tracing disabled")
        return None

    encoded = base64.b64encode(api_key.encode("utf-8")).decode("utf-8")
    headers = {"authorization": f"Basic {encoded}"}

    resource = Resource.create({
        "service.name": "langgraph-travel-agent",
        "deployment.environment": "development",
    })

    tracer_provider = trace_sdk.TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    otlp_exporter = OTLPSpanExporter(endpoint=otel_endpoint, headers=headers)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)

    print(f"[tracing] Lumoz tracing configured — sending traces to {otel_endpoint}")
    return tracer_provider


# Module-level tracing setup — runs before the graph or any tool/model
# objects are constructed, since LangChainInstrumentor patches classes.
tracer_provider = configure_lumoz_tracing()
tracing_enabled = tracer_provider is not None


# ============================================================================
# Tool Definitions
# ============================================================================

# Toggle used only by the "car_rental_outage" scenario to demonstrate what a
# downstream tool failure looks like in a trace. A real integration would
# have this fail because the upstream service is actually down.
SIMULATE_CAR_RENTAL_OUTAGE = False


@tool
def search_flights(departure_airport: str, arrival_airport: str) -> list[dict]:
    """Search for flights between two airport codes, e.g. JFK and ZRH."""
    return [
        f
        for f in db.FLIGHTS
        if f["departure_airport"].upper() == departure_airport.upper()
        and f["arrival_airport"].upper() == arrival_airport.upper()
    ]


@tool
def lookup_policy(topic: str) -> str:
    """Look up company policy on a topic: cancellation, baggage, change, or refund."""
    for key, text in db.POLICIES.items():
        if key in topic.lower():
            return text
    return f"No specific policy found for '{topic}'. Available topics: {', '.join(db.POLICIES)}."


@tool
def search_hotels(location: str) -> list[dict]:
    """Search for hotels in a given location."""
    return [h for h in db.HOTELS if location.lower() in h["location"].lower()]


@tool
def search_car_rentals(location: str) -> list[dict]:
    """Search for car rentals in a given location."""
    return [c for c in db.CAR_RENTALS if location.lower() in c["location"].lower()]


@tool
def search_excursions(location: str) -> list[dict]:
    """Search for trip excursions and activities in a given location."""
    return [e for e in db.EXCURSIONS if location.lower() in e["location"].lower()]


@tool
def get_my_bookings(config: RunnableConfig) -> list[dict]:
    """Fetch the current traveler's existing bookings."""
    passenger_id = config.get("configurable", {}).get("passenger_id")
    return db.BOOKINGS.get(passenger_id, [])


@tool
def book_flight(flight_id: str, config: RunnableConfig) -> str:
    """Book a flight by its flight ID for the current traveler."""
    passenger_id = config.get("configurable", {}).get("passenger_id")
    flight = next((f for f in db.FLIGHTS if f["flight_id"] == flight_id), None)
    if not flight:
        return f"No flight found with ID {flight_id}."
    booking = {
        "booking_id": db.next_booking_id(passenger_id),
        "type": "flight",
        "ref": flight_id,
        "summary": f"{flight['flight_no']} {flight['departure_airport']} -> {flight['arrival_airport']}",
        "amount_usd": flight["price_usd"],
    }
    db.BOOKINGS.setdefault(passenger_id, []).append(booking)
    return f"Booked {booking['summary']} for ${booking['amount_usd']}. Booking ID: {booking['booking_id']}."


@tool
def book_hotel(hotel_id: str, nights: int, config: RunnableConfig) -> str:
    """Book a hotel by its ID for a number of nights for the current traveler."""
    passenger_id = config.get("configurable", {}).get("passenger_id")
    hotel = next((h for h in db.HOTELS if h["hotel_id"] == hotel_id), None)
    if not hotel:
        return f"No hotel found with ID {hotel_id}."
    amount = hotel["price_per_night_usd"] * nights
    booking = {
        "booking_id": db.next_booking_id(passenger_id),
        "type": "hotel",
        "ref": hotel_id,
        "summary": f"{hotel['name']} ({nights} nights)",
        "amount_usd": amount,
    }
    db.BOOKINGS.setdefault(passenger_id, []).append(booking)
    return f"Booked {booking['summary']} for ${amount}. Booking ID: {booking['booking_id']}."


@tool
def book_car_rental(rental_id: str, days: int, config: RunnableConfig) -> str:
    """Book a car rental by its ID for a number of days for the current traveler."""
    if SIMULATE_CAR_RENTAL_OUTAGE:
        error = ConnectionError("Car rental service is unavailable (simulated outage).")
        if tracing_enabled:
            current_span = trace.get_current_span()
            if current_span and current_span.is_recording():
                current_span.record_exception(error)
                current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
        raise error

    passenger_id = config.get("configurable", {}).get("passenger_id")
    rental = next((c for c in db.CAR_RENTALS if c["rental_id"] == rental_id), None)
    if not rental:
        return f"No car rental found with ID {rental_id}."
    amount = rental["price_per_day_usd"] * days
    booking = {
        "booking_id": db.next_booking_id(passenger_id),
        "type": "car_rental",
        "ref": rental_id,
        "summary": f"{rental['name']} ({days} days)",
        "amount_usd": amount,
    }
    db.BOOKINGS.setdefault(passenger_id, []).append(booking)
    return f"Booked {booking['summary']} for ${amount}. Booking ID: {booking['booking_id']}."


@tool
def book_excursion(excursion_id: str, config: RunnableConfig) -> str:
    """Book an excursion by its ID for the current traveler."""
    passenger_id = config.get("configurable", {}).get("passenger_id")
    excursion = next((e for e in db.EXCURSIONS if e["excursion_id"] == excursion_id), None)
    if not excursion:
        return f"No excursion found with ID {excursion_id}."
    booking = {
        "booking_id": db.next_booking_id(passenger_id),
        "type": "excursion",
        "ref": excursion_id,
        "summary": excursion["name"],
        "amount_usd": excursion["price_usd"],
    }
    db.BOOKINGS.setdefault(passenger_id, []).append(booking)
    return f"Booked {booking['summary']} for ${booking['amount_usd']}. Booking ID: {booking['booking_id']}."


@tool
def cancel_booking(booking_id: str, config: RunnableConfig) -> str:
    """Cancel an existing booking by its booking ID and refund according to policy.

    The refund amount is always computed from the actual cancellation policy,
    regardless of what the traveler asks for in conversation — this is the
    source of truth a trace can be checked against.
    """
    passenger_id = config.get("configurable", {}).get("passenger_id")
    bookings = db.BOOKINGS.get(passenger_id, [])
    booking = next((b for b in bookings if b["booking_id"] == booking_id), None)
    if not booking:
        return f"No booking found with ID {booking_id} for this traveler."

    refund_rate = 0.8
    if booking["type"] == "flight":
        flight = next((f for f in db.FLIGHTS if f["flight_id"] == booking["ref"]), None)
        if flight:
            departure = datetime.fromisoformat(flight["scheduled_departure"])
            hours_until = (departure - datetime.now()).total_seconds() / 3600
            if hours_until < 24:
                refund_rate = 0.0
            elif hours_until < 72:
                refund_rate = 0.5

    refund = round(booking["amount_usd"] * refund_rate, 2)
    bookings.remove(booking)
    return (
        f"Cancelled {booking['summary']} (booking {booking_id}). "
        f"Refund per policy: ${refund} ({int(refund_rate * 100)}% of ${booking['amount_usd']})."
    )


SAFE_TOOLS = [
    search_flights,
    lookup_policy,
    search_hotels,
    search_car_rentals,
    search_excursions,
    get_my_bookings,
]
SENSITIVE_TOOLS = [book_flight, book_hotel, book_car_rental, book_excursion, cancel_booking]
SENSITIVE_TOOL_NAMES = {t.name for t in SENSITIVE_TOOLS}
ALL_TOOLS = SAFE_TOOLS + SENSITIVE_TOOLS


# ============================================================================
# State
# ============================================================================

class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    user_info: str


# ============================================================================
# Assistant Node
# ============================================================================

ASSISTANT_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a helpful travel booking assistant for a Swiss airline. "
            "Use the provided tools to search for flights, hotels, car rentals, and excursions, "
            "look up company policy, and manage bookings. "
            "Booking or cancelling anything requires calling the matching tool — never tell the "
            "traveler something is booked or refunded without actually calling the tool. "
            "If a tool call is denied by a reviewer, explain that to the traveler honestly and "
            "do not retry the same action unless they ask again."
            "\n\nCurrent traveler bookings:\n<Bookings>\n{user_info}\n</Bookings>"
            "\nCurrent time: {time}.",
        ),
        ("placeholder", "{messages}"),
    ]
).partial(time=datetime.now)

llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
assistant_runnable = ASSISTANT_PROMPT | llm.bind_tools(ALL_TOOLS)


class Assistant:
    def __init__(self, runnable: Runnable):
        self.runnable = runnable

    def __call__(self, state: State, config: RunnableConfig):
        result = self.runnable.invoke(state)
        # Re-prompt once if the model returns an empty response with no tool call
        if not result.tool_calls and not (result.content or "").strip():
            state = {**state, "messages": state["messages"] + [("user", "Please give a real response.")]}
            result = self.runnable.invoke(state)
        return {"messages": [result]}


def fetch_user_info(state: State, config: RunnableConfig) -> dict:
    bookings = get_my_bookings.invoke({}, config)
    if not bookings:
        return {"user_info": "No bookings on file."}
    lines = [f"- {b['booking_id']}: {b['summary']} (${b['amount_usd']})" for b in bookings]
    return {"user_info": "\n".join(lines)}


def handle_tool_error(state: State) -> dict:
    error = state.get("error")
    tool_calls = state["messages"][-1].tool_calls
    return {
        "messages": [
            ToolMessage(
                content=f"Error: {repr(error)}. Tell the traveler this action failed and suggest trying again shortly.",
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]
    }


def create_tool_node_with_fallback(tools: list) -> Runnable:
    return ToolNode(tools).with_fallbacks([RunnableLambda(handle_tool_error)], exception_key="error")


def route_tools(state: State) -> Literal["safe_tools", "sensitive_tools", "__end__"]:
    route = tools_condition(state)
    if route == END:
        return END
    # Demo scope: routes on the first tool call only; a real integration
    # handling parallel tool calls would need to split them across nodes.
    first_call = state["messages"][-1].tool_calls[0]
    return "sensitive_tools" if first_call["name"] in SENSITIVE_TOOL_NAMES else "safe_tools"


# ============================================================================
# Graph Construction
# ============================================================================

def build_graph():
    """Build the travel agent graph with a safe/sensitive tool split.

    The graph pauses via `interrupt_before` whenever the assistant wants to
    call a sensitive tool, so a reviewer (a human at the terminal, or a
    scripted scenario) can approve or deny the action before it runs.
    """
    builder = StateGraph(State)
    builder.add_node("fetch_user_info", fetch_user_info)
    builder.add_node("assistant", Assistant(assistant_runnable))
    builder.add_node("safe_tools", create_tool_node_with_fallback(SAFE_TOOLS))
    builder.add_node("sensitive_tools", create_tool_node_with_fallback(SENSITIVE_TOOLS))

    builder.add_edge(START, "fetch_user_info")
    builder.add_edge("fetch_user_info", "assistant")
    builder.add_conditional_edges("assistant", route_tools, ["safe_tools", "sensitive_tools", END])
    builder.add_edge("safe_tools", "assistant")
    builder.add_edge("sensitive_tools", "assistant")

    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer, interrupt_before=["sensitive_tools"])


# ============================================================================
# Conversation Runner (shared by interactive + scripted modes)
# ============================================================================

def _consume(events, printed: set) -> None:
    for event in events:
        message = event.get("messages")
        if not message:
            continue
        if isinstance(message, list):
            message = message[-1]
        if message.id in printed:
            continue
        printed.add(message.id)
        text = message.content if isinstance(message.content, str) else str(message.content)
        label = getattr(message, "type", "message")
        if text.strip():
            print(f"[{label}] {text}")
        if getattr(message, "tool_calls", None):
            for tc in message.tool_calls:
                print(f"[tool_call] {tc['name']}({tc['args']})")


def run_turn(app, config: dict, user_text: str, printed: set, approve) -> None:
    """Send one user message through the graph, resolving any approval
    interrupts along the way.

    `approve` is a callable that takes the pending tool_calls and returns
    True (continue) or False (deny). It's `input()`-backed in interactive
    mode and scenario-defined in scripted mode.
    """
    print(f"\nTraveler: {user_text}")
    events = app.stream({"messages": [("user", user_text)]}, config, stream_mode="values")
    _consume(events, printed)

    snapshot = app.get_state(config)
    while snapshot.next:
        tool_calls = snapshot.values["messages"][-1].tool_calls
        if approve(tool_calls):
            events = app.stream(None, config, stream_mode="values")
        else:
            denial = {
                "messages": [
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content="Reviewer denied this action. Explain the denial to the traveler and do not retry it.",
                    )
                    for tc in tool_calls
                ]
            }
            events = app.stream(denial, config, stream_mode="values")
        _consume(events, printed)
        snapshot = app.get_state(config)


# ============================================================================
# Scripted Scenario Mode
# ============================================================================

def run_scenario_chain(names: list[str]) -> None:
    """Run one or more named scenarios back-to-back as a single session.

    A single name behaves exactly like running that scenario alone — one
    session, one trace. Multiple names share one session_id/thread_id, so
    their turns land in the same continuous conversation and the same trace,
    as if a traveler moved from one line of questioning straight into the
    next without hanging up.
    """
    from scenarios import SCENARIOS

    chosen = []
    for name in names:
        scenario = SCENARIOS.get(name)
        if not scenario:
            print(f"Unknown scenario '{name}'. Use --list-scenarios to see available names.")
            sys.exit(1)
        chosen.append((name, scenario))

    chain_label = "+".join(names)
    session_id = f"{chain_label}-{uuid.uuid4().hex[:8]}"
    # A chained session needs one passenger/user identity throughout — take
    # it from the first scenario in the chain unless every scenario agrees
    # on their own (in which case it wouldn't matter which one we picked).
    passenger_id = chosen[0][1].get("passenger_id", "PASSENGER-100")
    user_id = chosen[0][1].get("user_id", "demo-traveler")

    print(f"\n{'=' * 70}")
    print(f"SESSION: {chain_label}")
    for name, scenario in chosen:
        print(f"  - {name}: {scenario['description']}")
    print(f"session.id={session_id} user.id={user_id} passenger.id={passenger_id}")
    print(f"{'=' * 70}")

    app = build_graph()
    config = {"configurable": {"passenger_id": passenger_id, "thread_id": session_id}}

    tracer = trace.get_tracer("travel_agent") if tracing_enabled else None
    span_ctx = (
        tracer.start_as_current_span(
            f"travel_agent_scenario_{chain_label}",
            attributes={"session.id": session_id, "user.id": user_id, "scenario": chain_label},
        )
        if tracing_enabled
        else nullcontext()
    )
    attr_ctx = using_attributes(user_id=user_id, session_id=session_id) if tracing_enabled else nullcontext()

    global SIMULATE_CAR_RENTAL_OUTAGE
    printed: set = set()
    with span_ctx, attr_ctx:
        for name, scenario in chosen:
            SIMULATE_CAR_RENTAL_OUTAGE = scenario.get("simulate_car_rental_outage", False)
            for turn in scenario["turns"]:
                approve_this_turn = turn.get("approve", True)
                run_turn(app, config, turn["user"], printed, approve=lambda _tc: approve_this_turn)
    SIMULATE_CAR_RENTAL_OUTAGE = False

    print(f"\n--- End of session: {chain_label} ---\n")


def run_scenario(name: str) -> None:
    run_scenario_chain([name])


def list_scenarios() -> None:
    from scenarios import SCENARIOS

    print("Available scenarios:\n")
    for name, scenario in SCENARIOS.items():
        print(f"  {name:28s} {scenario['description']}")


# ============================================================================
# Interactive REPL Mode
# ============================================================================

def run_interactive() -> None:
    print("\n=== LangGraph Travel Agent ===\n")

    passenger_id = input("Enter passenger ID (Enter for 'PASSENGER-100' demo account): ").strip()
    if not passenger_id:
        passenger_id = "PASSENGER-100"
    user_id = input("Enter your user ID (Enter for 'demo-traveler'): ").strip() or "demo-traveler"
    session_id = str(uuid.uuid4())
    print(f"Session: {session_id}\n")
    print("Type /quit to exit. Booking or cancelling will ask you to approve as the reviewer.\n")

    app = build_graph()
    config = {"configurable": {"passenger_id": passenger_id, "thread_id": session_id}}
    tracer = trace.get_tracer("travel_agent") if tracing_enabled else None
    printed: set = set()

    def approve_interactively(tool_calls) -> bool:
        names = ", ".join(tc["name"] for tc in tool_calls)
        answer = input(f"Approve {names}? [y/n]: ").strip().lower()
        return answer == "y"

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() == "/quit":
            break

        span_ctx = (
            tracer.start_as_current_span(
                "travel_agent_turn",
                attributes={"session.id": session_id, "user.id": user_id, "query": user_input},
            )
            if tracing_enabled
            else nullcontext()
        )
        attr_ctx = using_attributes(user_id=user_id, session_id=session_id) if tracing_enabled else nullcontext()
        with span_ctx, attr_ctx:
            run_turn(app, config, user_input, printed, approve=approve_interactively)

    if tracing_enabled and tracer_provider:
        print("[tracing] Flushing traces...")
        tracer_provider.force_flush()
    print("Goodbye!")


def main():
    parser = argparse.ArgumentParser(description="LangGraph Travel Agent demo")
    parser.add_argument(
        "--scenario",
        help=(
            "Run scripted scenario(s) end to end (see --list-scenarios). "
            "A single name is one session. Comma-separate names (e.g. "
            "'flight_and_policy,book_trip') to chain them into ONE session/trace. "
            "'all' runs every scenario, each as its own separate session."
        ),
    )
    parser.add_argument("--list-scenarios", action="store_true", help="List available scenario names and exit")
    args = parser.parse_args()

    if args.list_scenarios:
        list_scenarios()
        return

    if args.scenario:
        if args.scenario == "all":
            from scenarios import SCENARIOS

            for name in SCENARIOS:
                run_scenario_chain([name])
        else:
            names = [n.strip() for n in args.scenario.split(",") if n.strip()]
            run_scenario_chain(names)
        if tracing_enabled and tracer_provider:
            tracer_provider.force_flush()
        return

    run_interactive()


if __name__ == "__main__":
    main()
