#!/usr/bin/env python3
"""forge_guard.py — machine-wide memory authority. Song Forge always wins.

Matt, 2026-07-27, after the M5 panicked twice in one afternoon (14:07 and
15:58) with the swap -> SIGBUS -> launchd signature: "it shouldn't matter what
we're doing. It should always just hold its own no matter what other program is
trying to play, and if another program is trying to play, then it should tell
that other program that it has to adjust." And: "it shouldn't just be the story
forge. It should be all around."

So this is not a Story Forge feature. It is a small daemon that owns the
machine's memory policy for EVERY program on the box:

  * The Song Forge engines (ACE :8001, gemma :9420, forge :8767, app :8770) get
    a permanent reservation, sized from their own measured high-water RSS. That
    reservation is never lent out, never evicted, never paused.
  * Anybody who wants to load something heavy ASKS FIRST (POST /api/reserve).
    A grant is only issued out of what is left after the reservation. If it
    doesn't fit, the caller waits instead of shoving the box into swap.
  * Anybody who doesn't ask gets managed anyway. The enforcement sweep watches
    real memory every few seconds and, when headroom gets thin, tells heavy
    non-forge programs to adjust: stop granting, evict idle ones, SIGSTOP the
    biggest one (a paused renderer resumes; a killed one loses the work), and
    only SIGTERM it if the box is still headed for a panic.

Design notes worth keeping:
  - Admission control is BUDGET accounting, not free-memory guessing. The panic
    log said `memoryPressure: false` with 46GB free while the kernel was dying,
    so "free %" is not a safe gate. We add up what has been promised.
  - Pausing does not free RAM directly, but it stops the hog allocating and
    lets its pages go cold, which is what actually gives ACE room. That beats
    the old policy of SIGTERMing renders mid-flight.
  - Never touch a forge pid, pid 1, the window server, or Matt's terminals.

HTTP API (127.0.0.1:8790):
  GET    /api/state                  full picture (also the health endpoint)
  POST   /api/reserve {name,gb,ttl}  -> 200 {id,...} | 503 {retry_after,...}
  GET    /api/wait?name=&gb=&timeout= blocking form of the above
  POST   /api/heartbeat {id}         extend a lease
  DELETE /api/reserve?id=            release a lease
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT = int(os.getenv("FORGE_GUARD_PORT", "8790"))
STATE_FILE = Path(os.getenv("FORGE_GUARD_STATE",
                            str(Path.home() / ".forge_guard_state.json")))
LOG_FILE = Path(os.getenv("FORGE_GUARD_LOG", "/tmp/forge_guard.log"))

# Ports whose listening process IS Song Forge. Protected absolutely, and their
# resident size is what the reservation is sized from.
FORGE_PORTS = [int(p) for p in
               os.getenv("FORGE_PORTS", "8001,9420,8767,8770").split(",") if p.strip()]

# The subset of FORGE_PORTS whose warmth actually reflects model weights —
# only these gate forge_ok (the warm-first routing signal). Frontends stay
# protected via FORGE_PORTS but never mark the node cold.
WARM_GATE_PORTS = {int(p) for p in
                   os.getenv("FORGE_WARM_GATE_PORTS", "8001,9420").split(",") if p.strip()}

# Fraction of physical RAM we are willing to promise in total. The rest is the
# slack macOS itself needs (wired, file cache, the compressor's own working set).
SAFETY = float(os.getenv("FORGE_GUARD_SAFETY", "0.86"))
# Never let the reservation fall below this, even before the engines have been
# measured — a cold guard must not hand the whole box away.
RESERVE_FLOOR_GB = float(os.getenv("FORGE_RESERVE_FLOOR_GB", "34"))
# ...and never above this. Measuring the engines honestly (phys_footprint, added
# 2026-08-01) moved ACE from an apparent 15.6GB to a real 34GB, with a 62GB PEAK
# recorded during a decode. Reserving an all-time peak would leave ~24GB for the
# whole rest of the machine and no render could ever be seated again — the guard
# would have replaced "freezes the box" with "nothing ever runs", which is not an
# improvement. Reserve the warm steady state; the enforcement sweep is what
# protects Song Forge through a transient spike.
RESERVE_CAP_GB = float(os.getenv("FORGE_RESERVE_CAP_GB", "60"))
# Swap thresholds. The 7/27 panic ran up 19GB of swap across 20 swapfiles.
SWAP_WARN_GB = float(os.getenv("FORGE_SWAP_WARN_GB", "2"))
SWAP_CRIT_GB = float(os.getenv("FORGE_SWAP_CRIT_GB", "6"))
# Swap RATE is the real panic signal (MB/min paged OUT). The 7/27 panic was
# sustained heavy paging; an idle box carrying an old swapfile is not.
SWAP_RATE_CRIT = float(os.getenv("FORGE_SWAP_RATE_CRIT_MB_MIN", "400"))
SWAP_RATE_WARN = float(os.getenv("FORGE_SWAP_RATE_WARN_MB_MIN", "100"))
AVAIL_TIGHT_GB = float(os.getenv("FORGE_AVAIL_TIGHT_GB", "16"))
AVAIL_CRIT_GB = float(os.getenv("FORGE_AVAIL_CRIT_GB", "8"))
# GENUINELY free ram (free + speculative). A big model load needs this, not the
# optimistic "available" figure that includes purgeable pages and file cache.
FREE_CRIT_GB = float(os.getenv("FORGE_FREE_CRIT_GB", "12"))
# ...but low free pages ALONE is not distress — see the free_crit comment in
# sample(). A memory-mapped model load empties the free list into clean file
# cache on a perfectly healthy box.
FREE_CRIT_NEEDS_SWAP = os.getenv("FORGE_FREE_CRIT_NEEDS_SWAP", "1") == "1"
HOG_GB = float(os.getenv("FORGE_HOG_GB", "4"))      # "heavy" for the sweep
# A process this big that is not a forge engine, not on the known list, and
# never asked for room is a model workload by any other name (a Picture Eyes
# :8181 VL server, a hand-run benchmark, somebody's notebook). Above this size
# it becomes adjustable too — Matt's rule is that the OTHER program adjusts,
# and that can't only apply to programs we remembered to name.
BIG_UNLEASED_GB = float(os.getenv("FORGE_BIG_UNLEASED_GB", "12"))
# How far past its grant a job may drift before the log says so by name. It is
# never a kill trigger — an overrun already pays for itself by being charged.
OVERRUN_GB = float(os.getenv("FORGE_OVERRUN_GB", "8"))
TICK = float(os.getenv("FORGE_GUARD_TICK", "10"))
LEASE_TTL = float(os.getenv("FORGE_LEASE_TTL", "1800"))
KILL_AFTER_TICKS = int(os.getenv("FORGE_KILL_AFTER_TICKS", "6"))  # ~1min critical

GB = 1024.0 ** 3

# Programs the guard is allowed to tell "you have to adjust", by command-line
# pattern. Everything else only gets logged, never signalled — a memory guard
# that pauses the wrong process is worse than the problem it solves.
ADJUSTABLE = [
    # 2026-07-28: this used to read "main.py --listen 127.0.0.1 --port 8188", but
    # ComfyUI's actual argv on this box is just "... main.py --listen" (host/port
    # come from its config). Nothing matched, so ComfyUI was classified
    # "unregistered" — which meant _evict_idle's "never kill a BUSY comfyui" guard
    # never applied to it, and the guard SIGTERMed it mid-render all night. Match
    # the launcher, not the flags.
    ("comfyui", "main.py --listen"),
    ("lmstudio", ".lmstudio/.internal/utils/node"),
    ("ollama", "ollama runner"),
    ("film_qc", "film_qc.py"),
    ("qwen_vl", "mlx_vlm"),
    ("wan", "wan_"),
    ("flux", "flux_"),
    # 2026-07-31: forge-shot carries the ~18GB VL judge IN-PROCESS while it waits
    # on an mflux subprocess. It matched nothing here, so it was "unregistered",
    # looked idle, and evict-idle SIGTERMed two consecutive stills runs at
    # 13:34 and 14:27. Registered + leasing (storyforge-still/animate) it is
    # evicted LAST instead of first — still adjustable, customers still win.
    ("storyforge", "forge-shot"),
    ("flux", "mflux-generate"),
    # 2026-08-03: the Local AI Setup launchers (Narrative Gemma et al.) run an
    # Anthropic-protocol shim on :4000 that holds a ~18GB Gemma 4 31B. Its CPU
    # reads ~0 during a GPU prefill, so at 15:34:51 evict-idle SIGTERMed it
    # mid-request and Claude Code spun on ConnectionRefused for the rest of the
    # session. Same shape as the forge-shot case above: name it and let it
    # lease, so it is evicted LAST instead of first.
    ("localclaude", "mlx-native-server/server.py"),
    # 2026-08-07: the launchers' shared in-process agent (Qwen 3 Coder /
    # Gemma 4 / Narrative Gemma windows) holds its ~18-31GB directly in
    # agent.py, leased under names prefixed "agent-". Before it was registered
    # here, every double-click was granted a seat and then evict-idle SIGTERMed
    # the "unregistered" process seconds later (01:48:09, 01:48:40, 01:53:18
    # tonight). Fourth instance of the same lesson as comfyui / forge-shot /
    # localclaude above.
    ("local-agent", "Local AI Setup/agent/agent.py"),
    # 2026-08-10: the Agent-12 benchmark runner imports the same agent.py
    # in-process (cmdline says runner.py, so the pattern above never fires).
    # Its 31GB was double-charged — counted as resident AND its agent- lease
    # looked unspent — and the guard SIGSTOPped it mid-benchmark at 01:02.
    # Fifth instance of the comfyui / forge-shot / localclaude lesson.
    ("local-agent", "agent12/runner.py"),
    # 2026-08-10 11:12: bench-video narration (ChatterboxTTS in chatterbox-env)
    # leases as "chatterbox-narr" but its cmdline says gen_narration.py — the
    # guard saw a 56GB "unregistered" hog and SIGTERMed it mid-render. Sixth
    # instance of the register-your-programs lesson.
    ("chatterbox", "gen_narration.py"),
    # 2026-08-26 18:28: the Hive Strike background renders lease as
    # "hive-ltx2-render" but their cmdline is the mlx_video module path, so the
    # guard saw a 45GB+ "unregistered" hog and SIGTERMed a 7-second HD clip ten
    # minutes in, during stage 2. Seventh instance of the register-your-programs
    # lesson (comfyui / forge-shot / localclaude / local-agent / agent12 /
    # chatterbox above).
    ("ltx2", "mlx_video.models.ltx_2"),
]
# Which lease names mean "this program asked for its room". Used to decide who
# adjusts first — the program that went through admission control is protected
# ahead of the one that just allocated.
LEASE_LABELS = {
    # ComfyUI is NOT used during a still pass — mflux is. Mapping it onto the
    # still lease meant a ComfyUI that had not been reclaimed yet was charged to
    # whichever still pass happened to be live, which taught the guard that a
    # still costs 59GB (2026-08-01). The clamped hint then made every still ask
    # for 45 of a 50GB budget, one pass reserved nearly the whole box while using
    # almost none of it, and c1_celebration was BLOCKED after five refusals for a
    # memory problem this accounting invented. Animate only.
    "comfyui": ("storyforge-animate",),
    "film_qc": ("film-qc-vl", "storyforge-vl_qc"),
    "qwen_vl": ("film-qc-vl", "storyforge-vl_qc"),
    "storyforge": ("storyforge-still", "storyforge-animate"),
    # The subprocesses a storyforge phase spends its grant ON. Without these the
    # charge is counted twice: mflux's memory shows up as resident AND the still
    # lease still looks entirely unspent (2026-08-01).
    "flux": ("storyforge-still",),
    "wan": ("storyforge-animate",),
    "localclaude": ("localclaude",),
    "local-agent": ("agent-",),
    "chatterbox": ("chatterbox-narr",),
    "ltx2": ("hive-ltx2-render", "ltx2-render"),
}
# Never signalled under any circumstances, whatever they weigh.
PROTECTED_PAT = re.compile(
    r"(WindowServer|kernel_task|loginwindow|launchd|Terminal|iTerm|"
    r"claude|Code Helper|forge_guard\.py|forge_server\.py|acestep|mlx_lm\.server)",
    re.I)


# What each engine is expected to weigh when its weights are actually resident.
# Learned high-water is used when it is larger, but this floor matters: if the
# guard first samples an engine while macOS has it paged out (the mini does this
# after a few idle hours), a purely learned reservation would size itself from a
# 1GB RSS and hand the engine's own seat to somebody else.
ENGINE_MIN_GB = {}
for _pair in os.getenv("FORGE_ENGINE_GB", "8001:16,9420:16").split(","):
    if ":" in _pair:
        _p, _g = _pair.split(":", 1)
        try:
            ENGINE_MIN_GB[_p.strip()] = float(_g)
        except ValueError:
            pass

# Ceiling on the yardstick the WARM test measures against — the engine's warm
# steady state, not its all-time peak. Same reasoning as RESERVE_CAP_GB above:
# high-water only ever ratchets up, so one transient decode spike poisons it
# forever. ACE recorded a 122GB high-water on 2026-08-03 (a decode spike under
# the honest phys_footprint accounting added 8/01) against a real resting size
# of 35GB. That put the warm bar at 61GB — unreachable — so this M5 reported
# cold permanently while rendering an 8s clip in 12 seconds, and the customer
# app's warm-first routing was one mini-goes-warm away from sending every song
# to the slower box. High-water still sizes the RESERVATION; it no longer
# decides warmth. Set a port to 0 to opt out and use pure high-water.
WARM_REF_CAP_GB = {}
for _pair in os.getenv("FORGE_WARM_REF_GB", "8001:36,9420:28").split(","):
    if ":" in _pair:
        _p, _g = _pair.split(":", 1)
        try:
            WARM_REF_CAP_GB[_p.strip()] = float(_g)
        except ValueError:
            pass


def log(msg):
    # stdout only — both launch paths (the KeepAlive agent and the supervisor
    # fallback) redirect it to LOG_FILE, so writing the file here too doubled
    # every line.
    print(f"[guard] {time.strftime('%m-%d %H:%M:%S')} {msg}", flush=True)


# ───────────────────────── machine sampling ─────────────────────────

def _total_gb():
    out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / GB if out.isdigit() else 0.0


TOTAL_GB = _total_gb()


def _swap_gb():
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"used\s*=\s*([0-9.]+)M", out)
        return float(m.group(1)) / 1024 if m else 0.0
    except Exception:
        return 0.0


_SWAP_RATE = {"t": 0.0, "outs": 0, "rate": 0.0}
# Shortest interval that yields a meaningful pages-per-minute figure. Samples
# closer together than this reuse the last measurement instead of overwriting
# the baseline — see the comment in _swap_rate_mb_min.
RATE_WINDOW_S = float(os.getenv("FORGE_RATE_WINDOW_S", "20"))


def _swap_rate_mb_min():
    """Pages swapped OUT per minute, measured — not the swapfile's size.

    2026-07-28: vm.swapusage reported 6.6GB "used" while vm_stat showed ZERO
    swapouts and ZERO swapins over a full minute and 49.7GB was free. macOS
    allocates a swapfile and never returns the space, so swap-size only ever
    rises: once it crossed SWAP_CRIT_GB the guard latched "critical" forever,
    refused every lease, and then SIGTERMed the unleased work it had just
    refused to seat. film_qc died that way three times on a completely idle box.
    Size says "this machine swapped at some point". Rate says "this machine is
    in trouble RIGHT NOW", which is the only thing worth panicking about."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=5).stdout
        m = re.search(r"Swapouts:\s+(\d+)", out)
        if not m:
            return 0.0
        outs, now = int(m.group(1)), time.time()
        prev_t, prev_o = _SWAP_RATE["t"], _SWAP_RATE["outs"]
        if not prev_t or outs < prev_o:        # first tick / counter reset
            _SWAP_RATE["t"], _SWAP_RATE["outs"] = now, outs
            return _SWAP_RATE["rate"]
        dt = now - prev_t
        if dt < RATE_WINDOW_S:
            # TOO SOON TO MEASURE — and the baseline must NOT move.
            #
            # 2026-08-01, caught live: sample() runs on the 10s enforce tick AND
            # on every GET /api/state, and this function used to advance its
            # baseline on every call. Two samples 200ms apart see the same
            # swapout counter, so the computed rate is 0 — and anyone polling the
            # health endpoint (a monitor, mem_client.state(), the customer app)
            # was silently pinning the rate at zero. During a real event the box
            # had put 7.1GB into swap in four minutes, ~1800MB/min, and the guard
            # reported swap_rate_mb_min: 0.0. Swap RATE is the ONLY admissible
            # panic signal (story-forge CLAUDE.md rule 20) and health checks were
            # erasing it. Measure over a real window, keep the last true reading
            # in between.
            return _SWAP_RATE["rate"]
        _SWAP_RATE["t"], _SWAP_RATE["outs"] = now, outs
        _SWAP_RATE["rate"] = (outs - prev_o) * 16384 / (1024.0**2) * (60.0 / dt)
        return _SWAP_RATE["rate"]
    except Exception:
        return 0.0


