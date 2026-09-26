"""
Travel deal finder + Discord poster.

What this does, step by step:
  1. Checks flight prices from multiple world-hub origins (ORIGINS) to a
     list of destinations (DESTINATIONS) — every origin x every
     destination, checked in parallel for speed.
  2. Compares each price to that destination's typical price to get a %
     discount, and rates qualifying ones into a tier (GOOD/GREAT/INSANE).
  3. Anti-spam rules decide what actually gets posted:
       - DESTINATION_COOLDOWN_DAYS: won't repost the same destination city
         again within this many days, regardless of exact dates.
       - MAX_POSTS_PER_CONTINENT_PER_DAY: caps how many deals land in any
         one continent channel per calendar day.
       - The single best deal found today (highest discount %) becomes
         the "Deal of the Day" and gets its own spotlight post, once per
         day, in DEAL_OF_THE_DAY channel.
  4. Posts whatever survives those rules to the matching continent
     channel (and the Deal of the Day channel, if applicable).

"typical_price" per destination is a rough estimate you set yourself —
refine it over time with typical_price_research.py.

HOW TO USE (local testing):
  1. pip install discord.py requests
  2. Set the required environment variables before running (see the GitHub
     Actions workflow for the full list) — either in your terminal session
     or a local .env setup. Don't hardcode real credentials into this file.
  3. Edit ORIGINS, DESTINATIONS, MIN_DISCOUNT_PERCENT, DEAL_TIERS, and the
     anti-spam settings below to match what you want.
  4. Run: python deal_finder.py
  5. It checks every route once, posts any deals found, then stops.

For automated scheduled runs, this is meant to be triggered by a GitHub
Actions workflow that supplies these same values as encrypted Secrets.
"""

import datetime
import json
import os

import discord
import requests

STATE_FILE = "bot_state.json"

# ============ CONFIG ============
# Credentials now come from environment variables (set as GitHub Secrets
# when automated, or in your terminal when testing locally) instead of
# being hardcoded here — safer, since this file can be committed to a
# repo without leaking anything.

TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "")
TRAVELPAYOUTS_MARKER = os.environ.get("TRAVELPAYOUTS_MARKER", "")
TRAVELPAYOUTS_TRS = os.environ.get("TRAVELPAYOUTS_TRS", "")

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

# One channel ID per continent — deals get routed to the matching channel
# automatically based on the destination's continent.
CONTINENT_CHANNELS = {
    "Europe": os.environ.get("CHANNEL_EUROPE", ""),
    "Asia": os.environ.get("CHANNEL_ASIA", ""),
    "South America": os.environ.get("CHANNEL_SOUTH_AMERICA", ""),
    "Caribbean": os.environ.get("CHANNEL_CARIBBEAN", ""),
    "Africa": os.environ.get("CHANNEL_AFRICA", ""),
    "Oceania": os.environ.get("CHANNEL_OCEANIA", ""),
    "North America": os.environ.get("CHANNEL_NORTH_AMERICA", ""),
}

# Used if a route's continent isn't in CONTINENT_CHANNELS above
DEFAULT_CHANNEL_ID = os.environ.get("CHANNEL_DEFAULT", "")

# Optional spotlight channel for the single best deal of the day, across
# every continent. Leave the env var unset/blank to disable this feature.
DEAL_OF_THE_DAY_CHANNEL_ID = os.environ.get("CHANNEL_DEAL_OF_THE_DAY", "")

# Origin hubs — major airports across different world regions. Every one
# of these gets checked against every destination below, so adding an
# origin here multiplies your route coverage.
ORIGINS = [
    "JFK",  # New York
    "LHR",  # London
    "DXB",  # Dubai
    "SIN",  # Singapore
    "GRU",  # São Paulo
    "JNB",  # Johannesburg
]

