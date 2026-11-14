"""The checkout DOM matrix gets Chromium in its own hosted shard."""
import unittest
from unittest.mock import patch

from scripts.ci import portable


# The static lane runs this directory with ``unittest discover``, which collects only
# TestCase methods: a bare pytest-style function here is silently never run.
class VaultBrowserProvisionTests(unittest.TestCase):
    def test_checkout_only_shard_provisions_browser(self):
        commands = []
        with patch.object(portable, 'python', return_value='/fixture/python'), \
             patch.object(portable, 'require_wal_capable_sqlite'), \
             patch.object(portable, 'require_tools'), \
             patch.object(portable, 'run', side_effect=lambda cmd, **kw: commands.append(cmd)), \
             patch.object(portable.os, 'access', return_value=True):
            portable.python_tests({}, ['tests/tools/test_browser_vault_checkout_live.py'], 1)
        self.assertIn(['/fixture/python', '-m', 'playwright', 'install', 'chromium'], commands)
        self.assertEqual(commands[-1][:2], ['bash', 'scripts/run_tests.sh'])


if __name__ == '__main__':
    unittest.main()
