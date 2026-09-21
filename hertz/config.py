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


# Sellers whose detail pages carry a history link the monitor can open.
# CarMax hard-blocks its detail pages; Enterprise runs a JS app and sells
# only inspected, warrantied cars, which is what its rows already say.
# Lives here so both the pipeline and the dashboard can see it without the
# dashboard having to import the pipeline (and with it, Playwright).
CONDITION_SOURCES = ("hertz", "avis", "byers-mazda", "byers-volvo")

# Sources you drive to. Dealer.com sources say so with `kind = "dealer"`;
# these run their own reader and have no `[[source]]` entry.
DEALER_PICKUP_SOURCES = ("mazdausa",)

# Every franchise-dealer source name, filled in by `load()` from the
# `kind = "dealer"` sources plus the pickup sources above. The hedonic
# carries an indicator for them: a dealer's certified asking price sits a
# level above the ex-rental channels for the same car, and pooling the two
# without it moved the CX-50 level up the first time 160 dealer CPO rows
# arrived, turning an Enterprise car from -5.7% into -10.3% overnight.
DEALER_SOURCE_NAMES: set[str] = set(DEALER_PICKUP_SOURCES)


_BODY_WORDS = {"suv", "crossover", "sedan", "hatchback", "wagon", "coupe", "convertible", "van",
               "minivan", "truck", "pickup"}


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
    # Which dashboard tab this watch belongs to: "primary" (the CX-50
    # Hybrid), "hertz" (other Hertz finds), or "suv" (XC60 and CX-70). The
    # station-wagon tab is fed by the dream module, not by watches.
    group: str = "hertz"
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
    # Every source this watch reads. A franchise dealer's new and used
    # stock live on two pages, i.e. two sources, and one watch should cover
    # both without being written twice. `source` stays the first of them
    # for the code paths that key on a single name (CarMax, Enterprise).
    sources: list[str] = field(default_factory=list)
    # Only cars the seller certifies. A dealer's used page lists certified
    # and plain used cars together; this keeps the plain ones out.
    require_certified: bool = False
    # "new" or "used": a source that carries both (the Mazda USA locator)
    # must not put a 300-mile certified car on the new-car lane.
    require_stock: str = ""
    # This lane's own alert radius, inside the global one. The locator
    # covers twenty dealers within 300 miles and a grey Premium arrives at
    # one of them most days; the colour lanes alert on arrival, so they
    # keep to the Columbus area while the board still shows the rest.
    alert_radius_miles: int | None = None
    year_max: int = 9999
    # Fire when a matching car FIRST appears, not only when it is cheap.
    # New inventory is the event worth knowing about: a good car that arrives
    # and sells in three days is missed entirely by a value-only rule.
    alert_on_new: bool = False
    # For new-listing alerts only: how far above the predicted price is still
    # worth telling you about. 0 means "at or below prediction".
    new_max_residual_pct: float = 0.0

    # Tell me when a car I am watching is MARKED DOWN into deal territory.
    # Arrival alerts fire once, on the day a car appears; the value gate
    # needs the tier bar. Between them sat a blind spot: a car that arrives
    # at prediction and is cut week by week until it is genuinely cheap
    # never says anything. On 2026-09-17 the best drivable car on the board
    # was exactly that, a 2026 Palisade SEL at -4.9% and -1.6 sigma, marked
    # down since it arrived eight days earlier. Lorenzo asked for this on
    # his primary targets only.
    markdown_alert: bool = False

    # A sweep: no model list, a make list, and the filters Hertz applies
    # server-side (price band, mileage cap, body styles, model years). The
    # point is a model Hertz starts selling that no model list names.
    sweep: bool = False
    price_min: int = 0
    price_max: int = 0
    body_styles: list[str] = field(default_factory=list)
    # Drop trims the make's ladder (hertz/trims.py) positively calls base.
    exclude_base_trims: bool = False
    # Rows from this watch reach the board only at or below this residual;
    # None shows every matching car. Alerts use threshold_pct as before.
    board_max_residual_pct: float | None = None
    # A studentized-residual bar on top of the percentage, for the board AND
    # the alert: -2.0 means "two of the model's own sigmas below". Thirteen
    # Rent2Buy Highlanders with no trim label read -10% to -15% against a
    # model whose labelled cars are XLE and up, at only -1.7 sigma: not a
    # deal, a missing label. None applies no sigma bar.
    min_sigma: float | None = None

    def matches(self, listing) -> bool:
        model = (listing.model or "").lower()
        make = (listing.make or "").lower()
        # Models, when named, decide; makes alone match a whole marque. An
        # Enterprise watch names both: the makes drive its API sweep and the
        # models say which of those cars it is actually about.
        by_model = any(m.strip().lower() in model for m in self.models)
        by_make = any(m.strip().lower() == make for m in self.makes)
        if self.models and not by_model:
            return False
        if not self.models and not by_make:
            return False
        if listing.year and self.year_min and listing.year < self.year_min:
            return False
        if listing.year and listing.year > self.year_max:
            return False
        allowed = self.sources or ([self.source] if self.source else [])
        if allowed and (listing.source or "hertz") not in allowed:
            return False
        if self.require_certified and not listing.certified:
            return False
        if self.require_stock and (listing.stock or "used") != self.require_stock:
            return False
        if listing.odometer is not None and listing.odometer > self.odometer_max:
            return False
        if self.price_min and listing.price and listing.price < self.price_min:
            return False
        if self.price_max and listing.price and listing.price > self.price_max:
            return False
        # Body style, only when the row carries one of the real body words.
        # Dealer feeds put "AUTO FWD" and "AWD" in that field now and then,
        # and a car the server already filtered to SUV must not be dropped
        # for it.
        if self.body_styles:
            # Dealer feeds put trim strings, "AWD", "FWD 8-PASSENGER (NATL)"
            # and "Van Passenger Van" in this field. The first real body
            # word found decides; a value with none is left alone, since a
            # car the server already filtered to SUV must not be dropped.
            words = re.findall(r"[a-z]+", (listing.body_style or "").lower())
            body = next((w for w in words if w in _BODY_WORDS), "")
            if body and body not in {b.lower() for b in self.body_styles}:
                return False
        trim = (listing.trim or "").lower()
        if self.exclude_base_trims:
            from .trims import is_base_trim
            if is_base_trim(listing.trim, listing.make):
                return False
        if self.require_trims and not any(
            t.strip().lower() in trim for t in self.require_trims
        ):
            return False
        # Whole-word, not prefix: "SE" as a prefix excluded "SEL", which is
        # every Santa Fe Hertz stocks. A base-trim name must match a whole
        # token of the trim string ("LX", "S", "SE", "LE"), and a multi-word
        # entry ("3.3 Turbo Preferred") must appear as a phrase.
        trim_words = set(re.findall(r"[a-z0-9.]+", trim))
        for bad in self.exclude_trims:
            bad = bad.strip().lower()
            if not bad:
                continue
            if " " in bad:
                if re.search(rf"(?<![a-z0-9.]){re.escape(bad)}(?![a-z0-9.])", trim):
                    return False
            elif bad in trim_words:
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
class MarketModel:
    """One model to benchmark against the open market, independent of watches.

    Curves used to hang off watch entries, which keyed them by the watch's
    FIRST model, so a watch naming fourteen models could only ever have one
    curve and the primary target had none at all. Every car Lorenzo might
    buy deserves an outside benchmark, so they live here instead.

    `make` and `model` are the seller's own strings, matched against the
    listing; `slug` is Cars.com's ("mazda-cx_50_hybrid"), whose prefix is
    also the Cars.com make.
    """

    make: str
    model: str
    slug: str
    year_min: int = 0

    @property
    def key(self) -> str:
        """Match `score._normalize_model`: "make|model", lowercased."""
        return f"{self.make.strip().lower()}|{self.model.strip().lower()}"

    @property
    def cars_make(self) -> str:
        return self.slug.split("-", 1)[0]


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
    # Five-year cost inputs. Mileage is the one you do not know yet, so the
    # page carries a selector; this is only its starting value.
    fuel_price: float = 3.20
    depreciation_per_year: float = -0.08     # log rate, when no market curve knows better
    warranty_reserve: int = 1500             # set aside when bumper-to-bumper cover is gone
    miles_per_year: int = 12000
    holding_years: int = 5

    # buyer preferences (soft: they shape reporting, they do not filter)
    pref_colors: list[str] = field(default_factory=list)
    pref_trims: list[str] = field(default_factory=list)
    odometer_ideal: int = 0
    odometer_tolerance: int = 0
    # The Columbus-area radius for the dealer tab's default view and count.
    dealer_radius_miles: int = 75

    carmax_max_shipping: int = 499

    # Condition survey: how many drivable cars may have their history read
    # per run, how long that may take, and how long an attempt that found
    # nothing readable stands before it is retried.
    condition_survey_per_run: int = 30
    condition_survey_seconds: int = 360
    condition_retry_days: int = 14

    # Fleet drops: Hertz offloads a model in waves. A wave is only a wave
    # against that model's own recent arrival rate, so the detector needs a
    # window, a multiple and an absolute floor.
    fleet_alert: bool = True
    fleet_min_drivable: int = 4
    fleet_multiple: float = 2.0
    fleet_window_days: int = 14

    # scoring
    min_comps: int = 12
    min_abs_discount: int = 400
    # Weight on the within-inventory fit is n / (n + market_blend_k) when a
    # Cars.com curve exists for the model; the rest goes to the curve, and
    # the curve never gets less than `market_weight_floor`.
    #
    # The floor is the point. The within-inventory benchmark is ENDOGENOUS:
    # it is fitted on the same sellers whose prices it judges, with a fixed
    # effect per model, so when Hertz offloads a wave of cheap cars the
    # benchmark moves with them and every one reads as ordinary. The
    # Cars.com curve is noisier but exogenous to that. Given a choice
    # between a precise biased number and a noisy unbiased one, and a buyer
    # asking "is Hertz cheap right now", the unbiased one has to lead.
    market_blend_k: float = 20.0
    market_weight_floor: float = 0.6
    # A curve this loose is not worth 60% of the verdict. Thin Cars.com
    # samples occasionally fit badly (a C-Class sample full of AMGs, a GV70
    # whose age term comes out positive), and the floor would hand such a
    # curve the lead. Above this log-RMSE the model keeps the internal fit.
    market_max_rmse: float = 0.12
    # Markdown alerts: how far below its model a marked-down car must sit
    # to be worth a message, and how big the cut must be to count.
    #
    # EITHER bar passes, because one alone does not work across models. The
    # XC60 is scored against a Cars.com curve whose spread is 6.0% of price,
    # so -1.5 sigma there means -9% and never fires; the CX-50 Hybrid, on
    # the within-inventory fit, has a 3.2% spread where -1.5 sigma is -4.8%.
    # Measured over the first eight days of price history: the sigma bar
    # alone fired 0 times, the percentage bar fired once, on a certified
    # XC60 thirteen miles away that had been cut $600.
    markdown_pct: float = -5.0
    markdown_sigma: float = -1.5
    markdown_min_drop: int = 250

    # alerts
    enable_email: bool = True
    enable_push: bool = True
    digest_every_days: int = 3
    realert_price_drop: int = 500

    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    ntfy_server: str = "https://ntfy.sh"

    watch: list[WatchEntry] = field(default_factory=list)
    market: list[MarketModel] = field(default_factory=list)
    sources: dict = field(default_factory=dict)

    # "owner/name" of the repo whose issues hold followed listings. On
    # Actions the runner's GITHUB_REPOSITORY overrides it.
    github_repo: str = ""

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
        fuel_price=float(econ.get("fuel_price", 3.20)),
        depreciation_per_year=float(econ.get("depreciation_per_year", -0.08)),
        warranty_reserve=int(econ.get("warranty_reserve", 1500)),
        miles_per_year=int(prefs.get("miles_per_year", 12000)),
        holding_years=int(prefs.get("holding_years", 5)),
        pref_colors=list(prefs.get("colors", [])),
        pref_trims=list(prefs.get("trims", [])),
        odometer_ideal=int(prefs.get("odometer_ideal", 0)),
        odometer_tolerance=int(prefs.get("odometer_tolerance", 0)),
        dealer_radius_miles=int(prefs.get("dealer_radius_miles", 75)),
        carmax_max_shipping=int(raw.get("carmax", {}).get("max_shipping", 499)),
        condition_survey_per_run=int(raw.get("condition", {}).get("survey_per_run", 30)),
        condition_survey_seconds=int(raw.get("condition", {}).get("survey_seconds", 360)),
        condition_retry_days=int(raw.get("condition", {}).get("retry_days", 14)),
        fleet_alert=bool(raw.get("fleet_drop", {}).get("alert", True)),
        fleet_min_drivable=int(raw.get("fleet_drop", {}).get("min_drivable", 4)),
        fleet_multiple=float(raw.get("fleet_drop", {}).get("multiple", 2.0)),
        fleet_window_days=int(raw.get("fleet_drop", {}).get("window_days", 14)),
        min_comps=int(scoring.get("min_comps", 12)),
        min_abs_discount=int(scoring.get("min_abs_discount", 400)),
        market_blend_k=float(scoring.get("market_blend_k", 20.0)),
        market_weight_floor=float(scoring.get("market_weight_floor", 0.6)),
        market_max_rmse=float(scoring.get("market_max_rmse", 0.12)),
        markdown_pct=float(scoring.get("markdown_pct", -5.0)),
        markdown_sigma=float(scoring.get("markdown_sigma", -1.5)),
        markdown_min_drop=int(scoring.get("markdown_min_drop", 250)),
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
            source=str(w.get("source") or (list(w.get("sources", [])) or ["hertz"])[0]),
            sources=[str(x) for x in (w.get("sources") or [w.get("source") or "hertz"])],
            require_certified=bool(w.get("require_certified", False)),
            require_stock=str(w.get("require_stock", "")),
            alert_radius_miles=(None if w.get("alert_radius_miles") is None
                                else int(w.get("alert_radius_miles"))),
            group=str(w.get("group", "hertz")),
            year_max=int(w.get("year_max", 9999)),
            alert_on_new=bool(w.get("alert_on_new", False)),
            new_max_residual_pct=float(w.get("new_max_residual_pct", 0.0)),
            markdown_alert=bool(w.get("markdown_alert", False)),
            sweep=bool(w.get("sweep", False)),
            price_min=int(w.get("price_min", 0)),
            price_max=int(w.get("price_max", 0)),
            body_styles=list(w.get("body_styles", [])),
            exclude_base_trims=bool(w.get("exclude_base_trims", False)),
            board_max_residual_pct=(None if w.get("board_max_residual_pct") is None
                                    else float(w.get("board_max_residual_pct"))),
            min_sigma=(None if w.get("min_sigma") is None else float(w.get("min_sigma"))),
        )
        for w in raw.get("watch", [])
    ]

    cfg.market = [
        MarketModel(make=str(m.get("make", "")), model=str(m.get("model", "")),
                    slug=str(m.get("slug", "")), year_min=int(m.get("year_min", 0)))
        for m in raw.get("market", [])
        if m.get("make") and m.get("model") and m.get("slug")
    ]

    from .ingest import Source
    cfg.sources = {
        str(sd.get("name", "hertz")): Source(
            name=str(sd.get("name", "hertz")),
            base_url=str(sd.get("base_url", "https://www.hertzcarsales.com")),
            search_path=str(sd.get("search_path", "/all-inventory/index.htm")),
            fixed_params=dict(sd.get("fixed_params", {})),
            kind=str(sd.get("kind", "rental")),
        )
        for sd in raw.get("source", [])
    }
    if not cfg.sources:
        cfg.sources = {"hertz": Source("hertz")}
    DEALER_SOURCE_NAMES.update(name for name, src in cfg.sources.items() if src.kind == "dealer")

    cfg.db_path = _resolve_db_path(raw.get("paths", {}))
    cfg.github_repo = str(raw.get("github", {}).get("repo", ""))

    cfg.smtp_user = os.environ.get("HERTZ_SMTP_USER", "")
    cfg.smtp_password = os.environ.get("HERTZ_SMTP_PASSWORD", "")
    cfg.email_to = os.environ.get("HERTZ_EMAIL_TO", "") or cfg.smtp_user
    cfg.ntfy_topic = os.environ.get("HERTZ_NTFY_TOPIC", "")

    for directory in (cfg.db_path.parent, cfg.log_dir, cfg.out_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return cfg
