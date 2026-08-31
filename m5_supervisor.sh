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

# 0) GENERATION probe for :9420 — a hung LLM still answers /health, which
# starved the whole forge queue on 2026-08-09 ("last_seen is not liveness").
# Ask for one real token; two consecutive failures = kill it so the
# start_if_free below reboots it warm.
GEN_FLAG=/tmp/m5_gemma_genfail
if /usr/sbin/lsof -nP -iTCP:9420 -sTCP:LISTEN >/dev/null 2>&1; then
  GEN_OK=$(curl -s -m 20 http://127.0.0.1:9420/v1/chat/completions     -H 'content-type: application/json'     -d '{"model":"DreamFoundries/SuperGemma-4-12b-abliterated-4bit","messages":[{"role":"user","content":"say ok"}],"max_tokens":3}'     | /usr/bin/python3 -c "import json,sys;print(1 if json.load(sys.stdin).get('choices') else 0)" 2>/dev/null)
  if [ "${GEN_OK:-0}" = "1" ]; then
    rm -f "$GEN_FLAG"
  else
    if [ -f "$GEN_FLAG" ]; then
      echo "[m5-stack] $(date '+%H:%M:%S') :9420 alive but NOT generating twice — restarting" >> /tmp/m5_gemma.log
      /usr/sbin/lsof -ti :9420 | xargs -r kill -9
      rm -f "$GEN_FLAG"
      sleep 2
    else
      touch "$GEN_FLAG"
    fi
  fi
fi

# 1) Lyric LLM (:9420) — no-think config lives in the forge payload.
# 2026-08-17: SuperGemma-4-12B replaced Gemma-4-31B. Matt A/B'd them sung and
# preferred the 12B, and at 6.7 GB (vs 25 GB resident) it is what lets the MINI
# hold ACE + the planner in 64 GB. Both boxes now run the same lyric model.
start_if_free 9420 \
  "exec /usr/bin/caffeinate -ims '$MLX_VENV/bin/python' -m mlx_lm.server --model DreamFoundries/SuperGemma-4-12b-abliterated-4bit --host 0.0.0.0 --port 9420" \
  /tmp/m5_gemma.log

# 2) ACE-Step renderer (:8001)
# NB: invoke via venv python, NOT the acestep-api launcher — the launcher's
# /bin/sh polyglot exec line hard-codes the old Desktop path, which launchd
# cannot traverse (TCC). Python treats that line as a docstring and skips it.
# ACESTEP_NO_INIT=false (Matt 2026-07-18): preload weights at boot so the
# first song after any reboot/power outage renders warm, same as the mini.
# ACESTEP_INIT_LLM=true + explicit --lm-model-path (2026-08-15): the LM planner
# is what makes us beat MiniMax. `auto` happened to pick the 4B by GPU tier on
# this box, but that is a guess we shouldn't depend on. NOTE: THIS is the real
# launch path on the M5 — start_api_server_macos.sh is NOT sourced here, so
# editing that file alone changes nothing. Swap to acestep-5Hz-lm-1.7B for
# roughly half the generation time at lower quality.
# 2026-08-17 CORRECTION: it is not "lower quality" — the 4B LOOPS. Same reggae
# song, three seeds, penalty applied to both: 1.7B 3.4/3.5/5.1% repeat vs 4B
# 52.4/65.5/77.3%. Never put the 4B back. The forge sends the required
# lm_repetition_penalty 1.15 / top_k 50 / top_p 0.85 with every job.
start_if_free 8001 \
  "cd '$ACE_DIR' && ACESTEP_LM_BACKEND=mlx ACESTEP_INIT_LLM=true ACESTEP_NO_INIT=false TOKENIZERS_PARALLELISM=false PYTHONUNBUFFERED=1 exec /usr/bin/caffeinate -ims '$ACE_DIR/.venv/bin/python' '$ACE_DIR/.venv/bin/acestep-api' --host 0.0.0.0 --port 8001 --lm-model-path acestep-5Hz-lm-1.7B" \
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
  "$HOME/Library/Logs/forge_guard.log"

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