def _free_gb():
    """Genuinely FREE ram — pages free + speculative only.

    2026-07-28: _vm_stat() also counts purgeable and half the file cache, which are
    reclaimable in theory but a ~24GB contiguous GGUF load cannot wait for
    reclamation. The guard reported available_gb 63.6 minutes before actual free
    pages hit 0.2GB and the OS silently killed ComfyUI mid-load. A grant must be
    backed by memory that exists right now, not memory that could be freed."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return 0.0
    page = 16384
    m = re.search(r"page size of (\d+)", out)
    if m:
        page = int(m.group(1))
    tot = 0
    for key in ("Pages free", "Pages speculative"):
        m = re.search(rf"{key}:\s+(\d+)", out)
        if m:
            tot += int(m.group(1))
    return tot * page / GB


def _vm_stat():
    """Returns available GB — free + speculative + purgeable + half the file
    cache. File-backed pages are reclaimable without swapping, but only
    half-counted: some of them are hot and evicting them costs real time."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return 0.0
    page = 16384
    m = re.search(r"page size of (\d+)", out)
    if m:
        page = int(m.group(1))
    vals = {}
    for line in out.splitlines():
        m = re.match(r'"?([A-Za-z][A-Za-z \-]*)"?[^:]*:\s+(\d+)', line)
        if m:
            vals[m.group(1).strip().lower()] = int(m.group(2))
    free = vals.get("pages free", 0) + vals.get("pages speculative", 0)
    purge = vals.get("pages purgeable", 0)
    filed = vals.get("file-backed pages", 0)
    return (free + purge + filed * 0.5) * page / GB


