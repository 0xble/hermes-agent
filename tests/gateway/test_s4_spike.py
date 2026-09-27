"""Disposable cross-process feasibility probe; run with scripts/run_tests.sh."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall


def test_executor_turn_survives_router_replacement(tmp_path):
    from spikes.s4_router_executor.probe import run_probe

    evidence = Path(os.environ.get("S4_SPIKE_EVIDENCE", str(tmp_path / "evidence")))
    seconds = int(os.environ.get("S4_SPIKE_SECONDS", "8"))
    def answer(request):
        messages = request["body"]["messages"]
        text = next((m.get("content") for m in reversed(messages) if m["role"] == "user"), "")
        if "run long tool" in str(text):
            if messages[-1]["role"] != "tool":
                return ToolCall("terminal", {"command": f"sleep {seconds}"})
            return Text("A done")
        if "new B session" in str(text):
            return Text("B done")
        return Text("A follow-up")

    with FakeLLMServer(answer) as provider:
        receipt = run_probe(evidence, provider.base_url, tool_wait=seconds)
        tool_outputs = [m.get("content", "") for r in provider.main_requests() for m in r["messages"] if m["role"] == "tool"]
    assert any(json.loads(str(output)).get("exit_code") == 0 for output in tool_outputs), tool_outputs
    assert receipt["a_survived_router_replacement"], receipt
    assert receipt["b_served_during_a"]
    assert receipt["a_followup_ordered"]
    assert receipt["poller_max"] == 1
    assert receipt["duplicate_sends"] == 0
    assert (evidence / "receipt.json").exists()