# Destinations to check: (code, city name, continent, emoji flag, typical
# round-trip price USD). "typical_price" is a rough estimate — refine it
# over time with typical_price_research.py.
DESTINATIONS = [
    # Europe
    ("LIS", "Lisbon", "Europe", "🇵🇹", 650),
    ("CDG", "Paris", "Europe", "🇫🇷", 620),
    ("FCO", "Rome", "Europe", "🇮🇹", 403),
    ("BCN", "Barcelona", "Europe", "🇪🇸", 507),
    ("AMS", "Amsterdam", "Europe", "🇳🇱", 379),
    ("LHR", "London", "Europe", "🇬🇧", 553),
    # Asia
    ("NRT", "Tokyo", "Asia", "🇯🇵", 1043),
    ("BKK", "Bangkok", "Asia", "🇹🇭", 607),
    ("SIN", "Singapore", "Asia", "🇸🇬", 800),
    ("ICN", "Seoul", "Asia", "🇰🇷", 774),
    ("DXB", "Dubai", "Asia", "🇦🇪", 544),
    # South America
    ("GRU", "São Paulo", "South America", "🇧🇷", 1184),
    ("EZE", "Buenos Aires", "South America", "🇦🇷", 1365),
    ("BOG", "Bogotá", "South America", "🇨🇴", 684),
    ("LIM", "Lima", "South America", "🇵🇪", 800),
    # Caribbean
    ("BGI", "Barbados", "Caribbean", "🇧🇧", 1466),  # only 2 samples — low confidence, re-check later
    ("PUJ", "Punta Cana", "Caribbean", "🇩🇴", 881),
    ("MBJ", "Montego Bay", "Caribbean", "🇯🇲", 690),
    ("NAS", "Nassau", "Caribbean", "🇧🇸", 557),
    # Africa
    ("CAI", "Cairo", "Africa", "🇪🇬", 483),
    ("JNB", "Johannesburg", "Africa", "🇿🇦", 811),
    ("NBO", "Nairobi", "Africa", "🇰🇪", 418),
    # Oceania
    ("SYD", "Sydney", "Oceania", "🇦🇺", 1212),
    ("AKL", "Auckland", "Oceania", "🇳🇿", 1493),
    # North America
    ("MIA", "Miami", "North America", "🇺🇸", 607),
    ("YYZ", "Toronto", "North America", "🇨🇦", 578),  # one sample hit $3905 (outlier) — median still used
]

# Built automatically: every origin x every destination, skipping any pair
# where they're the same airport. (origin, destination, name, continent, flag, typical_price)
ROUTES = [
    (origin, code, name, continent, flag, typical_price)
    for origin in ORIGINS
    for code, name, continent, flag, typical_price in DESTINATIONS
    if origin != code
]

# Minimum discount below typical price to count as a deal worth posting at all
MIN_DISCOUNT_PERCENT = 15

# Tiers, from best to worst — first one whose threshold is met gets used.
# (label, minimum discount percent required)
DEAL_TIERS = [
    ("🔥🔥🔥 INSANE DEAL", 35),
    ("🔥🔥 GREAT DEAL", 25),
    ("🔥 GOOD DEAL", 15),
]

# ---- Anti-spam settings ----

# Don't post about the same destination CITY again within this many days,
# even if the specific dates/price are different. Prevents one chronically
# discounted route from dominating a channel.
DESTINATION_COOLDOWN_DAYS = 3

# Max number of deals allowed into any single continent channel per
# calendar day, even if more routes qualify.
MAX_POSTS_PER_CONTINENT_PER_DAY = 3

# ==================================================


def get_cheap_flight(origin, destination):
    """Ask the fresher v3 Data API for the cheapest real fare on this route,
    restricted to a realistic near-term booking window (next ~90 days)."""
    today = datetime.date.today()
    search_month = (today.replace(day=1) + datetime.timedelta(days=32)).strftime("%Y-%m")

    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": search_month,  # next calendar month — keeps results realistic
        "one_way": "false",
        "direct": "false",
        "sorting": "price",
        "currency": "usd",
        "limit": 1,
        "token": TRAVELPAYOUTS_TOKEN,
    }
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    if not data.get("success") or not data.get("data"):
        return None
    return data["data"][0]


def make_affiliate_link(brand_url):
    """Turn a plain brand URL into a tracked Travelpayouts affiliate link."""
    url = "https://api.travelpayouts.com/links/v1/create"
    headers = {
        "X-Access-Token": TRAVELPAYOUTS_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "trs": int(TRAVELPAYOUTS_TRS),
        "marker": int(TRAVELPAYOUTS_MARKER),
        "shorten": True,
        "links": [{"url": brand_url, "sub_id": "deal_bot"}],
    }
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    result = response.json()["result"]["links"][0]
    return result.get("partner_url")


