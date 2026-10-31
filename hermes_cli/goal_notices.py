"""Profile-scoped visibility policy for native goal and loop notices.

``goals.notices`` and ``loops.notices`` each take 1 of 3 levels:

- ``true`` (default) shows every lifecycle card.
- ``false`` hides automatic lifecycle output: goal verdict and status cards, loop outcome
  cards, ``goal_set``/``loop_set`` receipts and the ``/goal`` set confirmation. Approval cards
  and receipts for user-authorized controls still post.
- ``hidden`` hides every automatic card for that lifecycle, including approval cards and
  control receipts. An approval that would need a hidden card is refused at once, so the agent
  asks in chat instead. A hidden stall card (paused, blocked, failed, tick budget) wakes the
  session with an internal turn instead. Replies to the user's own ``/goal`` or ``/loop``
  command, including the ``/goal`` set confirmation, and the agent's own replies stay visible.

The level is ``hidden`` rather than ``none`` because ``hermes config set`` turns ``none``
into null.
"""
from hermes_cli.config import load_config_readonly

NOTICE_SECTIONS = ("goals", "loops")
HIDDEN = "hidden"


def notice_level(section: str = "goals") -> str:
    """``all``, ``quiet`` or ``hidden``. Read on use so multiplexed profiles never share a policy."""
    value = (load_config_readonly().get(section) or {}).get("notices", True)
    if isinstance(value, str) and value.strip().lower() == HIDDEN:
        return HIDDEN
    return "quiet" if value is False else "all"


def goal_notices_enabled() -> bool:
    """Whether automatic goal output shows. False under both ``false`` and ``hidden``."""
    return notice_level("goals") == "all"


def loop_notices_enabled() -> bool:
    """Whether automatic loop output shows. False under both ``false`` and ``hidden``."""
    return notice_level("loops") == "all"


def goal_command_replies_hidden() -> bool:
    """Only ``false`` hides the ``/goal`` set confirmation and drafting progress. Under
    ``hidden`` they stay, because they answer the user's own command."""
    return notice_level("goals") == "quiet"


def lifecycle_cards_hidden(kind: str) -> bool:
    """Whether every card for a ``goal``/``goals`` or ``loop``/``loops`` lifecycle is hidden."""
    section = kind if kind.endswith("s") else f"{kind}s"
    return section in NOTICE_SECTIONS and notice_level(section) == HIDDEN
