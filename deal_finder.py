"""
Travel deal finder + Discord poster.

What this does, step by step:
  1. Checks flight prices for a list of routes you define below.
  2. Compares each price to that route's typical price to get a % discount.
  3. Any route that beats MIN_DISCOUNT_PERCENT gets rated into a tier
     (GOOD / GREAT / INSANE) and turned into a real affiliate link.
  4. Posts a formatted, rated deal alert for each one into your Discord channel.

"typical_price" per route is a rough estimate you set yourself — refine it
over time as you learn what these routes actually usually cost.

HOW TO USE:
  1. pip install discord.py requests
  2. Fill in the CONFIG section below with your real credentials.
  3. Edit ROUTES, MIN_DISCOUNT_PERCENT, and DEAL_TIERS to match what you want.
  4. Run: python deal_finder.py
  5. It checks every route once, posts any deals found, then stops.
     (Later, this can be scheduled to run daily/weekly automatically.)
"""

import discord
import requests

# ============ CONFIG — fill these in ============

TRAVELPAYOUTS_TOKEN = "4101aeaeeb9fad8aedf7b1dff53b9de4"
TRAVELPAYOUTS_MARKER = "780757"
TRAVELPAYOUTS_TRS = "576936"

DISCORD_BOT_TOKEN = "MTU1MjEwNzE0ODk3NTAyMjIwMA.Gug7Kz.twtyJ_XK-og2I5-c9mZf0KtkWvA7jf_tGWHu24"

# One channel ID per continent — deals get routed to the matching channel
# automatically based on the destination's continent.
# Get each ID the same way as before: enable Developer Mode, right-click
# the channel, Copy Channel ID.
CONTINENT_CHANNELS = {
    "Europe": "1552278622197391472",
    "Asia": "1552278660533461023",
    "South America": "1552278717198237796",
    "Caribbean": "1552278748601258017",
}

# Used if a route's continent isn't in CONTINENT_CHANNELS above
DEFAULT_CHANNEL_ID = "1552088254017179761"

# Routes to check: (origin, destination, city name, continent, emoji flag, typical round-trip price USD)
# IATA city codes — find them by searching "[city] IATA code"
#
# "typical_price" is your own rough estimate of what a normal round trip
# on this route usually costs — used to calculate how good a deal is.
# Adjust these as you learn real typical prices over time.
ROUTES = [
    ("JFK", "LIS", "Lisbon", "Europe", "🇵🇹", 550),
    ("JFK", "CDG", "Paris", "Europe", "🇫🇷", 650),
    ("JFK", "NRT", "Tokyo", "Asia", "🇯🇵", 950),
    ("JFK", "BKK", "Bangkok", "Asia", "🇹🇭", 900),
    ("JFK", "GRU", "São Paulo", "South America", "🇧🇷", 700),
    ("JFK", "BGI", "Barbados", "Caribbean", "🇧🇧", 450),
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

# ==================================================


def get_cheap_flight(origin, destination):
    """Ask the fresher v3 Data API for the cheapest real fare on this route,
    restricted to a realistic near-term booking window (next ~90 days)."""
    import datetime

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


def get_deal_tier(discount_percent):
    """Return the matching tier label for a given discount percent, or None
    if it doesn't qualify as a deal at all."""
    for label, min_percent in DEAL_TIERS:
        if discount_percent >= min_percent:
            return label
    return None


def find_deals():
    """Check every route and return a list of deals under the threshold."""
    deals = []

    for origin, destination, name, continent, flag, typical_price in ROUTES:
        print(f"Checking {origin} -> {destination} ({name}, {continent})...")
        flight = get_cheap_flight(origin, destination)

        if flight is None:
            print("  No cached price data for this route yet.")
            continue

        price = flight["price"]
        discount_percent = round((typical_price - price) / typical_price * 100)
        print(f"  Cheapest found: ${price} (typical ~${typical_price}, {discount_percent}% off)")

        tier = get_deal_tier(discount_percent)

        if tier is not None:
            depart_date = flight.get("departure_at", "")[:10]
            return_date = flight.get("return_at", "")[:10]

            # v3/prices_for_dates gives fresh (48hr) price data, but its own
            # "link" field expires too fast to use for a posted deal — the
            # simple date-code format below re-searches live when clicked,
            # so it never goes stale.
            def ddmm(iso_date):
                _, month, day = iso_date.split("-")
                return f"{day}{month}"

            code = f"{origin}{ddmm(depart_date)}{destination}{ddmm(return_date)}1"
            brand_url = f"https://www.aviasales.com/search/{code}"
            print(f"  Raw brand URL: {brand_url}")
            affiliate_link = make_affiliate_link(brand_url)

            deals.append(
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
                    "departure_at": flight.get("departure_at", "")[:10],
                    "return_at": flight.get("return_at", "")[:10],
                    "link": affiliate_link,
                }
            )
            print(f"  -> {tier}")

    return deals


def format_deal_message(deal):
    """Turn one deal into a rated alert with tier, discount %, and a
    disclaimer that the price was live at scan time."""
    return (
        f"{deal['tier']}\n\n"
        f"{deal['origin']} → {deal['flag']} {deal['name']} ({deal['continent']})\n"
        f"✈️ Round trip: ${deal['price']} "
        f"(~{deal['discount_percent']}% below typical ${deal['typical_price']})\n"
        f"📅 {deal['departure_at']} – {deal['return_at']}\n\n"
        f"[CHECK DEAL]({deal['link']})\n"
        f"💡 Price was live at scan time — confirm final price on site"
    )


async def post_deals_to_discord(deals):
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        if not deals:
            print("No deals cleared the bar today — nothing posted.")
        else:
            posted_count = 0
            for deal in deals:
                channel_id = CONTINENT_CHANNELS.get(deal["continent"], DEFAULT_CHANNEL_ID)
                channel = client.get_channel(int(channel_id))

                if channel is None:
                    print(
                        f"Could not find channel for {deal['continent']} "
                        f"(ID: {channel_id}) — skipping this deal."
                    )
                    continue

                await channel.send(format_deal_message(deal))
                print(f"Posted {deal['continent']} deal to #{channel.name}.")
                posted_count += 1

            print(f"\nPosted {posted_count} of {len(deals)} deal(s) total.")

        await client.close()

    await client.start(DISCORD_BOT_TOKEN)


def main():
    placeholders = [
        TRAVELPAYOUTS_TOKEN,
        TRAVELPAYOUTS_MARKER,
        TRAVELPAYOUTS_TRS,
        DISCORD_BOT_TOKEN,
        DEFAULT_CHANNEL_ID,
        *CONTINENT_CHANNELS.values(),
    ]
    if any("PASTE_" in p for p in placeholders):
        print("Fill in the CONFIG section at the top of this file first.")
        return

    print("=== Checking routes for deals ===")
    deals = find_deals()

    print(f"\n=== Found {len(deals)} deal(s). Posting to Discord... ===")
    import asyncio

    asyncio.run(post_deals_to_discord(deals))


if __name__ == "__main__":
    main()
