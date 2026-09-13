"""Configuration loading.

Non-secret settings live in ``config.toml`` (committed). Secrets come from the
environment so the same file works locally and in GitHub Actions.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.toml"


@dataclass
class WatchEntry:
    """One line of the watchlist.

    ``models`` are matched case-insensitively as substrings of Hertz's model
    field, against inventory we already hold locally. Exact-string matching
    against their API is a known footgun: "CX-50" and "CX-50 Hybrid" are
    different values and the wrong one returns nothing at all.
    """

    tier: str
    label: str
    models: list[str] = field(default_factory=list)
    makes: list[str] = field(default_factory=list)
    threshold_pct: float = -8.0
    national: bool = False
    year_min: int = 0
    odometer_max: int = 10**9
    exclude_trims: list[str] = field(default_factory=list)
    # Whitelist: the trim must contain one of these. Cleaner than blacklisting
    # every trim you do not want, when you know which ones you do.
    require_trims: list[str] = field(default_factory=list)
    # Cars.com model slug, used to fit an external market curve for models
    # too thin in our own data for the pooled hedonic to say anything.
    market_slug: str = ""
    market_make: str = ""
    # CarMax make/model, when this entry should also sweep CarMax.
    carmax_make: str = ""
    carmax_model: str = ""
    # Hard colour requirements for this entry, matched as substrings.
    # Only Dealer.com sources carry interior colour: Cars.com result cards
    # show no colour at all and its detail pages are Cloudflare-blocked.
    require_exterior: list[str] = field(default_factory=list)
    require_interior: list[str] = field(default_factory=list)

    # How often to re-poll this entry, and how deep to page.
    #
    # Including the Rent2Buy fleet made the target model visible but also made
    # mainstream models enormous: Palisade alone is over 1,100 vehicles, or 49
    # pages. Polling every watched model on every run would mean ~130 page
    # loads an hour, which is both slow and rude, and the site rate-limits.
    # So the real target is polled often and cheaply, while the opportunistic
    # list is polled daily and only as deep as its "exceptional deal only"
    # threshold needs. Results are sorted cheapest-first, so a capped sweep
    # keeps the end of the distribution where an exceptional deal would be.
    poll_hours: float = 24.0
    max_pages: int = 6

    source: str = "hertz"
    year_max: int = 9999
    # Fire when a matching car FIRST appears, not only when it is cheap.
    # New inventory is the event worth knowing about: a good car that arrives
    # and sells in three days is missed entirely by a value-only rule.
    alert_on_new: bool = False
    # For new-listing alerts only: how far above the predicted price is still
    # worth telling you about. 0 means "at or below prediction".
    new_max_residual_pct: float = 0.0

    def matches(self, listing) -> bool:
        model = (listing.model or "").lower()
        make = (listing.make or "").lower()
        by_model = any(m.strip().lower() in model for m in self.models)
        by_make = any(m.strip().lower() == make for m in self.makes)
        if not (by_model or by_make):
            return False
        if listing.year and self.year_min and listing.year < self.year_min:
            return False
        if listing.year and listing.year > self.year_max:
            return False
        if self.source and (listing.source or "hertz") != self.source:
            return False
        if listing.odometer is not None and listing.odometer > self.odometer_max:
            return False
        trim = (listing.trim or "").lower()
        if self.require_trims and not any(
            t.strip().lower() in trim for t in self.require_trims
        ):
            return False
        for bad in self.exclude_trims:
            if bad.strip() and trim.startswith(bad.strip().lower()):
                return False

        exterior = (listing.exterior_color or "").lower()
        if self.require_exterior and not any(
            c.strip().lower() in exterior for c in self.require_exterior
        ):
            return False

        # Whole words, not substrings: "tan" is inside "Titan Black", and a
        # substring test put black-interior cars on the brown shortlist.
        interior_words = set(re.findall(r"[a-z]+", (listing.interior_color or "").lower()))
        if self.require_interior and not (
            interior_words & {c.strip().lower() for c in self.require_interior}
        ):
            return False

        return True


@dataclass
class Config:
    # location
    zip: str = "43220"
    city_state: str = "COLUMBUS, OH, US"
    coordinates: str = "40.0464,-83.0680"
    alert_radius_miles: int = 300
    national_mention_min_saving: int = 1500

    # economics
    sales_tax_rate: float = 0.075
    title_reg_fees: int = 100
    tax_applies_to_delivery: bool = False
    tax_excludes_doc_fee: bool = True
    delivery_base: float = 145.0
    delivery_per_mile: float = 2.00

    # buyer preferences (soft: they shape reporting, they do not filter)
    pref_colors: list[str] = field(default_factory=list)
    pref_trims: list[str] = field(default_factory=list)
    odometer_ideal: int = 0
    odometer_tolerance: int = 0

    carmax_max_shipping: int = 499

    # scoring
    min_comps: int = 12
    min_abs_discount: int = 400

    # alerts
    enable_email: bool = True
    enable_push: bool = True
    digest_every_days: int = 3
    realert_price_drop: int = 500

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    ntfy_server: str = "https://ntfy.sh"

    watch: list[WatchEntry] = field(default_factory=list)
    sources: dict = field(default_factory=dict)

    # secrets, from the environment
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""
    ntfy_topic: str = ""

    # paths
    base_dir: Path = BASE_DIR
    db_path: Path = BASE_DIR / "data" / "hertz.db"
    log_dir: Path = BASE_DIR / "logs"
    out_dir: Path = BASE_DIR / "out"

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.email_to)

    @property
    def push_configured(self) -> bool:
        return bool(self.ntfy_topic)

    def tier_entries(self, tier: str) -> list[WatchEntry]:
        return [w for w in self.watch if w.tier.upper() == tier.upper()]


def _resolve_db_path(paths_cfg: dict) -> Path:
    """Decide where the SQLite database lives.

    Never inside Dropbox by default. Dropbox syncs the -wal and -shm sidecars
    independently of the main file and can copy a database mid-transaction --
    both documented routes to SQLite corruption -- and a "conflicted copy" of
    the alert ledger would mean duplicate or missing alerts.

    Order: HERTZ_DB_PATH (used by CI, which wants the file inside the repo so
    price history is committed), then config, then a local app-data directory.
    """
    override = os.environ.get("HERTZ_DB_PATH")
    if override:
        return Path(override).expanduser()

    configured = paths_cfg.get("db")
    if configured:
        return Path(str(configured)).expanduser()

    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return root / "HertzMonitor" / "hertz.db"


def load(path: Path = CONFIG_PATH) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    loc = raw.get("location", {})
    econ = raw.get("economics", {})
    delivery = econ.get("delivery", {})
    prefs = raw.get("preferences", {})
    scoring = raw.get("scoring", {})
    alerts = raw.get("alerts", {})
    email = raw.get("email", {})
    push = raw.get("push", {})

    cfg = Config(
        zip=str(loc.get("zip", "43220")),
        city_state=str(loc.get("city_state", "COLUMBUS, OH, US")),
        coordinates=str(loc.get("coordinates", "40.0464,-83.0680")),
        alert_radius_miles=int(loc.get("alert_radius_miles", 300)),
        national_mention_min_saving=int(loc.get("national_mention_min_saving", 1500)),
        sales_tax_rate=float(econ.get("sales_tax_rate", 0.075)),
        title_reg_fees=int(econ.get("title_reg_fees", 100)),
        tax_applies_to_delivery=bool(econ.get("tax_applies_to_delivery", False)),
        tax_excludes_doc_fee=bool(econ.get("tax_excludes_doc_fee", True)),
        delivery_base=float(delivery.get("base", 145.0)),
        delivery_per_mile=float(delivery.get("per_mile", 2.00)),
        pref_colors=list(prefs.get("colors", [])),
        pref_trims=list(prefs.get("trims", [])),
        odometer_ideal=int(prefs.get("odometer_ideal", 0)),
        odometer_tolerance=int(prefs.get("odometer_tolerance", 0)),
        carmax_max_shipping=int(raw.get("carmax", {}).get("max_shipping", 499)),
        min_comps=int(scoring.get("min_comps", 12)),
        min_abs_discount=int(scoring.get("min_abs_discount", 400)),
        enable_email=bool(alerts.get("enable_email", True)),
        enable_push=bool(alerts.get("enable_push", True)),
        digest_every_days=int(alerts.get("digest_every_days", 3)),
        realert_price_drop=int(alerts.get("realert_price_drop", 500)),
        smtp_server=str(email.get("smtp_server", "smtp.gmail.com")),
        smtp_port=int(email.get("smtp_port", 587)),
        ntfy_server=str(push.get("server", "https://ntfy.sh")),
    )

    cfg.watch = [
        WatchEntry(
            tier=str(w.get("tier", "B")),
            label=str(w.get("label", "")),
            models=list(w.get("models", [])),
            makes=list(w.get("makes", [])),
            threshold_pct=float(w.get("threshold_pct", -8.0)),
            national=bool(w.get("national", False)),
            year_min=int(w.get("year_min", 0)),
            odometer_max=int(w.get("odometer_max", 10**9)),
            exclude_trims=list(w.get("exclude_trims", [])),
            require_trims=list(w.get("require_trims", [])),
            market_slug=str(w.get("market_slug", "")),
            market_make=str(w.get("market_make", "")),
            carmax_make=str(w.get("carmax_make", "")),
            carmax_model=str(w.get("carmax_model", "")),
            require_exterior=list(w.get("require_exterior", [])),
            require_interior=list(w.get("require_interior", [])),
            poll_hours=float(w.get("poll_hours", 24.0)),
            max_pages=int(w.get("max_pages", 6)),
            source=str(w.get("source", "hertz")),
            year_max=int(w.get("year_max", 9999)),
            alert_on_new=bool(w.get("alert_on_new", False)),
            new_max_residual_pct=float(w.get("new_max_residual_pct", 0.0)),
        )
        for w in raw.get("watch", [])
    ]

    from .ingest import Source
    cfg.sources = {
        str(sd.get("name", "hertz")): Source(
            name=str(sd.get("name", "hertz")),
            base_url=str(sd.get("base_url", "https://www.hertzcarsales.com")),
            search_path=str(sd.get("search_path", "/all-inventory/index.htm")),
            fixed_params=dict(sd.get("fixed_params", {})),
        )
        for sd in raw.get("source", [])
    }
    if not cfg.sources:
        cfg.sources = {"hertz": Source("hertz")}

    cfg.db_path = _resolve_db_path(raw.get("paths", {}))

    cfg.smtp_user = os.environ.get("HERTZ_SMTP_USER", "")
    cfg.smtp_password = os.environ.get("HERTZ_SMTP_PASSWORD", "")
    cfg.email_to = os.environ.get("HERTZ_EMAIL_TO", "") or cfg.smtp_user
    cfg.ntfy_topic = os.environ.get("HERTZ_NTFY_TOPIC", "")

    for directory in (cfg.db_path.parent, cfg.log_dir, cfg.out_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return cfg
