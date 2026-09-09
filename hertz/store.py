"""SQLite persistence.

History is the point. A single snapshot tells you what a car costs; a run of
snapshots tells you it has been sitting unsold for four months and just took
its second markdown. Every observation is therefore appended to
``price_history``, and nothing is ever overwritten in place except the
current-state mirror in ``listings``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .models import AutoCheck, Listing

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    vin TEXT PRIMARY KEY,
    year INTEGER, make TEXT, model TEXT, trim TEXT,
    price INTEGER, no_haggle_price INTEGER, odometer INTEGER, geodist REAL,
    lot TEXT, city TEXT, state TEXT, postal_code TEXT,
    inventory_date TEXT, fuel_type TEXT, normal_fuel_type TEXT,
    drive_line TEXT, body_style TEXT, engine TEXT,
    exterior_color TEXT, interior_color TEXT,
    city_mpg REAL, highway_mpg REAL,
    stock_number TEXT, status TEXT, certified INTEGER, classification TEXT, source TEXT,
    url TEXT, image_url TEXT, delivery_quote INTEGER,
    first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    price INTEGER, odometer INTEGER, geodist REAL, status TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_history_vin ON price_history(vin, observed_at);

CREATE TABLE IF NOT EXISTS autocheck (
    vin TEXT PRIMARY KEY,
    score INTEGER, peer_low INTEGER, peer_high INTEGER,
    title_brand TEXT, accident_summary TEXT,
    accidents_reported INTEGER, odometer_issue INTEGER, open_recalls INTEGER,
    owners INTEGER, usage TEXT, in_service_date TEXT, last_odometer INTEGER,
    fetched_at TEXT, raw_text TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vin TEXT NOT NULL, sent_at TEXT NOT NULL,
    tier TEXT, kind TEXT, price INTEGER, landed_cost REAL, residual_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_vin ON alerts(vin, sent_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    ok INTEGER, fetched INTEGER, new_vins INTEGER, price_drops INTEGER,
    alerts_sent INTEGER, error TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- Rent2Buy cars sit at rental branches, so the inventory spans hundreds of
-- distinct postal codes. Without this, every run would re-geocode all of
-- them over the network.
CREATE TABLE IF NOT EXISTS geocache (
    postal_code TEXT PRIMARY KEY,
    latitude REAL, longitude REAL, resolved_at TEXT
);
"""

