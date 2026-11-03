"""A browse holder releasing between conflict probes must not abort gateway boot."""
from unittest.mock import patch

import pytest

from gateway.run_bootstrap import _reserve_waiting_out_browse_reconcile
from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership
from hermes_state_reconcile import BROWSE_RECONCILE_PURPOSE


@pytest.mark.parametrize("wait_s", [0.0, 1.0])
def test_browse_releasing_during_conflict_probe_respects_reservation_budget(tmp_path, wait_s):
    from gateway import run_bootstrap

    holder = ProfileOwnership()
    contender = ProfileOwnership()
    holder.reserve([tmp_path], purpose=BROWSE_RECONCILE_PURPOSE)
    original_probe = run_bootstrap._conflicting_homes

    def released_before_probe(homes):
        holder.close()
        return original_probe(homes)

    try:
        with patch.object(run_bootstrap, "_conflicting_homes", released_before_probe):
            if wait_s == 0:
                with pytest.raises(OwnershipConflict):
                    _reserve_waiting_out_browse_reconcile(contender, [tmp_path], wait_s=wait_s)
                assert not contender.owns(tmp_path)
            else:
                _reserve_waiting_out_browse_reconcile(contender, [tmp_path], wait_s=wait_s)
                assert contender.owns(tmp_path)
    finally:
        holder.close()
        contender.close()
