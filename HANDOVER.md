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
| Byers Mazda, new page | **works** | `byers-mazda-new`, `/new-inventory/index.htm`, same Dealer.com reader; the feed's `pricing` block carries sticker, doc fee ($398), a $50 delivery line and the manufacturer cash; 116 new Mazdas, 26 CX-50/CX-70 on 2026-09-21 |
| Mazda USA locator | **works (API)** | `hertz/mazdausa.py`: `GET /handlers/dealer.ajax` for the dealers within a radius, `POST /api/inventorysearch` (form-encoded, 200 a page, `ResultsStart` is a page number, `Vehicle[Carline][]` codes from the response's own `Filters.Models`) for new (`n`) and certified (`c`) stock; VIN, trim, exterior AND interior descriptions, sticker, in-transit ETA, dealer site URL; no dealer price on new cars |
| Germain Mazda (two Columbus stores) | **not built** | Dealer Inspire, not Dealer.com: WordPress `admin-ajax.php` action `getDealerListings` over Algolia (`window.di_search_settings`); the locator covers their stock, Cars.com their advertised prices |
| Cars.com (new and CPO) | **works** | `stock_type=new_cpo`, `trims[]` facet (slugs in `dream.MAZDA_MODELS`), `sort=list_price`, `msrp` and `stockType` in the vehicle array; the array appends far "shippable" rows after the organic 24, so the radius is enforced on our side |
| Avis Car Sales | **works** | Dealer.com like Hertz: `/used-inventory/index.htm`, `window.DDC.dataLayer`, server-side `odometer`/`model`/`year`; 1,594 cars on 2026-09-14, 26% under 25k miles, 29 CX-50 Hybrids; postal codes present; no Rent2Buy; delivery by the per-mile fallback |
| Enterprise Car Sales | **works (API)** | `hertz/enterprise.py`: opens the site's results page once to capture the headers of its own search (anonymous bearer token; plain HTTP gets "no Route matched"), then replays `POST api.ehi.com/vehicle/sales/retail/inventory/search/template` per make through the browser context's request API, 200 hits a page. Hits carry VIN, odometer, trim, exterior AND interior colour, mpg, postal code, sale price, KBB value. Stock skews high-mileage (median 54k within 300 mi, 5% under 25k), so the reader applies the mileage cap and model-year floor at the API. Stored as certified (109-point inspection, 12/12 powertrain warranty, 7-day repurchase). |
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
13. **A dealer feed's `msrp` is the sticker, Hertz's is a pre-doc price.** The
    same Dealer.com field, two meanings; `Source.kind` decides. Read as a
    no-haggle price, Byers's sticker became the tax base and a CPO car's
    "pre-doc price" sat above its asking price.
14. **Cars.com's vehicle array is longer than the page.** The organic 24 come
    first, sorted as asked; a tail of "shippable" listings from anywhere
    follows, in its own order, whatever `include_shippable` says. A 300-mile
    search returned Cary NC and Minnesota. Filter on the seller zip.
15. **Mazda USA's `Price` on a new car IS the sticker.** The locator knows no
    dealer price; a row from it must never read as "0.0% under sticker".
16. **Mazda USA's inventory API cannot be paged.** It sorts on model year,
    which ties on every car of one year, so page order is unstable: on
    2026-09-22 a 1,439-row sweep of new CX-50s returned 975 distinct VINs,
    two back-to-back sweeps agreed on under 70%, and their union still missed
    166. Every missed car was marked sold and later re-announced as an
    arrival. `mazdausa._collect` now reads a model in one call when it fits a
    page and dealer by dealer when it does not, checks the distinct count
    against `TotalVehicles`, and the pipeline marks a model's cars sold only
    when its new and certified sweeps were both complete
    (`mazdausa.COMPLETE`). First complete sweep: 1,439 of 1,439.
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

**The market leads the benchmark (2026-09-18).** Lorenzo: "we are comparing
cars to other Hertz listings? I would like to benchmark more to the global
market, because otherwise a wave of cheap cars changes the model." Correct,
and it was the central weakness. The within-inventory fit is ENDOGENOUS: it
is fitted on the same sellers it judges, with a fixed effect per model, so a
fleet offload drags the yardstick along with the cars and every one reads as
ordinary. Two changes followed.

*Curves are configured per MODEL, not per watch.* They used to hang off a
watch entry and be keyed by that watch's first model, so a watch naming
fourteen models could only ever have one curve, and the primary target had
none: before this, only the XC60 and CX-70 had any outside benchmark at all.
The `[[market]]` table in `config.toml` now lists one entry per model
(seller's `make` and `model`, Cars.com `slug`, `year_min`), read by
`pipeline.market_curve_targets`. Legacy `market_slug` on a watch still works.

*The curve gets at least `market_weight_floor` (0.6) of the verdict.* Given
a precise but biased estimate and a noisy unbiased one, and a buyer asking
"is Hertz cheap right now", the unbiased one has to lead. `market_max_rmse`
(0.12) sets a badly fitted curve aside instead of letting the floor promote
it: thin Cars.com samples occasionally misfit, such as a C-Class sample full
of AMGs or a GV70 whose age term comes out positive.

What it changed on the drivable board, same cars, same day:

| | inventory only | market leads |
|---|---|---|
| median residual | +2.2% | −0.6% |
| 10th percentile | −3.7% | −5.8% |
| rows below −3% | 12 | 21 |
| best CX-50 Hybrid | −0.6% | −4.8% |

So Hertz's drivable stock is around 3% under the open market for the same
car, which the old benchmark could not show because it compared Hertz with
Hertz. Caveat worth keeping: Cars.com asking prices are negotiable while
Hertz is no-haggle, so part of that gap is bargaining room, not discount.

All 23 curves fitted on 2026-09-18 (n = 24 to 61, RMSE 0.040 to 0.113).
The first pass got 19; the four stragglers came in on a second, targeted
pass (`--curves-only genesis-gv80,mazda-cx_5,...`, added for exactly this).

Two lessons from those four. The CX-5 and GV80 slugs were right all along
and had simply been Cloudflare-blocked, so **a missing curve usually means
blocked, not wrong**; retry before re-guessing. The Mercedes slugs were
genuinely wrong, and the fix was to read Cars.com's own Model filter out of
the `CarsWeb.SearchController.index` payload rather than guess a third time:
it lists both a generic `mercedes_benz-gla_class` and a specific
`mercedes_benz-gla_250`, and the specific one is what Hertz writes.

Watch the GV80: its curve fits at RMSE 0.113, just inside the 0.12 guard,
on the model with the most inventory rows. A model without a usable curve
keeps the internal benchmark and the board says "hertz".

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

## Ownership cost, the price index and the wait-or-buy rule

**Own cost** (page-side, `costOf` in `docs/index.html`, inputs from
`data.json`): landed price, minus expected resale after the chosen years
and miles per year, plus fuel, plus a warranty reserve, plus whatever the
"assumptions" panel holds for maintenance, insurance and the cost of cash
(simple interest on the landed price), undiscounted. Resale: the model's
own market-curve yearly rate where one exists (XC60 −9.9%, CX-70 −7.8% on
2026-09-14) else `[economics] depreciation_per_year` (−8%), read as the
AVERAGE over years 1-3 of a car's life and spread along a front-loaded
shape (`SHAPE` in the page: year 1 = 1.36×, year 2 = 0.88×, year 3 = 0.76×,
flattening to 0.3× by year 12; mean over years 1-3 = 1) starting from the
car's current age, so a two-year-old car is past the cliff; a "flat" tick
applies the rate every year instead. Lorenzo's point, 2026-09-14: first-year
depreciation is much higher than later years, and a flat rate over-charged
the later years. The hedonic's fitted mileage terms (−3.5% per 10k at the
origin, flattening) apply on top. The panel also exposes the depreciation
override, the warranty reserve, and the resale floor (15%). Fuel is the car's EPA combined figure (Hertz rows carry it;
`market.mpg_by_model` supplies medians and a fallback table for CarMax,
Byers and Cars.com rows) at `[economics] fuel_price`. The reserve
(`warranty_reserve`, $1,500) applies when bumper-to-bumper cover is under
12 months or under one year of the chosen mileage; terms by make are in
`market.WARRANTY`. Mileage and years are selectors on the page (persisted
in the browser), because how much Lorenzo will drive is the open question;
the "Best at budget" tab ranks every row on the board by this cost under a
landed-price cap.