LISTING_COLUMNS = [
    "vin", "year", "make", "model", "trim", "price", "no_haggle_price",
    "odometer", "geodist", "lot", "city", "state", "postal_code",
    "inventory_date", "fuel_type", "normal_fuel_type", "drive_line",
    "body_style", "engine", "exterior_color", "interior_color", "city_mpg",
    "highway_mpg", "stock_number", "status", "certified", "classification",
    "source", "url", "image_url", "delivery_quote",
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so a database created by an
    # older version keeps its old shape and every insert fails. The CI
    # database persists across runs, so this has to be handled, not avoided.
    MIGRATIONS = {
        "listings": {
            "classification": "TEXT",
            "source": "TEXT",
            "delivery_quote": "INTEGER",
        },
    }

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, columns in self.MIGRATIONS.items():
            existing = {
                r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue
            for column, sql_type in columns.items():
                if column not in existing:
                    logger.info("Migrating %s: adding column %s", table, column)
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- listings ---------------------------------------------------------

    def known_vins(self) -> set[str]:
        return {r["vin"] for r in self.conn.execute("SELECT vin FROM listings")}

    def previous_price(self, vin: str) -> int | None:
        row = self.conn.execute("SELECT price FROM listings WHERE vin = ?", (vin,)).fetchone()
        return row["price"] if row else None

    def upsert(self, listing: Listing) -> dict:
        """Insert or update one listing. Returns what changed.

        The returned dict is the change signal the rest of the pipeline acts
        on: ``is_new`` drives new-listing alerts, ``price_delta`` drives
        price-drop alerts.
        """
        now = _now()
        data = asdict(listing)
        data["certified"] = int(bool(data.get("certified")))

        existing = self.conn.execute(
            "SELECT price, odometer FROM listings WHERE vin = ?", (listing.vin,)
        ).fetchone()

        is_new = existing is None
        price_delta = None
        if existing is not None and existing["price"] is not None and listing.price is not None:
            price_delta = listing.price - existing["price"]

        columns = ", ".join(LISTING_COLUMNS)
        placeholders = ", ".join("?" for _ in LISTING_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in LISTING_COLUMNS if c != "vin")
        values = [data.get(c) for c in LISTING_COLUMNS]

        self.conn.execute(
            f"""INSERT INTO listings ({columns}, first_seen, last_seen, active)
                VALUES ({placeholders}, ?, ?, 1)
                ON CONFLICT(vin) DO UPDATE SET {updates}, last_seen=excluded.last_seen, active=1""",
            values + [now, now],
        )

        # Append to history only when something we care about moved.
        if is_new or price_delta not in (None, 0) or (
            existing is not None and existing["odometer"] != listing.odometer
        ):
            self.conn.execute(
                """INSERT INTO price_history (vin, observed_at, price, odometer, geodist, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (listing.vin, now, listing.price, listing.odometer, listing.geodist, listing.status),
            )

        self.conn.commit()
        return {"is_new": is_new, "price_delta": price_delta}

    def mark_inactive(self, seen_vins: set[str], polled_models: set[str] | None = None,
                      polled_sources: set[str] | None = None) -> int:
        """Flag listings we did not see this run. Usually means sold.

        Scoped to the (source, model) pairs actually polled. Watchlist
        entries poll on different schedules, so an unscoped sweep would mark
        every car of an unpolled model as sold and then "rediscover" them all
        next time, producing a burst of bogus new-listing alerts. Scoping by
        model alone had the same effect across sources: a Hertz XC60 poll was
        marking the CarMax and Byers XC60s sold.
        """
        if not seen_vins:
            return 0

        vin_placeholders = ",".join("?" for _ in seen_vins)
        sql = f"UPDATE listings SET active = 0 WHERE active = 1 AND vin NOT IN ({vin_placeholders})"
        params = list(seen_vins)

        if polled_models:
            model_placeholders = ",".join("?" for _ in polled_models)
            sql += f" AND LOWER(model) IN ({model_placeholders})"
            params += [m.lower() for m in polled_models]
        if polled_sources:
            source_placeholders = ",".join("?" for _ in polled_sources)
            sql += f" AND COALESCE(source, 'hertz') IN ({source_placeholders})"
            params += list(polled_sources)

        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    def price_drop_since(self, vin: str, days: int = 30) -> int | None:
        """Largest observed price minus current price over the window."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self.conn.execute(
            """SELECT MAX(price) AS high FROM price_history
               WHERE vin = ? AND observed_at >= ? AND price IS NOT NULL""",
            (vin, cutoff),
        ).fetchone()
        current = self.previous_price(vin)
        if not row or row["high"] is None or current is None:
            return None
        drop = row["high"] - current
        return drop if drop > 0 else None

    # -- autocheck --------------------------------------------------------

    def get_autocheck(self, vin: str, max_age_days: int = 14) -> AutoCheck | None:
        row = self.conn.execute("SELECT * FROM autocheck WHERE vin = ?", (vin,)).fetchone()
        if not row:
            return None
        try:
            fetched = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            return None
        if datetime.now() - fetched > timedelta(days=max_age_days):
            return None

        def tri(value):
            return None if value is None else bool(value)

        return AutoCheck(
            vin=row["vin"], score=row["score"],
            peer_low=row["peer_low"], peer_high=row["peer_high"],
            title_brand=row["title_brand"] or "",
            accident_summary=row["accident_summary"] or "",
            accidents_reported=tri(row["accidents_reported"]),
            odometer_issue=tri(row["odometer_issue"]),
            open_recalls=tri(row["open_recalls"]),
            owners=row["owners"], usage=row["usage"] or "",
            in_service_date=row["in_service_date"] or "",
            last_odometer=row["last_odometer"],
            fetched_at=row["fetched_at"] or "",
            raw_text=row["raw_text"] or "",
        )

    def save_autocheck(self, report: AutoCheck) -> None:
        def tri(value):
            return None if value is None else int(bool(value))

        self.conn.execute(
            """INSERT INTO autocheck (vin, score, peer_low, peer_high, title_brand,
                    accident_summary, accidents_reported, odometer_issue, open_recalls,
                    owners, usage, in_service_date, last_odometer, fetched_at, raw_text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(vin) DO UPDATE SET
                    score=excluded.score, peer_low=excluded.peer_low,
                    peer_high=excluded.peer_high, title_brand=excluded.title_brand,
                    accident_summary=excluded.accident_summary,
                    accidents_reported=excluded.accidents_reported,
                    odometer_issue=excluded.odometer_issue,
                    open_recalls=excluded.open_recalls, owners=excluded.owners,
                    usage=excluded.usage, in_service_date=excluded.in_service_date,
                    last_odometer=excluded.last_odometer,
                    fetched_at=excluded.fetched_at, raw_text=excluded.raw_text""",
            (
                report.vin, report.score, report.peer_low, report.peer_high,
                report.title_brand, report.accident_summary,
                tri(report.accidents_reported), tri(report.odometer_issue),
                tri(report.open_recalls), report.owners, report.usage,
                report.in_service_date, report.last_odometer,
                report.fetched_at or _now(), report.raw_text[:20000],
            ),
        )
        self.conn.commit()

    # -- alerts -----------------------------------------------------------

    def last_alert_price(self, vin: str) -> int | None:
        row = self.conn.execute(
            "SELECT price FROM alerts WHERE vin = ? ORDER BY sent_at DESC LIMIT 1", (vin,)
        ).fetchone()
        return row["price"] if row else None

    def should_alert(self, vin: str, price: int | None, realert_drop: int) -> bool:
        """Alert once per VIN, then again only after a further real price cut."""
        previous = self.last_alert_price(vin)
        if previous is None:
            return True
        if price is None:
            return False
        return (previous - price) >= realert_drop

    def record_alert(self, vin: str, tier: str, kind: str, price, landed, residual) -> None:
        self.conn.execute(
            """INSERT INTO alerts (vin, sent_at, tier, kind, price, landed_cost, residual_pct)
               VALUES (?,?,?,?,?,?,?)""",
            (vin, _now(), tier, kind, price, landed, residual),
        )
        self.conn.commit()

    # -- runs / meta ------------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at, ok) VALUES (?, 0)", (_now(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, ok: bool, fetched: int, new_vins: int,
                   price_drops: int, alerts_sent: int, error: str | None = None) -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=?, ok=?, fetched=?, new_vins=?,
                   price_drops=?, alerts_sent=?, error=? WHERE id=?""",
            (_now(), int(ok), fetched, new_vins, price_drops, alerts_sent, error, run_id),
        )
        self.conn.commit()

    def last_successful_fetch_count(self) -> int:
        """Used by the silent-failure guard: a run that suddenly returns zero
        rows after a healthy run is a bug, not an empty market."""
        row = self.conn.execute(
            "SELECT fetched FROM runs WHERE ok = 1 AND fetched > 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["fetched"] if row else 0

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- geocode cache ----------------------------------------------------

    def load_geocache(self) -> dict[str, tuple[float, float] | None]:
        """Every cached postal code, including the misses (stored as None)."""
        rows = self.conn.execute(
            "SELECT postal_code, latitude, longitude FROM geocache"
        ).fetchall()
        return {
            r["postal_code"]: (
                (r["latitude"], r["longitude"])
                if r["latitude"] is not None and r["longitude"] is not None
                else None
            )
            for r in rows
        }

    def save_geocache(self, entries: dict[str, tuple[float, float] | None]) -> None:
        if not entries:
            return
        now = _now()
        self.conn.executemany(
            """INSERT INTO geocache (postal_code, latitude, longitude, resolved_at)
               VALUES (?,?,?,?)
               ON CONFLICT(postal_code) DO UPDATE SET
                   latitude=excluded.latitude, longitude=excluded.longitude,
                   resolved_at=excluded.resolved_at""",
            [
                (code, coords[0] if coords else None, coords[1] if coords else None, now)
                for code, coords in entries.items()
            ],
        )
        self.conn.commit()

    def active_listings(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM listings WHERE active = 1").fetchall()
        return [dict(r) for r in rows]

    def history_for(self, vin: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT observed_at, price, odometer FROM price_history WHERE vin = ? ORDER BY observed_at",
            (vin,),
        ).fetchall()
        return [dict(r) for r in rows]

    def export_json(self, path: Path) -> None:
        payload = {
            "generated_at": _now(),
            "listings": self.active_listings(),
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
