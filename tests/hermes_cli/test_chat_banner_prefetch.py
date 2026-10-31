"""Only classic chat warms a Python banner, while both surfaces seed skills."""

import pytest


@pytest.mark.parametrize("prefetch_banner", [False, True])
def test_banner_prefetch_preserves_skills_sync(monkeypatch, tmp_path, prefetch_banner):
    from hermes_cli import banner, main

    calls = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(main, "_termux_should_prefetch_update_check", lambda: True)
    monkeypatch.setattr(banner, "prefetch_update_check", lambda: calls.append("update"))
    monkeypatch.setattr(banner, "prefetch_banner_data", lambda: calls.append("banner"))
    monkeypatch.setattr(main, "_sync_bundled_skills_for_startup", lambda: calls.append("skills"))

    main._start_chat_background_prefetch(prefetch_banner=prefetch_banner)

    assert calls == (["update", "banner", "skills"] if prefetch_banner else ["skills"])
