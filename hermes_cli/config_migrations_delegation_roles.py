"""The fork's decided role policy, expressed through the official plugin's config."""
from __future__ import annotations

from hermes_yaml import roundtrip_yaml

# Disabled routes are comments, so they cannot become model-facing choices.
ROLE_SETTINGS = """\
default: worker
options:
  - name: worker
    description: "Perform mechanically specified edits, extraction/classification from supplied inputs, existing-tool runs and routine summaries with objective checks. Not discovering or reconciling source facts (explorer), novel code (coder), or recommendations/critique (planner); urgency, importance and volume alone do not escalate."
    provider: custom:openai-proxy
    model: gpt-6-luna
    reasoning_effort: low
    service_tier: normal
    fallback_providers:
      - provider: custom:openai-proxy
        model: gpt-6.1-terra
        reasoning_effort: low
  - name: explorer
    description: "Locate, verify and extract facts from files, official sources, pages, photos or screenshots, returning source locations and gaps. Not recommendations or non-code critique (planner), creating pictures (visual), or interface design (designer)."
    provider: custom:openai-proxy
    model: gpt-6.1-terra
    reasoning_effort: medium
    service_tier: normal
    fallback_providers:
      - provider: custom:openai-proxy
        model: gpt-6.1-sol
        reasoning_effort: medium
  - name: coder
    description: "Implement and debug substantive code with tests, including routine multi-file changes, specified UI and standard charts where the design is already settled. Not interface design judgment (designer), demonstrated hard failures or unresolved irreversible execution risk (critical), or code review (AutoReview)."
    provider: custom:openai-proxy
    model: gpt-6.1-sol
    reasoning_effort: high
    service_tier: normal
    fallback_providers:
      - provider: custom:claude-proxy
        model: claude-sonnet-5-5
        reasoning_effort: high
      - provider: custom:openai-proxy
        model: muse-wonder-full-1.0
        reasoning_effort: high
      - provider: xai-oauth
        model: grok-4.7
        reasoning_effort: high
  # Claude routes remain disabled until the proxy protects Brian's 25% reserve.
  # - name: critical
  #   description: "Solve unusually hard engineering problems or implement high-consequence changes with unresolved risk to money, production data or security; a prior capable failure is evidence, not a prerequisite. Not routine implementation (coder), read-only synthesis or plan critique (planner), routine summaries (worker), or urgency alone."
  #   provider: custom:claude-proxy
  #   model: claude-fable-5-2
  #   reasoning_effort: high
  #   service_tier: normal
  #   fallback_providers:
  #     - provider: custom:openai-proxy
  #       model: gpt-6.1-sol
  #       reasoning_effort: high
  # - name: planner
  #   description: "Synthesize evidence, compare options, recommend decisions, define technical architecture or acceptance contracts, and create or critique non-code plans and non-code audits. Not implementation (coder/critical), source-only fact finding (explorer), drawing decided architecture (visual), voice polishing (writer), or code review (AutoReview)."
  #   provider: custom:claude-proxy
  #   model: claude-opus-5-5
  #   reasoning_effort: high
  #   service_tier: normal
  #   fallback_providers:
  #     - provider: custom:openai-proxy
  #       model: gpt-6.1-sol
  #       reasoning_effort: high
  # - name: designer
  #   description: "Design or repair interfaces people use: screen hierarchy, app flows, responsive layouts, interaction, visual polish and screenshot-driven UI fidelity, including dashboards with charts. Not implementing an already-specified UI or standard chart (coder), inspecting a page for facts (explorer), or standalone pictures and diagrams (visual)."
  #   provider: custom:claude-proxy
  #   model: claude-opus-5-5
  #   reasoning_effort: high
  #   service_tier: normal
  #   fallback_providers:
  #     - provider: custom:openai-proxy
  #       model: gpt-6.1-sol
  #       reasoning_effort: high
  # - name: visual
  #   description: "Create or repair pictures rather than interfaces: 3D scenes and geometry, illustrations, and designed standalone diagrams or explanatory data visualizations, including diagrams of an already-decided architecture. Not app/dashboard interfaces (designer), standard chart implementation (coder), architecture decisions (planner), or extracting image facts (explorer)."
  #   provider: custom:claude-proxy
  #   model: claude-fable-5-2
  #   reasoning_effort: high
  #   service_tier: normal
  #   fallback_providers:
  #     - provider: custom:openai-proxy
  #       model: gpt-6.1-sol
  #       reasoning_effort: high
  # - name: writer
  #   description: "Draft substantial voice-sensitive prose or make substantial editorial revisions from established facts and supplied voice examples. Not routine summaries or replies (worker), research extraction (explorer), or analytical reports whose main work is reaching a recommendation (planner)."
  #   provider: custom:claude-proxy
  #   model: claude-opus-5-5
  #   reasoning_effort: medium
  #   service_tier: normal
  #   fallback_providers:
  #     - provider: custom:openai-proxy
  #       model: gpt-6.1-sol
  #       reasoning_effort: medium
"""

