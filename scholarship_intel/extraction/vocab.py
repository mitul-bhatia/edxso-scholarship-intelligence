"""Controlled vocabularies + the keyword patterns that justify each enum value.

An enum value is only accepted when its pattern matches inside the quoted evidence – the LLM can propose
'OBC' but the system verifies the quote actually contains OBC-ish wording.
"""
from __future__ import annotations

import re

CATEGORY_PATTERNS: dict[str, re.Pattern] = {
    "SC": re.compile(r"\b(scheduled castes?|SC)\b|\bS\.?C\.?(?=[\s/,&)]|$)"),
    "ST": re.compile(r"\b(scheduled tribes?|ST|tribal)\b", re.I),
    "OBC": re.compile(r"\b(OBC|other backward (classes|class)|backward classes?|DNT|denotified|nomadic|semi[- ]nomadic)\b", re.I),
    "EWS": re.compile(r"\b(EWS|economically weaker sections?)\b", re.I),
    "MINORITY": re.compile(r"\b(minorit(y|ies)|muslims?|christians?|sikhs?|buddhists?|jains?|parsis?|zoroastrians?)\b", re.I),
    "PWD": re.compile(r"\b(persons? with disabilit(y|ies)|PwD|differently[- ]abled|disabled|divyang\w*|disability|handicapped|visually impaired|hearing impaired)\b", re.I),
    "ORPHAN": re.compile(r"\borphan\w*\b", re.I),
    "DEFENCE_WARD": re.compile(r"\b(ex[- ]?servicemen|armed forces|defence personnel|martyred|shaheed|paramilitary|CAPF|war widows?|wards? of (soldiers|army|defence)|rail(way)? protection force|RPF)\b", re.I),
    "SPORTS": re.compile(r"\b(sports ?(persons?|men|women)?|athletes?|olympic|medal(ist)? )\b", re.I),
    "GENERAL": re.compile(r"\b(general category|unreserved|open category|all categories|any category|irrespective of (caste|category))\b", re.I),
}

LEVEL_PATTERNS: dict[str, re.Pattern] = {
    "SCHOOL_PRE_MATRIC": re.compile(r"\b(pre[- ]matric|class(es)? (i{1,3}|iv|v|vi{1,3}|ix|x|[1-9]|10)\b|standard (ix|x|9|10)|9th|10th)\b", re.I),
    "SCHOOL_POST_MATRIC": re.compile(r"\b(post[- ]matric|class(es)? (xi|xii|11|12)\b|11th|12th|higher secondary|senior secondary|intermediate|plus two|\+2)\b", re.I),
    "UNDERGRADUATE": re.compile(r"\b(under[- ]?graduate|UG\b|bachelor'?s?|b\.? ?tech|b\.? ?e\.?\b|b\.? ?sc|b\.? ?a\.?\b|b\.? ?com|mbbs|bds|b\.? ?pharm|llb|degree (course|programme)|first[- ]year|college and university)", re.I),
    "POSTGRADUATE": re.compile(r"\b(post[- ]?graduate|PG\b|master'?s?|m\.? ?tech|m\.? ?sc|m\.? ?a\.?\b|m\.? ?com|mba|m\.? ?d\.?\b|llm|m\.? ?pharm)", re.I),
    "DOCTORAL": re.compile(r"\b(ph\.? ?d|doctoral|doctorate|research scholars?|JRF|SRF|post[- ]?doc\w*)\b", re.I),
    "DIPLOMA": re.compile(r"\b(diploma|polytechnic|lateral entry)\b", re.I),
    "VOCATIONAL": re.compile(r"\b(ITI|vocational|skill development|certificate course)\b"),
    "PROFESSIONAL": re.compile(r"\b(professional (course|degree|programme)|engineering|medical|law|management|architecture|pharmacy|nursing|CA\b|CMA\b|CS\b)\b", re.I),
}

GENDER_PATTERNS: dict[str, re.Pattern] = {
    "FEMALE": re.compile(r"\b(girls?|female|women|woman|daughters?|single girl child|ladies|lady)\b", re.I),
    "MALE": re.compile(r"\b(boys?|male candidates?|men only|sons?)\b", re.I),
    "TRANSGENDER": re.compile(r"\btransgender\b", re.I),
    "ANY": re.compile(r"\b(irrespective of gender|all genders?|both (boys|male) and (girls|female)|boys and girls|male and female|gender[- ]neutral|open to all genders?)\b", re.I),
}

# A "deadline_note" is only accepted when its quote really states an open/rolling window.
ROLLING_RX = re.compile(r"(rolling basis|round the year|throughout the year|open (all|throughout) the year|no (fixed )?(last|closing) date|"
                        r"applications? (are|is) (open|invited|accepted) (throughout|round|year)|accepted (throughout|year[- ]round)|year[- ]round)", re.I)

# "Applications are now open" – a CURRENT open-window statement without a closing date (weaker than a dated deadline).
OPEN_RX = re.compile(r"(applications?\b[^.\n]{0,70}\b(are|is|have been) (now |currently )?(open|invited)|"
                     r"(now|currently) (open for|accepting|inviting) (applications?|nominations)|open for applications?|"
                     r"(registrations?|applications?) (are |is )?(now )?(live|open))", re.I)
CLOSED_RX = re.compile(r"(applications?\b[^.\n]{0,40}\b(are|is|have been) (now |currently )?closed|currently closed|stands closed|"
                       r"application deadline:?\s*closed|no longer (accepting|open))", re.I)


