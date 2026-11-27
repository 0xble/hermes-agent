import pytest

from hermes_state_runtime import RuntimeStoreError


class _Adapter:
    pass


class _Source:
    delivered_via_upstream_relay = False
    profile = None


@pytest.fixture
def restore_fixture(monkeypatch):
    import gateway.session_ingress_context as context
    import gateway.session_identity

    adapter = _Adapter()
    home = object()
    expected = {
        'transport_home': '/primary',
        'runtime_home': '/primary',
        'platform': 'telegram',
        'connector': 'connector',
        'transport_profile': None,
    }
    monkeypatch.setattr(
        context, '_binding', lambda *_args: (adapter, home, home, expected.copy()))
    monkeypatch.setattr(gateway.session_identity, 'canonical_identity', lambda *_args, **_kwargs: None)
    return context, _Source(), expected


def test_restore_accepts_legacy_primary_provenance_without_transport_profile(restore_fixture):
    context, source, expected = restore_fixture
    stored = {key: value for key, value in expected.items() if key != 'transport_profile'}

    assert context.restore_provenance(object(), source, stored) is not None


def test_restore_accepts_default_primary_profile_spelling(restore_fixture):
    context, source, expected = restore_fixture
    expected['transport_profile'] = 'default'
    stored = {key: value for key, value in expected.items() if key != 'transport_profile'}

    assert context.restore_provenance(object(), source, stored) is not None


def test_restore_keeps_named_profile_mismatch_fail_closed(restore_fixture):
    context, source, expected = restore_fixture
    expected['transport_profile'] = 'secondary'
    source.profile = 'secondary'
    stored = {key: value for key, value in expected.items() if key != 'transport_profile'}

    with pytest.raises(RuntimeStoreError, match='profile_mismatch'):
        context.restore_provenance(object(), source, stored)
