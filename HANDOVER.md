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

Pooled OLS on the **pre-doc-fee price**, refit every run, in pure Python
(`score.fit_hedonic`):

```
log(P_i) = α + β₁(odo/10⁴) + β₂(odo/10⁴)² + Σ_y γ_y·1[model year = y]
             + β₃·trim_tier + β₄·rent2buy + Σ_m δ_m·1[model = m] + ε
```

Hertz's quoted price includes its doc fee; CarMax, Byers and Cars.com prices
exclude theirs, so every model is fitted on the pre-doc price (Hertz lot
cars: the No Haggle Price; Rent2Buy cars: quoted price less the sample's
median Hertz doc fee, $399 on 2026-09-14). Model-year dummies replace a
linear age term: the first-year drop is a cliff, and a straight line through
2024-2026 once priced a 2025 above its MSRP. `trim_tier` is the make's own
ladder (`hertz/trims.py`: Audi Premium = base, Mazda Premium = third rung,
Volvo Core/Plus/Ultra, Hyundai SE/SEL/Limited/Calligraphy, ...), 0 to 3,
1.5 when unrecognised, plus a missing-label indicator: 30 of Hertz's 72
Highlanders are unlabelled Rent2Buy units at a median $36k against $40.7k
for the labelled lot cars, and without the indicator every one read as a
15% bargain. The ridge is numerical only (10⁻⁶·N): the earlier
10⁻³·N shrank every model dummy towards the reference model's level, a ~6%
bias for a nine-row model 60% dearer than the reference. Typical fit on
2026-09-14: N = 982, 25 models, leave-one-out log-RMSE 0.040; −3.9% per 10k
miles at the origin, +3.9% for 2025 and +8.4% for 2026 over 2024, +5% per
trim rung, Rent2Buy +0.8%.

**The residual is leave-one-out.** The car's own row is removed before it is
predicted, exactly, from the hat matrix kept at fit time: e_LOO = e/(1−h).
Without it a two-row model has its dummy fitted through both cars and the
residual is arithmetic. The reported RMSE is the PRESS RMSE; a car's own
sigma is its model's LOO residual SD where the model has 20 or more rows,
else the pooled value, and is widened by 1/√(1−h) for a car in a thin model.
`residual_sigma` on the board is the studentized residual.

**Two benchmarks, blended.** Where a Cars.com curve exists for the model
(`benchmark.fit_curve`: log price on mileage, age, trim rung and a certified
flag, fitted on the years the watch wants, three "best match" pages per
model, ~70 cars, seeded from `docs/market_curves.json` by
`python -m hertz --curves`), the prediction is w·inventory + (1−w)·market
with w = n/(n+K), K = `market_blend_k` = 20: nine XC60s give the market 69%,
sixty CX-50 Hybrids give it 25%. The board's `benchmark` column says which
("hertz", "market", "blend 69% market") and `comps` counts both samples. The
within-inventory fit answers "cheap for what these sellers charge"; the
market curve answers "cheap for what everyone charges"; with one dealer's
uniform pricing behind most thin models, the second is the sharper question.

**Capped sweeps are sorted by mileage, not price.** Taking the cheapest pages
of a big model truncates the sample on the outcome and biases that model's
fitted level down; truncating on mileage, a regressor, does not bias OLS.

**Known limitations.** No colour, options or condition controls. Asking
prices, not transaction prices. Cars.com curves are a 70-car "best match"
sample of a market that can be 1,600 deep. No standard errors on the blend
weight, which is a judgement, not an estimate.

**Alerts fire on two independent events**: value (residual below the tier
threshold) and arrival (`alert_on_new`). Condition is never waived — except
that CarMax cars, having no readable history report, are explicitly labelled
unverified and cannot pass the value gate.

---

## The Hertz 2026 sweep

`[[watch]]` with `sweep = true` and a make list instead of a model list.
Hertz applies the band, mileage cap, body styles and model year server-side
(Dealer.com honours `internetPrice=lo-hi`, `odometer=lo-hi`, `bodyStyle`,
`year`), each make capped at `max_pages` sorted by mileage. Base trims are
dropped by the make's ladder (`exclude_base_trims`); a Palisade SEL stays,
an Audi Premium goes, a Lexus with no trim suffix is unknown and stays. The
board shows a sweep car only at or below `board_max_residual_pct` (−6%) AND
`min_sigma` (−2 studentized); alerts need `threshold_pct` (−8%), the same
sigma bar and a clean AutoCheck. The very-discounted lane carries the same
sigma bar: thirteen unlabelled-trim Rent2Buy Highlanders read −10% to −15%
at only −1.7 sigma, which is a missing trim label, not a deal. A make|model the
store has never held for Hertz sends one quiet push the run it appears
(`RunResult.new_models`, computed before the upsert makes it known).

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
- **Cars.com from the runner is challenged per request, not per session**:
  on run 34802725706 the XC60 curve request was blocked and the CX-70 one,
  made seconds later in the same session, went through. Hence the committed
  curve seed and the seeded Cars.com tabs; the runner's own sweeps are a
  bonus when they land.
- **Cars.com clamps page size to 24**, whatever is asked, so a market curve
  is fitted on the 24 "best match" listings for the year filter. Paging would
  need a fresh browser per page (the second request in a session is blocked)
  and is not done; 24 same-model, same-years cars with real trims is enough
  for a three-parameter curve, and the caveat is on the board.
- **Every stamp that crosses machines is naive UTC** (`hertz/clock.py`).
  Seeds are made in Ohio and adopted on a UTC runner; naive local time made
  a seed refit at 00:23 Eastern look older than a runner fetch at 03:33 UTC
  an hour earlier, and the committed CX-70 curve was never adopted.
- **The persist step resolves a concurrent-snapshot conflict in its own
  favour** (`git pull --rebase -X theirs`, three attempts): two runs close
  together both commit the binary database, and a plain rebase stopped on
  the conflict and lost a full sweep (run 34807668912).
- **Following needs Issues enabled on the repo** (`has_issues` was false on
  2026-09-13). Until then the page keeps stars in the browser and says so.
- **Cars.com from a datacenter IP allows about one request** before
  Cloudflare blocks. Market curves for thin models will fit intermittently
  and rely on the 24 h cache. A residential IP (local run) fills them in.
- **`out/board.html` (email render) and `out/board_artifact.html` (page) are
  separate renderers** sharing data, not markup. Changing one does not change
  the other.
