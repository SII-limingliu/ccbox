"""Sandbox lifecycle — create, start, stop, remove, list."""

from __future__ import annotations

import os
import subprocess
import sys

import importlib.resources

from ccbox import lxd
from ccbox.config import Config, SandboxEntry
from ccbox.mount import add_auto_mounts, add_mount, ensure_uv_shim, ensure_profile_script, fix_mount_parents, prune_stale_mounts
from ccbox.session import list_sessions
from ccbox.uv_server import ensure_server_running

CONTAINER_PREFIX = "ccbox-"
BASE_IMAGE = "ccbox-base"
IDMAP_VALUE = "both 1000 1000"

IS_ROOT = os.getuid() == 0


def _setup_host_acls(paths: list[str]) -> None:
    """Grant UID 1000 ACL access to host paths for privileged container mode."""
    if not IS_ROOT:
        return
    home = os.path.expanduser("~")
    subprocess.run(["setfacl", "-m", "u:1000:x", home],
                   check=False, capture_output=True)
    for p in paths:
        if not os.path.exists(p):
            continue
        if os.path.isdir(p):
            subprocess.run(["setfacl", "-R", "-m", "u:1000:rwx,m::rwx", p],
                           check=False, capture_output=True)
            subprocess.run(["setfacl", "-R", "-d", "-m", "u:1000:rwx,m::rwx", p],
                           check=False, capture_output=True)
        else:
            subprocess.run(["setfacl", "-m", "u:1000:rw,m::rw", p],
                           check=False, capture_output=True)
    parents: set[str] = set()
    for p in paths:
        parent = os.path.dirname(p)
        while parent and parent != "/" and parent != home:
            parents.add(parent)
            parent = os.path.dirname(parent)
    for parent in sorted(parents):
        if os.path.exists(parent):
            subprocess.run(["setfacl", "-m", "u:1000:rx", parent],
                           check=False, capture_output=True)


def _fix_privileged_networking(cname: str) -> None:
    """Fix IPv4 networking in privileged containers where systemd-networkd fails."""
    import re
    import time
    time.sleep(2)
    r = lxd.exec_cmd(cname, ["ip", "-4", "addr", "show", "eth0"], capture=True, check=False)
    if "inet " in (r.stdout or ""):
        return
    r2 = subprocess.run(["ip", "-4", "addr", "show", "lxdbr0"],
                        capture_output=True, text=True)
    match = re.search(r"inet (\d+\.\d+\.\d+)\.\d+/(\d+)", r2.stdout)
    if not match:
        return
    subnet = match.group(1)
    prefix = match.group(2)
    container_ip = f"{subnet}.200/{prefix}"
    gateway = f"{subnet}.1"
    lxd.exec_cmd(cname, ["ip", "addr", "add", container_ip, "dev", "eth0"], check=False)
    lxd.exec_cmd(cname, ["ip", "route", "add", "default", "via", gateway], check=False)
    lxd.exec_cmd(cname, ["bash", "-c",
        "echo 'nameserver 8.8.8.8' > /etc/resolv.conf"], check=False)
    print(f"Fixed privileged container networking: {container_ip} via {gateway}")


def _push_known_hosts(cname: str) -> None:
    """Push bundled SSH known_hosts into the container."""
    import tempfile
    from pathlib import Path
    home = str(Path.home())
    asset_ref = importlib.resources.files("ccbox").parent.parent / "assets" / "ssh_known_hosts"
    content = asset_ref.read_text()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".known_hosts", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        lxd.exec_cmd(cname, ["mkdir", "-p", f"{home}/.ssh"], user="1000", check=False)
        lxd.push_file(cname, tmp, f"{home}/.ssh/known_hosts", uid=1000, gid=1000, mode="0644")
    finally:
        os.unlink(tmp)


def _push_claude_settings(cname: str) -> None:
    """Copy ~/.claude.json and patch onboarding state for container use."""
    import json
    import tempfile
    from pathlib import Path
    home = str(Path.home())

    settings_file = f"{home}/.claude.json"
    if os.path.exists(settings_file):
        lxd.push_file(cname, settings_file, f"{home}/.claude.json",
                      uid=1000, gid=1000, mode="0644")

    config_dir_json = f"{home}/.claude/.claude.json"
    if not os.path.exists(config_dir_json):
        return
    try:
        with open(config_dir_json) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    r = lxd.exec_cmd(cname, [f"{home}/.local/bin/claude", "--version"],
                     user="1000", capture=True, check=False)
    version = r.stdout.strip().split()[0] if r.returncode == 0 else "2.1.74"

    data["hasCompletedOnboarding"] = True
    data["lastOnboardingVersion"] = version
    data.setdefault("numStartups", 100)
    data.setdefault("hasSeenTasksHint", True)
    data.setdefault("installMethod", "native")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = f.name
    try:
        lxd.push_file(cname, tmp, config_dir_json,
                      uid=1000, gid=1000, mode="0644")
    finally:
        os.unlink(tmp)


