"""Typed containers passed between extraction, grounding, verification and storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Claim:
    """One extractor's assertion about one field, plus the verbatim text it says supports it."""
    field: str
    value: Any
    quote: str | None
    extractor: str
    kind: str = "QUOTE"           # QUOTE (verbatim text evidence) | LINK (value is a URL present in the page's links)
    alt_values: list[Any] = field(default_factory=list)


@dataclass
class Extraction:
    extractor: str
    claims: dict[str, Claim] = field(default_factory=dict)
    is_scholarship: bool | None = None
    error: str = ""
    raw: Any = None
    prompt_chars: int = 0


@dataclass
class Grounded:
    """A claim that survived grounding: quote located in the page, value consistent with the quote."""
    field: str
    value: Any
    quote: str
    start: int | None
    end: int | None
    match_score: float
    extractor: str
    kind: str = "QUOTE"
    agreed_by: list[str] = field(default_factory=list)     # other extractors that independently produced the same value
    conflicting: dict[str, Any] = field(default_factory=dict)   # extractor -> differing value
    page_id: int | None = None          # set when the evidence comes from a supporting page of the same official domain
    source_url: str | None = None


@dataclass
class Rejected:
    field: str
    proposed_value: Any
    proposed_quote: str | None
    reason: str
    extractor: str
