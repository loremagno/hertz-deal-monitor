"""HTML rendering for the board and the alert emails.

One renderer serves both. Styles are inline because email clients discard
stylesheets, and the standalone board adds a small style block on top for the
niceties that email cannot use anyway.
"""
from __future__ import annotations

import html
from datetime import datetime

from jinja2 import Template

from .config import Config
from .models import Scored

ACCENT = "#f7c948"       # Hertz yellow
GOOD = "#1a7f4b"
BAD = "#b3261e"
INK = "#16181d"
MUTED = "#6b7280"
LINE = "#e5e7eb"


def _money(value) -> str:
    if value is None:
        return "&mdash;"
    return f"${float(value):,.0f}"


def _residual_cell(scored: Scored) -> str:
    if scored.residual_pct is None:
        return f'<span style="color:{MUTED}">n/a</span>'
    color = GOOD if scored.residual_pct < 0 else BAD
    return f'<span style="color:{color};font-weight:600">{scored.residual_pct:+.1f}%</span>'


def _condition_cell(scored: Scored) -> str:
    report = scored.autocheck
    if report is None:
        return f'<span style="color:{MUTED}">not checked</span>'
    if report.is_clean:
        label = "clean"
        if report.score is not None:
            label += f" ({report.score})"
        return f'<span style="color:{GOOD};font-weight:600">{label}</span>'
    return (
        f'<span style="color:{BAD};font-weight:600">'
        f'{html.escape("; ".join(report.concerns)[:60])}</span>'
    )


