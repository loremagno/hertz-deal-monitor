"""Time and timing: a price index, a markdown hazard, and a wait-or-buy rule.

Everything here reads the price history the store has committed since 9
September 2026 and gets better every run. Nothing here alerts.

The index. Every car's price is a step function through its history rows,
so for each week the panel holds every car listed that week at the price it
carried at the week's end. Each observation is residualised against the
CURRENT hedonic (the car's own mileage at the time, the fit's slopes), and
the weekly mean residual, exponentiated, is the price level relative to
now: 1.03 means cars were 3% dearer that week for the same mileage, year,
trim and model. Fleets deflect after the summer; this is how to see whether
it is happening rather than assume it.

The hazard. Two piecewise-constant rates by days on lot: how much the price
falls per week (a rate over every car-week, cuts and quiet weeks alike),
and how often a car leaves the site per week. A car leaving a capped sweep
looks like a sale, so the rates on the primary model, which is swept in
full, are the honest ones; the pooled rates carry that caveat.

The rule. For a car you want, waiting a week gains the expected cut times
the chance it is still there, and loses the surplus over the market times
the chance it is gone. Wait when the first exceeds the second.
"""
from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta

from . import clock
from .models import Listing

logger = logging.getLogger(__name__)

# Days-on-lot buckets. Hertz's own cadence seems to move at a month.
BUCKETS = ((0, 14), (15, 30), (31, 60), (61, 10 ** 6))

# Manufacturer warranty, years / miles: bumper-to-bumper, then powertrain,
# as they transfer to a second owner (Hyundai, Kia and Genesis keep the
# 10/100 powertrain for the first owner only).
WARRANTY = {
    "mazda": ((3, 36000), (5, 60000)), "toyota": ((3, 36000), (5, 60000)),
    "honda": ((3, 36000), (5, 60000)), "subaru": ((3, 36000), (5, 60000)),
    "hyundai": ((5, 60000), (5, 60000)), "kia": ((5, 60000), (5, 60000)),
    "genesis": ((5, 60000), (5, 60000)), "volvo": ((4, 50000), (4, 50000)),
    "volkswagen": ((4, 50000), (4, 50000)), "audi": ((4, 50000), (4, 50000)),
    "mercedes-benz": ((4, 50000), (4, 50000)), "bmw": ((4, 50000), (4, 50000)),
    "lexus": ((4, 50000), (6, 70000)), "acura": ((4, 50000), (6, 70000)),
    "nissan": ((3, 36000), (5, 60000)), "ford": ((3, 36000), (5, 60000)),
    "chevrolet": ((3, 36000), (5, 60000)), "jeep": ((3, 36000), (5, 60000)),
}

# Combined mpg for watched models the store never sees with a figure
# (CarMax, Byers and Cars.com rows carry none). EPA combined, current gen.
MPG_FALLBACK = {
    "volvo|xc60": 26, "mazda|cx-70": 25, "mazda|cx-90": 25, "mazda|cx-50 hybrid": 38,
    "mazda|cx-50": 27, "mazda|cx-5": 26, "hyundai|palisade": 22, "hyundai|santa fe": 24,
    "hyundai|santa fe hybrid": 34, "hyundai|tucson": 28, "hyundai|tucson hybrid": 37,
    "kia|sorento": 25, "kia|telluride": 22, "kia|sportage": 27, "toyota|highlander": 24,
    "toyota|rav4 hybrid": 39, "toyota|sienna": 36, "toyota|camry": 32, "toyota|camry hybrid": 48,
    "toyota|corolla cross": 32, "honda|cr-v hybrid": 37, "honda|accord hybrid": 46,
    "volkswagen|atlas": 21, "volkswagen|tiguan": 26, "genesis|gv70": 23, "genesis|gv80": 20,
    "genesis|g70": 25, "subaru|outback": 28, "subaru|forester": 28, "volvo|xc40": 26,
    "volvo|v60 cross country": 26, "volvo|v90 cross country": 25, "volvo|s60": 28,
    "audi|q5": 25, "audi|a4 allroad": 27, "audi|a6 allroad": 24, "audi|a4": 28, "audi|a6": 26,
    "mercedes-benz|glc": 25, "mercedes-benz|glc 300": 25, "mercedes-benz|c-class": 27,
    "mercedes-benz|gla 250": 28, "mercedes-benz|glb 250": 27, "mercedes-benz|gle": 22,
    "mercedes-benz|e-class": 24, "bmw|x3": 26, "bmw|3 series": 29, "lexus|nx 350": 25,
    "lexus|rx 350": 25,
}


