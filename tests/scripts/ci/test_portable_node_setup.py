"""Portable CI must follow the repository's workspace package manager."""
from contextlib import ExitStack
import json
import os
from unittest.mock import patch

from scripts.ci import portable as ci


def test_setup_prepares_locked_workspaces_and_keeps_website_separate(tmp_path):
    commands = []
    with ExitStack() as stack:
        for name in ('ensure_setup_free_space', 'require_checkout_venv_isolation',
                     'require_tools', 'provision_npm', 'resolve_local_toolchain', 'provision_rg'):
            stack.enter_context(patch.object(ci, name))
        stack.enter_context(patch.object(ci, 'prepare_pnpm'))
        stack.enter_context(patch.object(ci, 'run', side_effect=lambda argv, **kw: commands.append((argv, kw))))
        ci.setup({'PATH': str(tmp_path)})
    installs = [(argv, options.get('cwd', ci.ROOT)) for argv, options in commands
                if argv[:2] == ['npm', 'ci']]
    assert installs == [(['npm', 'ci', '--no-audit', '--no-fund'], ci.ROOT / 'website')]
    prepare = [argv for argv, _ in commands if 'scripts/build/node-deps.mjs' in argv]
    assert len(prepare) == 1
    assert prepare[0][:2] == ['node', 'scripts/build/node-deps.mjs']
    assert '--reuse' in prepare[0]
    assert set(prepare[0][index + 1] for index, value in enumerate(prepare[0]) if value == '--workspace') == {
        'apps/shared', 'apps/desktop', 'tests-js', 'ui-tui', 'ui-tui/packages/hermes-ink', 'web',
    }


def test_pnpm_preparation_is_checkout_owned_and_preserves_ci_node(tmp_path):
    binary = tmp_path / 'toolchain' / 'pm' / 'pnpm' / 'pnpm'
    env = {'PATH': '/ci-node/bin', 'HERMES_CI_PINNED_NODE': '/ci-node/bin/node'}
    with patch.object(ci, 'STATE', tmp_path / '.ci'), \
            patch.object(ci, 'TOOLCHAIN', tmp_path / 'toolchain'), \
            patch.object(ci.subprocess, 'check_output', return_value=json.dumps(str(binary))) as child:
        assert ci.prepare_pnpm(env, install=True) == binary
    child_env = child.call_args.kwargs['env']
    assert child_env['HERMES_HOME'] == str(tmp_path / '.ci' / 'pm-home')
    assert child_env['HERMES_RUNTIME_DIR'] == str(tmp_path / 'toolchain' / 'pm')
    assert env['PATH'].split(os.pathsep) == [str(binary.parent), '/ci-node/bin']
    assert env['HERMES_CI_PINNED_NODE'] == '/ci-node/bin/node'
    assert child.call_args.kwargs['cwd'] == ci.ROOT