def _pids_on_ports(ports):
    """pid -> port for anything LISTENing on the given ports."""
    out = {}
    for p in ports:
        try:
            r = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{p}",
                                "-sTCP:LISTEN", "-t"],
                               capture_output=True, text=True, timeout=8).stdout
            for pid in r.split():
                if pid.isdigit():
                    out[int(pid)] = p
        except Exception:
            pass
    return out


def _rss_of(pid):
    """Resident GB for one pid. The sweep below ignores anything under 1GB, but
    a forge engine must be measured even when the pager has squeezed it down to
    nothing — that reading is exactly how we know it went cold."""
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=5).stdout.strip()
        return int(r) / 1048576.0 if r.isdigit() else 0.0
    except Exception:
        return 0.0


_FOOTPRINT = "/usr/bin/footprint"
_FP_UNITS = {"B": 1.0, "KB": 1024.0, "MB": 1024.0 ** 2, "GB": 1024.0 ** 3,
             "TB": 1024.0 ** 4}


def _footprint_gb(pid):
    """phys_footprint for one pid — RSS *plus* the memory RSS cannot see.

    2026-08-01, measured on this box: a 6GB torch MPS allocation moved `ps rss`
    by 0.04GB and phys_footprint by 6.26GB. Metal/GPU buffers are wired into the
    task and never counted as resident, so every render this guard has ever
    sized was measured at a fraction of its true weight — story-forge CLAUDE.md
    rule 20 wrote that down as "the guard saw ComfyUI at 9.8GB while it held
    ~32GB… sizing policy off RSS under-measures a render by 3x" and it is
    exactly right. Admission control cannot be honest on RSS.

    ~30ms per call, so it is only run on the handful of processes that matter.
    Falls back to 0 (caller keeps RSS) if footprint is unavailable.
    """
    try:
        out = subprocess.run([_FOOTPRINT, "-p", str(pid)],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return 0.0
    m = re.search(r"phys_footprint:\s+([\d.]+)\s*([KMGT]?B)", out)
    if not m:
        return 0.0
    return float(m.group(1)) * _FP_UNITS.get(m.group(2), 1.0) / GB


def _procs():
    """[(pid, rss_gb, cpu, command)] for everything worth accounting for.

    The floor used to be a flat 1GB RSS, which is another way of not seeing GPU
    memory: a ComfyUI holding 40GB of wired Wan weights can sit at 0.3GB RSS
    between allocations and drop off this list entirely. Anything matching a
    known heavy program is kept whatever its RSS says, and _footprint_gb has the
    final word on how big it is.
    """
    try:
        r = subprocess.run(["ps", "-axo", "pid=,rss=,pcpu=,command="],
                           capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    out = []
    for line in r.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, rss, cpu = int(parts[0]), int(parts[1]), float(parts[2])
        except ValueError:
            continue
        gb = rss / 1048576.0
        if gb >= 1.0 or any(pat in parts[3] for _, pat in ADJUSTABLE):
            out.append((pid, gb, cpu, parts[3]))
    return out


# ───────────────────────── guard state ─────────────────────────

class Guard:
    def __init__(self):
        self.lock = threading.RLock()
        self.leases = {}          # id -> {name, gb, expires, granted}
        self.seq = 0
        self.paused = {}          # pid -> {"since": ts, "label": str}
        self.crit_ticks = 0
        self.highwater = {}       # str(port) -> GB, persisted
        self.peaks = {}           # lease key -> measured peak GB, persisted
        self._over_logged = {}    # lease id -> last overrun log ts
        self.last = {}            # last sample, served by /api/state
        self._load_state()

    # ── persistence: remember how big the engines really are ──
    def _load_state(self):
        try:
            d = json.loads(STATE_FILE.read_text())
            self.highwater = {str(k): float(v)
                              for k, v in d.get("highwater", {}).items()}
            self.peaks = {str(k): float(v)
                          for k, v in d.get("peaks", {}).items()}
        except Exception:
            self.highwater = {}
            self.peaks = {}

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps({"highwater": self.highwater,
                                              "peaks": self.peaks,
                                              "saved": time.time()}, indent=2))
        except Exception:
            pass

    # ── the reservation ──
    def reserve_gb(self, forge):
        """What Song Forge is owed: the high-water resident size of each engine
        we have ever seen, so a paged-out engine still keeps its seat at the
        table. Never below the floor."""
        total = sum(self.highwater.values())
        return min(max(RESERVE_FLOOR_GB, total + 2.0), RESERVE_CAP_GB)

    def budget_gb(self, forge):
        """What is left for everybody else, in total, across all leases."""
        return max(0.0, TOTAL_GB * SAFETY - self.reserve_gb(forge))

    def leased_gb(self):
        now = time.time()
        return sum(l["gb"] for l in self.leases.values() if l["expires"] > now)

    # ── what the budget has actually been SPENT (2026-08-01) ──
    #
    # The box froze on 2026-08-01 with four Python processes holding 144GB of a
    # 128GB machine, an hour after admission control had approved everything it
    # was asked. It approved because the only number it ever added up was the
    # DECLARED one: storyforge asked for 22GB (bin/forge-shot) and then loaded
    # mflux, an in-process VL judge and a ComfyUI full of cached Wan weights on
    # top of it. Worse, LEASE_LABELS maps comfyui onto storyforge's lease, so
    # ComfyUI's 63GB counted as "asked politely" and sorted LAST for eviction —
    # the biggest thing in the room was the best protected.
    #
    # So the budget is now charged for what is actually held, plus the part of
    # each grant that has not been taken yet. Under-declaring buys nothing:
    # allocate past your grant and the next caller is the one who waits.
    def _owner(self, label, live):
        """Which live lease, if any, this hog's memory belongs to."""
        pats = LEASE_LABELS.get(label)
        if not pats:
            return None
        cands = [lid for lid, l in live.items()
                 if any(p in l["name"] for p in pats)]
        # one hog, one owner — the biggest grant claims it, so the rest keep
        # their unspent room and nobody's usage is counted twice
        return max(cands, key=lambda lid: live[lid]["gb"], default=None)

    def charge(self, hogs):
        """(charged_gb, used_by_lease). Charged = every heavy non-forge program
        resident right now + the unspent remainder of every outstanding grant."""
        now = time.time()
        live = {lid: l for lid, l in self.leases.items() if l["expires"] > now}
        used = {lid: 0.0 for lid in live}
        for h in hogs:
            lid = self._owner(h["label"], live)
            if lid:
                used[lid] += h["mem_gb"]
        resident = sum(h["mem_gb"] for h in hogs)
        unspent = sum(max(0.0, l["gb"] - used[lid]) for lid, l in live.items())
        return resident + unspent, used

    def _learn_peak(self, live, used):
        """Remember what each KIND of job really weighs, so the next one can ask
        for the right number instead of a hopeful one. Served by /api/hint."""
        for lid, l in live.items():
            key = l["name"].split()[0] if l["name"].split() else l["name"]
            got = used.get(lid, 0.0)
            if got <= 0:
                continue
            if got > self.peaks.get(key, 0.0):
                self.peaks[key] = round(got, 1)
                self._save_state()
            over = got - l["gb"]
            if over >= OVERRUN_GB and time.time() - self._over_logged.get(lid, 0) > 60:
                self._over_logged[lid] = time.time()
                log(f"overrun {lid} {l['name']}: declared {l['gb']:.0f}GB, "
                    f"holding {got:.0f}GB — charging the real number")

    # ── sampling ──
    def sample(self):
        forge_pids = _pids_on_ports(FORGE_PORTS)
        procs = _procs()
        by_pid = {p[0]: p for p in procs}

        forge = []
        for pid, port in sorted(forge_pids.items(), key=lambda kv: kv[1]):
            rss = by_pid[pid][1] if pid in by_pid else _rss_of(pid)
            # ACE and gemma are MLX: most of their weights DO show in RSS, but
            # not all of it. Size the reservation off whichever is larger.
            rss = max(rss, _footprint_gb(pid))
            key = str(port)
            prev = max(self.highwater.get(key, 0.0), ENGINE_MIN_GB.get(key, 0.0))
            if rss > prev:
                prev = round(rss, 2)
            if prev != self.highwater.get(key):
                self.highwater[key] = prev
                self._save_state()
            hw = prev
            # Warmth is judged against the steady state, not the peak: a spike
            # that ratcheted high-water up must not raise the bar out of reach.
            _cap = WARM_REF_CAP_GB.get(key, 0.0)
            ref = min(hw, _cap) if _cap > 0 else hw
            forge.append({"port": port, "pid": pid, "rss_gb": round(rss, 2),
                          "highwater_gb": round(hw, 2),
                          "warm_ref_gb": round(ref, 2),
                          # resident enough to answer fast? below half its warm
                          # reference means macOS has paged the weights out
                          "warm": bool(ref <= 0 or rss >= ref * 0.5)})

        # Which programs currently hold a granted lease, by label. Somebody who
        # asked politely is the LAST one we interfere with; the one who just
        # took the memory is the one told to adjust first.
        asked = set()
        for l in self.leases.values():
            if l["expires"] > time.time():
                for lab, pats in LEASE_LABELS.items():
                    if any(p in l["name"] for p in pats):
                        asked.add(lab)

        hogs = []
        for pid, gb, cpu, cmd in procs:
            if pid in forge_pids:
                continue
            label = None
            for name, pat in ADJUSTABLE:
                if pat in cmd:
                    label = name
                    break
            # mem_gb is the honest weight (RSS + wired GPU); rss_gb is kept
            # alongside it because the two disagreeing by 3x is itself the
            # symptom worth reading in the log.
            mem = max(gb, _footprint_gb(pid)) if (gb >= HOG_GB or label) else gb
            if mem < HOG_GB:
                continue
            protected = bool(PROTECTED_PAT.search(cmd))
            known = bool(label)
            hogs.append({"pid": pid, "rss_gb": round(gb, 2),
                         "mem_gb": round(mem, 2), "cpu": cpu,
                         "label": label or "unregistered",
                         "asked": label in asked,
                         "adjustable": (known or mem >= BIG_UNLEASED_GB)
                                       and not protected,
                         "cmd": cmd[:120]})
        # biggest first, but anything that never asked goes ahead of anything
        # that did, whatever the sizes
        hogs.sort(key=lambda h: (h["asked"], -h["mem_gb"]))

        swap = _swap_gb()
        srate = _swap_rate_mb_min()
        avail = _vm_stat()
        free = _free_gb()
        level = "ok"
        why = ""
        # 2026-07-28: a memory-mapped GGUF load parks the WHOLE model in clean,
        # file-backed pages. vm_stat does not count those as free, so `free`
        # collapses to ~3GB on every healthy i2v render even though the kernel can
        # reclaim them instantly. Measured during a Wan2.2 i2v load: free 2.8GB,
        # File-backed 30.7GB, swap rate 0 — a box in perfect health. With a bare
        # free-pages trigger that read as "critical", and the guard SIGSTOPed then
        # SIGTERMed ComfyUI mid-load four times in nine minutes (22:34, 22:36,
        # 22:42, 22:43), destroying a 20-minute render every time. Real exhaustion
        # always shows swap MOVING, so the free-pages trigger needs swap growth to
        # corroborate it. FORGE_FREE_CRIT_NEEDS_SWAP=0 restores the bare trigger.
        free_crit = free <= FREE_CRIT_GB and (srate > 0 or not FREE_CRIT_NEEDS_SWAP)
        if srate >= SWAP_RATE_CRIT or avail <= AVAIL_CRIT_GB or free_crit:
            level, why = "critical", (f"swapping {srate:.0f}MB/min"
                                      if srate >= SWAP_RATE_CRIT
                                      else (f"free {free:.1f}GB while swapping "
                                            f"{srate:.0f}MB/min" if free_crit
                                            else f"available {avail:.1f}GB"))
        elif srate >= SWAP_RATE_WARN or avail <= AVAIL_TIGHT_GB:
            level, why = "tight", (f"swapping {srate:.0f}MB/min"
                                   if srate >= SWAP_RATE_WARN
                                   else f"available {avail:.1f}GB")

        now = time.time()
        with self.lock:
            live = {lid: l for lid, l in self.leases.items()
                    if l["expires"] > now}
            charged, used = self.charge(hogs)
            self._learn_peak(live, used)
            # A grant is a promise of room. A job sitting INSIDE the room it was
            # given is not the one that has to adjust; a job past it is.
            for h in hogs:
                _lid = self._owner(h["label"], live)
                h["within_grant"] = bool(_lid) and used.get(_lid, 0.0) <= live[_lid]["gb"]
        snap = {
            "total_gb": round(TOTAL_GB, 1),
            "reserve_gb": round(self.reserve_gb(forge), 1),
            "budget_gb": round(self.budget_gb(forge), 1),
            "leased_gb": round(self.leased_gb(), 1),
            # what the budget is actually spending: everything heavy that is
            # resident, plus grants not yet drawn down. This, not leased_gb, is
            # what admission control subtracts.
            "charged_gb": round(charged, 1),
            "resident_nonforge_gb": round(sum(h["mem_gb"] for h in hogs), 1),
            "available_gb": round(avail, 1),
            "free_gb": round(free, 1),
            "swap_gb": round(swap, 1),
            "swap_rate_mb_min": round(srate, 1),
            "level": level, "why": why,
            "forge": forge,
            # Only the MODEL engines (ACE :8001, gemma :9420) can be "cold" —
            # the HTTP frontends (:8767 forge, :8770 app) are tiny and their
            # RSS vs high-water says nothing about weights. Gating on them
            # made a freshly restarted forge_server report forge_ok=false
            # (2026-07-29: routed customer jobs to the paged-out backup).
            "forge_ok": (all(f["warm"] for f in forge
                             if f["port"] in WARM_GATE_PORTS)
                         and any(f["port"] in WARM_GATE_PORTS for f in forge)),
            "hogs": hogs[:12],
            "paused": [{"pid": p, **v} for p, v in self.paused.items()],
            "leases": [{"id": k, **v, "used_gb": round(used.get(k, 0.0), 1)}
                       for k, v in self.leases.items()
                       if v["expires"] > now],
            "peaks": dict(self.peaks),
            "ts": now,
        }
        with self.lock:
            self.last = snap
        return snap

    # ── admission ──
    def try_reserve(self, name, gb, ttl=LEASE_TTL):
        snap = self.last or self.sample()
        with self.lock:
            # Charged, not leased: a program that took more than it declared has
            # already spent the room, whether or not it filed the paperwork.
            #
            # But charged_gb comes off the last SAMPLE, which can be a whole tick
            # old, and a grant issued inside that tick is invisible to it. Caught
            # in test 2026-08-01: a 46GB animate lease and a 38GB still lease were
            # both granted a second apart against a 52GB budget — the two halves
            # of one shot, stacked, which is the exact thing this fix exists to
            # stop. So take the worse of the sampled reality and the live ledger:
            # every grant already issued, plus whatever resident memory the sample
            # could not attribute to any lease.
            sampled = snap.get("charged_gb", 0.0)
            attributed = sum(l.get("used_gb", 0.0) for l in snap.get("leases", []))
            unattributed = max(0.0, snap.get("resident_nonforge_gb", 0.0) - attributed)
            charged = max(sampled, self.leased_gb() + unattributed)
            free_budget = self.budget_gb(snap["forge"]) - charged
            if snap["level"] == "critical":
                return None, f"machine critical ({snap['why']})", 30
            if gb > free_budget:
                return None, (f"{gb:.0f}GB would exceed the non-forge budget "
                              f"({free_budget:.0f}GB free of "
                              f"{snap['budget_gb']:.0f}GB; "
                              f"{snap.get('resident_nonforge_gb', 0):.0f}GB "
                              f"already resident)"), 30
            if snap["level"] == "tight" and gb > free_budget * 0.6:
                return None, f"machine tight ({snap['why']}), large ask held", 30
            self.seq += 1
            lid = f"L{self.seq}-{int(time.time())}"
            self.leases[lid] = {"name": name, "gb": float(gb),
                                "granted": time.time(),
                                "expires": time.time() + ttl}
            log(f"grant {lid} {name} {gb:.0f}GB "
                f"(budget {snap['budget_gb']:.0f}GB, charged {charged:.0f}GB, "
                f"reserve {snap['reserve_gb']:.0f}GB)")
            return lid, "granted", 0

    def release(self, lid):
        with self.lock:
            l = self.leases.pop(lid, None)
        if l:
            log(f"release {lid} {l['name']} {l['gb']:.0f}GB")
        return bool(l)

    def heartbeat(self, lid, ttl=LEASE_TTL):
        with self.lock:
            l = self.leases.get(lid)
            if not l:
                return False
            l["expires"] = time.time() + ttl
            return True

    # ── enforcement: tell the other program to adjust ──
    def _signal(self, pid, sig, label, why):
        try:
            os.kill(pid, sig)
            log(f"{label} pid={pid} <- {sig.name if hasattr(sig,'name') else sig} ({why})")
            return True
        except Exception as e:
            log(f"signal pid={pid} failed: {e}")
            return False

    def _comfy_busy(self):
        """Is ComfyUI mid-render? FAILS SAFE — an unanswered check means BUSY.

        2026-07-28: this used to `return False` on any exception, i.e. "not busy,
        kill it". But the 3s HTTP check is exactly what stops answering when the
        box is thrashing — and thrashing is the only time the guard asks. At
        23:19 a render 90 seconds from completion was SIGTERMed on
        "swapping 5021MB/min" because /queue timed out under that very load.
        The check failed precisely when it mattered and its failure mode was
        lethal. "I could not tell" must never authorise a kill: a wrongly-spared
        renderer costs headroom the escalation path can still reclaim elsewhere,
        a wrongly-killed one costs 15 minutes of GPU work that cannot be undone.
        """
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=3) as r:
                q = json.loads(r.read().decode())
            busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", [])) > 0
            if busy:
                self._comfy_busy_seen = time.time()
            return busy
        except Exception:
            # Unreachable/timed out => BUSY, unconditionally.
            #
            # An earlier version of this fallback required having SEEN a busy
            # queue at some point in the last 10 minutes. That still had a hole,
            # and it cost a render at 01:15 on 2026-07-29: ComfyUI is restarted
            # immediately before every animate, and while it loads a ~24GB GGUF
            # its HTTP server does not answer at all. So the check never once
            # succeeded during that render, "seen busy" stayed unset, the
            # fallback said not-busy, and _evict_idle SIGTERMed it six minutes
            # into the job. The whole window this protection exists to cover is
            # a window where the check cannot answer.
            #
            # Cost of being wrong the other way: a genuinely hung, idle ComfyUI
            # is never evicted. That is survivable — it drops out of the hog
            # list once its pages go, the swap-rate and available-GB triggers
            # still fire on everything else, and an operator can kill it. A
            # wrongly-killed render is 15 minutes of GPU work that is simply gone.
            return True

    def _evict_idle(self, hogs, why):
        """Idle heavy servers go first — this is the old supervisor policy, kept
        because an idle model server holding 20GB is pure waste."""
        acted = False
        for h in hogs:
            if not h["adjustable"] or h["pid"] in self.paused:
                continue
            if h.get("asked"):
                # A lease the evictor ignores is decorative — written 2026-07-28,
                # paid for AGAIN 2026-07-31: storyforge held a granted 22GB lease
                # and was evict-idled 96 seconds later (its CPU reads ~0 while it
                # waits on its mflux subprocess). A program inside admission
                # control is waiting, not wasting. The emergency paths (swap
                # rate, critical) still outrank any lease — customers still win.
                continue
            if h["label"] == "comfyui" and self._comfy_busy():
                continue
            if h["cpu"] >= 2.0 and h["label"] == "comfyui":
                continue  # GPU work shows ~0% CPU; queue check above is the truth
            if h["cpu"] < 2.0:
                acted |= self._signal(h["pid"], signal.SIGTERM,
                                      f"evict-idle {h['label']}", why)
        return acted

    def _growing(self, pid, rss_gb):
        """True if this pid's RSS went UP since the previous tick — a load in
        flight. Grows the sample as a side effect, so call once per pid per tick."""
        prev = getattr(self, "_rss_hist", None)
        if prev is None:
            prev = self._rss_hist = {}
        was = prev.get(pid)
        prev[pid] = rss_gb
        return was is not None and rss_gb > was + 0.5

    def _pause_biggest(self, hogs, why):
        for h in hogs:
            # NEVER pause a ComfyUI that is mid-render (2026-07-28). Two reasons,
            # both measured tonight. (1) Wan's weights are WIRED Metal/GPU memory —
            # 32GB of it, invisible to RSS — and SIGSTOP cannot release a single
            # page of it, so the pause buys nothing. (2) It actively makes things
            # worse: a stopped process gets paged out, so swap went 4.5GB -> 17GB in
            # two minutes after the pause, kept the box "critical", and the render
            # was SIGTERMed 50s later. Load spikes drain by themselves when the
            # render is left alone; pausing turns a spike into a death spiral.
            # _evict_idle already refuses to touch a busy comfyui — the escalation
            # path has to honour the same rule or the protection is decorative.
            if h["label"] == "comfyui" and self._comfy_busy():
                continue
            # Nor pause anything whose RSS is still CLIMBING — that is a model
            # load in progress, and SIGSTOP mid-load strands it: it can never
            # reach the point where it would release anything, so crit_ticks
            # runs out and it is SIGTERMed. 2026-07-29 00:51, the ellie_eye clip
            # judge was paused at 19GB while loading and would have been killed
            # with the finished render sitting unjudged on disk — the exact
            # failure forge-shot's own comments describe from 7/27. Let a load
            # finish; a loaded process can at least be paused usefully later.
            if self._growing(h["pid"], h["mem_gb"]):
                continue
            # HONOUR THE GRANT (2026-08-01). Rule 20 says "a lease buys nothing
            # unless the enforcement path honours it" and records film_qc being
            # SIGSTOPped while holding a granted 26GB. _evict_idle was fixed to
            # skip `asked`; this path never was, and it cost two more: forge-shot
            # SIGSTOPped at 19:12:47 and 19:42:44 tonight while holding a valid
            # lease, mid-shot. Skipping leaseholders outright would be the
            # opposite mistake — during a render the leaseholder is usually the
            # ONLY hog, so the guard would have no lever at all. The line is the
            # grant itself: inside your room you are protected, past it you are
            # the one who over-allocated and the first to adjust.
            if h.get("asked") and h.get("within_grant"):
                continue
            if h["adjustable"] and h["pid"] not in self.paused:
                if self._signal(h["pid"], signal.SIGSTOP,
                                f"pause {h['label']} ({h['mem_gb']:.0f}GB)", why):
                    self.paused[h["pid"]] = {"since": time.time(),
                                             "label": h["label"],
                                             "rss_gb": h["rss_gb"],
                                             "mem_gb": h["mem_gb"]}
                    return True
        return False

    def _resume_all(self):
        for pid, info in list(self.paused.items()):
            if self._signal(pid, signal.SIGCONT,
                            f"resume {info['label']}", "headroom recovered"):
                self.paused.pop(pid, None)

    def enforce(self):
        snap = self.sample()
        now = time.time()
        with self.lock:
            for lid, l in list(self.leases.items()):
                if l["expires"] <= now:
                    log(f"lease {lid} ({l['name']}) expired")
                    self.leases.pop(lid, None)

        # a paused process that has since died is not our problem
        for pid in list(self.paused):
            try:
                os.kill(pid, 0)
            except Exception:
                self.paused.pop(pid, None)

        level = snap["level"]
        if level == "ok":
            self.crit_ticks = 0
            if self.paused:
                self._resume_all()
            return snap

        if level == "tight":
            self.crit_ticks = 0
            self._evict_idle(snap["hogs"], snap["why"])
            return snap

        # critical — Song Forge's seat is the thing we are protecting
        self.crit_ticks += 1
        self._evict_idle(snap["hogs"], snap["why"])
        if not self.paused:
            self._pause_biggest(snap["hogs"], snap["why"])
        elif self.crit_ticks >= KILL_AFTER_TICKS:
            # pausing didn't drain it; better one lost render than a kernel panic
            for pid, info in list(self.paused.items()):
                self._signal(pid, signal.SIGCONT, "pre-term resume", "so it can exit")
                self._signal(pid, signal.SIGTERM, f"terminate {info['label']}",
                             f"still critical after {self.crit_ticks} ticks")
                self.paused.pop(pid, None)
            self.crit_ticks = 0
        return snap

    def loop(self):
        while True:
            try:
                self.enforce()
            except Exception as e:
                log(f"enforce error: {e}")
            time.sleep(TICK)


