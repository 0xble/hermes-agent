"""The vault key against the host's REAL OS keychain (macOS Keychain, Windows Credential Manager).

Everything else in the vault suite uses an in-memory keyring, which proves Hermes' plumbing but not
that the platform store accepts, returns and deletes an entry the way ``agent.vault_keychain``
expects. These run only on the macOS and Windows CI lanes (never on Linux, so a developer's Secret
Service is never touched), use uniquely named dummy entries, and delete them in teardown.
"""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from contextlib import ExitStack, contextmanager

import pytest
from cryptography.fernet import Fernet

from agent import vault_keychain as kc
from agent.vault_store import VaultStore

_LOGIN = {"identifier_type": "email", "identifier": "ci@example.com", "password": "dummy-pw-not-real-42"}


@contextmanager
def _noninteractive_macos_keychain():
    from keyring.backends.macOS import api

    security = api._sec
    get = security.SecKeychainGetUserInteractionAllowed
    get.argtypes = [ctypes.POINTER(ctypes.c_bool)]
    get.restype = ctypes.c_int32
    set_allowed = security.SecKeychainSetUserInteractionAllowed
    set_allowed.argtypes = [ctypes.c_bool]
    set_allowed.restype = ctypes.c_int32
    previous = ctypes.c_bool()
    assert get(ctypes.byref(previous)) == 0
    assert set_allowed(False) == 0
    try:
        yield
    finally:
        assert set_allowed(previous.value) == 0


@pytest.fixture
def os_keychain(monkeypatch):
    """Use the real account keychain, including under CI's synthetic HOME."""
    with ExitStack() as stack:
        if sys.platform == "darwin":
            import pwd

            # SecItemAdd consults HOME for the account's default keychain. CI's
            # empty HOME can otherwise require interactive keychain access.
            # VaultStore itself stays scoped to the test's HERMES_HOME.
            monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
            stack.enter_context(_noninteractive_macos_keychain())
        kc.os_keyring()
        made: list[dict] = []
        try:
            yield made
        finally:
            for ref in made:
                assert kc.delete_key(ref)


@pytest.mark.platforms("macos", "windows")
def test_real_keychain_stores_reads_and_deletes_a_dummy_entry(os_keychain):
    ref = {"storage": "keychain", "service": kc.SERVICE, "account": f"hermes-ci-smoke-{uuid.uuid4().hex}"}
    os_keychain.append(ref)
    key = Fernet.generate_key()
    kc.store_key(ref, key)  # raises unless the store hands the same bytes back
    assert kc.load_key(ref) == key
    assert kc.delete_key(ref)
    assert kc.load_key(ref) is None


@pytest.mark.platforms("macos", "windows")
def test_keychain_mode_vault_round_trips_through_the_real_keychain(os_keychain, tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("vault:\n  key_storage: keychain\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    vault = home / "vault"

    item = VaultStore().add_item("login", "Example", dict(_LOGIN), origin="https://example.com").id
    ref = kc.read_ref(vault / "vault.keyref")
    assert ref is not None, "keychain mode must keep only a pointer beside the ciphertext"
    os_keychain.append(ref)
    assert not (vault / "vault.key").exists()
    assert VaultStore().resolve_secret(item)["password"] == _LOGIN["password"]

    assert VaultStore().migrate_key("file") == "file"
    assert (vault / "vault.key").exists() and not (vault / "vault.keyref").exists()
    assert kc.load_key(ref) is None, "moving to the file deletes the vault's own keychain entry"

    assert VaultStore().migrate_key("keychain") == "keychain"
    back = kc.read_ref(vault / "vault.keyref")
    os_keychain.append(back)
    assert not (vault / "vault.key").exists()
    assert VaultStore().resolve_secret(item)["password"] == _LOGIN["password"]
