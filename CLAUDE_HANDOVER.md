# CLAUDE_HANDOVER.md — Car deal monitor

Read this first when resuming. Stable technical context is in `HANDOVER.md`.

## Last Session
- **Date**: 2026-09-08 → 2026-09-09
- **Machine**: LORESLG (user `lorem`)
- **Summary**: Built the monitor from scratch. The pre-existing `src/` had never
  worked (zero rows, Feb 2026 log shows both checkers failing); it is archived at
  `legacy/src/` and was not reused. New package is `hertz/`.

## Current State

Working end to end locally. Actions workflow installed; first green cloud run still pending (browser-install step flaky on the runner image).

**Sources live**: Hertz Car Sales (lot + Rent2Buy), Byers Volvo certified,
Byers Mazda, CarMax (real Chrome), Cars.com (benchmark curves only).

**Live findings at time of writing** (these will go stale fast):
- Hertz has **61 CX-50 Hybrids nationally, all 2025** — zero 2026s, though it has
  739 2026 CX-50 *gas*. The hybrid has not reached the ex-rental channel yet.
- Best drivable candidate: **2025 CX-50 Hybrid Premium Plus, Toledo OH,
  $28,975, 33,446 mi, 115 mi away, landed $31,393, AutoCheck clean.** It is
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

## Pending / Next Steps
- [x] Workflow installed; `gh` credential widened to `workflow` scope.
- [x] Repo private; Pages deleted; cadence every 2 h; only the DB committed.
- [ ] **Lorenzo to choose**: 2-hour refresh on a private repo, or 15-minute
      refresh on a public one (or a VPS). He asked for fast; private forces slow.
- [ ] Rotate the Gmail app password — it was pasted in chat, and is now stored
      in GitHub Secrets where it belongs.
- [ ] Consider switching alert thresholds from percent to σ units (see below).

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
