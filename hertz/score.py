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
from .trims import UNKNOWN as TRIM_UNKNOWN, trim_tier

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


def typical_doc_fee(listings) -> int:
    """The median Hertz doc fee in the sample (Hertz Price minus No Haggle)."""
    fees = sorted(l.doc_fee for l in listings
                  if (l.source or "hertz") == "hertz" and l.doc_fee and 0 < l.doc_fee < 2000)
    return int(fees[len(fees) // 2]) if fees else 0


def fit_price(listing: Listing, doc_fee: int = 0) -> float | None:
    """The price every model is fitted on: pre-doc-fee, whatever the seller.

    Hertz's quoted price includes its doc fee ($387 OH, $649 AZ); CarMax,
    Byers and every Cars.com asking price exclude theirs. Fitting the two
    together put a tilt of about 1.5% against Hertz into every residual.
    Hertz lot cars carry the pre-doc "No Haggle Price"; Rent2Buy cars omit
    it, so the sample's typical Hertz doc fee comes off instead.
    """
    if not listing.price:
        return None
    if (listing.source or "hertz") == "hertz":
        if listing.no_haggle_price:
            return float(listing.no_haggle_price)
        return float(listing.price) - doc_fee
    return float(listing.price)


@dataclass
class Hedonic:
    """Fitted log-price surface plus what is needed to judge one car by it.

    The residual a car is judged on is LEAVE-ONE-OUT: the car's own row is
    taken out of the fit before it is predicted. Without that, a model with
    two rows has its dummy fitted through them and the residual is
    arithmetic, not evidence. With the hat matrix kept from the fit, the
    exact LOO residual is e / (1 - h), no refit needed.
    """

    coefficients: list[float]
    model_keys: list[str]
    year_keys: list[int]
    counts: dict[str, int]
    n_obs: int
    rmse: float                       # leave-one-out (PRESS) log-RMSE, pooled
    xtx_inv: list[list[float]]        # (X'X + ridge)^-1, for hat values
    fitted_vins: set[str]
    sigma_by_model: dict[str, float]  # LOO residual SD per model with >= 20 rows
    doc_fee: int

    def features(self, listing: Listing) -> list[float] | None:
        """Design row: [1, odo, odo^2, model-year dummies, trim tier, trim unknown,
        rent2buy, model dummies].

        Model-year dummies rather than a linear age: the first-year drop is
        a cliff, and a straight line through 2024-2026 priced a 2025 above
        its MSRP. `trim_tier` is the make's own ladder, so Audi's "Premium"
        (its base) and Mazda's (its third rung) no longer share a value. A
        car with no trim label at all gets the missing-label indicator: 30
        of Hertz's 72 Highlanders are unlabelled Rent2Buy units at a median
        $36k against $40.7k for the labelled LE/XSE lot cars, and reading
        them as mid-ladder made every one of them a 15% "bargain".
        """
        if listing.odometer is None or not listing.year:
            return None
        odo = listing.odometer / 10000.0
        row = [1.0, odo, odo * odo]
        row.extend(1.0 if listing.year == y else 0.0 for y in self.year_keys)
        tier = trim_tier(listing.trim, listing.make)
        row.append(tier)
        row.append(1.0 if tier == TRIM_UNKNOWN else 0.0)
        row.append(1.0 if listing.is_rent2buy else 0.0)
        key = _normalize_model(listing)
        row.extend(1.0 if key == mk else 0.0 for mk in self.model_keys)
        return row

    def leverage(self, row: list[float]) -> float:
        k = len(row)
        h = 0.0
        for i in range(k):
            if row[i]:
                h += row[i] * sum(self.xtx_inv[i][j] * row[j] for j in range(k) if row[j])
        return min(max(h, 0.0), 1.0)

    def assess(self, listing: Listing) -> dict | None:
        """Judge one car: LOO prediction, LOO residual, its own sigma, and n.

        Returns None when the car cannot be placed. `resid` is None when the
        car's own dummy is the only thing fitting it (a one-row model): no
        evidence either way.
        """
        row = self.features(listing)
        if row is None or len(row) != len(self.coefficients):
            return None
        price = fit_price(listing, self.doc_fee)
        if not price or price <= 0:
            return None
        y = math.log(price)
        yhat = sum(x * b for x, b in zip(row, self.coefficients))
        key = _normalize_model(listing)
        n = self.counts.get(key, 0)
        if listing.vin in self.fitted_vins:
            h = self.leverage(row)
            if h > 0.98:
                return {"pred_log": yhat, "resid": None, "sigma": None, "n": n, "leverage": h}
            e = (y - yhat) / (1.0 - h)
            pred = y - e
        else:
            h = 0.0
            e = y - yhat
            pred = yhat
        sigma = self.sigma_by_model.get(key, self.rmse)
        # The LOO residual's own spread is sigma / sqrt(1 - h): a car in a
        # thin model is judged against a wider band, as it should be.
        sigma_i = sigma / math.sqrt(1.0 - h) if sigma else None
        return {"pred_log": pred, "resid": e, "sigma": sigma_i, "n": n, "leverage": h}

    def predict(self, listing: Listing) -> float | None:
        """Out-of-sample predicted pre-doc price."""
        a = self.assess(listing)
        if a is None:
            return None
        try:
            return math.exp(a["pred_log"])
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


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting; None if singular."""
    n = len(matrix)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for row in range(n):
            if row != col and aug[row][col]:
                factor = aug[row][col]
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [row[n:] for row in aug]


def fit_hedonic(listings: list[Listing], ridge: float = 1e-6) -> Hedonic | None:
    """Fit log(pre-doc price) on mileage, model year, trim tier, Rent2Buy and
    model fixed effects, and keep what leave-one-out judgement needs.

    The ridge is numerical only (1e-6 N). An earlier 1e-3 N shrank every
    model dummy a little towards the reference model's level, which for a
    nine-row model with a level 60% above the reference was a 6% bias in
    exactly the residual that matters. Thin models are handled by the LOO
    residual and its wider sigma, and by the market blend in
    `score_listing`, not by pulling their level towards an Elantra's.
    """
    doc_fee = typical_doc_fee(listings)
    usable = []
    for l in listings:
        p = fit_price(l, doc_fee)
        if p and p > 1000 and l.odometer is not None and l.year:
            usable.append(l)
    if len(usable) < 20:
        logger.warning("Only %d usable rows; skipping hedonic fit", len(usable))
        return None

    counts: dict[str, int] = {}
    for listing in usable:
        counts[_normalize_model(listing)] = counts.get(_normalize_model(listing), 0) + 1
    # The reference model is the LARGEST one, never the alphabetical first:
    # a one-row "Audi Q4 e-tron" as the reference had no dummy of its own,
    # its level fell on the pooled intercept, and it read +9.9% against
    # nothing. With the biggest model as reference every thin model gets a
    # dummy, and its leave-one-out residual is honestly undefined.
    reference = max(counts, key=lambda k: (counts[k], k))
    model_keys = sorted(k for k in counts if k != reference)
    year_keys = sorted({l.year for l in usable})[1:]

    scratch = Hedonic([], model_keys, year_keys, counts, 0, 0.0, [], set(), {}, doc_fee)
    design: list[list[float]] = []
    target: list[float] = []
    rows_used: list[Listing] = []
    for listing in usable:
        row = scratch.features(listing)
        if row is None:
            continue
        design.append(row)
        target.append(math.log(fit_price(listing, doc_fee)))
        rows_used.append(listing)
    if not design:
        return None

    k = len(design[0])
    if len(design) <= k:
        logger.warning("Design is %dx%d; too few rows to fit", len(design), k)
        return None

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

    xtx_inv = _invert(xtx)
    if xtx_inv is None:
        logger.warning("Hedonic normal equations were singular")
        return None
    coefficients = [sum(xtx_inv[i][j] * xty[j] for j in range(k)) for i in range(k)]

    fitted = Hedonic(coefficients, model_keys, year_keys, counts, len(design), 0.0,
                     xtx_inv, {l.vin for l in rows_used}, {}, doc_fee)
    press: list[float] = []
    per_model: dict[str, list[float]] = {}
    for row, y, listing in zip(design, target, rows_used):
        h = fitted.leverage(row)
        if h > 0.98:
            continue                     # a one-row model: its dummy is the row
        e = (y - sum(x * b for x, b in zip(row, coefficients))) / (1.0 - h)
        press.append(e * e)
        per_model.setdefault(_normalize_model(listing), []).append(e)
    if not press:
        return None
    fitted.rmse = math.sqrt(sum(press) / len(press))
    for key, errs in per_model.items():
        if len(errs) >= 20:
            mean = sum(errs) / len(errs)
            fitted.sigma_by_model[key] = math.sqrt(sum((e - mean) ** 2 for e in errs) / (len(errs) - 1))

    logger.info(
        "Hedonic fitted on %d vehicles across %d models, %d model years "
        "(leave-one-out log-RMSE %.3f, doc fee $%d)",
        len(design), len(counts), len(year_keys) + 1, fitted.rmse, doc_fee,
    )
    return fitted


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

# Brown-family interiors, as sellers actually name them. Matched on WHOLE
# WORDS: a substring test on "tan" flagged "Titan Black", which is the exact
# false positive that would put a black-interior car on the shortlist.
# Tiers: "light" is the blond/sand family Lorenzo does not want, "mid" is
# the caramel/cognac/saddle range he does, "dark" is espresso/chocolate.
_INTERIOR_TIERS = (
    ("mid", ("brown", "cognac", "caramel", "saddle", "tan", "terracotta", "russet",
             "mocha", "chestnut", "hazel", "amber", "tobacco", "nougat", "walnut",
             "sienna", "camel", "toffee", "maple")),
    ("light", ("blond", "blonde", "beige", "sand", "cream", "ivory", "parchment",
               "oyster", "linen", "almond", "macchiato", "cashmere", "canvas")),
    ("dark", ("espresso", "chocolate", "coffee", "mahogany", "truffle", "umber")),
)


def interior_tier(interior: str) -> str | None:
    """'mid' | 'light' | 'dark' for a brown-family interior, else None.

    Whole-word matching. Mixed interiors like "Black w Brown" count as the
    brown tier, since that is the brown he is after.
    """
    words = set(re.findall(r"[a-z]+", (interior or "").lower()))
    if not words:
        return None
    for tier, names in _INTERIOR_TIERS:
        if words & set(names):
            return tier
    return None


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

    # Interior is a bonus, never a miss: most sellers omit it, and a car
    # should not be marked down for a field the seller left blank.
    tier = interior_tier(listing.interior_color)
    if tier == "mid":
        matches += 1
    elif tier == "light":
        misses.append(f"{listing.interior_color}: light-brown interior, lighter than you like")

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

    # Two benchmarks, blended by how much each knows about this model.
    #
    # The within-inventory fit answers "cheap for what Hertz/CarMax/Byers
    # charge"; a Cars.com curve for the same model answers "cheap for what
    # everyone charges". With hundreds of CX-50 Hybrids the first is the
    # sharper instrument; with nine XC60s, most of them one dealer's uniform
    # pricing, the second is. The weight on the inventory fit is n / (n + K),
    # K from config, so neither ever switches off abruptly. Both are on the
    # pre-doc-fee price; the quoted price gets its own doc fee back below.
    key = _normalize_model(listing)
    curve = (market_curves or {}).get(key)
    internal = hedonic.assess(listing) if hedonic else None
    price_fit = fit_price(listing, hedonic.doc_fee if hedonic else 0)
    if not price_fit or price_fit <= 0:
        return scored
    y = math.log(price_fit)
    n = internal["n"] if internal else 0

    pred_int = sig_int = None
    if internal and internal["resid"] is not None and internal["sigma"]:
        pred_int, sig_int = internal["pred_log"], internal["sigma"]
    pred_mkt = sig_mkt = None
    if curve is not None and listing.odometer is not None and listing.age_years is not None:
        try:
            pred_mkt = math.log(curve.predict(listing.odometer, listing.age_years,
                                              trim_tier(listing.trim, listing.make),
                                              listing.certified))
            sig_mkt = curve.rmse or None
        except (ValueError, OverflowError):
            pred_mkt = None

    if pred_int is not None and pred_mkt is not None and sig_mkt:
        w = n / (n + cfg.market_blend_k)
        pred = w * pred_int + (1.0 - w) * pred_mkt
        sigma = math.sqrt(w * sig_int ** 2 + (1.0 - w) * sig_mkt ** 2)
        scored.benchmark = f"blend {int(round(100 * (1 - w)))}% market"
        scored.comp_n = n + curve.n
    elif pred_int is not None:
        pred, sigma = pred_int, sig_int
        scored.benchmark = "hertz"
        scored.comp_n = n
    elif pred_mkt is not None and sig_mkt:
        pred, sigma = pred_mkt, sig_mkt
        scored.benchmark = "market"
        scored.comp_n = curve.n
    else:
        return scored

    e = y - pred
    # Back on the quoted-price basis: whatever doc fee sits inside the
    # quoted price is added to the predicted pre-doc price.
    scored.predicted_landed = math.exp(pred) + (float(listing.price) - price_fit)
    scored.residual_pct = 100.0 * (math.exp(e) - 1.0)
    scored.residual_sigma = e / sigma if sigma else None
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
    if entry and entry.min_sigma is not None:
        if scored.residual_sigma is None or scored.residual_sigma > entry.min_sigma:
            return False, [
                f"{scored.residual_pct:+.1f}% is only "
                f"{scored.residual_sigma:+.2f} sigma within its model (bar {entry.min_sigma:+.1f})"
                if scored.residual_sigma is not None else "no sigma for this residual"
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
    if scored.autocheck is None and scored.listing.certified:
        # Franchise dealers' certified cars carry a manufacturer inspection
        # and CPO warranty but no AutoCheck link on the page, so the history
        # cannot be independently read. Certification is a stronger condition
        # signal than a clean AutoCheck, so it passes -- labelled as such,
        # never as a clean report we did not actually see.
        seller = (scored.listing.source or "").lower()
        reasons.append(
            "Enterprise certified (109-point inspection, 12-month/12k powertrain warranty, "
            "7-day repurchase); history not independently read"
            if seller == "enterprise" else
            "Certified pre-owned by the franchise dealer (inspection + CPO warranty); "
            "no AutoCheck link on the page, so history was not independently read"
        )
    elif scored.autocheck is None:
        return False, reasons + ["AutoCheck not yet retrieved"]
    elif not scored.autocheck.is_clean:
        return False, reasons + [f"AutoCheck: {'; '.join(scored.autocheck.concerns)}"]
    else:
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