def container_name(sandbox_name: str) -> str:
    return f"{CONTAINER_PREFIX}{sandbox_name}"


def create_sandbox(
    config: Config,
    name: str,
    mounts: list[tuple[str, bool]] | None = None,
) -> str:
    cname = container_name(name)
    if config.get_sandbox(name) is not None:
        raise ValueError(f"Sandbox '{name}' already exists")
    if not lxd.image_exists(BASE_IMAGE):
        print("Base image not found. Run 'ccbox init' first.", file=sys.stderr)
        raise SystemExit(1)

    ensure_uv_shim()
    ensure_profile_script()
    ensure_server_running()

    lxd.init_container(BASE_IMAGE, cname, storage=config.state.storage_pool)

    if IS_ROOT:
        lxd.set_config(cname, "security.privileged", "true")
        acl_paths = [m.path for m in config.state.get_auto_mounts()
                     if os.path.exists(m.path)]
        _setup_host_acls(acl_paths)
    else:
        lxd.set_config(cname, "raw.idmap", IDMAP_VALUE)

    add_auto_mounts(cname, config)

    entry = SandboxEntry(container=cname)
    config.set_sandbox(name, entry)

    if mounts:
        for path, readonly in mounts:
            if IS_ROOT and not readonly:
                _setup_host_acls([path])
            add_mount(config, name, path, readonly)

    lxd.start(cname)

    if IS_ROOT:
        lxd.exec_cmd(cname, ["chmod", "755", "/root"], check=False)
        _fix_privileged_networking(cname)

    _push_known_hosts(cname)
    _push_claude_settings(cname)
    fix_mount_parents(cname, config)

    return cname


def ensure_running(config: Config, name: str) -> str:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    prune_stale_mounts(config, name)
    state = lxd.container_state(entry.container)
    if state == "NotFound":
        config.remove_sandbox(name)
        raise ValueError(f"Container for sandbox '{name}' no longer exists. Removed from config.")
    if state == "Stopped":
        ensure_uv_shim()
        ensure_profile_script()
        ensure_server_running()
        lxd.start(entry.container)
        if IS_ROOT:
            _fix_privileged_networking(entry.container)
        _push_claude_settings(entry.container)
    elif state == "Running" and IS_ROOT:
        # Always verify networking — Docker may reset iptables at any time,
        # and privileged containers lose DNS/IP on network disruption.
        _fix_privileged_networking(entry.container)
    return entry.container


def stop_sandbox(config: Config, name: str) -> None:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    state = lxd.container_state(entry.container)
    if state == "Running":
        lxd.stop(entry.container)


def remove_sandbox(config: Config, name: str) -> None:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    if lxd.container_exists(entry.container):
        lxd.delete(entry.container, force=True)
    config.remove_sandbox(name)


def list_sandboxes(config: Config) -> list[dict]:
    result = []
    stale = []
    for name, entry in config.state.sandboxes.items():
        state = lxd.container_state(entry.container)
        if state == "NotFound":
            stale.append(name)
            continue
        sessions = 0
        if state == "Running":
            sessions = len(list_sessions(entry.container))
        result.append({
            "name": name,
            "container": entry.container,
            "state": state,
            "sessions": sessions,
            "mounts": len(entry.mounts),
        })
    for name in stale:
        print(f"Warning: sandbox '{name}' container no longer exists. Removing from config.",
              file=sys.stderr)
        config.remove_sandbox(name)
    return result


def sandbox_status(config: Config, name: str) -> dict:
    entry = config.get_sandbox(name)
    if entry is None:
        raise ValueError(f"Sandbox '{name}' not found")
    state = lxd.container_state(entry.container)
    sessions = []
    if state == "Running":
        sessions = list_sessions(entry.container)
    return {
        "name": name,
        "container": entry.container,
        "state": state,
        "sessions": sessions,
        "mounts": [m.to_dict() for m in entry.mounts],
    }


def resolve_sandbox(config: Config, name: str | None) -> str:
    if name is not None:
        if config.get_sandbox(name) is None:
            raise ValueError(f"Sandbox '{name}' not found")
        return name
    found = config.sandbox_for_path(os.getcwd())
    if found is not None:
        return found
    raise ValueError("No sandbox specified and none found for current directory")


def auto_sandbox_name_from_cwd() -> str:
    base = os.path.basename(os.getcwd())
    sanitized = ""
    for c in base:
        if c.isalnum() or c in "-_":
            sanitized += c
    return sanitized or "default"