GUARD = Guard()


# ───────────────────────── http ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/api/state", "/", "/health"):
            return self._json(GUARD.sample())
        if u.path == "/api/hint":
            # "How big does this kind of job actually turn out to be?" — so a
            # caller can ask for the truth instead of the number somebody typed
            # into a script months ago.
            name = (q.get("name") or [""])[0]
            key = name.split()[0] if name.split() else name
            snap = GUARD.last or GUARD.sample()
            # A hint must never be big enough to make a job permanently
            # unschedulable. Peaks only ever grow, so one bad sample (a stale
            # ComfyUI cache attributed to a render) could otherwise ratchet a
            # phase past the whole budget and hold it there forever.
            ceiling = snap.get("budget_gb", 0) * 0.9
            peak = min(GUARD.peaks.get(key, 0.0), ceiling) if ceiling else 0.0
            return self._json({"name": key, "peak_gb": round(peak, 1),
                               "measured_peak_gb": GUARD.peaks.get(key, 0.0),
                               "peaks": GUARD.peaks})
        if u.path == "/api/wait":
            name = (q.get("name") or ["anon"])[0]
            gb = float((q.get("gb") or ["0"])[0])
            timeout = float((q.get("timeout") or ["900"])[0])
            ttl = float((q.get("ttl") or [str(LEASE_TTL)])[0])
            t0 = time.time()
            waited = 0
            while True:
                lid, msg, retry = GUARD.try_reserve(name, gb, ttl)
                if lid:
                    return self._json({"id": lid, "granted": True, "gb": gb,
                                       "waited_s": round(time.time() - t0)})
                if time.time() - t0 >= timeout:
                    return self._json({"granted": False, "reason": msg,
                                       "waited_s": round(time.time() - t0)}, 503)
                if waited % 6 == 0:
                    log(f"hold {name} {gb:.0f}GB — {msg}")
                waited += 1
                time.sleep(min(retry or 10, 10))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/reserve":
            lid, msg, retry = GUARD.try_reserve(b.get("name", "anon"),
                                                float(b.get("gb", 0)),
                                                float(b.get("ttl", LEASE_TTL)))
            if lid:
                return self._json({"id": lid, "granted": True})
            return self._json({"granted": False, "reason": msg,
                               "retry_after": retry}, 503)
        if u.path == "/api/heartbeat":
            ok = GUARD.heartbeat(b.get("id", ""), float(b.get("ttl", LEASE_TTL)))
            return self._json({"ok": ok}, 200 if ok else 404)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        lid = (q.get("id") or [""])[0] or self._body().get("id", "")
        ok = GUARD.release(lid)
        return self._json({"ok": ok}, 200 if ok else 404)


def main():
    log(f"forge_guard up on :{PORT} — {TOTAL_GB:.0f}GB box, "
        f"reserve floor {RESERVE_FLOOR_GB:.0f}GB, safety {SAFETY}")
    threading.Thread(target=GUARD.loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
