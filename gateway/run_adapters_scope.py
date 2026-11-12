"""Routed-profile home sentinel type for adapter scope resolution."""

from __future__ import annotations


class _UnresolvedProfileHome:
    """A NAMED routed profile whose home does not resolve — never the same thing as ``None``
    ("this body is the launch profile's own work"). Overloading ``None`` for both let an inbound
    message on a secondary's bot run with the LAUNCH profile's ``.env`` and frozen env."""

    __slots__ = ()

    def __repr__(self) -> str:  # log/diagnostic readability
        return "<unresolved profile home>"