**Price index** (`market.price_index`): each car's price is a step function
through `price_history`; for each week the panel holds every car listed
that week at its end-of-week price, residualised against the CURRENT
hedonic; exp(mean residual) is the level relative to today. Overall and for
the primary model, with the primary model's count on the site. Starts
2026-09-09 and accumulates; shown on the About tab.

**Hazard** (`market.hazard`): by days on sale (0-14, 15-30, 31-60, 61+),
the weekly log price change over every car-week (cuts and quiet weeks
alike), P(cut within a week), and P(still listed a week later) from
exposure days and leaving events. Leaving events count only for models
whose last sweep was complete (`coverage:<model>` meta, written by the
collector from `ingest.COVERAGE`), since a car dropping out of a capped
sweep looks exactly like a sale. Rent2Buy rows carry a future availability
date in the inventory field, so their days on sale run from first sight.

**Wait or buy** (`market.advise`): expected cut next week × P(still there)
against surplus over the benchmark × P(gone); "wait" when the first is
larger, "buy" otherwise, "no rush" when the car is not below its benchmark.
Shown on the CX-50 tab per car (the primary model's own hazard) and
elsewhere with the pooled rates.

---

## Inactivation is scoped to (source, model) PAIRS

`collect()` returns the exact pairs it asked for; `store.mark_inactive(seen,
pairs)` builds one `(source = ? AND model = ?)` clause per pair; the
zero-fetch guard compares each pair against `active_count(model, source)`
and drops only the offending pair. `mark_inactive` with no pairs does
nothing, rather than marking every unseen car sold.

The earlier version crossed a set of models with a set of sources. Avis
carries no Palisade, so its query returned zero while Hertz's 120 Palisades
sat in the cross product, unseen that run and therefore due to be marked
sold. The guard caught it correctly every two hours for three days and
pushed "a source was skipped" each time, because the label carried the
changing count. Fixed 2026-09-17 (`aa3f699`), verified on a copy of the live
database and on cloud run 35271973915: no guard warning, no nuisance push,
all 120 Palisades still active, 2,129 vehicles fetched.

---

## Fleet drops

Lorenzo, from having bought at Hertz before: "they start offloading a model,
a bunch of listings go up, then demand catches up; we need to be there
catching that flurry." That event is real and the monitor was blind to it.

**Why the residual cannot see it.** A model-wide price move is absorbed by
that model's own fixed effect. If Hertz dumped three hundred CX-50s at 10%
under their usual level, the CX-50 dummy would shift down with them and
every one would read as average against a benchmark they themselves had
just moved. The "vs model" column is a within-model measure by construction,
so a fleet drop is invisible to it. The detector therefore watches COUNTS,
plus the model's own price level over time.

`pipeline.detect_fleet_drops` records one `model_polls` row per (source,
model) polled, with seen, arrivals, matched, drivable, cheapest and median
price. Arrival counts alone are confounded by cadence, since a lane polled
every 48 hours looks bursty every 48 hours, so the baseline is that pair's
own median over `window_days`.

It fires when a watched model brings at least `min_drivable` (4) cars inside
the radius AND that is at least `multiple` (2.0) times its usual drivable
haul. Both tests are on drivable cars: a wave of ninety Atlases in Texas is
not an opportunity at $2 a mile. Only arrivals that match a watch count, or
a flood of base-trim Sportages would fire every run. A pair needs two prior
polls before it can fire, since the first sight of a pair lists its whole
stock as new.

Tuned by replaying nine days of real arrivals through the detector:

| min_drivable | multiple | alerts / week |
|---|---|---|
| 3 | 2.0 | 5.4 |
| 4 | 2.0 | **2.3 (chosen)** |
| 4 | 3.0 | 1.6 |
| 5 | 2.0 | 0.8 |

At the chosen setting it caught a 24-car Palisade wave (4 drivable, usually
1) and two CX-50 waves (9 and 4 drivable, usually 2). The alert is high
priority and names the drivable count, the usual haul, the cheapest match
and the model's price level against its recent norm.

**One caveat measured and worth keeping.** Over these nine days, cars
arriving in a batch of 20 or more were NOT cheaper than trickle arrivals
(mean residual −0.15% against +0.04%, and a smaller share below −3%), and
model price levels were flat through each wave. So the value of the alert
here is first pick of a bigger choice, not a discount. That may change; the
price-level line in the alert is there to show it when it does.

---

## Condition coverage

Every car within `alert_radius_miles` that sits on a watch and comes from a
seller with a readable history page gets a verdict, not only the cars
heading for an alert. Before this, `enrich()` bought a report solely for
alert candidates and the board read "not checked" on 97% of the drivable
rows: 91 cars, 3 verdicts. Condition is the thing Lorenzo was burned by, so
a price without a verdict is close to useless to him.

Two pieces, in `pipeline.py`:

- `attach_known_conditions(store, scored)` hangs every report and history
  link we already hold onto the scored cars. Costs nothing, and alone it
  fixed rows whose report had been bought days earlier but still displayed
  as unchecked.
- `survey_conditions(session, store, scored, cfg)` buys the rest, cheapest
  against model first then nearest, under `[condition] survey_per_run` (30)
  and `survey_seconds` (360). Reports cache 14 days, so this is a one-off
  backfill of about three runs and then only new arrivals.

`CONDITION_SOURCES` (in `config.py`, so the dashboard need not import the
pipeline) is hertz, avis and the two Byers sites. CarMax hard-blocks its
detail pages; Enterprise runs a JS app and sells only inspected, warrantied
cars, which its rows already say.

Every attempt is recorded in the new `condition_attempts` table, whatever
came back, so a seller that publishes nothing readable is not re-opened
every two hours. `outcome` is one of:

| outcome | board shows |
|---|---|
| `report` | the AutoCheck verdict, clean or flagged |
| `carfax` | "Carfax linked, not read", with a link chip (Avis) |
| `none` | "no history published" |
| never tried | "not checked yet" |

That last distinction matters: "no history published" means we looked, and
it is not the same as an unchecked car. `dashboard._coverage` counts over
the rows actually published, not over every car in the store, because the
base trims the watches exclude are never surveyed and would otherwise hold
the number below 100% forever. The header shows the count.

---

## Markdown alerts

A car that arrives at prediction and is cut week by week until it is
genuinely cheap used to say nothing: arrival alerts fire once, on the day
it appears, and the value gate wants the tier bar. `[[watch]]
markdown_alert = true` closes that gap, and is set on the primary targets
only (the three CX-50 Hybrid watches, the three XC60 watches, the two
CX-70 watches) at Lorenzo's request, 2026-09-17.

A markdown alert needs all of: the watch asking for it, a cut of at least
`[scoring] markdown_min_drop` ($250) this run, the car now below EITHER
`markdown_pct` (-5%) or `markdown_sigma` (-1.5) against its own model, and
the same condition gate as every other alert (clean AutoCheck, or a
franchise certification, never waived).

Either bar, not both, and that matters. The XC60 is scored against a
Cars.com curve whose spread is 6.0% of price, so -1.5 sigma there means -9%
and never fires; the CX-50 Hybrid sits on the within-inventory fit at a
3.2% spread where -1.5 sigma is -4.8%. Measured across the first eight days
of committed price history: 931 cars were cut by $250 or more, 25 of them
were drivable and watched but not primary targets, and the rule fired
exactly once, on a certified XC60 thirteen miles away cut $600 to -5.3%.
About one a week.

---

## New cars: the sticker benchmark, the locator and the Mazda tab

Lorenzo, 2026-09-21: new or certified CX-50 / CX-70 at dealers, NA, turbo
or hybrid, Premium or higher, ideally green or grey, terracotta interior
loved; and a negotiation strategy for the Columbus dealers
(`docs/negotiation.md`).

**A new car is not a used car with five miles on it.** It has a sticker,
and the only number a buyer negotiates is the dealer's discount from it,
before the manufacturer's cash. So a new car (`Listing.stock == "new"`) is
excluded from the hedonic fit and never placed by it or by a used-market
curve; `score_listing` gives it `benchmark = "msrp"` and `residual_pct` =
pre-doc price against MSRP (`Listing.sticker_pct`). The value gate skips
the comps test for that benchmark; `qualifies` passes on the factory
warranty (there is no history to read, and the condition survey and the
detail-page enrichment skip new cars); arrival, value and markdown alerts
all work; the push names colours, sticker and the advertised cash.

**Dealer feeds are read with `Source.kind = "dealer"`.** On Hertz and Avis
the Dealer.com `msrp` field is a pre-doc price (`no_haggle_price`); on a
franchise dealer it is the manufacturer's sticker, on new cars and as the
original sticker on late-model used ones. `ingest.fetch_page` moves it into
`Listing.msrp` for dealer sources and recovers the pre-doc price from the
feed's quoted doc fee (`pricing.documentFee`) instead. Byers's feed also
carries `pricing.SICRule`, the manufacturer cash it advertises, kept as
`Listing.incentive` and shown as "Mfr cash". Delivery on a dealer row is
zero: you drive there. Byers's used rows had been charged the Hertz
per-mile tariff, $165 at ten miles, since the start.

**The hedonic carries a franchise-dealer indicator** (`config.DEALER_SOURCE_NAMES`:
the `kind = "dealer"` sources plus the locator), beside the Rent2Buy one. A
dealer's certified asking price sits a level above Hertz, Avis and
Enterprise for the same car, and on the first cloud run with 160 dealer CPO
rows in the fit the CX-50 level rose and an Enterprise CX-50 went from
-5.7% to -10.3% overnight, which sent an alert. With the term, dealer CPO
rows are judged against dealer-level asks (and the market curve), the
rental channels against their own. CarMax is deliberately not in the set:
its XC60 residuals were calibrated without it.

**Watches can name several sources** (`sources = [...]`, kept alongside the
legacy `source`), require certification (`require_certified`), require a
stock kind (`require_stock = "new" | "used"`), and cap their own alert
radius (`alert_radius_miles`, inside the global one). The four Mazda lanes
(group `mazda`) are: tier A "in your colours" new and certified (exterior
cypress/green/gray/grey/polymetal/machine required, terracotta preferred,
the Turbo Meridian admitted because it is the CX-50 trim that carries
terracotta; arrivals and markdowns alert, but only within 75 miles), and
tier B "other colours" new and certified (board only, alert at -7% under
sticker or -5% vs model).

**Mazda USA's locator** (`hertz/mazdausa.py`, own browser pass like
Enterprise, `pipeline.collect_mazdausa`) covers every dealer within the
alert radius: twenty of them, 1,168 Premium-or-better CX-50 / Hybrid /
CX-70 rows on 2026-09-21, 125 within 75 miles, with interior colour and
in-transit ETA (kept as `inventory_date`, so `available_from` shows it).
Its new-car rows carry the sticker as the price and no dealer price:
`Listing.price_is_sticker` makes `sticker_pct` None, the benchmark reads
"sticker", the tab shows a "sticker only" chip, and no discount or markdown
can ever be claimed for them. A VIN that Byers's own feed holds is left to
Byers (`store.sources_for`), which knows the doc fee, the internet price
and the cash; otherwise the row's source would flip every poll.

