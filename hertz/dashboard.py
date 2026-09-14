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

from .config import Config
from .score import interior_tier, preference_fit


def _row(s, cfg: Config, groups: dict | None = None) -> dict:
    groups = groups or {}
    l = s.listing
    report = s.autocheck
    if report is None:
        condition, condition_kind = ("certified, not independently read" if l.certified
                                     else "not checked"), ("cert" if l.certified else "none")
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
    }


def build(result, cfg: Config, dream: dict | None, store, suv: dict | None = None) -> dict:
    """Assemble the document the page renders."""
    scored = [s for s in result.all_scored if s.listing.price]
    watched = [s for s in scored if s.tier]

    # A watch whose whole point is "only if very discounted" must not put
    # every car it matches on the board. The first cut of the lane showed
    # 237 rows, most priced ABOVE prediction. Rows from such a lane are kept
    # only when they clear the lane's own bar; the alert path already does
    # this, the board did not.
    bars = {w.label: w.threshold_pct for w in cfg.watch if w.label.lower().startswith("very discounted")}
    watched = [
        s for s in watched
        if s.matched_label not in bars
        or (s.residual_pct is not None and s.residual_pct <= bars[s.matched_label])
    ]

    last_run = store.conn.execute(
        "SELECT started_at, finished_at, ok, fetched, new_vins, price_drops, alerts_sent "
        "FROM runs WHERE ok = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()

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
                      "delivery_per_mile": cfg.delivery_per_mile},
        "preferences": {"colors": cfg.pref_colors, "trims": cfg.pref_trims,
                        "odometer_ideal": cfg.odometer_ideal},
        "last_run": dict(last_run) if last_run else None,
        "skipped_sources": [label for label, _ in getattr(result, "failed_entries", [])],
        "watches": watches,
        "listings": [_row(s, cfg, groups) for s in watched],
        "dream": dream or {"rows": [], "counts": {}, "skipped": []},
        # Cars.com rows for the SUV tab. No colour, no history, no VIN: the
        # page marks them as Cars.com and never shows them as clean.
        "suv_market": suv or {"rows": [], "counts": {}, "skipped": []},
    }


def write(doc: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=None, separators=(",", ":"), default=str),
                    encoding="utf-8")
