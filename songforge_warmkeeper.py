#!/usr/bin/env python3
"""songforge_warmkeeper.py — keep this Mac ready to make a song (2026-09-04).

Matt's rule: unless HE is using the machine for something heavy, the box should
always be sitting there warm and ready for a customer song. Nothing did that.

What already existed, and why it wasn't enough:
  * m5_supervisor.sh boots anything whose port is free. A LOADED engine whose
    weights macOS has paged out to swap still holds its port, so the supervisor
    sees nothing wrong.
  * forge_guard (:8790) protects memory and evicts idle hogs, and it MEASURES
    warmth — but it has no action that makes a cold engine warm again.
  * The lyric LLM gets warm for free: the supervisor's 120s "can you still
    generate a token" probe walks every layer, which pages gemma back in.
    ACE-Step has never had an equivalent, so after a video/ComfyUI session
    pushed its 35GB out, it stayed out until the next PAYING customer ate the
    3-minute page-in (2026-09-04: 188s for a 2-minute song, should be ~50s).

So this is ACE's version of that probe: when the engine is cold and the machine
is otherwise idle, render a throwaway 8-second instrumental and delete it. The
render is the warm-up — a forward pass is the only thing that pulls the weights
back off the SSD.

It never competes with real work. It bails out if a song is queued or running,
if ComfyUI is busy, if the guard is unhappy about memory, or if there isn't
comfortably enough room to hold the engine.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

GUARD = "http://127.0.0.1:8790"
FORGE = "http://127.0.0.1:8767"
COMFY = "http://127.0.0.1:8188"

LOG = Path.home() / "Library/Logs/songforge_warm.log"
STAMP = Path("/tmp/songforge_warmkeeper.stamp")   # last warm render
LOCK = Path("/tmp/songforge_warmkeeper.lock")

MIN_GAP_S = float(os.getenv("WARM_MIN_GAP_S", "900"))    # 15 min between tries
MIN_UPTIME_S = float(os.getenv("WARM_MIN_UPTIME_S", "300"))  # let boot preload finish
# Room to page the engine back in. Sized from what the engine is actually
# missing rather than a fixed number, so the same script is right on the 128GB
# M5 (ACE rests ~35GB) and the 64GB mini (~25GB). WARM_MIN_AVAIL_GB overrides.
HEADROOM_GB = float(os.getenv("WARM_HEADROOM_GB", "6"))
RENDER_TIMEOUT_S = float(os.getenv("WARM_RENDER_TIMEOUT_S", "420"))
ACE_PORT = 8001


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(url, payload, timeout=20):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def delete(url, timeout=20):
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def uptime_s():
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        return time.time() - int(m.group(1)) if m else 1e9
    except Exception:
        return 1e9


def comfy_busy():
    """ComfyUI mid-render looks idle on CPU (it is on the GPU). Ask its queue."""
    try:
        q = get(COMFY + "/queue", timeout=3)
        return len(q.get("queue_running", [])) + len(q.get("queue_pending", [])) > 0
    except Exception:
        return False   # not running at all


def take_lock():
    """One warm render at a time, even though launchd fires every 2 minutes."""
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(LOCK.read_text().strip())
            os.kill(pid, 0)          # still alive — leave it alone
            return False
        except Exception:
            LOCK.unlink(missing_ok=True)   # stale lock from a killed run
            return take_lock()


def _hq_report(summary, details):
    """Tell the HQ dashboard what we just did, so the Song Forge card can say it.

    Best-effort and silent: a warm engine matters, a dashboard line does not."""
    try:
        env = Path.home() / "Desktop/PROJECTS/ineedhemp website/.env"
        conf = {}
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
        url, tok = conf.get("HQ_URL"), conf.get("HQ_TOKEN")
        if not url or not tok:
            return
        import socket
        req = urllib.request.Request(
            url.rstrip("/") + "/api/agents/songforge-warmkeeper/report",
            data=json.dumps({"host": socket.gethostname().split(".")[0],
                             "action": "warmed",
                             "summary": summary,
                             "details": details}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {tok}"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def main():
    if uptime_s() < MIN_UPTIME_S and "--force" not in sys.argv:
        return 0                     # the supervisor's own preload owns boot

    try:
        state = get(GUARD + "/api/state")
    except Exception:
        return 0                     # no guard, no opinion

    ace = next((f for f in state.get("forge", []) if f["port"] == ACE_PORT), None)
    if not ace:
        return 0                     # ACE isn't up; that's the supervisor's job
    force = "--force" in sys.argv    # for testing the path on a warm engine
    if ace["warm"] and not force:
        return 0                     # already ready — the normal case

    # --- from here on the engine IS cold; decide whether we may fix it ---
    if state.get("level") not in ("ok", "warn"):
        log(f"ACE cold ({ace['rss_gb']}GB) but memory level={state.get('level')} — leaving it")
        return 0
    need = float(os.getenv("WARM_MIN_AVAIL_GB", "0")) or (
        max(0.0, ace["warm_ref_gb"] - ace["rss_gb"]) + HEADROOM_GB)
    if state.get("available_gb", 0) < need:
        log(f"ACE cold ({ace['rss_gb']}GB), needs ~{need:.0f}GB to page in but the "
            f"machine has {state.get('available_gb')}GB — leaving it")
        return 0

    try:
        st = get(FORGE + "/api/status")
    except Exception:
        return 0
    if st.get("jobs_running") or st.get("queue_depth"):
        return 0                     # a real song is about to warm it for free
    if not st.get("ace_up"):
        return 0

    if comfy_busy():
        log(f"ACE cold ({ace['rss_gb']}GB) — ComfyUI is rendering, waiting my turn")
        return 0

    now = time.time()
    if (STAMP.exists() and now - STAMP.stat().st_mtime < MIN_GAP_S
            and "--force" not in sys.argv):
        return 0
    if not take_lock():
        return 0

    try:
        STAMP.touch()
        log(("forced warm-up" if force else
             f"ACE cold at {ace['rss_gb']}GB (warm is {ace['warm_ref_gb']}GB)")
            + " — rendering a throwaway 8s clip")
        t0 = time.time()
        job = post(FORGE + "/api/song", {
            "style": "instrumental soft ambient warm-up tone",
            "lyrics": "[inst]",
            "title": "warmup",
            "duration": 8,
            "planner": "off",
            "inference_steps": 10,
            "private": True,      # never published, never mailed to anyone
            "video_only": True,   # keeps it out of Matt's library and the songs page
        })
        jid = job.get("id") or (job.get("ids") or [None])[0]
        if not jid:
            log(f"warm-up refused: {job}")
            return 0

        status = "queued"
        while time.time() - t0 < RENDER_TIMEOUT_S:
            time.sleep(5)
            try:
                status = get(f"{FORGE}/api/song/{jid}").get("status", "?")
            except Exception:
                continue
            if status in ("done", "error"):
                break

        try:
            delete(f"{FORGE}/api/song/{jid}")
        except Exception as e:
            log(f"could not delete warm-up song {jid[:8]}: {e}")

        secs = round(time.time() - t0)
        try:
            after = next(f for f in get(GUARD + "/api/state")["forge"]
                         if f["port"] == ACE_PORT)
            log(f"warm-up {status} in {secs}s — "
                f"ACE {ace['rss_gb']}GB → {after['rss_gb']}GB, warm={after['warm']}")
            _hq_report(
                f"warmed the song engine back up in {secs}s "
                f"({ace['rss_gb']:.0f}GB → {after['rss_gb']:.0f}GB resident)"
                if after["warm"] else
                f"tried to warm the song engine ({secs}s) and it is still cold",
                {"rss_before_gb": ace["rss_gb"], "rss_after_gb": after["rss_gb"],
                 "seconds": secs, "warm": after["warm"], "render": status})
        except Exception:
            log(f"warm-up {status} in {secs}s")
            _hq_report(f"ran a warm-up render ({status}) in {secs}s",
                       {"seconds": secs, "render": status})
    finally:
        LOCK.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
