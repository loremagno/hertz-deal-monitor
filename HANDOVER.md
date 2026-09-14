# HANDOVER.md — Car deal monitor (Hertz + Byers + CarMax)

Stable technical context. Architecture and setup live in `README.md`; this file
records the operational knowledge that was expensive to acquire and is not
recoverable by reading the code.

**Repo**: `github.com/LorenzoMagnolfi/hertz-deal-monitor` (**private**)
**Local**: `ClaudeCodeProjects/Hertz/` (the folder name predates the multi-source scope)
**Purpose**: alert Lorenzo when a good car appears within driving range of 43220.
Primary target a 2025+ Mazda CX-50 Hybrid; several secondary watches.

---

## Source landscape — what works, what does not, and why

| Source | Status | Access route |
|---|---|---|
| Hertz Car Sales | **works** | Dealer.com `window.DDC.dataLayer.vehicles`, headless Chromium |
| Byers Volvo (certified) | **works** | same Dealer.com reader, different base URL |
| Byers Mazda | **works** | same; zero CX-70 stock at time of writing |
| Cars.com (search) | **works** | listings read from the page's own `<search-provider data-vehicle-array>` JSON (trim as written, VIN, price, mileage, exterior-colour bucket, seller zip, CPO flag); page size clamped to 24; one request per browser session; colour facets (`exterior_color_slugs[]`, `interior_color_slugs[]`) filter server-side |
| CarMax | **works, real Chrome only** | `.kmx-car-tile__content` tiles |
| Cars.com (detail pages) | **blocked** | Cloudflare "Attention Required" |
| CarMax (detail pages) | **blocked** | per-page Akamai check, even in real Chrome |
| CarGurus | **not implemented** | opaque per-model entity IDs; dropped at Lorenzo's request |
| Autotrader, Enterprise | **not attempted** | Akamai per prior research |

**Plain HTTP works nowhere.** Hertz returns 403 for everything including
`robots.txt`; CarMax and Cars.com likewise. A browser is a hard requirement.

**CarMax is the interesting case.** It rejects Playwright's bundled Chromium
with a hard Access Denied and accepts **real Chrome** (`channel="chrome"`) from
the same machine and the same IP. That is headless fingerprinting, not an IP
block. Playwright's sync API also refuses to start a second browser while one is
live on the same thread, so CarMax runs as its own pass *before* the main
session (`pipeline.collect_carmax`), never nested inside it.

---

## Traps that produce silent, plausible-looking wrong answers

Each of these was hit for real during the build. All are guarded in code now,
but they will re-emerge if the guards are removed.

1. **`CX-50` and `CX-50 Hybrid` are disjoint model values.** Querying the wrong
   one returns zero rows and no error. A model matching nothing is logged loudly
   for exactly this reason.
2. **`/used-inventory` hides two thirds of the cars.** `/all-inventory` also
   covers the Rent2Buy fleet: 61 CX-50 Hybrids versus 19, and the Rent2Buy side
   holds the closest and cheapest cars to Columbus.
3. **`geodist` is absent and `geoRadius` is silently ignored** unless the session
   carries server-side location state a headless run cannot reliably obtain. A
   radius query then returns the entire national inventory while looking normal.
   Distances are therefore computed locally in `geo.py` from lot postcodes;
   haversine reproduces Hertz's own figures well (their "86 mi" vs a computed 83).
4. **Capped sweeps hide new model years.** Sweeps are page-capped and sorted
   cheapest-first, so newer cars sit past the cap. A *local* year filter then
   silently never sees them. This was hiding every 2026 Palisade, Atlas and
   GV80. Year filtering must be **server-side** (`year=2026`, repeatable param,
   `urlencode(doseq=True)`).
5. **Hertz's `msrp` field is the pre-doc-fee "No Haggle Price"**, not a
   manufacturer MSRP, and sits *below* `internetPrice`. `internetPrice − msrp`
   is the embedded state doc fee ($387 OH, $649 AZ). Never use `msrp` as a
   discount baseline.
6. **Rent2Buy `inventoryDate` is a FUTURE availability date.** Naive
   days-on-lot arithmetic yields negative ages.
