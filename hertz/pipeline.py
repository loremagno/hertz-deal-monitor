"""The monitor run: fetch, store, score, gate, alert.

Order matters here. The cheap, broad work happens first (one sweep of the
whole radius), the expensive per-car work happens last and only for cars that
already cleared the value gate. Fetching an AutoCheck report costs two page
loads, so we spend that only on cars we might actually alert on.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import autocheck as autocheck_mod
from . import benchmark as bm
from . import carmax as carmax_mod
from . import geo, ingest, score
from .config import Config
from .models import Listing, Scored
from .store import Store

logger = logging.getLogger(__name__)


def market_curves(session, cfg: Config, store: Store, max_age_hours: float = 24.0) -> dict:
    """Fit one Cars.com price curve per model that asks for it.

    This is the benchmark for models where our own data is too thin for the
    pooled hedonic to say anything: with two XC60s in inventory, the model's
    own dummy fits its level almost exactly and the residual is arithmetic.
    A curve fitted on the open market for the same model is real evidence.

    Cached for a day. Cars.com blocks a second page request, so each model
    costs exactly one page load.
    """
    wanted = {
        (w.market_make, w.market_slug): _normalize_key(w)
        for w in cfg.watch if w.market_slug and w.market_make
    }
    curves: dict = {}

    for (make, slug), model_key in wanted.items():
        cache_key = f"market_curve:{slug}"
        cached = store.get_meta(cache_key)
        if cached:
            try:
                payload = json.loads(cached)
                fetched = datetime.fromisoformat(payload["fetched_at"])
                if datetime.now() - fetched < timedelta(hours=max_age_hours):
                    curves[model_key] = bm.MarketCurve(**payload["curve"])
                    logger.info("Market curve for %s: cached (n=%d)",
                                slug, payload["curve"]["n"])
                    continue
            except Exception:
                pass

        comps = bm.fetch_comps(session, make, slug, cfg.zip, "all")
        curve = bm.fit_curve(comps)
        if curve is None:
            logger.warning("No market curve for %s (%d comps)", slug, len(comps))
            continue
        curves[model_key] = curve
        store.set_meta(cache_key, json.dumps({
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "curve": curve.__dict__,
        }))

    return curves


def collect_carmax(cfg: Config, store: Store) -> list[Listing]:
    """Sweep CarMax in its own browser, before the main session opens.

    CarMax needs real Chrome, and Playwright's sync API refuses to start a
    second browser while another is live on the same thread, so this cannot
    be nested inside the main session -- it has to run as its own pass.
    """
    entries = [w for w in cfg.watch
               if w.carmax_make and w.carmax_model and _entry_due(store, w)]
    if not entries:
        return []

    out: list[Listing] = []
    try:
        with ingest.BrowserSession(
            headless=True, channel="chrome", postal_code=cfg.zip,
            city_state=cfg.city_state, coordinates=cfg.coordinates,
        ) as session:
            seen: set[str] = set()
            for entry in entries:
                if entry.carmax_model in seen:
                    continue
                seen.add(entry.carmax_model)
                cars = carmax_mod.fetch(session, entry.carmax_make,
                                        entry.carmax_model, cfg.zip)
                # Apply the entry's own year, mileage and trim rules here.
                # CarMax has no server-side filter we can use, so an
                # unfiltered sweep would drop 2018 cars into the price model.
                keep = [c for c in cars
                        if (c.delivery_quote or 0) <= cfg.carmax_max_shipping
                        and entry.matches(c)]
                if len(keep) < len(cars):
                    logger.info("CarMax: dropped %d over the $%d shipping cap",
                                len(cars) - len(keep), cfg.carmax_max_shipping)
                out.extend(keep)
    except Exception as exc:
        logger.warning(
            "CarMax sweep skipped: %s. Real Chrome is required; install it "
            "with: playwright install chrome", exc)
        return []

    logger.info("CarMax: %d listings within the shipping cap", len(out))
    return out


def _normalize_key(entry) -> str:
    """Match `score._normalize_model`: "make|model", lowercased."""
    model = (entry.models[0] if entry.models else "").strip().lower()
    make = (entry.market_make or "").strip().lower()
    return f"{make}|{model}"


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


FORCE_POLL = False   # set by the CLI's --force: ignore every poll window


def _entry_due(store: Store, entry) -> bool:
    if FORCE_POLL:
        return True
    last = store.get_meta(_poll_key(entry))
    if not last:
        return True
    try:
        elapsed = datetime.now() - datetime.fromisoformat(last)
    except ValueError:
        return True
    # A 15-minute tolerance: the cron fires at a fixed minute and the window
    # is stamped a few minutes into the run, so an exact comparison made every
    # "2-hour" watch actually poll every 4 hours.
    return elapsed >= timedelta(hours=entry.poll_hours) - timedelta(minutes=15)


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
    hedonic_n: int = 0
    hedonic_rmse: float = 0.0
    curves: dict = field(default_factory=dict)
    failed_entries: list = field(default_factory=list)   # (label, error) skipped this run


def collect(
    session: ingest.BrowserSession, cfg: Config, store: Store
) -> tuple[list[Listing], set[str], list]:
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
    polled_sources: set[str] = set()
    failed_entries: list[tuple[str, str]] = []
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

        # Entries on a non-Dealer.com source (CarMax) are swept by their own
        # reader. Falling through to Hertz here issued a phantom Hertz query
        # for the CarMax entry's model on every run, logged as carmax:XC60=0.
        if entry.source not in cfg.sources:
            polled_entries.append(entry)
            continue
        source = cfg.sources[entry.source]
        polled_sources.add(entry.source)

        # One source going dark must not take the run down. A 20-second
        # dataLayer timeout on Byers Mazda (a side quest with zero stock)
        # once aborted everything, pausing the CX-50 Hybrid alerts that are
        # the whole point. So a failing entry is skipped: its old rows are
        # kept, its poll window is NOT reset (it retries next run), and it is
        # reported. Only when every entry fails is the run itself a failure.
        try:
            for model in entry.models:
                key = f"{entry.source}:{model}"
                if key in per_model:
                    continue
                years = None
                if entry.year_min:
                    top = min(entry.year_max, entry.year_min + 4)
                    years = list(range(entry.year_min, top + 1))
                listings = ingest.fetch_model_nationwide(
                    session, model, entry.max_pages, source, years)
                per_model[key] = len(listings)
                polled.add(model.lower())
                for listing in listings:
                    by_vin.setdefault(listing.vin, listing)

            # Make-level queries catch a model that is not in inventory today
            # but might appear later, without guessing its exact model string.
            for make in entry.makes:
                key = f"{entry.source}:make:{make}"
                if key in per_model:
                    continue
                listings = ingest.fetch_make_nationwide(session, make, entry.max_pages, source)
                per_model[key] = len(listings)
                polled.update(l.model.lower() for l in listings)
                for listing in listings:
                    by_vin.setdefault(listing.vin, listing)
        except Exception as exc:
            failed_entries.append((entry.label, f"{type(exc).__name__}: {exc}"))
            logger.warning("Skipping %r this run: %s", entry.label, exc)
            continue

        polled_entries.append(entry)

    summary = ", ".join(f"{m}={n}" for m, n in sorted(per_model.items()))
    logger.info("Collected %d vehicles across %d models (%s)", len(by_vin), len(per_model), summary)

    attempted = [e for e in cfg.watch if e.source in cfg.sources and _entry_due(store, e)]
    if attempted and len(failed_entries) == len(attempted):
        # Every Dealer.com source failed: that is the scraper or the network,
        # not one dealer's bad afternoon, and it deserves the loud path.
        raise ingest.BlockedError(
            "Every watch entry failed this run: "
            + "; ".join(f"{label} ({err[:80]})" for label, err in failed_entries)
        )

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

    return listings, polled, polled_sources, polled_entries, failed_entries


def enrich(session: ingest.BrowserSession, store: Store, candidates: list[Scored], cfg: Config) -> None:
    """Attach the per-car delivery quote and AutoCheck report, in place.

    Cached AutoCheck reports are reused for two weeks; a car's history does
    not change often enough to justify re-fetching it every hour.
    """
    for scored in candidates:
        listing = scored.listing

        # CarMax detail pages refuse automation even from real Chrome, and
        # cost a 90-180s cooldown each when tried. They also carry no
        # AutoCheck we can read, so there is nothing to gain by opening them.
        if (listing.source or "") == "carmax":
            continue

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


def run(cfg: Config, dry_run: bool = False, force: bool = False) -> RunResult:
    global FORCE_POLL
    FORCE_POLL = force
    result = RunResult()
    provisional: list[Scored] = []
    store = Store(cfg.db_path)
    seeding = _is_first_run(store)
    if seeding:
        logger.info("First successful run: seeding the baseline, no arrival alerts")
    run_id = store.start_run()

    try:
        # CarMax runs first, in its own real-Chrome browser.
        carmax_listings = collect_carmax(cfg, store)

        with ingest.BrowserSession(
            headless=True,
            postal_code=cfg.zip,
            city_state=cfg.city_state,
            coordinates=cfg.coordinates,
        ) as session:
            (listings, polled_models, polled_sources,
             polled_entries, failed_entries) = collect(session, cfg, store)
            result.failed_entries = failed_entries
            listings.extend(carmax_listings)
            if carmax_listings:
                # Only when the sweep actually returned cars: an empty CarMax
                # result (Chrome missing, block) must not "sell" its rows.
                polled_sources.add("carmax")
                polled_models.update(c.model.lower() for c in carmax_listings)
            curves = market_curves(session, cfg, store)
            result.curves = curves
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

            # The finer guard, learned the hard way: a run can be "green"
            # with every OTHER model present while the target model comes
            # back empty because Hertz throttled that one page. That run
            # then marked all 52 CX-50 Hybrids sold, emptied the dashboard,
            # and queued 52 bogus arrival alerts for the next run. A watched
            # model dropping from a healthy count to zero is a fetch failure
            # until a human says otherwise: its rows are kept, it is not
            # marked polled, and the run reports it.
            counts_now: dict[str, int] = {}
            for l in listings:
                counts_now[l.model.lower()] = counts_now.get(l.model.lower(), 0) + 1
            suspect: list[str] = []
            for model in sorted(polled_models):
                before = store.active_count(model)
                if before >= 5 and counts_now.get(model, 0) == 0:
                    suspect.append(f"{model} ({before} -> 0)")
            if suspect:
                logger.warning("Refusing to mark these models sold on a zero fetch: %s",
                               "; ".join(suspect))
                result.failed_entries.extend(
                    (f"zero-fetch guard: {m}", "model returned no rows after a healthy run")
                    for m in suspect)
                polled_models = {m for m in polled_models
                                 if not (store.active_count(m) >= 5 and counts_now.get(m, 0) == 0)}

            seen: set[str] = set()
            for listing in listings:
                change = store.upsert(listing)
                seen.add(listing.vin)
                if change["is_new"]:
                    result.new_vins.append(listing.vin)
                delta = change["price_delta"]
                if delta and delta < 0:
                    result.price_drops.append((listing.vin, -delta))
            store.mark_inactive(seen, polled_models, polled_sources)

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
            result.hedonic_n = hedonic.n_obs if hedonic else 0
            result.hedonic_rmse = hedonic.rmse if hedonic else 0.0

            scored_all = [
                score.score_listing(
                    l, cfg, hedonic, price_drop_30d=store.price_drop_since(l.vin),
                    market_curves=curves,
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
            # Same model only. Comparing across models produced a digest box
            # recommending a Honolulu XC40 Core against a Toledo CX-50 Hybrid.
            target_model = result.best_drivable.listing.model.lower()
            far = [
                s for s in result.watched
                if (s.listing.geodist or 0) > cfg.alert_radius_miles
                and s.listing.model.lower() == target_model
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
