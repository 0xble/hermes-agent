"""A safe worker's reaction gating must not discover platform plugins."""
import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("safe", [False, True])
def test_reaction_gating_respects_frozen_safe_worker_policy(safe):
    probe = r'''
import json, sys
if sys.argv[1] == "True":
    from agent.safe_worker_policy import _bind_safe_worker_policy
    _bind_safe_worker_policy(safe_mode=True, ignore_user_config=True, config={})
opened = []
def witness(event, args):
    if event == "open" and "/plugins/" in str(args[0]) and str(args[0]).endswith("plugin.yaml"):
        opened.append(str(args[0]))
sys.addaudithook(witness)
from gateway.config import Platform
from toolsets import session_disabled_toolsets
assert Platform("telegram") is Platform.TELEGRAM
session_disabled_toolsets(None, "cli")
try:
    Platform("not-a-platform")
except ValueError:
    pass
else:
    raise AssertionError("unknown platform accepted")
print(json.dumps(opened))
'''
    result = subprocess.run([sys.executable, "-c", probe, str(safe)],
                            capture_output=True, text=True, check=True)
    opened = json.loads(result.stdout)
    assert bool(opened) is not safe, opened
