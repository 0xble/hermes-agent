"""Profile skill-count cache freshness."""

import pytest


def test_count_skills_publishes_timestamp_after_the_walk(tmp_path, monkeypatch):
    """A scan longer than the TTL must not publish an already-expired cache entry (#107151):
    the cached timestamp is taken after _walk_skill_count returns, not before it starts."""
    from hermes_cli import profiles as mod

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(mod, "_SKILL_COUNT_CACHE", {})
    monkeypatch.setattr(mod, "_SKILL_COUNT_SCAN_LOCKS", {}, raising=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])

    def slow_walk(_dir):
        clock["now"] += mod._SKILL_COUNT_TTL_SECONDS + 5  # walk outlives the TTL
        return 3

    monkeypatch.setattr(mod, "_walk_skill_count", slow_walk)
    assert mod._count_skills(tmp_path) == 3
    _sig, stamped, count = mod._SKILL_COUNT_CACHE[str(skills_dir)]
    assert count == 3
    assert stamped >= clock["now"], "published timestamp must be >= scan end"
    # Entry is fresh: a second call within the TTL must not walk again.
    monkeypatch.setattr(mod, "_walk_skill_count", lambda _d: pytest.fail("re-walked a fresh entry"))
    assert mod._count_skills(tmp_path) == 3
