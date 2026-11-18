"""Profile-owned named execution targets, shared by tool adapters.

SSH aliases own addressing, keys and host trust. This registry never connects or
copies controller configuration to a node. Adapters consume one immutable target
snapshot so interleaved calls cannot change each other's destination.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import shlex
from typing import Any


DEFAULT_CUA_DRIVER = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


class NodeError(ValueError):
    """An explicit or configured target cannot be resolved. Never fall back."""


@dataclass(frozen=True)
class Node:
    name: str
    ssh: str
    platform: str
    # A capability's settings are stored once, beside the shared SSH identity.
    cua_driver: str = DEFAULT_CUA_DRIVER
    cua_permission_mode: str = "standard"

    def ssh_argv(self, *remote_args: str) -> list[str]:
        """OpenSSH reparses the remote command through the login shell."""
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ConnectionAttempts=1", "-o", "ServerAliveInterval=2",
                "-o", "ServerAliveCountMax=2", self.ssh, shlex.join(remote_args)]


def load_nodes() -> tuple[dict[str, Node], str | None]:
    from hermes_cli.config_effective import load_user_config_effective

    config = load_user_config_effective(fail_closed=True)
    cu = config.get("computer_use", {})
    if isinstance(cu, dict) and "nodes" in cu:
        raise NodeError("Move computer_use.nodes to the shared nodes.hosts registry and nodes.default")
    raw = config.get("nodes", {})
    if not isinstance(raw, dict):
        raise NodeError("nodes must be a mapping with default and hosts")
    hosts = raw.get("hosts", {})
    if not isinstance(hosts, dict):
        raise NodeError("nodes.hosts must be a mapping")
    nodes = {name: _parse_node(name, value) for name, value in hosts.items()}
    default = raw.get("default")
    if default is not None and (not isinstance(default, str) or default not in nodes):
        raise NodeError("nodes.default must name an entry in nodes.hosts")
    return nodes, default


def _parse_node(name: str, value: Any) -> Node:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise NodeError(f"Invalid node name: {name!r}")
    if not isinstance(value, dict):
        raise NodeError(f"nodes.hosts.{name} must be a mapping")
    alias = value.get("ssh")
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", alias):
        raise NodeError(f"nodes.hosts.{name}.ssh must be an SSH alias or user@host, not SSH options")
    platform = value.get("platform", "macos")
    if not isinstance(platform, str) or not platform:
        raise NodeError(f"nodes.hosts.{name}.platform must be a nonempty string")
    cu = value.get("computer_use", {})
    if not isinstance(cu, dict):
        raise NodeError(f"nodes.hosts.{name}.computer_use must be a mapping")
    driver = cu.get("driver", DEFAULT_CUA_DRIVER)
    if not isinstance(driver, str) or not driver.startswith("/") or any(ord(c) < 32 for c in driver):
        raise NodeError(f"nodes.hosts.{name}.computer_use.driver must be an absolute remote path")
    mode = cu.get("permission_mode", "standard")
    if mode not in ("standard", "unrestricted"):
        raise NodeError(f"nodes.hosts.{name}.computer_use.permission_mode must be standard or unrestricted")
    return Node(name, alias, platform, driver, mode)


def resolve_node(name: str | None = None) -> Node | None:
    """None means legacy local only when neither a node nor a default was selected."""
    nodes, default = load_nodes()
    if name is not None and (not isinstance(name, str) or not name):
        raise NodeError("node must be a nonempty configured name")
    selected = default if name is None else name
    if selected is None:
        return None
    if selected not in nodes:
        raise NodeError(f"Unknown node {selected!r}. Configured nodes: {', '.join(nodes) or '(none)'}")
    return nodes[selected]
