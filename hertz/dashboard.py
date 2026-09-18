"""Dashboard data: one JSON document per run, rendered client-side.

The published page is static (`docs/index.html`) and reads `docs/data.json`
that the pipeline writes. Keeping data and page separate means the page is
edited once and the data changes every run, and the page can sort, filter
and tab without a server.

Everything in the JSON is already public listing data plus derived numbers.
The one personal field is the home zip, which the buyer has accepted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import market
from .config import CONDITION_SOURCES, Config
from .score import interior_tier, preference_fit


def _row(s, cfg: Config, groups: dict | None = None, extra: dict | None = None) -> dict:
    groups = groups or {}
    extra = extra or {}
    l = s.listing
    report = s.autocheck
    if report is None:
        if l.certified:
            condition, condition_kind = "certified, not independently read", "cert"
        elif s.history_url:
            condition, condition_kind = "Carfax linked, not read", "none"
        elif s.condition_outcome == "none":
            # We opened the page. The seller publishes no history at all.
            condition, condition_kind = "no history published", "none"
        else:
            condition, condition_kind = "not checked yet", "none"
    elif report.is_clean:
        condition, condition_kind = (f"clean · {report.score}" if report.score else "clean"), "clean"
    else:
        condition, condition_kind = (report.concerns[0] if report.concerns else "flagged"), "bad"

    fit, misses = preference_fit(l, cfg)
    return {
        "vin": l.vin,
        "label": l.label,
        "year": l.year, "make": l.make, "model": l.model, "trim": l.trim,
        "source": l.source or "hertz",
        "type": "certified" if l.certified else ("rent2buy" if l.is_rent2buy else "lot"),
        "color": l.exterior_color, "interior": l.interior_color,
        "interior_tier": interior_tier(l.interior_color),
        "lot": l.lot, "city": l.city, "state": l.state,
        "distance": None if l.geodist is None else round(l.geodist),
        "miles": l.odometer,
        "price": l.price,
        "delivery": round(s.delivery),
        "landed": round(s.landed_cost),
        "residual_pct": None if s.residual_pct is None else round(s.residual_pct, 1),
        "residual_sigma": None if s.residual_sigma is None else round(s.residual_sigma, 2),
        "benchmark": s.benchmark,
        "comps": s.comp_n,
        "tier": s.tier,
        "watch": s.matched_label,
        "group": groups.get(s.matched_label, "hertz"),
        "condition": condition, "condition_kind": condition_kind,
        "fit": fit, "misses": misses,
        "days_on_lot": l.days_on_lot,
        "available_from": l.available_from.isoformat() if l.available_from else None,
        "price_drop_30d": s.price_drop_30d,
        "cleared": bool(s.reasons),
        "reasons": s.reasons,
        "url": l.url,
        "image": l.image_url,
        # For the cost view and the wait-or-buy rule.
        "mpg": market.combined_mpg(l),
        "fuel_type": l.fuel_type,
        "age_years": None if l.age_years is None else round(l.age_years, 2),
        "warranty": market.warranty_left(l),
        "wait": extra.get("wait"),
        "days_on_sale": extra.get("days_on_sale"),
        "history_url": s.history_url,
    }


def _coverage(watched, cfg: Config) -> dict:
    """Condition verdicts across the drivable, history-bearing part of the board."""
    rows = [s for s in watched
            if (s.listing.source or "hertz") in CONDITION_SOURCES
            and s.listing.geodist is not None
            and s.listing.geodist <= cfg.alert_radius_miles]
    read = sum(1 for s in rows if s.autocheck is not None)
    linked = sum(1 for s in rows if s.autocheck is None and s.history_url)
    # Looked, and the seller publishes nothing. Checked, just not answerable.
    none_published = sum(1 for s in rows if s.autocheck is None and not s.history_url
                         and s.condition_outcome == "none")
    return {"total": len(rows), "read": read, "link_only": linked,
            "no_report": none_published,
            "unchecked": len(rows) - read - linked - none_published}


def build(result, cfg: Config, dream: dict | None, store, suv: dict | None = None,
          follow: dict | None = None) -> dict:
    """Assemble the document the page renders."""
    scored = [s for s in result.all_scored if s.listing.price]
    watched = [s for s in scored if s.tier]

    # A watch whose whole point is "only if discounted" must not put every
    # car it matches on the board: the first cut of the very-discounted lane
    # showed 237 rows, most priced ABOVE prediction, and the 2026 sweep
    # would show hundreds. Rows from such a watch are kept only at or below
    # its board bar (`board_max_residual_pct` in config.toml).
    bars = {w.label: w.board_max_residual_pct for w in cfg.watch
            if w.board_max_residual_pct is not None}
    sigmas = {w.label: w.min_sigma for w in cfg.watch if w.min_sigma is not None}
    watched = [
        s for s in watched
        if (s.matched_label not in bars
            or (s.residual_pct is not None and s.residual_pct <= bars[s.matched_label]))
        and (s.matched_label not in sigmas
             or (s.residual_sigma is not None and s.residual_sigma <= sigmas[s.matched_label]))
    ]

    last_run = store.conn.execute(
        "SELECT started_at, finished_at, ok, fetched, new_vins, price_drops, alerts_sent "
        "FROM runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # Timing: the price index and the markdown/leaving hazard from the
    # committed price history, and a wait-or-buy verdict per Hertz car.
    hedonic = getattr(result, "hedonic", None)
    primary = next((w for w in cfg.watch if w.group == "primary"), None)
    primary_key = ""
    if primary and primary.models:
        make = next((s.listing.make for s in scored
                     if s.listing.model.lower() == primary.models[0].lower()), "")
        primary_key = f"{make.strip().lower()}|{primary.models[0].strip().lower()}"
    try:
        index = market.price_index(store, hedonic, primary_key) if hedonic else []
        hazard = market.hazard(store, primary_key)
    except Exception as exc:                      # never let timing break the board
        import logging
        logging.getLogger(__name__).warning("Market timing skipped: %s", exc)
        index, hazard = [], {}
    first_seen = {r["vin"]: r["first_seen"] for r in store.conn.execute(
        "SELECT vin, first_seen FROM listings").fetchall()}
    extras: dict[str, dict] = {}
    for s in watched:
        l = s.listing
        if (l.source or "hertz") != "hertz":
            continue
        days = market.lot_days(l, first_seen.get(l.vin))
        table = hazard.get("primary" if primary_key and market._key(l) == primary_key else "all", {})
        extras[l.vin] = {"days_on_sale": days,
                         "wait": market.advise(l, s.predicted_landed, table, days) if table else None}

    # What the cost view needs: mileage slopes from the fit, a yearly rate
    # per model from its market curve, mpg per model, warranty terms.
    depreciation = {"per_year_default": cfg.depreciation_per_year,
                    "per_10k": None, "per_10k_sq": None, "per_year_by_model": {}}
    if hedonic is not None and len(hedonic.coefficients) > 2:
        depreciation["per_10k"] = round(hedonic.coefficients[1], 5)
        depreciation["per_10k_sq"] = round(hedonic.coefficients[2], 5)
    for key, curve in (getattr(result, "curves", None) or {}).items():
        depreciation["per_year_by_model"][key] = round(curve.per_year, 4)

    groups = {w.label: w.group for w in cfg.watch}
    watches = [
        {"label": w.label, "tier": w.tier, "source": w.source, "models": w.models,
         "group": w.group,
         "poll_hours": w.poll_hours, "year_min": w.year_min or None,
         "year_max": None if w.year_max >= 9999 else w.year_max,
         "odometer_max": None if w.odometer_max >= 10**8 else w.odometer_max,
         "count": sum(1 for s in watched if s.matched_label == w.label)}
        for w in cfg.watch
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": {"zip": cfg.zip, "radius_miles": cfg.alert_radius_miles},
        "model": {"n": result.hedonic_n, "log_rmse": round(result.hedonic_rmse, 4)},
        "economics": {"tax_rate": cfg.sales_tax_rate, "delivery_base": cfg.delivery_base,
                      "delivery_per_mile": cfg.delivery_per_mile, "title_reg_fees": cfg.title_reg_fees,
                      "fuel_price": cfg.fuel_price, "warranty_reserve": cfg.warranty_reserve,
                      "miles_per_year": cfg.miles_per_year, "holding_years": cfg.holding_years},
        "depreciation": depreciation,
        "mpg_by_model": market.mpg_by_model([s.listing for s in scored]),
        "warranty_by_make": market.WARRANTY,
        "market": {"index": index, "hazard": hazard, "primary": primary_key},
        "preferences": {"colors": cfg.pref_colors, "trims": cfg.pref_trims,
                        "odometer_ideal": cfg.odometer_ideal},
        "last_run": dict(last_run) if last_run else None,
        # How much of the drivable board carries a condition verdict. Counted
        # over the rows actually published, not over every car in the store:
        # the base trims the watches exclude are not on the board and are
        # never surveyed, so counting them could never reach 100%.
        "condition_coverage": _coverage(watched, cfg),
        "skipped_sources": [label for label, _ in getattr(result, "failed_entries", [])],
        "fleet_drops": getattr(result, "fleet_drops", []),
        "watches": watches,
        "listings": [_row(s, cfg, groups, extras.get(s.listing.vin)) for s in watched],
        "dream": dream or {"rows": [], "counts": {}, "skipped": []},
        # Cars.com rows for the SUV tab. No colour, no history, no VIN: the
        # page marks them as Cars.com and never shows them as clean.
        "suv_market": suv or {"rows": [], "counts": {}, "skipped": []},
        # Followed cars (GitHub issues) with their current state and change log.
        "follow": follow or {"enabled": False, "repo": cfg.github_repo, "new_issue_url": "",
                             "reason": "not checked this run", "rows": []},
    }


def write(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=None, separators=(",", ":"), default=str),
                    encoding="utf-8")