# Cron jobs keep Sol as their primary route, then use the same subscription
# fallback order as delegation without spending the Opus pool by default.
CRON_FALLBACKS = [
    {"model": "claude-sonnet-5-5", "provider": "custom:claude-proxy", "reasoning_effort": "medium"},
    {"model": "muse-wonder-full-1.0", "provider": "custom:openai-proxy", "reasoning_effort": "high"},
    {"model": "grok-4.7", "provider": "xai-oauth", "reasoning_effort": "medium"},
]


def install_cron_fallbacks(config: dict, changes: list[str], *, install_missing: bool = False) -> None:
    cron = config.get("cron")
    if cron is None:
        if "cron" not in config and install_missing:
            cron = config["cron"] = {}
        else:
            return
    if not isinstance(cron, dict):
        return
    if "fallback_providers" not in cron:
        if install_missing:
            cron["fallback_providers"] = [dict(route) for route in CRON_FALLBACKS]
            changes.append("cron.fallback_providers: claude-sonnet-5-5 -> Muse -> Grok (Opus removed from the default cron chain)")
        return
    current = cron["fallback_providers"]
    if (isinstance(current, list) and current and isinstance(current[0], dict)
            and current[0].get("model") == "claude-opus-5-5"
            and current[0].get("provider") == "custom:claude-proxy"):
        cron["fallback_providers"] = [dict(route) for route in CRON_FALLBACKS]
        changes.append("cron.fallback_providers: claude-sonnet-5-5 -> Muse -> Grok (Opus removed from the default cron chain)")


# Review routes keep their own effort per entry. Only the Opus head becomes Sonnet,
# so the Muse then Grok tail and every other key on the entry are left as authored.
REVIEW_ROUTES = ("review", "background_review")


def retire_opus_review_fallbacks(config: dict, changes: list[str]) -> None:
    auxiliary = config.get("auxiliary")
    if not isinstance(auxiliary, dict):
        return
    for name in REVIEW_ROUTES:
        route = auxiliary.get(name)
        chain = route.get("fallback_chain") if isinstance(route, dict) else None
        head = chain[0] if isinstance(chain, list) and chain else None
        if (isinstance(head, dict) and head.get("model") == "claude-opus-5-5"
                and head.get("provider") == "custom:claude-proxy"):
            head["model"] = "claude-sonnet-5-5"
            changes.append(f"auxiliary.{name}.fallback_chain: claude-opus-5-5 -> claude-sonnet-5-5 (Opus removed from the review chain)")


def role_settings():
    # Ruamel preserves absolute comment columns when this block is nested in
    # delegation.backends.model_map. Its option entries start at column 8.
    nested = "\n".join("      " + line if line.startswith("  #") else line for line in ROLE_SETTINGS.splitlines())
    return roundtrip_yaml().load(nested)


def install_role_routes(config: dict, aliases: dict, changes: list[str]) -> None:
    settings = role_settings()
    config["delegation"].setdefault("backends", {})["model_map"] = settings
    config["delegation"]["backend"] = "model_map"
    config["delegation"]["inherit_service_tier"] = False
    config["delegation"]["fallback_providers"] = []
    install_cron_fallbacks(config, changes, install_missing=True)
    for option in settings["options"]:
        name = option["name"]
        aliases[name] = {"provider": option["provider"], "model": option["model"]}
        option["model"] = name
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if "advanced-delegation" not in enabled:
        enabled.append("advanced-delegation")
    disabled = plugins.get("disabled", [])
    if "advanced-delegation" in disabled:
        disabled.remove("advanced-delegation")
    changes.append("delegation: official model_map worker default, explorer and coder enabled; 5 Claude routes commented pending reserve protection")
