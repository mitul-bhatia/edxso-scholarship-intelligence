"""Relevance gates applied before extraction: is this ONE programme, and is it open to Indian students?

These are deliberately conservative structural checks (no LLM): they stop listing pages (e.g. a page with one
deadline per country) and foreign-only programmes from becoming records, and they let re-verification retire
records that were created by mistake in an earlier run.
"""
from __future__ import annotations

import re

from ..discovery.classifier import SourceClass
from ..extraction import parsers as P

_INDIA = re.compile(r"\bIndia(n)?\b", re.I)
_ANY_NATIONALITY = re.compile(r"(all nationalities|any nationality|international students|students from (any|all) countr|worldwide|"
                              r"open to (all|international)|developing countries|commonwealth countries)", re.I)
MAX_CLOSING_STATEMENTS = 8


_NEWS_PATH = re.compile(r"/(media[-_ ]?releases?|press[-_ ]?releases?|newsroom|news|blogs?|articles?|stories|story)(/|$)", re.I)


def relevance_gate(url_host: str, source: SourceClass, text: str, url: str = "") -> str | None:
    """Return a human-readable rejection reason, or None if the document may proceed."""
    if url and _NEWS_PATH.search(url.split("?")[0]):
        return "news / press-release page about a programme, not the programme's own page"
    closing = sum(1 for h in P.find_dates(text) if P.classify_date_context(text, h) == "closing")
    if closing >= MAX_CLOSING_STATEMENTS:
        return f"listing page: {closing} separate closing-date statements (e.g. one round per country), not a single programme"
    # Indian providers (government, academic, registry NGOs/CSR arms, .in domains) serve Indian students by definition.
    # The check applies to foreign funders and to domains we could not tie to an Indian provider.
    foreign_or_unproven = source.source_type == "INTERNATIONAL" or source.tier in ("T3h", "T4")
    if url_host.endswith((".in", ".nic.in")) or not foreign_or_unproven:
        return None
    if not _INDIA.search(text) and not _ANY_NATIONALITY.search(text):
        return "no indication that the programme is open to Indian students (India / Indian / all nationalities not mentioned)"
    return None
