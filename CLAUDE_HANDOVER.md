# CLAUDE_HANDOVER.md — Car deal monitor

Read this first when resuming. Stable technical context is in `HANDOVER.md`.

## Last Session
- **Date**: 2026-09-08 → 2026-09-09
- **Machine**: LORESLG (user `lorem`)
- **Summary**: Built the monitor from scratch. The pre-existing `src/` had never
  worked (zero rows, Feb 2026 log shows both checkers failing); it is archived at
  `legacy/src/` and was not reused. New package is `hertz/`.

## Current State

**Live on GitHub Actions since 2026-09-09 18:00 UTC**, every 2 hours. First
green run (34385973008) collected 546 vehicles from Hertz + Byers, seeded the
baseline, fetched AutoCheck for the one value-gate car, sent one push, one deal
email and one digest, and committed the DB back. **Akamai does not block the
runner IP** — the last untested assumption is now tested.

Cloud-only caveats seen on that run:
- CarMax served a different page variant to the runner: 44 tiles, none parsed
  (22 parse locally). Parser now scans for the title line and logs a sample
  of an unparsed tile; verify on the next scheduled run.
- Cars.com allowed one request from the datacenter IP then Cloudflare-blocked
  the second, so only the XC60 market curve fitted, not CX-70. Curves cache
  24 h when they succeed; coverage from CI will be partial.

**Sources live**: Hertz Car Sales (lot + Rent2Buy), Byers Volvo certified,
Byers Mazda, CarMax (real Chrome), Cars.com (benchmark curves only).

**Live findings at time of writing** (these will go stale fast):
- Hertz has **61 CX-50 Hybrids nationally, all 2025** — zero 2026s, though it has
  739 2026 CX-50 *gas*. The hybrid has not reached the ex-rental channel yet.
- Best drivable candidate: **2025 CX-50 Hybrid Premium Plus, Toledo OH,
  $28,831 (down $144 on 2026-09-09), 33,446 mi, 115 mi away, AutoCheck clean.** It is
  Rent2Buy, so collect in person; price and mileage are Hertz estimates.
- **Two of thirteen drivable CX-50 Hybrids have reported damage**, both scoring
  AutoCheck 95 with clean titles: Woodhaven MI (severe collision, airbag, towed,
  10,294 mi) and Des Plaines IL (towed, impact). The Woodhaven car is the
  lowest-mileage one in the set — exactly the trap Lorenzo has been burned by.
- Nothing clean exists under 20,000 miles in a colour he wants.

### Files Touched
All new this session unless noted.
- `hertz/{models,config,ingest,geo,store,score,autocheck,benchmark,carmax,notify,board,artifact,pipeline,__main__}.py`
- `config.toml` — rewritten; watchlist, sources, preferences, cadence
- `README.md`, `HANDOVER.md`, `deploy/{README.md,monitor-workflow.yml}`
- `.github/workflows/monitor.yml` — installed and tracked
- `legacy/` — the previous non-working version, plus two stray scraped files
  (`legacy/stray/`) that appeared mid-session and are not project files
- `out/board.html`, `out/board_artifact.html`, `out/inventory.json`

### Decisions Made
- **Alert radius 300 mi**, Lorenzo's call, overriding an early (wrong) suggestion
  from me that delivery was flat and distance therefore irrelevant.
- **Mileage cap 35,000 across every watch**; Palisade restricted to 2026 only.
- **XC60: Plus or Ultra only**, no Core.
- Buyer preferences (blue / slate grey, Premium Plus, ideally <20k mi) are
  **soft** — they shape the board and alert text, they do not filter. Hard
  filtering on colour would have hidden the whole board.
- CX-70 gray-exterior/brown-interior watch runs on **Dealer.com sources only**.
  Cars.com cannot serve it: result cards carry no colour and detail pages are
  Cloudflare-blocked.
- Franklin County sales tax is **8.00%** (5.75 state + 1.25 county + 0.50 COTA
  LinkUS, effective April 2025). I had 7.5% initially and was wrong.