**The Cars.com Mazda sweep** (`dream.MAZDA_MODELS`, `build_mazda`,
`docs/mazda_seed.json`, `python -m hertz --seed mazda`) supplies advertised
prices: `stock_type=new_cpo`, the `trims[]` facet so a 24-row page sorted
cheapest first is all wanted trims, gray or green outside, and a second
sweep per model asking for the interior bucket (CX-50 "brown" = terracotta,
CX-70 "beige"/"brown" = Tan Nappa). New rows are ranked against their
sticker, used rows against the model's curve fitted on the used rows only.
`refresh_sweep` in `__main__` is the one cache-seed-merge routine for the
SUV and Mazda sweeps.

**The Mazda tab** (`docs/index.html`, group `mazda` plus `mazda_market`):
Sticker, Price, vs sticker, Mfr cash, Landed, Own cost, vs model, Days,
Condition ("new car" for new rows); chips `new`, `cert`, `terracotta`,
`ETA <date>`, `sticker only`, `trim?`; default "Away" is
`[preferences] dealer_radius_miles` (75), the Columbus area, and the tab's
count is over that radius; clear it for the 300-mile field.

**Facts fixed on 2026-09-21.** Cypress green is offered on the CX-50 2.5 S
and Turbo only, never on the Hybrid (Cars.com: black, blue, gray, red,
white). Terracotta leather is on the Turbo Meridian Edition and the Turbo
Premium Plus; the Hybrid's non-black interiors are red and "Black Leather
with Brown". The CX-70's are Greige (Preferred, Premium) and Tan Nappa
(S Premium, S Premium Plus). Ohio's 2026 doc-fee cap is $398. September
programs (through the 30th): CX-50 $1,000 / $1,500 (Hybrid) / $3,000
(Turbo) customer cash or 0% for 36 months; CX-70 $3,500 or 0%/36; CPO
CX-70 3.9%. Advertised discounts before cash within 300 mi: 6-8% on the
Turbo CX-50, 8% on the CX-70 (Germain West, six miles); Byers 0%.

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
