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

## Pending / Next Steps
- [ ] **Lorenzo, as `loremagno`**: make the repo public and enable Pages
      (Settings → Pages → branch `main`, folder `/docs`). Both returned 404
      to the collaborator token. URL will be
      `https://loremagno.github.io/hertz-deal-monitor/`.
- [ ] Verify the first run after the guard commit shows CX-50 Hybrid back at
      ~52 and the zero-fetch guard did not misfire.
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
