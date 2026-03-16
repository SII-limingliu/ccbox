"""Base image creation — ccbox init command."""

from __future__ import annotations

import getpass
import importlib.resources
import os
import shlex
import sys

from ccbox import lxd
from ccbox.mount import add_auto_mounts, ensure_profile_script
from ccbox.sandbox import IS_ROOT, _setup_host_acls

TEMP_CONTAINER = "ccbox-init-temp"
BASE_IMAGE = "ccbox-base"
BASE_OS_IMAGE = "ubuntu:24.04"
IDMAP_VALUE = "both 1000 1000"


def _asset_path(name: str) -> str:
    ref = importlib.resources.files("ccbox").parent.parent / "assets" / name
    return str(ref)


def _init_env() -> dict[str, str]:
    from pathlib import Path
    home = str(Path.home())
    return {
        "HOME": home,
        "CLAUDE_CONFIG_DIR": f"{home}/.claude",
        "PATH": f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }


def check_prerequisites() -> None:
    r = lxd.run_lxc("version", check=False, capture=True)
    if r.returncode != 0:
        print("Error: Cannot access LXD.", file=sys.stderr)
        raise SystemExit(1)


def _bootstrap(container: str, username: str) -> None:
    """Bootstrap container user and directory structure."""
    r = lxd.exec_cmd(container, ["id", "-un", "1000"], capture=True, check=False)
    existing = r.stdout.strip() if r.returncode == 0 else ""

    if username == "root":
        container_username = existing or "ubuntu"
        home_dir = "/root"
        lxd.exec_cmd(container, ["chmod", "755", "/root"], check=False)
    else:
        container_username = username
        if existing and existing != username:
            lxd.exec_cmd(container, ["pkill", "-u", existing], check=False, capture=True)
            lxd.exec_cmd(container, ["usermod", "-l", username, "-d", f"/home/{username}",
                                      "-m", existing])
            lxd.exec_cmd(container, ["groupmod", "-n", username, existing])
        elif not existing:
            lxd.exec_cmd(container, ["useradd", "-m", "-s", "/bin/bash", username])
        home_dir = f"/home/{container_username}"

    lxd.exec_cmd(container, ["bash", "-c",
        f"echo '{container_username} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/{container_username} "
        f"&& chmod 0440 /etc/sudoers.d/{container_username}"])

    dirs = [".local/bin", ".local/share/claude", ".cache/uv", ".claude", ".config/ccbox"]
    for d in dirs:
        lxd.exec_cmd(container, ["mkdir", "-p", f"{home_dir}/{d}"])
    lxd.exec_cmd(container, ["chown", "-R", "1000:1000", home_dir], check=False)

    bashrc_snippet = (
        '\n# ccbox\n'
        '[ -f ~/.config/ccbox/profile.sh ] && . ~/.config/ccbox/profile.sh\n'
    )
    lxd.exec_cmd(container, ["bash", "-c",
        f"echo {shlex.quote(bashrc_snippet)} >> {home_dir}/.bashrc"])


