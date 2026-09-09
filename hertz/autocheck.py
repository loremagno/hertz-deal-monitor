"""Experian AutoCheck history parsing.

Hertz links a free full AutoCheck report from every vehicle detail page. That
report is the condition gate: a car with a reported accident, a branded
title, or an odometer discrepancy never reaches an alert, no matter how
cheap it is.

Parsing fails safe. Any field we cannot read stays ``None``, and
``AutoCheck.is_clean`` treats ``None`` as "not clean", so a parse failure
suppresses the alert instead of waving a damaged car through.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from .models import AutoCheck, to_int

logger = logging.getLogger(__name__)

# AutoCheck states the accident verdict in one of two unambiguous sentences.
# Match those, in this order, and nothing else.
#
# Substring matching on shorter phrases is actively dangerous here: "Damage
# Reported" occurs inside "No Accidents or Damage Reported", so a naive
# positive check marks every clean car as damaged. Verified against two live
# reports on 2026-09-08, one clean and one with a severe collision.
DAMAGE_REPORTED = re.compile(
    r"Accident or damage event\(s\)\s+have been reported", re.I
)
NO_DAMAGE_REPORTED = re.compile(
    r"There were no accidents or damage events disclosed"
    r"|No Accidents? or Damage Reported",
    re.I,
)

# Corroborating detail, only read once damage is already established.
SEVERITY = re.compile(r"\b(Severe|Moderate|Minor)\b")
DAMAGE_ROW = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s+([A-Za-z ]+?)\s+(Severe|Moderate|Minor)\b"
)
# Only spellings that appear in the event detail, never in the summary table.
# "Airbag Deployed", "Structural Damage" and "Overturned" are column headings
# printed on every report including clean ones, so matching them would put
# "overturned" in the summary of a car that was never overturned. The event
# rows spell it "Air Bag Deployed", with a space, which is the discriminator.
DAMAGE_FLAGS = [
    (re.compile(r"Air Bag Deployed", re.I), "airbag deployed"),
    (re.compile(r"Vehicle Was Towed", re.I), "vehicle towed"),
    (re.compile(r"Damage Reported as Disabling", re.I), "disabling damage"),
    (re.compile(r"Point of Impact[^\n]{0,40}", re.I), "impact recorded"),
]


def _section(text: str, heading: str, window: int = 220) -> str:
    """Return the text just after a heading, for narrow local matching."""
    match = re.search(re.escape(heading), text, re.I)
    if not match:
        return ""
    return text[match.end(): match.end() + window]


def _describe_damage(text: str) -> str:
    """Summarise a damage report in one line, for the alert and the board."""
    parts: list[str] = []

    rows = DAMAGE_ROW.findall(text)
    for damage_date, damage_type, severity in rows[:3]:
        parts.append(f"{severity.lower()} {damage_type.strip().lower()} on {damage_date}")

    flags = [label for pattern, label in DAMAGE_FLAGS if pattern.search(text)]
    if flags:
        parts.append(", ".join(flags))

    if not parts:
        severity = SEVERITY.search(text)
        parts.append(
            f"{severity.group(1).lower()} damage reported" if severity else "damage reported"
        )

    return "; ".join(parts)


def parse_autocheck(text: str, vin: str) -> AutoCheck:
    """Turn the rendered AutoCheck report into a structured record."""
    report = AutoCheck(vin=vin, fetched_at=datetime.now().isoformat(timespec="seconds"))
    report.raw_text = text

    # -- AutoCheck score and its peer range ------------------------------
    peer = re.search(r"range\s+between\s+(\d{2,3})\s+and\s+(\d{2,3})", text, re.I)
    if peer:
        report.peer_low, report.peer_high = int(peer.group(1)), int(peer.group(2))

    score_block = _section(text, "AutoCheck Score", 60)
    numbers = re.findall(r"\b(\d{2,3})\b", score_block)
    if numbers:
        candidate = int(numbers[0])
        if 1 <= candidate <= 100:
            report.score = candidate

    # -- title brand ------------------------------------------------------
    title_block = _section(text, "State Title Brand", 80)
    title_value = next(
        (line.strip() for line in title_block.splitlines() if line.strip()), ""
    )
    if title_value:
        report.title_brand = title_value

    # -- accidents and damage --------------------------------------------
    # Read the whole report, not a window: the verdict sentence lives in the
    # "Accident & Damage" detail section, well below the summary table.
    # Damage is checked first so that a report containing both a positive
    # verdict and boilerplate reassurance resolves as damaged.
    if DAMAGE_REPORTED.search(text):
        report.accidents_reported = True
        report.accident_summary = _describe_damage(text)
    elif NO_DAMAGE_REPORTED.search(text):
        report.accidents_reported = False
        report.accident_summary = "No accidents or damage reported"

    # -- odometer ---------------------------------------------------------
    odo_block = _section(text, "Odometer Check", 400)
    if re.search(r"rollback|tamper|discrepan|odometer\s+brand", odo_block, re.I) and not re.search(
        r"no\s+odometer\s+brands", odo_block, re.I
    ):
        report.odometer_issue = True
    elif re.search(r"no\s+issues?\s+reported|No\s+Issue|Your Vehicle Checks Out", odo_block, re.I):
        report.odometer_issue = False

    last_odo = re.search(r"Last\s+Reported\s+Odometer:?\s*\n?\s*([\d,]+)", text, re.I)
    if last_odo:
        report.last_odometer = to_int(last_odo.group(1))

    # -- recalls ----------------------------------------------------------
    if re.search(r"No\s+Open\s+Recalls", text, re.I):
        report.open_recalls = False
    elif re.search(r"Open\s+Recall[^\n]*\n\s*(\d+|Yes)", text, re.I):
        report.open_recalls = True

    # -- ownership and usage ---------------------------------------------
    owners = re.findall(r"^\s*Owner\s+(\d+)\s*$", text, re.I | re.M)
    if owners:
        report.owners = max(int(o) for o in owners)

    usage = re.search(r"Vehicle\s+Usage\s*\n\s*([A-Za-z ]+)", text, re.I)
    if usage:
        report.usage = usage.group(1).strip()

    in_service = re.search(r"Estimated\s+In\s+Service:?\s*\n?\s*([\d/]{8,10})", text, re.I)
    if in_service:
        report.in_service_date = in_service.group(1)

    return report


def fetch_autocheck(session, autocheck_url: str, vin: str) -> AutoCheck | None:
    """Load and parse the AutoCheck report behind a per-VIN link."""
    if not autocheck_url:
        return None
    try:
        with session.page() as page:
            session.load(page, autocheck_url)
            try:
                page.wait_for_selector("text=/Vehicle History|AutoCheck Score/i", timeout=20000)
            except Exception:
                logger.debug("AutoCheck page for %s rendered without the usual headings", vin)
            text = page.evaluate("() => document.body.innerText || ''")
    except Exception as exc:
        logger.warning("AutoCheck fetch failed for %s: %s", vin, exc)
        return None

    if not text or len(text) < 200:
        logger.warning("AutoCheck report for %s looked empty", vin)
        return None

    report = parse_autocheck(text, vin)
    logger.info(
        "AutoCheck %s: score=%s title=%s accidents=%s clean=%s",
        vin, report.score, report.title_brand or "?", report.accidents_reported, report.is_clean,
    )
    return report
