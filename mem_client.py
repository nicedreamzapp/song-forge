#!/usr/bin/env python3
"""mem_client.py — ask forge_guard before you load something heavy.

Any program on this machine that is about to pull a big model into memory
should wrap that work:

    from mem_client import reserve
    with reserve("wan-render", 70):
        ...load Wan, sample, decode...

The reservation is only granted out of what is left after Song Forge's
permanent seat, so a customer can always start a song. If it doesn't fit,
`reserve` waits instead of pushing the box into swap.

Shell callers:
    python3 mem_client.py wait wan-render 70 --timeout 900   # blocks, prints id
    python3 mem_client.py release L12-1785...
    python3 mem_client.py state

If the guard is not running this degrades to a no-op with one warning — a
memory broker must never be the reason a render can't start.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager

GUARD = os.getenv("FORGE_GUARD_URL", "http://127.0.0.1:8790")
_warned = False


def _call(method, path, payload=None, timeout=30):
    url = f"{GUARD}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode()), e.code
        except Exception:
            return {}, e.code
    except Exception:
        return None, 0


def state():
    d, _ = _call("GET", "/api/state", timeout=15)
    return d


def hint(name):
    """What jobs of this kind have actually WEIGHED, measured by the guard
    (phys_footprint, so wired GPU memory included). 0.0 when it has never seen
    one. Use it so a job asks for its real size instead of the number somebody
    typed into a script months ago — under-declaring is what let four Python
    processes reach 144GB on a 128GB box on 2026-08-01."""
    q = urllib.parse.urlencode({"name": name})
    d, _ = _call("GET", f"/api/hint?{q}", timeout=10)
    try:
        return float((d or {}).get("peak_gb") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def acquire(name, gb, timeout=900, ttl=1800, honest=True):
    """Block until `gb` fits alongside Song Forge. Returns a lease id, or None
    if the guard is unreachable (caller proceeds) or the wait timed out.

    `honest` raises the ask to the measured peak for this kind of job when that
    is bigger than what the caller declared. Passing honest=False keeps the
    caller's number exactly (for a job that genuinely knows its own size)."""
    global _warned
    if honest:
        peak = hint(name)
        if peak > gb:
            ask = peak + 2
            # Never ask for more than the budget can ever grant. Measured
            # 2026-08-01: a live animate held 49GB against a 52GB budget, so the
            # NEXT one would have asked 51 and been refused by a couple of GB —
            # turning a render that just succeeded into a shot HELD five times
            # and then blocked. A learned peak must never ratchet a job out of
            # its own machine.
            s = state() or {}
            ceiling = (s.get("budget_gb") or 0) * 0.9
            if ceiling:
                ask = min(ask, ceiling)
            if ask > gb:
                print(f"[mem] {name}: asking for {ask:.0f}GB — measured, not the "
                      f"{gb:.0f}GB declared", flush=True)
                gb = round(ask, 1)
    q = urllib.parse.urlencode({"name": name, "gb": gb,
                                "timeout": timeout, "ttl": ttl})
    d, code = _call("GET", f"/api/wait?{q}", timeout=timeout + 30)
    if d is None:
        if not _warned:
            print(f"[mem] forge_guard unreachable at {GUARD} — proceeding unguarded",
                  file=sys.stderr, flush=True)
            _warned = True
        return None
    if d.get("granted"):
        if d.get("waited_s"):
            print(f"[mem] {name}: waited {d['waited_s']}s for {gb}GB", flush=True)
        return d["id"]
    print(f"[mem] {name}: not granted ({d.get('reason')}) — proceeding carefully",
          file=sys.stderr, flush=True)
    return None


def acquire_ex(name, gb, timeout=900, ttl=1800, honest=True):
    """(lease_id, status, granted_gb) — status is "granted"|"refused"|"unguarded".

    A caller that can defer its work needs to tell those last two apart: no
    guard means proceed (a broker must never be why a render can't start),
    but a REFUSAL means the room genuinely isn't there and starting anyway is
    how the box ends up in swap. `acquire` collapses both to None."""
    asked = max(gb, hint(name) + 2) if honest else gb
    lid = acquire(name, gb, timeout=timeout, ttl=ttl, honest=honest)
    if lid:
        return lid, "granted", asked
    return None, ("unguarded" if state() is None else "refused"), asked


def release(lease_id):
    if lease_id:
        _call("DELETE", f"/api/reserve?id={urllib.parse.quote(lease_id)}", timeout=10)


def heartbeat(lease_id, ttl=1800):
    if lease_id:
        _call("POST", "/api/heartbeat", {"id": lease_id, "ttl": ttl}, timeout=10)


@contextmanager
def reserve(name, gb, timeout=900, ttl=1800, honest=True):
    """Hold a reservation for the duration of the block, heartbeating so a long
    render doesn't lose its seat to the TTL."""
    lid = acquire(name, gb, timeout=timeout, ttl=ttl, honest=honest)
    stop = threading.Event()

    def beat():
        while not stop.wait(ttl / 3.0):
            heartbeat(lid, ttl)

    t = None
    if lid:
        t = threading.Thread(target=beat, daemon=True)
        t.start()
    try:
        yield lid
    finally:
        stop.set()
        release(lid)


@contextmanager
def reserve_ex(name, gb, timeout=900, ttl=1800, honest=True):
    """reserve(), but yields (lease_id, status) — see acquire_ex."""
    lid, status, _gb = acquire_ex(name, gb, timeout=timeout, ttl=ttl, honest=honest)
    stop = threading.Event()

    def beat():
        while not stop.wait(ttl / 3.0):
            heartbeat(lid, ttl)

    if lid:
        threading.Thread(target=beat, daemon=True).start()
    try:
        yield lid, status
    finally:
        stop.set()
        release(lid)


def _cli():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "state":
        print(json.dumps(state(), indent=2))
        return 0
    if cmd == "wait":
        name, gb = sys.argv[2], float(sys.argv[3])
        timeout = 900
        if "--timeout" in sys.argv:
            timeout = float(sys.argv[sys.argv.index("--timeout") + 1])
        lid = acquire(name, gb, timeout=timeout)
        print(lid or "")
        return 0 if lid else 1
    if cmd == "release":
        release(sys.argv[2])
        return 0
    print(f"unknown command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