def get_cheap_hotel(destination, check_in, check_out):
    """Ask Hotellook's price cache for the cheapest hotel in this city for
    these dates. NOTE: the exact current endpoint for this isn't confirmed
    (docs are unclear and the original guess 404'd) — so this tries a few
    plausible candidates and logs which one actually works. Once we know,
    this can be simplified to just the working one.
    Returns None if nothing found or all candidates fail."""
    candidate_urls = [
        "https://engine.hotellook.com/api/v2/cache.json",
        "https://api.travelpayouts.com/hotels/v2/cache.json",
        "https://api.travelpayouts.com/hotellook/v2/cache.json",
    ]
    params = {
        "location": destination,
        "checkIn": check_in,
        "checkOut": check_out,
        "currency": "usd",
        "limit": 1,
        "token": TRAVELPAYOUTS_TOKEN,
    }

    for url in candidate_urls:
        try:
            response = requests.get(url, params=params, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f"  Hotel check ({url}) failed for {destination}: {e}")
            continue

        if response.status_code == 404:
            continue  # try the next candidate silently — this one doesn't exist

        if not response.ok:
            print(f"  Hotel check ({url}) for {destination}: HTTP {response.status_code}")
            continue

        try:
            data = response.json()
        except ValueError:
            continue

        if not data:
            return None

        hotel = data[0] if isinstance(data, list) else data
        print(f"  [HOTEL_WORKING_URL] {url} worked for {destination}!")
        if os.environ.get("HOTEL_DEBUG"):
            print(f"  [HOTEL_DEBUG] Raw response for {destination}: {hotel}")
        return hotel

    return None


def build_hotel_affiliate_link(destination, check_in, check_out):
    brand_url = (
        f"https://search.hotellook.com/?destination={destination}"
        f"&checkIn={check_in}&checkOut={check_out}&adults=1"
    )
    return make_affiliate_link(brand_url)


# ---- Persistent state (survives between runs via bot_state.json) ----
#
# Structure:
# {
#   "destination_last_posted": {"BKK": "2026-09-25", ...},
#   "daily_post_counts": {"2026-09-25": {"Asia": 2, "Europe": 1}},
#   "deal_of_the_day_date": "2026-09-25"
# }

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"destination_last_posted": {}, "daily_post_counts": {}, "deal_of_the_day_date": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def destination_on_cooldown(destination, state):
    last_posted = state["destination_last_posted"].get(destination)
    if last_posted is None:
        return False
    last_date = datetime.date.fromisoformat(last_posted)
    days_since = (datetime.date.today() - last_date).days
    return days_since < DESTINATION_COOLDOWN_DAYS


def continent_posts_today(continent, state):
    today_str = datetime.date.today().isoformat()
    return state["daily_post_counts"].get(today_str, {}).get(continent, 0)


def record_post(destination, continent, state):
    today_str = datetime.date.today().isoformat()
    state["destination_last_posted"][destination] = today_str
    state["daily_post_counts"].setdefault(today_str, {})
    state["daily_post_counts"][today_str][continent] = (
        state["daily_post_counts"][today_str].get(continent, 0) + 1
    )


def get_deal_tier(discount_percent):
    """Return the matching tier label for a given discount percent, or None
    if it doesn't qualify as a deal at all."""
    for label, min_percent in DEAL_TIERS:
        if discount_percent >= min_percent:
            return label
    return None


