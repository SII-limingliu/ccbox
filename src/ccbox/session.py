"""Tmux session lifecycle inside LXD containers."""

from __future__ import annotations

import os
import shlex

from ccbox import lxd

CONTAINER_USER = "1000"  # UID for the mapped user
TMUX_CONF = "/etc/tmux.conf"


def list_sessions(container: str) -> list[dict]:
    r = lxd.exec_cmd(
        container,
        ["tmux", "list-sessions", "-F", "#{session_name}|#{session_attached}|#{session_created}"],
        user=CONTAINER_USER, capture=True, check=False,
    )
    if r.returncode != 0:
        return []
    sessions = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            sessions.append({
                "name": parts[0],
                "attached": int(parts[1]) > 0,
                "created": parts[2],
            })
    return sessions


def detached_sessions(container: str) -> list[dict]:
    return [s for s in list_sessions(container) if not s["attached"]]


def next_session_name(container: str) -> str:
    existing = list_sessions(container)
    used = set()
    for s in existing:
        name = s["name"]
        if name.startswith("s-"):
            try:
                used.add(int(name[2:]))
            except ValueError:
                pass
    n = 0
    while n in used:
        n += 1
    return f"s-{n}"


def _ensure_onboarding_complete(container: str, home: str) -> None:
    """Patch $CLAUDE_CONFIG_DIR/.claude.json so Claude skips onboarding.

    Claude may overwrite this file during a session, dropping the
    hasCompletedOnboarding flag. We re-inject it before every new session.
    """
    import json
    config_json = f"{home}/.claude/.claude.json"
    r = lxd.exec_cmd(
        container, ["cat", config_json],
        user=CONTAINER_USER, capture=True, check=False,
    )
    if r.returncode != 0:
        return
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return
    if data.get("hasCompletedOnboarding") is True:
        return
    rv = lxd.exec_cmd(
        container, [f"{home}/.local/bin/claude", "--version"],
        user=CONTAINER_USER, capture=True, check=False,
    )
    version = rv.stdout.strip().split()[0] if rv.returncode == 0 else "0"
    data["hasCompletedOnboarding"] = True
    data["lastOnboardingVersion"] = version
    data.setdefault("numStartups", 100)
    data.setdefault("hasSeenTasksHint", True)
    payload = json.dumps(data)
    lxd.exec_cmd(
        container,
        ["bash", "-c", f"cat > {config_json} << 'CCBOX_EOF'\n{payload}\nCCBOX_EOF"],
        user=CONTAINER_USER, check=False,
    )


def create_session(
    container: str,
    command: str,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    session_name: str | None = None,
) -> str:
    if session_name is None:
        session_name = next_session_name(container)
    if env is None:
        env = {}

    from pathlib import Path
    env.setdefault("HOME", str(Path.home()))

    import getpass
    env.setdefault("USER", getpass.getuser())
    env.setdefault("LOGNAME", env["USER"])
    env.setdefault("CLAUDE_CONFIG_DIR", f"{env['HOME']}/.claude")

    from ccbox.config import UV_SOCK
    env.setdefault("UV_HARDLINK_SOCKET", str(UV_SOCK))

    _ensure_onboarding_complete(container, env.get("HOME", str(Path.home())))

    tmux_args = ["tmux", "-f", TMUX_CONF, "new-session", "-d", "-s", session_name]
    if cwd:
        tmux_args += ["-c", cwd]

    lxd.exec_cmd(container, tmux_args, user=CONTAINER_USER, env=env)
    lxd.exec_cmd(
        container,
        ["tmux", "send-keys", "-t", session_name, f"exec {command}", "Enter"],
        user=CONTAINER_USER,
    )
    return session_name


def attach_session(container: str, session_name: str) -> None:
    lxd.exec_interactive(
        container,
        ["tmux", "-f", TMUX_CONF, "attach-session", "-t", session_name],
        user=CONTAINER_USER,
    )
    print(f"Detached from session '{session_name}'.")


def kill_session(container: str, name: str) -> None:
    lxd.exec_cmd(container, ["tmux", "kill-session", "-t", name],
                 user=CONTAINER_USER, check=False)


def kill_all_sessions(container: str) -> None:
    lxd.exec_cmd(container, ["tmux", "kill-server"],
                 user=CONTAINER_USER, check=False)


def build_claude_command(extra_args: list[str] | None = None) -> str:
    parts = ["claude", "--dangerously-skip-permissions"]
    if extra_args:
        for arg in extra_args:
            if arg in ("--allow-dangerously-skip-permissions",
                       "--dangerously-skip-permissions"):
                continue
            parts.append(arg)
    return shlex.join(parts)


def _find_codex() -> str | None:
    import glob
    import shutil
    matches = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/codex"))
    if matches:
        return matches[0]
    return shutil.which("codex")


def build_codex_command(extra_args: list[str] | None = None) -> str:
    codex_path = _find_codex()
    if codex_path:
        codex_dir = os.path.dirname(codex_path)
        nvm_bin = codex_dir if "/.nvm/" in codex_path else None
        parts = [codex_path, "--yolo"]
    else:
        nvm_bin = None
        parts = ["codex", "--yolo"]
    if extra_args:
        for arg in extra_args:
            if arg in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
                continue
            parts.append(arg)
    cmd = shlex.join(parts)
    if nvm_bin:
        cmd = f"env PATH={shlex.quote(nvm_bin)}:$PATH {cmd}"
    return cmd


def get_forwarded_env(whitelist: list[str]) -> dict[str, str]:
    result = {}
    for var in whitelist:
        val = os.environ.get(var)
        if val is not None:
            result[var] = val
    return result