TEMPLATE = Template("""
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            color:{{ ink }};max-width:1100px;margin:0 auto;padding:16px">

  <div style="border-left:5px solid {{ accent }};padding:8px 0 8px 14px;margin-bottom:18px">
    <h1 style="margin:0;font-size:21px">Hertz deal monitor</h1>
    <div style="color:{{ muted }};font-size:13px;margin-top:4px">
      {{ generated }} &middot; {{ fetched }} vehicles scanned within {{ radius }} mi of {{ zip }}
      {%- if not hedonic %} &middot; <span style="color:{{ bad }}">price model not fitted</span>{% endif %}
    </div>
  </div>

  {% if alerts %}
  <h2 style="font-size:16px;margin:18px 0 10px">
    {{ alerts|length }} car{{ '' if alerts|length == 1 else 's' }} cleared both gates
  </h2>
  {% for s in alerts %}
  <div style="border:2px solid {{ good }};border-radius:10px;padding:14px;margin-bottom:14px;background:#f6fbf8">
    <table role="presentation" width="100%" style="border-collapse:collapse">
      <tr>
        {% if s.listing.image_url %}
        <td width="180" valign="top" style="padding-right:14px">
          <img src="{{ s.listing.image_url }}" width="170" style="border-radius:8px;display:block" alt="">
        </td>
        {% endif %}
        <td valign="top">
          <div style="font-size:17px;font-weight:700">{{ s.listing.label }}
            {%- if s.listing.is_rent2buy %}
            <span style="font-size:10px;background:#e8eefc;color:#1c3f94;padding:2px 6px;
                         border-radius:3px;vertical-align:middle;font-weight:700">RENT2BUY</span>
            {%- endif %}
          </div>
          <div style="color:{{ muted }};font-size:13px;margin:3px 0 10px">
            {{ s.listing.lot }} &middot; {{ '%.0f'|format(s.listing.geodist or 0) }} mi away
            &middot; {{ '{:,}'.format(s.listing.odometer or 0) }} mi
            {%- if s.listing.days_on_lot %} &middot; {{ s.listing.days_on_lot }} days on lot{% endif %}
            {%- if s.listing.available_from %} &middot; available {{ s.listing.available_from.strftime('%d %b') }}{% endif %}
          </div>
          {% if s.listing.is_rent2buy %}
          <div style="background:#eef3fd;border-left:3px solid #1c3f94;padding:7px 10px;
                      font-size:12px;margin:0 0 10px;line-height:1.5">
            Still in the rental fleet. Mileage and price are Hertz's estimates and can move,
            the car may be out on rent, and there is no delivery &mdash; you collect it at the
            branch. Landed cost below assumes you drive there.
          </div>
          {% endif %}
          <table role="presentation" style="border-collapse:collapse;font-size:13px">
            <tr><td style="padding:2px 14px 2px 0;color:{{ muted }}">Hertz price</td>
                <td style="font-weight:600">{{ money(s.listing.price) }}</td></tr>
            <tr><td style="padding:2px 14px 2px 0;color:{{ muted }}">Delivery</td>
                <td>{{ money(s.delivery) }}</td></tr>
            <tr><td style="padding:2px 14px 2px 0;color:{{ muted }}">Ohio tax + fees</td>
                <td>{{ money(s.tax + s.fees) }}</td></tr>
            <tr><td style="padding:4px 14px 2px 0;font-weight:700;border-top:1px solid {{ line }}">Landed</td>
                <td style="font-weight:700;font-size:15px;border-top:1px solid {{ line }}">{{ money(s.landed_cost) }}</td></tr>
          </table>
          <ul style="margin:10px 0 0;padding-left:18px;font-size:13px;line-height:1.55">
            {% for r in s.reasons %}<li>{{ r }}</li>{% endfor %}
          </ul>
          {% set bw = battery(s) %}
          {% if bw %}
          <div style="background:#f2f7f3;border-left:3px solid {{ good }};padding:7px 10px;
                      font-size:12px;margin:10px 0 0;line-height:1.5">
            In service {{ bw.in_service.strftime('%b %Y') }} &mdash; Mazda's 8yr/100k high-voltage
            battery cover runs from that date, so roughly
            <strong>{{ '%.1f'|format(bw.years_left) }} years and {{ '{:,}'.format(bw.miles_left) }} miles</strong>
            of traction-battery warranty should remain. Confirm with Mazda for this VIN.
          </div>
          {% endif %}
          <a href="{{ s.listing.url }}"
             style="display:inline-block;margin-top:11px;background:{{ accent }};color:{{ ink }};
                    padding:8px 15px;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px">
            View this car</a>
        </td>
      </tr>
    </table>
  </div>
  {% endfor %}
  {% else %}
  <p style="color:{{ muted }};font-size:14px;margin:14px 0">
    No car cleared both gates this run. The board below is the current field.
  </p>
  {% endif %}

  {% if national %}
  <div style="border:1px dashed {{ muted }};border-radius:8px;padding:12px;margin:16px 0;font-size:13px">
    <strong>Worth knowing, outside your {{ radius }} mi radius:</strong>
    {{ national.listing.label }} at {{ national.listing.lot }}
    ({{ '%.0f'|format(national.listing.geodist or 0) }} mi) lands at
    <strong>{{ money(national.landed_cost) }}</strong> including {{ money(national.delivery) }} delivery
    {%- if best_drivable %}, about {{ money(best_drivable.landed_cost - national.landed_cost) }}
    under the best car you could drive to{% endif %}.
  </div>
  {% endif %}

  <h2 style="font-size:16px;margin:22px 0 8px">Watchlist board</h2>
  <table role="presentation" width="100%" style="border-collapse:collapse;font-size:13px">
    <tr style="background:#f3f4f6;text-align:left">
      <th style="padding:7px 8px">Vehicle</th>
      <th style="padding:7px 8px">Lot</th>
      <th style="padding:7px 8px;text-align:right">Miles</th>
      <th style="padding:7px 8px;text-align:right">Price</th>
      <th style="padding:7px 8px;text-align:right">Landed</th>
      <th style="padding:7px 8px;text-align:right">vs model</th>
      <th style="padding:7px 8px">AutoCheck</th>
      <th style="padding:7px 8px;text-align:right">Days</th>
    </tr>
    {% for s in board %}
    <tr style="border-bottom:1px solid {{ line }};{% if s.tier == 'A' %}background:#fffdf3{% endif %}">
      <td style="padding:7px 8px">
        <a href="{{ s.listing.url }}" style="color:{{ ink }};font-weight:600;text-decoration:none">{{ s.listing.label }}</a>
        {%- if s.tier %} <span style="font-size:10px;background:{{ accent if s.tier == 'A' else line }};
              padding:1px 5px;border-radius:3px;vertical-align:middle">{{ s.tier }}</span>{% endif %}
        {%- if s.listing.is_rent2buy %} <span style="font-size:9px;background:#e8eefc;color:#1c3f94;
              padding:1px 4px;border-radius:3px;vertical-align:middle;font-weight:700">R2B</span>{% endif %}
      </td>
      <td style="padding:7px 8px">{{ s.listing.lot }}
        <span style="color:{{ muted }}">{{ '%.0f'|format(s.listing.geodist or 0) }}mi</span></td>
      <td style="padding:7px 8px;text-align:right">{{ '{:,}'.format(s.listing.odometer or 0) }}</td>
      <td style="padding:7px 8px;text-align:right">{{ money(s.listing.price) }}</td>
      <td style="padding:7px 8px;text-align:right;font-weight:600">{{ money(s.landed_cost) }}</td>
      <td style="padding:7px 8px;text-align:right">{{ residual(s) }}</td>
      <td style="padding:7px 8px">{{ condition(s) }}</td>
      <td style="padding:7px 8px;text-align:right;color:{{ muted }}">{{ s.listing.days_on_lot or '' }}</td>
    </tr>
    {% endfor %}
  </table>

  <p style="color:{{ muted }};font-size:11.5px;margin-top:20px;line-height:1.6">
    Landed cost = Hertz price (doc fee included) + delivery + {{ '%.1f'|format(tax_rate * 100) }}% Ohio tax + fees.
    Delivery is Hertz's own quote where available, otherwise
    ${{ '%.0f'|format(base) }} + ${{ '%.2f'|format(per_mile) }}/mile.
    &ldquo;vs model&rdquo; is the residual from a hedonic fit of log price on mileage, age and model,
    so it measures cheapness within a model, not across the catalogue.
    <strong>R2B</strong> marks Rent2Buy cars, still in the rental fleet: no delivery, collected at the
    branch, with mileage and price quoted as estimates.
    AutoCheck cannot see unreported damage &mdash; a clean report is not a substitute for an inspection.
  </p>
</div>
""")


def render(result, cfg: Config, standalone: bool = False) -> str:
    """Render the board. `standalone` wraps it as a full HTML document."""
    from .score import battery_warranty, rank

    board = rank([s for s in result.watched if s.listing.price])
    body = TEMPLATE.render(
        generated=datetime.now().strftime("%a %d %b %Y, %H:%M"),
        fetched=result.fetched,
        radius=cfg.alert_radius_miles,
        zip=cfg.zip,
        hedonic=result.hedonic_fitted,
        alerts=result.alerts,
        board=board,
        national=result.national_pick,
        best_drivable=result.best_drivable,
        tax_rate=cfg.sales_tax_rate,
        base=cfg.delivery_base,
        per_mile=cfg.delivery_per_mile,
        money=_money,
        residual=_residual_cell,
        condition=_condition_cell,
        battery=battery_warranty,
        ink=INK, muted=MUTED, line=LINE, accent=ACCENT, good=GOOD, bad=BAD,
    )

    if not standalone:
        return body

    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Hertz Deal Board</title>"
        "<style>body{margin:0;background:#fafafa}"
        "@media(max-width:640px){table{font-size:12px}}</style>"
        f"</head><body>{body}</body></html>"
    )
