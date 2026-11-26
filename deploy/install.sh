#!/usr/bin/env bash
# Install or update laser-server on a Raspberry Pi (Raspberry Pi OS / Debian).
# Run from the repo checkout:  ./deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"
HOME_DIR="$HOME"

echo "==> Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-opencv python3-serial v4l-utils fonts-dejavu-core fonts-liberation python3-pil python3-numpy

echo "==> Adding $USER_NAME to dialout (serial) and video (camera) groups"
sudo usermod -aG dialout,video "$USER_NAME"

echo "==> Creating virtualenv"
# System site packages so the apt-installed OpenCV is used (no slow pip build on the Pi).
python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"

mkdir -p "$HOME_DIR/laser-jobs"

if [ ! -f /etc/laser-server.env ]; then
  echo "==> Writing /etc/laser-server.env"
  sudo cp "$DIR/deploy/laser-server.env" /etc/laser-server.env
fi

echo "==> Installing systemd service"
sed -e "s#__USER__#$USER_NAME#g" -e "s#__DIR__#$DIR#g" -e "s#__HOME__#$HOME_DIR#g" \
  "$DIR/deploy/laser-server.service" | sudo tee /etc/systemd/system/laser-server.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable laser-server >/dev/null
sudo systemctl restart laser-server

sleep 2
sudo systemctl --no-pager --lines=5 status laser-server || true
PORT="$(grep -E '^LASER_HTTP_PORT=' /etc/laser-server.env | cut -d= -f2)"
echo
echo "Open http://$(hostname).local:${PORT:-8000}/"
