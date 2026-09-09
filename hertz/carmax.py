"""CarMax ingestion.

CarMax is not a Dealer.com site, so it needs its own reader. The important
discovery is not the markup but the browser: CarMax's bot check rejects
Playwright's bundled Chromium with a hard "Access Denied", and passes real
Chrome (``channel="chrome"``) from the same machine and the same IP. It is
headless-fingerprint detection, not an IP block, which is why the site loads
perfectly well in an ordinary browser window.

CarMax also prices shipping per car and shows it on the results page, so the
landed cost is available without opening anything. Their fees are small and
banded ($149-$349 nationally) rather than per-mile, which makes distance
close to irrelevant for a CarMax car: it ships to you either way.
"""
from __future__ import annotations

import logging
import re

from .models import Listing

logger = logging.getLogger(__name__)

SEARCH = "https://www.carmax.com/cars/{make}/{model}?zip={zip}&distance=all"
CARD_SELECTOR = ".kmx-car-tile__content"

CARD_TEXT = """
() => [...document.querySelectorAll('.kmx-car-tile__content')].map(c => ({
    text: (c.innerText || '').trim(),
    href: (c.querySelector('a[href*="/car/"]') || {}).getAttribute
          ? c.querySelector('a[href*="/car/"]').getAttribute('href') : null
}))
"""

TITLE = re.compile(r"^(\d{4})\s+(\S+)\s+(\S+)\s*(.*)$")
PRICE = re.compile(r"\$([\d,]+)\*?")
MILEAGE = re.compile(r"([\d,.]+)\s*(K?)\s*mi\b", re.I)
SHIPPING = re.compile(r"\$([\d,]+)\s*Shipping", re.I)
STORE = re.compile(r"CarMax\s+(.+?),\s*([A-Z]{2})")


def parse_tile(text: str, href: str | None) -> Listing | None:
    """Turn one result tile into a Listing.

    Example tile:
        2025 Volvo XC60 B5 Plus
        $39,998*
        23K mi
        $149 Shipping | Est. arrival 9/12-9/15
        CarMax Florence, KY
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None

    # Current tile layout (2026-09), one field per line:
    #   Compare / 2023 Volvo XC60 / B5 Plus Bright Theme / · / 24K mi /
    #   Test drive today·Columbus Easton  -or-  $149 shipping·Sep 12–15 from KY /
    #   Est. $560/mo / · / $33,998
    # So: the title line is not first, the trim is on the line AFTER the
    # title, and the tile carries three dollar figures of which only the last
    # is the price. Taking the first "$" silently returned the monthly
    # payment as the price, which is how a $33,998 car scored as $560.
    title_index = None
    for i, line in enumerate(lines[:6]):
        m = TITLE.match(line)
        if m and 1990 <= int(m.group(1)) <= 2100:
            title_index, title = i, m
            break
    if title_index is None:
        return None
    year, make, model, trim = title.groups()
    trim = trim.strip()
    if not trim and title_index + 1 < len(lines):
        candidate = lines[title_index + 1]
        if (candidate not in ("·", "|") and not MILEAGE.search(candidate)
                and "$" not in candidate
                and not candidate.lower().startswith(("compare", "test drive", "est."))):
            trim = candidate

    # Price: the largest dollar figure that is neither a monthly estimate
    # nor a shipping fee.
    amounts = []
    for m in re.finditer(r"\$([\d,]+)", text):
        tail = text[m.end(): m.end() + 14].lower()
        if "/mo" in tail or "shipping" in tail:
            continue
        amounts.append(int(m.group(1).replace(",", "")))
    mileage = MILEAGE.search(text)
    if not (amounts and mileage):
        return None
    price_value = max(amounts)

    miles = float(mileage.group(1).replace(",", ""))
    if mileage.group(2).upper() == "K":
        miles *= 1000

    shipping = SHIPPING.search(text)
    local_store = re.search(r"Test drive today\s*[·|-]\s*(.+)", text, re.I)
    ship_from = re.search(r"shipping[^\n]*?\bfrom\s+([A-Z]{2})\b", text, re.I)
    store = STORE.search(text)   # older layout: "CarMax Florence, KY"

    if local_store:
        lot, city, state, ship_fee = f"CarMax {local_store.group(1).strip()}", local_store.group(1).strip(), "OH", 0
    elif store:
        lot, city, state = f"CarMax {store.group(1)}, {store.group(2)}", store.group(1), store.group(2)
        ship_fee = int(shipping.group(1).replace(",", "")) if shipping else None
    else:
        state = ship_from.group(1).upper() if ship_from else ""
        lot, city = (f"CarMax (ships from {state})" if state else "CarMax"), ""
        ship_fee = int(shipping.group(1).replace(",", "")) if shipping else None

    # CarMax listing URLs are /car/<id>; the id is not a VIN, so synthesise a
    # stable key from it. Every other source is keyed on VIN, and mixing the
    # two would let the same car appear twice, so the prefix keeps them apart.
    listing_id = (href or "").rstrip("/").split("/")[-1]
    if not listing_id:
        return None

    return Listing(
        vin=f"CARMAX-{listing_id}".ljust(17, "0")[:17],
        year=int(year),
        make=make,
        model=model,
        trim=trim.strip(),
        price=price_value,
        no_haggle_price=None,
        odometer=int(miles),
        geodist=0.0,          # CarMax ships nationally; shipping is the cost
        lot=lot,
        city=city,
        state=state,
        delivery_quote=ship_fee,
        status="live",
        source="carmax",
        url=("https://www.carmax.com" + href) if href and href.startswith("/") else (href or ""),
    )


def fetch(session, make: str, model: str, zip_code: str, max_scrolls: int = 3) -> list[Listing]:
    """Fetch CarMax listings for one make/model.

    Requires a session running real Chrome; bundled Chromium is refused.
    """
    url = SEARCH.format(make=make.lower(), model=model.lower(), zip=zip_code)
    logger.info("Fetching %s %s from CarMax", make, model)

    try:
        with session.page() as page:
            session.load(page, url)
            page.wait_for_timeout(7000)
            # Results lazy-load on scroll.
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(2500)
            tiles = page.evaluate(CARD_TEXT)
    except Exception as exc:
        logger.warning("CarMax fetch failed for %s %s: %s", make, model, exc)
        return []

    listings: list[Listing] = []
    seen: set[str] = set()
    for tile in tiles:
        listing = parse_tile(tile.get("text") or "", tile.get("href"))
        if listing and listing.vin not in seen:
            seen.add(listing.vin)
            listings.append(listing)

    if not listings and tiles:
        # Make the failure diagnosable from the log alone: show what a tile
        # actually looked like, since the runner sees a different page
        # variant from a desktop browser.
        sample = (tiles[0].get("text") or "").replace("\n", " | ")[:220]
        logger.warning("CarMax returned %d tiles but none parsed; first tile: %r",
                       len(tiles), sample)
    logger.info("CarMax: %d listings for %s %s", len(listings), make, model)
    return listings
