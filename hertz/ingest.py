"""Inventory ingestion from hertzcarsales.com.

The site runs on Dealer.com, which ships a fully structured vehicle array in
``window.DDC.dataLayer.vehicles`` on every search results page. We read that
object directly instead of parsing HTML, so the ingest does not break when
the markup changes -- the failure mode that killed the previous version of
this project.

Plain HTTP is not an option: the site sits behind Akamai and a bare request
(even for robots.txt) returns 403. A real browser passes, so Playwright is a
hard requirement rather than a convenience.
"""
from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

from .models import Listing, to_int

logger = logging.getLogger(__name__)

BASE_URL = "https://www.hertzcarsales.com"

# /all-inventory covers BOTH the sales lots and the Rent2Buy fleet;
# /used-inventory covers only the lots. The difference is not marginal: for
# the CX-50 Hybrid it is 61 vehicles versus 19, and the Rent2Buy side holds
# the closest and cheapest cars to Columbus (Toledo, Parma, Louisville).
SEARCH_PATH = "/all-inventory/index.htm"
PAGE_SIZE = 24


@dataclass
class Source:
    """One dealer site to watch.

    Dealer.com powers a large share of US dealer sites, and every one of them
    exposes the same `window.DDC.dataLayer.vehicles` object. So adding a
    dealer costs a config entry, not a new scraper: Hertz Car Sales and Byers
    Volvo are read by identical code.
    """

    name: str
    base_url: str = BASE_URL
    search_path: str = SEARCH_PATH
    fixed_params: dict = field(default_factory=dict)

    def url(self, params: dict) -> str:
        merged = {**self.fixed_params, **params}
        clean = {k: v for k, v in merged.items() if v not in (None, "")}
        return f"{self.base_url}{self.search_path}?{urlencode(clean, doseq=True)}"


HERTZ = Source("hertz")
MAX_PAGES = 60  # 1,440 vehicles; a safety stop, not an expected limit

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Applied before any page script runs, so automation flags are gone by the
# time Akamai's fingerprinting script executes.
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

READ_DATALAYER = """
() => {
    const dl = (window.DDC && window.DDC.dataLayer) || {};
    return {
        vehicles: dl.vehicles || [],
        total: (dl.page && dl.page.attributes && dl.page.attributes.vehicleResultCount) || 0,
        facets: (dl.page && dl.page.queryFacets) || {},
    };
}
"""


class BlockedError(RuntimeError):
    """Raised when the site refuses to serve us the inventory payload."""


