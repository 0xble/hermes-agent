"""Shared caller instructions for the web search schema and generated SDK."""

WEB_SEARCH_STAKES_GUIDANCE = (
    "Optional. Classify the WHOLE TASK CONTEXT, not only the search words. "
    "Omit stakes or pass high for legal, financial, tax, medical/health, compliance, "
    "official-source work (including authoritative vendor docs/API facts and contact "
    "verification), current events/news, prices, safety, time-critical work (including "
    "support escalation/production outages), non-English or uncertain cases, and "
    "anything costly to get wrong. Use standard only for nonprotected commodity "
    "tutorials, how-tos, recipes, navigation or background where error is genuinely "
    "cheap. standard may use a cheaper search tier, subject to a query-only protected "
    "guard that cannot see surrounding task context. When unsure, omit."
)