### Git Status
- Branch `main`, pushed after every change this session; see `git log`.
- Remote `github.com/LorenzoMagnolfi/hertz-deal-monitor`, **private** (was
  public for a few hours on 2026-09-09; see Incident).
- Repository secrets set via API: `HERTZ_SMTP_USER`, `HERTZ_SMTP_PASSWORD`,
  `HERTZ_EMAIL_TO`, `HERTZ_NTFY_TOPIC`.
- No Dropbox conflicted copies observed.

## Incident, 2026-09-09 (read this)
Enabling GitHub Pages on this repo published the car board at
**www.lorenzomagnolfi.com/hertz-deal-monitor/** — Lorenzo's professional
website — because project Pages inherit the account's custom domain. He was,
correctly, angry. Pages was deleted within minutes; his site's own Pages config
(`LorenzoMagnolfi.github.io`, `master`) was never touched. Root cause: I
enabled a publishing surface without checking where it resolved, on a
personal project that had no business being public-facing at all. Repo is
private again. **Do not enable Pages here. Do not make this repo public
without asking.**

## Session 2026-09-13: new owner, public dashboard, dream tab

**The repo moved to `loremagno/hertz-deal-monitor`** (Lorenzo's second
GitHub account, created for this). `LorenzoMagnolfi` survived the transfer
as a collaborator with **push** rights: code and the schedule work from this
machine, but visibility, Pages and secrets are admin-only and need
`loremagno`. `gh auth switch` flips between the two accounts; the git
credential helper is `gh`, so it follows the active one. Check `gh auth
status` at the start of every session. Secrets survived the transfer.

Why a second account: GitHub serves every project page on an account under
that account's custom domain, so no `LorenzoMagnolfi` repo can ever host a
public page without landing on lorenzomagnolfi.com. `loremagno` has no
Pages and no domain anywhere; its project pages live at
`loremagno.github.io/…` and cannot inherit anything.

**Dashboard**: `docs/index.html` (static, client-side; tabs, sort, filter,
hash deep-links, light/dark) renders `docs/data.json`, which the pipeline
writes every run and the workflow commits. Once Pages is enabled on
`main`/`/docs` the page is self-updating.

**Dream tab** (`hertz/dream.py`): A6 allroad, V90 Cross Country, E-Class
All-Terrain from Cars.com, one fresh browser per model with a 45 s pause,
ranked against a per-model curve (floor 6). Aspirational: no AutoCheck, no
alerts. The E-Class needs `body_style_slugs[]=wagon`; unfiltered, the
cheapest page is all sedans. Refreshed every 12 h, cached in `meta`, and
a partial refresh merges with the cache per model.

**Dream models (2026-09-13, station wagons only)**: A6 allroad, **A4
allroad (300 mi radius, Lorenzo's ask)**, V90 Cross Country, **V60**,
E-Class All-Terrain. V60 and E-Class carry Cars.com's
`body_style_slugs[]=wagon` so sedans never appear; allroad and Cross
Country are wagon-only badges. **Rows are clickable**: the card reader
`CARD_TEXT_LINKS` captures each card's `/vehicledetail/` href (the older
text-only `CARD_TEXT` never did, which is why nothing was clickable), the
tracking query string is stripped, and the page links the vehicle cell.

**Session 2026-09-13, late: list pruning, dismissals, XC60 on Cars.com.**

- *Other Hertz finds*: Highlander dropped (Hertz stocks only the LE); CX-90
  Preferred excluded; Sorento/Telluride, Santa Fe (+Hybrid), and Mercedes
  C-Class/GLA 250/GLB 250/GLE added **from live Hertz model strings** (my
  earlier "zero at Hertz" for Sorento/Santa Fe came from a stale local DB;
  Hertz has 25 Sorento and 1,290 Santa Fe, and 262 Mercedes). A **"Very
  discounted, any trim"** lane admits base trims at −10%, which on the
  hertz-group residual distribution (p5 −5.5%, min −12.5%) is a true
  outlier.
- **`exclude_trims` is now whole-word, not prefix.** Prefix matching on
  `"SE"` excluded `"SEL"`, i.e. every Santa Fe Hertz stocks. Multi-word
  entries match as a phrase. The old `"S "` trailing-space trick is gone.
- **Dismissals** live in the browser's `localStorage`
  (`carwatch.dismissed.v1`), keyed by VIN (or URL for Cars.com rows): they
  survive refreshes and republishes, do not sync between devices, and never
  reach the pipeline, so the price model keeps every car. Verified: hide,
  persist across a real reload, footer count, show-hidden, restore.
- **XC60 on Cars.com** is `dream.SUV_MODELS` (`build_suv`), Plus-only via a
  whole-word "plus" marker, 300 mi, ≤35k mi, ≤$47k, with its own cache,
  seed (`docs/suv_seed.json`) and 12-h refresh, rendered on the SUV tab as
  source "cars.com" with condition "not checked (Cars.com)". First seed: 3
  cars, best a 2025 Plus, 11,534 mi, $34,900, Maumee OH (111 mi).

- **CX-70 on Cars.com is on the SUV tab, not the wagon tab** (Lorenzo's
  correction). `dream.SUV_MODELS` holds both XC60 and CX-70; the page
  derives make/model from the row's own label, so a new SUV model needs no
  page change. The wagon seed was stripped of its CX-70 rows and restamped.
- **The very-discounted lane's bar now gates the BOARD, not just alerts.**
  Its first run put 237 rows on the page, most priced above prediction
  (an Atlas at +11.9%). `dashboard.build` drops a lane's rows unless they
  clear the lane's `threshold_pct`; live after the fix: 12 rows, none above
  −10.2%. Rule: a watch whose whole point is "only if very discounted" must
  never list a car that is not.

- **Every listing link opens in a new tab.** The wagon tab already did; the
  shared `veh` renderer (three tables) and the cleared-panel CTA did not.
- **CX-70 on Cars.com: Premium and Premium Plus, Turbo and Turbo S.**
  `DreamModel.trim_markers=("premium",)` / `trim_reject=("preferred",)`,
  whole-word. Cars.com exposes **no trim facet** (probed), and 4 of 7 rows
  carried only a dealer abbreviation ("PR", "PF") that cannot be resolved,
  so those rows are **kept as `trim_status="unknown"` and flagged `trim?`**
  on the page rather than dropped: dropping them would hide half the
  market, and "PR" is likely Premium. Verified on all seven real titles.

**Tab reorganisation (Lorenzo, 2026-09-13).** Every watch carries a
`group`: `primary` (CX-50 Hybrid), `hertz` (Other Hertz finds: the named
upgrade list daily, plus a curated **Broad Hertz finds** list every 48 h at
2 pages/model), `suv` (XC60 Plus-only 2024–25 from Byers/CarMax/Hertz;
CX-70 3.3 **Premium or better** from Hertz/Byers, brown interior flagged),
and the station-wagon tab fed by `dream.py`. The page buckets rows by
`group` into four tabs. `require_trims` is **any-of**: `["Premium"]` admits
Premium Sport / Premium Plus / S Premium and excludes Preferred; my first
draft `["3.3","Premium"]` would have admitted "3.3 Turbo Preferred".
Verified against the real trim strings.

**"Scan the full Hertz inventory for $28–35k finds" was rejected on the
numbers**: that band is 10,551 cars (440 pages) of Blazers, Rogues and
Tiguans, and `geoRadius` is silently ignored, so it cannot be narrowed
server-side. Hence the curated broad list instead.

**V60 Cross Country is its own model everywhere** (Hertz: 3 in stock, e.g.
a 2025 B5 Plus at $32,249; Cars.com slug `volvo-v60_cross_country`: 11
under $47k). The plain "V60" returned zero on both — the CX-50 / CX-50
Hybrid trap again. Both fixed.

**Brown interiors (Lorenzo, 2026-09-13: "absolute sucker for brown/light
brown, but not super light")**: `score.interior_tier()` classifies the
seller's interior string into `mid` (brown/cognac/caramel/saddle/tan…, the
range he wants), `light` (blond/beige/sand…, lighter than he likes) or
`dark`. **Whole-word matching**: a substring test on "tan" flagged "Titan
Black", which would have put black-interior cars on the shortlist; the
same bug was in `WatchEntry.matches` and is fixed there too. `mid` is a
preference bonus everywhere on the board (chip "brown interior"); `light`
is listed as a miss. Held right now: three Baltimore CX-50 Hybrids in
"Black w Brown" (15–19k mi, one Ingot Blue) and both Byers XC60s in Blond.
Only Hertz (~14% of rows) and Byers report interior colour; Cars.com and
CarMax never do, so dream rows cannot be colour-checked and the page says so.

**CX-70 on Cars.com** is a dream-tab model (300 mi, ≤35k mi, ≤$47k), not a
watch: no colour, no history, but visible and clickable.

**Cars.com detail pages are closed to automation**: a fresh browser landing
directly on a VDP gets Cloudflare's "Just a moment…" challenge. So no
AutoCheck/Carfax and no interior colour can ever come from Cars.com;
the report has to be read by hand on the listing page.

**Dream-car cap: $47,000** (Lorenzo, 2026-09-13), enforced at fetch via
Cars.com's `list_price_max` and again client-side. Of the first uncapped
seed's 26 rows only one was under the cap, so the cap changes what the
query *finds*, not just what the page shows.

**Cars.com from GitHub's runners is one request per session at best**:
across four cloud runs it blocked every request but the first of a fresh
session. So the cloud rarely fills the dream tab from scratch. The tab is
therefore **seeded from this machine** (`docs/dream_seed.json`, residential
IP, three sweeps with 50 s pauses, ~4 min) and adopted by the cloud when its
cache is empty; the seed is rewritten whenever a model answers. To refresh
the seed by hand: run `dream.build(cfg)` locally and commit the file.

**Incident, 2026-09-13 21:56 UTC.** A green run recorded zero CX-50
Hybrids (52 an hour earlier): Hertz served that one page empty while every
other model came through, and the run marked all 52 sold and emptied the
dashboard. Fixed with a per-model zero-fetch guard (keep rows, do not mark
polled, report) and `data/hertz.db` restored from `ef6f158`. Lesson: the
run-level "fetched zero" guard is not enough; guards must be per model.

**Local machine note.** Dropbox sync on this tree makes imports, git and
SQLite crawl for minutes at a time (`site` import alone hit 2 s). When
local commands time out, do not chase locks: let the cloud run do the work
and verify from GitHub's side.

**Seed precedence** (`__main__.py`): a committed `docs/dream_seed.json` is
adopted when the cache has no rows OR the seed is newer, judged by the
seed's own `seeded_at` field, never the file mtime (a fresh checkout resets
every mtime, so an mtime rule re-adopted the seed on every run). Two
earlier rules were wrong in sequence: "adopt when the cache string is
missing" lost to an empty `{"rows": []}` cache; "adopt when the cache has
no rows" lost to a stale 26-row uncapped cache. Both are reproduced in
the commit messages.

**Empty-fetch retry** (`ingest.fetch_model_nationwide`, `expected=`): an
empty page is retried once after 20 s **only when the model had cars in the
database** (the caller passes `store.active_count`). The CX-50 Hybrid came
back 0 twice in eight cloud runs while every other model answered; the
guard held both times, but a retry recovers the common case. The first,
unconditional version fired 17 times in one run on models Hertz does not
stock, recovered nothing, and took the run from 3½ to 9½ minutes.

## Session 2026-09-14: XC60 residuals, Cars.com rewrite, CX-70 colours, Follow

Lorenzo: "something seems off in the price pred model for the XC60, all these
CarMax ones that are not that cheap with massive negative residuals? Also by
default order by residual." Then: "the CX-70 is a bit useless, I WANT ONLY
certain colour/interior combos, how do we get those from Cars.com?" and "I
want an option to FOLLOW a listing so I get alerted on price drops or other
changes."

**XC60 residuals, root cause.** Every XC60 was scored on the Cars.com market
curve (internal comps 11 < min_comps 12). That curve was fitted on 11 comps
with no year filter (median 2021, 42k mi, $32k); its −10%/yr age term
extrapolated a 2025 B5 Plus to $48–50k, above MSRP, so a $36k CarMax car read
−17%. Two more defects underneath: `parse_card` only kept a trim when the
title contained "Hybrid" (every XC60/CX-70 comp sat in the middle tier), and
the card reader only saw 6 of 24 cards because Cars.com renders cards in
shadow DOM. Fixes: `fetch_comps(year_min=)` from the watch; whole-word trim
ladder incl. Volvo Core/Plus/Ultra; comps carry the full title as trim; cache
key `market_curve:<slug>:y<year>`; `score.MARKET_PREFERRED_BELOW = 30` so a
same-model market curve beats a dozen internal rows; default sort on every
listing table is now `residual_pct` ascending.

**Cars.com, the real find.** The results page carries
`<search-provider data-vehicle-array="[...]">`: per listing trim as the
dealer wrote it, VIN, year, price, mileage, `exteriorColor` bucket, seller
name + zip, listingId, cpoIndicator. `benchmark.read_results(page)` reads it
(cards are the fallback). Page size is clamped to 24 regardless of
`page_size`. `interior_color_slugs[]` / `exterior_color_slugs[]` filter
server-side (verified: CX-70 2024+ gray|blue × brown|beige = 10 nationwide);
the array names the exterior bucket but never the interior. Distances come
from the seller zip via `geo.coordinates_for_zip`. Rows now carry `vin`,
`color`, `interior` (the bucket asked for), `certified`; the page keys them by
VIN.

**CX-70 combos.** Cars.com CX-70 sweep: 500 mi (Lorenzo's original radius
for this car), gray|blue exterior, brown|beige interior (Mazda's Tan is filed
under either). Hertz/Byers CX-70 watches now REQUIRE `require_exterior`
(gray/grey/graphite/machine/polymetal/blue, substring) and `require_interior`
(the brown family, whole-word). The SUV note on the page says all this.

**Follow.** `hertz/follow.py`: GitHub issues labelled `follow` are the
followed set (Issues were DISABLED on the repo at the time; the label was
created via API; Lorenzo must flip Settings → General → Features → Issues).
Workflow: `issues: write`, `GITHUB_TOKEN` env. Page: ☆/★ on every row,
Followed tab (server rows + device-only stars), pre-filled new-issue URL,
toasts. State in `meta` (`follow_state:<key>`, `follow_log:<key>`); dry runs
never advance it.

**Local verification (2026-09-13, residential IP).** Filtered curves from
the vehicle array, 24 comps each: XC60 2024+ national (site reports 1,605)
−4.7%/10k mi, −9.7%/yr, +13.1%/trim step, RMSE 0.073, tiers {Core 6, Plus 15,
Ultra 3}; CX-70 2024+ national (428) −2.4%/10k, −13.6%/yr, +16.7%/step, RMSE
0.062. Residuals against them: CarMax XC60 B5 Plus rows −9.2, −7.8, −5.8,
−2.9, −1.5, +0.5% (were −16.6 … −5.5); Byers 2024 Plus Dark Theme −2.5%;
Byers 2024 Core +9.5% (priced like a Plus); Byers 2025 Ultra −3.8%; the two
Hertz CX-70 Preferreds −2.1% and +2.3%.

**Cloud run 34802725706 (forced, 2026-09-14 03:28 UTC, green).** 982
vehicles, 0 new, 0 alerts. Follow: `enabled: true`, 0 issues yet (Lorenzo
enabled Issues during the session). SUV tab: 14 Cars.com rows adopted from
the seed (9 XC60 Plus, 5 CX-70 gray/brown-beige Turbo S Premium Plus, 161 to
998 mi). Wagons: 47 rows; V90 CC and V60 CC re-swept on the runner, A6/A4/
E-Class challenged and kept from the seed. BUT the runner's XC60 curve
request was Cloudflare-blocked (the CX-70 one seconds later was not), so the
live XC60 rows were scored on the internal model (benchmark "hertz", 9
comps: −5.9 … +4.4%). Fixed after the run with a committed curve seed
(`python -m hertz --curves` → `docs/market_curves.json`, adopted when newer,
stale curve kept on a failed refresh), a per-source retry guard (the Hertz
XC60/CX-70 queries retried twice for nothing because CarMax/Byers rows
counted), and "just a moment" recognised as a challenge.

**Cloud run 34804042019 (forced, 2026-09-14 03:52 UTC, green).** "Market
curves: adopted 1 from the committed seed"; XC60 curve cached (n=24), CX-70
cached from the runner's own fetch; no needless retries; follow enabled, 0
followed. Live XC60 rows on benchmark "market", 24 comps: CarMax B5 Plus
−11.3, −9.0, −8.2, −4.2, −1.2, −1.1%; Byers Plus Dark Theme −3.3%. These
differ from the local validation figures by up to 2 points because the two
curves came from two different 24-car "best match" pages: the seed is now
fitted on three pages per model (fresh browser per page) to steady it.

## Session 2026-09-14 (later): hedonics rebuilt, Hertz 2026 sweep

Lorenzo: "i like your reg improvements, implement." and "hertz sweep: 2026
only; a list of brands we curate: Audi, Mazda, Volvo, Mercedes, VW, Honda,
Toyota, Genesis, Lexus, Subaru; thresholds look fine" (Hyundai and Kia added
by me since the Palisade, Sorento and Telluride are already on his list).

**Hedonics** (`score.py`, `trims.py`, `benchmark.py`): pre-doc-fee prices
for every source; model-year dummies instead of linear age; make-aware trim
ladders (0-3, unknown 1.5) shared by the fit, the market curve and the sweep;
numerical-only ridge; exact leave-one-out residuals from the kept hat matrix;
PRESS RMSE; per-model sigma at n >= 20, widened by 1/sqrt(1-h); market curve
gains a certified term; inventory and market predictions blended with
w = n/(n+20) (`[scoring] market_blend_k`); `benchmark` reads "hertz",
"market" or "blend NN% market", `comps` counts both. Capped Hertz sweeps
sort by odometer (`ingest.SORT_ASC`). Validation on the 982-row cloud DB:
LOO log-RMSE 0.040, doc fee $399, years +3.9%/+8.4%, trim +5%/rung; XC60
internal LOO residuals −7.1 … +6.1%. NOTE the curve seed had to be refit
after the ladder change: the old seed (0-2 scale) with new tiers (Plus = 2)
over-predicted the XC60 by ~13% until `--curves` was re-run. Refit seed
(three pages per model, fresh browser each): XC60 n=61, RMSE 0.070, −5.0%/10k,
−9.4%/yr, +4.5%/rung, certified +3.9%; CX-70 n=59, RMSE 0.065, −4.3%/10k,
−7.5%/yr, +8.2%/rung, certified +2.1%. Blended residuals on the cloud DB:
CarMax XC60 B5 Plus −6.5, −4.8, −2.9, +0.4, +2.2, +4.0% (z −1.07 … +0.63);
Byers Plus Dark Theme −4.3%, Core +4.5%, Ultra +4.5%; Hertz CX-70 Preferreds
−4.6% and −2.4%. Blend and market-only views agree within a point.

**Sweep** (`config.toml` "Hertz 2026 sweep", `WatchEntry.sweep/price_min/
price_max/body_styles/exclude_base_trims/board_max_residual_pct`,
`ingest.fetch_make_nationwide` filters, `store.known_models`,
`RunResult.new_models`, one push in `__main__`). Hertz honours
internetPrice/odometer/bodyStyle/year server-side (probe: 1,043 → 876 → 862
→ 849 for 2026 Mazdas). Board bar −6%, alert −8%, 12 pages per make by
mileage, every 48 h. The very-discounted lane now uses
`board_max_residual_pct = -10` instead of a label-prefix hack, and both it
and the sweep carry `min_sigma = -2.0` (board and alert): the local dry run
(2,007 vehicles, 2,013-row fit, LOO log-RMSE 0.036, doc fee $459) put 13
unlabelled-trim Rent2Buy Highlanders at −10% to −15% but only −1.7 sigma
into the very-discounted lane, and a Camry SE at −6.7% / −1.8 sigma into
the sweep; a Tiguan SE at −8.0% / −2.0 sigma survives. Local dry run
otherwise clean: first sweep recorded the baseline, curves cached, board
624 rows.

**Cloud run 34807668912 (forced, 04:53 UTC): monitor green, persist step
FAILED** on a rebase conflict (a 27-second cron run had pushed its snapshot
seconds earlier; binary DB + data.json conflict), so the sweep's snapshot
was lost and the live board stayed on the cron run's output. Fixed with
`git pull --rebase -X theirs` and three attempts in the workflow. The run
also fired a value alert (push + email) for a 2026 Palisade SEL at $37,329
under the new fit: within-Hertz benchmark, Palisade sigma ~0.026, so −8% is
about −3 sigma there; locally the cheapest 2026 SELs sit at −6.3%. Not a
bug, a tight model. Two more fixes from the run: the committed CX-70 curve
was not adopted because local seeds carried Eastern time and the runner
compares UTC (now `hertz/clock.py`, all cross-machine stamps naive UTC;
existing seeds' stamps shifted +4 h; curve cache key bumped to `:v2` for
the 0-3 trim scale), and unlabelled trims get a missing-label dummy (the
Highlander case above).

## Pending / Next Steps
- [x] Issues enabled on `loremagno/hertz-deal-monitor` (Lorenzo, 2026-09-14);
      the run reports `follow.enabled: true`. Nothing followed yet: the first
      ☆ on the board creates the first issue.
- [ ] Refit the market-curve seed every week or two: `python -m hertz --curves`
      locally, commit `docs/market_curves.json`. The board's `benchmark`
      column reading "hertz" for the XC60 means the seed is stale or missing.
- [ ] Cars.com "best match" gives 24 of 102 XC60 Plus within 300 mi and 24
      of 167 V60 CC nationwide; paging needs a fresh browser per page and is
      not done. Fine for a curve, thin for a shopping list.
- [x] **Site is public and live**: `https://loremagno.github.io/hertz-deal-monitor/`
      (Lorenzo did both admin clicks as `loremagno`, 2026-09-13). Four tabs:
      CX-50 Hybrid / Other Hertz finds / SUV watch / Station wagons.
- [ ] The broad-finds list is a first guess at "cars Lorenzo would want";
      many of its 24 models matched nothing at Hertz (A4, A6, Q5, GLC, X3,
      3 Series, CR-V/Camry/Sorento/Sportage Hybrid, Forester, CX-90 PHEV).
      Prune or extend on his say-so. Each costs one page load per 48 h.
- [ ] CX-70 at Hertz/Byers now requires gray/blue outside and a brown-family
      interior on top of Premium-or-better; Hertz's two are Preferred, Byers
      has none, so the lane fires on arrival. Cars.com carries the combos
      (colour facets) within 500 mi.
- [x] Zero-fetch guard verified live: a later run hit `Atlas=0` (throttled
      page) and total listings held at 512 instead of 119 Atlases being
      marked sold.
- [ ] A6 allroad and V60 under $47k returned zero on the capped seed and
      are treated as "skipped" by the empty-model rule. A6: almost certainly
      a true zero (post-2022 A6 allroads under $47k barely exist). V60: see
      the probe result recorded below once run. Consider a per-model
      `allow_empty` flag so a genuinely thin model does not read as a block
      forever.
- [x] Dream rows are clickable: 22/22 links in the cloud-produced data.json
      (run 34790564471, `2a5c875`). Models now: CX-70 (Cars.com, 300 mi),
      A6 allroad, A4 allroad (300 mi), V90 CC, V60, E-Class All-Terrain.
      V60 returns zero even uncapped with the wagon filter: the slug or the
      body-style classification is wrong on Cars.com's side, not a block.
- [x] Brown-interior flag live: cloud data shows 2 `mid` (both Baltimore
      CX-50 Hybrids, "Black w Brown", 15–19k mi) and 1 `light` (Byers XC60,
      Blond). A third brown Baltimore car sold between the local and cloud
      snapshots.
- [ ] **Lorenzo's standing caution**: a standout dream-tab price is more
      likely a bad history than a bargain, and Cars.com will not give us the
      report. Every dream row says so on the page. Do not build a "deal"
      badge for the dream tab.
- [x] Workflow installed; `gh` credential widened to `workflow` scope.
- [x] Repo private; Pages deleted; cadence every 2 h; only the DB committed.
- [x] Review defects fixed (header stats, hard-coded board sentence,
      pagination gap, certified-car gate, poll-window tolerance, source-scoped
      inactivation, phantom CarMax→Hertz query, same-model digest pick,
      dead-run ntfy ping) and residue removed. Verified offline; cloud run
      after these changes: see "Last cloud run" below once recorded.
- [ ] **Public dashboard, safely.** Lorenzo accepts a public board but it must
      not touch his website. Plan: he creates a free GitHub **organization**
      (a separate owner with no custom domain, so its project pages live at
      `<org>.github.io/…` and cannot inherit `www.lorenzomagnolfi.com`); then
      transfer the repo there, make it public, re-set the four secrets, write
      the board to `docs/index.html`, enable Pages on `main`/`/docs`, add
      client-side sort/filter. **Waiting on the org name.** Never enable Pages
      on any repo owned by `LorenzoMagnolfi`.
- [ ] CarMax shipping from the runner: the `KmxStore`/`KmxVisitor_0` cookies did
      not fully pin the store (cloud kept 6 of 44 under the cap vs 44 locally).
      Cloud CarMax shipping figures are not yet trustworthy.
- [ ] Rotate the Gmail app password (pasted in chat; now in GitHub Secrets).
- [ ] Group B improvements proposed and not yet approved: σ thresholds
      (Toledo is −1.7%, t=−0.56 on its own model — an ordinary car), fit the
      CX-50 Hybrid on its own sample, Tier B arrivals off, push priority by
      fit with quiet hours, richer alert text. Board email-attachment on
      change as the private dashboard fallback.

## Last cloud run (after the A+D fixes)
Run 34418038999, 2026-09-09 23:42 UTC, green. 530 vehicles across 12 queries
(phantom carmax:XC60 query gone). Byers certified XC60: 4 (was 2). Five cars
reached the condition gate for the first time (one Byers XC60, three CarMax
XC60s) and were held back by the VALUE gate: "only 11 comparable listings
(need 12)". The XC60 market curve has n=11 Cars.com comps against
min_comps=12, so no XC60 can alert until that is resolved (proposed: a
separate, lower floor for market-curve benchmarks). CarMax: 44 parsed, 5 kept
under the $499 cap (44 locally) -- the store cookie is still not pinning
shipping to Columbus.

## Open Questions
- **Alert thresholds are weak as set.** Tier A fires at −3% against a residual
  SD of ~4%, i.e. under 0.9σ — an ordinary car, not an outlier. Lorenzo has been
  told; he has not yet said whether to move to σ-based thresholds (−2σ ≈ −8%).
- **Refresh cadence vs privacy** is unresolved (see Pending).
- Financing: he will pay ≥$15k cash, no trade-in, so the new-car 0% APR argument
  is much weaker than it first appeared (~$1.5–1.8k on ~$16k financed, versus the
  $4–6k it would be on the full price). He asked about credit unions; that
  conversation was not concluded.

## Session Lessons
- **Verify the endpoint, not just the parse.** The single largest error was
  reading `/used-inventory` and confidently reporting 19 cars when
  `/all-inventory` had 61, including the closest and cheapest. A background
  research pass caught it; I had not.
- **Fail-safe parsing hides its own bugs.** The AutoCheck substring bug made
  every clean car look damaged, which *suppresses* alerts and so produces no
  visible symptom. It surfaced only because I inspected a specific car by hand.
  Any gate that fails closed needs a positive test against a known-good and a
  known-bad case.
- **Sorted-and-capped sweeps are a silent filter.** Cheapest-first plus a page
  cap is a truncation on price, which is a truncation on model year. Anything
  filtered locally after such a sweep can be structurally unable to match.
- Two background runs writing the same `run.log` produced an interleaved line
  that looked like a live bug and was not. Use distinct log paths for
  concurrent runs.
- **Check where a publishing surface resolves before enabling it.** Pages,
  custom domains, public repos: verify the destination URL first, and treat
  anything outward-facing on a personal project as needing an explicit yes.
