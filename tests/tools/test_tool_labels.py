"""MCP labels preserve callable names on direct, bridged and client payload paths."""

import pytest

from agent.display import progress_tool_label, set_friendly_tool_labels, tool_labels_for_call
from tools.tool_labels import label_for_tool_name, labels_for_call


@pytest.mark.parametrize("tool", ["topic_icons", "topic_list", "Get_Topic-Icons", "topic__list"])
def test_mcp_tool_name_is_verbatim(tool):
    name = f"mcp__telegram__{tool}"
    direct = label_for_tool_name(name)
    bridged = labels_for_call("tool_call", {"calls": [{"name": name, "arguments": {}}]})

    assert direct is not None
    assert direct.kind == "mcp"
    assert direct.action == tool
    assert direct.text == f"Telegram · {tool}"
    assert bridged == [direct]
    assert labels_for_call(name, {}) == [direct]
    payload = tool_labels_for_call("tool_call", {"calls": [{"name": name}]})[0].as_payload()
    assert payload["name"] == name
    assert payload["action"] == tool
    assert payload["text"] == f"Telegram · {tool}"


@pytest.mark.parametrize("tool", ["topic_icons", "topic_list"])
def test_progress_label_preserves_mcp_underscores(tool):
    set_friendly_tool_labels(True)
    assert progress_tool_label(f"mcp__telegram__{tool}")[1] == f"telegram · {tool}"


def test_connector_label_keeps_friendly_phrase_and_account():
    label = label_for_tool_name(
        "connectors__gmail__GMAIL_SEND_EMAIL", {"connector_alias": "work"})

    assert label is not None
    assert label.kind == "connector"
    assert label.text == "Gmail · send email (work)"


def test_mcp_app_title_stays_friendly_without_changing_tool_name():
    label = label_for_tool_name("mcp__my_server__List_Items")

    assert label is not None
    assert label.app == "My Server"
    assert label.text == "My Server · List_Items"
