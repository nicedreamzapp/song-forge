"""
forge_server.py — Song Forge backend.

One job:
  Take a one-line idea (or nothing) → write lyrics → generate a real song with
  vocals via ACE-Step (local, MPS+MLX) → save to outputs/ → serve it.

Endpoints:
  GET  /              static index.html
  GET  /api/status    {ace_up, model_ready, jobs_queued, last_song?, dl_pct?}
  POST /api/song      {idea?, style?, lyrics?, voice?} -> {task_id}
  GET  /api/song/{id} {status: queued|running|done|error, audio?, lyrics?, ...}
  GET  /api/songs     list of generated songs
  GET  /audio/<file>  serves a wav from outputs/

Deliberately small. Everything else lives in ACE-Step (port 8001).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest, urlopen

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)
ACE = "http://127.0.0.1:8001"
PORT = 8767

# Where ACE-Step writes every generated wav before forge copies it to outputs/.
ACE_CACHE = ROOT / "engines" / "ACE-Step-1.5" / ".cache" / "acestep" / "tmp" / "api_audio"

# Voice-swap pipeline configuration.
SVC_DIR    = ROOT / "engines" / "seed-vc"
SVC_PY     = SVC_DIR / ".venv" / "bin" / "python"
DEMUCS_BIN = Path.home() / "Library" / "Python" / "3.9" / "bin" / "demucs"
SWAP_WORK  = ROOT / "voice_swap_work"
SWAP_WORK.mkdir(exist_ok=True)

# Voice library — scan known locations for sample wavs.
VOICE_DIRS = [
    Path.home() / "Library" / "Application Support" / "sh.voicebox.app",
    Path.home() / "Desktop" / "Content" / "AMBIENT EMPIRE" / "voice_clone_2026-05-01",
]


def _list_voices() -> list:
    """Return [{name, path, size_kb}] for every voice sample we can find."""
    voices = []
    seen = set()
    for d in VOICE_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.wav")):
            if p.stat().st_size < 50_000:  # skip tiny/empty samples
                continue
            if p in seen:
                continue
            seen.add(p)
            # Friendly display name from filename.
            stem = p.stem
            name = stem.replace("-voice-sample", "").replace("_voice", "").replace("voice-", "")
            name = name.replace("_", " ").replace("-", " ").strip().title() or stem
            voices.append({
                "name": name,
                "path": str(p),
                "size_kb": p.stat().st_size // 1024,
            })
    return voices

# ----- jobs registry (in-memory, restart-safe via outputs/ as ground truth) -----
JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


# ----- tiny lyric writer (template fallback when no local LLM is up) ---------
LYRIC_THEMES = [
    "morning light", "ancient road", "cool breeze", "river deep",
    "rising sun", "open sky", "neon dream", "burning fire",
    "city lights", "midnight rain", "soft thunder", "wild horses",
]
LYRIC_VERBS = [
    "rising", "falling", "running", "calling",
    "shining", "burning", "flowing", "turning",
    "waking", "breathing", "dreaming", "feeling",
]


LM_URL    = "http://127.0.0.1:1234/v1/chat/completions"
LM_MODEL  = "gemma-4-31b-it-abliterated"
LM_TIMEOUT = 90  # Gemma 4 31B ~5–20s for ~400 tokens on M5 Max


# Persistent ban list — lives next to outputs/ so it survives restarts.
BANNED_PATH = ROOT / ".banned_phrases.json"


def _load_banned() -> list:
    try:
        return json.loads(BANNED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_banned(items: list) -> None:
    cleaned = sorted({s.strip() for s in items if s and s.strip()}, key=str.lower)
    BANNED_PATH.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")


SYSTEM_PROMPT = (
    "You are a working songwriter — not an AI imitating a genre. Write lyrics that "
    "feel like a real human wrote them in a specific moment of their life.\n\n"
    "RULES:\n"
    "1. Output ONLY the lyrics, with [verse] and [chorus] section tags. No "
    "commentary, no explanations, no preamble.\n"
    "2. AVOID GENRE CLICHÉS. Don't reach for the obvious phrase — every genre has "
    "tropes that make lyrics feel generic. Reggae lyrics shouldn't all be 'concrete "
    "jungle / one love / Babylon'. Country lyrics shouldn't all be 'tailgate / "
    "moonshine / Friday night'. Find a specific human moment and write THAT.\n"
    "3. Use concrete sensory details: specific places, objects, weather, smells, "
    "textures, time of day. Avoid abstract emotion-words (love, freedom, hope, "
    "soul) unless they're earned by surrounding specificity.\n"
    "4. Fresh metaphors over stock ones. Surprise the listener.\n"
    "5. Lines must be short (4–8 words) and singable. Use the rhyme scheme the "
    "genre traditionally uses, but the imagery should be unexpected.\n"
    "6. If the user gave a THEME, anchor every verse in concrete details from "
    "that theme — real nouns, real places, real objects."
)


def _llm_lyrics(style: str = "", theme: str = "", banned: Optional[list] = None) -> Optional[str]:
    """Ask LM Studio (Gemma) for genre-appropriate lyrics. Returns None on any
    failure — caller falls back to _seed_lyrics()."""
    try:
        all_banned = list(_load_banned())
        if banned:
            all_banned += list(banned)
        all_banned = sorted({s.strip() for s in all_banned if s and s.strip()}, key=str.lower)
        ban_block = (
            "\n\nFORBIDDEN — do not use these words or phrases at all:\n"
            + "\n".join(f"  - {b}" for b in all_banned)
        ) if all_banned else ""

        style_part = (style or "pop song").strip()
        theme_part = (f"\nTHEME (anchor the lyrics in this — use concrete details from it): {theme}." if theme else "").strip()
        user_prompt = (
            f"Write a 2-minute song.\n"
            f"STYLE: {style_part}.{theme_part}\n\n"
            "Structure:\n"
            "[verse]  4 short lines\n"
            "[chorus] 4 short lines\n"
            "[verse]  4 short lines (DIFFERENT imagery from verse 1)\n"
            "[chorus] same chorus repeated\n"
            f"{ban_block}\n\n"
            "Output the lyrics now."
        )
        body = json.dumps({
            "model": LM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.95,
            "top_p": 0.92,
            "frequency_penalty": 0.6,  # discourage repeating its own clichés
            "presence_penalty":  0.4,
            "max_tokens": 600,
        }).encode("utf-8")
        req = UrlRequest(LM_URL, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=LM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not msg or "[verse]" not in msg.lower():
            return None
        # Belt-and-braces: if the model still slipped a banned phrase in,
        # null out so the caller can retry or fall back. (Case-insensitive.)
        low = msg.lower()
        for b in all_banned:
            if b.lower() in low:
                print(f"[llm_lyrics] reject — slipped banned phrase: {b!r}", flush=True)
                return None
        return msg
    except Exception as e:
        print(f"[llm_lyrics] {e}", flush=True)
        return None


def _seed_lyrics() -> str:
    """Cheap lyric scaffold for when the user leaves Lyrics blank. Pure random
    English so the song has SOMETHING to sing — never includes user-supplied
    style/theme text (that belongs in the style prompt, not the vocal track)."""
    import random
    rnd = random.Random(time.time())
    t1, t2, t3 = rnd.sample(LYRIC_THEMES, 3)
    v1, v2, v3 = rnd.sample(LYRIC_VERBS, 3)
    return (
        "[verse]\n"
        f"{t1} keeps {v1}\n"
        f"every step a {t2} {v2}\n"
        f"all i need is {t3} {v3}\n"
        f"keep on going, keep on flowing\n"
        "[chorus]\n"
        f"feel it now, feel it loud\n"
        f"{t1} keeps {v1}\n"
        f"never stopping, never falling\n"
        f"this is how we live alive\n"
        "[verse]\n"
        f"{t1} on my mind\n"
        f"{t2} in my soul\n"
        f"{t3} in my eyes\n"
        f"and i'm {v1} home\n"
    )


def _seed_prompt(style: Optional[str], idea: Optional[str], bpm: Optional[float] = None) -> str:
    """Build the music-style prompt for ACE-Step. The user's `style` text is
    authoritative — we only append a default vocal hint if they haven't already
    specified one (otherwise hard-coding 'expressive male vocal' silently
    overrides duet/falsetto/female/choir requests)."""
    style = (style or "uplifting reggae groove with male vocals, warm bass, conga drums").strip()
    bpm_val = int(bpm) if bpm and 40 <= bpm <= 220 else 88
    style_l = style.lower()
    has_vocal_hint = any(k in style_l for k in (
        "vocal", "voice", "singer", "duet", "choir", "harmony", "harmonies",
        "falsetto", "baritone", "tenor", "soprano", "alto", "rap", "spoken",
        "instrumental", "no vocal",
    ))
    parts = [style, f"{bpm_val} bpm", "clean mix"]
    if not has_vocal_hint:
        parts.append("expressive male vocal")
    return ", ".join(parts)


# ----- sidecar persistence ---------------------------------------------------
# JOBS is in-memory; we mirror finished jobs to outputs/<id>.json so the
# library survives a forge_server restart.
SIDECAR_FIELDS = (
    "id", "status", "ace_task_id", "prompt", "lyrics",
    "idea", "style", "title", "created_at", "finished_at",
    "audio", "progress", "stage", "duration", "bpm",
    "ace_cache_files",  # absolute paths to original ACE-Step outputs we should
                        # delete alongside outputs/<id>.wav when the user hits ✕
)


def _sidecar_path(jid: str) -> Path:
    return OUT / f"{jid}.json"


def _save_sidecar(job: Dict[str, Any]) -> None:
    try:
        snap = {k: job.get(k) for k in SIDECAR_FIELDS if k in job}
        _sidecar_path(job["id"]).write_text(
            json.dumps(snap, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print("[sidecar save]", e, flush=True)


# Where we drop a properly-named, ID3-tagged mp3 every time a song finishes,
# so the user can drag the folder into Music.app and have correct titles.
EXPORTS = ROOT / "exports"
EXPORTS.mkdir(exist_ok=True)


def _safe_filename(s: str) -> str:
    s = (s or "untitled").strip()
    s = re.sub(r'[^\w\s\-\(\)\.,!\']+', '', s)
    return (s[:120] or "untitled")


def _export_tagged_mp3(job: Dict[str, Any]) -> None:
    """Write a 320k mp3 to exports/ with proper ID3 metadata so the song
    shows up named correctly in Music.app instead of as a UUID hex hash."""
    try:
        jid = job.get("id")
        wav = OUT / f"{jid}.wav"
        if not wav.is_file():
            return
        title = _safe_filename(job.get("title") or job.get("idea") or jid)
        out_mp3 = EXPORTS / f"{title}.mp3"
        if out_mp3.is_file():
            # Already exported (probably a re-render of the same title).
            return
        style = (job.get("style") or "")[:300]
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav),
             "-codec:a", "libmp3lame", "-b:a", "320k",
             "-metadata", f"title={title}",
             "-metadata", "artist=Matt Macosko (AI · Song Forge)",
             "-metadata", "album=Song Forge — first sessions",
             "-metadata", "genre=AI Song Forge",
             "-metadata", f"comment={style}",
             str(out_mp3)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        print(f"[export_mp3] {e}", flush=True)


def _hydrate_jobs() -> int:
    """Load any sidecar JSONs from outputs/ into the in-memory JOBS dict on
    startup, so the library persists across forge_server restarts."""
    n = 0
    for p in OUT.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            jid = data.get("id")
            if not jid:
                continue
            wav = OUT / f"{jid}.wav"
            if not wav.is_file():
                continue  # sidecar without audio = stale; skip
            JOBS[jid] = data
            n += 1
        except Exception as e:
            print(f"[hydrate] {p.name}: {e}", flush=True)
    return n


# ----- voice-swap pipeline (demucs → seed-vc → ffmpeg) -----------------------
def _run_swap(jid: str) -> None:
    """Background pipeline: demucs split → seed-vc convert → ffmpeg mix.
    Updates the JOBS[jid] record at every stage so the UI can poll progress."""
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if not job:
        return
    src_wav = Path(job["src_wav"])
    voice_path = Path(job["voice_path"])
    work = SWAP_WORK / jid
    work.mkdir(exist_ok=True)

    def _set(stage: str, progress: float):
        with JOBS_LOCK:
            job["stage"] = stage
            job["progress"] = progress
            job["status"] = "running"

    try:
        # Step 1 — demucs split
        _set("splitting vocals from instrumental (demucs)…", 0.10)
        r = subprocess.run(
            [str(DEMUCS_BIN), "--two-stems", "vocals", "-d", "cpu", "-o", str(work), str(src_wav)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            raise RuntimeError(f"demucs failed: {r.stderr[-500:]}")

        # demucs writes to <work>/htdemucs/<basename>/{vocals,no_vocals}.wav
        stem = src_wav.stem
        vocals = work / "htdemucs" / stem / "vocals.wav"
        no_vocals = work / "htdemucs" / stem / "no_vocals.wav"
        if not vocals.is_file() or not no_vocals.is_file():
            raise RuntimeError(f"demucs output not found at {vocals}")

        # Step 2 — seed-vc voice conversion
        _set(f"converting vocals to {job['voice_name']} (seed-vc)…", 0.50)
        r = subprocess.run(
            [str(SVC_PY), "inference.py",
             "--source", str(vocals),
             "--target", str(voice_path),
             "--output", str(work),
             "--f0-condition", "true",
             "--auto-f0-adjust", "true",
             "--diffusion-steps", "25"],
            capture_output=True, text=True, timeout=600, cwd=str(SVC_DIR),
        )
        if r.returncode != 0:
            raise RuntimeError(f"seed-vc failed: {r.stderr[-500:]}")

        # seed-vc writes vc_<source-stem>_<target-stem>_<...>.wav to --output dir
        converted = sorted(work.glob("vc_vocals_*.wav"), key=lambda p: p.stat().st_mtime)
        if not converted:
            raise RuntimeError("seed-vc produced no converted wav")
        converted_vox = converted[-1]

        # Step 3 — ffmpeg mix.
        #
        # If group_effect is on, layer 4 copies of the converted vocal with
        # slight pitch + timing + pan variation so a single voice clone
        # sounds like a small group of kids singing in unison. asetrate
        # changes pitch, atempo restores duration. adelay offsets each
        # layer slightly so they don't phase-cancel.
        out_wav = OUT / f"{jid}.wav"
        if job.get("group_effect"):
            _set("mixing converted vocals as a group of kids singing…", 0.85)
            filter_complex = (
                # 4 voice layers, each pitched/delayed/panned differently.
                "[1:a]asplit=4[v1][v2][v3][v4];"
                "[v1]asetrate=44100*1.000,aresample=44100,atempo=1.000,adelay=0|0,pan=stereo|c0=0.6*c0|c1=0.6*c0[L1];"
                "[v2]asetrate=44100*1.024,aresample=44100,atempo=0.977,adelay=22|22,pan=stereo|c0=0.5*c0|c1=0.7*c0[L2];"
                "[v3]asetrate=44100*0.984,aresample=44100,atempo=1.016,adelay=14|14,pan=stereo|c0=0.7*c0|c1=0.5*c0[L3];"
                "[v4]asetrate=44100*1.012,aresample=44100,atempo=0.988,adelay=8|8,pan=stereo|c0=0.55*c0|c1=0.55*c0[L4];"
                "[L1][L2][L3][L4]amix=inputs=4:duration=longest:normalize=0,volume=1.10[choir];"
                "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.95[bed];"
                "[bed][choir]amix=inputs=2:duration=longest:normalize=0[mix];"
                "[mix]loudnorm=I=-14:TP=-1.5:LRA=11"
            )
        else:
            _set("mixing converted vocals over the instrumental…", 0.85)
            filter_complex = (
                "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=0.95[bed];"
                "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,volume=1.20[vox];"
                "[bed][vox]amix=inputs=2:duration=longest:normalize=0[mix];"
                "[mix]loudnorm=I=-14:TP=-1.5:LRA=11"
            )
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(no_vocals), "-i", str(converted_vox),
             "-filter_complex", filter_complex,
             "-ar", "48000", "-ac", "2", "-sample_fmt", "s16",
             str(out_wav)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {r.stderr[-500:]}")

        with JOBS_LOCK:
            job["status"] = "done"
            job["audio"] = f"/audio/{out_wav.name}"
            job["finished_at"] = time.time()
            job["progress"] = 1.0
            job["stage"] = "succeeded"
            _save_sidecar(job)

        # Tidy up the working directory now we have the final wav.
        try:
            shutil.rmtree(work)
        except Exception:
            pass
    except Exception as e:
        with JOBS_LOCK:
            job["status"] = "error"
            job["last_error"] = str(e)[:500]
        print(f"[swap {jid}] {e}", flush=True)


# ----- ACE-Step bridge -------------------------------------------------------
def _ace_post(path: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    req = UrlRequest(
        ACE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ace_get(path: str, timeout: int = 10) -> Dict[str, Any]:
    with urlopen(ACE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_ACE_STATE = {"alive": False, "ts": 0.0}


def _ace_heartbeat_loop():
    """Background heartbeat. /api/status reads the cached flag — never blocks
    the request thread waiting on ACE while it's slammed by model downloads."""
    while True:
        try:
            _ace_get("/health", timeout=4)
            _ACE_STATE["alive"] = True
        except Exception:
            _ACE_STATE["alive"] = False
        _ACE_STATE["ts"] = time.time()
        time.sleep(3)


