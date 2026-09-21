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
import re
import time
from dataclasses import dataclass, field

from . import geo
from .benchmark import (MarketComp, MarketCurve, annotate_distances, fit_curve, read_results,
                        trim_tier)

logger = logging.getLogger(__name__)

SEARCH = (
    "https://www.cars.com/shopping/results/"
    "?makes[]={make}&models[]={slug}&zip={zip}&maximum_distance={distance}"
    "&stock_type={stock_type}&page_size=100&year_min={year_min}&list_price_max={price_max}"
    "&page={page}&include_shippable=false{extra}"
)
BLOCKED_JS = "() => /you have been blocked|attention required|just a moment/i.test(document.body.innerText.slice(0, 500))"


@dataclass
class DreamModel:
    label: str
    make: str
    slug: str
    year_min: int = 2022
    # Substrings, any of which marks the body style wanted. Empty = all.
    body_markers: tuple[str, ...] = ()
    # Trim rule for models whose titles carry a real trim. Titles that match
    # `trim_markers` pass; titles matching `trim_reject` are dropped; titles
    # matching NEITHER are dealer abbreviations ("PR", "PF") that cannot be
    # read, so they are kept and flagged rather than silently thrown away.
    trim_markers: tuple[str, ...] = ()
    trim_reject: tuple[str, ...] = ()
    odometer_max: int = 60000
    # Lorenzo's ceiling for a dream car. Applied at fetch time so the
    # per-model curve is fitted on cars he would actually consider, not on
    # $66k All-Terrains that then make a $46k one look like a bargain.
    price_max: int = 47000
    # Extra query string. Cars.com's body-style filter is the only way to
    # reach the E-Class wagons: unfiltered, the cheapest page is all sedans
    # and the pricier All-Terrains never appear.
    extra_query: str = ""
    # Miles from home, or None for nationwide. Cars.com accepts a numeric
    # maximum_distance, so this is a server-side filter, not a post-hoc one.
    radius_miles: int | None = None
    # Cars.com colour buckets, applied server-side (exterior_color_slugs[],
    # interior_color_slugs[]; several values OR together). The results page
    # names each car's exterior bucket but NOT its interior, so an interior
    # requirement can only be met by asking Cars.com for it up front.
    exterior_colors: tuple[str, ...] = ()
    interior_colors: tuple[str, ...] = ()
    # Cars.com's stock_type: "used" (the wagons and the used SUVs), "new",
    # "cpo", or "new_cpo" for a dealer's new and certified stock together.
    stock_type: str = "used"
    # Cars.com's trim facet (`trims[]`), verified 2026-09-21 on CX-50, CX-50
    # Hybrid and CX-70 new_cpo searches. Slugs are "<make>-<model>-<trim>"
    # as the facet lists them. With trims asked for server-side the 24-car
    # page can be sorted by price and still be all wanted trims, which the
    # client-side `trim_markers` rule on a "best match" page never was.
    trims: tuple[str, ...] = ()
    # Cars.com sort: "best_match_desc" (default), "list_price" (cheapest
    # first), "distance", "listed_at_desc", "best_deal".
    sort: str = ""
    # Pages to read, a fresh browser each (Cars.com blocks the second
    # request in a session); 24 listings a page.
    pages: int = 1


# Station wagons, every one. Cars.com's own body-style filter keeps sedans
# out for the models that also come as sedans (V60, A4, E-Class); the
# allroad and Cross Country badges are wagon-only, so they need no filter.
WAGON = "&body_style_slugs[]=wagon"

# Cars.com sweeps that belong on the SUV tab, not the wagon tab. Same
# fetcher, same caveats (no colour, no history, one request per session).
# XC60: Plus only. Cars.com abbreviates trims inconsistently ("B5 Plus",
# "Plus, B5 AWD"), so the marker is a whole-word "plus"; Core and Ultra
# have neither.
SUV_MODELS = [
    DreamModel("Volvo XC60 (Cars.com)", "volvo", "volvo-xc60", year_min=2024,
               radius_miles=300, odometer_max=35000, body_markers=("plus",)),
    # The CX-70 is an SUV and belongs beside the Hertz/Byers CX-70 watch, not
    # on the wagon tab. Cars.com adds the wider market but carries NO colour
    # on its result cards and blocks its detail pages, so these rows cannot be
    # colour- or trim-checked (Cars.com abbreviates trims to "PF"/"PR"), and
    # the page says so. Within driving range, capped like the rest.
    # Lorenzo: Premium and Premium Plus, for both 3.3 Turbo and Turbo S.
    # "premium" as a whole word covers "3.3 Turbo Premium", "Turbo Premium
    # Plus", "Turbo S Premium", "S Premium Plus"; Preferred is rejected.
    # Colours are the point of this watch: gray or blue outside, brown or
    # beige inside, in Cars.com's buckets. The CX-70's only non-black
    # interior below the red Nappa is Mazda's Tan, which dealers file as
    # either bucket, so both are asked for. With colours this narrow the
    # net is 500 miles, Lorenzo's original radius for this car.
    DreamModel("Mazda CX-70 (Cars.com)", "mazda", "mazda-cx_70", year_min=2024,
               radius_miles=500, odometer_max=35000,
               trim_markers=("premium",), trim_reject=("preferred",),
               exterior_colors=("gray", "blue"), interior_colors=("brown", "beige")),
]

