"""Standalone deal board, designed for reading on a phone or a laptop.

Kept separate from `board.py`, which renders email. Email clients discard
stylesheets and need table layout with inline styles; this page can use real
CSS, web fonts and both colour themes. They share the data, not the markup.
"""
from __future__ import annotations

import html
from datetime import datetime

from jinja2 import Template

from .config import Config
from .score import battery_warranty, rank

PAGE = Template("""<title>Hertz CX-50 Hybrid Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
  :root{
    --paper:#f7f5f1; --surface:#fffefc; --sunk:#efece6;
    --ink:#14171b; --muted:#6f7178; --line:#ddd8ce;
    --accent:#b8790a; --accent-soft:#f5e7cb;
    --pass:#1c7a4f; --pass-soft:#e4f2ea;
    --flag:#b0392a; --flag-soft:#f8e6e2;
    --shadow:0 1px 2px rgba(20,23,27,.05), 0 8px 24px -16px rgba(20,23,27,.28);
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --paper:#15171a; --surface:#1c1f24; --sunk:#212429;
      --ink:#e9e7e2; --muted:#9a9ca3; --line:#31353c;
      --accent:#e2a33c; --accent-soft:#3a2f1a;
      --pass:#4ec98a; --pass-soft:#172b21;
      --flag:#f0836f; --flag-soft:#2e1c19;
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.7);
    }
  }
  :root[data-theme="dark"]{
    --paper:#15171a; --surface:#1c1f24; --sunk:#212429;
    --ink:#e9e7e2; --muted:#9a9ca3; --line:#31353c;
    --accent:#e2a33c; --accent-soft:#3a2f1a;
    --pass:#4ec98a; --pass-soft:#172b21;
    --flag:#f0836f; --flag-soft:#2e1c19;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -16px rgba(0,0,0,.7);
  }

  *{box-sizing:border-box}
  body{background:var(--paper); color:var(--ink);
       font-family:"IBM Plex Sans",-apple-system,Segoe UI,Roboto,sans-serif;
       line-height:1.55; -webkit-font-smoothing:antialiased}
  .wrap{max-width:1080px; margin:0 auto; padding:34px 22px 60px;
        display:flex; flex-direction:column; gap:30px}
  h1,h2,h3{font-family:Archivo,Segoe UI,sans-serif; margin:0; text-wrap:balance;
           letter-spacing:-.018em; line-height:1.18}
  h1{font-size:clamp(25px,4.2vw,36px); font-weight:700}
  h2{font-size:16px; font-weight:600; letter-spacing:.09em; text-transform:uppercase;
     color:var(--muted); font-size:12.5px}
  .num{font-family:"IBM Plex Mono",ui-monospace,monospace; font-variant-numeric:tabular-nums}

  header{display:flex; flex-direction:column; gap:9px;
         border-bottom:2px solid var(--ink); padding-bottom:16px}
  .eyebrow{font-family:"IBM Plex Mono",monospace; font-size:11px; letter-spacing:.14em;
           text-transform:uppercase; color:var(--accent); font-weight:600}
  .sub{color:var(--muted); font-size:13.5px}
  .sub b{color:var(--ink); font-weight:600}

  section{display:flex; flex-direction:column; gap:12px}

  /* verdict panels ------------------------------------------------------ */
  .panel{background:var(--surface); border:1px solid var(--line); border-radius:11px;
         padding:20px; display:flex; flex-direction:column; gap:14px; box-shadow:var(--shadow)}
  .panel.pass{border-left:5px solid var(--pass)}
  .panel.flag{border-left:5px solid var(--flag)}
  .phead{display:flex; flex-wrap:wrap; align-items:baseline; gap:10px}
  .phead .name{font-family:Archivo,sans-serif; font-weight:600; font-size:19px}
  .where{color:var(--muted); font-size:13px}
  .chip{font-family:"IBM Plex Mono",monospace; font-size:10.5px; font-weight:600;
        letter-spacing:.07em; text-transform:uppercase; padding:3px 8px; border-radius:4px}
  .chip.pass{background:var(--pass-soft); color:var(--pass)}
  .chip.flag{background:var(--flag-soft); color:var(--flag)}
  .chip.r2b{background:var(--accent-soft); color:var(--accent)}

  .split{display:grid; grid-template-columns:minmax(210px,260px) 1fr; gap:24px}
  @media(max-width:660px){ .split{grid-template-columns:1fr} }

  .ledger{display:flex; flex-direction:column; gap:0; font-size:13.5px}
  .ledger div{display:flex; justify-content:space-between; gap:14px; padding:5px 0}
  .ledger div.total{border-top:1px solid var(--line); margin-top:5px; padding-top:9px;
                    font-weight:600; font-size:16px}
  .ledger .k{color:var(--muted)}

  ul.why{margin:0; padding-left:17px; font-size:13.5px; display:flex;
         flex-direction:column; gap:5px}
  .note{font-size:12.5px; padding:9px 12px; border-radius:7px; line-height:1.5}
  .note.pass{background:var(--pass-soft)}
  .note.warn{background:var(--accent-soft)}
  .note.flag{background:var(--flag-soft); color:var(--flag); font-weight:500}

  a.cta{align-self:flex-start; text-decoration:none; font-weight:600; font-size:13px;
        background:var(--ink); color:var(--paper); padding:9px 17px; border-radius:6px}
  a.cta:hover{background:var(--accent); color:#14171b}
  a{color:var(--accent)}
  :focus-visible{outline:2px solid var(--accent); outline-offset:2px}

  /* table --------------------------------------------------------------- */
  .scroll{overflow-x:auto; border:1px solid var(--line); border-radius:11px;
          background:var(--surface)}
  table{border-collapse:collapse; width:100%; font-size:13px; min-width:720px}
  th{background:var(--sunk); text-align:left; font-weight:600; font-size:11px;
     letter-spacing:.06em; text-transform:uppercase; color:var(--muted);
     padding:10px 12px; white-space:nowrap}
  td{padding:10px 12px; border-top:1px solid var(--line); white-space:nowrap}
  td.veh{white-space:normal; min-width:210px}
  .r{text-align:right}
  tr.best td{background:var(--pass-soft)}
  tr.bad td{background:var(--flag-soft)}
  .good{color:var(--pass); font-weight:600}
  .bad{color:var(--flag); font-weight:600}
  .dim{color:var(--muted)}

  footer{font-size:12px; color:var(--muted); line-height:1.65;
         border-top:1px solid var(--line); padding-top:16px;
         display:flex; flex-direction:column; gap:9px}
  footer b{color:var(--ink)}
  @media (prefers-reduced-motion:reduce){*{animation:none!important; transition:none!important}}
</style>

<div class="wrap">
  <header>
    <div class="eyebrow">Hertz Car Sales &middot; watch from {{ zip }}</div>
    <h1>Mazda CX-50 Hybrid deal board</h1>
    <div class="sub">
      {{ generated }} &middot; CX-50 Hybrid: <b>{{ total_model }}</b> nationwide,
      <b>{{ drivable }}</b> inside {{ radius }} mi &middot; watchlist board below shows
      <b>{{ nearby|length }}</b> cars &middot; priced against <b>{{ comps }}</b> listings
      (log-RMSE {{ '%.3f'|format(rmse) }})
    </div>
  </header>

  {% if cleared %}
  <section>
    <h2>Cleared both gates</h2>
    {% for s in cleared %}
    <div class="panel pass">
      <div class="phead">
        <span class="name">{{ s.listing.label }}</span>
        <span class="chip pass">value &amp; condition</span>
        {% if s.listing.is_rent2buy %}<span class="chip r2b">Rent2Buy</span>{% endif %}
      </div>
      <div class="where num">{{ s.listing.lot }} &middot; {{ '%.0f'|format(s.listing.geodist or 0) }} mi
        &middot; {{ '{:,}'.format(s.listing.odometer or 0) }} mi &middot; {{ s.listing.exterior_color }}</div>
      <div class="split">
        <div class="ledger num">
          <div><span class="k">Hertz price</span><span>{{ money(s.listing.price) }}</span></div>
          {% if s.listing.doc_fee %}
          <div><span class="k">&nbsp;&nbsp;incl. doc fee</span><span>{{ money(s.listing.doc_fee) }}</span></div>
          {% endif %}
          <div><span class="k">Delivery</span><span>{{ money(s.delivery) if s.delivery else 'collect' }}</span></div>
          <div><span class="k">Ohio tax</span><span>{{ money(s.tax) }}</span></div>
          <div><span class="k">Title &amp; reg</span><span>{{ money(s.fees) }}</span></div>
          <div class="total"><span>Landed</span><span>{{ money(s.landed_cost) }}</span></div>
        </div>
        <div style="display:flex;flex-direction:column;gap:11px">
          <ul class="why">{% for r in s.reasons %}<li>{{ r }}</li>{% endfor %}</ul>
          {% set bw = battery(s) %}
          {% if bw %}
          <div class="note pass">In service {{ bw.in_service.strftime('%b %Y') }}. Mazda's 8yr/100k
            high-voltage battery cover runs from that date, so roughly
            <b>{{ '%.1f'|format(bw.years_left) }} years and {{ '{:,}'.format(bw.miles_left) }} miles</b>
            of traction-battery warranty should remain. Confirm with Mazda for this VIN.</div>
          {% endif %}
          {% if s.listing.is_rent2buy %}
          <div class="note warn">Still in the rental fleet: mileage and price are Hertz's estimates,
            the car may be out on rent, and there is no delivery &mdash; you collect it at the branch.</div>
          {% endif %}
          <a class="cta" href="{{ s.listing.url }}">Open listing &middot; {{ s.listing.vin[-6:] }}</a>
        </div>
      </div>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if flagged %}
  <section>
    <h2>Held back on condition</h2>
    {% for s in flagged %}
    <div class="panel flag">
      <div class="phead">
        <span class="name">{{ s.listing.label }}</span>
        <span class="chip flag">blocked</span>
      </div>
      <div class="where num">{{ s.listing.lot }} &middot; {{ '%.0f'|format(s.listing.geodist or 0) }} mi
        &middot; {{ '{:,}'.format(s.listing.odometer or 0) }} mi &middot; {{ money(s.listing.price) }}</div>
      {% for c in s.autocheck.concerns %}<div class="note flag">{{ c }}</div>{% endfor %}
      <div class="note warn">AutoCheck rates this car <b>{{ s.autocheck.score }}</b> with a
        <b>clean title</b>. Score and title alone would have passed it. Only the damage record
        stops it.</div>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if taste %}
  <section>
    <h2>Against your preferences</h2>
    <div class="panel">
      <div class="sub" style="font-size:13.5px">
        Wanted: <b>{{ want_colors }}</b>, <b>Premium Plus</b>, ideally under
        <b>{{ '{:,}'.format(odo_ideal) }} mi</b>. Every Hertz CX-50 Hybrid is Premium Plus,
        so colour and mileage are the binding constraints &mdash; and they bind hard:
      </div>
      <div class="scroll">
        <table>
          <thead><tr><th>Colour</th><th class="r">Miles</th><th class="r">Price</th>
            <th class="r">Away</th><th>Lot</th><th>Condition</th></tr></thead>
          <tbody>
          {% for s in taste %}
          <tr class="{{ 'bad' if s.vin in flagged_vins else '' }}">
            <td>{{ s.listing.exterior_color }}</td>
            <td class="r num">{{ '{:,}'.format(s.listing.odometer or 0) }}</td>
            <td class="r num">{{ money(s.listing.price) }}</td>
            <td class="r num">{{ '%.0f'|format(s.listing.geodist or 0) }}</td>
            <td>{{ s.listing.lot }}</td>
            <td>{{ condition(s) }}</td>
          </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="note flag">Both cars under 22,000 miles in a colour you asked for have
        reported damage. The low-mileage Ingot Blue is the severe collision; the 21,146-mile
        Gray was towed. On this inventory, low mileage and a clean history do not currently
        come together in the colour you want.</div>
    </div>
  </section>
  {% endif %}

  {% if carmax %}
  <section>
    <h2>Best CarMax XC60s &mdash; landed, shipping included</h2>
    <div class="scroll">
      <table>
        <thead><tr><th>Vehicle</th><th class="r">Miles</th><th class="r">Price</th>
          <th class="r">Shipping</th><th class="r">Landed</th><th>Store</th></tr></thead>
        <tbody>
        {% for s in carmax %}
        <tr>
          <td class="veh"><a href="{{ s.listing.url }}">{{ s.listing.year }}
            {{ s.listing.model }} {{ s.listing.trim }}</a></td>
          <td class="r num">{{ '{:,}'.format(s.listing.odometer or 0) }}</td>
          <td class="r num">{{ money(s.listing.price) }}</td>
          <td class="r num">{{ money(s.delivery) }}</td>
          <td class="r num" style="font-weight:600">{{ money(s.landed_cost) }}</td>
          <td>{{ s.listing.lot }}</td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    <div class="note warn">CarMax ships nationally for a flat banded fee, so distance
      barely matters &mdash; the shipping column is the whole geographic cost. These are
      not certified and carry no AutoCheck from CarMax, so condition is unverified here;
      CarMax's own 30-day return is the protection.</div>
  </section>
  {% endif %}

  <section>
    <h2>Everything on the watchlist within {{ radius }} mi</h2>
    <div class="scroll">
      <table>
        <thead><tr>
          <th>Vehicle</th><th>Type</th><th>Colour</th><th>Lot</th><th class="r">Away</th>
          <th class="r">Miles</th><th class="r">Price</th><th class="r">Landed</th>
          <th class="r">vs model</th><th>Condition</th>
        </tr></thead>
        <tbody>
        {% for s in nearby %}
        {% set fit = pref(s.listing) %}
        <tr class="{{ 'best' if s.vin in cleared_vins else ('bad' if s.vin in flagged_vins else '') }}">
          <td class="veh"><a href="{{ s.listing.url }}">{{ s.listing.year }} {{ s.listing.make }}
            {{ s.listing.model }}</a>
            {%- if s.tier == 'A' %} <span class="chip pass">A</span>{% endif %}</td>
          <td>{% if s.listing.certified %}<span class="chip pass">certified</span>
              {%- elif s.listing.is_rent2buy %}<span class="chip r2b">Rent2Buy</span>
              {%- else %}<span class="dim">lot</span>{% endif %}</td>
          <td>{{ s.listing.exterior_color or '&mdash;' }}
            {%- if fit[0] >= 2 %} <span class="chip pass">fit</span>{% endif %}</td>
          <td>{{ s.listing.lot }}</td>
          <td class="r num">{{ '%.0f'|format(s.listing.geodist or 0) }}</td>
          <td class="r num">{{ '{:,}'.format(s.listing.odometer or 0) }}</td>
          <td class="r num">{{ money(s.listing.price) }}</td>
          <td class="r num" style="font-weight:600">{{ money(s.landed_cost) }}</td>
          <td class="r num {{ 'good' if (s.residual_pct or 0) < 0 else 'bad' }}">
            {{ '%+.1f'|format(s.residual_pct) if s.residual_pct is not none else '&mdash;' }}%</td>
          <td>{{ condition(s) }}</td>
        </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </section>

  <footer>
    <div><b>Landed cost</b> = Hertz price (doc fee included) + delivery + {{ '%.1f'|format(tax*100) }}%
      Franklin County tax on the pre-doc-fee amount + title and registration. Delivery is Hertz's own
      per-vehicle quote where readable, otherwise ${{ '%.0f'|format(base) }} + ${{ '%.2f'|format(mile) }}/mile;
      it is not flat, and it is zero for Rent2Buy cars you collect yourself.</div>
    <div><b>vs model</b> is the residual from a hedonic fit of log price on mileage, age and model
      fixed effects across every watched Hertz vehicle, so it measures cheapness within a model
      rather than against the catalogue. It is fitted on asking prices, not transaction prices.</div>
    <div><b>Condition</b> is Experian AutoCheck, free on every Hertz listing. A car with reported
      damage, a branded title or an odometer flag never reaches an alert. AutoCheck cannot see
      unreported damage, and Hertz self-insures, so in-house repairs may never appear on any
      history report &mdash; a clean record is not a substitute for a pre-purchase inspection.</div>
    <div>Hertz lists cars that may still be out on rent, with mileage and price quoted as estimates.
      Confirm availability, the VIN and the price by phone before travelling.</div>
  </footer>
</div>
""")


