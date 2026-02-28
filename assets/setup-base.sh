#!/bin/bash
set -euo pipefail

# Setup script run inside the base container during ccbox init

export DEBIAN_FRONTEND=noninteractive

# Install packages
apt-get update
apt-get install -y \
    tmux git curl build-essential \
    python3 python3-venv sudo locales

# Set up locale
locale-gen en_US.UTF-8
update-locale LANG=en_US.UTF-8

# Create user with UID 1000 (matches host identity mapping)
USERNAME="zj"
if ! id "$USERNAME" &>/dev/null; then
    useradd -m -s /bin/bash -u 1000 "$USERNAME"
fi

# NOPASSWD sudo
echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/$USERNAME
chmod 0440 /etc/sudoers.d/$USERNAME

# Create directory stubs for mount points
sudo -u "$USERNAME" mkdir -p \
    /home/$USERNAME/.local/bin \
    /home/$USERNAME/.local/share/claude \
    /home/$USERNAME/.cache/uv \
    /home/$USERNAME/.claude

# Add ~/.local/bin to PATH in .bashrc
cat >> /home/$USERNAME/.bashrc << 'BASHRC'

# ccbox: add local bin to PATH
export PATH="$HOME/.local/bin:$PATH"

# ccbox: disable XON/XOFF so Ctrl+Q works for tmux detach
stty -ixon 2>/dev/null || true
BASHRC

chown $USERNAME:$USERNAME /home/$USERNAME/.bashrc

echo "Base container setup complete."
