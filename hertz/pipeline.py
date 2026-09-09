"""The monitor run: fetch, store, score, gate, alert.

Order matters here. The cheap, broad work happens first (one sweep of the
whole radius), the expensive per-car work happens last and only for cars that
already cleared the value gate. Fetching an AutoCheck report costs two page
loads, so we spend that only on cars we might actually alert on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import autocheck as autocheck_mod
from . import geo, ingest, score
from .config import Config
from .models import Listing, Scored
from .store import Store

logger = logging.getLogger(__name__)


def _is_first_run(store: Store) -> bool:
    """True when no earlier run has succeeded.

    On a fresh database every car looks newly listed, so firing arrival
    alerts would send one message per vehicle on the very first run. The
    first run seeds the baseline silently instead.
    """
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE ok = 1 AND fetched > 0"
    ).fetchone()
    return not row or not row["n"]


def _wants_new_alert(scored, cfg: Config) -> bool:
    """Does a watch entry want to hear that this car has just appeared?

    Separate from the value gate on purpose. "Something good came online" and
    "something is underpriced" are different events, and for a buyer who is
    waiting rather than ready, the first one matters more.
    """
    entry = next(
        (w for w in cfg.watch
         if w.tier.upper() == scored.tier and w.label == scored.matched_label),
        None,
    )
    if entry is None or not entry.alert_on_new:
        return False
    if scored.residual_pct is None:
        return True                     # no prediction yet: still worth knowing
    return scored.residual_pct <= entry.new_max_residual_pct


def _poll_key(entry) -> str:
    return f"last_poll:{entry.tier}:{entry.label}"


def _entry_due(store: Store, entry) -> bool:
    last = store.get_meta(_poll_key(entry))
    if not last:
        return True
    try:
        elapsed = datetime.now() - datetime.fromisoformat(last)
    except ValueError:
        return True
    return elapsed >= timedelta(hours=entry.poll_hours)


def _mark_entry_polled(store: Store, entry) -> None:
    store.set_meta(_poll_key(entry), datetime.now().isoformat(timespec="seconds"))


@dataclass
class RunResult:
    fetched: int = 0
    new_vins: list[str] = field(default_factory=list)
    price_drops: list[tuple[str, int]] = field(default_factory=list)
    alerts: list[Scored] = field(default_factory=list)
    watched: list[Scored] = field(default_factory=list)
    all_scored: list[Scored] = field(default_factory=list)
    national_pick: Scored | None = None
    best_drivable: Scored | None = None
    ok: bool = True
    error: str | None = None
    hedonic_fitted: bool = False


def collect(
    session: ingest.BrowserSession, cfg: Config, store: Store
) -> tuple[list[Listing], set[str]]:
    """One nationwide query per due watched model.

    Returns the listings, the set of model names actually polled (marking
    every unseen VIN inactive would otherwise "sell" every car of a model
    this run did not re-poll), and the watch entries whose poll window the
    caller should reset once the data is safely stored.

    Distance is computed locally in `geo`, not filtered server-side: Hertz's
    `geoRadius` accepts only a fixed set of values and is silently ignored
    when the session has no location.
    """
    by_vin: dict[str, Listing] = {}
    per_model: dict[str, int] = {}
    polled: set[str] = set()
    # Entries whose poll window should be reset -- but only once the run has
    # actually stored what it fetched. A run that dies after collecting would
    # otherwise consume the window and skip the model until it reopens.
    polled_entries: list = []

    for entry in cfg.watch:
        if not _entry_due(store, entry):
            logger.info(
                "Skipping %r; polled within the last %.0fh", entry.label, entry.poll_hours
            )
            continue

        source = cfg.sources.get(entry.source) or ingest.HERTZ

        for model in entry.models:
            key = f"{entry.source}:{model}"
            if key in per_model:
                continue
            listings = ingest.fetch_model_nationwide(session, model, entry.max_pages, source)
            per_model[key] = len(listings)
            polled.add(model.lower())
            for listing in listings:
                by_vin.setdefault(listing.vin, listing)

        # Make-level queries catch a model that is not in inventory today but
        # might appear later, without needing to guess its exact model string.
        for make in entry.makes:
            key = f"{entry.source}:make:{make}"
            if key in per_model:
                continue
            listings = ingest.fetch_make_nationwide(session, make, entry.max_pages, source)
            per_model[key] = len(listings)
            polled.update(l.model.lower() for l in listings)
            for listing in listings:
                by_vin.setdefault(listing.vin, listing)

        polled_entries.append(entry)

    summary = ", ".join(f"{m}={n}" for m, n in sorted(per_model.items()))
    logger.info("Collected %d vehicles across %d models (%s)", len(by_vin), len(per_model), summary)

    missing = [m for m, n in per_model.items() if n == 0]
    if missing:
        logger.warning("These model strings matched nothing: %s", ", ".join(missing))

    listings = list(by_vin.values())

    home = geo.parse_coordinates(cfg.coordinates)
    if home is None:
        raise ValueError(f"Could not parse home coordinates {cfg.coordinates!r}")
    resolved, discovered = geo.annotate_distances(listings, home, store.load_geocache())
    store.save_geocache(discovered)
    logger.info(
        "Distances resolved for %d of %d vehicles (%d postal codes newly geocoded)",
        resolved, len(listings), len(discovered),
    )

    return listings, polled, polled_entries


def enrich(session: ingest.BrowserSession, store: Store, candidates: list[Scored], cfg: Config) -> None:
    """Attach the per-car delivery quote and AutoCheck report, in place.

    Cached AutoCheck reports are reused for two weeks; a car's history does
    not change often enough to justify re-fetching it every hour.
    """
    for scored in candidates:
        listing = scored.listing

        cached = store.get_autocheck(listing.vin)
        if cached is not None:
            scored.autocheck = cached

        # Rent2Buy cars are collected in person, so there is no delivery quote
        # to look up; only the AutoCheck report needs a page load.
        needs_delivery = listing.delivery_quote is None and not listing.is_rent2buy
        if cached is not None and not needs_delivery:
            continue

        try:
            details = ingest.fetch_vdp_details(session, listing.url)
        except Exception as exc:
            logger.warning("Detail page failed for %s: %s", listing.vin, exc)
            continue

        if details.get("delivery_quote") is not None:
            listing.delivery_quote = details["delivery_quote"]
            store.upsert(listing)

        if cached is None:
            report = autocheck_mod.fetch_autocheck(session, details.get("autocheck_url", ""), listing.vin)
            if report is not None:
                store.save_autocheck(report)
                scored.autocheck = report


def run(cfg: Config, dry_run: bool = False) -> RunResult:
    result = RunResult()
    provisional: list[Scored] = []
    store = Store(cfg.db_path)
    seeding = _is_first_run(store)
    if seeding:
        logger.info("First successful run: seeding the baseline, no arrival alerts")
    run_id = store.start_run()

    try:
        with ingest.BrowserSession(
            headless=True,
            postal_code=cfg.zip,
            city_state=cfg.city_state,
            coordinates=cfg.coordinates,
        ) as session:
            listings, polled_models, polled_entries = collect(session, cfg, store)
            result.fetched = len(listings)

            # Silent-failure guard. An empty result after a healthy run means
            # the scraper broke, not that Hertz sold every car. The previous
            # version of this project failed silently for months.
            #
            # Only meaningful when something was actually due to be polled:
            # a run where every watchlist entry is still inside its polling
            # window legitimately fetches nothing.
            previous = store.last_successful_fetch_count()
            if polled_models and not listings and previous > 0:
                raise ingest.BlockedError(
                    f"Fetched 0 vehicles but the last good run saw {previous}. "
                    "Treating this as a scraper failure, not an empty market."
                )

            seen: set[str] = set()
            for listing in listings:
                change = store.upsert(listing)
                seen.add(listing.vin)
                if change["is_new"]:
                    result.new_vins.append(listing.vin)
                delta = change["price_delta"]
                if delta and delta < 0:
                    result.price_drops.append((listing.vin, -delta))
            store.mark_inactive(seen, polled_models)

            # Safe to reset the poll windows now that the rows are committed.
            for entry in polled_entries:
                _mark_entry_polled(store, entry)

            # Score everything currently known, not only what this run
            # re-polled. Watchlist tiers poll on different schedules, so
            # otherwise most runs would render a board containing just the
            # tier-A model and fit the price model on a fraction of the data.
            known = [Listing.from_row(r) for r in store.active_listings()]
            logger.info("Scoring %d active listings (%d fetched this run)",
                        len(known), len(listings))

            hedonic = score.fit_hedonic(known)
            result.hedonic_fitted = hedonic is not None

            scored_all = [
                score.score_listing(
                    l, cfg, hedonic, price_drop_30d=store.price_drop_since(l.vin)
                )
                for l in known
            ]
            result.all_scored = scored_all
            result.watched = [s for s in scored_all if s.tier]

            drivable = [
                s for s in result.watched
                if (s.listing.geodist or 9e9) <= cfg.alert_radius_miles
            ]
            result.best_drivable = min(drivable, key=lambda s: s.landed_cost, default=None)

            # Two reasons to spend an AutoCheck lookup on a car: it cleared
            # the value gate, or it is newly listed on a watch that asked to
            # hear about arrivals. New inventory is the event most likely to
            # be missed, because a good car can arrive and sell inside a week.
            new_vins = set() if seeding else set(result.new_vins)
            for scored in result.watched:
                distance = scored.listing.geodist
                if distance is None or distance > cfg.alert_radius_miles:
                    continue
                if score.value_gate(scored, cfg)[0]:
                    provisional.append(scored)
                elif scored.vin in new_vins and _wants_new_alert(scored, cfg):
                    provisional.append(scored)

            if provisional:
                logger.info("Enriching %d candidates with AutoCheck", len(provisional))
                enrich(session, store, provisional, cfg)

        new_vins = set() if seeding else set(result.new_vins)
        for scored in provisional:
            ok, reasons = score.qualifies(scored, cfg)
            is_new = scored.vin in new_vins

            if not ok and is_new and _wants_new_alert(scored, cfg):
                # A new arrival still has to be clean; it just does not have
                # to be a bargain. Condition is never waived.
                report = scored.autocheck
                if report is not None and report.is_clean:
                    ok = True
                    reasons = [f"Newly listed at {scored.listing.lot}"] + [
                        r for r in reasons if "below predicted" in r or "AutoCheck" in r
                    ]
                    reasons.append("AutoCheck clean: no accidents, clean title, no odometer flags")
                elif report is not None:
                    reasons = [f"Newly listed, but AutoCheck: {'; '.join(report.concerns)}"]

            scored.reasons = reasons
            if not ok:
                logger.info("  %s not alerting: %s", scored.vin, "; ".join(reasons))
                continue
            if not store.should_alert(scored.vin, scored.listing.price, cfg.realert_price_drop):
                logger.info("  %s already alerted at this price", scored.vin)
                continue
            result.alerts.append(scored)

        # A nationwide car is worth mentioning only if it clearly beats the
        # best car you could drive to, after delivery.
        if result.best_drivable:
            far = [
                s for s in result.watched
                if (s.listing.geodist or 0) > cfg.alert_radius_miles
                and result.best_drivable.landed_cost - s.landed_cost >= cfg.national_mention_min_saving
            ]
            result.national_pick = min(far, key=lambda s: s.landed_cost, default=None)

        if not dry_run:
            for scored in result.alerts:
                store.record_alert(
                    scored.vin, scored.tier, "deal",
                    scored.listing.price, scored.landed_cost, scored.residual_pct,
                )

        store.finish_run(
            run_id, True, result.fetched, len(result.new_vins),
            len(result.price_drops), len(result.alerts),
        )

    except Exception as exc:
        logger.exception("Run failed")
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        store.finish_run(run_id, False, result.fetched, 0, 0, 0, result.error)
    finally:
        store.close()

    return result
