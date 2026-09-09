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

import logging
import math
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# One deep page rather than several shallow ones. Cars.com sits behind
# Cloudflare and blocks on the second page request ("Sorry, you have been
# blocked"), but serves page_size=100 happily. For a model this new that is
# effectively the whole national market in a single request.
SEARCH = (
    "https://www.cars.com/shopping/results/"
    "?makes[]={make}&models[]={model}&zip={zip}&maximum_distance={radius}"
    "&stock_type=used&page_size=100&page={page}"
)

BLOCKED = re.compile(r"you have been blocked|security service to protect", re.I)

# CX-50 Hybrid trim ladder. Hertz stocks only Premium Plus, while the open
# market is mostly Preferred, so a curve that ignores trim compares Hertz's
# top trim against everyone else's base trim and makes Hertz look expensive.
TRIM_TIERS = (
    ("premium plus", 2),
    ("premium", 1),
    ("preferred", 0),
)


def trim_tier(trim: str) -> int:
    """0 Preferred, 1 Premium, 2 Premium Plus. Unknown trims sit in the middle."""
    text = (trim or "").strip().lower()
    for needle, tier in TRIM_TIERS:
        if needle in text:
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
        trim=full_title.split("Hybrid")[-1].strip() if "Hybrid" in full_title else "",
        dealer=dealer,
        city=location.group(1).strip() if location else "",
        state=location.group(2) if location else "",
        distance=int(location.group(3)) if location else None,
        rating=(rating.group(1).title() + " Deal") if rating else "",
    )


def fetch_comps(session, make: str, model: str, zip_code: str,
                radius: str | int = "all", max_pages: int = 1) -> list[MarketComp]:
    """Collect market listings for one model. `radius` accepts "all".

    Defaults to a single page: see SEARCH above on why paging gets blocked.
    """
    comps: list[MarketComp] = []
    seen: set[tuple] = set()

    for page in range(1, max_pages + 1):
        url = SEARCH.format(make=make, model=model, zip=zip_code, radius=radius, page=page)
        try:
            with session.page() as p:
                session.load(p, url)
                p.wait_for_timeout(5000)
                if BLOCKED.search(p.evaluate("() => document.body.innerText.slice(0, 400)")):
                    logger.warning(
                        "Cars.com blocked the request at page %d; keeping the %d comps "
                        "already collected rather than retrying.", page, len(comps))
                    break
                cards = p.evaluate(CARD_TEXT)
        except Exception as exc:
            logger.warning("Cars.com page %d failed: %s", page, exc)
            break

        if not cards:
            break

        fresh = 0
        for text in cards:
            comp = parse_card(text)
            if comp is None:
                continue
            key = (comp.price, comp.mileage, comp.year, comp.dealer)
            if key in seen:
                continue
            seen.add(key)
            comps.append(comp)
            fresh += 1

        logger.info("Cars.com page %d: %d cards, %d new (%d total)",
                    page, len(cards), fresh, len(comps))
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


def fit_curve(comps: list[MarketComp]) -> MarketCurve | None:
    """Ordinary least squares on the market sample. Needs a real sample."""
    usable = [c for c in comps if c.price > 5000 and c.mileage >= 0]
    if len(usable) < 10:
        logger.warning("Only %d usable market comps; not fitting a curve", len(usable))
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
        key = ("Preferred", "Premium", "Premium Plus")[trim_tier(c.trim)]
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
