"""Protect updates from dropping commits already live from ``origin/main``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class MainDrop:
    """Commits present in the live release and ``origin/main`` but absent from the target."""

    target: str
    live: str
    main: str
    commits: tuple[tuple[str, str], ...]

    @property
    def count(self) -> int:
        return len(self.commits)

    @property
    def message(self) -> str:
        names = "; ".join(f"{sha[:12]} ({subject})" for sha, subject in self.commits[:3])
        if self.count > 3:
            names += f"; and {self.count - 3} more"
        noun = "commit" if self.count == 1 else "commits"
        return (
            f"✗ Refusing update: target {self.target[:12]} would drop {self.count} {noun} "
            f"that are already live and on origin/main: {names}. "
            "Merge origin/main into the candidate and retry. "
            "To intentionally roll back, re-run `hermes update --allow-drop-main`."
        )


class MainDropCheckError(RuntimeError):
    """The ancestry check could not be completed safely."""


class MainDropCheckSkipped(RuntimeError):
    """The comparison ref was unavailable, so the advisory check was recorded as skipped."""


def find_main_drop(
    run_git: Callable[..., object],
    target_ref: str,
    live_ref: str,
    *,
    main_ref: str = "origin/main",
) -> MainDrop | None:
    """Return a main-drop finding, or ``None`` when the target preserves main.

    ``run_git`` is the updater's checked Git runner. Keeping the ancestry logic
    here makes the safety rule testable without invoking the full update command.
    """

    def output(args: list[str]) -> str:
        try:
            result = run_git(args, check=True)
        except Exception as exc:  # the updater turns this into a refused update
            raise MainDropCheckError(f"git {' '.join(args)} failed: {exc}") from exc
        value = getattr(result, "stdout", "") or ""
        return str(value).strip()

    target = output(["rev-parse", "--verify", f"{target_ref}^{{commit}}"])
    live = output(["rev-parse", "--verify", f"{live_ref}^{{commit}}"])
    try:
        main = output(["rev-parse", "--verify", f"{main_ref}^{{commit}}"])
    except MainDropCheckError as exc:
        raise MainDropCheckSkipped(str(exc)) from exc
    if not target or not live:
        raise MainDropCheckError("the selected target or live release resolved to an empty SHA")
    if not main:
        raise MainDropCheckSkipped(f"git rev-parse {main_ref} returned an empty SHA")

    live_only = output(["rev-list", f"{target}..{live}"]).splitlines()
    if not live_only:
        return None
    main_history = set(output(["rev-list", main]).splitlines())
    dropped = [sha for sha in live_only if sha in main_history]
    if not dropped:
        return None

    commits: list[tuple[str, str]] = []
    for sha in dropped:
        subject = output(["show", "-s", "--format=%s", sha]).replace("\n", " ").strip()
        commits.append((sha, subject or "(no subject)"))
    return MainDrop(target=target, live=live, main=main, commits=tuple(commits))
