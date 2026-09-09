# Hertz deal monitor

Watches Hertz Car Sales inventory for a genuinely good deal on a **2025+ Mazda
CX-50 Hybrid** (and a short opportunistic list of alternatives), scores every
car on landed cost, checks its accident history, and alerts only when both
gates pass.

Home base is **43220** (Upper Arlington, OH). Push alerts are limited to cars
within a drivable radius; anything exceptional further away gets mentioned in
the digest rather than pushed.

---

## How it works

```
per-model nationwide sweep of /all-inventory  ─┐
  tier A hourly · tier B daily, page-capped     ├─► SQLite (+ price history)
distance computed locally from lot postcode   ─┘          │
                                                          ▼
                                          hedonic price model
                                                   │
                                    ┌──────────────┴──────────────┐
                                    ▼                             ▼
                              gate 1: value                 landed cost
                          (residual vs prediction)     (price + delivery +
                                    │                    tax + fees)
                                    ▼
                              gate 2: condition
                            (AutoCheck must be clean)
                                    │
                                    ▼
                        ntfy push  +  rich email  +  board.html
```

### Where the data comes from

hertzcarsales.com runs on Dealer.com, which ships a fully structured vehicle
array in `window.DDC.dataLayer.vehicles` on every search page. The monitor
reads that object rather than parsing HTML, so a markup change does not break
it — the exact failure that killed the previous version of this project.

Plain HTTP does not work: the site sits behind Akamai and a bare request
(even for `robots.txt`) returns **403**. A real browser passes cleanly, so
Playwright is a hard requirement. Requests are throttled to roughly one every
1.5–3 seconds.

Two endpoints exist and the difference is not marginal. `/used-inventory`
shows only the sales lots; **`/all-inventory` also covers the Rent2Buy fleet**,
cars still out on rent. For the CX-50 Hybrid that is 61 vehicles versus 19,
and the Rent2Buy side holds the closest and cheapest cars to Columbus. The
monitor reads `/all-inventory`.

Hertz's nastiest trap: `CX-50` and `CX-50 Hybrid` are *different* model
values, and querying the wrong one returns nothing at all rather than
erroring. A model that matches nothing is logged loudly for that reason.

Distance is computed locally, in `geo.py`. Hertz omits `geodist` entirely
unless the session carries server-side location state a headless run cannot
reliably get, and its `geoRadius` parameter is then **silently ignored** — a
radius query returns the whole national inventory while looking normal.
Lot postcodes plus haversine reproduce Hertz's own figures closely (their
"86 mi" to Cincinnati against a computed 83) and work from any IP.

Including Rent2Buy makes mainstream models enormous — Palisade alone is over
1,100 vehicles. So tier A is polled hourly and in full, while tier B is
polled daily, cheapest-first, and page-capped. Truncation is always logged.

### The two gates

**Gate 1 — value.** A hedonic regression of log price on mileage, age, and
model fixed effects, fitted on the whole sweep. The residual measures whether
a car is cheap *for what it is*, within its own model rather than against the
catalogue. Tier A alerts at −3%, tier B only at −8%. A car also needs enough
same-model comparables (`min_comps`) before its residual is trusted.

The model is fitted on **Hertz price**, not landed cost, deliberately: landed
cost embeds distance, so fitting on it would confound "cheap" with "far away."

**Gate 2 — condition.** Hertz links a free Experian **AutoCheck** report from
every vehicle page. A car with a reported accident, a branded title, or an
odometer discrepancy never reaches an alert. Parsing fails safe: any field
that cannot be read counts as *not* clean, so an unreadable report suppresses
the alert rather than waving a damaged car through.

AutoCheck cannot see unreported damage. A clean report is not a substitute
for a pre-purchase inspection on the car you actually buy.

### Landed cost

```
landed = Hertz price (doc fee already included)
       + delivery
       + 7.5% Franklin County sales tax
       + title & registration
```

Delivery is Hertz's own per-vehicle quote where readable, otherwise
`$145 + $2.00/mile`. That slope was calibrated from two live quotes on
2026-09-08: 86 mi → \$316, and 1,656 mi → \$3,464. **Delivery is not flat**,
and treating it as flat makes distant cars look far better than they are.

---

## Setup

### Local

```bash
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
venv/Scripts/python.exe -m playwright install chromium chromium-headless-shell
```

Secrets come from the environment, never from `config.toml`:

| Variable | What it is |
|---|---|
| `HERTZ_SMTP_USER` | Gmail address used to send |
| `HERTZ_SMTP_PASSWORD` | Gmail **app password**, not the account password |
| `HERTZ_EMAIL_TO` | where alerts go (defaults to `HERTZ_SMTP_USER`) |
| `HERTZ_NTFY_TOPIC` | an unguessable [ntfy.sh](https://ntfy.sh) topic name |

```bash
python -m hertz --dry-run    # score and render, send nothing
python -m hertz              # full run
python -m hertz --digest     # force the digest email
```

### GitHub Actions

`.github/workflows/monitor.yml` runs hourly and commits each snapshot back to
the repo, which is what makes price-drop and days-on-lot detection possible.
Add the four variables above as **repository secrets**.

Committing every run also keeps the repo active, which matters: GitHub
disables scheduled workflows in repositories that go 60 days without activity.

---

## Configuration

Everything tunable lives in `config.toml`: the radius, the tax rate, the
delivery fallback, the comparable-set requirements, and the watchlist itself.

Watchlist entries match **case-insensitively as substrings** of Hertz's model
field, so `"CX-50 Hybrid"` matches only the hybrid while `"CX-50"` would match
both. Tier A is the real target and alerts on a modest discount; tier B is
opportunistic and only fires on an exceptional one.

---

## Failure modes this guards against

- **Silent scraper death.** A run that returns zero vehicles after a healthy
  run is treated as a failure, not an empty market, and raises an urgent
  alert. The previous version returned nothing for months unnoticed.
- **Alert fatigue.** Each VIN alerts once, then only again after a further
  real price cut (`realert_price_drop`).
- **Accident cars.** Gate 2, above.
- **Thin comparables.** A residual computed from too few same-model cars is
  refused rather than trusted.

---

## Layout

```
hertz/
  models.py      data types; normalises Hertz's misleading price field names
  config.py      config.toml + environment secrets
  ingest.py      Playwright, dataLayer extraction, pagination
  autocheck.py   AutoCheck fetch and fail-safe parsing
  store.py       SQLite: listings, price_history, autocheck, alerts, runs
  score.py       landed cost, hedonic model, the two gates
  notify.py      ntfy push + email
  board.py       HTML for the board and the emails
  __main__.py    CLI
legacy/          the previous, non-working version, kept for reference
out/board.html   the current board, rewritten every run
```
