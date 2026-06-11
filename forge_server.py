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

FFMPEG = next(
    (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg")
     if os.path.isfile(p)),
    "ffmpeg",
)

# Where ACE-Step writes every generated wav before forge copies it to outputs/.
ACE_CACHE = ROOT / "engines" / "ACE-Step-1.5" / ".cache" / "acestep" / "tmp" / "api_audio"

# Voice-swap pipeline configuration.
SVC_DIR    = ROOT / "engines" / "seed-vc"
SVC_PY     = SVC_DIR / ".venv" / "bin" / "python"
DEMUCS_BIN = Path.home() / "Library" / "Python" / "3.9" / "bin" / "demucs"
SWAP_WORK  = ROOT / "voice_swap_work"
SWAP_WORK.mkdir(exist_ok=True)

# whisper-cpp for capturing what was ACTUALLY sung after generation. ACE-Step
# improvises and skips lines, so the prompt's lyrics field doesn't always
# match the audio. We transcribe the rendered wav and store as lyrics_actual.
WHISPER_BIN   = Path("/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = Path.home() / "whisper-models" / "ggml-small.en.bin"

# Voice library — scan known locations for sample wavs.
VOICE_DIRS = [
    Path.home() / "Library" / "Application Support" / "sh.voicebox.app",
    Path.home() / "Desktop" / "Content" / "AMBIENT EMPIRE" / "voice_clone_2026-05-01",
    ROOT / "voice_refs",
]
(ROOT / "voice_refs").mkdir(exist_ok=True)

# Genres whose vocal default in ACE-Step otherwise drifts toward a thin
# white-pop tenor. When the user picks any of these we auto-append timbre
# cues so the lead reads as Black/African American instead of being silently
# whitened by the model's training-data bias.
BLACK_ROOTED_GENRE_KEYS = (
    "gospel", "soul", "r&b", "rnb", "rhythm and blues",
    "doo-wop", "doo wop", "doowop", "motown",
    "blues", "delta blues", "chicago blues", "jump blues",
    "funk", "p-funk", "p funk",
    "hip-hop", "hip hop", "rap", "trap",
    "reggae", "dancehall", "ska", "rocksteady", "dub",
    "afrobeat", "afro-beat", "afrobeats", "highlife",
    "african", "west african", "east african", "swahili",
    "neo soul", "neo-soul",
)
BLACK_VOCAL_REINFORCEMENT = (
    "Black African American male lead vocalist with deep rich gospel-rooted "
    "timbre, natural blues-tinged melisma and grain (Sonny Til, Clyde McPhatter, "
    "Sam Cooke, Otis Redding voice type), NOT a thin clear-toned white pop "
    "tenor, soulful church-trained delivery"
)

# Hip-hop / rap is rhythmic speech, not melodic singing — the gospel-singer
# reinforcement above pushes rap output toward a sung/melodic vocal that drifts
# white. Use this rap-specific framing instead. Locked in 2026-05-08 after Matt
# confirmed the recipe produced the right male MC sound on the knowledge batch.
HIPHOP_GENRE_KEYS = (
    "hip-hop", "hip hop", "rap", "trap", "drill", "boom-bap", "boom bap",
    "emcee", " mc ", "rapper",
)
HIPHOP_VOCAL_REINFORCEMENT = (
    "Black male MC, African American emcee, deep Black male baritone rap voice, "
    "PURE hip-hop in the Public Enemy / KRS-One / Black Thought / Mos Def / "
    "Talib Kweli / Killer Mike / Joey Bada$$ / Royce 5'9 / Immortal Technique "
    "lineage, gritty Black male rap delivery, scratched chorus samples, "
    "vinyl crackle warmth, "
    "NO electric guitar, NO rock drums, NO nu-metal, NO rap-rock fusion, NO punk, "
    "NOT white, NOT pop, NOT indie, NOT alt-rock, NOT rap-metal, "
    "raw Black conscious hip-hop authenticity"
)

# Roots reggae / dancehall / dub / ska is rhythmic-chant Rasta vocal style, NOT
# gospel melisma. The generic BLACK_VOCAL_REINFORCEMENT above pulls reggae toward
# Sam Cooke / Otis Redding curls on a one-drop riddim, which reads as a thin
# white pop tenor doing soul runs over a reggae beat — Matt called this
# "too white" 2026-05-13. Use the chesty, Rasta-rooted recipe below instead.
REGGAE_GENRE_KEYS = (
    "reggae", "roots reggae", "roots-reggae", "dancehall", "dance hall",
    "rocksteady", "rock steady", "ska", "dub reggae", "dub-reggae",
    "nyabinghi", "rasta", "rastafarian",
)
REGGAE_VOCAL_REINFORCEMENT = (
    "Black Jamaican Rastafarian male lead vocalist with chesty resonant chant-toned "
    "delivery in the Burning Spear / Bob Marley / Peter Tosh / Sizzla / Capleton / "
    "Buju Banton / Jacob Miller lineage, deep roots-reggae timbre, patois inflection, "
    "earthy organic Nyabinghi-rooted voice, "
    "NOT gospel melisma, NOT soul curls, NOT R&B runs, "
    "NOT a thin clear-toned white pop tenor, NOT pop-reggae, NOT UB40 style, "
    "NOT light Caribbean lilt — heavy chesty Rasta authenticity"
)

# Auto voice-swap registry. When a song's style matches one of these keyword
# clusters, the worker auto-queues a seed-vc voice-swap to retimbre the lead
# vocal onto a real Black artist's voice — because ACE-Step's default vocal
# distribution drifts white and prompt cues alone don't reliably override it.
# First matching entry wins. Drop new reference wavs in voice_refs/ and add
# rows here.
BLACK_VOICE_REGISTRY = [
    # Roots reggae lineage — chesty Rasta chant tone. Listed first so reggae
    # styles never fall through to the gospel/soul Sam Cooke entry (whose
    # keyword list contains "blues" which used to swallow reggae prompts that
    # mentioned blue notes or any blue/blues word).
    {
        "gender": "male",
        "keywords": (
            "reggae", "roots reggae", "roots-reggae",
            "dancehall", "dance hall",
            "rocksteady", "rock steady",
            "dub reggae", "dub-reggae",
            "ska", "nyabinghi", "rastafarian",
            "burning spear", "bob marley", "peter tosh",
            "sizzla", "capleton", "buju banton", "jacob miller",
        ),
        "voice_name": "Burning Spear",
        "voice_path": ROOT / "voice_refs" / "burning_spear.wav",
    },
    # Delta blues lineage — RJ's sharp haunted Mississippi voice. Listed before
    # Sam Cooke so Delta/country-blues phrases beat the broader Sam Cooke entry.
    {
        "gender": "male",
        "keywords": (
            "delta blues", "country blues", "mississippi blues",
            "acoustic blues", "rural blues", "prewar blues",
            "robert johnson", "son house", "skip james",
        ),
        "voice_name": "Robert Johnson",
        "voice_path": ROOT / "voice_refs" / "robert_johnson.wav",
    },
    # Gospel/soul/R&B/doo-wop/Chicago-blues/funk lineage — Sam Cooke.
    {
        "gender": "male",
        "keywords": (
            "gospel", "doo-wop", "doo wop", "doowop", "soul", "deep soul",
            "neo soul", "neo-soul", "r&b", "rnb", "rhythm and blues",
            "motown", "blues", "chicago blues", "jump blues",
            "funk", "p-funk", "p funk",
        ),
        "voice_name": "Sam Cooke (Soul Stirrers)",
        "voice_path": ROOT / "voice_refs" / "sam_cooke_soul_stirrers.wav",
    },
    # Female lineage — Mahalia Jackson covers gospel/soul/R&B/blues/funk.
    {
        "gender": "female",
        "keywords": (
            "gospel", "doo-wop", "doo wop", "doowop", "soul", "deep soul",
            "neo soul", "neo-soul", "r&b", "rnb", "rhythm and blues",
            "motown", "blues", "delta blues", "chicago blues", "jump blues",
            "funk", "p-funk", "p funk",
        ),
        "voice_name": "Mahalia Jackson",
        "voice_path": ROOT / "voice_refs" / "mahalia_jackson.wav",
    },
]
AUTO_BLACKIFY = True  # default ON — Black-rooted genres (gospel/soul/blues/funk/etc) auto-swap to the right reference voice. Override with explicit voice_path or auto_voice_assist:false per-request.


_FEM_PATTERNS = [
    r"\bfemale\s+(?:vocal|voice|lead|singer|tenor|alto|soprano|contralto|mc|rapper)",
    r"\bwoman\s+(?:vocal|voice|singer|lead)",
    r"\b(?:her\s+(?:vocal|voice)|she\s+sings|diva|queen\s+of\s+soul|girl\s+(?:group|singer))",
]
_MALE_PATTERNS = [
    r"\bmale\s+(?:vocal|voice|lead|singer|tenor|baritone|bass|mc|rapper)",
    r"\bman['']?s\s+voice",
    r"\b(?:his\s+(?:vocal|voice)|he\s+sings)",
]


def _detect_gender(style: str) -> str:
    """Return 'male', 'female', or 'unknown' based on gender hints in style.

    Word-boundary regex: 'male lead' must NOT match inside 'female lead', and
    'tenor' alone does not match 'tenor saxophone'. Hints must explicitly tag a
    vocal context (e.g. 'female lead', 'male tenor', 'her voice')."""
    import re
    s = (style or "").lower()
    fem = any(re.search(p, s) for p in _FEM_PATTERNS)
    male = any(re.search(p, s) for p in _MALE_PATTERNS)
    if fem and not male:
        return "female"
    if male and not fem:
        return "male"
    if fem and male:
        return "duet"
    return "unknown"


def _pick_black_voice_for_style(style: str) -> Optional[Dict[str, str]]:
    """Return {voice_name, voice_path, gender} if the style should auto-swap."""
    if not AUTO_BLACKIFY or not style:
        return None
    import re as _re
    style_l = style.lower()
    # Hip-hop/rap is rhythmic speech, not melodic singing. Swapping a rap
    # vocal onto a melodic gospel/blues reference timbre produces nonsense
    # (smeared formants, lost diction). Skip auto-swap for those genres —
    # ACE-Step's rap output is acceptable on its own with the prompt cue.
    if any(k in style_l for k in (
        "hip-hop", "hip hop", "rap", "trap", "drill", "boom-bap", "boom bap",
    )):
        return None
    # Skip the swap only if the style POSITIVELY claims a non-Black ethnicity
    # for the singer. Negation in front of the phrase ('NOT a white tenor')
    # must NOT trigger the skip — check the preceding 30 chars for negation.
    ethno_re = _re.compile(
        r"\b(white|asian|latin|korean|japanese|chinese|indian|arabic|celtic|european)\s+"
        r"(singer|vocalist|vocal|tenor|baritone|soprano|alto|voice|lead|rapper|mc)\b"
    )
    neg_re = _re.compile(r"\b(?:not|no|never|n't|nor)\b[^.]*$")
    for m in ethno_re.finditer(style_l):
        pre = style_l[max(0, m.start() - 40):m.start()]
        if neg_re.search(pre):
            continue  # negated — keep looking
        return None  # genuine positive ethnicity claim → respect it
    gender = _detect_gender(style_l)
    # Filter registry by gender match (or any if unknown/duet — pick first).
    candidates = []
    for entry in BLACK_VOICE_REGISTRY:
        path: Path = entry["voice_path"]
        if not path.is_file():
            continue
        if not any(k in style_l for k in entry["keywords"]):
            continue
        if gender in ("male", "female") and entry["gender"] != gender:
            continue
        candidates.append(entry)
    if not candidates:
        return None
    chosen = candidates[0]
    return {
        "voice_name": chosen["voice_name"],
        "voice_path": str(chosen["voice_path"]),
        "gender": chosen["gender"],
    }


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
    "rising sun", "open sky", "salt wind", "burning fire",
    "tide turning", "midnight rain", "soft thunder", "fog rolling",
]
LYRIC_VERBS = [
    "rising", "falling", "running", "calling",
    "shining", "burning", "flowing", "turning",
    "waking", "breathing", "drifting", "feeling",
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
    parts = [style, f"{bpm_val} bpm"]
    # Only append "clean mix" if the user wants polished production. When the
    # style mentions raw/lofi/tape/field-recording/cassette/bootleg/demo, the
    # hardcoded "clean mix" was fighting the intended aesthetic — Matt called
    # the output "too produced and white" on 2026-05-13. Skip the polish cue
    # for explicitly raw styles.
    if not any(k in style_l for k in (
        "lo-fi", "lofi", "lo fi", "raw", "tape hiss", "cassette", "field record",
        "field-record", "bootleg", "basement", "demo", "rough cut", "rough-cut",
        "unmixed", "undermastered", "porch recording", "yard recording",
    )):
        parts.append("clean mix")
    if not has_vocal_hint:
        parts.append("expressive male vocal")

    # ACE-Step's vocal default skews toward white pop. For Black-rooted
    # genres, append explicit timbre + lineage cues UNLESS the user already
    # named a non-Black ethnicity/voice family (don't override deliberate
    # choices like "Latin tenor" or "Korean ballad").
    if any(k in style_l for k in BLACK_ROOTED_GENRE_KEYS):
        contradicts = any(k in style_l for k in (
            "white", "asian", "latin", "korean", "japanese", "chinese",
            "indian", "arabic", "celtic", "european",
        ))
        if not contradicts and "black" not in style_l and "african" not in style_l:
            # Hip-hop/rap needs MC framing, not gospel-singer framing — the
            # default melodic reinforcement below makes rap drift sung/white.
            # Reggae/dancehall/dub needs chesty Rasta framing — the gospel
            # reinforcement makes reggae drift toward soul curls + white tenor
            # (Matt called the first 'Mountain in the Mist' too white 2026-05-13).
            if any(k in style_l for k in HIPHOP_GENRE_KEYS):
                parts.append(HIPHOP_VOCAL_REINFORCEMENT)
            elif any(k in style_l for k in REGGAE_GENRE_KEYS):
                parts.append(REGGAE_VOCAL_REINFORCEMENT)
            else:
                parts.append(BLACK_VOCAL_REINFORCEMENT)
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
    "voice_assist",       # {voice_name, voice_path, gender} chosen by registry
    "voice_assist_jid",   # jid of the auto-spawned swap job
    "rating",                # 0–5; mirrored to/from VPS manifest
    "published", "published_at", "published_url",  # tracks VPS state
    "kind", "auto_assist", "src_jid", "voice_name", "voice_path",  # swap-job
)


