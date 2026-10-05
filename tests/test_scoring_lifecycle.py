import datetime as dt

from scholarship_intel.discovery.classifier import classify_host, upgrade_by_provider
from scholarship_intel.extraction.models import Grounded
from scholarship_intel.verification import lifecycle, scoring

TODAY = dt.date(2026, 10, 5)


def G(field, value, extractor="llm:a", score=100.0, agreed=("rules",)):
    return Grounded(field, value, "quote for " + field, 10, 50, score, extractor, "QUOTE", agreed_by=list(agreed))


def good_inputs(host="scholarships.gov.in", **over):
    facts = {f: G(f, "x") for f in ["name", "provider", "benefit_text", "eligibility_text", "selection_process", "documents_required"]}
    facts["closing_date"] = G("closing_date", "2026-12-31")
    facts["education_levels"] = G("education_levels", ["UNDERGRADUATE"])
    facts["amount"] = G("amount", {"min": None, "max": 50000.0, "currency": "INR", "period": "per_year"})
    facts["application_url"] = G("application_url", "https://scholarships.gov.in/apply")
    base = dict(source=classify_host(host), fetch_ok=True, fetch_status=200, needs_ocr=False, name_location="title", page_kind=0.9,
                facts=facts, absent={f: (True, "no hint") for f in ["income", "age", "gender", "categories", "domicile", "academic_requirements",
                                                                   "institution_requirements", "opening_date"]},
                n_extractors=3, agreement={f: 1.0 for f in ["name", "provider", "closing_date", "amount", "education_levels", "application_url"]},
                conflicts=[], apply_check={"status": 200, "trusted": True}, page_text="Academic year 2026-27 apply now", today=TODAY,
                closing_date=dt.date(2026, 12, 31))
    base.update(over)
    return scoring.ScoreInput(**base)


def test_weights_sum_to_100():
    from scholarship_intel import config
    assert sum(config.settings()["weights"].values()) == 100


def test_fully_supported_official_scholarship_is_verified():
    r = scoring.compute(good_inputs())
    assert r.label == "VERIFIED" and r.total >= 95, (r.total, r.caps, r.gates)
    assert {c.name for c in r.components} >= {"source_authority", "deadline_support", "traceability"}


def test_aggregator_can_never_verify():
    r = scoring.compute(good_inputs(host="buddy4study.com"))
    assert r.label == "REVIEW_REQUIRED" and r.total <= 59


def test_single_extractor_cannot_verify():
    r = scoring.compute(good_inputs(n_extractors=1, agreement={}))
    assert r.label == "REVIEW_REQUIRED" and r.total <= 90


def test_missing_deadline_blocks_verification():
    inp = good_inputs()
    inp.facts.pop("closing_date")
    inp.closing_date = None
    r = scoring.compute(inp)
    assert r.label == "REVIEW_REQUIRED" and any("closing date" in g for g in r.gates)


def test_name_missing_or_fetch_failed_caps_score():
    assert scoring.compute(good_inputs(name_location="missing")).total <= 40
    assert scoring.compute(good_inputs(fetch_ok=False, fetch_status=0)).total <= 50


def test_conflict_penalty_and_gate():
    conf = [{"field": "closing_date", "chosen": "a", "other": "b", "chosen_by": "x", "other_by": "y"}]
    r = scoring.compute(good_inputs(conflicts=conf))
    assert r.label == "REVIEW_REQUIRED" and r.penalty == 3.0


def test_expired_deadline_lowers_freshness():
    r = scoring.compute(good_inputs(closing_date=dt.date(2026, 1, 1)))
    fresh = next(c for c in r.components if c.name == "freshness")
    assert fresh.score <= 0.3 and r.label == "REVIEW_REQUIRED"


def test_provider_name_domain_heuristic():
    sc = classify_host("examplefoundation.org")
    assert sc.tier == "T4"
    up = upgrade_by_provider(sc, "Example Foundation", "Example Foundation Scholarship", "Example Foundation offers... Example Foundation team")
    assert up.tier == "T3h" and up.official
    assert upgrade_by_provider(sc, "Some Other Trust", "t", "text").tier == "T4"


def _st(**kw):
    d = dict(fetch_ok=True, fetch_status=200, fetch_error="", gone=False, transient=False, needs_ocr=False, name_found=True,
             is_scholarship_page=True, closing=None, has_rolling_note=False, text="Scholarship page", prior_miss_count=0, today=TODAY, name="X")
    d.update(kw)
    return lifecycle.determine(**d)


def test_lifecycle_states():
    assert _st(closing=dt.date(2026, 9, 1)).status == "EXPIRED"
    assert _st(closing=dt.date(2026, 10, 12)).status == "EXPIRING_SOON"
    assert _st(closing=dt.date(2026, 12, 1)).status == "ACTIVE"
    assert _st().status == "REVIEW_REQUIRED"                                   # no window stated
    assert _st(has_rolling_note=True).status == "ACTIVE"
    assert _st(fetch_ok=False, fetch_status=404, gone=True).status == "NO_LONGER_VERIFIABLE"
    assert _st(fetch_ok=False, fetch_status=503, transient=True).status == "REVIEW_REQUIRED"
    assert _st(fetch_ok=False, fetch_status=503, transient=True, prior_miss_count=2).status == "NO_LONGER_VERIFIABLE"
    assert _st(name_found=False).status == "NO_LONGER_VERIFIABLE"
    assert _st(text="Applications are closed for this scheme.", closing=dt.date(2027, 1, 1)).status == "EXPIRED"
    assert _st(text="This scholarship scheme has been discontinued with effect from 2025.").status == "REVIEW_REQUIRED"
