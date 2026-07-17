#!/bin/bash
# Double-click launcher for macOS. First time: right-click -> Open
# (macOS warns about downloaded scripts from unidentified developers).
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "This game needs Python 3, which isn't installed yet."
  echo "Install it from https://www.python.org/downloads/"
  echo "(or run 'xcode-select --install' in Terminal), then try again."
  read -n1 -s -r -p "Press any key to close..."
  exit 1
fi

echo "Starting the trading game - your browser will open the host panel."
echo "Keep this window open while you play. Press Ctrl+C to stop."
echo

python3 server.py --open || {
  echo
  echo "The game server stopped with an error (see above)."
  read -n1 -s -r -p "Press any key to close..."
}