# ---------------------------------------------------------------------------
# New and certified Mazda CX-50 / CX-70 (Lorenzo, 2026-09-21): Premium or
# better, ideally green or grey outside, terracotta inside. Within 300 mi,
# because a new car is bought in person and Columbus has three Mazda stores
# of its own; Cars.com adds the 20-odd dealers from Cincinnati to Cleveland,
# Pittsburgh, Detroit and Indianapolis. Trims are asked for server-side,
# cheapest first, two pages: on 2026-09-21 the CX-50 Turbo Premium Plus and
# Turbo Meridian in gray or green were 123 cars new+CPO within 300 mi, and
# 48 cheapest is the useful end. The Hybrid has no green and no terracotta
# (Cars.com: black or red only), so it is asked for in gray.
#
# The terracotta interior is Cars.com's "brown" bucket on the CX-50 (24 of
# the 123 above; Mazda's only brown-family CX-50 interior is terracotta,
# on the Turbo Meridian and Turbo Premium Plus). The CX-70's Tan Nappa is
# filed as "beige" (55) and once as "brown", so both are asked for there.
# Interior-filtered sweeps run alongside the colour-only ones so the rows
# the search demanded an interior for can carry the chip.
# ---------------------------------------------------------------------------
_CX50_TRIMS = ("mazda-cx_50-2.5_s_premium", "mazda-cx_50-2.5_s_premium_package",
               "mazda-cx_50-2.5_s_premium_plus_package",
               "mazda-cx_50-2.5_turbo_premium_package", "mazda-cx_50-2.5_turbo_premium_plus",
               "mazda-cx_50-2.5_turbo_premium_plus_package",
               "mazda-cx_50-2.5_turbo_meridian_edition")
_CX50H_TRIMS = ("mazda-cx_50_hybrid-premium_awd", "mazda-cx_50_hybrid-premium_package_awd",
                "mazda-cx_50_hybrid-premium_plus_awd", "mazda-cx_50_hybrid-premium_plus_package_awd")
_CX70_TRIMS = ("mazda-cx_70-3.3_turbo_premium_pack", "mazda-cx_70-3.3_turbo_premium_plus",
               "mazda-cx_70-3.3_turbo_premium_plus_pack", "mazda-cx_70-3.3_turbo_s_premium_pack",
               "mazda-cx_70-3.3_turbo_s_premium_plus_pack", "mazda-cx_70-premium_plus_package")

MAZDA_MODELS = [
    DreamModel("Mazda CX-50 (Cars.com)", "mazda", "mazda-cx_50", year_min=2025,
               radius_miles=300, odometer_max=30000, price_max=50000, stock_type="new_cpo",
               trims=_CX50_TRIMS, exterior_colors=("gray", "green"), sort="list_price", pages=2),
    DreamModel("Mazda CX-50 terracotta (Cars.com)", "mazda", "mazda-cx_50", year_min=2025,
               radius_miles=300, odometer_max=30000, price_max=50000, stock_type="new_cpo",
               trims=_CX50_TRIMS, exterior_colors=("gray", "green"), interior_colors=("brown",),
               sort="list_price", pages=1),
    DreamModel("Mazda CX-50 Hybrid (Cars.com)", "mazda", "mazda-cx_50_hybrid", year_min=2025,
               radius_miles=300, odometer_max=30000, price_max=50000, stock_type="new_cpo",
               trims=_CX50H_TRIMS, exterior_colors=("gray",), sort="list_price", pages=1),
    DreamModel("Mazda CX-70 (Cars.com)", "mazda", "mazda-cx_70", year_min=2025,
               radius_miles=300, odometer_max=30000, price_max=62000, stock_type="new_cpo",
               trims=_CX70_TRIMS, exterior_colors=("gray",), sort="list_price", pages=2),
    DreamModel("Mazda CX-70 tan (Cars.com)", "mazda", "mazda-cx_70", year_min=2025,
               radius_miles=300, odometer_max=30000, price_max=62000, stock_type="new_cpo",
               trims=_CX70_TRIMS, exterior_colors=("gray",), interior_colors=("beige", "brown"),
               sort="list_price", pages=1),
]

