"""Exa's query-only backstop to the caller's whole-task stakes judgment."""

import re
import unicodedata

# Deliberately broad: ambiguous terms (trust, benefits, policy, safety) buy quality,
# not savings. Background callers use the same classifier as interactive calls.
_PROTECTED = re.compile(
    r"\b(?:"
    r"legal|law|laws|lawyer|attorney|court|lawsuit|litigation|contract|contracts|nda|"
    r"liability|liable|copyright|trademark|patent|tenant|landlord|eviction|immigration|visa|"
    r"divorce|custody|criminal|jurisdiction|statutory|severance|fiduciary|rights|sued|suing|suits?|"
    r"finance|financial|tax|taxes|taxable|income|gains|earnings|ticker|irs|investment|investing|stock|stocks|bond|bonds|"
    r"mortgage|loan|loans|credit|bank|banking|insurance|payroll|wage|wages|salary|"
    r"money|debt|savings|portfolio|currency|crypto|bitcoin|equity|valuation|inflation|"
    r"401k|ira|hsa|fsa|pension|retirement|estate|trust|benefits|accounting|deduction|deductions|"
    r"quarterly|results?|usd|eur|gbp|jpy|cad|aud|"
    r"medical|medicine|health|healthcare|clinical|doctor|diagnosis|symptom|symptoms|"
    r"treatments?|remed(?:y|ies)|adverse|migraine|aspirin|drug|drugs|medication|dosage|dose|disease|cancer|diabetes|vaccine|"
    r"pregnancy|nutrition|supplement|supplements|fitness|exercise|diet|"
    r"asthma|hypertension|depression|anxiety|infection|allerg(?:y|ies)|arthritis|stroke|"
    r"[a-z]*(?:cillin|cycline|mycin|pril|sartan|statin|olol|azole|oxetine)|"
    r"compliance|comply|regulatory|regulation|regulations|regulator|regulators|"
    r"statute|statutes|legislation|legislative|permit|permits|license|licensing|"
    r"official|authoritative|canonical|docs|documentation|government|federal|municipal|agency|primary|original|source|sources|"
    r"news|current|events|election|elections|war|conflict|headline|headlines|"
    r"today|tonight|yesterday|latest|breaking|recent|recently|live|now|urgent|"
    r"price|prices|pricing|deadline|deadlines|updated|updates|outages?|incidents?|escalat(?:e|ion|ing)|status|"
    r"policy|policies|requirement|requirements|risk|risks|safe|safety|"
    r"sec|fda|cdc|nih|osha|hipaa|gdpr|finra|apr|apy|ada|erisa"
    r")\b|\.gov\b|\b(?:last|past|next)\s+(?:\d+(?:[-–]\d+)?\s*)?(?:hours?|hrs?|h|days?|weeks?)\b|"
    r"\b(?:as of|this (?:morning|week|month))\b|\b\d+\s*(?:hours?|hrs?|h)\s+ago\b|"
    r"\b(?:can|should|may|must)\s+(?:i|we)\b|\bside[ -]effects?\b|\b(?:401|403)\s*\([bk]\)|"
    r"\b(?:noncompete|enforceability|interest|pain|fever|blood|cholesterol|a1c|ibuprofen|"
    r"acetaminophen|antibiotics?|contraindications?|interactions?|iso|nist|soc2|tomorrow|"
    r"tratamiento|gripe|pajak|penghasilan)\b|"
    r"\bstate\s+(?:laws?|tax(?:es)?|regulations?|legislation|benefits|elections?)\b|"
    r"\b(?:interest|exchange|mortgage|tax|currency)\s+rates?\b|"
    r"\bcurrency\s+convert(?:er|ing|s|ion|ions)?\b|"
    # Documentation syntax signals intent, not verified ownership or authority.
    # docs/documentation above also cover hosts and paths, including site.example/docs.
    r"\bdoc\.[a-z0-9-]+\.[a-z0-9.-]+\b|/doc(?:[/\s?#]|$)|"
    r"\b(?:api[ -]reference|reference\s+manual|time[ -]critical)\b|"
    r"\b(?:contact\s+(?:details|information|info|verification)|"
    r"(?:e[ -]?mail|phone|telephone)\s+(?:address|number)|"
    r"(?:customer|technical)\s+(?:support|service)|"
    r"support\s+(?:contact|e[ -]?mail|address|phone|number|ticket))\b",
    re.IGNORECASE,
)


def select_search_policy(query: str, stakes: str | None = None) -> tuple[str, str]:
    """Use Lite only on explicit standard hints with no detected protected query signal."""
    if stakes == "high":
        return "auto", "hint_high"
    if stakes != "standard":
        return "auto", "default"
    text = unicodedata.normalize("NFKC", query or "")
    if not text.strip() or not (query or "").isascii() or _PROTECTED.search(text):
        return "auto", "protected_guard"
    return "lite", "hint_standard"
