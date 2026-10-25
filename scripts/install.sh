#!/usr/bin/env bash
set -e
sudo apt-get update
sudo apt-get install -y mininet openvswitch-switch openvswitch-testcontroller iperf curl wget xterm unzip qrencode \
  python3-pip python3-venv python3-matplotlib
sudo systemctl enable --now openvswitch-switch || sudo service openvswitch-switch start
sudo systemctl disable --now openvswitch-testcontroller 2>/dev/null || true

if [ ! -x "$HOME/ryu-venv/bin/ryu-manager" ] || ! "$HOME/ryu-venv/bin/ryu-manager" --version >/dev/null 2>&1; then
  echo "Installing Ryu in a Python 3.9 environment (Ryu does not run on newer Python versions)"
  if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  UV=$(command -v uv || echo "$HOME/.local/bin/uv")
  rm -rf "$HOME/ryu-venv"
  "$UV" venv --python 3.9 --seed "$HOME/ryu-venv"
  "$HOME/ryu-venv/bin/pip" install "setuptools==67.6.1" wheel pbr
  "$HOME/ryu-venv/bin/pip" install --no-build-isolation ryu "eventlet==0.30.2" "dnspython<2"
fi
"$HOME/ryu-venv/bin/ryu-manager" --version

if ! command -v cloudflared >/dev/null; then
  echo "Installing cloudflared (for the public exam links)"
  ARCH=$(dpkg --print-architecture)
  if wget -q -O /tmp/cloudflared.deb "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}.deb"; then
    sudo dpkg -i /tmp/cloudflared.deb || echo "cloudflared install failed - the public link is optional"
  else
    echo "Could not download cloudflared - the public link is optional"
  fi
fi
echo "Done. Ryu is in ~/ryu-venv (run_controller.sh activates it for you)."
