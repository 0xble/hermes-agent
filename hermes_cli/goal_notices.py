"""Profile-scoped visibility policy for native goal and loop notices.

``goals.notices`` and ``loops.notices`` each take 1 of 3 levels:

- ``true`` (default) shows every lifecycle card.
- ``false`` hides automatic lifecycle output: goal verdict and status cards, loop outcome
  cards, ``goal_set``/``loop_set`` receipts and the ``/goal`` set confirmation. Approval cards
  and receipts for user-authorized controls still post.
- ``none`` also hides those approval cards and control receipts. An approval that would need a
  hidden card is refused at once, so the agent asks in chat instead. Other replies to the
  user's own ``/goal`` or ``/loop`` command and the agent's own replies stay visible. Hidden
  blocker, pause and failure cards are handed to the agent's next turn instead.
"""
from hermes_cli.config import load_config_readonly

NOTICE_SECTIONS = ("goals", "loops")


def notice_level(section: str = "goals") -> str:
    """``all``, ``quiet`` or ``none``. Read on use so multiplexed profiles never share a policy."""
    value = (load_config_readonly().get(section) or {}).get("notices", True)
    if isinstance(value, str) and value.strip().lower() == "none":
        return "none"
    return "quiet" if value is False else "all"


def goal_notices_enabled() -> bool:
    return notice_level("goals") == "all"


def lifecycle_cards_hidden(kind: str) -> bool:
    """Whether every card for a ``goal``/``goals`` or ``loop``/``loops`` lifecycle is hidden."""
    section = kind if kind.endswith("s") else f"{kind}s"
    return section in NOTICE_SECTIONS and notice_level(section) == "none"
