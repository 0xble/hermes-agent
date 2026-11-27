"""Secret stores under HERMES_HOME are write-denied; control files stay writable (#110464).

``get_read_block_error`` refuses every credential store. The write side is deliberately
narrower — #45947 freed ``auth.json`` / ``config.yaml`` / ``webhook_subscriptions.json`` so
the user can ask to edit them — but the secret *material* it meant to keep blocked had
drifted: ``auth/google_oauth.json``, the plaintext Bitwarden cache, ``vault/`` and
``browser-profile/`` were writable through ``write_file`` / ``patch``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent.file_safety as fs

SECRET_STORES = (
    "auth/google_oauth.json", "cache/bws_cache.json", "vault/vault.key", "browser-profile/Default/Cookies",
)
WRITABLE_CONTROL_FILES = ("auth.json", "config.yaml", "webhook_subscriptions.json")
CREDENTIAL_LAYOUTS = ("ordinary", "leaf", "parent", "nested-parent", "into-credential")
_SYMLINKS = pytest.mark.require_symlinks


def _layouts(*names):
    """Every layout but ``ordinary`` creates a symlink, so it skips where links cannot exist."""
    return [name if name == "ordinary" else pytest.param(name, marks=_SYMLINKS) for name in names]


@pytest.fixture(autouse=True)
def scratch_account_home(tmp_path, monkeypatch):
    """No credential fixture or home lookup may use the operator's account."""
    import hermes_constants

    home = tmp_path / "account"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    for lookup in ("get_real_home", "get_subprocess_home", "_profile_home_path"):
        monkeypatch.setattr(hermes_constants, lookup, lambda *_args, **_kwargs: home)
    if os.name == "posix":
        import pwd

        account = pwd.struct_passwd(("scratch", "x", os.getuid(), os.getgid(), "", str(home), "/bin/sh"))
        monkeypatch.setattr(pwd, "getpwuid", lambda uid: account)
        getpwnam = pwd.getpwnam
        monkeypatch.setattr(pwd, "getpwnam", lambda name: account if name == "root" else getpwnam(name))
    return home


@pytest.fixture()
def hermes_layout(tmp_path, monkeypatch):
    """Profile HERMES_HOME plus a distinct global root, both patched."""
    root = tmp_path / "hermes_root"
    profile = root / "profiles" / "coder"
    profile.mkdir(parents=True)
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: profile)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    return root, profile