def fetch_all_prices(routes):
    """Check every route's price concurrently instead of one at a time —
    with 100+ routes, sequential checking would take too long."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {}

    def check_one(route):
        origin, destination, name, continent, flag, typical_price = route
        try:
            flight = get_cheap_flight(origin, destination)
        except requests.exceptions.RequestException as e:
            print(f"  {origin} -> {destination}: request failed ({e})")
            return route, None
        return route, flight

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(check_one, route) for route in routes]
        for future in as_completed(futures):
            route, flight = future.result()
            results[route] = flight

    return results


def ddmm(iso_date):
    _, month, day = iso_date.split("-")
    return f"{day}{month}"


def build_affiliate_link(origin, destination, depart_date, return_date):
    """Non-expiring search-link format that re-searches live when clicked."""
    code = f"{origin}{ddmm(depart_date)}{destination}{ddmm(return_date)}1"
    brand_url = f"https://www.aviasales.com/search/{code}"
    return make_affiliate_link(brand_url)


def attach_hotel_info(deal):
    """Look up a cheap hotel for this deal's destination/dates and attach
    it to the deal dict. Safe to call even if hotel data isn't available —
    the deal just won't show a hotel line."""
    hotel = get_cheap_hotel(deal["destination"], deal["departure_at"], deal["return_at"])
    if hotel is None:
        return deal

    # Field names below are a best guess based on Hotellook's documented
    # response shape — verify against real HOTEL_DEBUG output and adjust
    # if the actual keys differ.
    hotel_price = hotel.get("priceFrom") or hotel.get("price")
    hotel_name = hotel.get("hotelName") or hotel.get("name")

    if hotel_price is None:
        return deal

    deal["hotel_price"] = hotel_price
    deal["hotel_name"] = hotel_name
    deal["hotel_link"] = build_hotel_affiliate_link(
        deal["destination"], deal["departure_at"], deal["return_at"]
    )
    return deal


def find_candidate_deals():
    """Check every route and return ALL deals that clear the discount bar,
    with no anti-spam filtering applied yet — that happens in select_deals."""
    candidates = []

    print(f"Checking {len(ROUTES)} routes (in parallel, this may take a minute)...")
    price_results = fetch_all_prices(ROUTES)

    for route in ROUTES:
        origin, destination, name, continent, flag, typical_price = route
        flight = price_results.get(route)

        if flight is None:
            continue

        price = flight["price"]
        discount_percent = round((typical_price - price) / typical_price * 100)
        tier = get_deal_tier(discount_percent)

        if tier is None:
            continue

        depart_date = flight.get("departure_at", "")[:10]
        return_date = flight.get("return_at", "")[:10]

        candidates.append(
            {
                "name": name,
                "continent": continent,
                "flag": flag,
                "origin": origin,
                "destination": destination,
                "price": price,
                "typical_price": typical_price,
                "discount_percent": discount_percent,
                "tier": tier,
                "departure_at": depart_date,
                "return_at": return_date,
            }
        )
        print(f"{origin} -> {destination} ({name}, {continent}): ${price} "
              f"(typical ~${typical_price}, {discount_percent}% off) -> {tier}")

    return candidates


def select_deals(candidates, state):
    """Apply anti-spam rules to decide what actually gets posted:
    - best overall candidate becomes Deal of the Day (once per calendar day)
    - remaining candidates go through destination cooldown + per-continent
      daily cap before being allowed into their continent channel
    Returns (deal_of_the_day_or_None, list_of_continent_deals)
    """
    # Best first, so the strongest deals get priority for both DOTD and
    # the per-continent caps.
    candidates_sorted = sorted(candidates, key=lambda d: d["discount_percent"], reverse=True)

    today_str = datetime.date.today().isoformat()
    deal_of_the_day = None

    if DEAL_OF_THE_DAY_CHANNEL_ID and state.get("deal_of_the_day_date") != today_str and candidates_sorted:
        deal_of_the_day = candidates_sorted[0]

    continent_deals = []
    for deal in candidates_sorted:
        # Don't double-post the exact same deal as both DOTD and a regular post
        if deal_of_the_day is not None and deal is deal_of_the_day:
            continue

        if destination_on_cooldown(deal["destination"], state):
            continue

        if continent_posts_today(deal["continent"], state) >= MAX_POSTS_PER_CONTINENT_PER_DAY:
            continue

        continent_deals.append(deal)

    return deal_of_the_day, continent_deals


def format_deal_message(deal, is_deal_of_the_day=False):
    """Turn one deal into a rated alert with tier, discount %, and a
    disclaimer that the price was live at scan time. Includes a hotel
    line if hotel data was attached (see attach_hotel_info)."""
    header = "🏆 DEAL OF THE DAY 🏆\n\n" if is_deal_of_the_day else ""

    hotel_line = ""
    if deal.get("hotel_price") is not None:
        name_part = f" ({deal['hotel_name']})" if deal.get("hotel_name") else ""
        hotel_line = (
            f"🏨 Hotel from ${deal['hotel_price']}/night{name_part}\n"
            f"[CHECK HOTEL]({deal['hotel_link']})\n"
        )

    return (
        f"{header}{deal['tier']}\n\n"
        f"{deal['origin']} → {deal['flag']} {deal['name']} ({deal['continent']})\n"
        f"✈️ Round trip: ${deal['price']} "
        f"(~{deal['discount_percent']}% below typical ${deal['typical_price']})\n"
        f"📅 {deal['departure_at']} – {deal['return_at']}\n\n"
        f"[CHECK FLIGHT]({deal['link']})\n"
        f"{hotel_line}"
        f"💡 Prices were live at scan time — confirm final price on site"
    )