DREAM_MODELS = [
    DreamModel("Audi A6 allroad", "audi", "audi-a6_allroad"),
    DreamModel("Audi A4 allroad", "audi", "audi-a4_allroad", radius_miles=300),
    DreamModel("Volvo V90 Cross Country", "volvo", "volvo-v90_cross_country"),
    # "V60 Cross Country" is its own model on Cars.com, exactly as it is on
    # Hertz -- the same trap as CX-50 vs CX-50 Hybrid. The plain "volvo-v60"
    # slug with the wagon filter returned zero even uncapped.
    DreamModel("Volvo V60 Cross Country", "volvo", "volvo-v60_cross_country"),
    # Cars.com's own body-style filter does the wagon selection. A title
    # marker on top of it dropped 4 of 5 All-Terrains once titles came from
    # the vehicle array, where dealers write "E 450 4MATIC" and no more.
    DreamModel("Mercedes E-Class All-Terrain", "mercedes_benz", "mercedes_benz-e_class",
               extra_query=WAGON),
]


@dataclass
class DreamRow:
    model: str
    comp: MarketComp
    market_pct: float | None = None     # negative = below the model's own curve
    rating: str = ""
    interior: str = ""                  # the interior buckets the search asked for


@dataclass
class DreamBoard:
    rows: list[DreamRow] = field(default_factory=list)
    counts: dict = field(default_factory=dict)      # label -> listings seen
    curves: dict = field(default_factory=dict)      # label -> MarketCurve|None
    skipped: list[str] = field(default_factory=list)


def _fetch_page(cfg, model: DreamModel, page_no: int) -> tuple[list[MarketComp], int | None]:
    """One page of one model, one fresh browser. Cars.com's tolerance is per session."""
    from . import ingest
    extra = model.extra_query
    extra += "".join(f"&exterior_color_slugs[]={c}" for c in model.exterior_colors)
    extra += "".join(f"&interior_color_slugs[]={c}" for c in model.interior_colors)
    extra += "".join(f"&trims[]={t}" for t in model.trims)
    if model.sort:
        extra += f"&sort={model.sort}"
    url = SEARCH.format(make=model.make, slug=model.slug, zip=cfg.zip,
                        distance=model.radius_miles if model.radius_miles else "all",
                        year_min=model.year_min, price_max=model.price_max,
                        stock_type=model.stock_type or "used", page=page_no, extra=extra)
    with ingest.BrowserSession(headless=True, min_delay=1.0, max_delay=1.5,
                               postal_code=cfg.zip, city_state=cfg.city_state,
                               coordinates=cfg.coordinates) as session:
        with session.page() as page:
            session.load(page, url)
            page.wait_for_timeout(5000)
            if page.evaluate(BLOCKED_JS):
                raise RuntimeError("Cars.com served a Cloudflare challenge")
            return read_results(page)


