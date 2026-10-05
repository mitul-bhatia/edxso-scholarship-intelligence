from scholarship_intel.discovery.classifier import classify_host
from scholarship_intel.verification.relevance import relevance_gate


def test_listing_page_with_many_closing_dates_is_rejected():
    rows = "\n".join(f"Country{i} (Example Scholarship) Open for applications until 6 October 2026, at 11:00" for i in range(12))
    assert "listing page" in relevance_gate("example.org", classify_host("example.org"), rows)


def test_single_programme_page_passes():
    text = "Example Scholarship for Indian students. Last date to apply is 31 October 2026."
    assert relevance_gate("example.org", classify_host("example.org"), text) is None


def test_foreign_only_programme_is_rejected():
    text = "Example Fellowship for residents of Ghana and Kenya. Apply by 1 November 2026."
    assert relevance_gate("example.org", classify_host("example.org"), text) is not None


def test_government_indian_domain_passes_without_mentioning_india():
    assert relevance_gate("scholarships.gov.in", classify_host("scholarships.gov.in"), "Post Matric scheme. Last date 31 October 2026.") is None


def test_indian_registry_provider_passes_without_saying_india():
    assert relevance_gate("vidyadhan.org", classify_host("vidyadhan.org"), "Vidyadhan supports students after Class 10. Apply online.") is None


def test_press_release_page_is_rejected():
    r = relevance_gate("reliancefoundation.org", classify_host("reliancefoundation.org"), "Scholarships announced. Last date 18 October 2026.",
                       "https://www.reliancefoundation.org/media/media-release/Reliance_Foundation_Scholarships_2026-27")
    assert r and "press-release" in r
