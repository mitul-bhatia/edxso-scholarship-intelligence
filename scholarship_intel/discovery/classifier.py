"""Source classification: who is publishing this page, and how much authority does that carry?

Order of evidence (strongest first): aggregator/deny lists -> curated known-provider registry ->
TLD/suffix rules -> provider-name<->domain heuristic (needs page self-identification) -> UNKNOWN.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .. import config
from ..textproc import fold_text
from ..util import host_of, registered_domain

TIER_AUTHORITY = {"T1": 1.00, "T2": 0.95, "T3": 0.85, "T3h": 0.80, "T4": 0.40, "T5": 0.0}
OFFICIAL_TIERS = {"T1", "T2", "T3", "T3h"}


@dataclass
class SourceClass:
    domain: str
    source_type: str          # GOVERNMENT | UNIVERSITY | CORPORATE_CSR | NGO_TRUST | INTERNATIONAL | AGGREGATOR | UNKNOWN
    tier: str
    reason: str
    provider_name: str | None = None

    @property
    def authority(self) -> float:
        return TIER_AUTHORITY.get(self.tier, 0.0)

    @property
    def official(self) -> bool:
        return self.tier in OFFICIAL_TIERS

    def as_dict(self) -> dict:
        return {"domain": self.domain, "source_type": self.source_type, "tier": self.tier,
                "authority": self.authority, "reason": self.reason, "provider_name": self.provider_name}


def _matches(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_denied(url_or_host: str) -> bool:
    host = host_of(url_or_host) if "/" in url_or_host else url_or_host
    return _matches(host, config.domain_config().get("deny_fetch", []))


def classify_host(host: str) -> SourceClass:
    host = host.lower().removeprefix("www.")
    cfg = config.domain_config()
    if _matches(host, cfg.get("aggregators", [])):
        return SourceClass(host, "AGGREGATOR", "T5", "listed aggregator / blog / news / social domain – discovery only")
    for kp in cfg.get("known_providers", []):
        if host == kp["domain"] or host.endswith("." + kp["domain"]):
            return SourceClass(host, kp["type"], kp["tier"], f"curated provider registry: {kp['name']}", kp["name"])
    for suffix, stype, tier, reason in cfg.get("suffix_rules", []):
        if host.endswith(suffix):
            return SourceClass(host, stype, tier, reason)
    return SourceClass(host, "UNKNOWN", "T4", "unrecognised domain – not official until provider-name match proves ownership")


def classify_url(url: str) -> SourceClass:
    return classify_host(host_of(url))


_STOP = {"the", "of", "and", "for", "india", "indian", "limited", "ltd", "pvt", "private", "trust", "foundation", "society",
         "programme", "program", "scholarship", "scholarships", "scheme", "bank", "group", "company", "corporation"}


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def provider_matches_domain(provider: str, host: str) -> bool:
    """True when the provider's name is recognisably embedded in the registered domain label."""
    label = _compact(registered_domain(host).split(".")[0])
    full = _compact(provider)
    if not label or not full:
        return False
    sig = [t for t in re.findall(r"[a-z0-9]+", provider.lower()) if t not in _STOP and len(t) >= 3]
    key = "".join(sig)
    if len(key) >= 4 and (key in label or (len(label) >= 5 and label in key)):
        return True
    if sig and all(t in label for t in sig[:2]):
        return True
    return len(label) >= 5 and fuzz.ratio(label, full) >= 85


_INITIAL_STOP = {"of", "the", "for", "and", "in", "at", "to", "a"}


def domain_owner_matches(host: str, provider: str | None) -> bool:
    """Does the extracted provider plausibly OWN this domain? (stops coaching-site articles on .ac.in from passing as 'university')"""
    if not provider:
        return False
    label = _compact(registered_domain(host).split(".")[0])
    pc = _compact(provider)
    if not label or not pc:
        return False
    if provider_matches_domain(provider, host):
        return True
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", provider) if w.lower() not in _INITIAL_STOP).lower()
    if len(initials) >= 3 and (label == initials or label.startswith(initials)):
        return True
    return len(label) >= 4 and label in pc


def infer_type_from_name(provider: str) -> str:
    hints = config.domain_config().get("name_hints", {})
    p = provider.lower()
    for stype in ("UNIVERSITY", "GOVERNMENT", "NGO_TRUST", "CORPORATE_CSR"):
        if any(re.search(rf"\b{re.escape(h)}\b", p) for h in hints.get(stype, [])):
            # A "<Corporate> Foundation" is still CSR; trusts/endowments are NGO/Trust.
            if stype == "NGO_TRUST" and any(re.search(rf"\b{h}\b", p) for h in hints.get("CORPORATE_CSR", [])):
                return "CORPORATE_CSR"
            return stype
    return "UNKNOWN"


def upgrade_by_provider(sc: SourceClass, provider: str | None, title: str, text: str) -> SourceClass:
    """Refine tier using the extracted provider:
    * T2 (academic TLD) is kept only if the provider owns the domain – otherwise it is a third-party article => T4.
    * T4 -> T3h when the provider name is embedded in the domain AND the page identifies itself as that provider."""
    if sc.tier == "T2" and not domain_owner_matches(sc.domain, provider):
        return SourceClass(sc.domain, "UNKNOWN", "T4",
                           f"academic-style domain, but the provider ({provider or 'not identified'}) does not own it – treated as a third-party page")
    if sc.tier != "T4" or not provider:
        return sc
    if not provider_matches_domain(provider, sc.domain):
        return sc
    pf = fold_text(provider)
    sig = [t for t in re.findall(r"[a-z0-9]+", pf) if t not in _STOP and len(t) >= 3]
    hay = fold_text(title) + " " + fold_text(text[:60_000])
    mentions = sum(hay.count(t) for t in sig[:2]) if sig else 0
    if mentions < 2:
        return sc
    stype = infer_type_from_name(provider)
    if stype == "UNKNOWN":
        stype = "NGO_TRUST" if sc.domain.endswith((".org", ".org.in")) else "CORPORATE_CSR"
    return SourceClass(sc.domain, stype, "T3h",
                       f"provider name '{provider}' is embedded in domain and page self-identifies as provider", provider)