def _fetch_model(cfg, model: DreamModel, pause_seconds: float = 40.0) -> list[MarketComp]:
    """Every page the model asks for, a fresh browser each, deduplicated."""
    comps: list[MarketComp] = []
    total = None
    for page_no in range(1, max(1, model.pages) + 1):
        if page_no > 1:
            time.sleep(pause_seconds)
        found, total = _fetch_page(cfg, model, page_no)
        if not found:
            break
        comps.extend(found)
        if len(found) < 20 or (total and len(comps) >= total):
            break
    logger.info("Cars.com %s: %d listings over %d page(s), site reports %s matching",
                model.label, len(comps), page_no, total)
    annotate_distances(comps, geo.parse_coordinates(cfg.coordinates))
    if model.body_markers:
        # Whole-word: the XC60 marker "plus" must not match "Premium Plus"
        # and "wagon" must not match "Wagoneer".
        comps = [c for c in comps
                 if any(re.search(rf"\b{re.escape(m)}\b", (c.title or "").lower())
                        for m in model.body_markers)]
    comps = [c for c in comps if c.year >= model.year_min
             and c.mileage <= model.odometer_max and c.price <= model.price_max]
    # The page's vehicle array carries the 24 organic results AND a tail of
    # "shippable" listings from anywhere (Cary NC at 379 mi, Minnesota at
    # 611 mi on a 300-mile search), whatever include_shippable says. The
    # radius is enforced here on the seller's zip; a row with no zip stays.
    if model.radius_miles:
        comps = [c for c in comps if c.distance is None or c.distance <= model.radius_miles]

    # Trim rule. A title that names a rejected trim is dropped; one that
    # names a wanted trim is kept as "ok"; one that names neither is a dealer
    # abbreviation ("PR", "PF") Cars.com passes through unparsed. Those are
    # kept as "unknown" and flagged on the page: 4 of 7 CX-70s within 300 mi
    # were abbreviated, and dropping them would hide half the market.
    if model.trim_markers or model.trim_reject:
        kept = []
        for c in comps:
            title = (c.title or "").lower()
            words = set(re.findall(r"[a-z0-9.]+", title))
            if any(r in words for r in model.trim_reject):
                continue
            # A separate field: the trim itself stays readable for the curve.
            c.trim_status = "ok" if any(m in words for m in model.trim_markers) else "unknown"
            kept.append(c)
        comps = kept

    # The same car often appears twice, listed through two dealer channels
    # with the dealer name blank on one. Price, mileage and year together
    # identify a physical car well enough for a ranked tab.
    seen: set = set()
    unique: list[MarketComp] = []
    for c in comps:
        key = c.vin or c.url or (c.year, c.price, c.mileage)
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
        # New cars have a sticker and are judged against it, never against
        # a curve, and they must not be in the used curve's sample either.
        used = [c for c in comps if c.stock_type != "new"]
        curve = fit_curve(used, min_n=6) if len(used) >= 6 else None
        board.curves[model.label] = curve
        logger.info("Dream tab: %s -> %d listings%s", model.label, len(comps),
                    f", curve n={curve.n}" if curve else ", no curve")

        interior = "/".join(model.interior_colors)
        for c in comps:
            pct = None
            if c.stock_type == "new":
                if c.msrp and c.price:
                    pct = 100.0 * (c.price - c.msrp) / c.msrp
            elif curve:
                # The curve was fitted with each comp's own trim tier, so
                # predict with it too; a Premium Plus is not priced as a Premium.
                predicted = curve.predict(c.mileage, c.age_years, trim_tier(c.trim, c.make),
                                          c.certified)
                pct = 100.0 * (c.price - predicted) / predicted
            board.rows.append(DreamRow(model.label, c, pct, c.rating, interior))

    # Cheapest against its own model's curve first; unpriced-by-curve last.
    board.rows.sort(key=lambda r: (r.market_pct is None, r.market_pct or 0.0, r.comp.price))
    return board


def build_suv(cfg, pause_seconds: float = 45.0) -> DreamBoard:
    """The Cars.com SUV sweep (XC60 Plus within 300 mi). Same machinery."""
    return build(cfg, pause_seconds=pause_seconds, models=SUV_MODELS)


def build_mazda(cfg, pause_seconds: float = 45.0) -> DreamBoard:
    """The new/CPO Mazda sweep for the Mazda tab. Same machinery."""
    return build(cfg, pause_seconds=pause_seconds, models=MAZDA_MODELS)


def to_json(board: DreamBoard) -> dict:
    """Plain data for the dashboard, one dict per listing.

    `seeded_at` is the document's own clock. It decides whether a committed
    seed out-ranks the runner's cache, and it must live in the file because
    a fresh checkout resets every mtime.
    """
    from . import clock
    return {
        "seeded_at": clock.now_iso(),
        "counts": board.counts,
        "skipped": board.skipped,
        "rows": [
            {
                "model": r.model, "year": r.comp.year, "title": r.comp.title,
                "price": r.comp.price, "mileage": r.comp.mileage,
                "city": r.comp.city, "state": r.comp.state, "distance": r.comp.distance,
                "dealer": r.comp.dealer, "rating": r.rating, "url": r.comp.url,
                # From the page's vehicle array: the VIN keys the row like a
                # dealer car; the colour is Cars.com's bucket, not the paint
                # name; the interior is what the search demanded, since the
                # array does not carry it.
                "vin": r.comp.vin, "color": r.comp.exterior, "interior": r.interior,
                "certified": r.comp.certified,
                # New cars: the sticker Cars.com carries, and the seller's
                # own stock word ("new", "used"); `market_pct` is then the
                # price against the sticker rather than against a curve.
                "msrp": r.comp.msrp or None,
                "stock_type": r.comp.stock_type,
                "market_pct": None if r.market_pct is None else round(r.market_pct, 1),
                # "ok" = named a wanted trim; "unknown" = dealer abbreviation
                # the title could not be read from; "" = no trim rule.
                "trim_status": r.comp.trim_status,
            }
            for r in board.rows
        ],
    }