async def post_deals_to_discord(deal_of_the_day, continent_deals, state):
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        if deal_of_the_day is None and not continent_deals:
            print("No deals cleared the anti-spam rules today — nothing posted.")
            await client.close()
            return

        posted_count = 0
        today_str = datetime.date.today().isoformat()

        if deal_of_the_day is not None:
            channel = client.get_channel(int(DEAL_OF_THE_DAY_CHANNEL_ID))
            if channel is None:
                print("Could not find the Deal of the Day channel — skipping it.")
            else:
                deal_of_the_day["link"] = build_affiliate_link(
                    deal_of_the_day["origin"], deal_of_the_day["destination"],
                    deal_of_the_day["departure_at"], deal_of_the_day["return_at"],
                )
                attach_hotel_info(deal_of_the_day)
                await channel.send(format_deal_message(deal_of_the_day, is_deal_of_the_day=True))
                state["deal_of_the_day_date"] = today_str
                record_post(deal_of_the_day["destination"], deal_of_the_day["continent"], state)
                print(f"Posted Deal of the Day: {deal_of_the_day['origin']} -> {deal_of_the_day['destination']}")
                posted_count += 1

        for deal in continent_deals:
            channel_id = CONTINENT_CHANNELS.get(deal["continent"], DEFAULT_CHANNEL_ID)
            channel = client.get_channel(int(channel_id))

            if channel is None:
                print(f"Could not find channel for {deal['continent']} (ID: {channel_id}) — skipping.")
                continue

            deal["link"] = build_affiliate_link(
                deal["origin"], deal["destination"], deal["departure_at"], deal["return_at"]
            )
            attach_hotel_info(deal)
            await channel.send(format_deal_message(deal))
            record_post(deal["destination"], deal["continent"], state)
            print(f"Posted {deal['continent']} deal to #{channel.name}.")
            posted_count += 1

        save_state(state)
        print(f"\nPosted {posted_count} deal(s) total.")
        await client.close()

    await client.start(DISCORD_BOT_TOKEN)


def main():
    required = {
        "TRAVELPAYOUTS_TOKEN": TRAVELPAYOUTS_TOKEN,
        "TRAVELPAYOUTS_MARKER": TRAVELPAYOUTS_MARKER,
        "TRAVELPAYOUTS_TRS": TRAVELPAYOUTS_TRS,
        "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
        "CHANNEL_DEFAULT": DEFAULT_CHANNEL_ID,
        "CHANNEL_EUROPE": CONTINENT_CHANNELS["Europe"],
        "CHANNEL_ASIA": CONTINENT_CHANNELS["Asia"],
        "CHANNEL_SOUTH_AMERICA": CONTINENT_CHANNELS["South America"],
        "CHANNEL_CARIBBEAN": CONTINENT_CHANNELS["Caribbean"],
        "CHANNEL_AFRICA": CONTINENT_CHANNELS["Africa"],
        "CHANNEL_OCEANIA": CONTINENT_CHANNELS["Oceania"],
        "CHANNEL_NORTH_AMERICA": CONTINENT_CHANNELS["North America"],
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        print("Missing required environment variables: " + ", ".join(missing))
        return

    print("=== Checking routes for deals ===")
    state = load_state()
    candidates = find_candidate_deals()

    print(f"\n=== {len(candidates)} candidate(s) cleared the discount bar. Applying anti-spam rules... ===")
    deal_of_the_day, continent_deals = select_deals(candidates, state)

    print(f"=== Posting: {'1 Deal of the Day + ' if deal_of_the_day else ''}{len(continent_deals)} continent deal(s) ===")
    import asyncio

    asyncio.run(post_deals_to_discord(deal_of_the_day, continent_deals, state))


if __name__ == "__main__":
    main()
