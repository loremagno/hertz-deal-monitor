"""Mazda USA's own inventory locator: every franchise dealer's new and certified stock.

mazdausa.com/shopping-tools/inventory/results is a JavaScript app over two
endpoints, found 2026-09-21:

  GET  /handlers/dealer.ajax?zip=43220&maxDistance=300&p=1
       the dealers within a radius: id, name, address, zip, driving miles.
  POST /api/inventorysearch  (form-encoded, X-Requested-With: XMLHttpRequest)
       200 vehicles a page (`ResultsStart` is the PAGE number, 1, 2, 3) for a
       list of dealer ids, a `Vehicle[Type][]` ("n" new, "c" certified), an
       optional `Vehicle[Carline][]` code ("C50" CX-50, "50H" CX-50 Hybrid,
       "C70" CX-70; the codes are read from the response's own Filters.Models
       so nothing is guessed) and an optional major exterior colour.

Each record carries the VIN, model year, trim name, exterior AND interior
descriptions ("Machine Gray Metallic" / "Terracotta Leather"), the sticker
(`Price` is MSRP with freight, packages and options), mileage, the dealer,
whether the car is at the dealership or in transit with its ETA, and the
dealer's own listing URL. There is NO dealer price on a new car: the locator
knows the sticker, the dealer's page knows the discount. For a certified
car `Price` is the dealer's asking price and `BaseMsrp` the original sticker.

Why this source: Byers is one Dealer.com page, but Germain's two Columbus
stores run Dealer Inspire (Algolia behind a WordPress ajax handler), and the
locator covers all three, plus the twenty dealers from Cincinnati to
Cleveland, Pittsburgh, Detroit and Indianapolis, in a handful of requests.
It needs a browser only for the session cookies; the calls themselves go
through the page context's request API, like Enterprise.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from .models import Listing, to_int

logger = logging.getLogger(__name__)

BASE = "https://www.mazdausa.com"
RESULTS_PAGE = BASE + "/shopping-tools/inventory/results"
DEALERS_URL = BASE + "/handlers/dealer.ajax"
SEARCH_URL = BASE + "/api/inventorysearch"
HEADERS = {
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "x-requested-with": "XMLHttpRequest",
}
PAGE_SIZE = 200
MAX_PAGES = 15          # 3,000 cars of one model within the radius; a safety stop
SOURCE = "mazdausa"
TYPE_NAMES = {"n": "new", "c": "certified"}


def _dealers(req, zip_code: str, radius_miles: int) -> list[dict]:
    """Every dealer within the radius, paged until the reported total."""
    out: list[dict] = []
    total = None
    for page in range(1, 11):
        r = req.get(f"{DEALERS_URL}?zip={zip_code}&maxDistance={int(radius_miles)}&p={page}")
        if r.status != 200:
            raise RuntimeError(f"dealer list HTTP {r.status}")
        body = (r.json() or {}).get("body") or {}
        results = body.get("results") or []
        if total is None:
            total = to_int(body.get("total"))
        out.extend(results)
        if not results or (total is not None and len(out) >= total):
            break
    logger.info("Mazda USA: %d dealer(s) within %d mi of %s", len(out), radius_miles, zip_code)
    return out


def _search(req, dealer_ids: list[str], cond: str, carline: str | None = None,
            page: int = 1, size: int = PAGE_SIZE) -> dict:
    params = {
        "ResultsPageSize": str(size), "ResultsParameterFilter": "",
        "ResultsSortAttribute": "Year", "ResultsSortOrder": "asc",
        "ResultsStart": str(page), "NearResultsStart": "1", "TimeSearchPerformed": "",
        "Vehicle[DealerId][]": dealer_ids, "Vehicle[Type][]": cond, "Vehicle[cond][]": cond,
        "GetNearMatch": "false", "IsIntransitDisplay": "true",
    }
    if carline:
        params["Vehicle[Carline][]"] = carline
    r = req.post(SEARCH_URL, data=urlencode(params, doseq=True), headers=HEADERS)
    if r.status != 200:
        raise RuntimeError(f"inventory search HTTP {r.status}")
    return (r.json() or {}).get("response") or {}


def _carline_codes(filters: dict, models: list[str]) -> dict[str, str]:
    """{model string: carline code} from the response's own model filter.

    Filter names read "MAZDA CX-50", "MAZDA CX-50 HYBRID"; the watch names
    "CX-50", "CX-50 Hybrid". Exact match after dropping the make, so the
    CX-50 never absorbs the hybrid (the Hertz trap, again).
    """
    entries = filters.get("Models") or []
    by_name = {}
    for e in entries:
        if not isinstance(e, dict) or not e.get("Code"):
            continue
        name = str(e.get("Name") or "").upper().replace("MAZDA", "").strip()
        by_name[name] = str(e["Code"])
    out = {}
    for model in models:
        code = by_name.get(model.upper().strip())
        if code:
            out[model] = code
        else:
            logger.info("Mazda USA: no model code for %r in this search (%s)", model,
                        ", ".join(sorted(by_name)) or "no models listed")
    return out


def _listing(v: dict, dealer: dict | None, cond: str,
             asked_model: str = "") -> Listing | None:
    vin = str(v.get("Vin") or "").strip().upper()
    if len(vin) != 17:
        return None
    info = v.get("Model") or {}
    colors = v.get("Colors") or {}
    name = str(info.get("Name") or "").strip()            # "2026 CX-50 Hybrid"
    year = to_int(info.get("Year")) or 0
    model = name
    if year and name.startswith(str(year)):
        model = name[len(str(year)):].strip()
    # A sizeable minority of records come back with a sparse Model object:
    # no Name and no TrimName, only Description ("2026 CX-50 2.5 TURBO
    # MERIDIAN EDITION"). Both fields then land empty, the car matches no
    # watch at all, and it disappears silently. On 2026-09-21 that hid 30
    # of the 41 terracotta cars within 120 miles, every one a Meridian,
    # which is precisely the trim Lorenzo is hunting. The carline we asked
    # for supplies the model; Description supplies the trim.
    if not model:
        model = asked_model
    price = to_int(v.get("Price"))
    is_new = cond == "n"
    msrp = price if is_new else (to_int(v.get("BaseMsrp")) or None)
    in_transit = str(v.get("VehicleLocation") or "") == "01"
    eta = str(v.get("ETADate") or "")[:10]
    dealer = dealer or {}
    engine = info.get("Engine") or {}
    url = str(v.get("DealerSiteURL") or "").strip()
    if not url and v.get("DetailsPageURL"):
        url = BASE + str(v["DetailsPageURL"])
    # TrimName is empty on a sizeable minority of records (about 30 of the
    # 41 terracotta cars within 120 mi on 2026-09-21, every one a Meridian
    # Edition), and an empty trim is worse than a wrong one here: it fails
    # `require_trims` and `wheel_inches` alike, so the cars Lorenzo most
    # wants vanish from their lane. Fall back to the marketing description
    # with its "<year> <model>" prefix removed, then to the internal code.
    trim = str(info.get("TrimName") or "").strip()
    if not trim:
        desc = str(info.get("Description") or "").strip()
        for prefix in (f"{year} {model}", model, f"{year}"):
            prefix = str(prefix).strip().upper()
            if prefix and desc.upper().startswith(prefix):
                desc = desc[len(prefix):].strip()
        trim = desc.title() if desc else str(info.get("Trim") or "").strip()

    return Listing(
        vin=vin, year=year, make="Mazda", model=model,
        trim=trim,
        price=price,
        no_haggle_price=None,           # the locator quotes no doc fee
        odometer=to_int(v.get("Mileage")) or 0,
        geodist=None,                   # from the dealer's zip, in geo
        lot=str(v.get("DealerName") or dealer.get("name") or "").title(),
        city=str(dealer.get("city") or "").title(),
        state=str(dealer.get("state") or ""),
        postal_code=str(dealer.get("zip") or "").strip()[:5],
        # For a car in transit the ETA doubles as "available from": the
        # listing model reads a future inventory date exactly that way.
        inventory_date=eta if (in_transit and eta) else "",
        fuel_type={"G": "Gasoline", "H": "Hybrid", "E": "Electric"}.get(str(engine.get("FuelType") or ""), ""),
        drive_line=str(info.get("DriveTrainDesc") or ""),
        body_style=str(info.get("BodyStyle") or ""),
        engine=str(engine.get("TypeDesc") or ""),
        exterior_color=str(colors.get("ExteriorDescription") or "").strip(),
        interior_color=str(colors.get("InteriorDescription") or "").strip(),
        status="in transit" + (f", ETA {eta}" if eta else "") if in_transit else "at dealer",
        certified=not is_new,
        classification="primary",
        source=SOURCE,
        url=url,
        stock="new" if is_new else "used",
        msrp=msrp,
    )


def fetch(session, zip_code: str, radius_miles: int, models: list[str],
          types: tuple[str, ...] = ("n", "c")) -> list[Listing]:
    """Every new and certified unit of the wanted models at every dealer
    within the radius. One page load for the session, then API calls."""
    out: dict[str, Listing] = {}
    with session.page() as page:
        session.load(page, RESULTS_PAGE)
        page.wait_for_timeout(4000)
        req = page.request
        dealers = _dealers(req, zip_code, radius_miles)
        if not dealers:
            raise RuntimeError("Mazda USA listed no dealers")
        by_id = {str(d.get("id")): d for d in dealers}
        ids = list(by_id)
        for cond in types:
            probe = _search(req, ids, cond, page=1, size=1)
            total = to_int(probe.get("TotalVehicles")) or 0
            codes = _carline_codes(probe.get("Filters") or {}, models)
            logger.info("Mazda USA: %d %s car(s) at %d dealers; models resolved: %s",
                        total, TYPE_NAMES.get(cond, cond), len(ids),
                        ", ".join(f"{m}={c}" for m, c in codes.items()) or "none")
            for model, code in codes.items():
                got = 0
                for page_no in range(1, MAX_PAGES + 1):
                    if page_no > 1:
                        time.sleep(1.0)
                    resp = _search(req, ids, cond, carline=code, page=page_no)
                    vehicles = resp.get("Vehicles") or []
                    for v in vehicles:
                        listing = _listing(v, by_id.get(str(v.get("DealerId"))), cond,
                                           asked_model=model)
                        if listing is not None:
                            out.setdefault(listing.vin, listing)
                            got += 1
                    if len(vehicles) < PAGE_SIZE:
                        break
                logger.info("  Mazda USA %s %s: %d row(s)", TYPE_NAMES.get(cond, cond), model, got)
    return list(out.values())
