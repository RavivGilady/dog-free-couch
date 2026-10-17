#!/usr/bin/env bash
# Dog Free Couch camera agent installer for Linux and Raspberry Pi OS.
#
#   curl -fsSL https://YOUR-SERVER/install.sh | bash -s -- \
#       --server https://YOUR-SERVER --token dfc_xxxxxxxx
#
# What it does: installs the few system packages it needs, fetches the agent
# into ~/dog-free-couch, builds a private Python environment, downloads the
# detection model, saves your camera token, and (given sudo) installs a
# systemd service that starts at boot, restarts if it crashes, and updates
# itself each time it starts. Safe to run again: it updates in place.
#
# Options:
#   --server URL   the dashboard's address (required)
#   --token T      the camera token shown in the dashboard (required)
#   --dir PATH     where to install (default ~/dog-free-couch)
#   --repo URL     where to fetch the agent from
#   --pi           this is a Raspberry Pi using its ribbon-cable camera
#   --no-service   install only; print the command to run it by hand
#   --dry-run      print what would be done and change nothing

set -euo pipefail

REPO="https://github.com/RavivGilady/dog-free-couch.git"
DIR="$HOME/dog-free-couch"
SERVER=""
TOKEN=""
PI=0
SERVICE=1
DRY=0

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'Error: %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '[dry-run] %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server)     SERVER="${2:-}"; shift 2 ;;
    --token)      TOKEN="${2:-}"; shift 2 ;;
    --dir)        DIR="${2:-}"; shift 2 ;;
    --repo)       REPO="${2:-}"; shift 2 ;;
    --pi)         PI=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    --dry-run)    DRY=1; shift ;;
    -h|--help)    sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
    *)            die "unknown option: $1" ;;
  esac
done

[ -n "$SERVER" ] || die "missing --server (copy the full command from the dashboard)"
[ -n "$TOKEN" ]  || die "missing --token (copy the full command from the dashboard)"
SERVER="${SERVER%/}"
case "$SERVER" in http://*|https://*) ;; *) die "--server must start with https://" ;; esac
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux and Raspberry Pi. For Windows and macOS see $SERVER/help#agent"
[ "$(id -u)" != 0 ] || die "run this as your normal user, not root (it will ask for sudo when needed)"

SUDO=""
if command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

say "Installing system packages"
if command -v apt-get >/dev/null 2>&1 && [ -n "$SUDO" ]; then
  PKGS="git python3 python3-venv python3-pip libportaudio2 ffmpeg"
  [ "$PI" = 1 ] && PKGS="$PKGS python3-picamera2"
  run $SUDO apt-get update -qq
  run $SUDO apt-get install -y -qq $PKGS
else
  echo "Skipping apt (not available here). Make sure git, python3 with venv, and pip are installed."
fi
for c in git python3; do command -v "$c" >/dev/null 2>&1 || [ "$DRY" = 1 ] || die "$c is not installed"; done

say "Fetching the agent into $DIR"
if [ -d "$DIR/.git" ]; then
  run git -C "$DIR" pull --ff-only --autostash -q
else
  run git clone -q --depth 1 "$REPO" "$DIR"
fi

say "Setting up Python (a few minutes on a Raspberry Pi)"
VENV_ARGS=""
[ "$PI" = 1 ] && VENV_ARGS="--system-site-packages"
[ -d "$DIR/venv" ] || run python3 -m venv $VENV_ARGS "$DIR/venv"
run "$DIR/venv/bin/pip" install -q --upgrade pip
run "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

say "Downloading the detection model"
run "$DIR/venv/bin/python" "$DIR/download_model.py"

if [ "$PI" = 1 ]; then
  say "Selecting the Raspberry Pi camera"
  run sed -i 's/^\(\s*backend:\s*\).*/\1picamera2/' "$DIR/config.yaml"
fi

say "Saving the camera token"
if [ "$DRY" = 0 ]; then
  mkdir -p "$DIR/instance"
  umask 077
  printf '%s' "$TOKEN" > "$DIR/instance/device_token"
fi

START="$DIR/venv/bin/python $DIR/agent.py --server $SERVER"

if [ "$SERVICE" = 1 ] && [ -n "$SUDO" ] && command -v systemctl >/dev/null 2>&1; then
  say "Installing the background service"
  UNIT=/etc/systemd/system/dog-free-couch.service
  CONTENT="[Unit]
Description=Dog Free Couch camera agent
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
SupplementaryGroups=video audio
WorkingDirectory=$DIR
# Auto-update: each start pulls the latest agent. Failures (offline, local
# edits) are ignored so the agent still starts with what it has.
ExecStartPre=-/usr/bin/git -C $DIR pull --ff-only --autostash -q
ExecStartPre=-$DIR/venv/bin/pip install -q -r $DIR/requirements.txt
ExecStart=$START
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"
  if [ "$DRY" = 1 ]; then
    printf '[dry-run] write %s:\n%s\n' "$UNIT" "$CONTENT"
  else
    printf '%s' "$CONTENT" | $SUDO tee "$UNIT" >/dev/null
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now dog-free-couch.service
  fi
  say "Done. The camera should show as online in your dashboard within a minute."
  echo "  Watch its log:   journalctl -u dog-free-couch -f"
  echo "  Restart/update:  sudo systemctl restart dog-free-couch   (it updates itself on start)"
  echo "  Remove it:       sudo systemctl disable --now dog-free-couch && sudo rm $UNIT"
else
  say "Installed. Start the agent with:"
  echo "  $START"
  echo "(No background service was set up, so it stops when you close the terminal.)"
fi
