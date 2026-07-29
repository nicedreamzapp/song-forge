#!/bin/bash
# m5_supervisor.sh — keeps the M5 Song Forge stack alive (2026-07-08).
# Mirrors the mini's forge_supervisor.sh: boot anything whose port is free.
# Run by com.nicedreamz.songforge-m5-stack every 120s + at login, so the
# music pipeline survives reboots and crashes without hand-holding.
set -u
FORGE_DIR="$HOME/SongForgeM5"
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
  "exec /usr/bin/caffeinate -ims '$MLX_VENV/bin/python' -m mlx_lm.server --model divinetribe/gemma-4-31b-it-abliterated-4bit-mlx --host 0.0.0.0 --port 9420" \
  /tmp/m5_gemma.log

# 2) ACE-Step renderer (:8001)
# NB: invoke via venv python, NOT the acestep-api launcher — the launcher's
# /bin/sh polyglot exec line hard-codes the old Desktop path, which launchd
# cannot traverse (TCC). Python treats that line as a docstring and skips it.
# ACESTEP_NO_INIT=false (Matt 2026-07-18): preload weights at boot so the
# first song after any reboot/power outage renders warm, same as the mini.
start_if_free 8001 \
  "cd '$ACE_DIR' && ACESTEP_LM_BACKEND=mlx ACESTEP_INIT_LLM=auto ACESTEP_NO_INIT=false TOKENIZERS_PARALLELISM=false PYTHONUNBUFFERED=1 exec /usr/bin/caffeinate -ims '$ACE_DIR/.venv/bin/python' '$ACE_DIR/.venv/bin/acestep-api' --host 0.0.0.0 --port 8001" \
  /tmp/m5_ace.log

# 3) Forge orchestrator (:8767)
start_if_free 8767 \
  "cd '$FORGE_DIR' && exec /usr/bin/caffeinate -ims /usr/bin/python3 '$FORGE_DIR/forge_server.py'" \
  /tmp/song_forge_hub_m5.log

# 3b) Memory authority (:8790). Owns the machine's memory policy so Song Forge
#     always has room to start a song, whatever else is running. Its own
#     KeepAlive agent (com.nicedreamz.forge-guard) is the primary; this is the
#     backstop for when that agent is unloaded.
start_if_free 8790 \
  "exec /usr/bin/python3 '$FORGE_DIR/forge_guard.py'" \
  /tmp/forge_guard.log

# 4) Idle-hog eviction (Matt 2026-07-09, after ComfyUI + an LM Studio worker
#    squatted on 64GB for two days, filled swap, and OOM'd a customer song).
#    When swap is critically full AND a known heavy model server sits at ~zero
#    CPU, quit it and log. Song Forge's own engines (ACE/Gemma/forge) exempt.
#    Superseded by forge_guard when it is up: the guard does the same eviction
#    on a 10s tick with a reservation behind it, so running both would double
#    up on the same processes. Only fall back to this when :8790 is silent.
if curl -s -m 3 -o /dev/null http://127.0.0.1:8790/api/state 2>/dev/null; then
  exit 0
fi
SWAP_USED_MB=$(sysctl -n vm.swapusage | sed -E 's/.*used = ([0-9.]+)M.*/\1/' | cut -d. -f1)
# Threshold lowered 40000→8000 (Matt 2026-07-18): evict idle non-forge model
# servers as soon as the box starts swapping at all, not once it's drowning.
if [ "${SWAP_USED_MB:-0}" -gt 8000 ]; then
  for PAT in "main.py --listen 127.0.0.1 --port 8188" ".lmstudio/.internal/utils/node" "ollama runner"; do
    # ComfyUI: skip eviction while its render queue is active. GPU-bound Wan
    # sampling shows ~0% CPU, so the CPU test alone SIGTERMed live renders
    # (2026-07-22, Story Forge Director drafts). Idle-with-empty-queue still dies.
    if [ "$PAT" = "main.py --listen 127.0.0.1 --port 8188" ]; then
      QLEN=$(curl -s -m 2 http://127.0.0.1:8188/queue | /usr/bin/python3 -c "import json,sys;q=json.load(sys.stdin);print(len(q['queue_running'])+len(q['queue_pending']))" 2>/dev/null)
      if [ "${QLEN:-0}" -gt 0 ]; then
        echo "[m5-stack] $(date '+%H:%M:%S') swap ${SWAP_USED_MB}MB — ComfyUI busy (queue=$QLEN), not evicting" >> /tmp/m5_stack_agent.log
        continue
      fi
    fi
    for PID in $(pgrep -f "$PAT" 2>/dev/null); do
      CPU=$(ps -o %cpu= -p "$PID" 2>/dev/null | tr -d ' ' | cut -d. -f1)
      if [ "${CPU:-0}" -lt 2 ]; then
        echo "[m5-stack] $(date '+%H:%M:%S') swap ${SWAP_USED_MB}MB — evicting idle hog pid=$PID ($PAT)" >> /tmp/m5_stack_agent.log
        kill "$PID" 2>/dev/null
      fi
    done
  done
fi

exit 0