def _key(listing: Listing) -> str:
    return f"{listing.make.strip().lower()}|{listing.model.strip().lower()}"


def combined_mpg(listing: Listing) -> float | None:
    """55/45 city/highway blend; a hybrid with only a city figure keeps it."""
    city, hwy = listing.city_mpg, listing.highway_mpg
    if city and hwy:
        return 1.0 / (0.55 / city + 0.45 / hwy)
    return city or hwy or None


def mpg_by_model(listings: list[Listing]) -> dict[str, float]:
    """Median combined mpg per model from the rows that carry one, with the
    fallback table underneath for the rest."""
    seen: dict[str, list[float]] = {}
    for l in listings:
        mpg = combined_mpg(l)
        if mpg and 8 < mpg < 80:
            seen.setdefault(_key(l), []).append(mpg)
    out = dict(MPG_FALLBACK)
    for key, values in seen.items():
        values.sort()
        out[key] = round(values[len(values) // 2], 1)
    return out


def warranty_left(listing: Listing) -> dict | None:
    """Months and miles of bumper-to-bumper and powertrain cover left, from
    the model year (in service taken as mid the prior year) and the odometer."""
    terms = WARRANTY.get((listing.make or "").strip().lower())
    if not terms or not listing.year or listing.odometer is None:
        return None
    age = listing.age_years or 0.0
    out = {}
    for name, (years, miles) in zip(("b2b", "powertrain"), terms):
        months = max(0, int(round((years - age) * 12)))
        left_miles = max(0, miles - int(listing.odometer))
        out[name] = {"months": months, "miles": left_miles}
    if listing.certified:
        # A franchise CPO adds roughly a year of comprehensive cover on top
        # of what is left; the exact terms differ by make.
        out["b2b"]["months"] += 12
        out["cpo"] = True
    return out


# --------------------------------------------------------------------------
# Price index
# --------------------------------------------------------------------------

def _history(store) -> dict[str, list[tuple[datetime, int, int | None]]]:
    """vin -> [(observed_at, price, odometer)] in time order."""
    rows = store.conn.execute(
        "SELECT vin, observed_at, price, odometer FROM price_history "
        "WHERE price IS NOT NULL ORDER BY vin, observed_at").fetchall()
    out: dict[str, list] = {}
    for r in rows:
        try:
            out.setdefault(r["vin"], []).append(
                (datetime.fromisoformat(r["observed_at"]), int(r["price"]), r["odometer"]))
        except (TypeError, ValueError):
            continue
    return out


def _price_as_of(history: list, when: datetime, fallback: int | None) -> tuple[int | None, int | None]:
    """The price (and odometer) a car carried at `when`: the last history row
    at or before it, else the earliest row if the car was already listed."""
    price, odo = None, None
    for t, p, o in history:
        if t <= when:
            price, odo = p, o
        else:
            break
    if price is None and history:
        price, odo = history[0][1], history[0][2]
    return price if price is not None else fallback, odo


def price_index(store, hedonic, primary_key: str, weeks: int = 26) -> list[dict]:
    """Weekly price level relative to the current fit, overall and for the
    primary model, with the primary model's inventory count."""
    if hedonic is None:
        return []
    rows = store.conn.execute(
        "SELECT * FROM listings WHERE first_seen IS NOT NULL").fetchall()
    listings = {r["vin"]: (Listing.from_row(dict(r)), r["first_seen"], r["last_seen"], r["active"])
                for r in rows}
    history = _history(store)
    now = clock.now()
    out: list[dict] = []
    for k in range(weeks - 1, -1, -1):
        week_end = now - timedelta(days=7 * k)
        week_start = week_end - timedelta(days=7)
        resid_all: list[float] = []
        resid_primary: list[float] = []
        inventory_primary = 0
        prices_primary: list[int] = []
        for vin, (listing, first_seen, last_seen, active) in listings.items():
            try:
                fs = datetime.fromisoformat(first_seen)
                ls = datetime.fromisoformat(last_seen) if last_seen else now
            except (TypeError, ValueError):
                continue
            if fs > week_end or (not active and ls < week_start):
                continue
            price, odo = _price_as_of(history.get(vin, []), week_end, listing.price)
            if not price:
                continue
            as_of = replace(listing, price=price, odometer=odo if odo is not None else listing.odometer)
            a = hedonic.assess(as_of)
            if a is None:
                continue
            e = a["resid"] if a["resid"] is not None else None
            if e is None:
                continue
            resid_all.append(e)
            if _key(listing) == primary_key:
                resid_primary.append(e)
                inventory_primary += 1
                prices_primary.append(price)
        if not resid_all:
            continue
        prices_primary.sort()
        out.append({
            "week_end": week_end.date().isoformat(),
            "n": len(resid_all),
            "index": round(math.exp(sum(resid_all) / len(resid_all)), 4),
            "n_primary": len(resid_primary),
            "index_primary": (round(math.exp(sum(resid_primary) / len(resid_primary)), 4)
                              if resid_primary else None),
            "inventory_primary": inventory_primary,
            "median_price_primary": (prices_primary[len(prices_primary) // 2]
                                     if prices_primary else None),
        })
    return out


# --------------------------------------------------------------------------
# Hazard
# --------------------------------------------------------------------------

def _bucket(days_on_lot: int | None) -> str | None:
    if days_on_lot is None or days_on_lot < 0:
        return None
    for lo, hi in BUCKETS:
        if lo <= days_on_lot <= hi:
            return f"{lo}-{hi}" if hi < 10 ** 6 else f"{lo}+"
    return None


def _lot_date(listing: Listing, first_seen: datetime) -> datetime:
    """When the car went on sale. Rent2Buy rows carry a FUTURE availability
    date in the inventory field, so for them the day we first saw the car is
    the best lower bound."""
    stamp = listing._inventory_date()
    if stamp is None:
        return first_seen
    lot = datetime(stamp.year, stamp.month, stamp.day)
    return lot if lot <= first_seen else first_seen


def lot_days(listing: Listing, first_seen: str | None) -> int | None:
    """Days on sale for the wait-or-buy rule, same convention as the hazard."""
    try:
        fs = datetime.fromisoformat(first_seen) if first_seen else None
    except (TypeError, ValueError):
        fs = None
    if fs is None:
        return listing.days_on_lot
    return (clock.now() - _lot_date(listing, fs)).days


def hazard(store, primary_key: str) -> dict:
    """Weekly markdown rate and weekly leaving rate by days on lot, for all
    fully swept Hertz models and for the primary model alone.

    A car dropping out of a capped sweep looks exactly like a sale, so the
    leaving rate is estimated only on models whose last sweep covered every
    car (`coverage:<model>` = "full", written by the collector); the
    markdown rate uses every car, since a price path is a price path.
    """
    rows = store.conn.execute(
        "SELECT * FROM listings WHERE source = 'hertz' AND first_seen IS NOT NULL").fetchall()
    history = _history(store)
    now = clock.now()
    full = {r["key"][len("coverage:"):] for r in store.conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'coverage:%' AND value = 'full'").fetchall()}

    def empty():
        return {b: {"cut_log": 0.0, "days": 0.0, "cuts": 0, "left": 0, "exposure": 0.0}
                for b in (_bucket(lo) for lo, _ in BUCKETS)}

    tables = {"all": empty(), "primary": empty()}
    for r in rows:
        listing = Listing.from_row(dict(r))
        try:
            fs = datetime.fromisoformat(r["first_seen"])
            ls = datetime.fromisoformat(r["last_seen"]) if r["last_seen"] else now
        except (TypeError, ValueError):
            continue
        lot = _lot_date(listing, fs)
        active = bool(r["active"])
        end = now if active else ls
        is_primary = _key(listing) == primary_key
        covered = listing.model.lower() in full or is_primary
        targets = [tables["all"]] + ([tables["primary"]] if is_primary else [])

        # Price path: segments between history rows, then the tail to `end`.
        hist = history.get(r["vin"], [])
        points = [(t, p) for t, p, _ in hist] or [(fs, listing.price or 0)]
        points.append((end, points[-1][1]))
        for (t0, p0), (t1, p1) in zip(points, points[1:]):
            days = (t1 - t0).total_seconds() / 86400.0
            if days <= 0 or not p0 or not p1:
                continue
            b = _bucket((t0 - lot).days)
            if b is None:
                continue
            for table in targets:
                table[b]["days"] += days
                if p1 != p0:
                    table[b]["cut_log"] += math.log(p1 / p0)
                    if p1 < p0:
                        table[b]["cuts"] += 1

        # Exposure by lot-age bucket, day by day; the leaving event lands in
        # the bucket of the last day seen. Only fully swept models count.
        if not covered:
            continue
        day = fs
        while day < end:
            b = _bucket((day - lot).days)
            if b:
                for table in targets:
                    table[b]["exposure"] += 1.0
            day += timedelta(days=1)
        if not active:
            b = _bucket((ls - lot).days)
            if b:
                for table in targets:
                    table[b]["left"] += 1

    out: dict = {"computed_at": clock.now_iso(), "all": {}, "primary": {}}
    for name, table in tables.items():
        for b, t in table.items():
            weekly_rate = (t["cut_log"] / t["days"] * 7.0) if t["days"] >= 7 else None
            leave = (t["left"] / t["exposure"]) if t["exposure"] >= 14 else None
            out[name][b] = {
                "weekly_price_change_pct": (round(100.0 * (math.exp(weekly_rate) - 1.0), 2)
                                            if weekly_rate is not None else None),
                "p_cut_week": (round(1.0 - math.exp(-t["cuts"] / t["days"] * 7.0), 3)
                               if t["days"] >= 7 else None),
                "p_survive_week": (round(math.exp(-7.0 * leave), 3) if leave is not None else None),
                "car_days": int(t["days"]), "cuts": t["cuts"], "left": t["left"],
            }
    return out


def advise(listing: Listing, predicted: float | None, table: dict,
           days_on_lot: int | None = None) -> dict | None:
    """Wait or buy, for one Hertz car, from a hazard table.

    Expected gain from waiting a week = expected cut x P(still there).
    Expected loss = surplus over the market x P(gone). Surplus below zero
    means the car is not a deal, so nothing is lost by waiting.
    """
    if not listing.price or not table:
        return None
    b = _bucket(listing.days_on_lot if days_on_lot is None else days_on_lot)
    if b is None or b not in table:
        return None
    row = table[b]
    if row["weekly_price_change_pct"] is None or row["p_survive_week"] is None:
        return None
    expected_cut = max(0.0, -row["weekly_price_change_pct"] / 100.0) * float(listing.price)
    p_survive = row["p_survive_week"]
    surplus = max(0.0, (predicted or 0.0) - float(listing.price)) if predicted else 0.0
    gain = expected_cut * p_survive
    loss = surplus * (1.0 - p_survive)
    if surplus <= 0:
        advice = "no rush"
    elif gain > loss:
        advice = "wait"
    else:
        advice = "buy"
    return {"bucket": b, "expected_cut": round(expected_cut), "p_survive": p_survive,
            "surplus": round(surplus), "advice": advice}
