"""Enterprise Car Sales, through its own search API.

Not a Dealer.com site. The page fetches an anonymous bearer token from
`api.ehi.com` with a public API key, then queries an Elasticsearch search
template. The token is issued only to a browser context that looks like the
site (plain HTTP gets "no Route matched"), so the reader opens the site's
results page once, captures the headers its own search carries, and then
replays searches through Playwright's request API: one request per 200
cars, no card scraping, no paging blocks.

Every hit is structured: VIN, year, make, model, trim as written, sale
price, odometer, postal code, exterior AND interior colour, EPA mpg, list
date, a KBB value. Enterprise sells ex-rentals with a 109-point inspection,
a 12-month/12k-mile limited powertrain warranty and a 7-day repurchase
agreement; every car is stored as certified for the condition gate, and
the alert text says what that certification is.

The stock skews high-mileage (measured 2026-09-14: 988 cars within 300 mi,
median 54k, 5% under 25k), so the reader takes a mileage cap and a model
year floor and applies both before a car ever reaches the store.
"""
from __future__ import annotations

import json
import logging
import time

from .models import Listing

logger = logging.getLogger(__name__)

SITE = "https://www.enterprisecarsales.com/"
WARM_UP = ("https://www.enterprisecarsales.com/content/carsales-web/us/en_us/"
           "search/buy-a-car/sport-utility-vehicles.html")
SEARCH_URL = "https://api.ehi.com/vehicle/sales/retail/inventory/search/template"
# Detail page: the site addresses a car by its VIN (verified 2026-09-14).
VDP_URL = "https://www.enterprisecarsales.com/vehicle/{vin}"
PAGE = 200


def _capture_headers(session, timeout_ms: int = 12000) -> dict:
    """Open the results page and keep the headers of the site's own search
    request: the bearer token, the trace ids, the accept headers."""
    headers: dict = {}
    with session.page() as page:
        def on_request(req):
            if "inventory/search" in req.url and req.headers.get("authorization"):
                headers.update({k: v for k, v in req.headers.items()
                                if not k.startswith(":") and k.lower() not in ("content-length", "host")})
        page.on("request", on_request)
        session.load(page, WARM_UP)
        waited = 0
        while not headers.get("authorization") and waited < timeout_ms:
            page.wait_for_timeout(500)
            waited += 500
        if not headers.get("authorization"):
            raise RuntimeError("Enterprise: the page never made its search request (no token)")
        return dict(headers), page.context


def _listing(hit: dict) -> Listing | None:
    src = hit.get("_source") or {}
    v = src.get("vehicle") or {}
    sp = v.get("specification") or {}
    vin = (v.get("vin") or "").strip().upper()
    price = src.get("salePrice")
    try:
        year = int(sp.get("year") or 0)
        odometer = int((v.get("odometer") or {}).get("lastKnownValue") or 0)
        price = int(price) if price is not None else None
    except (TypeError, ValueError):
        return None
    if not vin or not year or not price:
        return None
    colour = v.get("color") or {}
    mpg = sp.get("fuelEconomy") or {}
    loc = v.get("physicalLocation") or {}
    body = (sp.get("bodyTypes") or [""])[0] if isinstance(sp.get("bodyTypes"), list) else ""
    return Listing(
        vin=vin,
        year=year,
        make=str(sp.get("makeDescription") or "").strip(),
        model=str(sp.get("modelDescription") or "").strip(),
        trim=str(sp.get("trimDescription") or "").strip(),
        price=price,
        no_haggle_price=None,
        odometer=odometer,
        geodist=None,
        lot="Enterprise",
        postal_code=str(loc.get("postalCode") or "").strip()[:5],
        inventory_date=(src.get("listDate") or "")[:10],
        fuel_type=str(sp.get("fuelTypeDescription") or ""),
        drive_line=str(sp.get("drivetrainDescription") or ""),
        body_style=str(body or ""),
        engine=str(sp.get("engineDescription") or ""),
        exterior_color=str(colour.get("exteriorColorDescription") or ""),
        interior_color=str(colour.get("interiorColorDescription") or ""),
        city_mpg=float(mpg["city"]) if mpg.get("city") else None,
        highway_mpg=float(mpg["highway"]) if mpg.get("highway") else None,
        stock_number=str(v.get("fleetVehicleId") or ""),
        status="live",
        certified=True,
        classification="primary",
        source="enterprise",
        url=VDP_URL.format(vin=vin),
        image_url=((v.get("marketingImages") or {}).get("images") or [""])[0],
    )


def fetch(session, makes: list[str], home: tuple[float, float], radius_miles: int,
          odometer_max: int, year_min: int, pause: float = 0.8) -> list[Listing]:
    """Every Enterprise car of these makes within the radius, under the
    mileage cap and at or above the model year, one API page at a time."""
    headers, context = _capture_headers(session)
    out: list[Listing] = []
    seen: set[str] = set()
    for make in makes:
        start, total = 0, None
        while True:
            params = {"vehicleAvailableForSale": [["true"]],
                      "latitude": home[0], "longitude": home[1], "radius": int(radius_miles),
                      "makeDescription": [[make]], "size": PAGE, "from": start}
            body = json.dumps({"id": "filter_search_template", "params": params})
            response = context.request.post(SEARCH_URL, data=body, headers=headers)
            if not response.ok:
                logger.warning("Enterprise: %s page at %d refused (HTTP %d): %s",
                               make, start, response.status, response.text()[:120])
                break
            payload = response.json()
            hits = (payload.get("hits") or {}).get("hits") or []
            if total is None:
                total = int(((payload.get("hits") or {}).get("total") or {}).get("value") or 0)
            kept = 0
            for hit in hits:
                listing = _listing(hit)
                if listing is None or listing.vin in seen:
                    continue
                if listing.odometer > odometer_max or listing.year < year_min:
                    continue
                seen.add(listing.vin)
                out.append(listing)
                kept += 1
            logger.info("Enterprise: %s from %d: %d hits, %d kept (of %s within %d mi)",
                        make, start, len(hits), kept, total, radius_miles)
            start += PAGE
            if not hits or start >= (total or 0):
                break
            time.sleep(pause)
    logger.info("Enterprise: %d cars under %d miles, %d+ across %d makes",
                len(out), odometer_max, year_min, len(makes))
    return out
