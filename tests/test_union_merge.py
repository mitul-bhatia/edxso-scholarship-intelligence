from scholarship_intel.extraction.models import Grounded
from scholarship_intel.verification.grounding import merge_extractions


def G(v, ex, quote="Reliance invites first year postgraduate students; scored 7.5 in their Undergraduate CGPA"):
    return Grounded("education_levels", v, quote, 0, len(quote), 100.0, ex)


def test_subset_answers_do_not_union_in_the_extra_value():
    final, agree, _ = merge_extractions([("llm", {"education_levels": G(["POSTGRADUATE"], "llm")}),
                                         ("rules", {"education_levels": G(["POSTGRADUATE", "UNDERGRADUATE"], "rules")})])
    assert final["education_levels"].value == ["POSTGRADUATE"]          # the LLM's (agreed, narrower) answer wins


def test_disjoint_answers_justified_by_one_quote_are_unioned():
    q = "Candidate should be an orphan, or a ward of Armed Forces personnel martyred in action"
    final, _, _ = merge_extractions([("llm", {"categories": Grounded("categories", ["ORPHAN"], q, 0, len(q), 100.0, "llm")}),
                                     ("rules", {"categories": Grounded("categories", ["DEFENCE_WARD"], q, 0, len(q), 100.0, "rules")})])
    assert final["categories"].value == ["DEFENCE_WARD", "ORPHAN"]
