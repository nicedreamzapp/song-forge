#!/usr/bin/env bash
# forge_supervisor.sh — boots all three local servers for Song Forge:
#   8765 = Living Loop Lab (static)
#   8766 = AI Music Radio (radio_supervisor.sh)
#   8767 = Song Forge hybrid page (this app)
#
# Idempotent: each port is started only if nothing is listening.
set -u

LAB_DIR="$HOME/Desktop/PROJECTS/Living Loop Lab"
RADIO_DIR="$HOME/Desktop/PROJECTS/AI Music Radio"
FORGE_DIR="$HOME/Desktop/PROJECTS/Song Forge"

start_if_free() {
  local port="$1" cmd="$2" log="$3"
  if /usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[forge] :$port already up"
    return 0
  fi
  echo "[forge] booting :$port — $cmd"
  /usr/bin/nohup /bin/bash -c "$cmd" </dev/null >>"$log" 2>&1 &
}

# Loop Lab (8765) and AI Music Radio (8766) used to be embedded in v1's iframe
# hybrid. v2 dropped that; those apps still live as standalone Desktop launchers.
# We no longer boot them from here.

# 1) ACE-Step API server (port 8001) — local song-with-vocals model
ACE_DIR="$FORGE_DIR/engines/ACE-Step-1.5"
if [ -x "$ACE_DIR/start_api_server_macos.sh" ]; then
  start_if_free 8001 \
    "cd '$ACE_DIR' && ACESTEP_NO_INIT=true exec '$ACE_DIR/start_api_server_macos.sh'" \
    /tmp/song_forge_ace.log
fi

# 4) Song Forge backend (port 8767) — UI + ACE-Step orchestrator
start_if_free 8767 \
  "cd '$FORGE_DIR' && exec /usr/bin/python3 '$FORGE_DIR/forge_server.py'" \
  /tmp/song_forge_hub.log

# Wait for Forge hub to respond before returning.
for i in $(seq 1 20); do
  if /usr/bin/curl -s -o /dev/null http://localhost:8767/; then
    exit 0
  fi
  sleep 0.25
done
exit 0
