"""External market benchmark, from Cars.com.

The within-Hertz hedonic answers "is this cheap for a Hertz car?" It cannot
answer "is Hertz cheap?", because it only ever sees Hertz's own prices. If
Hertz priced its whole book high, every car would look fairly priced.

This module supplies the missing level: the same car, same year and trim,
listed by everyone else. Cars.com blocks plain HTTP but serves a real
browser, and each result card carries price, mileage, year, trim, dealer,
distance and Cars.com's own deal rating.

Two caveats that the comparison cannot remove, and which the board states:
franchise-dealer asking prices usually EXCLUDE a documentation fee and are
negotiable, while Hertz's price includes its doc fee and is no-haggle; and
every figure here is an asking price, not a transaction price.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

from . import geo

logger = logging.getLogger(__name__)

# One page per browser session. Cars.com sits behind Cloudflare and blocks
# the second request in a session ("Sorry, you have been blocked"). It also
# clamps page_size to 24, whatever is asked, so one request is 24 listings
# in Cars.com's "best match" order. The listings are read from the page's
# own data, not from the cards: see VEHICLE_ARRAY.
SEARCH = (
    "https://www.cars.com/shopping/results/"
    "?makes[]={make}&models[]={model}&zip={zip}&maximum_distance={radius}"
    "&stock_type=used&page_size=100&page={page}{extra}"
)

BLOCKED = re.compile(r"you have been blocked|security service to protect", re.I)

# Trim ladders, base < mid < top. Hertz stocks only Premium Plus CX-50s
# while the open market is mostly Preferred, so a curve that ignores trim
# compares Hertz's top trim against everyone else's base trim and makes
# Hertz look expensive. Volvo's ladder is Core < Plus < Ultra. Whole-word,
# and "premium plus" before "plus", so a Premium Plus is never read as a
# Volvo Plus. Unknown trims sit in the middle.
TRIM_TIERS = (
    ("premium plus", 2),
    ("premium", 1),
    ("preferred", 0),
    ("ultra", 2),
    ("plus", 1),
    ("core", 0),
)
_TIER_RE = [(re.compile(rf"\b{re.escape(needle)}\b"), tier) for needle, tier in TRIM_TIERS]


def trim_tier(trim: str) -> int:
    """0 base, 1 mid, 2 top, on either ladder. Unknown trims sit in the middle."""
    text = (trim or "").strip().lower()
    for pattern, tier in _TIER_RE:
        if pattern.search(text):
            return tier
    return 1

# One results card, as rendered text. Example:
#   $38,548 / 6,736 mi. / Est. $710/mo
#   Used 2025 Mazda CX-50 Hybrid Premium Package / Fair Deal
#   Ray Skillman Mazda West / Indianapolis, IN (170 mi)
CARD_TEXT = """
() => [...document.querySelectorAll('fuse-card')]
        .map(c => (c.innerText || '').trim())
        .filter(t => /^\\$[\\d,]+/.test(t) && /\\bmi\\./.test(t))
"""

# Text plus the listing link. The text-only reader above made every dream
# row unclickable, because nothing ever captured the URL.
CARD_TEXT_LINKS = """
() => [...document.querySelectorAll('fuse-card')]
        .map(c => {
            const a = c.querySelector('a[href*="/vehicledetail/"]') || c.querySelector('a[href]');
            return {text: (c.innerText || '').trim(), href: a ? a.getAttribute('href') : null};
        })
        .filter(o => /^\\$[\\d,]+/.test(o.text) && /\\bmi\\./.test(o.text))
"""

# The results page carries every listing it shows as JSON on the
# <search-provider> element: trim as the dealer wrote it, VIN, year, price,
# mileage, exterior colour, seller name and zip, listing id, CPO flag. The
# cards themselves render in shadow DOM and only a handful ever expose text,
# which is why the card reader below found 6 of 24 and every curve was fitted
# on a dozen cars. The array is the source; the cards are the fallback.
VEHICLE_ARRAY = """
() => { const el = document.querySelector('search-provider[data-vehicle-array]');
        return el ? el.getAttribute('data-vehicle-array') : null; }
