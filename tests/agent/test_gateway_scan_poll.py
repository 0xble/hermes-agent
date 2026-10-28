"""Hot skill and config signature checks reuse a short poll instead of stating every call.

The live gateway rebuilt a skills-tree manifest and restated config.yaml on every
prompt and hook. Another process's install or edit is still visible once the poll
expires, or immediately after the owning cache is cleared. Pytest itself keeps the
interval at zero so existing freshness tests stay immediate.
"""

import pytest

from utils import filesystem_poll_seconds


def test_poll_interval_is_zero_while_pytest_owns_the_process():
    assert filesystem_poll_seconds(5.0) == 0.0


def test_poll_interval_is_the_default_outside_pytest(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("HERMES_FS_POLL", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/Users/brianle/.hermes")
    assert filesystem_poll_seconds(5.0) == 5.0
    assert filesystem_poll_seconds(0.0) == 0.0


@pytest.mark.parametrize("override, expected", [
    ("0.0", 0.0), ("00", 0.0), ("-1", 0.0), ("OFF", 0.0), ("false", 0.0),
    ("1", 1.0), ("2.5", 2.5), ("invalid", 5.0), ("nan", 5.0), ("inf", 5.0),
])
def test_poll_interval_override_is_finite_seconds(monkeypatch, override, expected):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/operator-home")
    monkeypatch.setenv("HERMES_FS_POLL", override)
    assert filesystem_poll_seconds(5.0) == expected


def test_positive_override_does_not_enable_polling_under_pytest(monkeypatch):
    monkeypatch.setenv("HERMES_FS_POLL", "1")
    assert filesystem_poll_seconds(5.0) == 0.0


def test_skills_manifest_walks_once_per_poll_then_sees_a_new_file(tmp_path, monkeypatch):
    import agent.prompt_builder as pb

    skills = tmp_path / "skills"
    (skills / "tools" / "one").mkdir(parents=True)
    (skills / "tools" / "one" / "SKILL.md").write_text("---\nname: one\n---\n", encoding="utf-8")
    clock = {"now": 100.0}
    walks = {"n": 0}
    real_walk = pb._walk_skills_manifest

    def counting_walk(path):
        walks["n"] += 1
        return real_walk(path)

    monkeypatch.setattr(pb, "filesystem_poll_seconds", lambda _default: 5.0)
    monkeypatch.setattr(pb.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(pb, "_walk_skills_manifest", counting_walk)
    pb._MANIFEST_POLL.clear()

    assert any(path.endswith("one/SKILL.md") for path in pb._build_skills_manifest(skills))
    assert pb._build_skills_manifest(skills)
    assert walks["n"] == 1

    (skills / "tools" / "two").mkdir(parents=True)
    (skills / "tools" / "two" / "SKILL.md").write_text("---\nname: two\n---\n", encoding="utf-8")
    assert not any(path.endswith("two/SKILL.md") for path in pb._build_skills_manifest(skills))
    assert walks["n"] == 1

    clock["now"] = 106.0
    assert any(path.endswith("two/SKILL.md") for path in pb._build_skills_manifest(skills))
    assert walks["n"] == 2

    pb.clear_skills_system_prompt_cache()
    (skills / "tools" / "three").mkdir(parents=True)
    (skills / "tools" / "three" / "SKILL.md").write_text("---\nname: three\n---\n", encoding="utf-8")
    assert any(path.endswith("three/SKILL.md") for path in pb._build_skills_manifest(skills))
    assert walks["n"] == 3


def test_catalog_signature_walks_once_per_poll_and_clear_is_immediate(tmp_path, monkeypatch):
    import tools.skills_tool as st
    import utils

    root = tmp_path / "skills"
    (root / "mlops" / "training" / "existing").mkdir(parents=True)
    clock = {"now": 50.0}
    walks = {"n": 0}
    real = st._dir_tree_signature

    def counting(path, sig, depth=st._SIGNATURE_MAX_DEPTH):
        if depth == st._SIGNATURE_MAX_DEPTH:
            walks["n"] += 1
        return real(path, sig, depth)

    # The signature function imports the poll helper at call time.
    monkeypatch.setattr(utils, "filesystem_poll_seconds", lambda _default: 5.0)
    monkeypatch.setattr(st.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(st, "_dir_tree_signature", counting)
    st._SCAN_POLL.clear()

    first = st._skills_scan_signature([root], set())
    assert st._skills_scan_signature([root], set()) == first
    assert walks["n"] == 1

    (root / "mlops" / "training" / "installed").mkdir()
    assert st._skills_scan_signature([root], set()) == first
    assert walks["n"] == 1

    st.clear_skills_cache()
    assert st._skills_scan_signature([root], set()) != first
    assert walks["n"] == 2


def test_poll_stays_off_for_a_pytest_home_and_an_explicit_disable(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/var/tmp/hermes-pytest-1/pytest-of-ci/home")
    assert filesystem_poll_seconds(5.0) == 0.0
    monkeypatch.setenv("HERMES_HOME", "/Users/brianle/.hermes")
    monkeypatch.setenv("HERMES_FS_POLL", "0")
    assert filesystem_poll_seconds(5.0) == 0.0
    monkeypatch.delenv("HERMES_FS_POLL")
    assert filesystem_poll_seconds(5.0) == 5.0

