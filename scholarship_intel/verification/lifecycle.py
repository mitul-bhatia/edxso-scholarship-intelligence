"""Lifecycle status: ACTIVE / EXPIRING_SOON / EXPIRED / REVIEW_REQUIRED / NO_LONGER_VERIFIABLE.

Statuses are decided from evidence (HTTP outcome of the official URL, presence of the scholarship on the page,
grounded dates, discontinuation wording) – never from model opinion.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from .. import config

STATUSES = ["ACTIVE", "EXPIRING_SOON", "EXPIRED", "REVIEW_REQUIRED", "NO_LONGER_VERIFIABLE"]

_DISCONTINUED = re.compile(
    r"(has been (discontinued|withdrawn|cancelled|closed)|(is|are|stands?) (now )?(discontinued|withdrawn|cancelled)|no longer (available|accepting|offered|open|being offered)|"
    r"(applications?|registrations?) (are |is |have been )?(now )?closed|stands closed|closed for (the )?(academic )?(year|session|current)|"
    r"not (accepting|inviting) (any )?(new )?applications?|scheme (has been )?(discontinued|wound up|merged))", re.I)
_SUPERSEDED = re.compile(r"(superseded|replaced by|has been replaced|merged (with|into)|revised (guidelines|scheme) .{0,40}supersede)", re.I)


@dataclass
class Status:
    status: str
    reason: str
    miss_count: int = 0
    evidence_quote: str | None = None


def determine(*, fetch_ok: bool, fetch_status: int, fetch_error: str, gone: bool, transient: bool, needs_ocr: bool,
              name_found: bool, is_scholarship_page: bool, closing: dt.date | None, has_rolling_note: bool,
              text: str, prior_miss_count: int, today: dt.date, name: str, deadline_conflict: str | None = None) -> Status:
    soon = int(config.settings()["verification"]["expiring_soon_days"])
    max_miss = int(config.settings()["verification"]["transient_misses_before_unverifiable"])

    if gone:
        return Status("NO_LONGER_VERIFIABLE", f"official URL returned HTTP {fetch_status} – removed from the official source", 0)
    if not fetch_ok and transient:
        miss = prior_miss_count + 1
        if miss >= max_miss:
            return Status("NO_LONGER_VERIFIABLE", f"official source unreachable on {miss} consecutive runs ({fetch_error or fetch_status})", miss)
        return Status("REVIEW_REQUIRED", f"official source temporarily unreachable ({fetch_error or fetch_status}); miss {miss}/{max_miss}", miss)
    if not fetch_ok:
        return Status("REVIEW_REQUIRED", f"official source could not be read ({fetch_error or fetch_status})", prior_miss_count + 1)
    if needs_ocr:
        return Status("REVIEW_REQUIRED", "official document is a scanned image – text cannot be verified", 0)
    if not name_found or not is_scholarship_page:
        return Status("NO_LONGER_VERIFIABLE", "scholarship is no longer identifiable on the official page (name/content absent)", prior_miss_count + 1)

    head = text[:6000]
    m = _DISCONTINUED.search(head)
    if m:
        ctx = head[max(0, m.start() - 80): m.end() + 80].replace("\n", " ").strip()
        if re.search(r"scholarship|scheme|fellowship|application|programme|program", ctx, re.I):
            expired_like = re.search(r"closed|stands closed", m.group(0), re.I)
            return Status("EXPIRED" if expired_like else "REVIEW_REQUIRED",
                          f"official page states: “{ctx[:200]}”", 0, ctx)
    m = _SUPERSEDED.search(head)
    if m:
        ctx = head[max(0, m.start() - 80): m.end() + 80].replace("\n", " ").strip()
        return Status("REVIEW_REQUIRED", f"page indicates the scheme was superseded/merged: “{ctx[:200]}”", 0, ctx)
    if deadline_conflict:
        return Status("REVIEW_REQUIRED", deadline_conflict, 0)

    if closing is not None:
        if closing < today:
            return Status("EXPIRED", f"closing date {closing.isoformat()} has passed (as of {today.isoformat()})", 0)
        if (closing - today).days <= soon:
            return Status("EXPIRING_SOON", f"closing date {closing.isoformat()} is {(closing - today).days} day(s) away", 0)
        return Status("ACTIVE", f"closing date {closing.isoformat()} is in the future", 0)
    if has_rolling_note:
        return Status("ACTIVE", "official page states applications are open (rolling or currently open; no closing date published)", 0)
    return Status("REVIEW_REQUIRED", "official page does not state an application window – cannot confirm it is open", 0)
