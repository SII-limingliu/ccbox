"""Mount management — adding/removing disk devices on containers."""

from __future__ import annotations

import os
import re

from ccbox import lxd
from ccbox.config import Config, MountEntry


def device_name_from_path(path: str) -> str:
    """Sanitize a path into an LXD device name.

    /home/zj/Projects/X -> mount-home-zj-Projects-X
    """
    clean = path.strip("/")
    clean = re.sub(r"[^a-zA-Z0-9_.-]", "-", clean)
    return f"mount-{clean}"


def _ensure_path_exists(path: str) -> None:
    """Create directory if it doesn't exist. Skip for files."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def add_mount(
    config: Config,
    sandbox_name: str,
    path: str,
    readonly: bool = False,
) -> None:
    """Add a mount to a sandbox (both LXD device and config state)."""
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        raise ValueError(f"Sandbox '{sandbox_name}' not found")

    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        raise ValueError(f"Path does not exist: {resolved}")

    mode = "ro" if readonly else "rw"
    dev_name = device_name_from_path(resolved)

    # Add LXD disk device (identity-mapped: host path = container path)
    lxd.add_disk_device(
        entry.container, dev_name, resolved, resolved, readonly=readonly,
    )

    # Update config
    # Remove existing mount for same path if any
    entry.mounts = [m for m in entry.mounts if os.path.realpath(m.path) != resolved]
    entry.mounts.append(MountEntry(path=resolved, mode=mode))
    config.set_sandbox(sandbox_name, entry)


def remove_mount(config: Config, sandbox_name: str, path: str) -> None:
    """Remove a mount from a sandbox."""
    entry = config.get_sandbox(sandbox_name)
    if entry is None:
        raise ValueError(f"Sandbox '{sandbox_name}' not found")

    resolved = os.path.realpath(path)
    dev_name = device_name_from_path(resolved)

    lxd.remove_disk_device(entry.container, dev_name)

    entry.mounts = [m for m in entry.mounts if os.path.realpath(m.path) != resolved]
    config.set_sandbox(sandbox_name, entry)


def add_auto_mounts(container: str, config: Config | None = None) -> None:
    """Add auto-mounts to a container. Reads from config if provided."""
    if config is not None:
        mounts = config.state.get_auto_mounts()
    else:
        from ccbox.config import _default_auto_mounts
        mounts = _default_auto_mounts()

    for m in mounts:
        resolved = os.path.realpath(m.path)
        # Create directory stubs for dirs that don't exist yet
        if not os.path.exists(resolved):
            os.makedirs(resolved, exist_ok=True)
        dev_name = device_name_from_path(resolved)
        lxd.add_disk_device(
            container, dev_name, resolved, resolved, readonly=(m.mode == "ro"),
        )
