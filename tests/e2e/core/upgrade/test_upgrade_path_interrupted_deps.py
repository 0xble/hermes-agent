"""Historical N-1 upgrade recovery after an interrupted dependency handoff.

Each file keeps fresh installs and the shared upgrade-path behavior contract.
"""
from __future__ import annotations

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
    leg as leg,
    provider as provider,
    template_home,
)

pytestmark = U.pytestmark


def test_kill_before_deps_then_retry_heals(leg, provider):
    (leg.root / "arm-deps").touch()
    _publish_head(leg)
    _freeze_update_at(leg, "frozen-deps")
    assert _git("rev-parse", "HEAD", cwd=leg.install) == _refs().head, "kill point was not after the code swap"
    _age_git_locks(leg.install)
    final = _update(leg)
    assert_healthy_at_head(leg, provider, final)