def _touch(base: Path, rel: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dummy", encoding="utf-8")
    return p


def test_read_denied_secret_stores_are_write_denied_on_profile_and_root(hermes_layout):
    root, profile = hermes_layout
    for base in (profile, root):
        for rel in SECRET_STORES:
            path = _touch(base, rel)
            assert fs.get_read_block_error(str(path)), f"fixture drift: not read-denied: {path}"
            assert fs.is_write_denied(str(path)), f"write allowed: {path}"


def test_control_files_and_lookalikes_outside_home_stay_writable(hermes_layout, tmp_path):
    root, profile = hermes_layout
    for base in (profile, root):
        for rel in WRITABLE_CONTROL_FILES:
            assert fs.is_write_denied(str(_touch(base, rel))) is False, f"#45947 regression: {rel}"
    assert fs.is_write_denied(str(_touch(tmp_path / "myproject", "cache/bws_cache.json"))) is False
    assert fs.is_write_denied(str(_touch(tmp_path / "myproject", "vault/vault.key"))) is False


def _credential_layout(home: Path, tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """``(path as the caller spells it, file the write would land in)`` for a gh credential."""
    credential = home / ".config" / "gh" / "hosts.yml"
    if layout == "parent":
        outside = tmp_path / "outside-gh"
        outside.mkdir()
        credential.parent.parent.mkdir(parents=True)
        credential.parent.symlink_to(outside, target_is_directory=True)
        credential.write_text("dummy", encoding="utf-8")
        return credential, outside / "hosts.yml"
    credential.parent.mkdir(parents=True)
    if layout == "leaf":
        target = _touch(tmp_path / "outside", "hosts.yml")
        credential.symlink_to(target)
        return credential, target
    if layout in ("nested-parent", "nested-new"):
        outside = tmp_path / "outside-gh"
        outside.mkdir()
        (credential.parent / "nested").symlink_to(outside, target_is_directory=True)
        if layout == "nested-parent":
            _touch(outside, "hosts.yml")
        return credential.parent / "nested" / "hosts.yml", outside / "hosts.yml"
    credential.write_text("dummy", encoding="utf-8")
    if layout == "into-credential":
        alias = tmp_path / "unrelated.yml"
        alias.symlink_to(credential)
        return alias, credential
    return credential, credential


@pytest.mark.parametrize("layout", _layouts(*CREDENTIAL_LAYOUTS))
def test_credential_symlinks_are_write_denied(scratch_account_home, tmp_path, layout):
    credential, target = _credential_layout(scratch_account_home, tmp_path, layout)

    assert fs.is_write_denied(str(credential)), layout
    error = fs.get_write_denied_error(str(credential))
    assert error and "credential" in error
    assert target.read_text(encoding="utf-8") == "dummy"


def _v4a(op: str, path: Path, body: str = "") -> str:
    return f"*** Begin Patch\n*** {op} File: {path}\n{body}*** End Patch"


_TOOL_CALLS = {
    "write_file": lambda path: _tools().write_file_tool(str(path), "PWNED\n"),
    "patch": lambda path: _tools().patch_tool(path=str(path), old_string="dummy", new_string="PWNED"),
    "patch-v4a-update": lambda path: _tools().patch_tool(
        mode="patch", patch=_v4a("Update", path, "@@\n-dummy\n+PWNED\n")),
    "patch-v4a-delete": lambda path: _tools().patch_tool(mode="patch", patch=_v4a("Delete", path)),
}


def _tools():
    from tools import file_tools

    return file_tools


@pytest.mark.parametrize("tool", list(_TOOL_CALLS))
@pytest.mark.parametrize("layout", _layouts(*CREDENTIAL_LAYOUTS, "nested-new"))
def test_file_tools_refuse_credential_writes_through_symlinks(scratch_account_home, tmp_path, layout, tool):
    """The real tool entry points resolve the path before the backend guard sees it, so the
    credential spelling must be refused there too: the write never reaches the target."""
    spelled, target = _credential_layout(scratch_account_home, tmp_path, layout)
    existed = target.exists()

    result = json.loads(_TOOL_CALLS[tool](spelled))

    assert "credential" in (result.get("error") or ""), (layout, tool, result)
    if existed:
        assert target.read_text(encoding="utf-8") == "dummy"
    else:
        assert not target.exists()


@_SYMLINKS
def test_file_tools_still_write_through_unrelated_symlinks(tmp_path):
    allowed = tmp_path / "allowed"
    target = _touch(allowed, "notes.txt")
    link = tmp_path / "links"
    link.symlink_to(allowed, target_is_directory=True)

    patched = json.loads(_TOOL_CALLS["patch"](link / "notes.txt"))
    written = json.loads(_tools().write_file_tool(str(link / "new.txt"), "fresh\n"))

    assert not patched.get("error") and not written.get("error"), (patched, written)
    assert target.read_text(encoding="utf-8") == "PWNED"
    assert (allowed / "new.txt").read_text(encoding="utf-8") == "fresh\n"


@_SYMLINKS
@pytest.mark.parametrize("safe_root", [False, True])
def test_unrelated_symlink_remains_writable(tmp_path, monkeypatch, safe_root):
    target = _touch(tmp_path / "allowed", "notes.txt")
    alias = tmp_path / "notes-link.txt"
    alias.symlink_to(target)
    if safe_root:
        monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(target.parent))
    assert fs.is_write_denied(str(alias)) is False
    assert fs.get_write_denied_error(str(alias)) is None


@_SYMLINKS
@pytest.mark.parametrize("layout", ["leaf", "parent"])
def test_ssh_config_symlink_stays_approval_gated(scratch_account_home, tmp_path, layout):
    config = scratch_account_home / ".ssh" / "config"
    outside = tmp_path / "ssh-config-outside"
    if layout == "parent":
        outside.mkdir()
        config.parent.symlink_to(outside, target_is_directory=True)
        config.write_text("dummy", encoding="utf-8")
    else:
        config.parent.mkdir()
        config.symlink_to(_touch(tmp_path / "outside", "config"))
    assert fs.is_write_denied(str(config)) is False
    assert fs.is_write_approval_required(str(config)) is True


class TestProfileHomeProcessHome:
    """With the process HOME pinned to ``{HERMES_HOME}/home`` (TERMINAL_HOME_MODE=profile,
    containers, spawned workers) the write guards must still cover every home a write can
    land in: the OS user's real home, the profile home and ``~name`` accounts."""

    @pytest.fixture(params=_layouts("ordinary", "leaf", "parent"))
    def profile_home_env(self, tmp_path, monkeypatch, scratch_account_home, request):
        credential = scratch_account_home / ".config" / "gh" / "hosts.yml"
        if request.param == "parent":
            outside = tmp_path / "outside-gh"
            outside.mkdir()
            credential.parent.parent.mkdir(parents=True)
            credential.parent.symlink_to(outside, target_is_directory=True)
        else:
            credential.parent.mkdir(parents=True)
        if request.param == "leaf":
            credential.symlink_to(_touch(tmp_path / "outside", "hosts.yml"))
        else:
            credential.write_text("dummy", encoding="utf-8")
        profile = tmp_path / "profile"
        (profile / "home").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HOME", str(profile / "home"))
        monkeypatch.setattr(fs, "_hermes_home_path", lambda: profile)
        monkeypatch.setattr(fs, "_hermes_root_path", lambda: profile.parent)
        return profile

    def test_every_home_is_guarded(self, profile_home_env):
        import pwd

        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for rel in (".aws/credentials", ".ssh/id_ed25519", ".netrc", ".config/gh/hosts.yml"):
            assert fs.is_write_denied(str(real_home / rel)), rel
            assert fs.is_write_denied(str(profile_home_env / "home" / rel)), rel
        assert fs.is_write_denied("~/.aws/credentials")
        assert fs.is_write_denied("~root/.ssh/authorized_keys")
        # ``~/.ssh/config`` stays approval-gated (not hard-denied) on the real home too.
        assert fs.is_write_approval_required(str(real_home / ".ssh" / "config"))
        assert fs.is_write_denied(str(real_home / ".ssh" / "config")) is False

    def test_benign_paths_stay_writable(self, profile_home_env, tmp_path):
        import pwd

        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        for benign in (tmp_path / "scratch" / "notes.txt", real_home / "projects" / "notes.md"):
            assert fs.is_write_denied(str(benign)) is False, benign
            assert fs.is_write_approval_required(str(benign)) is False, benign
        assert fs.is_write_denied("~nosuchuser-hopefully/.ssh/authorized_keys") is False