"""
RESULT_TOTAL = """
() => { const s = document.getElementById('CarsWeb.SearchController.index');
        if (!s) return null;
        try { return JSON.parse(s.textContent).srp_results.metadata.total_listings; }
        catch (e) { return null; } }
"""

PRICE = re.compile(r"^\$([\d,]+)")
MILEAGE = re.compile(r"([\d,]+)\s*mi\.")
TITLE = re.compile(r"(?:Used|New|Certified)\s+(\d{4})\s+(.+)")
RATING = re.compile(r"\b(Great|Good|Fair|High)\s+Deal\b", re.I)
LOCATION = re.compile(r"^(.*),\s*([A-Z]{2})\s*\((\d+)\s*mi\)", re.M)


@dataclass
class MarketComp:
    price: int
    mileage: int
    year: int
    title: str
    trim: str = ""
    dealer: str = ""
    city: str = ""
    state: str = ""
    distance: int | None = None
    rating: str = ""
    url: str = ""
    vin: str = ""
    exterior: str = ""       # Cars.com's colour bucket ("gray"), not the paint name
    seller_zip: str = ""
    certified: bool = False
    trim_status: str = ""    # dream/SUV sweeps: "ok", "unknown" or "" (no rule)

    @property
    def age_years(self) -> float:
        from datetime import date
        today = date.today()
        return max(0.0, today.year + (today.month - 1) / 12.0 - (self.year - 0.5))


def parse_card(text: str) -> MarketComp | None:
    price = PRICE.search(text)
    mileage = MILEAGE.search(text)
    if not (price and mileage):
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title_line = next((l for l in lines if TITLE.match(l)), "")
    title_match = TITLE.match(title_line) if title_line else None
    if not title_match:
        return None

    year = int(title_match.group(1))
    full_title = title_match.group(2).strip()

    rating = RATING.search(text)
    location = LOCATION.search(text)

    # The dealer name sits just above the "City, ST (N mi)" line, but cards
    # often interleave a bare star rating ("4.5") and a review count, so walk
    # back past anything that is only digits and punctuation.
    dealer = ""
    if location:
        try:
            index = next(i for i, l in enumerate(lines)
                         if location.group(0).startswith(l[:18]))
            for candidate in reversed(lines[:index]):
                if re.fullmatch(r"[\d.,()\s]*(reviews?)?", candidate, re.I):
                    continue
                if RATING.search(candidate):
                    continue
                dealer = candidate
                break
        except StopIteration:
            dealer = ""

    return MarketComp(
        price=int(price.group(1).replace(",", "")),
        mileage=int(mileage.group(1).replace(",", "")),
        year=year,
        title=full_title,
        # The whole title. It used to be cut at "Hybrid", which left every
        # non-hybrid comp with an empty trim and put all XC60s and CX-70s in
        # the middle tier, so the curve never saw a trim step.
        trim=full_title,
        dealer=dealer,
        city=location.group(1).strip() if location else "",
        state=location.group(2) if location else "",
        distance=int(location.group(3)) if location else None,
        rating=(rating.group(1).title() + " Deal") if rating else "",
    )


def parse_vehicle_array(raw: str) -> list[MarketComp]:
    """The <search-provider> array, one comp per distinct car.

    Sponsored placements repeat a car that is also in the organic results,
    so the VIN (or listing id) is the identity. Numbers arrive as strings.
    """
    try:
        data = json.loads(raw or "null")
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out: list[MarketComp] = []
    seen: set[str] = set()
    for v in data:
        if not isinstance(v, dict):
            continue
        key = v.get("vin") or v.get("listingId") or ""
        if not key or key in seen:
            continue
        try:
            price = int(float(v.get("price") or 0))
            mileage = int(float(v.get("mileage") or 0))
            year = int(v.get("year") or 0)
        except (TypeError, ValueError):
            continue
        if not (price and year):
            continue
        seen.add(key)
        seller = v.get("seller") or {}
        trim = (v.get("trim") or "").strip()
        title = " ".join(x for x in (v.get("make"), v.get("model"), trim) if x)
        listing_id = v.get("listingId") or ""
        out.append(MarketComp(
            price=price, mileage=mileage, year=year, title=title, trim=trim,
            dealer=(seller.get("dealerName") or "").strip(),
            url=f"https://www.cars.com/vehicledetail/{listing_id}/" if listing_id else "",
            vin=(v.get("vin") or "").strip().upper(),
            exterior=(v.get("exteriorColor") or "").strip().lower(),
            seller_zip=str(seller.get("zip") or "").strip()[:5],
            certified=bool(v.get("cpoIndicator")),
        ))
    return out


def annotate_distances(comps: list[MarketComp], home: tuple[float, float] | None) -> None:
    """Miles from home, and city/state, via the seller's zip.

    The array has neither a distance nor a place, only the seller's zip; one
    cached lookup per zip supplies both.
    """
    for c in comps:
        if not c.seller_zip:
            continue
        if not (c.city or c.state):
            place = geo.place_for_zip(c.seller_zip)
            if place:
                c.city, c.state = place
        if home and c.distance is None:
            coords = geo.coordinates_for_zip(c.seller_zip)
            if coords:
                c.distance = int(round(geo.haversine_miles(home, coords)))


def read_results(page) -> tuple[list[MarketComp], int | None]:
    """Everything the loaded results page knows: (comps, total matching).

    The vehicle array first; the visible cards only if the array is missing,
    which would mean Cars.com changed its page again.
    """
    total = None
    try:
        total = page.evaluate(RESULT_TOTAL)
    except Exception:
        pass
    comps: list[MarketComp] = []
    try:
        comps = parse_vehicle_array(page.evaluate(VEHICLE_ARRAY))
    except Exception as exc:
        logger.warning("Cars.com vehicle array unreadable: %s", exc)
    if comps:
        return comps, total
    logger.warning("Cars.com page had no vehicle array; falling back to the cards")
    for card in page.evaluate(CARD_TEXT_LINKS) or []:
        comp = parse_card(card.get("text") or "")
        if comp is None:
            continue
        href = card.get("href") or ""
        if href.startswith("/"):
            href = "https://www.cars.com" + href
        comp.url = href.split("?")[0] if href else ""
        comps.append(comp)
    return comps, total


def fetch_comps(session, make: str, model: str, zip_code: str,
                radius: str | int = "all", max_pages: int = 1,
                year_min: int | None = None, extra: str = "") -> list[MarketComp]:
    """Collect market listings for one model. `radius` accepts "all".

    `year_min` is applied server-side and matters more than it looks. An
    unfiltered page is mostly older, higher-mileage cars, and a log-linear
    age slope fitted on 2019-2022 cars extrapolates a 2025 to above its
    MSRP: that curve priced a $36k CarMax XC60 as 17% "below market".
    Fitting on the years the watch actually wants keeps the curve where the
    data are.

    Defaults to a single page: see SEARCH above on why paging gets blocked.
    """
    comps: list[MarketComp] = []
    seen: set[tuple] = set()
    if year_min:
        extra = f"&year_min={int(year_min)}{extra}"

    for page in range(1, max_pages + 1):
        url = SEARCH.format(make=make, model=model, zip=zip_code, radius=radius,
                            page=page, extra=extra)
        try:
            with session.page() as p:
                session.load(p, url)
                p.wait_for_timeout(5000)
                if BLOCKED.search(p.evaluate("() => document.body.innerText.slice(0, 400)")):
                    logger.warning(
                        "Cars.com blocked the request at page %d; keeping the %d comps "
                        "already collected rather than retrying.", page, len(comps))
                    break
                found, total = read_results(p)
        except Exception as exc:
            logger.warning("Cars.com page %d failed: %s", page, exc)
            break

        if not found:
            break

        fresh = 0
        for comp in found:
            key = comp.vin or (comp.price, comp.mileage, comp.year, comp.dealer)
            if key in seen:
                continue
            seen.add(key)
            comps.append(comp)
            fresh += 1

        logger.info("Cars.com page %d: %d listings, %d new (%d total, site reports %s)",
                    page, len(found), fresh, len(comps), total)
        if not fresh:
            break

    return comps


@dataclass
class MarketCurve:
    """log(price) = a + b*(mileage/10k) + c*age + d*trim_tier, on market listings.

    The trim term matters more than it looks: Hertz stocks only Premium Plus
    while the open market is mostly Preferred, so omitting it would price
    Hertz's top trim against everyone else's base trim.
    """

    intercept: float
    per_10k_miles: float
    per_year: float
    per_trim_tier: float
    n: int
    rmse: float
    median_price: int
    median_mileage: int
    trim_counts: dict

    def predict(self, mileage: int, age_years: float, tier: int = 2) -> float:
        return math.exp(
            self.intercept
            + self.per_10k_miles * (mileage / 10000.0)
            + self.per_year * age_years
            + self.per_trim_tier * tier
        )


def fit_curve(comps: list[MarketComp], min_n: int = 10) -> MarketCurve | None:
    """Ordinary least squares on the market sample. Needs a real sample.

    `min_n` is the floor for a benchmark that gates alerts (10). The dream
    tab, which gates nothing, passes 6: three parameters on six points is
    thin but honest for a ranking.
    """
    usable = [c for c in comps if c.price > 5000 and c.mileage >= 0]
    if len(usable) < min_n:
        logger.warning("Only %d usable market comps (need %d); not fitting a curve",
                       len(usable), min_n)
        return None

    rows = [(1.0, c.mileage / 10000.0, c.age_years, float(trim_tier(c.trim)),
             math.log(c.price)) for c in usable]
    k = 4
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x, y = r[:k], r[k]
        for i in range(k):
            xty[i] += x[i] * y
            for j in range(k):
                xtx[i][j] += x[i] * x[j]

    # Small ridge term keeps the 3x3 solvable when mileage and age are collinear.
    for i in range(1, k):
        xtx[i][i] += 1e-6 * len(rows)

    from .score import _solve
    beta = _solve(xtx, xty)
    if beta is None:
        return None

    residuals = [r[k] - sum(x * b for x, b in zip(r[:k], beta)) for r in rows]
    rmse = math.sqrt(sum(e * e for e in residuals) / len(residuals))
    prices = sorted(c.price for c in usable)
    miles = sorted(c.mileage for c in usable)

    counts: dict = {}
    for c in usable:
        key = ("base", "mid", "top")[trim_tier(c.trim)]
        counts[key] = counts.get(key, 0) + 1

    logger.info(
        "Market curve on %d Cars.com listings: %.1f%% per 10k miles, %.1f%% per year, "
        "%.1f%% per trim step, RMSE %.3f, trims %s",
        len(usable), 100 * (math.exp(beta[1]) - 1), 100 * (math.exp(beta[2]) - 1),
        100 * (math.exp(beta[3]) - 1), rmse, counts,
    )
    return MarketCurve(beta[0], beta[1], beta[2], beta[3], len(usable), rmse,
                       prices[len(prices) // 2], miles[len(miles) // 2], counts)


def market_gap(listing, curve: MarketCurve | None) -> tuple[float, float] | None:
    """(dollars below market, percent below market) for one Hertz listing."""
    if curve is None or not listing.price or listing.odometer is None:
        return None
    age = listing.age_years
    if age is None:
        return None
    predicted = curve.predict(listing.odometer, age, trim_tier(listing.trim))
    gap = predicted - float(listing.price)
    return gap, 100.0 * gap / predicted