7. **AutoCheck parsing: `"Damage Reported"` is a substring of `"No Accidents or
   Damage Reported"`.** A naive positive check marks every clean car as damaged.
   Match the two unambiguous verdict sentences instead, damage first. Also,
   `Airbag Deployed` / `Structural Damage` / `Overturned` are column headings
   printed on every report including clean ones; the event rows spell it
   `Air Bag Deployed`, with a space.
8. **Delivery is not flat.** ~$145 + $2.00/mile, calibrated from two live quotes
   (86 mi → $316; 1,656 mi → $3,464). Rent2Buy has no delivery at all.
9. **SQLite must not live in Dropbox.** Dropbox syncs `-wal`/`-shm` sidecars
   independently and copies mid-transaction. The DB defaults to
   `%LOCALAPPDATA%\HertzMonitor\`; CI overrides `HERTZ_DB_PATH` into the repo.
10. **Rate limiting is real** on Hertz (roughly 6–10 rapid requests → 403,
    clearing in ~75s) and on Cars.com (page 2 of any search → Cloudflare block).
11. **GitHub Pages on this repo publishes to Lorenzo's professional website.**
    His account has a user Pages site with the custom domain
    `www.lorenzomagnolfi.com`, and every *project* Pages site inherits it. A
    Pages site here went live at `www.lorenzomagnolfi.com/hertz-deal-monitor/`
    on 2026-09-09 before being deleted. **Never enable Pages on this repo.**
    The dashboard is the Claude artifact plus the email digest.
12. **The Actions runner image ships Google's Chrome apt repo pre-configured**,
    and it intermittently serves a stale index that fails *every*
    `apt-get update` with a hash-sum mismatch, even for unrelated packages.
    The workflow removes `/etc/apt/sources.list.d/google*.list` before
    installing browsers; real Chrome (CarMax) is a separate, non-fatal step.

---

## The price model

Pooled OLS, refit every run, in pure Python (`score.fit_hedonic`):

```
log(P_i) = α + β₁(odo/10⁴) + β₂(odo/10⁴)² + β₃age + β₄trim + β₅rent2buy
             + Σ_m δ_m·1[model = m] + ε