def _ace_alive() -> bool:
    return _ACE_STATE["alive"]


# tqdm progress lines look like:
#   Downloading [acestep-5Hz-lm-1.7B/model.safetensors]:  63%|...| 2.19G/3.45G [06:02<02:02, 11.1MB/s]
_DL_RE = re.compile(
    r"Downloading\s*\[(?P<file>[^\]]+)\]:\s*(?P<pct>\d+)%[^|]*\|[^|]*\|\s*"
    r"(?P<done>[0-9.]+[KMG])/(?P<total>[0-9.]+[KMG])\s*\[(?P<elapsed>[^<]+)<"
    r"(?P<eta>[^,]+),\s*(?P<speed>[^\]]+)\]"
)
ACE_LOG = Path("/tmp/song_forge_ace.log")


def _ace_download_status() -> Optional[Dict[str, Any]]:
    """Tail the ACE-Step log and pull the most recent line per file. Returns a dict
    summarising overall download state, or None if no download lines seen yet."""
    if not ACE_LOG.exists():
        return None
    try:
        with open(ACE_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # Read last ~64 KB — plenty to capture the latest tqdm flush per file.
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return None

    files: Dict[str, Dict[str, str]] = {}
    for m in _DL_RE.finditer(tail):
        files[m.group("file")] = {
            "file": m.group("file"),
            "pct": int(m.group("pct")),
            "done": m.group("done"),
            "total": m.group("total"),
            "eta": m.group("eta").strip(),
            "speed": m.group("speed").strip(),
        }
    if not files:
        return None
    items = list(files.values())
    finished = sum(1 for it in items if it["pct"] >= 100)
    avg = round(sum(it["pct"] for it in items) / len(items))
    # Surface the slowest in-progress file as the user-facing line.
    inflight = [it for it in items if it["pct"] < 100]
    headline = min(inflight, key=lambda it: it["pct"]) if inflight else items[0]
    return {
        "overall_pct": avg,
        "files_total": len(items),
        "files_done": finished,
        "headline": headline,
        "files": items,
    }


# ----- background worker: poll ACE for each job we kicked off ----------------
def _worker():
    while True:
        try:
            with JOBS_LOCK:
                pending = [j for j in JOBS.values() if j["status"] in ("queued", "running")]
            for job in pending:
                tid = job["ace_task_id"]
                try:
                    res = _ace_post(
                        "/query_result",
                        {"task_id_list": json.dumps([tid])},
                        timeout=10,
                    )
                except Exception as e:
                    job["last_error"] = f"poll: {e}"
                    continue

                # ACE-Step shape:  {data:[{task_id, result:"[{file,wave,status,progress,stage}]", status:int, progress_text:str}]}
                # `data` is a list. `result` is a JSON STRING that decodes to a list of result dicts.
                data_list = res.get("data") or []
                if not data_list:
                    continue
                envelope = data_list[0]
                try:
                    inner = json.loads(envelope.get("result") or "[]")
                except Exception:
                    inner = []
                first = inner[0] if inner else {}
                # status: 0 = pending/running, 1 = done, -1/2/3 = errored variants.
                ace_status = first.get("status", envelope.get("status", 0))
                progress = float(first.get("progress") or 0.0)
                stage = first.get("stage") or envelope.get("progress_text") or ""
                audio_path = first.get("file") or first.get("wave") or ""

                # ACE renders multiple audio variants per request; capture every
                # filesystem path it returned so DELETE can scrub all of them.
                cache_files = []
                for v in inner:
                    p = v.get("file") or v.get("wave") or ""
                    if p.startswith("/v1/audio"):
                        q = parse_qs(urlparse(p).query)
                        if q.get("path"):
                            cache_files.append(q["path"][0])
                    elif p.startswith("/"):
                        cache_files.append(p)
                if cache_files:
                    job["ace_cache_files"] = cache_files

                if ace_status == 1 or (audio_path and progress >= 0.999):
                    state = "done"
                elif ace_status in (-1, 2, 3) or (stage and "error" in stage.lower()):
                    state = "error"
                else:
                    state = "running" if progress > 0 else "queued"

                job["progress"] = progress
                job["stage"] = stage
                if state == "running":
                    job["status"] = "running"
                elif state == "queued":
                    job["status"] = "queued"
                elif state == "done":
                    if audio_path:
                        # ACE returns audio_path in one of two shapes:
                        #   1) "/v1/audio?path=%2Fabs%2Fpath.wav"  — relative URL with the
                        #      filesystem path URL-encoded in the query string.
                        #   2) "/abs/path.wav"                     — bare filesystem path.
                        # Earlier code blindly did f"{ACE}/v1/audio?path={audio_path}",
                        # which double-wrapped form (1) into "/v1/audio?path=/v1/audio?path=…"
                        # and ACE answered 403 Forbidden. Since the file is local, just
                        # copy it directly off disk.
                        local = OUT / f"{job['id']}.wav"
                        try:
                            src: Optional[Path] = None
                            if audio_path.startswith("/v1/audio"):
                                q = parse_qs(urlparse(audio_path).query)
                                if q.get("path"):
                                    src = Path(q["path"][0])
                            elif audio_path.startswith("/") and Path(audio_path).is_file():
                                src = Path(audio_path)

                            if src and src.is_file():
                                shutil.copyfile(src, local)
                            else:
                                # Last-resort HTTP fallback (remote ACE / unusual path shape).
                                url = f"{ACE}{audio_path}" if audio_path.startswith("/") \
                                      else f"{ACE}/v1/audio?path={audio_path}"
                                req = UrlRequest(url, headers={"Accept": "audio/wav"})
                                with urlopen(req, timeout=120) as r, open(local, "wb") as f:
                                    shutil.copyfileobj(r, f)
                            job["status"] = "done"
                            job["audio"] = f"/audio/{local.name}"
                            job["finished_at"] = time.time()
                            _save_sidecar(job)
                            _export_tagged_mp3(job)
                        except Exception as e:
                            job["status"] = "error"
                            job["last_error"] = f"audio fetch: {e}"
                    else:
                        job["status"] = "error"
                        job["last_error"] = "no audio_path in result"
                elif state == "error":
                    job["status"] = "error"
                    job["last_error"] = stage or "unknown"
        except Exception as e:
            print("[worker]", e, flush=True)
        time.sleep(2)


# ----- HTTP handler ----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _json(self, obj: Any, code: int = 200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter logs
        # args[0] is the requestline string for log_request, but
        # log_error passes (int_code, str_message) — guard against both.
        first = args[0] if args else ""
        if isinstance(first, str) and "/api/" in first:
            return
        super().log_message(fmt, *args)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._file(ROOT / "index.html", "text/html; charset=utf-8")

        # Static JS / CSS / image assets sitting next to index.html.
        # Path is restricted to plain filenames in ROOT — dots allowed in the
        # basename (three.min.js etc), but ".." sequences forbidden, and
        # Path() ensures we never escape ROOT.
        m = re.match(r"^/([\w\-][\w\-\.]*\.(?:js|css|png|jpg|jpeg|svg|ico|woff2?))$", u.path)
        if m and ".." not in m.group(1):
            ctype = {
                "js":   "application/javascript; charset=utf-8",
                "css":  "text/css; charset=utf-8",
                "png":  "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "svg":  "image/svg+xml", "ico": "image/x-icon",
                "woff": "font/woff", "woff2": "font/woff2",
            }.get(m.group(1).rsplit(".", 1)[-1].lower(), "application/octet-stream")
            return self._file(ROOT / m.group(1), ctype)
        if u.path == "/api/status":
            with JOBS_LOCK:
                latest = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            return self._json({
                "ace_up": _ace_alive(),
                "jobs_total": len(latest),
                "jobs_running": sum(1 for j in latest if j["status"] in ("queued","running")),
                "last": latest[0] if latest else None,
                "download": _ace_download_status(),
            })
        if u.path == "/api/songs":
            with JOBS_LOCK:
                rows = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            return self._json({"songs": rows[:50]})
        if u.path == "/api/voices":
            return self._json({"voices": _list_voices()})
        if u.path == "/api/random_lyrics":
            qs = parse_qs(u.query)
            style = (qs.get("style", [""])[0] or "").strip()
            theme = (qs.get("theme", [""])[0] or "").strip()
            # Retry up to 3 times — first attempts can return None if Gemma
            # slipped a banned phrase past the filter.
            llm = None
            for _ in range(3):
                llm = _llm_lyrics(style=style, theme=theme)
                if llm:
                    break
            if llm:
                return self._json({"lyrics": llm, "source": "llm"})
            return self._json({"lyrics": _seed_lyrics(), "source": "template"})

        if u.path == "/api/banned":
            return self._json({"banned": _load_banned()})
        m = re.match(r"^/api/song/([\w\-]{1,80})$", u.path)
        if m:
            with JOBS_LOCK:
                j = JOBS.get(m.group(1))
            if not j: return self._json({"error":"not found"}, 404)
            return self._json(j)
        if u.path.startswith("/audio/"):
            name = u.path.split("/", 2)[-1]
            return self._file(OUT / name, "audio/wav")
        self.send_error(404)

    def do_DELETE(self):
        u = urlparse(self.path)

        def _remove_job(jid: str, job: Optional[Dict[str, Any]]):
            """Hard-delete every trace of a job: forge wav + sidecar +
            both ACE-Step cache variants (the rendered one and its sibling)."""
            paths = [OUT / f"{jid}.wav", OUT / f"{jid}.json"]
            for p in (job or {}).get("ace_cache_files") or []:
                paths.append(Path(p))
            for f in paths:
                try:
                    if f.is_file(): f.unlink()
                except Exception:
                    pass

        # Bulk clear: remove every finished job + its wav + sidecar + ACE cache.
        if u.path == "/api/songs":
            removed = 0
            with JOBS_LOCK:
                ids = list(JOBS.keys())
                for jid in ids:
                    job = JOBS.get(jid)
                    if not job or job.get("status") not in ("done", "error"):
                        continue
                    JOBS.pop(jid, None)
                    _remove_job(jid, job)
                    removed += 1
            return self._json({"cleared": removed})

        m = re.match(r"^/api/song/([\w\-]{1,80})$", u.path)
        if not m:
            self.send_error(404); return
        jid = m.group(1)
        with JOBS_LOCK:
            job = JOBS.pop(jid, None)
        try:
            _remove_job(jid, job)
        except Exception as e:
            return self._json({"deleted": False, "error": str(e)}, 500)
        return self._json({"deleted": True, "job_was_present": bool(job)})

    def do_PATCH(self):
        """Rename / retitle a song."""
        u = urlparse(self.path)
        m = re.match(r"^/api/song/([\w\-]{1,80})$", u.path)
        if not m:
            self.send_error(404); return
        jid = m.group(1)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            body = {}
        title = (body.get("title") or "").strip()[:200]
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job: return self._json({"error": "not found"}, 404)
            job["title"] = title
            _save_sidecar(job)
        return self._json({"ok": True, "title": title})

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            body = {}

        if u.path == "/api/song":
            # "idea" is legacy — we no longer feed it into lyrics (it was leaking
            # style descriptions into the sung vocal). Style + Lyrics are now the
            # two distinct user inputs; idea is kept only as library metadata.
            idea = (body.get("idea") or "").strip()
            style = (body.get("style") or "").strip()
            title = (body.get("title") or "").strip()[:200]
            lyrics = (body.get("lyrics") or "").strip()
            if not lyrics:
                # Retry up to 3 times if Gemma slips a banned phrase past
                # the system prompt and trips the post-filter.
                for _ in range(3):
                    lyrics = _llm_lyrics(style=style, theme=title or idea) or ""
                    if lyrics:
                        break
                if not lyrics:
                    lyrics = _seed_lyrics()
            try:
                bpm_in = float(body.get("bpm")) if body.get("bpm") else None
            except Exception:
                bpm_in = None
            prompt = _seed_prompt(style, idea, bpm_in)

            try:
                duration = float(body.get("duration") or 120.0)
            except Exception:
                duration = 120.0
            duration = max(15.0, min(duration, 240.0))

            try:
                language = (body.get("language") or "en").strip().lower() or "en"
                resp = _ace_post(
                    "/release_task",
                    {
                        "prompt": prompt,
                        "lyrics": lyrics,
                        "vocal_language": language,
                        "task_type": "text2music",
                        "inference_steps": 8,
                        "guidance_scale": 7.0,
                        "audio_format": "wav",
                        "audio_duration": duration,
                    },
                    timeout=15,
                )
            except Exception as e:
                return self._json({"error": f"ACE submit failed: {e}"}, 502)

            ace_tid = (resp.get("data") or {}).get("task_id")
            if not ace_tid:
                return self._json({"error": "no task_id from ACE", "ace_response": resp}, 502)

            jid = uuid.uuid4().hex
            with JOBS_LOCK:
                JOBS[jid] = {
                    "id": jid,
                    "status": "queued",
                    "ace_task_id": ace_tid,
                    "prompt": prompt,
                    "lyrics": lyrics,
                    "idea": idea,
                    "style": style,
                    "title": title,
                    "duration": duration,
                    "bpm": int(bpm_in) if bpm_in else None,
                    "created_at": time.time(),
                }
            return self._json({"id": jid})

        # Purge ACE cache files that aren't referenced by any current job.
        # This cleans up the orphaned 2nd variant ACE always renders, plus any
        # leftovers from prior sessions before we tracked cache file paths.
        if u.path == "/api/purge_cache":
            referenced = set()
            with JOBS_LOCK:
                for j in JOBS.values():
                    for p in j.get("ace_cache_files") or []:
                        referenced.add(Path(p).resolve())
            removed = 0
            freed_bytes = 0
            try:
                for f in ACE_CACHE.glob("*.wav"):
                    if f.resolve() in referenced:
                        continue
                    try:
                        sz = f.stat().st_size
                        f.unlink()
                        removed += 1
                        freed_bytes += sz
                    except Exception:
                        pass
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            return self._json({
                "removed": removed,
                "freed_mb": round(freed_bytes / 1_048_576, 1),
                "kept_referenced": len(referenced),
            })

        # Replace the banned-phrases list (full overwrite).
        if u.path == "/api/banned":
            items = body.get("banned") or []
            if not isinstance(items, list):
                return self._json({"error": "banned must be a list of strings"}, 400)
            _save_banned(items)
            return self._json({"banned": _load_banned()})

        # Voice swap — kicks off a background pipeline that splits the song,
        # converts the vocal stem to the target voice, and mixes back. The
        # result lands in the library as a new entry tagged "(voice: name)".
        m = re.match(r"^/api/swap_voice/([\w\-]{1,80})$", u.path)
        if m:
            src_jid = m.group(1)
            voice_path = (body.get("voice_path") or "").strip()
            voice_name = (body.get("voice_name") or "voice").strip()
            if not voice_path or not Path(voice_path).is_file():
                return self._json({"error": "voice_path missing or not a file"}, 400)
            with JOBS_LOCK:
                src = JOBS.get(src_jid)
            if not src or src.get("status") != "done" or not src.get("audio"):
                return self._json({"error": "source song not found or not done"}, 404)
            src_wav = OUT / Path(src["audio"]).name
            if not src_wav.is_file():
                return self._json({"error": f"source wav missing: {src_wav.name}"}, 404)

            group_effect = bool(body.get("group_effect"))
            new_jid = uuid.uuid4().hex
            base_title = src.get("title") or src.get("idea") or "song"
            tag = f"group of {voice_name}" if group_effect else f"voice: {voice_name}"
            new_title = f"{base_title} ({tag})"
            with JOBS_LOCK:
                JOBS[new_jid] = {
                    "id": new_jid,
                    "status": "queued",
                    "kind": "swap",
                    "title": new_title,
                    "idea": src.get("idea", ""),
                    "style": f"voice-swapped to {tag}",
                    "lyrics": src.get("lyrics", ""),
                    "duration": src.get("duration"),
                    "src_wav": str(src_wav),
                    "src_jid": src_jid,
                    "voice_name": voice_name,
                    "voice_path": voice_path,
                    "group_effect": group_effect,
                    "created_at": time.time(),
                    "progress": 0.0,
                    "stage": "queued",
                }
            threading.Thread(target=_run_swap, args=(new_jid,), daemon=True).start()
            return self._json({"id": new_jid})

        # Reveal the wav in Finder. Local-only — no path traversal risk because
        # we only ever open OUT/<id>.wav by id.
        m = re.match(r"^/api/reveal/([\w\-]{1,80})$", u.path)
        if m:
            jid = m.group(1)
            wav = OUT / f"{jid}.wav"
            if not wav.is_file():
                return self._json({"error": "not found"}, 404)
            try:
                subprocess.Popen(["/usr/bin/open", "-R", str(wav)])
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        self.send_error(404)


def main():
    n = _hydrate_jobs()
    print(f"[forge] hydrated {n} song(s) from outputs/", flush=True)
    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_ace_heartbeat_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[forge] http://127.0.0.1:{PORT}/   (ACE-Step at {ACE})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
