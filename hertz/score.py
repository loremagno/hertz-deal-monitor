"""Deal economics: landed cost, a hedonic price model, and the alert gates.

Two distinct questions get two distinct numbers, and conflating them is the
usual way these tools go wrong:

  * "Is this car cheap for what it is?"  -> residual from the hedonic, fitted
    on the Hertz Price, which is location-independent apart from the doc fee.
  * "What will it actually cost me?"     -> landed cost, which adds delivery,
    Ohio sales tax, and title/registration.

Alerts fire on the first. Ranking and comparison use the second. Fitting the
hedonic on landed cost instead would confound cheapness with distance.

The regression is plain OLS with a ridge penalty, implemented here in pure
Python. On a few hundred rows that is instant, and it keeps the tool free of
compiled dependencies.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from .config import Config, WatchEntry
from .models import AutoCheck, Listing, Scored

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Landed cost
# ---------------------------------------------------------------------------

def delivery_cost(listing: Listing, cfg: Config) -> float:
    """Hertz's own quote when we have it, else the calibrated linear fallback.

    The fallback slope comes from two live quotes on 2026-09-08:
    86 mi -> $316 and 1,656 mi -> $3,464, i.e. about $2.00/mile over a ~$145
    base. Delivery is emphatically not flat.

    Rent2Buy cars are not delivered at all -- you collect them from the rental
    branch -- so their delivery cost is zero and the trip is your own.
    """
    if listing.is_rent2buy:
        return 0.0
    if listing.delivery_quote is not None:
        return float(listing.delivery_quote)
    if listing.geodist is None:
        return 0.0
    return cfg.delivery_base + cfg.delivery_per_mile * float(listing.geodist)


def landed_cost(listing: Listing, cfg: Config) -> tuple[float, float, float, float]:
    """Return (delivery, tax, fees, landed_total).

    Ohio does not tax the dealer documentation fee, so the taxable base is the
    no-haggle price rather than the quoted Hertz Price. On a $387 Ohio doc fee
    that is only about $29, but it is free to get right and the doc fee runs
    to $999 in some states.
    """
    price = float(listing.price or 0)
    delivery = delivery_cost(listing, cfg)

    taxable = price
    if cfg.tax_excludes_doc_fee and listing.no_haggle_price:
        taxable = float(listing.no_haggle_price)
    if cfg.tax_applies_to_delivery:
        taxable += delivery

    tax = taxable * cfg.sales_tax_rate
    fees = float(cfg.title_reg_fees)
    return delivery, tax, fees, price + delivery + tax + fees


# ---------------------------------------------------------------------------
# Hedonic price model
# ---------------------------------------------------------------------------

def _normalize_model(listing: Listing) -> str:
    return f"{listing.make.strip().lower()}|{listing.model.strip().lower()}"


# An ordinal trim ladder, coarse on purpose. Manufacturers name trims
# differently, but almost all of them stack roughly base -> mid -> loaded, and
# a single ordinal term removes most of the trim variation that would
# otherwise sit in the error term and masquerade as a bargain.
_TRIM_LADDER = (
    (("plus ultimate", "premium plus", "ultra", "signature", "platinum",
      "calligraphy", "type s", "grand touring"), 3),
    (("premium", "plus", "limited", "sel premium", "touring", "xle", "ultimate"), 2),
    (("preferred", "select", "sel", "core", "sport", "s ", "le", "se"), 1),
)


def trim_tier(trim: str) -> int:
    """0 unknown/base, rising to 3 for a marque's loaded trim."""
    text = f" {(trim or '').strip().lower()} "
    for needles, tier in _TRIM_LADDER:
        if any(n in text for n in needles):
            return tier
    return 0


