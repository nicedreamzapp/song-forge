#!/bin/bash
# m5_supervisor.sh — keeps the M5 Song Forge stack alive (2026-07-08).
# Mirrors the mini's forge_supervisor.sh: boot anything whose port is free.
# Run by com.nicedreamz.songforge-m5-stack every 120s + at login, so the
# music pipeline survives reboots and crashes without hand-holding.
set -u
FORGE_DIR="$HOME/Desktop/PROJECTS/Song Forge"
ACE_DIR="$FORGE_DIR/engines/ACE-Step-1.5"
MLX_VENV="$HOME/.local/share/mlx-server/.venv"

start_if_free() {
  local port="$1" cmd="$2" log="$3"
  if /usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  echo "[m5-stack] $(date '+%H:%M:%S') booting :$port" >> "$log"
  /usr/bin/nohup /bin/bash -c "$cmd" </dev/null >>"$log" 2>&1 &
}

# 1) Gemma lyric LLM (:9420) — no-think config lives in the forge payload
start_if_free 9420 \
  "exec /usr/bin/caffeinate -imdsu '$MLX_VENV/bin/python' -m mlx_lm.server --model divinetribe/gemma-4-31b-it-abliterated-4bit-mlx --host 0.0.0.0 --port 9420" \
  /tmp/m5_gemma.log

# 2) ACE-Step renderer (:8001)
start_if_free 8001 \
  "cd '$ACE_DIR' && ACESTEP_LM_BACKEND=mlx ACESTEP_INIT_LLM=auto ACESTEP_NO_INIT=true TOKENIZERS_PARALLELISM=false exec /usr/bin/caffeinate -imdsu '$ACE_DIR/.venv/bin/acestep-api' --host 0.0.0.0 --port 8001" \
  /tmp/m5_ace.log

# 3) Forge orchestrator (:8767)
start_if_free 8767 \
  "cd '$FORGE_DIR' && exec /usr/bin/caffeinate -imdsu /usr/bin/python3 '$FORGE_DIR/forge_server.py'" \
  /tmp/song_forge_hub_m5.log

exit 0
