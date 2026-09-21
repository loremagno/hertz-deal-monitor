"""Followed listings: one GitHub issue per car you want to keep an eye on.

The board is a static page with no server, so "follow this car" needs a
home that both the page and the scheduled run can reach. A GitHub issue
on the repo is that home. The page pre-fills one (title, listing URL, a
machine-readable key) and opening it is the act of following; closing it
is the act of un-following. Every run then compares each followed car with
what it saw last time and reports the differences, as a comment on the
issue and as a push: price up or down, mileage moved (Rent2Buy cars are
still out on rent), gone from the site, back on the site, AutoCheck
changed. GitHub emails issue comments on its own, so the issue is also the
car's history.

Issues must be enabled on the repo (Settings > General > Features). Until
they are, the page keeps follows in the browser only and says so.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

import requests

from . import clock, notify
from .config import Config
from .store import Store

logger = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 20
LABEL = "follow"
# "key: 5YMX..." lines in the issue body, written by the page.
FIELD = re.compile(r"^\s*(key|url|source|price|miles|title|lot)\s*:\s*(.+?)\s*$", re.M | re.I)
MAX_LOG = 20
# Rent2Buy cars are on rent between polls; a few hundred miles is noise.
MILES_STEP = 300


@dataclass
class Follow:
    key: str
    number: int
    issue_url: str
    title: str
    listing_url: str = ""
    source: str = ""
    since: str = ""
    price_at_follow: int | None = None
    miles_at_follow: int | None = None


def _money(value) -> str:
    return "n/a" if value is None else f"${int(value):,}"


def _int(text) -> int | None:
    if text is None:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def credentials(cfg: Config) -> tuple[str, str] | None:
    """(repo, token) when this run can talk to GitHub, else None.

    On Actions the token is the workflow's own GITHUB_TOKEN and the repo is
    set by the runner. Locally there is normally neither, and the follow
    check simply does not run.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("HERTZ_GITHUB_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg.github_repo or ""
    return (repo, token) if token and repo else None


def issues_enabled(repo: str, token: str) -> bool | None:
    """The issues list endpoint answers [] even when issues are disabled, so
    ask the repo itself."""
    try:
        r = requests.get(f"{API}/repos/{repo}", headers=_headers(token), timeout=TIMEOUT)
    except Exception as exc:
        logger.warning("Follow: GitHub unreachable: %s", exc)
        return None
    if not r.ok:
        logger.warning("Follow: GitHub said %d for %s", r.status_code, repo)
        return None
    return bool(r.json().get("has_issues"))


def _parse_body(body: str) -> dict:
    return {m.group(1).lower(): m.group(2).strip() for m in FIELD.finditer(body or "")}


def fetch_follows(repo: str, token: str) -> list[Follow] | None:
    """Every open issue labelled `follow` (or titled "Follow: ..."), or None
    when the list could not be read."""
    try:
        r = requests.get(f"{API}/repos/{repo}/issues", headers=_headers(token),
                         params={"state": "open", "per_page": 100}, timeout=TIMEOUT)
    except Exception as exc:
        logger.warning("Follow: GitHub unreachable: %s", exc)
        return None
    if not r.ok:
        logger.warning("Follow: issues unreadable on %s (HTTP %d)", repo, r.status_code)
        return None

    follows: list[Follow] = []
    for item in r.json():
        if item.get("pull_request"):
            continue
        labels = {lab.get("name", "") for lab in item.get("labels", [])}
        title = (item.get("title") or "").strip()
        if LABEL not in labels and not title.lower().startswith("follow"):
            continue
        fields = _parse_body(item.get("body") or "")
        key = fields.get("key") or fields.get("url") or ""
        if not key:
            logger.warning("Follow: issue #%s has no key line; ignored", item.get("number"))
            continue
        follows.append(Follow(
            key=key,
            number=int(item["number"]),
            issue_url=item.get("html_url", ""),
            title=re.sub(r"^follow\s*:\s*", "", title, flags=re.I) or fields.get("title", key),
            listing_url=fields.get("url", ""),
            source=fields.get("source", ""),
            since=(item.get("created_at") or "")[:10],
            price_at_follow=_int(fields.get("price")),
            miles_at_follow=_int(fields.get("miles")),
        ))
    return follows


def _comment(repo: str, token: str, number: int, text: str) -> bool:
    try:
        r = requests.post(f"{API}/repos/{repo}/issues/{number}/comments",
                          headers=_headers(token), json={"body": text}, timeout=TIMEOUT)
    except Exception as exc:
        logger.warning("Follow: comment on #%d failed: %s", number, exc)
        return False
    if not r.ok:
        logger.warning("Follow: comment on #%d refused (HTTP %d): %s",
                       number, r.status_code, r.text[:160])
        return False
    return True


def _current(store: Store, key: str, market: dict) -> dict:
    """What the latest run knows about this car.

    Dealer cars (Hertz, Byers, CarMax) are keyed by VIN and live in the
    store with their full history. Cars.com rows are not in the store; they
    are looked up in the cached sweep by VIN, listing URL or the page's
    older CARSCOM-<listing id> key. The sweep keeps a model's last good
    rows, so a row that has vanished from it was dropped by a sweep that
    answered.
    """
    row = store.conn.execute(
        "SELECT year, make, model, trim, price, odometer, active, last_seen, url, source, lot "
        "FROM listings WHERE vin = ?", (key,)).fetchone()
    if row:
        report = store.get_autocheck(key, max_age_days=365)
        return {
            "found": True, "kind": "listing",
            "price": row["price"], "miles": row["odometer"], "listed": bool(row["active"]),
            "url": row["url"], "source": row["source"] or "hertz",
            "title": " ".join(str(x) for x in (row["year"], row["make"], row["model"], row["trim"]) if x),
            "lot": row["lot"], "seen": row["last_seen"],
            "condition": None if report is None else ("clean" if report.is_clean else "flagged"),
        }
    m = market.get(key)
    if m:
        return {
            "found": True, "kind": "market",
            "price": m.get("price"), "miles": m.get("mileage"), "listed": True,
            "url": m.get("url"), "source": "cars.com",
            "title": f"{m.get('year')} {m.get('title')}".strip(),
            "lot": ", ".join(x for x in (m.get("city"), m.get("state")) if x),
            "seen": None, "condition": None,
        }
    return {"found": False, "kind": "unknown", "price": None, "miles": None, "listed": False,
            "url": "", "source": "", "title": "", "lot": "", "seen": None, "condition": None}


def _events(prev: dict | None, cur: dict) -> list[str]:
    """What changed since the last run. Nothing on first sight: that is the baseline."""
    if prev is None:
        return []
    out: list[str] = []
    if not cur["found"]:
        if prev.get("found"):
            out.append("Not in the latest sweep (sold, or dropped out of the search)")
        return out
    if prev.get("listed") and not cur["listed"]:
        out.append("No longer listed (sold or pulled)")
    elif not prev.get("listed") and cur["listed"]:
        out.append("Back on the site")
    p0, p1 = prev.get("price"), cur.get("price")
    if p0 and p1 and p0 != p1:
        d = p1 - p0
        out.append(f"Price {'down' if d < 0 else 'up'} {_money(abs(d))}: "
                   f"{_money(p0)} -> {_money(p1)} ({100.0 * d / p0:+.1f}%)")
    m0, m1 = prev.get("miles"), cur.get("miles")
    if m0 is not None and m1 is not None and abs(m1 - m0) >= MILES_STEP:
        out.append(f"Mileage now {m1:,} (was {m0:,})")
    c0, c1 = prev.get("condition"), cur.get("condition")
    if c1 and c0 != c1:
        out.append(f"AutoCheck now {c1}" + (f" (was {c0})" if c0 else ""))
    return out


def run(cfg: Config, store: Store, dream_doc: dict | None, suv_doc: dict | None,
        dry_run: bool = False, extra_docs: list | None = None) -> dict:
    """Check every followed car, report changes, and describe the set for the page."""
    doc = {"enabled": False, "repo": os.environ.get("GITHUB_REPOSITORY") or cfg.github_repo or "",
           "new_issue_url": "", "reason": "", "rows": []}
    creds = credentials(cfg)
    if not creds:
        doc["reason"] = "this run has no GitHub token"
        if doc["repo"]:
            doc["new_issue_url"] = f"https://github.com/{doc['repo']}/issues/new"
        return doc
    repo, token = creds
    doc["repo"] = repo
    doc["new_issue_url"] = f"https://github.com/{repo}/issues/new"

    enabled = issues_enabled(repo, token)
    if not enabled:
        doc["reason"] = ("Issues are switched off on the repo: enable them under "
                         "Settings > General > Features" if enabled is False
                         else "GitHub did not answer")
        return doc
    follows = fetch_follows(repo, token)
    if follows is None:
        doc["reason"] = "the repo's issues could not be read"
        return doc
    doc["enabled"] = True

    market: dict = {}
    for source_doc in (dream_doc, suv_doc, *(extra_docs or [])):
        for r in (source_doc or {}).get("rows", []) or []:
            url = r.get("url") or ""
            if not url:
                continue
            market[url] = r
            market["CARSCOM-" + url.rstrip("/").split("/")[-1]] = r
            if r.get("vin"):
                market[r["vin"]] = r

    now = clock.now_iso()
    reported = 0
    for f in follows:
        cur = _current(store, f.key, market)
        state_key, log_key = f"follow_state:{f.key}", f"follow_log:{f.key}"
        prev = None
        try:
            prev = json.loads(store.get_meta(state_key) or "null")
        except Exception:
            prev = None
        try:
            log = json.loads(store.get_meta(log_key) or "[]")
        except Exception:
            log = []

        events = _events(prev, cur)
        if prev is None:
            if cur["found"]:
                miles = f", {cur['miles']:,} mi" if cur.get("miles") is not None else ""
                text = (f"Following. Now {_money(cur['price'])}{miles}"
                        f"{' at ' + cur['lot'] if cur.get('lot') else ''}"
                        f"{'' if cur['listed'] else ' (currently not listed)'}.")
            else:
                text = ("Following. The monitor has not seen this car in its latest data yet; "
                        "it will report here when it does.")
            log.append({"at": now, "text": text})
            if not dry_run:
                _comment(repo, token, f.number, text + "\n\n_Reported by the deal monitor._")
        elif events:
            reported += 1
            for e in events:
                log.append({"at": now, "text": e})
            if not dry_run:
                notify.send_push(cfg, f"Followed: {f.title}", "\n".join(events),
                                 url=f.listing_url or cur.get("url") or f.issue_url,
                                 priority="high")
                _comment(repo, token, f.number,
                         "\n".join(f"- {e}" for e in events)
                         + f"\n\n_Reported by the deal monitor, {now}._")
        log = log[-MAX_LOG:]
        if not dry_run:
            # A dry run must not advance the baseline, or the change it saw
            # would never be reported by the real run that follows it.
            store.set_meta(state_key, json.dumps({**cur, "checked_at": now}))
            store.set_meta(log_key, json.dumps(log))

        price_now = cur.get("price")
        doc["rows"].append({
            "key": f.key, "number": f.number, "issue_url": f.issue_url,
            "title": cur.get("title") or f.title,
            "url": f.listing_url or cur.get("url") or "",
            "source": cur.get("source") or f.source,
            "since": f.since,
            "price_at_follow": f.price_at_follow or (prev or {}).get("price") or price_now,
            "price_now": price_now, "miles_now": cur.get("miles"),
            "listed": bool(cur.get("listed")), "found": bool(cur.get("found")),
            "lot": cur.get("lot") or "", "condition": cur.get("condition"),
            "last_change": log[-1]["at"] if log else None,
            "log": log,
        })

    logger.info("Follow: %d followed, %d with changes this run", len(follows), reported)
    return doc