def states_open_window(quote: str) -> str | None:
    """'rolling' | 'open' | None – what kind of open-window statement does this quote make?"""
    if CLOSED_RX.search(quote):
        return None
    if ROLLING_RX.search(quote):
        return "rolling"
    if OPEN_RX.search(quote):
        return "open"
    return None


# A verbatim quote must actually talk about the field it is filed under (grounding proves the quote EXISTS, this checks it is ABOUT it).
FIELD_TOPIC_RX: dict[str, re.Pattern] = {
    "eligibility_text": re.compile(r"(eligib|criteria|who can apply|must |should |open to|candidates?|applicants?|students? (who|with|from|studying)|"
                                   r"required|qualif|enrolled|resident|citizen|aged?\b|income|marks|%)", re.I),
    "selection_process": re.compile(r"(select|shortlist|short-list|merit|interview|evaluat|assess|screening|aptitude test|panel|rank|scrutin|"
                                    r"jury|committee|nominat|shortlisted|written test)", re.I),
    "renewal_requirements": re.compile(r"(renew|continu|subsequent year|next year of study|retain|extension|each year|annual review|progress)", re.I),
    "documents_required": re.compile(r"(document|certificate|proof|marksheet|mark sheet|aadhaar|aadhar|photograph|passport|transcript|upload|"
                                     r"attested|copy of|bank (account|passbook)|id card|letter|statement)", re.I),
    "benefit_text": re.compile(r"(₹|\brs\b|inr|rupee|lakh|stipend|tuition|fee|allowance|grant|award|scholarship amount|funding|covers?|"
                               r"per (annum|month|year)|£|\$|€|fully funded|living)", re.I),
    "academic_requirements": re.compile(r"(marks|%|percent|cgpa|gpa|grade|rank|percentile|merit|passed|qualif|academic|score|first class|exam)", re.I),
    "institution_requirements": re.compile(r"(institution|college|universit|school|institute|recogni[sz]ed|approved|affiliated|accredited|NIRF|"
                                           r"enrolled|studying|admitted|AICTE|UGC|IIT|IIM|NIT)", re.I),
    "domicile": re.compile(r"(resident|domicile|citizen|national|belong|native|state of|india|passport)", re.I),
    "courses": re.compile(r"(course|degree|programme|program|stream|b\.?tech|m\.?tech|mbbs|diploma|engineering|medical|law|b\.?sc|m\.?sc|mba|ph\.?d|"
                          r"undergraduate|postgraduate|subject|discipline|field of study)", re.I),
}
# First-person narrative (testimonials, "I was selected …") is never a rule of the scheme.
FIRST_PERSON_RX = re.compile(r"\b(I (was|am|have|had|will|would|can|did|got|joined|learned|felt|think|applied)|my|me|myself|we were|our journey)\b")


def topic_ok(field: str, quote: str) -> str | None:
    """None if the quote plausibly belongs to the field; otherwise the reason it does not."""
    rx = FIELD_TOPIC_RX.get(field)
    if rx is None:
        return None
    if FIRST_PERSON_RX.search(quote):
        return "quote is first-person narrative (testimonial), not a rule of the scheme"
    if not rx.search(quote):
        return f"quote does not discuss {field.replace('_', ' ')}"
    return None


INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana", "Himachal Pradesh",
    "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha",
    "Orissa", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Delhi", "Jammu", "Kashmir", "Ladakh", "Puducherry", "Chandigarh", "Andaman", "Lakshadweep", "North East", "North-East", "Northeast",
]

FIELD_ENUMS = {
    "categories": CATEGORY_PATTERNS,
    "education_levels": LEVEL_PATTERNS,
}

# Fields stored verbatim from the source (value == quote).
TEXT_FIELDS = [
    "benefit_text", "eligibility_text", "academic_requirements", "courses", "institution_requirements",
    "documents_required", "selection_process", "renewal_requirements", "deadline_note", "domicile",
]

# Fields whose value is *derived* from a quote and therefore validated against it.
DERIVED_FIELDS = ["amount", "income", "age", "gender", "categories", "education_levels", "opening_date", "closing_date"]

# Everything extractors may claim (name/provider/application_url are identity fields).
ALL_FIELDS = ["name", "provider", "application_url"] + TEXT_FIELDS + DERIVED_FIELDS

# Fields that count for "eligibility support" (needs to be grounded or verified absent)
ELIGIBILITY_FIELDS = ["eligibility_text", "education_levels", "income", "age", "gender", "categories",
                      "domicile", "academic_requirements", "institution_requirements"]

FIELD_LABELS = {
    "name": "Scholarship name", "provider": "Provider", "application_url": "Application URL",
    "benefit_text": "Benefit", "amount": "Amount", "eligibility_text": "Eligibility",
    "academic_requirements": "Academic requirements", "courses": "Course", "education_levels": "Education level",
    "income": "Income criteria", "age": "Age criteria", "gender": "Gender criteria", "categories": "Category criteria",
    "domicile": "Domicile / state", "institution_requirements": "Institution requirements",
    "opening_date": "Opening date", "closing_date": "Closing date", "deadline_note": "Deadline note",
    "documents_required": "Documents required", "selection_process": "Selection process",
    "renewal_requirements": "Renewal requirements",
}


def enum_values_supported_by(quote: str, field: str, values: list[str]) -> list[str]:
    pats = FIELD_ENUMS[field]
    return [v for v in values if v in pats and pats[v].search(quote)]
