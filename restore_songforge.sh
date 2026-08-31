#!/bin/bash
# Bring the M5's Song Forge node back after it was stopped for a heavy render.
# Written 2026-08-26 when the Hive Strike HD renders needed the whole box.
# Mirrors the shutdown in stop_songforge.sh; the mini serves customers alone
# while this node is down, so run this as soon as the render is finished.
set -u
echo "[restore] bootstrapping Song Forge stack on the M5..."
launchctl bootstrap gui/501 "$HOME/Library/LaunchAgents/com.nicedreamz.songforge-m5-stack.plist" 2>&1 | grep -v "already bootstrapped" || true
launchctl bootstrap gui/501 "$HOME/Library/LaunchAgents/com.nicedreamz.songforge-m5render-bridge-contabo.plist" 2>&1 | grep -v "already bootstrapped" || true
launchctl kickstart -k "gui/501/com.nicedreamz.songforge-m5-stack" 2>/dev/null || true

echo "[restore] waiting for the forge to answer on :8767 (up to 6 min, it pages 19GB back in)..."
for i in $(seq 1 72); do
  if curl -s -m 4 http://127.0.0.1:8767/api/status >/dev/null 2>&1; then
    echo "[restore] forge answering after ~$((i*5))s:"
    curl -s -m 5 http://127.0.0.1:8767/api/status
    echo
    exit 0
  fi
  sleep 5
done
echo "[restore] STILL DOWN after 6 min — check 'launchctl list | grep songforge' and ~/SongForgeM5/forge.log"
exit 1
