"""Distance from home to each Hertz lot.

Hertz only returns a `geodist` field when the browser session has server-side
location state that a headless run cannot reliably obtain, and its
`geoRadius` parameter is silently ignored without it -- a radius query then
returns the entire national inventory while looking perfectly normal.

So we compute distance ourselves. Every listing carries its lot's postal
code, there are only about thirty lots, and Hertz's own published distances
are great-circle (their "86 mi" to the Cincinnati lot matches a haversine of
83). Doing it locally is deterministic, needs no session state, works from a
GitHub Actions runner, and cannot silently degrade.

Coordinates come from a bundled table of known lot postal codes, falling back
to a free postal-code lookup that is cached in SQLite.
"""
from __future__ import annotations

import logging
import math

import requests

logger = logging.getLogger(__name__)

EARTH_RADIUS_MILES = 3958.8

# Known Hertz Car Sales lot postal codes. Bundled so the monitor keeps working
# when the geocoding service is unreachable.
LOT_COORDINATES: dict[str, tuple[float, float]] = {
    "45140": (39.2689, -84.2583),   # Cincinnati (Loveland), OH
    "48183": (42.1303, -83.2416),   # Woodhaven, MI
    "15108": (40.5031, -80.2059),   # Pittsburgh (Coraopolis), PA
    "60018": (42.0186, -87.8859),   # Des Plaines, IL
    "27103": (36.0770, -80.2790),   # Winston-Salem, NC
    "37217": (36.1084, -86.6689),   # Nashville, TN
    "23230": (37.5760, -77.4874),   # Richmond, VA
    "21236": (39.3826, -76.4900),   # Baltimore (Nottingham), MD
    "28205": (35.2199, -80.7867),   # Charlotte, NC
    "27603": (35.7490, -78.6510),   # Raleigh, NC
    "63031": (38.7890, -90.3229),   # St. Louis (Florissant), MO
    "19153": (39.8912, -75.2404),   # Philadelphia, PA
    "30273": (33.5829, -84.3383),   # Morrow, GA
    "11787": (40.8543, -73.2010),   # Smithtown, NY
    "01905": (42.4668, -70.9495),   # Lynn, MA
    "32225": (30.3400, -81.5090),   # Jacksonville, FL
    "32822": (28.4870, -81.3120),   # Orlando, FL
    "70058": (29.8958, -90.0517),   # New Orleans (Harvey), LA
    "33765": (27.9736, -82.7462),   # Clearwater, FL
    "33612": (28.0500, -82.4500),   # Tampa, FL
    "73122": (35.5230, -97.6200),   # Oklahoma City, OK
    "76021": (32.8440, -97.1430),   # Bedford, TX
    "34134": (26.3400, -81.7900),   # Bonita Springs, FL
    "77094": (29.7830, -95.6800),   # Houston, TX
    "85034": (33.4400, -112.0100),  # Phoenix, AZ
    "89119": (36.1000, -115.1400),  # Las Vegas, NV
    "80239": (39.7900, -104.8400),  # Denver, CO
    "84116": (40.7770, -111.9300),  # Salt Lake City, UT
    "92507": (33.9800, -117.3400),  # Riverside, CA
    "92154": (32.5700, -117.0400),  # San Diego, CA
}

LOOKUP_URL = "https://api.zippopotam.us/us/{zip}"
# zip -> (coordinates or None, (city, state) or None), one network call each.
_lookup_cache: dict[str, tuple[tuple[float, float] | None, tuple[str, str] | None]] = {}


def _lookup(postal_code: str) -> tuple[tuple[float, float] | None, tuple[str, str] | None]:
    if postal_code in _lookup_cache:
        return _lookup_cache[postal_code]
    coords = place = None
    try:
        response = requests.get(LOOKUP_URL.format(zip=postal_code), timeout=10)
        response.raise_for_status()
        entry = (response.json().get("places") or [{}])[0]
        coords = (float(entry["latitude"]), float(entry["longitude"]))
        place = (str(entry.get("place name") or ""), str(entry.get("state abbreviation") or ""))
        logger.info("Geocoded postal code %s -> %s, %s", postal_code, coords, place)
    except Exception as exc:
        logger.warning("Could not geocode postal code %s: %s", postal_code, exc)
    _lookup_cache[postal_code] = (coords, place)
    return coords, place


def place_for_zip(postal_code: str) -> tuple[str, str] | None:
    """(city, state) for a zip. Cars.com gives a seller zip and nothing else."""
    postal_code = (postal_code or "").strip()[:5]
    if not postal_code:
        return None
    return _lookup(postal_code)[1]


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def parse_coordinates(text: str) -> tuple[float, float] | None:
    try:
        lat, lon = (float(p) for p in str(text).split(",", 1))
        return lat, lon
    except (ValueError, TypeError):
        return None


def coordinates_for_zip(postal_code: str, allow_lookup: bool = True) -> tuple[float, float] | None:
    """Bundled table first, then a cached network lookup."""
    postal_code = (postal_code or "").strip()[:5]
    if not postal_code:
        return None
    if postal_code in LOT_COORDINATES:
        return LOT_COORDINATES[postal_code]
    if not allow_lookup and postal_code not in _lookup_cache:
        return None
    return _lookup(postal_code)[0]


def annotate_distances(
    listings, home: tuple[float, float], cache: dict | None = None
) -> tuple[int, dict]:
    """Fill in `geodist` for every listing.

    Returns (resolved count, newly geocoded postal codes) so the caller can
    persist the additions. Rent2Buy cars sit at rental branches rather than
    the thirty sales lots, so the inventory spans hundreds of postal codes
    and re-resolving them over the network every run would be both slow and
    inconsiderate. Misses are cached too, as None, so a postal code the
    service cannot resolve is not retried forever.
    """
    resolved = 0
    known: dict[str, tuple[float, float] | None] = dict(cache or {})
    discovered: dict[str, tuple[float, float] | None] = {}

    for listing in listings:
        if listing.geodist is not None:
            resolved += 1
            continue

        postal_code = (listing.postal_code or "").strip()[:5]
        if postal_code not in known:
            coords = coordinates_for_zip(postal_code)
            known[postal_code] = coords
            if postal_code:
                discovered[postal_code] = coords

        coords = known[postal_code]
        if coords:
            listing.geodist = round(haversine_miles(home, coords), 1)
            resolved += 1

    unresolved = {l.lot for l in listings if l.geodist is None}
    if unresolved:
        logger.warning(
            "No distance for %d lot(s): %s",
            len(unresolved), ", ".join(sorted(unresolved)[:8]),
        )
    return resolved, discovered
