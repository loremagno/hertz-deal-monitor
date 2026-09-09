"""Alert delivery: ntfy push for urgency, email for the decision.

The split is deliberate. A push tells you within seconds that something
happened; the email carries everything you need to decide, including the
AutoCheck verdict and where the car sits against every other unit.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

import requests

from .config import Config
from .models import Scored

logger = logging.getLogger(__name__)

TIMEOUT = 20


def _money(value) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):,.0f}"


def send_push(cfg: Config, title: str, body: str, url: str = "", priority: str = "high") -> bool:
    if not (cfg.enable_push and cfg.push_configured):
        logger.info("Push not configured; skipping")
        return False

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": "car,moneybag",
    }
    if url:
        headers["Click"] = url

    try:
        response = requests.post(
            f"{cfg.ntfy_server.rstrip('/')}/{cfg.ntfy_topic}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        logger.info("Push sent: %s", title)
        return True
    except Exception as exc:
        logger.error("Push failed: %s", exc)
        return False


def send_email(cfg: Config, subject: str, html: str, text: str = "") -> bool:
    if not (cfg.enable_email and cfg.email_configured):
        logger.info("Email not configured; skipping")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Hertz Deal Monitor", cfg.smtp_user))
    message["To"] = cfg.email_to
    message.set_content(text or "This alert is best viewed as HTML.")
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(cfg.smtp_server, cfg.smtp_port, timeout=TIMEOUT) as server:
            server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(message)
        logger.info("Email sent to %s: %s", cfg.email_to, subject)
        return True
    except Exception as exc:
        logger.error("Email failed: %s", exc)
        return False


def alert_push_text(scored: Scored) -> tuple[str, str]:
    listing = scored.listing
    title = f"{listing.label} - {_money(listing.price)}"
    lines = [
        f"Landed {_money(scored.landed_cost)} incl. {_money(scored.delivery)} delivery",
        f"{listing.odometer:,} mi | {listing.lot} | {listing.geodist:.0f} mi away"
        if listing.odometer and listing.geodist is not None else listing.lot,
    ]
    if scored.residual_pct is not None:
        lines.append(f"{scored.residual_pct:+.1f}% vs predicted")
    return title, "\n".join(lines)


def send_deal_alerts(cfg: Config, alerts: list[Scored], board_html: str) -> int:
    """Push each qualifying car, then one email carrying the full comparison."""
    if not alerts:
        return 0

    for scored in alerts:
        title, body = alert_push_text(scored)
        send_push(cfg, title, body, url=scored.listing.url)

    count = len(alerts)
    lead = alerts[0].listing
    subject = (
        f"Hertz deal: {lead.label} at {_money(lead.price)}"
        if count == 1
        else f"Hertz: {count} deals matched, best {lead.label} at {_money(lead.price)}"
    )
    send_email(cfg, subject, board_html)
    return count


def send_digest(cfg: Config, subject: str, board_html: str) -> None:
    send_email(cfg, subject, board_html)


def send_failure_alert(cfg: Config, error: str) -> None:
    """A broken scraper looks exactly like a quiet market. Say so loudly.

    The previous version of this project returned zero results for months
    without anyone noticing, which is the failure this exists to prevent.
    """
    send_push(
        cfg,
        "Hertz monitor FAILED",
        f"The run did not complete:\n{error}\n\nInventory data is stale until this is fixed.",
        priority="urgent",
    )
    send_email(
        cfg,
        "Hertz monitor failed",
        f"<h2>The monitor run failed</h2><pre>{error}</pre>"
        "<p>No inventory was refreshed. Alerts are paused until this succeeds.</p>",
    )
