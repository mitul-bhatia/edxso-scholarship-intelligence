from scholarship_intel.util import is_generic_name, looks_like_list_title


def test_faq_questions_and_list_headings_are_not_programme_names():
    for bad in ["Q1. What are the eligibility criteria for the Scholarship?", "Top 10 scholarships in India", "How to apply for NSP?",
                "Scholarships", "About the Award"]:
        assert is_generic_name(bad) or looks_like_list_title(bad), bad


def test_real_programme_names_pass():
    for ok in ["Reliance Foundation Undergraduate Scholarships", "FFE - Info Edge Scholarship Program", "AICTE – SWANATH SCHOLARSHIP SCHEME FOR STUDENTS",
               "Kotak Kanya Scholarship", "Chevening Scholarship"]:
        assert not is_generic_name(ok) and not looks_like_list_title(ok), ok
