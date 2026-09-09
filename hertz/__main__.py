"""Command-line entry point.

    python -m hertz              # full run: fetch, score, alert, write board
    python -m hertz --dry-run    # everything except sending or recording alerts
    python -m hertz --digest     # force the periodic digest email
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from . import artifact, board, config, notify, pipeline
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
        return datetime.now() - datetime.fromisoformat(last) >= timedelta(days=every_days)
    except ValueError:
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hertz", description="Hertz Car Sales deal monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="score and render, but send nothing and record no alerts")
    parser.add_argument("--digest", action="store_true", help="force the digest email")
    parser.add_argument("--quiet", action="store_true", help="suppress the terminal summary")
    args = parser.parse_args(argv)

    cfg = config.load()
    setup_logging(cfg)
    logger.info("=== Hertz deal monitor starting ===")

    result = pipeline.run(cfg, dry_run=args.dry_run)

    board_html = board.render(result, cfg)
    (cfg.out_dir / "board.html").write_text(
        board.render(result, cfg, standalone=True), encoding="utf-8")

    # index.html is what GitHub Pages serves at the bare URL.
    try:
        rich = artifact.render(
            result.all_scored, cfg,
            comps=len(result.all_scored),
            rmse=0.0,
            curve=result.curves.get("mazda|cx-50 hybrid"),
        )
        (cfg.out_dir / "index.html").write_text(rich, encoding="utf-8")
    except Exception as exc:
        logger.warning("Rich board could not be rendered: %s", exc)

    logger.info("Boards written to %s", cfg.out_dir)

    with Store(cfg.db_path) as store:
        store.export_json(cfg.out_dir / "inventory.json")

        if not result.ok:
            logger.error("Run failed: %s", result.error)
            if not args.dry_run:
                notify.send_failure_alert(cfg, result.error or "unknown error")
            return 1

        if not args.dry_run:
            sent = notify.send_deal_alerts(cfg, result.alerts, board_html)
            logger.info("Sent %d deal alert(s)", sent)

            if args.digest or due_for_digest(store, cfg.digest_every_days):
                count = len(result.watched)
                notify.send_digest(
                    cfg, f"Hertz board: {count} watchlist cars, {len(result.alerts)} alerts",
                    board_html,
                )
                store.set_meta("last_digest_at", datetime.now().isoformat(timespec="seconds"))

    if not args.quiet:
        print_summary(result, cfg)

    logger.info("=== Run complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