@dataclass
class Hedonic:
    """Fitted log-price surface plus the bookkeeping needed to apply it."""

    coefficients: list[float]
    model_keys: list[str]
    counts: dict[str, int]
    n_obs: int
    rmse: float

    def features(self, listing: Listing) -> list[float] | None:
        """Design row: [1, odo, odo^2, age, trim_tier, rent2buy, model dummies].

        `trim_tier` and `rent2buy` were added after inspecting the residuals.
        Without a trim control, a loaded Palisade reads as overpriced and a
        base one as a bargain, purely because trim sat in the error term.
        Without a Rent2Buy indicator, two different products -- an
        inspectable lot car and a car still out on rent whose mileage is an
        estimate -- were pooled as if they were the same good.
        """
        if listing.odometer is None or listing.age_years is None:
            return None
        odo = listing.odometer / 10000.0
        row = [
            1.0,
            odo,
            odo * odo,
            listing.age_years,
            float(trim_tier(listing.trim)),
            1.0 if listing.is_rent2buy else 0.0,
        ]
        key = _normalize_model(listing)
        row.extend(1.0 if key == mk else 0.0 for mk in self.model_keys)
        return row

    def predict(self, listing: Listing) -> float | None:
        row = self.features(listing)
        if row is None or len(row) != len(self.coefficients):
            return None
        log_price = sum(x * b for x, b in zip(row, self.coefficients))
        try:
            return math.exp(log_price)
        except OverflowError:
            return None

    def comps_for(self, listing: Listing) -> int:
        return self.counts.get(_normalize_model(listing), 0)


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting."""
    n = len(rhs)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_value = aug[col][col]
        for row in range(col + 1, n):
            factor = aug[row][col] / pivot_value
            if factor:
                for k in range(col, n + 1):
                    aug[row][k] -= factor * aug[col][k]

    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = aug[row][n] - sum(aug[row][k] * out[k] for k in range(row + 1, n))
        out[row] = total / aug[row][row]
    return out


def fit_hedonic(listings: list[Listing], ridge: float = 1e-3) -> Hedonic | None:
    """Fit log(price) on mileage, age, and model fixed effects.

    Model fixed effects absorb the level differences between an Elantra and a
    GV80, so the residual measures cheapness within a model rather than across
    the catalogue. Thin models get an effect estimated from very few cars, so
    callers must check ``comps_for`` before trusting a residual.
    """
    usable = [
        l for l in listings
        if l.price and l.price > 1000 and l.odometer is not None and l.age_years is not None
    ]
    if len(usable) < 20:
        logger.warning("Only %d usable rows; skipping hedonic fit", len(usable))
        return None

    counts: dict[str, int] = {}
    for listing in usable:
        counts[_normalize_model(listing)] = counts.get(_normalize_model(listing), 0) + 1

    # Drop one model as the reference category to keep the design full rank.
    model_keys = sorted(counts)
    if model_keys:
        model_keys = model_keys[1:]

    scratch = Hedonic([], model_keys, counts, 0, 0.0)
    design: list[list[float]] = []
    target: list[float] = []
    for listing in usable:
        row = scratch.features(listing)
        if row is None:
            continue
        design.append(row)
        target.append(math.log(float(listing.price)))

    if not design:
        return None

    k = len(design[0])
    if len(design) <= k:
        logger.warning("Design is %dx%d; too few rows to fit", len(design), k)
        return None

    # Normal equations with a ridge penalty (intercept left unpenalised).
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for row, y in zip(design, target):
        for i in range(k):
            if row[i]:
                xty[i] += row[i] * y
                for j in range(i, k):
                    if row[j]:
                        xtx[i][j] += row[i] * row[j]
    for i in range(k):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
        if i:
            xtx[i][i] += ridge * len(design)

    coefficients = _solve(xtx, xty)
    if coefficients is None:
        logger.warning("Hedonic normal equations were singular")
        return None

    residuals = [
        y - sum(x * b for x, b in zip(row, coefficients))
        for row, y in zip(design, target)
    ]
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))

    logger.info(
        "Hedonic fitted on %d vehicles across %d models (log-RMSE %.3f)",
        len(design), len(counts), rmse,
    )
    return Hedonic(coefficients, model_keys, counts, len(design), rmse)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def preference_fit(listing: Listing, cfg: Config) -> tuple[int, list[str]]:
    """How well a car matches stated taste. Returns (matches, misses).

    Deliberately not a filter. Taste is negotiable against price and
    condition, and hard-filtering on colour would have hidden the whole
    board. The count drives ordering and the misses are shown, so a car that
    is wrong on one axis still gets seen with the reason attached.
    """
    matches = 0
    misses: list[str] = []

    colour = (listing.exterior_color or "").lower()
    if cfg.pref_colors:
        if any(c.strip().lower() in colour for c in cfg.pref_colors):
            matches += 1
        else:
            misses.append(f"{listing.exterior_color or 'colour unknown'}, not a colour you wanted")

    trim = (listing.trim or "").lower()
    if cfg.pref_trims:
        if any(t.strip().lower() in trim for t in cfg.pref_trims):
            matches += 1
        else:
            misses.append(f"{listing.trim or 'trim unknown'}")

    if cfg.odometer_ideal and listing.odometer is not None:
        limit = cfg.odometer_ideal + cfg.odometer_tolerance
        if listing.odometer <= cfg.odometer_ideal:
            matches += 1
        elif listing.odometer <= limit:
            matches += 1
            misses.append(f"{listing.odometer:,} mi, just over your {cfg.odometer_ideal:,} ideal")
        else:
            over = listing.odometer - cfg.odometer_ideal
            misses.append(f"{listing.odometer:,} mi, {over:,} over your ideal")

    return matches, misses


def match_watch(listing: Listing, cfg: Config) -> WatchEntry | None:
    """First matching watchlist entry, tier A checked before tier B."""
    for entry in sorted(cfg.watch, key=lambda w: w.tier.upper()):
        if entry.matches(listing):
            return entry
    return None


def score_listing(
    listing: Listing,
    cfg: Config,
    hedonic: Hedonic | None,
    autocheck: AutoCheck | None = None,
    price_drop_30d: int | None = None,
    market_curves: dict | None = None,
) -> Scored:
    delivery, tax, fees, total = landed_cost(listing, cfg)
    scored = Scored(
        listing=listing,
        delivery=delivery,
        tax=tax,
        fees=fees,
        landed_cost=total,
        autocheck=autocheck,
        price_drop_30d=price_drop_30d,
    )

    entry = match_watch(listing, cfg)
    if entry:
        scored.tier = entry.tier.upper()
        scored.matched_label = entry.label

    # Thin models get their benchmark from the open market instead.
    #
    # The pooled hedonic gives each model its own dummy, so a model with two
    # observations has its level fitted almost exactly and its residual is
    # arithmetic rather than evidence. That is not a corner case here: it is
    # the XC60 and the CX-70, the two models most worth watching. For those,
    # a curve fitted on Cars.com listings of the same model is a real
    # benchmark built on real variation.
    key = _normalize_model(listing)
    internal_comps = hedonic.comps_for(listing) if hedonic else 0
    curve = (market_curves or {}).get(key)

    if curve is not None and internal_comps < cfg.min_comps and listing.price:
        from .benchmark import market_gap
        gap = market_gap(listing, curve)
        if gap is not None:
            scored.predicted_landed = float(listing.price) + gap[0]
            scored.residual_pct = -gap[1]
            scored.residual_sigma = (-gap[1] / 100.0) / curve.rmse if curve.rmse else None
            scored.comp_n = curve.n
            scored.benchmark = "market"
            return scored

    if hedonic and listing.price:
        predicted = hedonic.predict(listing)
        if predicted:
            scored.predicted_landed = predicted
            scored.residual_pct = 100.0 * (listing.price - predicted) / predicted
            scored.comp_n = hedonic.comps_for(listing)
            scored.benchmark = "hertz"
            # How unusual is this residual, in units of the fit's own spread?
            # A percentage alone is not interpretable: -3% against a 4%
            # residual SD is under one sigma, i.e. an ordinary car.
            if hedonic.rmse > 0:
                import math as _math
                scored.residual_sigma = (
                    _math.log(listing.price / predicted) / hedonic.rmse
                )

    return scored


def value_gate(scored: Scored, cfg: Config) -> tuple[bool, list[str]]:
    """Gate 1: is this car cheap enough, on enough comparables, to care?

    Kept separate from the condition gate because it is free, while checking
    condition costs two page loads per car. The pipeline runs this first and
    only buys AutoCheck reports for the survivors.
    """
    listing = scored.listing

    if not scored.tier:
        return False, ["not on the watchlist"]

    entry = next(
        (w for w in cfg.watch if w.tier.upper() == scored.tier and w.label == scored.matched_label),
        None,
    )
    threshold = entry.threshold_pct if entry else -8.0

    if scored.residual_pct is None:
        return False, ["no price prediction available"]
    if scored.comp_n < cfg.min_comps:
        return False, [f"only {scored.comp_n} comparable listings (need {cfg.min_comps})"]

    discount = (scored.predicted_landed or 0) - float(listing.price or 0)
    if discount < cfg.min_abs_discount:
        return False, [f"discount ${discount:,.0f} below the ${cfg.min_abs_discount} floor"]
    if scored.residual_pct > threshold:
        return False, [
            f"{scored.residual_pct:+.1f}% vs predicted, above the "
            f"{threshold:+.1f}% tier-{scored.tier} bar"
        ]

    return True, [
        f"${discount:,.0f} ({scored.residual_pct:+.1f}%) below predicted price, "
        f"on {scored.comp_n} comparable listings"
    ]


def qualifies(scored: Scored, cfg: Config) -> tuple[bool, list[str]]:
    """Both gates. Returns (should_alert, human-readable reasons).

    Gate 1 is value; gate 2 is condition, where AutoCheck must come back
    clean. A car fails the condition gate even when we merely could not read
    its report, so an unparseable history suppresses the alert.
    """
    passed, reasons = value_gate(scored, cfg)
    if not passed:
        return False, reasons

    if (scored.listing.source or "") == "carmax":
        # CarMax exposes no history report we can read, so condition is
        # genuinely unverified. Say so rather than implying it passed.
        return False, reasons + [
            "CarMax: condition unverified here -- open the listing and read "
            "the AutoCheck CarMax publishes on it before travelling"
        ]
    if scored.autocheck is None:
        return False, reasons + ["AutoCheck not yet retrieved"]
    if not scored.autocheck.is_clean:
        return False, reasons + [f"AutoCheck: {'; '.join(scored.autocheck.concerns)}"]

    reasons.append("AutoCheck clean: no accidents, clean title, no odometer flags")

    if scored.price_drop_30d:
        reasons.append(f"price cut ${scored.price_drop_30d:,} in the last 30 days")
    days = scored.listing.days_on_lot
    if days and days > 60:
        reasons.append(f"on the lot {days} days, so a further markdown is plausible")

    return True, reasons


def rank(scored_list: list[Scored]) -> list[Scored]:
    """Cheapest landed cost first; unpriced cars last."""
    return sorted(scored_list, key=lambda s: (s.landed_cost <= 0, s.landed_cost))


_TRIM_NOISE = re.compile(r"\s+")


def trim_label(listing: Listing) -> str:
    return _TRIM_NOISE.sub(" ", listing.trim or "").strip() or "-"


# Mazda's high-voltage battery cover is 8 years / 100,000 miles federally,
# and it runs from the date of first service rather than the model year. That
# matters more than usual on an ex-rental hybrid: the car was registered as a
# fleet vehicle early in its life, so the clock started sooner than the model
# year suggests, but a great deal of cover typically remains. Confirm the term
# with Mazda for the specific VIN before relying on it.
HV_BATTERY_WARRANTY_YEARS = 8
HV_BATTERY_WARRANTY_MILES = 100_000


def battery_warranty(scored: Scored) -> dict | None:
    """Remaining high-voltage battery cover, from the AutoCheck in-service date."""
    report = scored.autocheck
    listing = scored.listing
    if report is None or not report.in_service_date:
        return None
    if "hybrid" not in f"{listing.model} {listing.fuel_type}".lower():
        return None

    from datetime import date, datetime

    started = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%Y"):
        try:
            started = datetime.strptime(report.in_service_date, fmt).date()
            break
        except ValueError:
            continue
    if started is None:
        return None

    try:
        expires = started.replace(year=started.year + HV_BATTERY_WARRANTY_YEARS)
    except ValueError:                      # 29 February
        expires = started.replace(month=3, day=1, year=started.year + HV_BATTERY_WARRANTY_YEARS)

    years_left = (expires - date.today()).days / 365.25
    miles_left = HV_BATTERY_WARRANTY_MILES - (listing.odometer or 0)
    return {
        "in_service": started,
        "expires": expires,
        "years_left": max(0.0, years_left),
        "miles_left": max(0, miles_left),
    }