```

Fitted on the **Hertz Price**, not landed cost — landed cost embeds distance and
would confound "cheap" with "far". Ridge λ = 10⁻³·N on non-intercept terms.
Typical fit: N ≈ 550, k = 16, log-RMSE ≈ 0.035–0.040.

Representative estimates: −6.0% per 10k miles, −9.9% per year. Trim and
Rent2Buy terms come out near zero, because Hertz appears to price
algorithmically off mileage, age and model, and there is little within-model
trim variation in its inventory.

**Known limitations, in order of importance.** It is a *within-Hertz* benchmark:
model FEs absorb the level, so a residual never says "Hertz is cheap". Tier B
samples are truncated from above by the page cap, biasing those models' fitted
level downward. No colour, options or condition controls. Asking prices, not
transaction prices. No standard errors; residuals are not studentized.

**Thin-model fallback and the market curve.** Watch entries carrying
`market_make`/`market_slug` take their benchmark from a Cars.com curve for the
same model (`benchmark.fit_curve`, cached 24h) whenever the model has fewer than
30 rows of our own (`score.MARKET_PREFERRED_BELOW`): a same-model curve on the
open market beats a model dummy fitted on a dozen rows, most of them one
dealer's uniform pricing. The CX-50 Hybrid, with hundreds of Hertz rows, keeps
the within-Hertz benchmark. The curve is fitted on the years the watch wants
(`year_min` goes to Cars.com) and on a whole-word trim ladder (Mazda: Preferred
< Premium < Premium Plus; Volvo: Core < Plus < Ultra). The trim term matters:
omitting it once flipped the Hertz-vs-market comparison from "5–9% over" to
"3–6% under", because Hertz stocks only Premium Plus CX-50s while the open
market is mostly Preferred.

Three defects made the first version of this curve wrong, found on
2026-09-13 when every CarMax XC60 showed −7% to −17% "vs model" while being
priced like every other XC60. The sample was unfiltered: 11 cars, median 2021,
42k miles, $32k, and a log-linear −10%/yr age term extrapolated a 2025 B5 Plus
to $48–50k, above its MSRP. The card parser cut trims at the word "Hybrid", so
every non-hybrid comp had an empty trim and sat in the middle tier. And the
sample was 11 because Cars.com renders its result cards in shadow DOM and only
a handful expose text: the card reader saw 6 of 24. The fetcher now reads the
page's vehicle array (`benchmark.read_results`) and the cache key carries the
year filter, so the old curve is never reused.

**Alerts fire on two independent events**: value (residual below the tier
threshold) and arrival (`alert_on_new`). Condition is never waived — except that
CarMax cars, having no readable history report, are explicitly labelled
unverified and cannot pass the value gate.

---

## Following a car

The board's ☆ pre-fills a GitHub issue on the repo (label `follow`; the body
carries `key:` = VIN or Cars.com listing URL, `url:`, `price:`, `miles:`).
Creating the issue is the act of following; closing it stops. Each run
(`hertz/follow.py`, with the workflow's own `GITHUB_TOKEN` and `issues: write`)
reads the open follow issues, compares each car with the state saved in `meta`
(`follow_state:<key>`), and on any change comments on the issue and sends a
push: price up or down, mileage moved ≥300 mi, gone from the site, back on the
site, AutoCheck changed. First sight posts a baseline comment. Cars.com cars
are looked up in the cached sweep (VIN or URL), so their changes surface only
when a sweep answers. `data.json.follow` carries the set, each car's state and
a 20-entry log for the Followed tab; the page also stars locally so the tab
updates before the next run, and marks such rows "device only".

---

## Replication

```bash
python -m venv venv                     # needs a Python with venv; C:\Py313 is broken
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m playwright install chromium chromium-headless-shell chrome
```

`chrome` is required for CarMax specifically. Secrets come from the environment
(`HERTZ_SMTP_USER`, `HERTZ_SMTP_PASSWORD`, `HERTZ_EMAIL_TO`, `HERTZ_NTFY_TOPIC`)
and are already set as repository secrets. Run `python -m hertz --dry-run` to
score and render without sending anything.

---

## Hosting decisions (settled 2026-09-09)

- **Repo is private.** It holds Lorenzo's home zip and car preferences. It was
  public for a few hours to get free Pages; that was a mistake (trap 11).
- **No GitHub Pages, ever, on this repo** (trap 11). Dashboard = the Claude
  artifact, refreshed on request, plus the email digest.
- **Cadence every 2 hours.** A private repo has 2,000 free Actions minutes a
  month; this uses ~1,200. Faster refresh requires either a public repo
  (unlimited minutes, but public zip/preferences) or a small VPS. Lorenzo has
  asked for fast refreshes and has not yet chosen between these.
- **Only `data/hertz.db` is committed by the workflow.** Rendered HTML and
  JSON (~1.3 MB per run) go to a 14-day run artifact instead.
- The workflow is installed at `.github/workflows/monitor.yml`; the `gh`
  credential now carries `workflow` scope (`gh auth refresh -s workflow`).
  `gh` lives at `C:\GitHubCLI\gh.exe`, off PATH.

## Known open issues

- **Hertz/Byers from a datacenter IP: verified working** (2026-09-09).
- **CarMax from a datacenter IP serves a different page variant** than a
  desktop browser; the parser now scans for the title line rather than
  assuming it is first. Confirm on a scheduled run.
- **Cars.com clamps page size to 24**, whatever is asked, so a market curve
  is fitted on the 24 "best match" listings for the year filter. Paging would
  need a fresh browser per page (the second request in a session is blocked)
  and is not done; 24 same-model, same-years cars with real trims is enough
  for a three-parameter curve, and the caveat is on the board.
- **Following needs Issues enabled on the repo** (`has_issues` was false on
  2026-09-13). Until then the page keeps stars in the browser and says so.
- **Cars.com from a datacenter IP allows about one request** before
  Cloudflare blocks. Market curves for thin models will fit intermittently
  and rely on the 24 h cache. A residential IP (local run) fills them in.
- **`out/board.html` (email render) and `out/board_artifact.html` (page) are
  separate renderers** sharing data, not markup. Changing one does not change
  the other.
