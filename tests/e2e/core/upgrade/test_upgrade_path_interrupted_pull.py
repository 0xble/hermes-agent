"""Historical N-1 upgrade recovery after an interrupted pull.

Each file keeps fresh installs and the shared upgrade-path behavior contract.
"""
from __future__ import annotations

import contextlib

import pytest

from tests.e2e.core._pending_fixes import known_failure
from tests.e2e.core.upgrade import _helpers as H
from tests.e2e.core.upgrade import test_upgrade_path as U
from tests.e2e.core.upgrade.test_upgrade_path import (
    _age_git_locks,
    _freeze_update_at,
    _git,
    _publish_head,
    _refs,
    _update,
    _upgrade_prerequisites,
    assert_healthy_at_head,
    install_cache,
    leg,
    provider,
    template_home,
)

pytestmark = U.pytestmark


@pytest.mark.parametrize("torn", [
    pytest.param(False, id="lock-only"),
    pytest.param(True, id="torn-tree"),
])
def test_kill_mid_pull_then_retry_heals(leg, provider, torn):
    changed = sorted(_git("diff", "--name-only", "--no-renames", "--diff-filter=AM", _refs().base, _refs().head,
                          cwd=H.WORKTREE).splitlines())
    assert changed, "N-1 and HEAD have no file differences"
    torn_paths = changed[: max(1, len(changed) // 2)] if torn else []
    (leg.root / "torn-paths").write_text("".join(p + "\n" for p in torn_paths))
    (leg.root / "arm-pull").touch()
    _publish_head(leg)
    _freeze_update_at(leg, "frozen-pull")
    assert _git("rev-parse", "HEAD", cwd=leg.install) == _refs().base, "kill point was not mid-pull"
    assert (leg.install / ".git" / "index.lock").exists()
    _age_git_locks(leg.install)
    final = _update(leg)
    # Merge-order safe (see _pending_fixes.known_failure): only the import-time death excuses torn-tree.
    gate = known_failure(
        r"(?s)^final `hermes update` failed:.*(cannot import name|No module named)",
        "#120339 (merged; passes once N-1 is a release carrying it): a fast-forward killed half-way leaves a prefix of the changed files at HEAD while "
        "HEAD still names N-1; every entry point, including `hermes update`, then dies at import (cannot import "
        "name ... from 'utils'), so nothing can heal the install without manual git") if torn else contextlib.nullcontext()
    with gate:
        assert_healthy_at_head(leg, provider, final)

