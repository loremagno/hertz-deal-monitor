"""Core data types for the Hertz deal monitor.

Every record the monitor handles is one of three things: a `Listing` (a car
Hertz Car Sales currently offers), an `AutoCheck` (its Experian vehicle
history), or a `Scored` listing (a listing plus the economics we derive).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime


def to_int(value) -> int | None:
    """Hertz returns numbers as strings, sometimes with $ and commas."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return int(digits) if digits else None


def to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


@dataclass
class Listing:
    """One vehicle as Hertz currently presents it.

    Field names follow the Dealer.com dataLayer where the meaning matches.
    Two of Hertz's own names are actively misleading and are renamed here:
    their ``msrp`` is the pre-doc-fee "No Haggle Price", not a manufacturer
    MSRP, and their ``internetPrice`` is the doc-fee-inclusive "Hertz Price".
    """

    vin: str
    year: int
    make: str
    model: str
    trim: str

    price: int | None            # "Hertz Price" -- doc fee included, pre-tax
    no_haggle_price: int | None  # Hertz's mislabelled `msrp` field
    odometer: int | None
    geodist: float | None        # miles from the configured home zip

    lot: str = ""                # e.g. "Cincinnati, OH"
    city: str = ""
    state: str = ""
    postal_code: str = ""

    inventory_date: str = ""     # MM/DD/YYYY, when the unit entered inventory
    fuel_type: str = ""
    normal_fuel_type: str = ""
    drive_line: str = ""
    body_style: str = ""
    engine: str = ""
    exterior_color: str = ""
    interior_color: str = ""
    city_mpg: float | None = None
    highway_mpg: float | None = None

    stock_number: str = ""
    status: str = ""
    certified: bool = False
    classification: str = ""     # "primary" = sales lot, "fleet" = Rent2Buy
    source: str = "hertz"        # which dealer site this came from
    url: str = ""
    image_url: str = ""

    # Filled in later, from the vehicle detail page.
    delivery_quote: int | None = None

    @property
    def is_rent2buy(self) -> bool:
        """True for cars still in the rental fleet, sold through Rent2Buy.

        These are two-thirds of the CX-50 Hybrid inventory and include the
        closest and cheapest cars, so they matter. They come with real
        caveats: the car may still be out on rent, its mileage and price are
        explicitly estimates, there is no delivery (you collect it from the
        rental branch), and there are far fewer photos.
        """
        return self.classification.strip().lower() == "fleet" or "renttwobuy" in self.url.lower() \
            or "/rent2buy/" in self.url.lower()

    def _inventory_date(self) -> date | None:
        if not self.inventory_date:
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(self.inventory_date, fmt).date()
            except ValueError:
                continue
        return None

    @property
    def doc_fee(self) -> int | None:
        """Hertz Price minus No Haggle Price. Varies by state ($387 OH, $649 AZ).

        Rent2Buy listings omit the no-haggle field on the search page, so this
        is None for them even though the detail page shows the same structure
        and the doc fee is likewise already inside the quoted price.
        """
        if self.price is None or self.no_haggle_price is None:
            return None
        return self.price - self.no_haggle_price

    @property
    def available_from(self) -> date | None:
        """For Rent2Buy, `inventoryDate` is a future availability date."""
        listed = self._inventory_date()
        if listed and listed > date.today():
            return listed
        return None

    @property
    def days_on_lot(self) -> int | None:
        """Days since the unit entered inventory. Long tenure predicts markdowns.

        None when the date is in the future, which is how Rent2Buy expresses
        "available from"; counting that as negative tenure would be wrong.
        """
        listed = self._inventory_date()
        if listed is None or listed > date.today():
            return None
        return (date.today() - listed).days

    @property
    def age_years(self) -> float | None:
        """Approximate age, treating a model year as arriving mid prior year."""
        if not self.year:
            return None
        today = date.today()
        now = today.year + (today.month - 1) / 12.0
        return max(0.0, now - (self.year - 0.5))

    @property
    def label(self) -> str:
        return " ".join(p for p in (str(self.year), self.make, self.model, self.trim) if p).strip()

    @classmethod
    def from_datalayer(cls, raw: dict, base_url: str = "https://www.hertzcarsales.com") -> "Listing | None":
        """Build a Listing from one entry of ``window.DDC.dataLayer.vehicles``."""
        vin = str(raw.get("vin") or "").strip().upper()
        if len(vin) != 17:
            return None

        address = raw.get("address") or {}
        images = raw.get("images") or []
        link = str(raw.get("link") or "")
        image_url = ""
        if images and isinstance(images[0], dict):
            image_url = images[0].get("uri", "") or ""

        engine = " ".join(
            str(raw.get(k) or "").strip() for k in ("engineSize", "engine")
        ).strip()

        return cls(
            vin=vin,
            year=to_int(raw.get("year") or raw.get("modelYear")) or 0,
            make=str(raw.get("make") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            trim=str(raw.get("trim") or "").strip(),
            price=to_int(raw.get("internetPrice") or raw.get("askingPrice")),
            no_haggle_price=to_int(raw.get("msrp")),
            odometer=to_int(raw.get("odometer")),
            # Never the site's own figure: Avis's dataLayer carries a distance
            # from some default point (Orlando read as 2,429 mi from Columbus,
            # Houston 1,787). Every distance is computed locally from the
            # postal code in geo.annotate_distances.
            geodist=None,
            lot=str(address.get("accountName") or "").strip(),
            city=str(address.get("city") or "").strip(),
            state=str(address.get("state") or "").strip(),
            postal_code=str(address.get("postalCode") or "").strip(),
            inventory_date=str(raw.get("inventoryDate") or "").strip(),
            fuel_type=str(raw.get("fuelType") or "").strip(),
            normal_fuel_type=str(raw.get("normalFuelType") or "").strip(),
            drive_line=str(raw.get("driveLine") or "").strip(),
            body_style=str(raw.get("bodyStyle") or raw.get("normalBodyStyle") or "").strip(),
            engine=engine,
            exterior_color=str(raw.get("exteriorColor") or "").strip(),
            interior_color=str(raw.get("interiorColor") or "").strip(),
            city_mpg=to_float(raw.get("cityFuelEfficiency") or raw.get("cityFuelEconomy")),
            highway_mpg=to_float(raw.get("highwayFuelEfficiency")),
            stock_number=str(raw.get("stockNumber") or "").strip(),
            status=str(raw.get("status") or "").strip(),
            certified=bool(raw.get("certified")),
            classification=str(raw.get("classification") or "").strip(),
            url=(base_url + link) if link.startswith("/") else link,
            image_url=image_url,
        )

    def to_row(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: dict) -> "Listing":
        """Rebuild from a stored row.

        Lets a run render the full board and fit the price model on every
        vehicle currently known, not only the models re-polled this time.
        """
        fields = {f for f in cls.__dataclass_fields__}
        data = {k: v for k, v in row.items() if k in fields}
        data["certified"] = bool(data.get("certified"))
        return cls(**data)


@dataclass
class AutoCheck:
    """Experian AutoCheck history, free on every Hertz detail page.

    ``is_clean`` is the gate that keeps accident cars out of alerts.
    """

    vin: str
    score: int | None = None
    peer_low: int | None = None
    peer_high: int | None = None
    title_brand: str = ""
    accident_summary: str = ""
    accidents_reported: bool | None = None
    odometer_issue: bool | None = None
    open_recalls: bool | None = None
    owners: int | None = None
    usage: str = ""
    in_service_date: str = ""
    last_odometer: int | None = None
    fetched_at: str = ""
    raw_text: str = ""

    @property
    def is_clean(self) -> bool:
        """True only when every hard condition check passes.

        Deliberately conservative: unknown (None) counts as not clean, so a
        parse failure suppresses an alert rather than letting a damaged car
        through. AutoCheck cannot see unreported damage, so a clean result
        still warrants a pre-purchase inspection.
        """
        if self.accidents_reported is not False:
            return False
        if self.odometer_issue is not False:
            return False
        if self.title_brand and self.title_brand.strip().lower() != "clean":
            return False
        return True

    @property
    def concerns(self) -> list[str]:
        """Human-readable reasons this car should give you pause."""
        out: list[str] = []
        if self.accidents_reported:
            out.append(f"Accident/damage reported: {self.accident_summary or 'see report'}")
        elif self.accidents_reported is None:
            out.append("Accident history could not be read")
        if self.odometer_issue:
            out.append("Odometer discrepancy reported")
        elif self.odometer_issue is None:
            out.append("Odometer check could not be read")
        if self.title_brand and self.title_brand.strip().lower() != "clean":
            out.append(f"Title brand: {self.title_brand}")
        if self.open_recalls:
            out.append("Open recall outstanding")
        if self.score is not None and self.peer_low is not None and self.score < self.peer_low:
            out.append(
                f"AutoCheck score {self.score} below peer range ({self.peer_low}-{self.peer_high})"
            )
        return out


@dataclass
class Scored:
    """A listing with the economics attached."""

    listing: Listing
    delivery: float
    tax: float
    fees: float
    landed_cost: float
    predicted_landed: float | None = None
    residual_pct: float | None = None      # negative = cheaper than predicted
    residual_sigma: float | None = None    # residual in units of the fit's RMSE
    comp_n: int = 0
    benchmark: str = ""                    # "hertz" (pooled hedonic) or "market" (Cars.com)
    tier: str = ""                          # "A", "B", or ""
    matched_label: str = ""
    price_drop_30d: int | None = None
    autocheck: AutoCheck | None = None
    history_url: str = ""                  # a Carfax the seller links, unread
    reasons: list[str] = field(default_factory=list)

    @property
    def vin(self) -> str:
        return self.listing.vin
