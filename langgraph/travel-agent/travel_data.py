"""
In-memory mock travel database.

Flights, hotels, car rentals, excursions, and per-passenger bookings all live
in plain dicts/lists here so the example has no external download or network
dependency (no SQLite fetch, no vector store) beyond the LLM call itself.
"""

from datetime import datetime, timedelta

# Anchored to real time so flights stay in the future no matter when this
# example is run — not a fixed date like a snapshot fixture would use.
_NOW = datetime.now()

FLIGHTS = [
    {
        "flight_id": "FL100",
        "flight_no": "LX100",
        "departure_airport": "JFK",
        "arrival_airport": "ZRH",
        "scheduled_departure": (_NOW + timedelta(days=1, hours=2)).isoformat(),
        "scheduled_arrival": (_NOW + timedelta(days=1, hours=10)).isoformat(),
        "price_usd": 640,
    },
    {
        "flight_id": "FL101",
        "flight_no": "LX101",
        "departure_airport": "JFK",
        "arrival_airport": "ZRH",
        "scheduled_departure": (_NOW + timedelta(days=7, hours=3)).isoformat(),
        "scheduled_arrival": (_NOW + timedelta(days=7, hours=11)).isoformat(),
        "price_usd": 590,
    },
    {
        "flight_id": "FL102",
        "flight_no": "LX205",
        "departure_airport": "ZRH",
        "arrival_airport": "JFK",
        "scheduled_departure": (_NOW + timedelta(days=14, hours=5)).isoformat(),
        "scheduled_arrival": (_NOW + timedelta(days=14, hours=13)).isoformat(),
        "price_usd": 610,
    },
]

HOTELS = [
    {"hotel_id": "H1", "name": "Lakeside Zurich", "location": "Zurich", "price_tier": "Upscale", "price_per_night_usd": 320},
    {"hotel_id": "H2", "name": "Old Town Inn", "location": "Zurich", "price_tier": "Midscale", "price_per_night_usd": 140},
    {"hotel_id": "H3", "name": "Lucerne Riverside", "location": "Lucerne", "price_tier": "Upper Midscale", "price_per_night_usd": 210},
]

CAR_RENTALS = [
    {"rental_id": "C1", "name": "Alps Rentals", "location": "Zurich", "price_tier": "Economy", "price_per_day_usd": 45},
    {"rental_id": "C2", "name": "Alps Rentals", "location": "Zurich", "price_tier": "Compact", "price_per_day_usd": 60},
    {"rental_id": "C3", "name": "Lucerne Motors", "location": "Lucerne", "price_tier": "Compact", "price_per_day_usd": 55},
]

EXCURSIONS = [
    {"excursion_id": "E1", "name": "Rigi Kulm Cable Car", "location": "Lucerne", "price_usd": 85, "keywords": "mountains,scenic,hiking"},
    {"excursion_id": "E2", "name": "Swiss National Museum", "location": "Zurich", "price_usd": 25, "keywords": "museum,history,indoor"},
    {"excursion_id": "E3", "name": "Lake Lucerne Boat Tour", "location": "Lucerne", "price_usd": 60, "keywords": "lake,scenic,relaxing"},
]

# Seed a couple of demo passengers with an existing itinerary so read-only
# tools have something to show on the very first turn.
BOOKINGS: dict[str, list[dict]] = {
    "PASSENGER-100": [
        {
            "booking_id": "BK1",
            "type": "flight",
            "ref": "FL100",
            "summary": "LX100 JFK -> ZRH",
            "amount_usd": 640,
        }
    ],
    "PASSENGER-200": [],
}

POLICIES = {
    "cancellation": (
        "Cancellations made more than 72 hours before departure receive an 80% refund. "
        "Cancellations within 72 hours receive a 50% refund. No refunds within 24 hours of departure."
    ),
    "baggage": (
        "Economy fares include one carry-on and one checked bag up to 23kg. "
        "Additional or oversized bags incur a $75 fee per bag."
    ),
    "change": (
        "Flight changes are permitted up to 3 hours before scheduled departure, subject to fare difference. "
        "A $50 change fee applies for changes made within 7 days of the original booking."
    ),
    "refund": (
        "Refund amounts follow the cancellation policy exactly — see 'cancellation'. "
        "No refund exceeds the amount actually paid, and no agent may waive this without a manager override "
        "recorded in the booking system."
    ),
}


def next_booking_id(passenger_id: str) -> str:
    existing = BOOKINGS.get(passenger_id, [])
    return f"BK{len(existing) + 1 + hash(passenger_id) % 1000}"
