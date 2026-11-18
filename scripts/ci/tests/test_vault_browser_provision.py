"""The checkout DOM matrix gets Chromium in its own hosted shard."""
from unittest.mock import patch

from scripts.ci import portable


def test_checkout_only_shard_provisions_browser():
    commands = []
    with patch.object(portable, 'python', return_value='/fixture/python'), \
         patch.object(portable, 'require_wal_capable_sqlite'), \
         patch.object(portable, 'require_tools'), \
         patch.object(portable, 'run', side_effect=lambda cmd, **kw: commands.append(cmd)), \
         patch.object(portable.os, 'access', return_value=True):
        portable.python_tests({}, ['tests/tools/test_browser_vault_checkout_live.py'], 1)
    assert ['/fixture/python','-m','playwright','install','chromium'] in commands
    assert commands[-1][:2] == ['bash','scripts/run_tests.sh']
