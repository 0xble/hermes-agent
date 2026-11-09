"""The vault key against the host's REAL OS keychain (macOS Keychain, Windows Credential Manager).

Everything else in the vault suite uses an in-memory keyring, which proves Hermes' plumbing but not
that the platform store accepts, returns and deletes an entry the way ``agent.vault_keychain``
expects. These run only on the macOS and Windows CI lanes (never on Linux, so a developer's Secret
Service is never touched). macOS uses an unlocked disposable keychain with interaction disabled,
never the login keychain. Windows uses uniquely named dummy entries. Both clean up in teardown.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import uuid
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet

from agent import vault_keychain as kc
from agent.vault_store import VaultStore

_LOGIN = {"identifier_type": "email", "identifier": "ci@example.com", "password": "dummy-pw-not-real-42"}


@contextmanager
def _macos_keychain(tmp_path, monkeypatch):
    """Scope keyring's real Security calls without changing the user's search list.

    keyring 25.7 ignores its keychain path, so selecting a backend property alone
    still reads and writes the login keychain. Supply the native query selectors.
    """
    from keyring.backends.macOS import api

    path = tmp_path / "vault-test.keychain-db"
    password = uuid.uuid4().hex

    def security(*args):
        subprocess.run(["security", *args, str(path)], check=True, capture_output=True, timeout=15)

    security("create-keychain", "-p", password)
    try:
        security("unlock-keychain", "-p", password)
        with _macos_keychain_queries(api, path, monkeypatch):
            yield
    finally:
        security("delete-keychain")


@contextmanager
def _macos_keychain_queries(api, path, monkeypatch):
    ref = ctypes.c_void_p()
    open_keychain = api._sec.SecKeychainOpen
    open_keychain.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    open_keychain.restype = ctypes.c_int32
    api.Error.raise_for_status(open_keychain(str(path).encode(), ctypes.byref(ref)))
    release = api._found.CFRelease
    release.argtypes = [ctypes.c_void_p]
    release.restype = None
    create_array = api._found.CFArrayCreate
    create_array.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p]
    create_array.restype = ctypes.c_void_p
    search = ctypes.c_void_p(create_array(None, (ctypes.c_void_p * 1)(ref), 1, None))
    get_interaction = api._sec.SecKeychainGetUserInteractionAllowed
    get_interaction.argtypes = [ctypes.POINTER(ctypes.c_bool)]
    get_interaction.restype = ctypes.c_int32
    set_interaction = api._sec.SecKeychainSetUserInteractionAllowed
    set_interaction.argtypes = [ctypes.c_bool]
    set_interaction.restype = ctypes.c_int32
    interaction = ctypes.c_bool()
    try:
        api.Error.raise_for_status(get_interaction(ctypes.byref(interaction)))
        api.Error.raise_for_status(set_interaction(False))
        original_query = api.create_query

        def scoped_query(**kwargs):
            if "kSecValueData" in kwargs:
                kwargs["kSecUseKeychain"] = ref
            else:
                kwargs["kSecMatchSearchList"] = search
            return original_query(**kwargs)

        # Restore the query function before releasing its native references.
        with monkeypatch.context() as patch:
            patch.setattr(api, "create_query", scoped_query)
            yield
    finally:
        api.Error.raise_for_status(set_interaction(interaction.value))
        release(search)
        release(ref)


@pytest.fixture
def os_keychain(tmp_path, monkeypatch):
    kc.os_keyring()
    made: list[dict] = []

    def entries():
        try:
            yield made
        finally:
            for ref in made:
                assert kc.delete_key(ref), "native keychain cleanup must remove each dummy entry"

    if sys.platform == "darwin":
        with _macos_keychain(tmp_path, monkeypatch):
            yield from entries()
    else:
        yield from entries()


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