def _sidecar_path(jid: str) -> Path:
    return OUT / f"{jid}.json"


# --- VPS rating mirror (Forge → nicedreamzwholesale.com/songs) -------------

VPS_SITE_URL = "https://nicedreamzwholesale.com/songs"
VPS_SSH_HOST = "ineedhemp"
VPS_ADMIN_TOKEN_PATH = "/home/u701983700/domains/nicedreamzwholesale.com/public_html/songs/.admin_token"

_vps_token_cache: dict = {"value": None, "ts": 0.0}


def _vps_admin_token() -> Optional[str]:
    if _vps_token_cache["value"] and (time.time() - _vps_token_cache["ts"]) < 600:
        return _vps_token_cache["value"]
    try:
        out = subprocess.check_output(
            ["ssh", VPS_SSH_HOST, f"cat {VPS_ADMIN_TOKEN_PATH}"],
            text=True, timeout=10,
        ).strip()
        _vps_token_cache["value"] = out
        _vps_token_cache["ts"] = time.time()
        return out
    except Exception as e:
        print(f"[rating] vps token fetch failed: {e}", flush=True)
        return None


def _delete_from_vps(jid: str) -> None:
    """Delete a song from the VPS published library (manifest + mp3 + sync)."""
    import urllib.request as _ur
    token = _vps_admin_token()
    if not token:
        return
    body = json.dumps({"id": jid, "token": token}).encode()
    req = _ur.Request(
        f"{VPS_SITE_URL}/delete.php", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        _ur.urlopen(req, timeout=15).read()
        print(f"[delete] vps purged {jid[:8]}", flush=True)
    except Exception as e:
        print(f"[delete] vps delete failed for {jid[:8]}: {e}", flush=True)


def _mirror_rating_to_vps(jid: str, rating: int) -> None:
    import urllib.request as _ur
    token = _vps_admin_token()
    if not token:
        return
    body = json.dumps({"id": jid, "rating": int(rating), "token": token}).encode()
    req = _ur.Request(
        f"{VPS_SITE_URL}/rate.php", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        _ur.urlopen(req, timeout=10).read()
        print(f"[rating] mirrored {jid[:8]} → {rating} to VPS", flush=True)
    except Exception as e:
        print(f"[rating] vps mirror failed for {jid[:8]}: {e}", flush=True)


def _auto_push_loop() -> None:
    """Background loop: every 60s, push any local 'done' song that hasn't been
    published yet to the VPS via the songs_sync helper. Pairs with
    `_pull_vps_state_loop` so the local Forge and the published URL stay in
    lockstep without Matt running songs_sync manually.

    Sticky-delete safety: this only pushes songs whose sidecar still exists. If
    Matt deletes a song locally, the sidecar is gone, and this loop won't
    resurrect it. If he deletes from the VPS admin, the pull loop hard-deletes
    the local copy; afterward the sidecar is gone and we won't re-push it."""
    import subprocess as _sp
    sync_script = Path.home() / "Scripts" / "songs_sync.py"
    if not sync_script.is_file():
        print(f"[push] songs_sync.py not at {sync_script} — auto-push disabled", flush=True)
        return
    # Wait for the first ACE-Step generations to settle before pushing
    time.sleep(30)
    while True:
        try:
            with JOBS_LOCK:
                unpublished = [
                    jid for jid, j in JOBS.items()
                    if j.get("status") == "done"
                    and not j.get("published")
                    and (OUT / f"{jid}.json").is_file()  # sidecar still on disk
                ]
            if unpublished:
                print(f"[push] auto-publishing {len(unpublished)} song(s) to VPS",
                      flush=True)
                # songs_sync push <id1> <id2> ...   pushes specifically the listed jobs
                r = _sp.run(
                    ["/usr/bin/python3", str(sync_script), "push", *unpublished],
                    capture_output=True, text=True, timeout=600,
                )
                if r.returncode == 0:
                    # songs_sync writes published=true into each sidecar; reload
                    # those into in-memory JOBS so we don't try to re-push.
                    with JOBS_LOCK:
                        for jid in unpublished:
                            sc_path = OUT / f"{jid}.json"
                            try:
                                sc = json.loads(sc_path.read_text())
                                if sc.get("published"):
                                    j = JOBS.get(jid)
                                    if j is not None:
                                        j["published"] = True
                                        j["published_at"] = sc.get("published_at")
                                        j["published_url"] = sc.get("published_url")
                            except Exception:
                                pass
                else:
                    print(f"[push] songs_sync exit {r.returncode}: "
                          f"{(r.stderr or r.stdout)[-300:]}", flush=True)
        except Exception as e:
            print(f"[push] auto-push loop: {e}", flush=True)
        time.sleep(60)


def _pull_vps_state_loop() -> None:
    """Background poll: every 60s, pull VPS manifest and mirror rating + delete
    state into local sidecars. Lets the published page act as another source of
    rating truth — Matt rates a song on his phone, Forge picks it up.

    Deletions require seeing a song missing from VPS for two consecutive polls
    before nuking the local copy — defends against transient manifest states
    (e.g. mid-sync rewrites) that briefly hide entries."""
    import urllib.request as _ur
    miss_count: dict[str, int] = {}
    DELETE_THRESHOLD = 2  # need this many consecutive misses
    while True:
        try:
            data = _ur.urlopen(f"{VPS_SITE_URL}/manifest.json", timeout=10).read()
            m = json.loads(data)
            vps_index = {s["id"]: s for s in m.get("songs", [])}
            with JOBS_LOCK:
                local_ids = list(JOBS.keys())
            # Mirror ratings down + detect VPS-side deletions.
            for jid in local_ids:
                with JOBS_LOCK:
                    job = JOBS.get(jid)
                    if not job: continue
                    is_published = bool(job.get("published"))
                vps_song = vps_index.get(jid)
                if vps_song:
                    miss_count.pop(jid, None)
                    new_rating = int(vps_song.get("rating") or 0)
                    cur_rating = int((job.get("rating") or 0))
                    if new_rating != cur_rating:
                        with JOBS_LOCK:
                            job["rating"] = new_rating
                            _save_sidecar(job)
                        print(f"[sync] pulled rating {jid[:8]} → {new_rating}", flush=True)
                elif is_published:
                    miss_count[jid] = miss_count.get(jid, 0) + 1
                    if miss_count[jid] < DELETE_THRESHOLD:
                        print(f"[sync] {jid[:8]} missing on VPS (strike {miss_count[jid]}/{DELETE_THRESHOLD}) — waiting", flush=True)
                        continue
                    # was published, now gone from VPS for 2+ polls → local hard delete
                    print(f"[sync] {jid[:8]} deleted on VPS — removing local copy", flush=True)
                    with JOBS_LOCK:
                        JOBS.pop(jid, None)
                    # FULL purge — match do_DELETE so a VPS-side delete removes
                    # exports/ mp3, Music.app entry + ACE cache, not just outputs/.
                    sc = {}
                    try:
                        sc = json.loads((OUT / f"{jid}.json").read_text())
                    except Exception:
                        pass
                    title = ((job or {}).get("title") or sc.get("title") or
                             (job or {}).get("idea") or sc.get("idea") or jid).strip()
                    safe_title = _safe_filename(title)
                    paths = [OUT / f"{jid}.wav", OUT / f"{jid}.json",
                             OUT / f"{jid}.sync.json"]
                    for _p in (job or {}).get("ace_cache_files") or []:
                        paths.append(Path(_p))
                    music_auto = (Path.home() / "Music" / "Music" / "Media.localized" /
                                  "Automatically Add to Music.localized")
                    paths.append(music_auto / f"{jid}.mp3")
                    if safe_title:
                        paths.append(EXPORTS / f"{safe_title}.mp3")
                        music_lib = (Path.home() / "Music" / "Music" / "Media.localized" /
                                     "Music" / "Matt Macosko (AI · Song Forge)" /
                                     "Song Forge — first sessions")
                        paths.append(music_lib / f"{safe_title}.mp3")
                    for _f in paths:
                        try:
                            if _f.is_file(): _f.unlink()
                        except Exception:
                            pass
                    if safe_title:
                        _remove_from_music_library(safe_title)
                    miss_count.pop(jid, None)
        except Exception as e:
            print(f"[sync] vps pull loop: {e}", flush=True)
        time.sleep(60)


def _sync_path(jid: str) -> Path:
    return OUT / f"{jid}.sync.json"


def _strip_lyric_lines(lyrics_text: str) -> list:
    out = []
    for line in (lyrics_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            continue
        clean = re.sub(r"\([^)]*\)", "", s).strip()
        clean = re.sub(r"[^\w\s']", " ", clean).strip()
        if clean:
            out.append({"raw": s, "clean": clean})
    return out


def _norm_toks(s: str) -> list:
    s = re.sub(r"[^\w\s']", " ", (s or "").lower())
    return [t.rstrip("'") for t in s.split() if t]


def _fuzzy_score(a: str, b: str) -> float:
    """Recall-weighted token similarity: how many of A's content words appear
    in B (b can be longer than a — Whisper segments often span ~half a line
    of lyrics, so symmetric Jaccard underrates real matches)."""
    STOP = {"the","a","an","and","or","of","in","on","to","i","my","you","with","is","it","be","for","at"}
    A = [t for t in _norm_toks(a) if t not in STOP]
    B = set(_norm_toks(b))
    if not A:
        return 0.0
    hits = sum(1 for t in A if t in B)
    return hits / len(A)


def _align_lyrics(jid: str, audio_path: Path, lyrics_text: str, duration: float) -> Dict[str, Any]:
    """Smart per-line karaoke timing.

    Strategy:
      1. Run Whisper on the audio with word_timestamps just to learn WHEN things
         happen — never use whisper's text for display (it mishears sung lyrics).
      2. The user's written lyrics are the source of truth for SPELLING.
      3. For each user line, fuzzy-match it against whisper segments to find
         when it was sung. Skip user lines whisper never heard (no ghost lines).
      4. Inside each matched line, distribute the user's words across the matched
         time span — paired 1:1 to whisper word timestamps when counts agree,
         otherwise interpolated evenly.
    Cached to outputs/{id}.sync.json."""

    user_lines = _strip_lyric_lines(lyrics_text)
    if not user_lines:
        return {"lines": [], "error": "no lyric lines"}

    whisper_segs: list = []
    try:
        import mlx_whisper  # type: ignore
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo="mlx-community/whisper-medium.en-mlx",
            verbose=False,
            word_timestamps=True,
            condition_on_previous_text=False,
            no_speech_threshold=0.20,
            compression_ratio_threshold=2.4,
        )
        for s in (result.get("segments") or []):
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            content = re.sub(r"[^\w\s]", "", txt).strip()
            if len(content.split()) <= 1 and len(content) <= 5:
                continue
            seg_start = float(s.get("start") or 0.0)
            seg_end   = float(s.get("end")   or seg_start)
            ww = []
            for w in (s.get("words") or []):
                wt = (w.get("word") or "").strip()
                if not wt:
                    continue
                ww.append({
                    "text":  wt,
                    "start": float(w.get("start") or seg_start),
                    "end":   float(w.get("end")   or seg_end),
                })
            whisper_segs.append({
                "start": seg_start, "end": seg_end, "text": txt, "words": ww,
            })
    except Exception as e:
        print(f"[lyrics_sync] mlx_whisper failed: {e}", flush=True)

    out_lines: list = []
    whisper_segs_count = len(whisper_segs)

    if whisper_segs:
        # Walk monotonically through the whisper transcript, fuzzy-matching each
        # user line against a 1–3 segment window. Lines without a strong match
        # are SKIPPED — they were written but not actually sung.
        ws_idx = 0
        FORWARD = 12
        MAX_SPAN = 3
        MATCH_THRESHOLD = 0.30
        for ul in user_lines:
            best = None  # (score, j, k)
            scan_to = min(ws_idx + FORWARD, len(whisper_segs))
            for j in range(ws_idx, scan_to):
                for k in range(1, MAX_SPAN + 1):
                    if j + k > len(whisper_segs):
                        break
                    fused = " ".join(s["text"] for s in whisper_segs[j:j+k])
                    sc = _fuzzy_score(ul["clean"], fused)
                    sc *= (1.0 - 0.03 * (k - 1))
                    if best is None or sc > best[0]:
                        best = (sc, j, k)
            if not best or best[0] < MATCH_THRESHOLD:
                continue  # skip — not actually sung
            _, j, k = best
            line_start = whisper_segs[j]["start"]
            line_end   = whisper_segs[j + k - 1]["end"]
            # Per-word timing — pair user's words to whisper's words for spelling
            # accuracy + real timestamps.
            user_words = ul["raw"].split()
            wsp_words = []
            for s in whisper_segs[j:j+k]:
                wsp_words.extend(s["words"])
            words_out = []
            if user_words and wsp_words and len(user_words) == len(wsp_words):
                for i, uw in enumerate(user_words):
                    words_out.append({
                        "text":  uw,
                        "start": round(wsp_words[i]["start"], 3),
                        "end":   round(wsp_words[i]["end"],   3),
                    })
            elif user_words and wsp_words:
                # Counts differ — use whisper's word START times as anchors and
                # distribute user words across them by relative position.
                anchor_starts = [w["start"] for w in wsp_words]
                anchor_ends   = [w["end"]   for w in wsp_words]
                n_user = len(user_words)
                n_wsp  = len(wsp_words)
                for i, uw in enumerate(user_words):
                    frac = i / max(1, n_user)
                    src_idx = min(n_wsp - 1, int(frac * n_wsp))
                    next_idx = min(n_wsp - 1, int(((i + 1) / max(1, n_user)) * n_wsp))
                    words_out.append({
                        "text":  uw,
                        "start": round(anchor_starts[src_idx], 3),
                        "end":   round(anchor_ends[next_idx], 3),
                    })
            elif user_words:
                # No whisper words — even-spaced inside the segment span.
                step = (line_end - line_start) / max(1, len(user_words))
                for i, uw in enumerate(user_words):
                    t0 = line_start + step * i
                    t1 = line_start + step * (i + 1)
                    words_out.append({
                        "text": uw, "start": round(t0, 3), "end": round(t1, 3),
                    })
            out_lines.append({
                "text":  ul["raw"],
                "start": round(line_start, 3),
                "end":   round(line_end,   3),
                "words": words_out,
            })
            ws_idx = j + k
    else:
        # Whisper unavailable — fall back to even-spaced through total duration.
        word_counts = [max(1, len(l["clean"].split())) for l in user_lines]
        total_w = sum(word_counts) or 1
        cursor = 0.0
        for ul, wc in zip(user_lines, word_counts):
            line_dur = duration * (wc / total_w)
            out_lines.append({
                "text":  ul["raw"],
                "start": round(cursor, 3),
                "end":   round(cursor + line_dur, 3),
            })
            cursor += line_dur

    payload = {
        "id": jid,
        "lines": out_lines,
        "duration": duration,
        "whisper_segments": whisper_segs_count,
        "generated_at": time.time(),
    }
    try:
        _sync_path(jid).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[lyrics_sync] write failed: {e}", flush=True)
    return payload


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


def _remove_from_music_library(safe_title: str) -> None:
    """Yank the Music.app database entry by title, scoped to genre
    'AI Song Forge' so a same-named non-Forge track can't be hit by
    accident. Without this, deleting a song's file from disk leaves a
    broken-pointer row in Music.app (and iCloud won't propagate the
    delete to iPhone). Best-effort; silent no-op if Music isn't running."""
    if not safe_title:
        return
    try:
        script = (
            'on run argv\n'
            '  tell application "Music"\n'
            '    set victims to (every track of library playlist 1 whose name is (item 1 of argv) and genre is "AI Song Forge")\n'
            '    repeat with t in victims\n'
            '      delete t\n'
            '    end repeat\n'
            '  end tell\n'
            'end run'
        )
        subprocess.run(
            ["osascript", "-e", script, "--", safe_title],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        print(f"[music delete] {e}", flush=True)


def _drop_into_music_auto_add(job: Dict[str, Any]) -> None:
    """Copy this job's tagged 320k mp3 from exports/ into Music.app's
    'Automatically Add' folder so it gets ingested into the library on the
    next sweep. Idempotent — skips if a same-name drop is already pending."""
    try:
        title = _safe_filename(job.get("title") or job.get("idea") or job.get("id") or "untitled")
        src = EXPORTS / f"{title}.mp3"
        if not src.is_file():
            return
        auto = (Path.home() / "Music" / "Music" / "Media.localized" /
                "Automatically Add to Music.localized")
        if not auto.is_dir():
            return
        dst = auto / f"{title}.mp3"
        if dst.exists():
            return
        shutil.copy2(src, dst)
    except Exception as e:
        print(f"[music auto-add] {e}", flush=True)


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
            [FFMPEG, "-y", "-i", str(wav),
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
        # Skip non-job sidecars (e.g. <jid>.sync.json holds lyric alignment).
        if p.name.endswith(".sync.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            jid = data.get("id")
            if not jid or "status" not in data:
                continue  # malformed or non-job sidecar
            wav = OUT / f"{jid}.wav"
            if not wav.is_file():
                continue  # sidecar without audio = stale; skip
            JOBS[jid] = data
            n += 1
        except Exception as e:
            print(f"[hydrate] {p.name}: {e}", flush=True)
    return n


# ----- voice-swap pipeline (demucs → seed-vc → ffmpeg) -----------------------
SWAP_LOCK = threading.Lock()


def _spawn_auto_voice_assist(src_job: Dict[str, Any], voice: Dict[str, str]) -> str:
    """Queue a voice-swap for a just-finished song using the auto-Blackify
    registry. Returns the new swap jid. Does not block; mutates src_job in
    place to record the link."""
    src_jid = src_job["id"]
    src_wav = OUT / f"{src_jid}.wav"
    if not src_wav.is_file():
        return ""
    swap_jid = uuid.uuid4().hex
    base_title = src_job.get("title") or src_job.get("idea") or "song"
    new_title = f"{base_title} (auto: {voice['voice_name']})"
    JOBS[swap_jid] = {
        "id": swap_jid,
        "status": "queued",
        "kind": "swap",
        "auto_assist": True,
        "title": new_title,
        "idea": src_job.get("idea", ""),
        "style": f"auto Black-voice swap → {voice['voice_name']}",
        "lyrics": src_job.get("lyrics", ""),
        "duration": src_job.get("duration"),
        "src_wav": str(src_wav),
        "src_jid": src_jid,
        "voice_name": voice["voice_name"],
        "voice_path": voice["voice_path"],
        "group_effect": False,
        "created_at": time.time(),
        "progress": 0.0,
        "stage": "queued (auto Black-voice assist)",
    }
    src_job["voice_assist_jid"] = swap_jid
    _save_sidecar(src_job)
    threading.Thread(target=_run_swap, args=(swap_jid,), daemon=True).start()
    return swap_jid


def _run_swap(jid: str) -> None:
    """Background pipeline: demucs split → seed-vc convert → ffmpeg mix.
    Serialized via SWAP_LOCK because seed-vc model-loading on MPS can't
    handle two parallel processes — they hang on memory contention."""
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if not job:
        return
    # Wait our turn — only one swap runs at a time.
    if not SWAP_LOCK.acquire(blocking=False):
        with JOBS_LOCK:
            job["stage"] = "queued behind another voice swap…"
        SWAP_LOCK.acquire()
    try:
        _run_swap_impl(jid, job)
    finally:
        SWAP_LOCK.release()


def _run_swap_impl(jid: str, job: Dict[str, Any]) -> None:
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
            [str(DEMUCS_BIN), "--two-stems", "vocals", "-d", "mps", "-o", str(work), str(src_wav)],
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

        # Step 2 — seed-vc voice conversion.
        # Streamed so the UI can move between 0.50 and 0.85 while seed-vc
        # crunches each chunk. inference.py prints `[forge_progress] N/T`
        # at the top of every chunk loop iteration.
        _set(f"loading seed-vc model for {job['voice_name']}…", 0.50)
        proc = subprocess.Popen(
            [str(SVC_PY), "-u", "inference.py",
             "--source", str(vocals),
             "--target", str(voice_path),
             "--output", str(work),
             "--f0-condition", "true",
             "--auto-f0-adjust", "true",
             "--diffusion-steps", "25"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(SVC_DIR),
        )
        _svc_tail: list = []
        _svc_started = time.time()
        _model_loaded = False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                _svc_tail.append(line)
                if len(_svc_tail) > 200:
                    _svc_tail = _svc_tail[-200:]
                # The bar used to freeze at 50% for the full 30–60s of model
                # loading because nothing matched [forge_progress] yet. Show a
                # heartbeat instead — any seed-vc output line means it's alive,
                # not hung. Once chunk progress kicks in, switch to %.
                m = re.match(r"\[forge_progress\]\s+(\d+)/(\d+)", line)
                if m:
                    done_f = int(m.group(1))
                    total_f = max(int(m.group(2)), 1)
                    frac = min(done_f / total_f, 1.0)
                    _set(
                        f"converting vocals to {job['voice_name']} (seed-vc {int(frac*100)}%)…",
                        0.50 + 0.35 * frac,
                    )
                    _model_loaded = True
                elif not _model_loaded:
                    elapsed = int(time.time() - _svc_started)
                    _set(
                        f"loading seed-vc model for {job['voice_name']} ({elapsed}s)…",
                        0.50,
                    )
                if time.time() - _svc_started > 600:
                    proc.kill()
                    raise RuntimeError("seed-vc timeout (>600s)")
        finally:
            proc.wait()
        if proc.returncode != 0:
            tail = "".join(_svc_tail)[-500:]
            raise RuntimeError(f"seed-vc failed: {tail}")

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
            [FFMPEG, "-y", "-i", str(no_vocals), "-i", str(converted_vox),
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

        # Tag and export the final swapped wav as a 320k mp3, then drop a copy
        # into Music.app's auto-add folder so it appears in the library
        # immediately. Without this, swap jobs never land in Music.app and the
        # user has to track them down in exports/ manually.
        try:
            _export_tagged_mp3(job)
            _drop_into_music_auto_add(job)
        except Exception as e:
            print(f"[swap] export/auto-add failed for {jid[:8]}: {e}", flush=True)

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
                # .get() — never let a malformed/legacy entry kill the loop.
                pending = [j for j in JOBS.values()
                           if j.get("status") in ("queued", "running")
                           and j.get("ace_task_id")]
            for job in pending:
                # If the job was DELETEd mid-render, ACE-Step keeps churning and
                # eventually returns a result. Without this guard we'd happily
                # copy the wav back, write the sidecar, and the next forge
                # restart would resurrect the song from disk. Skip ghosts.
                with JOBS_LOCK:
                    if job["id"] not in JOBS:
                        continue
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
                            _drop_into_music_auto_add(job)
                            # Auto voice-swap to a Black vocalist if the style
                            # matched the registry. Fires once per source job.
                            va = job.get("voice_assist")
                            if va and not job.get("voice_assist_jid"):
                                _spawn_auto_voice_assist(job, va)
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

    def _file_ranged(self, path: Path, ctype: str):
        """Stream `path` honoring HTTP Range requests so browsers can seek
        in <audio>/<video> tags. Without this, seeking restarts the file."""
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self.send_error(404); return

        rng = self.headers.get("Range") or self.headers.get("range")
        start, end = 0, size - 1
        partial = False

        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",", 1)[0].strip()
            if "-" in spec:
                lo, hi = spec.split("-", 1)
                try:
                    if lo == "" and hi:
                        # suffix: last N bytes
                        n = int(hi)
                        start = max(0, size - n)
                        end   = size - 1
                    else:
                        start = int(lo)
                        end   = int(hi) if hi else size - 1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.end_headers()
                        return
                    partial = True
                except ValueError:
                    partial = False
                    start, end = 0, size - 1

        length = end - start + 1
        if partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # Stream in chunks so large files don't blow memory.
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError):
                    return
                remaining -= len(chunk)

    def do_HEAD(self):
        # Browsers probe with HEAD before seeking. Just answer with sizes.
        u = urlparse(self.path)
        if u.path.startswith("/audio/"):
            name = u.path.split("/", 2)[-1]
            p = OUT / name
            if not p.is_file():
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            return
        # default: 200 empty for known routes, 404 otherwise
        self.send_error(404)

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
                "jobs_running": sum(1 for j in latest if j.get("status") in ("queued","running")),
                "last": latest[0] if latest else None,
                "download": _ace_download_status(),
            })
        if u.path == "/api/songs":
            with JOBS_LOCK:
                rows = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            return self._json({"songs": rows})
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
        m = re.match(r"^/api/lyrics_sync/([\w\-]{1,80})$", u.path)
        if m:
            jid = m.group(1)
            p = _sync_path(jid)
            if p.is_file():
                try:
                    return self._json(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    pass
            return self._json({"error": "not generated", "id": jid}, 404)
        if u.path.startswith("/audio/"):
            name = u.path.split("/", 2)[-1]
            return self._file_ranged(OUT / name, "audio/wav")
        self.send_error(404)

    def do_DELETE(self):
        u = urlparse(self.path)

        def _remove_job(jid: str, job: Optional[Dict[str, Any]], _seen=None):
            """Hard-delete every trace of a job everywhere — Song Forge is the
            master, so when a job leaves it, every downstream copy goes too:
            local outputs, sync sidecar, ACE-Step cache, exports/ tagged MP3,
            voice_swap_work/ scratch dir, Music.app auto-add drop, Music.app
            *imported* library file, the VPS published copy if pushed there,
            AND the linked voice-swap (or source) job for cascade delete."""
            _seen = _seen or set()
            if jid in _seen:
                return
            _seen.add(jid)
            # Re-read sidecar — the in-memory job is stale for `published` (set
            # out-of-process by songs_sync push) and may also be missing
            # `title`, `voice_assist_jid`, or `src_jid` for old entries.
            sc = {}
            try:
                sc = json.loads((OUT / f"{jid}.json").read_text())
            except Exception:
                pass
            sc_published = bool((job or {}).get("published") or sc.get("published"))
            # Fallback chain must match _export_tagged_mp3 — when both title
            # and idea are empty the export saved as `{jid}.mp3`, so the
            # delete path has to resolve to the same name or the file lingers.
            title = ((job or {}).get("title") or sc.get("title") or
                     (job or {}).get("idea") or sc.get("idea") or jid).strip()
            safe_title = _safe_filename(title)
            voice_swap_jid = (job or {}).get("voice_assist_jid") or sc.get("voice_assist_jid")
            src_jid = (job or {}).get("src_jid") or sc.get("src_jid")

            paths = [
                OUT / f"{jid}.wav",
                OUT / f"{jid}.json",
                OUT / f"{jid}.sync.json",
            ]
            for p in (job or {}).get("ace_cache_files") or []:
                paths.append(Path(p))
            # Music.app auto-add drop (drop file pre-import, keyed by jid)
            music_auto = Path.home() / "Music" / "Music" / "Media.localized" / "Automatically Add to Music.localized"
            paths.append(music_auto / f"{jid}.mp3")
            # Tagged MP3 in exports/, keyed by title
            if safe_title:
                paths.append(EXPORTS / f"{safe_title}.mp3")
                # Music.app *imported* library file (post auto-add ingest),
                # keyed by title under the fixed Matt Macosko AI Song Forge folder.
                music_lib = (Path.home() / "Music" / "Music" / "Media.localized" /
                             "Music" / "Matt Macosko (AI · Song Forge)" /
                             "Song Forge — first sessions")
                paths.append(music_lib / f"{safe_title}.mp3")
            for f in paths:
                try:
                    if f.is_file(): f.unlink()
                except Exception:
                    pass
            # Also yank the Music.app library entry so it doesn't linger
            # as a missing-file ghost; this is what lets iCloud propagate
            # the delete to the iPhone.
            if safe_title:
                _remove_from_music_library(safe_title)
            # voice_swap_work/{jid}/ — intermediate demucs / seed-vc files
            try:
                swap_dir = SWAP_WORK / jid
                if swap_dir.is_dir():
                    shutil.rmtree(swap_dir)
            except Exception:
                pass
            # VPS — if this song was ever published, remove from manifest + audio + sync.
            if sc_published:
                try:
                    threading.Thread(
                        target=_delete_from_vps, args=(jid,), daemon=True,
                    ).start()
                except Exception as e:
                    print(f"[delete] vps thread spawn failed: {e}", flush=True)
            # Cascade — voice swap pairs (source ↔ swap) are two JOBS for one
            # song; deleting either should take both with it. The _seen guard
            # prevents the recursion from looping back through the same edge.
            for partner in (voice_swap_jid, src_jid):
                if partner and partner not in _seen:
                    with JOBS_LOCK:
                        partner_job = JOBS.pop(partner, None)
                    try:
                        _remove_job(partner, partner_job, _seen)
                    except Exception as e:
                        print(f"[delete] cascade {partner[:8]} failed: {e}", flush=True)

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
        """Rename / retitle / re-rate a song. Body may include {title?, rating?}.

        Setting rating mirrors the change to the VPS manifest so the published
        page sees the same star count without a manual sync."""
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
        out: dict = {"ok": True}
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job: return self._json({"error": "not found"}, 404)
            if "title" in body:
                job["title"] = (body.get("title") or "").strip()[:200]
                out["title"] = job["title"]
            if "rating" in body:
                try:
                    r = int(body.get("rating") or 0)
                except Exception:
                    r = 0
                r = max(0, min(5, r))
                job["rating"] = r
                out["rating"] = r
            _save_sidecar(job)
            mirror_rating = "rating" in body
            mirror_r = job.get("rating", 0)
            published = bool(job.get("published"))
        # Mirror rating to VPS (best-effort, doesn't block the local response).
        if mirror_rating and published:
            try:
                threading.Thread(
                    target=_mirror_rating_to_vps, args=(jid, mirror_r), daemon=True,
                ).start()
            except Exception as e:
                print(f"[rating] mirror thread spawn failed: {e}", flush=True)
        return self._json(out)

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
                # Default guidance_scale lowered from 15.0 → 10.0 on 2026-05-13
                # because Matt felt outputs were "too produced and white" —
                # high guidance over-forces ACE-Step's polished modern-pop
                # prior. Lower guidance = looser interpretation = more
                # organic/varied vocal character. Per-request override via
                # body["guidance_scale"] still respected.
                try:
                    gscale = float(body.get("guidance_scale") or 10.0)
                except Exception:
                    gscale = 10.0
                gscale = max(3.0, min(gscale, 20.0))
                try:
                    steps = int(body.get("inference_steps") or 27)
                except Exception:
                    steps = 27
                steps = max(10, min(steps, 60))
                resp = _ace_post(
                    "/release_task",
                    {
                        "prompt": prompt,
                        "lyrics": lyrics,
                        "vocal_language": language,
                        "task_type": "text2music",
                        "inference_steps": steps,
                        "guidance_scale": gscale,
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
            # Auto-Blackify: if the style hits a Black-rooted genre, queue a
            # voice swap to a real Black vocalist after ACE-Step finishes.
            # User can opt out per-request with auto_voice_assist:false, or
            # force a specific voice by passing voice_path (+optional voice_name).
            opt_in = body.get("auto_voice_assist")
            forced_path = (body.get("voice_path") or "").strip()
            if opt_in is False:
                voice_assist = None
            elif forced_path and Path(forced_path).is_file():
                voice_assist = {
                    "voice_name": (body.get("voice_name") or
                                   Path(forced_path).stem.replace("_", " ").title()),
                    "voice_path": forced_path,
                    "gender": (body.get("voice_gender") or "unknown"),
                }
            else:
                voice_assist = _pick_black_voice_for_style(style)
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
                    "voice_assist": voice_assist,
                }
            return self._json({"id": jid, "voice_assist": voice_assist})

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

        # Karaoke-style line timing. Runs Whisper once per song and caches the
        # result; subsequent requests just GET the sidecar.
        m = re.match(r"^/api/lyrics_sync/([\w\-]{1,80})$", u.path)
        if m:
            jid = m.group(1)
            with JOBS_LOCK:
                j = dict(JOBS.get(jid) or {})
            if not j:
                return self._json({"error": "not found"}, 404)
            audio_path = OUT / f"{jid}.wav"
            if not audio_path.is_file():
                return self._json({"error": "audio not found"}, 404)
            try:
                duration = float(j.get("duration") or 120.0)
            except Exception:
                duration = 120.0
            lyrics = j.get("lyrics") or ""
            try:
                payload = _align_lyrics(jid, audio_path, lyrics, duration)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            return self._json(payload)

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
    threading.Thread(target=_pull_vps_state_loop, daemon=True).start()
    threading.Thread(target=_auto_push_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[forge] http://127.0.0.1:{PORT}/   (ACE-Step at {ACE})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