class BrowserSession:
    """A Playwright browser plus the polite-crawling behaviour we want."""

    def __init__(
        self,
        headless: bool = True,
        min_delay: float = 1.5,
        max_delay: float = 3.0,
        postal_code: str = "43220",
        city_state: str = "COLUMBUS, OH, US",
        coordinates: str = "40.0464,-83.0680",
        retries: int = 3,
        channel: str | None = None,
        cookie_domains: tuple[str, ...] = (
            ".hertzcarsales.com", ".byersvolvo.com", ".byersmazda.com",
        ),
        carmax_store_id: str = "7176",
    ):
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.postal_code = postal_code
        self.city_state = city_state
        self.coordinates = coordinates
        self.retries = retries
        # CarMax's bot check rejects Playwright's bundled Chromium outright
        # and accepts real Chrome from the same machine and IP, so the
        # channel has to be selectable.
        self.channel = channel
        self.cookie_domains = cookie_domains
        self.carmax_store_id = carmax_store_id
        self._pw = None
        self._browser = None
        self._context = None
        self._last_request = 0.0

    def _location_cookies(self) -> list[dict]:
        """Pin the search origin explicitly rather than trusting IP geolocation.

        Without these, Hertz has no location for the session: `geodist` comes
        back null on every vehicle and `geoRadius` is silently ignored, so a
        radius query quietly returns the entire national inventory. Setting
        them also makes the result independent of where the job runs, which
        matters because GitHub Actions runners are nowhere near Columbus.
        """
        cookies = []
        for domain in self.cookie_domains:
            common = {"domain": domain, "path": "/"}
            cookies += [
                {"name": "DDC.postalCode", "value": self.postal_code, **common},
                {"name": "DDC.postalCityState", "value": self.city_state, **common},
                {"name": "DDC.userCoordinates", "value": self.coordinates, **common},
            ]

        # CarMax ignores ?zip= and geolocates by IP. On a datacenter runner
        # that prices every car's shipping to wherever Azure is, not to
        # Columbus, which made all 44 XC60s look over the shipping cap. These
        # two cookies are how carmax.com remembers "your store"; with them set
        # the quotes come out the same as in a browser at home. 7176 is the
        # Columbus store CarMax itself chose for this zip.
        if self.carmax_store_id:
            lat, _, lon = self.coordinates.partition(",")
            visitor = (f"StoreId={self.carmax_store_id}&Zip={self.postal_code}"
                       f"&Lat={lat.strip()}&Lon={lon.strip()}&ZipConfirmed=True")
            cookies += [
                {"name": "KmxStore", "value": f"StoreId={self.carmax_store_id}",
                 "domain": ".carmax.com", "path": "/"},
                {"name": "KmxVisitor_0", "value": visitor,
                 "domain": ".carmax.com", "path": "/"},
            ]
        return cookies

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        launch_args = {"headless": self.headless}
        if self.channel:
            launch_args["channel"] = self.channel
        self._browser = self._pw.chromium.launch(
            **launch_args,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        self._context.add_init_script(STEALTH_INIT)
        self._context.add_cookies(self._location_cookies())
        logger.info(
            "Browser started (headless=%s, channel=%s, origin=%s)",
            self.headless, self.channel or "chromium", self.postal_code,
        )
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        logger.info("Browser closed")

    def _throttle(self) -> None:
        """Space requests out. We are a guest on someone else's server."""
        elapsed = time.monotonic() - self._last_request
        wait = random.uniform(self.min_delay, self.max_delay) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @contextmanager
    def page(self):
        page = self._context.new_page()
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass

    def load(self, page, url: str, timeout: int = 45000):
        """Navigate with retries. A single DNS blip should not end a run."""
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except Exception as exc:
                last_error = exc
                backoff = min(30.0, 2.0 ** attempt)
                logger.warning(
                    "Navigation attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt, self.retries, type(exc).__name__, backoff,
                )
                time.sleep(backoff)
                continue

            title = (page.title() or "").strip()
            if "Access Denied" in title or "Request unsuccessful" in title:
                # A 403 here is ambiguous between rate limiting and a real
                # block, and rate limiting is the common case: sustained
                # requests trip it and it clears after roughly a minute. Cool
                # off and retry rather than failing the whole run.
                if attempt < self.retries:
                    cooloff = 90.0 * attempt
                    logger.warning(
                        "Edge protection refused %s (attempt %d/%d); cooling off %.0fs",
                        url, attempt, self.retries, cooloff,
                    )
                    time.sleep(cooloff)
                    continue
                raise BlockedError(
                    f"Blocked by edge protection at {url} after {self.retries} attempts "
                    f"(title={title!r}). Slow the request rate."
                )
            return page

        raise RuntimeError(f"Navigation failed after {self.retries} attempts: {url}") from last_error


def search_url(params: dict, source: Source = HERTZ) -> str:
    return source.url(params)


def fetch_page(session: BrowserSession, params: dict,
               source: Source = HERTZ) -> tuple[list[Listing], int]:
    """Fetch one results page. Returns (listings, total matching count)."""
    url = source.url(params)
    with session.page() as page:
        session.load(page, url)
        try:
            page.wait_for_function(
                "() => window.DDC && window.DDC.dataLayer", timeout=20000
            )
        except Exception as exc:
            raise BlockedError(f"dataLayer never appeared at {url}") from exc

        data = page.evaluate(READ_DATALAYER)

    raw_vehicles = data.get("vehicles") or []
    total = to_int(data.get("total")) or 0
    listings = [
        listing
        for listing in (Listing.from_datalayer(rv, source.base_url) for rv in raw_vehicles)
        if listing is not None
    ]
    for listing in listings:
        listing.source = source.name
    logger.info("  %s -> %d rows (of %d total)", url, len(listings), total)
    return listings, total


def fetch_all(session: BrowserSession, params: dict, max_pages: int = MAX_PAGES,
              source: Source = HERTZ) -> list[Listing]:
    """Page through a search until every matching vehicle is collected.

    Stops at `max_pages` and says so. A capped sweep is fine when results are
    sorted cheapest-first, but it must never look like full coverage.
    """
    collected: dict[str, Listing] = {}
    total = None
    truncated = False

    for page_index in range(max_pages):
        page_params = dict(params)
        if page_index:
            page_params["start"] = page_index * PAGE_SIZE

        listings, reported_total = fetch_page(session, page_params, source)
        if total is None:
            total = reported_total
            logger.info("Query reports %d matching vehicles", total)

        if not listings:
            break

        before = len(collected)
        collected.update({listing.vin: listing for listing in listings})
        if len(collected) == before:
            # A repeated page means pagination stopped advancing; stop rather
            # than loop forever.
            logger.warning("No new VINs on page %d; stopping pagination", page_index + 1)
            break

        if total and len(collected) >= total:
            break
    else:
        truncated = bool(total and len(collected) < total)

    result = list(collected.values())
    if truncated:
        logger.warning(
            "TRUNCATED: took the %d cheapest of %d matching vehicles (page cap %d). "
            "Anything above that price is not being watched.",
            len(result), total, max_pages,
        )
    elif total and len(result) < total:
        # Dealer.com pagination drops a row at a page boundary when prices
        # tie, so a full ascending sweep can come up one short -- and locally
        # the missing car was a candidate. One page from the other end of the
        # sort closes the gap cheaply.
        tail, _ = fetch_page(session, {**params, "sortBy": "internetPrice desc"}, source)
        before = len(collected)
        collected.update({l.vin: l for l in tail})
        result = list(collected.values())
        if len(collected) > before:
            logger.info("Reverse pass recovered %d vehicle(s) missed at a page boundary",
                        len(collected) - before)
        if len(result) < total:
            logger.warning(
                "Collected %d of %d reported vehicles (pagination stopped early)",
                len(result), total,
            )
    return result


def fetch_model_nationwide(
    session: BrowserSession, model: str, max_pages: int = MAX_PAGES,
    source: Source = HERTZ, years: list[int] | None = None,
) -> list[Listing]:
    """Every US unit of one model, with distance from the session's origin.

    `model` must be Hertz's exact model string. "CX-50" and "CX-50 Hybrid"
    are different values, and asking for the wrong one returns nothing at
    all rather than erroring, so a zero result is logged loudly below.

    Querying per model, nationally, rather than sweeping a radius: it costs
    an order of magnitude fewer page loads, it guarantees the price model
    same-model comparables, and it still surfaces a bargain parked far away.
    """
    params = {"model": model, "sortBy": "internetPrice asc"}
    if years:
        # Filter server-side, not after the fact. Sweeps are capped and sorted
        # cheapest-first, so a year restriction applied locally would silently
        # never see newer cars: they sit past the price cap. Hertz accepts a
        # repeated `year` parameter.
        params["year"] = years

    logger.info("Fetching %r from %s%s", model, source.name,
                f" (years {years})" if years else "")
    listings = fetch_all(session, params, max_pages=max_pages, source=source)

    if not listings:
        # An empty page from Hertz is far more often a throttled page than an
        # empty market: the target model came back 0 twice in eight cloud
        # runs while every other model answered. One retry after a pause
        # recovers the common case cheaply; the per-model zero-fetch guard in
        # the pipeline still catches the rest.
        logger.warning("Model %r returned no vehicles; retrying once after a pause", model)
        time.sleep(20)
        listings = fetch_all(session, params, max_pages=max_pages, source=source)

    if not listings:
        logger.warning(
            "Model %r returned no vehicles on retry. Either none are in stock or "
            "the model string does not match Hertz's vocabulary.", model,
        )

    # Distances are computed locally in `geo`, not read from the response:
    # Hertz omits `geodist` unless the session carries server-side location
    # state, and silently ignores `geoRadius` in its absence.
    return listings


def fetch_make_nationwide(
    session: BrowserSession, make: str, max_pages: int = MAX_PAGES,
    source: Source = HERTZ
) -> list[Listing]:
    """Every US unit of one make.

    Useful for a marque you would consider broadly, or where the model you
    want is not in stock today: a make query needs no exact model string and
    will pick the car up whenever it arrives.
    """
    logger.info("Fetching make %r from %s", make, source.name)
    listings = fetch_all(
        session, {"make": make, "sortBy": "internetPrice asc"},
        max_pages=max_pages, source=source,
    )
    if not listings:
        logger.warning("Make %r returned no vehicles", make)
    return listings


VDP_DETAILS = """
() => {
    const text = document.body.innerText || '';
    const deliveryMatch = text.match(/\\$([\\d,]+)\\s*Estimated Delivery/i);
    const link = document.querySelector('a.autocheck-link, a[href*="autocheck"]');
    let autocheckUrl = link ? link.getAttribute('href') : null;
    if (autocheckUrl) {
        try {
            const u = new URL(autocheckUrl, location.origin);
            const inner = u.searchParams.get('src');
            if (inner) autocheckUrl = inner;
        } catch (e) { /* keep the raw href */ }
    }
    return {
        delivery: deliveryMatch ? deliveryMatch[1].replace(/,/g, '') : null,
        autocheckUrl,
        freePickup: /Free Pickup/i.test(text),
    };
}
"""


def fetch_vdp_details(session: BrowserSession, url: str) -> dict:
    """Read the per-vehicle delivery quote and AutoCheck link from a detail page.

    One page load yields both, so enrichment costs a single request per car.
    """
    with session.page() as page:
        session.load(page, url)
        try:
            page.wait_for_selector("a[href*='autocheck'], .autocheck-link", timeout=15000)
        except Exception:
            logger.debug("No AutoCheck link surfaced on %s", url)
        details = page.evaluate(VDP_DETAILS)

    return {
        "delivery_quote": to_int(details.get("delivery")),
        "autocheck_url": details.get("autocheckUrl") or "",
        "free_pickup": bool(details.get("freePickup")),
    }
