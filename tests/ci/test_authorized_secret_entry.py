"""Fork contract: upstream syncs must preserve authorized secret entry, not just snapshots."""
from __future__ import annotations

import json
import re
from pathlib import Path

from model_tools import _apply_dynamic_schemas
from tools.registry import registry

ROOT = Path(__file__).resolve().parents[2]
# Inspect every live browser/vault schema, including deferred tools and both input backends.
BANNED = re.compile(
    r"vault[- ]only|keep card[^.]*vault|(?:card numbers?|CVCs?|PAN)[^.]{0,100}"
    r"(?:only (?:through|by)|remain[^.]*vault)|"
    r"(?:never|do not|don't|must not) (?:type|enter)[^.]{0,100}"
    r"(?:password|code|card|CVC)|browser-input redaction|redaction-capable runtime",
    re.IGNORECASE,
)


def test_live_browser_schemas_and_bundled_guidance_allow_authorized_secret_entry():
    from tools.browser_use_cli import BROWSER_EXEC_SCHEMA

    schemas = []
    for name in registry.get_all_tool_names():
        if name.startswith("browser_") or "vault" in name:
            entry = registry.get_entry(name)
            assert entry is not None
            schemas.append({"type": "function", "function": entry.schema})
    if not any(item["function"]["name"] == "browser_exec" for item in schemas):
        schemas.append({"type": "function", "function": BROWSER_EXEC_SCHEMA})
    terminal = registry.get_entry("terminal")
    assert terminal is not None
    schemas.append({"type": "function", "function": terminal.schema})
    rendered = {
        item["function"]["name"]: json.dumps(item["function"])
        for item in _apply_dynamic_schemas(schemas)
    }
    assert {"browser_type", "browser_exec", "browser_vault"} <= rendered.keys()
    for name, text in rendered.items():
        assert not BANNED.search(text), f"{name} forbids authorized secret entry"
    for name in ("browser_type", "browser_exec", "browser_vault"):
        text = rendered[name]
        assert "may be typed" in text, name
        for value in ("1Password", "card number", "expiry", "CVC", "password", "code"):
            assert value.lower() in text.lower(), (name, value)
        assert "approved exact charge" in text and "merchant origin" in text, name
    for folder in ("skills", "optional-skills"):
        for guidance in (ROOT / folder).rglob("*.md"):
            assert not BANNED.search(guidance.read_text(encoding="utf-8")), guidance
    guidance = ROOT / "skills/autonomous-ai-agents/computer-use/SKILL.md"
    text = guidance.read_text(encoding="utf-8")
    assert not BANNED.search(text), "bundled computer-use forbids authorized secret entry"
    for value in ("may be typed", "1Password", "card number", "expiry", "CVC", "password", "code"):
        assert value in text, value
    paths = [
        *ROOT.glob("tools/browser*.py"),
        ROOT / "evals/prefix_rediet/worker.py",
        ROOT / "website/docs/user-guide/features/credential-vault.md",
        ROOT / "website/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-computer-use.md",
        ROOT / "website/docs/user-guide/skills/optional/payments/payments-stripe-link-cli.md",
    ]
    for path in paths:
        assert not BANNED.search(path.read_text(encoding="utf-8")), path


def test_secret_entry_guard_rejects_restored_restrictions():
    for text in (
        "Card numbers and CVCs remain vault-only.",
        "Keep card numbers and CVCs in vault fills.",
        "Card numbers may be entered only through browser_vault_fill.",
        "Never type a password, card number, CVC or code.",
        "Direct entry requires browser-input redaction for PAN/CVC-shaped values.",
    ):
        assert BANNED.search(text), text
