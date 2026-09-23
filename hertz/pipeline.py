"""The monitor run: fetch, store, score, gate, alert.

Order matters here. The cheap, broad work happens first (one sweep of the
whole radius), the expensive per-car work happens last and only for cars that
already cleared the value gate. Fetching an AutoCheck report costs two page
loads, so we spend that only on cars we might actually alert on.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import autocheck as autocheck_mod
from . import benchmark as bm
from . import carmax as carmax_mod
from . import enterprise as enterprise_mod
from . import mazdausa as mazdausa_mod
from . import clock, geo, ingest, score
from .config import CONDITION_SOURCES, Config
from .models import Listing, Scored
from .store import Store

logger = logging.getLogger(__name__)


# Committed market curves, produced locally by `python -m hertz --curves`.
# The runner is Cloudflare-challenged on Cars.com more often than not, and a
# run without a curve scores the XC60 on a model dummy over nine of our own
# rows. Same idea as the dream seed: a residential connection fills it, the
# committed file out-ranks an older cache, and a failed live fetch falls
# back to whatever curve is cached rather than to nothing.
CURVE_SEED = "market_curves.json"


def market_curve_targets(cfg: Config) -> dict:
    """{(cars_make, slug): (model_key, year_min)} for every benchmarked model.

    Driven by the `[[market]]` table, not by watches: a watch naming
    fourteen models could only ever key one curve, which is why the primary
    target had no outside benchmark at all. Legacy `market_slug` on a watch
    is still honoured so nothing breaks mid-migration.
    """
    wanted: dict = {}
    for m in cfg.market:
        wanted[(m.cars_make, m.slug)] = (m.key, m.year_min)
    for w in cfg.watch:
        if not (w.market_slug and w.market_make):
            continue
        wanted.setdefault((w.market_make, w.market_slug), (_normalize_key(w), w.year_min or 0))
    return wanted


def _curve_cache_key(slug: str, year_min: int) -> str:
    # The year is part of the key so a curve fitted on the unfiltered
    # market is never reused once the filter changes.
    # v2: the trim tier moved to the make ladders' 0-3 scale, and a curve
    # fitted on the old 0-2 scale mispredicts by a rung.
    return f"market_curve:{slug}:y{year_min or 'all'}:v2"


def _curve_payload(text: str | None) -> dict | None:
    """A cached/seeded curve, or None if the text is missing or malformed."""
    if not text:
        return None
    try:
        payload = json.loads(text)
        datetime.fromisoformat(payload["fetched_at"])
        bm.MarketCurve(**payload["curve"])
        return payload
    except Exception:
        return None


def adopt_curve_seed(cfg: Config, store: Store) -> int:
    """Copy committed curves into the cache when they are newer than it."""
    path = cfg.base_dir / "docs" / CURVE_SEED
    if not path.exists():
        return 0
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Market curve seed unreadable: %s", exc)
        return 0
    adopted = 0
    for key, payload in (seed.get("curves") or {}).items():
        if _curve_payload(json.dumps(payload)) is None:
            continue
        cached = _curve_payload(store.get_meta(key))
        if cached is None or payload["fetched_at"] > cached["fetched_at"]:
            store.set_meta(key, json.dumps(payload))
            adopted += 1
    if adopted:
        logger.info("Market curves: adopted %d from the committed seed", adopted)
    return adopted


def market_curves(session, cfg: Config, store: Store, max_age_hours: float = 24.0) -> dict:
    """Fit one Cars.com price curve per model that asks for it.

    This is the benchmark for models where our own data is too thin for the
    pooled hedonic to say anything: with two XC60s in inventory, the model's
    own dummy fits its level almost exactly and the residual is arithmetic.
    A curve fitted on the open market for the same model is real evidence.

    Cached for a day, seeded from the committed file, and a failed refresh
    keeps the cached curve however old: prices drift by the month, and the
    alternative is a model dummy on a dozen of our own rows.
    """
    adopt_curve_seed(cfg, store)
    curves: dict = {}

    for (make, slug), (model_key, year_min) in market_curve_targets(cfg).items():
        cache_key = _curve_cache_key(slug, year_min)
        cached = _curve_payload(store.get_meta(cache_key))
        if cached is not None:
            age = clock.now() - datetime.fromisoformat(cached["fetched_at"])
            if age < timedelta(hours=max_age_hours):
                curves[model_key] = bm.MarketCurve(**cached["curve"])
                logger.info("Market curve for %s: cached (n=%d, %.0f h old)",
                            slug, cached["curve"]["n"], age.total_seconds() / 3600)
                continue

        comps = bm.fetch_comps(session, make, slug, cfg.zip, "all",
                               year_min=year_min or None)
        curve = bm.fit_curve(comps)
        if curve is None:
            if cached is not None:
                curves[model_key] = bm.MarketCurve(**cached["curve"])
                logger.warning("No fresh market curve for %s (%d comps); keeping the one "
                               "fitted %s", slug, len(comps), cached["fetched_at"])
            else:
                logger.warning("No market curve for %s (%d comps)", slug, len(comps))
            continue
        curves[model_key] = curve
        store.set_meta(cache_key, json.dumps({
            "fetched_at": clock.now_iso(),
            "year_min": year_min,
            "curve": curve.__dict__,
        }))

    return curves


def refresh_market_curves(cfg: Config, store: Store, pause_seconds: float = 45.0,
                          pages: int = 3, only: set[str] | None = None) -> dict:
    """Refit every market curve and write the committed seed.

    Meant to run locally: a residential connection gets through where the
    runner is challenged. Cars.com serves 24 listings a page and blocks the
    second request in a session, so each page gets its own browser, with a
    pause between. Two different 24-car "best match" pages moved the XC60
    prediction by two points; three pages is 70-odd cars and a steadier
    curve. A model that fails keeps its previous seed entry.
    """
    now = clock.now_iso()
    seed: dict = {"seeded_at": now, "curves": {}}
    first = True
    targets = market_curve_targets(cfg)
    if only:
        targets = {k: v for k, v in targets.items() if k[1] in only}
        logger.info("Refitting only: %s", ", ".join(sorted(only)))
    for (make, slug), (model_key, year_min) in targets.items():
        comps: list = []
        seen: set = set()
        for page in range(1, pages + 1):
            if not first:
                time.sleep(pause_seconds)
            first = False
            try:
                with ingest.BrowserSession(headless=True, min_delay=1.0, max_delay=1.5,
                                           postal_code=cfg.zip, city_state=cfg.city_state,
                                           coordinates=cfg.coordinates) as session:
                    found = bm.fetch_comps(session, make, slug, cfg.zip, "all",
                                           year_min=year_min or None, start_page=page)
            except Exception as exc:
                logger.warning("Market curve for %s: page %d not fetched: %s", slug, page, exc)
                break
            fresh = [c for c in found if (c.vin or c.url) not in seen]
            seen.update(c.vin or c.url for c in fresh)
            comps.extend(fresh)
            if not fresh:
                break
        curve = bm.fit_curve(comps)
        if curve is None:
            logger.warning("Market curve for %s not refreshed (%d comps)", slug, len(comps))
            continue
        payload = {"fetched_at": now, "year_min": year_min, "curve": curve.__dict__}
        key = _curve_cache_key(slug, year_min)
        store.set_meta(key, json.dumps(payload))
        seed["curves"][key] = payload
        logger.info("Market curve for %s refitted: n=%d, RMSE %.3f", slug, curve.n, curve.rmse)

    path = cfg.base_dir / "docs" / CURVE_SEED
    if path.exists():
        try:
            for key, payload in (json.loads(path.read_text(encoding="utf-8")).get("curves") or {}).items():
                seed["curves"].setdefault(key, payload)
        except Exception:
            pass
    path.write_text(json.dumps(seed, indent=1), encoding="utf-8")
    return seed


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


def collect_enterprise(cfg: Config, store: Store) -> list[Listing]:
    """Sweep Enterprise Car Sales through its API, in its own browser.

    One entry per make list: every due watch with `source = "enterprise"`
    contributes its makes (or the makes of its models are unknown, so a
    watch on this source must name `makes`), and the tightest mileage cap
    and model-year floor among them are applied at the API.
    """
    entries = [w for w in cfg.watch if w.source == "enterprise" and _entry_due(store, w)]
    if not entries:
        return []
    makes: list[str] = []
    for w in entries:
        for m in w.makes:
            if m not in makes:
                makes.append(m)
    if not makes:
        logger.warning("Enterprise watches must name makes; nothing to sweep")
        return []
    odometer_max = min(w.odometer_max for w in entries)
    year_min = min((w.year_min for w in entries if w.year_min), default=0)
    radius = max((cfg.alert_radius_miles,) + tuple(w.max_pages * 0 + cfg.alert_radius_miles for w in entries))
    home = geo.parse_coordinates(cfg.coordinates)
    out: list[Listing] = []
    try:
        with ingest.BrowserSession(headless=True, postal_code=cfg.zip,
                                   city_state=cfg.city_state, coordinates=cfg.coordinates) as session:
            cars = enterprise_mod.fetch(session, makes, home, radius, odometer_max, year_min)
        out = [c for c in cars if any(w.matches(c) for w in entries)]
        # This pass runs outside collect(), which geocodes its own rows, so
        # distances are resolved here; without them the cars sat behind the
        # board's radius filter and could never alert.
        if out and home:
            _, discovered = geo.annotate_distances(out, home, store.load_geocache())
            store.save_geocache(discovered)
        logger.info("Enterprise: %d of %d cars match a watch", len(out), len(cars))
    except Exception as exc:
        logger.warning("Enterprise sweep skipped: %s", exc)
        return []
    return out


def collect_mazdausa(cfg: Config, store: Store) -> list[Listing]:
    """Sweep Mazda USA's inventory locator in its own browser, before the
    main session opens: every due watch naming the "mazdausa" source
    contributes its models; new and certified stock at every dealer within
    the alert radius comes back with colours, trims and stickers.

    A VIN the store already holds from a dealer's own feed is left to that
    feed: Byers's page carries the doc fee, the internet price and the
    manufacturer cash, and the locator carries only the sticker. Without
    that rule the row's source would flip between the two every poll.
    """
    # Every Mazda USA lane is matched on every sweep, not only the lanes that
    # are due. The lanes poll on different cadences, and matching only the
    # due ones dropped cars that belonged to a lane that was not, which then
    # read as sold until that lane came round again.
    all_entries = [w for w in cfg.watch if "mazdausa" in w.sources]
    if not any(_entry_due(store, w) for w in all_entries):
        return []
    entries = all_entries
    models: list[str] = []
    for w in entries:
        for m in w.models:
            if m not in models:
                models.append(m)
    if not models:
        logger.warning("Mazda USA watches must name models; nothing to sweep")
        return []
    home = geo.parse_coordinates(cfg.coordinates)
    try:
        with ingest.BrowserSession(headless=True, postal_code=cfg.zip,
                                   city_state=cfg.city_state, coordinates=cfg.coordinates,
                                   cookie_domains=()) as session:
            cars = mazdausa_mod.fetch(session, cfg.zip, cfg.alert_radius_miles, models)
    except Exception as exc:
        logger.warning("Mazda USA sweep skipped: %s", exc)
        return []
    held = store.sources_for(c.vin for c in cars)
    deferred = [c for c in cars if held.get(c.vin) not in (None, mazdausa_mod.SOURCE)]
    cars = [c for c in cars if held.get(c.vin) in (None, mazdausa_mod.SOURCE)]
    out = [c for c in cars if any(w.matches(c) for w in entries)]
    if out and home:
        _, discovered = geo.annotate_distances(out, home, store.load_geocache())
        store.save_geocache(discovered)
    logger.info("Mazda USA: %d of %d cars match a watch (%d left to the dealer's own feed)",
                len(out), len(cars), len(deferred))
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


def _wants_markdown_alert(scored, cfg: Config) -> bool:
    """Has a watched car just been cut into genuinely cheap territory?

    Its watch must ask for markdown alerts, the cut must be real money
    (checked by the caller), and the car must now sit below EITHER bar:
    `markdown_pct` below its model, or `markdown_sigma` of that model's own
    spread. Either, not both, because the two bars mean different things
    across models: the XC60's spread is 6.0% of price, so -1.5 sigma there
    is -9% and unreachable, while the CX-50 Hybrid's is 3.2%.

    Condition is gated afterwards, exactly as everywhere else.
    """
    entry = next(
        (w for w in cfg.watch
         if w.tier.upper() == scored.tier and w.label == scored.matched_label),
        None,
    )
    if entry is None or not entry.markdown_alert:
        return False
    by_pct = scored.residual_pct is not None and scored.residual_pct <= cfg.markdown_pct
    by_sigma = scored.residual_sigma is not None and scored.residual_sigma <= cfg.markdown_sigma
    return by_pct or by_sigma


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
        elapsed = clock.now() - datetime.fromisoformat(last)
    except ValueError:
        return True
    # A 15-minute tolerance: the cron fires at a fixed minute and the window
    # is stamped a few minutes into the run, so an exact comparison made every
    # "2-hour" watch actually poll every 4 hours.
    return elapsed >= timedelta(hours=entry.poll_hours) - timedelta(minutes=15)


def _mark_entry_polled(store: Store, entry) -> None:
    store.set_meta(_poll_key(entry), clock.now_iso())


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
    hedonic: object = None                # the fitted model, for the index and the cost view
    curves: dict = field(default_factory=dict)
    failed_entries: list = field(default_factory=list)   # (label, error) skipped this run
    new_models: list = field(default_factory=list)       # (make, model, count, min price) first seen this run
    condition: dict = field(default_factory=dict)        # {"checked", "reports", "skipped"}
    fleet_drops: list = field(default_factory=list)      # models Hertz just offloaded in bulk


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
    # The (source, model) pairs this run actually asked for. Inactivation and
    # the zero-fetch guard are both scoped to these, never to a set of models
    # crossed with a set of sources.
    polled: set[tuple[str, str]] = set()
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
        dealer_sources = [s for s in entry.sources if s in cfg.sources]
        if not dealer_sources:
            # CarMax, Enterprise and Mazda USA have their own passes, which
            # mark their entries polled only when the sweep returned cars.
            if not (set(entry.sources) & {"carmax", "enterprise", "mazdausa"}):
                polled_entries.append(entry)
            continue

        # One source going dark must not take the run down. A 20-second
        # dataLayer timeout on Byers Mazda (a side quest with zero stock)
        # once aborted everything, pausing the CX-50 Hybrid alerts that are
        # the whole point. So a failing entry is skipped: its old rows are
        # kept, its poll window is NOT reset (it retries next run), and it is
        # reported. Only when every entry fails is the run itself a failure.
        try:
          for src_name in dealer_sources:
            source = cfg.sources[src_name]
            for model in entry.models:
                key = f"{src_name}:{model}"
                if key in per_model:
                    continue
                years = None
                if entry.year_min:
                    top = min(entry.year_max, entry.year_min + 4)
                    years = list(range(entry.year_min, top + 1))
                listings = ingest.fetch_model_nationwide(
                    session, model, entry.max_pages, source, years,
                    expected=store.active_count(model, src_name))
                per_model[key] = len(listings)
                polled.add((src_name, model.lower()))
                for listing in listings:
                    by_vin.setdefault(listing.vin, listing)

            # Make-level queries catch a model that is not in inventory today
            # but might appear later, without guessing its exact model string.
            # A sweep entry passes its band, mileage cap and body styles to
            # Hertz, which applies them server-side.
            for make in entry.makes:
                key = f"{src_name}:make:{make}"
                if key in per_model:
                    continue
                years = None
                if entry.year_min:
                    years = list(range(entry.year_min, min(entry.year_max, entry.year_min + 4) + 1))
                listings = ingest.fetch_make_nationwide(
                    session, make, entry.max_pages, source, years=years,
                    price_band=((entry.price_min, entry.price_max)
                                if entry.price_min or entry.price_max else None),
                    odometer_max=(entry.odometer_max if entry.odometer_max < 10**8 else None),
                    body_styles=entry.body_styles or None)
                per_model[key] = len(listings)
                # A capped make sweep must not "sell" the models it did not
                # reach: only models it actually returned count as polled.
                polled.update((src_name, l.model.lower()) for l in listings)
                for listing in listings:
                    by_vin.setdefault(listing.vin, listing)
        except Exception as exc:
            failed_entries.append((entry.label, f"{type(exc).__name__}: {exc}"))
            logger.warning("Skipping %r this run: %s", entry.label, exc)
            continue

        polled_entries.append(entry)

    summary = ", ".join(f"{m}={n}" for m, n in sorted(per_model.items()))
    logger.info("Collected %d vehicles across %d models (%s)", len(by_vin), len(per_model), summary)
    for label, coverage in ingest.COVERAGE.items():
        store.set_meta(f"coverage:{label}", coverage)

    attempted = [e for e in cfg.watch
                 if any(s in cfg.sources for s in e.sources) and _entry_due(store, e)]
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

    return listings, polled, polled_entries, failed_entries


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
        # A new car has no history and no delivery quote to read either.
        if (listing.source or "") == "carmax" or listing.is_new:
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
        if details.get("carfax_url"):
            scored.history_url = details["carfax_url"]

        if cached is None:
            report = autocheck_mod.fetch_autocheck(session, details.get("autocheck_url", ""), listing.vin)
            if report is not None:
                store.save_autocheck(report)
                scored.autocheck = report


def detect_fleet_drops(store: Store, cfg: Config, listings: list[Listing],
                       new_vins: set[str], polled_pairs: set[tuple[str, str]]) -> list[dict]:
    """Record this run's per-model arrivals and spot a fleet offload.

    Lorenzo's read of Hertz, from having bought there before: they offload a
    model in waves, a flurry of listings goes up, and demand catches up
    within days. Being present for the flurry is the whole game, and the
    residual cannot see it. A model-wide price drop is absorbed by that
    model's own fixed effect, so three hundred cheap CX-50s would each read
    as perfectly average against a benchmark they themselves just moved.
    So this watches COUNTS, against that pair's own recent history, plus
    the model's own price level.

    Only arrivals that actually match a watch are counted, or a flood of
    base-trim Sportages would fire every time.
    """
    by_pair: dict[tuple[str, str], list[Listing]] = {}
    for listing in listings:
        by_pair.setdefault(((listing.source or "hertz"), listing.model.lower()), []).append(listing)

    drops: list[dict] = []
    for source, model in sorted(polled_pairs):
        cars = by_pair.get((source, model), [])
        arrivals = [c for c in cars if c.vin in new_vins]
        matched = [c for c in arrivals if score.match_watch(c, cfg) is not None]
        drivable = [c for c in matched
                    if c.geodist is not None and c.geodist <= cfg.alert_radius_miles]
        prices = sorted(c.price for c in cars if c.price)
        median_price = prices[len(prices) // 2] if prices else None
        cheapest = min((c.price for c in matched if c.price), default=None)

        history = store.model_poll_history(source, model, cfg.fleet_window_days,
                                           exclude_latest=False)
        store.record_model_poll(source, model, len(cars), len(arrivals), len(matched),
                                len(drivable), cheapest, median_price)
        if not cfg.fleet_alert or not matched:
            continue
        # Two earlier polls at least: the first sight of a pair lists its
        # whole stock as new, which is a baseline and not a wave.
        if len(history) < 2:
            continue

        # Judged on the cars he could actually drive to. A wave of ninety
        # Atlases in Texas is not an opportunity: delivery runs $2 a mile.
        # Both tests are on the drivable count, one absolute and one against
        # this pair's own recent rate, and both must pass.
        typical_all = sorted(h["matched"] or 0 for h in history)
        typical = typical_all[len(typical_all) // 2]
        typical_near_all = sorted(h["drivable"] or 0 for h in history)
        typical_near = typical_near_all[len(typical_near_all) // 2]
        enough = len(drivable) >= cfg.fleet_min_drivable
        surge = len(drivable) >= cfg.fleet_multiple * max(typical_near, 1)
        if not (enough and surge):
            continue

        # Where the model's own price level sits against its recent norm,
        # which is the part a residual can never show.
        levels = [h["median_price"] for h in history if h["median_price"]]
        level_before = sorted(levels)[len(levels) // 2] if levels else None
        drops.append({
            "source": source, "model": cars[0].model if cars else model,
            "arrived": len(matched), "drivable": len(drivable),
            # Report the comparison the rule actually made: drivable against
            # this pair's usual drivable haul. `typical` is the national
            # figure, kept for context only.
            "typical": typical, "typical_drivable": typical_near, "cheapest": cheapest,
            "median_price": median_price, "median_before": level_before,
            "examples": [
                {"label": c.label, "price": c.price, "miles": c.odometer,
                 "distance": None if c.geodist is None else round(c.geodist),
                 "url": c.url}
                for c in sorted(drivable or matched,
                                key=lambda c: c.price or 9 * 10 ** 9)[:5]
            ],
        })
        logger.info("Fleet drop: %s listed %d %s (typical %d), %d drivable, cheapest %s",
                    source, len(matched), model, typical, len(drivable), cheapest)
    return drops


def attach_known_conditions(store: Store, scored_list: list[Scored]) -> int:
    """Hang every history fact we already hold onto the scored cars. Free.

    The board reads condition off the Scored object, and enrich() only ever
    filled that in for cars heading to an alert. So a car whose report was
    bought a week ago still displayed "not checked". This costs no page
    loads and is the reason the column was blank on 97% of the board.
    """
    filled = 0
    for scored in scored_list:
        vin = scored.listing.vin
        if scored.autocheck is None:
            report = store.get_autocheck(vin)
            if report is not None:
                scored.autocheck = report
                filled += 1
        attempt = store.condition_attempt(vin)
        if attempt:
            scored.condition_outcome = attempt.get("outcome") or ""
            if not scored.history_url and attempt.get("history_url"):
                scored.history_url = attempt["history_url"]
        elif scored.autocheck is not None:
            scored.condition_outcome = "report"
    return filled


def survey_conditions(session: ingest.BrowserSession, store: Store,
                      scored_list: list[Scored], cfg: Config) -> dict:
    """Buy a history report for drivable watched cars that have none.

    Condition is the thing Lorenzo was burned by, and a board that shows a
    price without a verdict cannot help him avoid it. Alerts already buy a
    report for their own candidates; this covers everything else he could
    actually drive to, cheapest-against-model first, under a per-run budget
    so the run stays well inside its timeout. Reports cache for two weeks,
    so the cost is a one-off backfill and then only new arrivals.

    Every attempt is recorded, successful or not, so a seller that
    publishes nothing readable is not re-opened every two hours.
    """
    budget = max(0, cfg.condition_survey_per_run)
    if not budget:
        return {"checked": 0, "reports": 0, "skipped": 0}

    due: list[Scored] = []
    for scored in scored_list:
        listing = scored.listing
        if (listing.source or "hertz") not in CONDITION_SOURCES or listing.is_new:
            continue
        if listing.geodist is None or listing.geodist > cfg.alert_radius_miles:
            continue
        if scored.autocheck is not None or store.get_autocheck(listing.vin) is not None:
            continue
        if store.condition_attempt_fresh(listing.vin, cfg.condition_retry_days):
            continue
        due.append(scored)

    # The cars most worth knowing about first: cheapest against their own
    # model, then nearest. A partial backfill should still answer the
    # question that matters before it runs out of budget.
    due.sort(key=lambda s: (s.residual_pct if s.residual_pct is not None else 999.0,
                            s.listing.geodist or 9e9))
    if not due:
        return {"checked": 0, "reports": 0, "skipped": 0}

    logger.info("Condition survey: %d drivable car(s) without a verdict, doing up to %d",
                len(due), budget)
    started = time.monotonic()
    checked = reports = 0
    for scored in due[:budget]:
        if time.monotonic() - started > cfg.condition_survey_seconds:
            logger.info("Condition survey: time budget reached after %d car(s)", checked)
            break
        listing = scored.listing
        checked += 1
        try:
            details = ingest.fetch_vdp_details(session, listing.url)
        except Exception as exc:
            logger.warning("Condition survey: detail page failed for %s: %s", listing.vin, exc)
            continue

        if details.get("delivery_quote") is not None and listing.delivery_quote is None:
            listing.delivery_quote = details["delivery_quote"]
            store.upsert(listing)

        carfax = details.get("carfax_url") or ""
        autocheck_url = details.get("autocheck_url") or ""
        report = None
        if autocheck_url:
            report = autocheck_mod.fetch_autocheck(session, autocheck_url, listing.vin)
        if report is not None:
            store.save_autocheck(report)
            scored.autocheck = report
            reports += 1
            scored.condition_outcome = "report"
            store.record_condition_attempt(listing.vin, "report", carfax)
        elif carfax:
            scored.history_url = carfax
            scored.condition_outcome = "carfax"
            store.record_condition_attempt(listing.vin, "carfax", carfax)
        else:
            scored.condition_outcome = "none"
            store.record_condition_attempt(listing.vin, "none", "")

    logger.info("Condition survey: opened %d car(s), %d report(s) read", checked, reports)
    return {"checked": checked, "reports": reports, "skipped": max(0, len(due) - checked)}


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
        # CarMax runs first, in its own real-Chrome browser; Enterprise in its
        # own too, since its token lives in a page context.
        carmax_listings = collect_carmax(cfg, store)
        enterprise_listings = collect_enterprise(cfg, store)
        mazdausa_listings = collect_mazdausa(cfg, store)

        with ingest.BrowserSession(
            headless=True,
            postal_code=cfg.zip,
            city_state=cfg.city_state,
            coordinates=cfg.coordinates,
        ) as session:
            (listings, polled_pairs,
             polled_entries, failed_entries) = collect(session, cfg, store)
            result.failed_entries = failed_entries

            # New at Hertz: a make|model the store has never held, arriving
            # through a sweep. Computed before the upsert makes it known.
            # "Hertz has started selling X" is the event the sweep exists for.
            sweeps = [w for w in cfg.watch if w.sweep]
            # The first sweep ever would call every 2026 model "new". That
            # run records the baseline silently; arrivals count from the next.
            first_sweep = sweeps and not store.get_meta("sweep_baseline_at")
            if sweeps and first_sweep:
                store.set_meta("sweep_baseline_at", clock.now_iso())
                logger.info("First sweep: recording the model baseline, no new-model push")
            if sweeps and not seeding and not first_sweep:
                known = store.known_models("hertz")
                arrivals: dict[str, list] = {}
                for l in listings:
                    if (l.source or "hertz") != "hertz":
                        continue
                    k = f"{l.make.strip().lower()}|{l.model.strip().lower()}"
                    if k in known or not any(w.matches(l) for w in sweeps):
                        continue
                    arrivals.setdefault(k, []).append(l)
                result.new_models = sorted(
                    (ls[0].make, ls[0].model, len(ls), min(l.price for l in ls if l.price))
                    for ls in arrivals.values() if any(l.price for l in ls))
                if result.new_models:
                    logger.info("New models at Hertz this run: %s",
                                "; ".join(f"{mk} {md} x{n} from ${p:,}" for mk, md, n, p in result.new_models))
            listings.extend(carmax_listings)
            if carmax_listings:
                # Only when the sweep actually returned cars: an empty CarMax
                # result (Chrome missing, block) must not "sell" its rows.
                polled_pairs.update(("carmax", c.model.lower()) for c in carmax_listings)
            listings.extend(enterprise_listings)
            if enterprise_listings:
                polled_pairs.update(("enterprise", c.model.lower()) for c in enterprise_listings)
                for w in cfg.watch:
                    if w.source == "enterprise" and _entry_due(store, w):
                        polled_entries.append(w)
            # The locator's rows join after the dealer feeds, so a VIN the
            # dealer's own page returned this run keeps that page as its source.
            seen_dealer = {l.vin for l in listings}
            mazdausa_listings = [c for c in mazdausa_listings if c.vin not in seen_dealer]
            listings.extend(mazdausa_listings)
            if mazdausa_listings:
                # Only a model whose new AND certified sweeps both reached the
                # API's total may have its unseen cars marked sold.
                for model in {c.model for c in mazdausa_listings}:
                    flags = [v for k, v in mazdausa_mod.COMPLETE.items()
                             if k.split(":", 1)[1].lower() == model.lower()]
                    if flags and all(flags):
                        polled_pairs.add(("mazdausa", model.lower()))
                    else:
                        logger.warning("Mazda USA %s sweep incomplete; none of its cars marked sold",
                                       model)
                for w in cfg.watch:
                    if "mazdausa" in w.sources and w not in polled_entries:
                        polled_entries.append(w)
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
            if polled_pairs and not listings and previous > 0:
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
            counts_now: dict[tuple[str, str], int] = {}
            for l in listings:
                key = ((l.source or "hertz"), l.model.lower())
                counts_now[key] = counts_now.get(key, 0) + 1
            suspect: list[tuple[str, str]] = []
            for source, model in sorted(polled_pairs):
                before = store.active_count(model, source)
                if before >= 5 and counts_now.get((source, model), 0) == 0:
                    suspect.append((source, model))
            if suspect:
                logger.warning(
                    "Refusing to mark these sold on a zero fetch: %s",
                    "; ".join(f"{s}:{m} ({store.active_count(m, s)} -> 0)" for s, m in suspect))
                result.failed_entries.extend(
                    (f"zero-fetch guard: {s}:{m}", "returned no rows after a healthy run")
                    for s, m in suspect)
                polled_pairs = polled_pairs - set(suspect)

            # A source polled for the first time is a baseline, not a wave
            # of arrivals: the first Avis sweep would otherwise push 29 new
            # CX-50 Hybrids at once. VINs from a source the store has never
            # held are stored without counting as new.
            known_sources = {r["source"] for r in store.conn.execute(
                "SELECT DISTINCT source FROM listings").fetchall()}
            source_of = {l.vin: (l.source or "hertz") for l in listings}
            seen: set[str] = set()
            # The first complete Mazda USA sweep stores about a third more
            # cars than any earlier one ever saw. Those are not arrivals, so
            # that sweep is a silent baseline, like a new source's first.
            rebaseline = bool(mazdausa_listings) and not store.get_meta("mazdausa_complete_since")
            for listing in listings:
                change = store.upsert(listing)
                seen.add(listing.vin)
                if change["is_new"] and rebaseline and (listing.source or "") == "mazdausa":
                    continue
                if change["is_new"] and (listing.source or "hertz") in known_sources:
                    result.new_vins.append(listing.vin)
                elif change["is_new"]:
                    logger.info("  %s: first sweep of source %r, stored as baseline",
                                listing.vin, listing.source)
                delta = change["price_delta"]
                if delta and delta < 0:
                    result.price_drops.append((listing.vin, -delta))
            store.mark_inactive(seen, polled_pairs)
            if rebaseline and mazdausa_mod.COMPLETE and all(mazdausa_mod.COMPLETE.values()):
                store.set_meta("mazdausa_complete_since", clock.now_iso())
                logger.info("Mazda USA: first complete sweep stored as a silent baseline")

            # A wave of listings for one model is the event Lorenzo most
            # wants: Hertz offloads in bulk and demand catches up in days.
            try:
                result.fleet_drops = detect_fleet_drops(
                    store, cfg, listings, set(result.new_vins), polled_pairs)
            except Exception as exc:
                logger.warning("Fleet-drop detection skipped: %s", exc)

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
            result.hedonic = hedonic
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
            # Cars cut by real money this run, for the markdown path below.
            cut_now = {vin: drop for vin, drop in result.price_drops
                       if drop >= cfg.markdown_min_drop}
            radius_of = {w.label: (w.alert_radius_miles or cfg.alert_radius_miles)
                         for w in cfg.watch}
            for scored in result.watched:
                distance = scored.listing.geodist
                radius = min(cfg.alert_radius_miles,
                             radius_of.get(scored.matched_label, cfg.alert_radius_miles))
                if distance is None or distance > radius:
                    continue
                if score.value_gate(scored, cfg)[0]:
                    provisional.append(scored)
                elif scored.vin in new_vins and _wants_new_alert(scored, cfg):
                    provisional.append(scored)
                elif scored.vin in cut_now and _wants_markdown_alert(scored, cfg):
                    provisional.append(scored)

            if provisional:
                logger.info("Enriching %d candidates with AutoCheck", len(provisional))
                enrich(session, store, provisional, cfg)

            # Everything we already know, then a budgeted sweep for the rest
            # of the drivable board. Both are no-ops on a dry run's budget of
            # zero only if the operator sets it so; a dry run is otherwise a
            # fair rehearsal and is allowed to read history.
            attached = attach_known_conditions(store, result.watched)
            if attached:
                logger.info("Condition: %d car(s) answered from cache", attached)
            result.condition = survey_conditions(session, store, result.watched, cfg)
            attach_known_conditions(store, result.all_scored)

        new_vins = set() if seeding else set(result.new_vins)
        cut_now = {vin: drop for vin, drop in result.price_drops
                   if drop >= cfg.markdown_min_drop}
        for scored in provisional:
            ok, reasons = score.qualifies(scored, cfg)
            is_new = scored.vin in new_vins
            on_price = ok     # cleared the value gate itself, not an arrival or markdown path

            if not ok and is_new and _wants_new_alert(scored, cfg):
                # A new arrival still has to be clean; it just does not have
                # to be a bargain. Condition is never waived.
                report = scored.autocheck
                if scored.listing.is_new:
                    # A new car at a dealer: nothing to read, the factory
                    # warranty is the condition. Say what it is against
                    # sticker and what cash the dealer advertises.
                    ok = True
                    l = scored.listing
                    reasons = [f"Newly listed at {l.lot}: new car, "
                               f"{l.exterior_color or 'colour unknown'} / {l.interior_color or 'interior unknown'}"]
                    if scored.residual_pct is not None and l.msrp:
                        reasons.append(f"{scored.residual_pct:+.1f}% vs the ${l.msrp:,} sticker"
                                       + (f", dealer advertises ${l.incentive:,} manufacturer cash on top"
                                          if l.incentive else ""))
                elif report is not None and report.is_clean:
                    ok = True
                    reasons = [f"Newly listed at {scored.listing.lot}"] + [
                        r for r in reasons if "below predicted" in r or "AutoCheck" in r
                    ]
                    reasons.append("AutoCheck clean: no accidents, clean title, no odometer flags")
                elif report is not None:
                    reasons = [f"Newly listed, but AutoCheck: {'; '.join(report.concerns)}"]
                elif scored.history_url:
                    # No AutoCheck on this seller's pages but a Carfax the
                    # monitor cannot read (Avis). An arrival is still worth
                    # hearing about; the alert says the history is unread.
                    ok = True
                    reasons = [f"Newly listed at {scored.listing.lot}",
                               "Carfax linked on the page but NOT read by the monitor: "
                               f"open it before trusting this car: {scored.history_url}"]

            # A markdown into deal territory. Condition is never waived: the
            # car must carry a clean report or a franchise certification,
            # which is what `qualifies` already decided above.
            if not ok and scored.vin in cut_now and _wants_markdown_alert(scored, cfg):
                report = scored.autocheck
                clean = (report is not None and report.is_clean) or (
                    report is None and scored.listing.certified) or scored.listing.is_new
                if clean:
                    ok = True
                    drop = cut_now[scored.vin]
                    sigma = (f" ({scored.residual_sigma:+.2f} sigma)"
                             if scored.residual_sigma is not None else "")
                    against = "the sticker" if scored.benchmark == "msrp" else "its model"
                    reasons = [
                        f"Price cut ${drop:,} to ${scored.listing.price:,}, now "
                        f"{scored.residual_pct:+.1f}% vs {against}{sigma} at {scored.listing.lot}"
                    ] + [r for r in reasons if "AutoCheck" in r or "Certified" in r
                         or "certified" in r or "New car" in r]
                elif report is not None:
                    reasons = [f"Price cut, but AutoCheck: {'; '.join(report.concerns)}"]


            scored.reasons = reasons
            if not ok:
                logger.info("  %s not alerting: %s", scored.vin, "; ".join(reasons))
                continue
            if cfg.quiet and not (
                    on_price and is_new
                    and (scored.listing.source or "hertz").lower() in (cfg.push_sources or ["hertz"])
                    and (scored.listing.model or "").lower() in cfg.push_models):
                logger.info("  %s quiet mode: not a new %s deal on a target model",
                            scored.vin, "/".join(cfg.push_sources or ["hertz"]))
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
