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
| Cars.com (search) | **works** | `fuse-card` result tiles, one deep page only |
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

**Thin-model fallback.** With two XC60s, that model's own dummy fits its level
almost exactly and the residual is arithmetic rather than evidence. Watch
entries carrying `market_make`/`market_slug` therefore take their benchmark from
a Cars.com curve for the same model (`benchmark.fit_curve`, cached 24h). The
curve controls for trim — omitting that flipped the sign of the Hertz-vs-market
comparison from "5–9% over" to "3–6% under", because Hertz stocks only Premium
Plus while the open market is mostly Preferred.

**Alerts fire on two independent events**: value (residual below the tier
threshold) and arrival (`alert_on_new`). Condition is never waived — except that
CarMax cars, having no readable history report, are explicitly labelled
unverified and cannot pass the value gate.

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

- **Datacenter-IP behaviour is verified only once** (first green run,
  2026-09-09). If Akamai starts refusing runners, the fallback is a small VPS.
- **`out/board.html` (email render) and `out/board_artifact.html` (page) are
  separate renderers** sharing data, not markup. Changing one does not change
  the other.