def run_init(force: bool = False, storage_pool: str | None = None) -> None:
    check_prerequisites()

    if lxd.image_exists(BASE_IMAGE) and not force:
        print(f"Base image '{BASE_IMAGE}' already exists. Use 'ccbox init --force' to rebuild.")
        return

    username = getpass.getuser()
    container_user = "1000"
    init_env = _init_env()

    if lxd.container_exists(TEMP_CONTAINER):
        print(f"Cleaning up leftover '{TEMP_CONTAINER}'...")
        lxd.delete(TEMP_CONTAINER, force=True)

    try:
        print(f"Creating temporary container from {BASE_OS_IMAGE}...")
        launch_args = ["launch", BASE_OS_IMAGE, TEMP_CONTAINER]
        if storage_pool:
            launch_args += ["-s", storage_pool]
        lxd.run_lxc(*launch_args)

        print("Waiting for container to be ready...")
        lxd.exec_cmd(TEMP_CONTAINER, ["cloud-init", "status", "--wait"],
                      check=False, capture=True)

        if IS_ROOT:
            lxd.set_config(TEMP_CONTAINER, "security.privileged", "true")
            from ccbox.config import Config
            config = Config()
            acl_paths = [m.path for m in config.state.get_auto_mounts()
                         if os.path.exists(m.path)]
            _setup_host_acls(acl_paths)
        else:
            lxd.set_config(TEMP_CONTAINER, "raw.idmap", IDMAP_VALUE)

        print(f"Bootstrapping user '{username}'...")
        _bootstrap(TEMP_CONTAINER, username)

        tmux_conf = _asset_path("tmux.conf")
        lxd.push_file(TEMP_CONTAINER, tmux_conf, "/etc/tmux.conf", mode="0644")

        print("Restarting to apply configuration...")
        lxd.stop(TEMP_CONTAINER)
        ensure_profile_script()
        add_auto_mounts(TEMP_CONTAINER)
        lxd.start(TEMP_CONTAINER)

        if IS_ROOT:
            from ccbox.sandbox import _fix_privileged_networking
            _fix_privileged_networking(TEMP_CONTAINER)

        print("Testing claude binary...")
        r = lxd.exec_cmd(
            TEMP_CONTAINER,
            ["bash", "-lc", "claude --version"],
            user=container_user, env=init_env,
            capture=True, check=False,
        )
        if r.returncode == 0:
            print(f"Claude: {r.stdout.strip()}")
        else:
            print("Warning: claude not found.")

        print()
        print("=" * 60)
        print("What should Claude install/configure in the base image?")
        print("Examples:")
        print("  - install tmux git curl build-essential python3")
        print("  - set apt source to mirrors.tuna.tsinghua.edu.cn first")
        print("  - install rust toolchain")
        print()
        print("Enter instructions (empty line to skip, Ctrl+D to finish multi-line):")
        print("=" * 60)

        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except EOFError:
            pass
        user_instructions = "\n".join(lines).strip()

        if user_instructions:
            prompt = (
                f"You are setting up an Ubuntu 24.04 container for development. "
                f"The user is '{username}' with sudo. "
                f"Follow these instructions:\n\n{user_instructions}\n\n"
                f"Run commands directly. Do not ask for confirmation."
            )
            print(f"\nStarting Claude inside the container...")
            lxd.exec_interactive(
                TEMP_CONTAINER,
                ["bash", "-lc",
                 f"claude --allow-dangerously-skip-permissions -p {shlex.quote(prompt)}"],
                user=container_user, env=init_env,
            )
        else:
            print("No instructions — skipping Claude setup.")

        print()
        print("=" * 60)
        print("Dropping into shell. Make any manual changes, then exit.")
        print("The container will be published as the base image.")
        print("=" * 60)
        print()

        lxd.exec_interactive(TEMP_CONTAINER, ["bash", "-l"],
                             user=container_user, env=init_env)

        print("\nPreparing to publish...")
        lxd.stop(TEMP_CONTAINER)

        r = lxd.run_lxc("config", "device", "list", TEMP_CONTAINER,
                          capture=True, check=False)
        if r.returncode == 0:
            for dev in r.stdout.strip().splitlines():
                dev = dev.strip()
                if dev and dev.startswith("mount-"):
                    lxd.remove_disk_device(TEMP_CONTAINER, dev)

        print(f"Publishing as '{BASE_IMAGE}'...")
        lxd.publish(TEMP_CONTAINER, BASE_IMAGE, force=force)
        print("Base image created successfully.")

    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up...")
    finally:
        if lxd.container_exists(TEMP_CONTAINER):
            print(f"Removing temporary container '{TEMP_CONTAINER}'...")
            lxd.delete(TEMP_CONTAINER, force=True)
