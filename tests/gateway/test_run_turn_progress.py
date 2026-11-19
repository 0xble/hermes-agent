"""Re-resolution regressions for progress debt surviving content-bubble resets."""
from types import SimpleNamespace

from gateway.run_turn_progress import GatewayTurnProgressMixin


class _Progress(GatewayTurnProgressMixin):
    def __init__(self):
        self._ctx = SimpleNamespace(last_progress_msg=["old"], repeat_count=[2])

    def _split_progress_groups(self, st, lines):
        return [[line] for line in lines]

    @staticmethod
    def _progress_text(lines):
        return "\n".join(lines)


def test_reset_keeps_unsent_groups_after_the_old_bubble():
    progress = _Progress()
    state = SimpleNamespace(dirty=True, can_edit=True, progress_msg_id="42",
                            progress_lines=["first", "second", "third"], owed_bubbles=[], owed_fresh=False)

    progress._reset_progress_bubble(state)

    assert state.owed_bubbles == [("42", "first")]
    assert state.progress_lines == ["second", "third"]
    assert state.progress_msg_id is None
    assert state.owed_fresh and state.dirty
    assert progress._ctx.last_progress_msg == [None]
    assert progress._ctx.repeat_count == [0]


def test_reset_keeps_a_flood_refused_fresh_bubble():
    progress = _Progress()
    state = SimpleNamespace(dirty=True, can_edit=True, progress_msg_id=None,
                            progress_lines=["not sent"], owed_bubbles=[], owed_fresh=True)

    progress._reset_progress_bubble(state)

    assert state.owed_bubbles == []
    assert state.progress_lines == ["not sent"]
    assert state.owed_fresh and state.dirty
