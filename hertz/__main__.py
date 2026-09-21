"""Command-line entry point.

    python -m hertz              # full run: fetch, score, alert, write board
    python -m hertz --dry-run    # everything except sending or recording alerts
    python -m hertz --digest     # force the periodic digest email
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta

from . import artifact, board, clock, config, dashboard, notify, pipeline
from .score import rank
from .store import Store

logger = logging.getLogger("hertz")


def setup_logging(cfg) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg.log_dir / "monitor.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def print_summary(result, cfg) -> None:
    """A compact terminal view of the same board the email carries."""
    print()
    print(f"  Scanned {result.fetched} vehicles within {cfg.alert_radius_miles} mi of {cfg.zip}")
    print(f"  On the watchlist: {len(result.watched)}   New this run: {len(result.new_vins)}   "
          f"Price drops: {len(result.price_drops)}")
    print(f"  Price model: {'fitted' if result.hedonic_fitted else 'NOT fitted'}")
    print()

    watched = rank([s for s in result.watched if s.listing.price])
    if not watched:
        print("  Nothing on the watchlist is currently in inventory.")
        return

    header = (f"  {'dist':>5} {'lot':<16} {'vehicle':<44} {'price':>9} {'landed':>9} "
              f"{'vs mdl':>7} {'mi':>8} {'days':>5}")
    print(header)
    print("  " + "-" * (len(header) - 2))
    for scored in watched[:40]:
        listing = scored.listing
        residual = f"{scored.residual_pct:+.1f}%" if scored.residual_pct is not None else "n/a"
        flag = "*" if scored.tier == "A" else " "
        print(f" {flag}{(listing.geodist or 0):>5.0f} {listing.lot[:16]:<16} {listing.label[:44]:<44} "
              f"{'$' + format(listing.price or 0, ',') :>9} {'$' + format(int(scored.landed_cost), ',') :>9} "
              f"{residual:>7} {(listing.odometer or 0):>8,} {listing.days_on_lot or 0:>5}")

    if result.alerts:
        print(f"\n  {len(result.alerts)} car(s) cleared BOTH gates:")
        for scored in result.alerts:
            print(f"    {scored.listing.label} - {scored.listing.lot}")
            for reason in scored.reasons:
                print(f"      - {reason}")
    else:
        print("\n  No car cleared both gates this run.")

    if result.national_pick:
        pick = result.national_pick
        print(f"\n  Outside the radius: {pick.listing.label} at {pick.listing.lot} "
              f"({pick.listing.geodist:.0f} mi) lands at ${pick.landed_cost:,.0f}")


def due_for_digest(store: Store, every_days: int) -> bool:
    last = store.get_meta("last_digest_at")
    if not last:
        return True
    try:
        return clock.now() - datetime.fromisoformat(last) >= timedelta(days=every_days)
    except ValueError:
        return True


def merge_sweep(old: dict | None, fresh: dict) -> dict:
    """Merge a sweep per model: a model that answered replaces its old rows;
    a model that was blocked keeps its last good rows. Overwriting the whole
    cache on a partial refresh once turned a three-model tab into a
    one-model tab for twelve hours."""
    old = old or {"rows": [], "counts": {}, "skipped": []}
    answered = set(fresh.get("counts") or {})
    rows = [r for r in old.get("rows", []) if r.get("model") not in answered]
    rows += list(fresh.get("rows", []))
    rows.sort(key=lambda r: (r.get("market_pct") is None, r.get("market_pct") or 0.0,
                             r.get("price") or 0))
    return {"rows": rows,
            "counts": {**(old.get("counts") or {}), **(fresh.get("counts") or {})},
            "skipped": list(fresh.get("skipped") or []),
            "seeded_at": clock.now_iso()}


def refresh_sweep(store: Store, cfg, key: str, builder: str, force: bool,
                  hours: float = 12.0) -> dict | None:
    """One Cars.com sweep with its cache, its committed seed and its window.

    `key` names the meta slots (`<key>_json`, `<key>_at`) and the seed file
    (docs/<key>_seed.json); `builder` is the dream-module function to call
    when a refresh is due. A committed seed is adopted when the cache has no
    rows OR the seed's own `seeded_at` is newer than the cache stamp, never
    by file mtime (a fresh checkout resets every mtime). Cars.com blocks
    GitHub's runners on all but the first request of a session, so the
    cloud rarely fills a sweep from scratch; `python -m hertz --seed <key>`
    on a residential connection writes the seed the cloud then adopts.
    """
    cached = store.get_meta(f"{key}_json")
    stamp = store.get_meta(f"{key}_at")
    seed_path = cfg.base_dir / "docs" / f"{key}_seed.json"
    has_rows = False
    if cached:
        try:
            has_rows = bool(json.loads(cached).get("rows"))
        except Exception:
            has_rows = False
    if seed_path.exists():
        try:
            sd = json.loads(seed_path.read_text(encoding="utf-8"))
            seeded = sd.get("seeded_at")
            cache_time = datetime.fromisoformat(stamp) if stamp else None
            seed_time = datetime.fromisoformat(seeded) if seeded else None
            if sd.get("rows") and (not has_rows or (seed_time and (cache_time is None or seed_time > cache_time))):
                cached = json.dumps(sd)
                stamp = (seed_time or clock.now()).isoformat(timespec="seconds")
                store.set_meta(f"{key}_json", cached)
                store.set_meta(f"{key}_at", stamp)
                logger.info("%s sweep: adopted the committed seed", key)
        except Exception as exc:
            logger.warning("%s seed unreadable: %s", key, exc)
    fresh = False
    if stamp:
        try:
            fresh = clock.now() - datetime.fromisoformat(stamp) < timedelta(hours=hours)
        except ValueError:
            fresh = False
    if cached and fresh and not force:
        return json.loads(cached)
    try:
        from . import dream   # pulls in Playwright; only when refreshing
        fresh_doc = dream.to_json(getattr(dream, builder)(cfg))
        doc = merge_sweep(json.loads(cached) if cached else None, fresh_doc)
        store.set_meta(f"{key}_json", json.dumps(doc))
        if fresh_doc.get("counts"):
            store.set_meta(f"{key}_at", doc["seeded_at"])
            # Keep the committed seed current whenever a model answers, so
            # the seed is never older than the last good fetch.
            try:
                seed_path.write_text(json.dumps(doc), encoding="utf-8")
            except Exception as exc:
                logger.warning("%s seed not updated: %s", key, exc)
        return doc
    except Exception as exc:
        logger.warning("%s sweep not refreshed: %s", key, exc)
        return json.loads(cached) if cached else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hertz", description="Hertz Car Sales deal monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="score and render, but send nothing and record no alerts")
    parser.add_argument("--digest", action="store_true", help="force the digest email")
    parser.add_argument("--quiet", action="store_true", help="suppress the terminal summary")
    parser.add_argument("--dream", action="store_true",
                        help="refresh the dream-car tab now, ignoring its 12-hour cache")
    parser.add_argument("--force", action="store_true",
                        help="re-poll every watch now, ignoring poll windows")
    parser.add_argument("--curve-pages", type=int, default=2,
                        help="Cars.com pages per model when refitting curves (default 2, ~24 cars each)")
    parser.add_argument("--curves-only", default="",
                        help="comma-separated Cars.com slugs to refit, instead of all of them")
    parser.add_argument("--curves", action="store_true",
                        help="refit the Cars.com market curves and write docs/market_curves.json, "
                             "then exit (run locally; the runner is usually challenged)")
    parser.add_argument("--seed", choices=["dream", "suv", "mazda"], default="",
                        help="refresh one Cars.com sweep now, write docs/<name>_seed.json, "
                             "then exit (run locally; the runner is usually challenged)")
    args = parser.parse_args(argv)

    cfg = config.load()
    setup_logging(cfg)
    if args.curves:
        with Store(cfg.db_path) as store:
            only = {s.strip() for s in args.curves_only.split(",") if s.strip()}
            seed = pipeline.refresh_market_curves(cfg, store, pages=args.curve_pages,
                                                  only=only or None)
        print(f"{len(seed['curves'])} market curve(s) in docs/{pipeline.CURVE_SEED}")
        return 0
    if args.seed:
        builder = {"dream": "build", "suv": "build_suv", "mazda": "build_mazda"}[args.seed]
        with Store(cfg.db_path) as store:
            doc = refresh_sweep(store, cfg, args.seed, builder, force=True)
        rows = (doc or {}).get("rows", [])
        print(f"{args.seed} sweep: {len(rows)} row(s) in docs/{args.seed}_seed.json; "
              f"skipped: {', '.join((doc or {}).get('skipped', [])) or 'none'}")
        return 0
    logger.info("=== Hertz deal monitor starting ===")

    result = pipeline.run(cfg, dry_run=args.dry_run, force=args.force)

    board_html = board.render(result, cfg)
    (cfg.out_dir / "board.html").write_text(
        board.render(result, cfg, standalone=True), encoding="utf-8")

    # The rich board: what gets published as the dashboard.
    try:
        rich = artifact.render(
            result.all_scored, cfg,
            comps=result.hedonic_n,
            rmse=result.hedonic_rmse,
            curve=result.curves.get("mazda|cx-50 hybrid"),
        )
        (cfg.out_dir / "board_artifact.html").write_text(rich, encoding="utf-8")
    except Exception as exc:
        logger.warning("Rich board could not be rendered: %s", exc)

    logger.info("Boards written to %s", cfg.out_dir)

    # The dream-car tab: three Cars.com sweeps with a fresh browser each and
    # a pause between them. Refreshed only every `dream_every_hours`, because
    # it is aspirational rather than a watch, and each sweep costs ~45 s.
    dream_doc = None
    with Store(cfg.db_path) as store:
        cached = store.get_meta("dream_json")
        stamp = store.get_meta("dream_at")
        # A committed seed outranks an empty cache. Cars.com blocks GitHub's
        # runners on all but the first request of a session, so the cloud
        # rarely fills the tab from scratch; a seed produced on a residential
        # connection and committed as docs/dream_seed.json gives it something
        # to keep and merge into. It also survives a database restore, which
        # is how the cache came to be empty in the first place.
        seed_path = cfg.base_dir / "docs" / "dream_seed.json"
        # "Empty" must mean no ROWS, not no string: a run in which every
        # model was blocked writes a cache of {"rows": []}, which is truthy
        # and silently out-ranked the seed on the very run meant to adopt it.
        cached_rows = False
        if cached:
            try:
                cached_rows = bool(json.loads(cached).get("rows"))
            except Exception:
                cached_rows = False
        # A committed seed is adopted when the cache is empty OR when the
        # seed is newer than the cache. "Only when empty" let a stale 26-row
        # uncapped cache out-rank a fresh 7-row capped seed, so the cap was
        # committed and still not shown. Newer wins; the seed's own mtime is
        # the clock, and adoption stamps dream_at so the next run does not
        # re-adopt it.
        # The clock is the seed's own `seeded_at` field, never the file's
        # mtime: a fresh checkout on a runner stamps every file with the
        # checkout time, so an mtime rule would re-adopt the seed on every
        # run and quietly undo each cloud refresh.
        if seed_path.exists():
            try:
                seed = seed_path.read_text(encoding="utf-8")
                seed_doc = json.loads(seed)
                seeded_at = seed_doc.get("seeded_at")
                cache_time = datetime.fromisoformat(stamp) if stamp else None
                seed_time = datetime.fromisoformat(seeded_at) if seeded_at else None
                seed_newer = (seed_time is not None
                              and (cache_time is None or seed_time > cache_time))
                if seed_doc.get("rows") and (not cached_rows or seed_newer):
                    cached = seed
                    stamp = (seed_time or clock.now()).isoformat(timespec="seconds")
                    store.set_meta("dream_json", seed)
                    store.set_meta("dream_at", stamp)
                    logger.info("Dream tab: adopted the committed seed (%s)",
                                "cache empty" if not cached_rows else "seed newer")
            except Exception as exc:
                logger.warning("Dream seed unreadable: %s", exc)
        fresh = False
        if stamp:
            try:
                fresh = clock.now() - datetime.fromisoformat(stamp) < timedelta(hours=12)
            except ValueError:
                fresh = False
        if cached and fresh and not args.dream:
            dream_doc = json.loads(cached)
        else:
            try:
                from . import dream   # pulls in Playwright; only when refreshing
                fresh_doc = dream.to_json(dream.build(cfg))
                # Merge per model: a model that answered replaces its old
                # rows; a model that was blocked keeps its last good rows.
                # Overwriting the whole cache on a partial refresh turned a
                # three-model tab into a one-model tab for twelve hours.
                old = json.loads(cached) if cached else {"rows": [], "counts": {}, "skipped": []}
                answered = set(fresh_doc["counts"])
                rows = [r for r in old["rows"] if r["model"] not in answered] + fresh_doc["rows"]
                counts = {**old.get("counts", {}), **fresh_doc["counts"]}
                rows.sort(key=lambda r: (r["market_pct"] is None, r["market_pct"] or 0.0, r["price"]))
                now_iso = clock.now_iso()
                dream_doc = {"rows": rows, "counts": counts, "skipped": fresh_doc["skipped"],
                             "seeded_at": now_iso}
                store.set_meta("dream_json", json.dumps(dream_doc))
                if answered:
                    store.set_meta("dream_at", now_iso)
                    # Keep the committed seed current whenever a model answers,
                    # so the seed is never older than the last good fetch.
                    try:
                        seed_path.write_text(json.dumps(dream_doc), encoding="utf-8")
                    except Exception as exc:
                        logger.warning("Dream seed not updated: %s", exc)
            except Exception as exc:
                logger.warning("Dream tab not refreshed: %s", exc)
                dream_doc = json.loads(cached) if cached else None

        # The Cars.com SUV sweep (XC60 Plus within 300 mi) and the new/CPO
        # Mazda sweep ride the same cache-and-seed logic as the wagons, each
        # in its own slot (see refresh_sweep), so a blocked run keeps the
        # last good rows and a committed seed fills a cold start.
        suv_doc = refresh_sweep(store, cfg, "suv", "build_suv", args.dream)
        mazda_doc = refresh_sweep(store, cfg, "mazda", "build_mazda", args.dream)

        # Followed cars: read the repo's open "follow" issues, report what
        # changed on each, and hand the set to the page. Never fatal.
        follow_doc = None
        try:
            from . import follow
            follow_doc = follow.run(cfg, store, dream_doc, suv_doc, dry_run=args.dry_run,
                                    extra_docs=[mazda_doc])
        except Exception as exc:
            logger.warning("Follow check skipped: %s", exc)

        # One JSON document drives the published dashboard (docs/index.html).
        try:
            doc = dashboard.build(result, cfg, dream_doc, store, suv_doc, follow_doc, mazda_doc)
            dashboard.write(doc, cfg.base_dir / "docs" / "data.json")
            logger.info("Dashboard data written: %d listings, %d dream rows, %d Mazda market rows",
                        len(doc["listings"]), len(doc["dream"]["rows"]),
                        len(doc["mazda_market"]["rows"]))
        except Exception as exc:
            logger.warning("Dashboard data could not be written: %s", exc)

    with Store(cfg.db_path) as store:
        if not result.ok:
            logger.error("Run failed: %s", result.error)
            if not args.dry_run:
                notify.send_failure_alert(cfg, result.error or "unknown error")
            return 1

        if not args.dry_run:
            sent = notify.send_deal_alerts(cfg, result.alerts, board_html)
            logger.info("Sent %d deal alert(s)", sent)

            # A fleet drop is the event worth interrupting for.
            if result.fleet_drops:
                sent = notify.send_fleet_drops(
                    cfg, result.fleet_drops,
                    "https://loremagno.github.io/hertz-deal-monitor/")
                logger.info("Sent %d fleet-drop alert(s)", sent)

            # One quiet push per run for models Hertz has started selling.
            if result.new_models:
                lines = [f"{mk} {md}: {n} car{'s' if n != 1 else ''} from ${p:,}"
                         for mk, md, n, p in result.new_models[:12]]
                if len(result.new_models) > 12:
                    lines.append(f"... and {len(result.new_models) - 12} more")
                notify.send_push(
                    cfg, "Hertz now sells: " + ", ".join(md for _, md, _, _ in result.new_models[:4])
                    + ("..." if len(result.new_models) > 4 else ""),
                    "\n".join(lines), priority="default")

            # A source that was skipped is worth one quiet line, not an
            # urgent failure alert: the rest of the run succeeded and the
            # skipped entry retries on its own next run. Only say it once per
            # streak, so a dealer that stays dark for a week is not a
            # notification every two hours.
            if result.failed_entries:
                labels = ", ".join(label for label, _ in result.failed_entries)
                if store.get_meta("skipped_sources") != labels:
                    # (the label carries no counts, so a steady state stays quiet)
                    notify.send_push(
                        cfg, "Hertz monitor: a source was skipped",
                        f"{labels} did not respond this run; everything else "
                        "refreshed normally and it will retry next run.",
                        priority="low",
                    )
                    store.set_meta("skipped_sources", labels)
            elif store.get_meta("skipped_sources"):
                store.set_meta("skipped_sources", "")

            if args.digest or due_for_digest(store, cfg.digest_every_days):
                count = len(result.watched)
                notify.send_digest(
                    cfg, f"Hertz board: {count} watchlist cars, {len(result.alerts)} alerts",
                    board_html,
                )
                store.set_meta("last_digest_at", clock.now_iso())

    if not args.quiet:
        print_summary(result, cfg)

    logger.info("=== Run complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