def _money(value) -> str:
    if value is None:
        return "&mdash;"
    return f"${float(value):,.0f}"


def _condition(scored) -> str:
    report = scored.autocheck
    if report is None:
        return '<span class="dim">not checked</span>'
    if report.is_clean:
        label = f"clean &middot; {report.score}" if report.score else "clean"
        return f'<span class="good">{label}</span>'
    return f'<span class="bad">{html.escape(report.concerns[0][:52]) if report.concerns else "flagged"}</span>'


def render(scored_rows, cfg: Config, model_key: str = "cx-50 hybrid",
           comps: int = 0, rmse: float = 0.0, curve=None) -> str:
    """Render the board for one focal model."""
    from .benchmark import market_gap
    from .score import preference_fit

    focal = [s for s in scored_rows if model_key in s.listing.model.lower() and s.listing.price]

    # The board covers every watched car; the panels above it stay on the
    # target model plus anything that actually cleared.
    watched = [s for s in scored_rows if s.tier and s.listing.price]
    nearby = rank([s for s in watched if (s.listing.geodist or 9e9) <= cfg.alert_radius_miles])
    focal_near = [s for s in nearby if model_key in s.listing.model.lower()]

    cleared = [s for s in nearby if s.reasons and s.autocheck and s.autocheck.is_clean]
    flagged = [s for s in watched if s.autocheck is not None and not s.autocheck.is_clean]

    # The five cheapest CarMax cars, landed. Kept in their own section
    # because CarMax prices shipping per car rather than per mile, so they do
    # not belong in a table sorted by distance.
    carmax_rows = rank([s for s in scored_rows
                        if (s.listing.source or "") == "carmax" and s.listing.price])[:5]

    # Cars in a colour asked for, cheapest mileage first.
    taste = sorted(
        (s for s in focal_near if preference_fit(s.listing, cfg)[0] >= 1
         and any(c.strip().lower() in (s.listing.exterior_color or "").lower()
                 for c in cfg.pref_colors)),
        key=lambda s: s.listing.odometer or 0,
    )

    return PAGE.render(
        taste=taste,
        carmax=carmax_rows,
        want_colors=", ".join(c.title() for c in cfg.pref_colors[:3]),
        odo_ideal=cfg.odometer_ideal,
        pref=lambda l: preference_fit(l, cfg),
        market=lambda l: market_gap(l, curve),
        generated=datetime.now().strftime("%A %d %B %Y, %H:%M"),
        zip=cfg.zip,
        radius=cfg.alert_radius_miles,
        total_model=len(focal),
        drivable=len(focal_near),
        comps=comps,
        rmse=rmse,
        cleared=cleared,
        flagged=flagged,
        cleared_vins={s.vin for s in cleared},
        flagged_vins={s.vin for s in flagged},
        nearby=nearby,
        money=_money,
        condition=_condition,
        battery=battery_warranty,
        tax=cfg.sales_tax_rate,
        base=cfg.delivery_base,
        mile=cfg.delivery_per_mile,
    )
