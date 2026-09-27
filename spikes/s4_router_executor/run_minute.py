"""Run the real-minute disposable process probe without the hermetic test env scrub."""
import json
import os
from pathlib import Path
import sys

from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from spikes.s4_router_executor.probe import run_probe


def answer(request):
    messages = request["body"]["messages"]
    text = next((m.get("content") for m in reversed(messages) if m["role"] == "user"), "")
    if "run long tool" in str(text):
        if messages[-1]["role"] != "tool":
            return ToolCall("terminal", {"command": "sleep 60"})
        return Text("A done")
    if "new B session" in str(text):
        return Text("B done")
    return Text("A follow-up")


if __name__ == "__main__":
    home = Path(sys.argv[1])
    with FakeLLMServer(answer) as provider:
        receipt = run_probe(home, provider.base_url, tool_wait=60)
        tool_outputs = [m.get("content", "") for r in provider.main_requests() for m in r["messages"] if m["role"] == "tool"]
        receipt["real_tool_round_trip"] = any(json.loads(str(output)).get("exit_code") == 0 for output in tool_outputs)
        receipt["tool_outputs"] = tool_outputs
        receipt["model_main_requests"] = len(provider.main_requests())
        (home / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))
