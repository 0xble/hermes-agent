"""Reader floor for managed updates, independent of mutable profile inventories.

Never use a zero-row count as permission to install an incompatible reader: live
writers and partially activated gateways can create admitted results immediately
after that count. Targets must retain both recovery and this guard.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import NoReturn

MANIFEST = 'runtime-compatibility.json'
REQUIRED = frozenset({'delegation-admitted-v1', 'managed-downgrade-floor-v1'})


class RuntimeCompatibilityError(RuntimeError):
    """An unverified runtime must not replace the installed reader."""


def _refuse(detail) -> NoReturn:
    raise RuntimeCompatibilityError(
        'Runtime compatibility guard refused source replacement: ' + detail
        + '. Keep the current runtime and select a revision declaring admitted-result '
          'recovery and managed downgrade protection. Zero admitted rows is not clearance; '
          'do not convert ledger states or use the rollback ref blindly.')


def require_manifest(text):
    try:
        value = json.loads(text)
        capabilities = value['capabilities']
        if (type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(capabilities, list)
                or any(not isinstance(item, str) for item in capabilities)
                or not REQUIRED.issubset(capabilities)):
            _refuse('target has no verified compatible capability contract')
    except (ValueError, TypeError, KeyError):
        _refuse('target capability contract is missing or malformed')


def require_directory(root):
    try:
        path = Path(root) / MANIFEST
        if path.is_symlink():
            _refuse('capability contract must be a regular file')
        require_manifest(path.read_text(encoding='utf-8'))
    except OSError as exc:
        _refuse(f'cannot read target capability contract ({type(exc).__name__})')


def _git(git_cmd, root, args):
    try:
        result = subprocess.run(git_cmd + args, cwd=root, capture_output=True,
                                text=True, encoding='utf-8', errors='strict')
    except (OSError, UnicodeError) as exc:
        _refuse(f'cannot verify Git target ({type(exc).__name__})')
    if result.returncode:
        _refuse('Git could not verify the immutable target or capability contract')
    return result.stdout.strip()


def require_git_target(git_cmd, root, target):
    sha = _git(git_cmd, root, ['rev-parse', '--verify', f'{target}^{{commit}}'])
    if not re.fullmatch('[0-9a-f]{40}', sha):
        _refuse('target did not resolve to an immutable commit')
    require_manifest(_git(git_cmd, root, ['show', f'{sha}:{MANIFEST}']))
    return sha


def guarded_git_args(git_cmd, root, args):
    """Pin supported source mutations to the object actually checked.

    Read-only plumbing and path-only cleanup do not choose a reader generation.
    Unverified merge composition and stash replay fail closed before mutation.
    """
    if not args:
        return args
    verb = args[0]
    if verb == 'stash' and len(args) > 1 and args[1] in ('apply', 'pop'):
        _refuse('automatic stash replay is not a verified runtime; keep the stash and review it separately')
    if verb == 'stash' and len(args) > 1 and args[1] == 'push':
        require_git_target(git_cmd, root, 'HEAD')
        return args
    if verb == 'merge' and '--abort' in args:
        require_git_target(git_cmd, root, 'HEAD')
        return args
    if verb not in ('checkout', 'reset', 'merge') or '--' in args:
        return args
    if verb == 'reset' and '--hard' not in args:
        return args
    if verb == 'merge' and '--ff-only' not in args:
        _refuse('non-fast-forward composition requires a separately reviewed compatible revision')
    target = args[-1] if not args[-1].startswith('-') and len(args) > 1 else 'HEAD'
    sha = require_git_target(git_cmd, root, target)
    if verb == 'checkout':
        if len(args) == 2 and not re.fullmatch('[0-9a-f]{40}', target):
            # Preserve named-branch semantics without re-reading a mutable ref.
            return ['checkout', '-B', target, sha]
        if '-B' in args or '-b' in args or '--detach' in args or re.fullmatch('[0-9a-f]{40}', target):
            return [*args[:-1], sha]
        _refuse('unsupported checkout shape')
    if target == 'HEAD' and args[-1].startswith('-'):
        return [*args, sha]
    return [*args[:-1], sha]
