"""The dream-car tab: aspirational wagons, ranked from the open market.

These are not watches. Nothing here alerts, nothing is gated on AutoCheck,
and nothing is scored against Hertz. It is a ranked window on three models
Lorenzo would buy if the right one appeared, refreshed from Cars.com with a
fresh browser per model, because Cars.com tolerates one request per browser
session and Cloudflare-blocks the second.

Body style is the awkward part. Cars.com's E-Class slug returns every body
and the result card carries no body-style field, so the wagon is recognised
from the title ("E 450 4MATIC Wagon", "All-Terrain"). The A6 allroad and the
V90 Cross Country are wagon-only models, so they need no such filter.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .benchmark import CARD_TEXT, MarketComp, MarketCurve, fit_curve, parse_card

logger = logging.getLogger(__name__)

SEARCH = (
    "https://www.cars.com/shopping/results/"
    "?makes[]={make}&models[]={slug}&zip={zip}&maximum_distance=all"
    "&stock_type=used&page_size=100&year_min={year_min}&list_price_max={price_max}{extra}"
)
BLOCKED_JS = "() => /you have been blocked|attention required/i.test(document.body.innerText.slice(0, 500))"


@dataclass
class DreamModel:
    label: str
    make: str
    slug: str
    year_min: int = 2022
    # Substrings, any of which marks the body style wanted. Empty = all.
    body_markers: tuple[str, ...] = ()
    odometer_max: int = 60000
    # Lorenzo's ceiling for a dream car. Applied at fetch time so the
    # per-model curve is fitted on cars he would actually consider, not on
    # $66k All-Terrains that then make a $46k one look like a bargain.
    price_max: int = 47000
    # Extra query string. Cars.com's body-style filter is the only way to
    # reach the E-Class wagons: unfiltered, the cheapest page is all sedans
    # and the pricier All-Terrains never appear.
    extra_query: str = ""


DREAM_MODELS = [
    DreamModel("Audi A6 allroad", "audi", "audi-a6_allroad"),
    DreamModel("Volvo V90 Cross Country", "volvo", "volvo-v90_cross_country"),
    DreamModel("Mercedes E-Class All-Terrain", "mercedes_benz", "mercedes_benz-e_class",
               body_markers=("wagon", "all-terrain", "all terrain", "estate"),
               extra_query="&body_style_slugs[]=wagon"),
]


@dataclass
class DreamRow:
    model: str
    comp: MarketComp
    market_pct: float | None = None     # negative = below the model's own curve
    rating: str = ""


@dataclass
class DreamBoard:
    rows: list[DreamRow] = field(default_factory=list)
    counts: dict = field(default_factory=dict)      # label -> listings seen
    curves: dict = field(default_factory=dict)      # label -> MarketCurve|None
    skipped: list[str] = field(default_factory=list)


def _fetch_model(cfg, model: DreamModel) -> list[MarketComp]:
    """One model, one fresh browser. Cars.com's tolerance is per session."""
    from . import ingest
    url = SEARCH.format(make=model.make, slug=model.slug, zip=cfg.zip,
                        year_min=model.year_min, price_max=model.price_max,
                        extra=model.extra_query)
    with ingest.BrowserSession(headless=True, min_delay=1.0, max_delay=1.5,
                               postal_code=cfg.zip, city_state=cfg.city_state,
                               coordinates=cfg.coordinates) as session:
        with session.page() as page:
            session.load(page, url)
            page.wait_for_timeout(5000)
            if page.evaluate(BLOCKED_JS):
                raise RuntimeError("Cars.com served a Cloudflare challenge")
            cards = page.evaluate(CARD_TEXT)
    comps = [c for c in (parse_card(t) for t in cards) if c]
    if model.body_markers:
        comps = [c for c in comps
                 if any(m in (c.title or "").lower() for m in model.body_markers)]
    comps = [c for c in comps if c.year >= model.year_min
             and c.mileage <= model.odometer_max and c.price <= model.price_max]

    # The same car often appears twice, listed through two dealer channels
    # with the dealer name blank on one. Price, mileage and year together
    # identify a physical car well enough for a ranked tab.
    seen: set[tuple] = set()
    unique: list[MarketComp] = []
    for c in comps:
        key = (c.year, c.price, c.mileage)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def build(cfg, pause_seconds: float = 45.0, models: list[DreamModel] | None = None) -> DreamBoard:
    """Fetch every dream model, fit each its own curve, rank within model.

    A model that fails (usually a Cloudflare challenge) is skipped and
    named, never allowed to abort the others.
    """
    board = DreamBoard()
    for i, model in enumerate(models or DREAM_MODELS):
        if i:
            time.sleep(pause_seconds)
        try:
            comps = _fetch_model(cfg, model)
        except Exception as exc:
            logger.warning("Dream tab: skipping %s: %s", model.label, exc)
            board.skipped.append(model.label)
            continue
        if not comps:
            # A wagon-only model with zero listings nationwide is not a
            # market fact, it is a throttled page. Keep the last good rows
            # rather than publishing an empty tab.
            logger.warning("Dream tab: %s returned nothing; treating as skipped", model.label)
            board.skipped.append(model.label)
            continue

        board.counts[model.label] = len(comps)
        # Three parameters on six or more points is a thin but honest
        # within-model curve; below that the residual column stays blank.
        curve = fit_curve(comps, min_n=6) if len(comps) >= 6 else None
        board.curves[model.label] = curve
        logger.info("Dream tab: %s -> %d listings%s", model.label, len(comps),
                    f", curve n={curve.n}" if curve else ", no curve")

        for c in comps:
            pct = None
            if curve:
                predicted = curve.predict(c.mileage, c.age_years, 1)
                pct = 100.0 * (c.price - predicted) / predicted
            board.rows.append(DreamRow(model.label, c, pct, c.rating))

    # Cheapest against its own model's curve first; unpriced-by-curve last.
    board.rows.sort(key=lambda r: (r.market_pct is None, r.market_pct or 0.0, r.comp.price))
    return board


def to_json(board: DreamBoard) -> dict:
    """Plain data for the dashboard, one dict per listing.

    `seeded_at` is the document's own clock. It decides whether a committed
    seed out-ranks the runner's cache, and it must live in the file because
    a fresh checkout resets every mtime.
    """
    from datetime import datetime
    return {
        "seeded_at": datetime.now().isoformat(timespec="seconds"),
        "counts": board.counts,
        "skipped": board.skipped,
        "rows": [
            {
                "model": r.model, "year": r.comp.year, "title": r.comp.title,
                "price": r.comp.price, "mileage": r.comp.mileage,
                "city": r.comp.city, "state": r.comp.state, "distance": r.comp.distance,
                "dealer": r.comp.dealer, "rating": r.rating,
                "market_pct": None if r.market_pct is None else round(r.market_pct, 1),
            }
            for r in board.rows
        ],
    }
