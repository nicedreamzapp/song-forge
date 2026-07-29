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

# This server is launched by nohup with a bare PATH (/usr/bin:/bin:/usr/sbin:
# /sbin) — no /opt/homebrew/bin. Any subprocess that calls a bare "ffmpeg"
# (songs_sync auto-push, mlx_whisper lyric-sync, demucs) then dies with
# FileNotFoundError, silently. Prepend the Homebrew/MacPorts bins so every
# child process we spawn can find ffmpeg & friends regardless of PATH.
for _bin in ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"):
    if os.path.isdir(_bin) and _bin not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _bin + ":" + os.environ.get("PATH", "")

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
    "tenor, NOT country, NOT country-gospel, NOT Nashville twang, NOT CCM, "
    "soulful church-trained delivery"
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

# Female variants (2026-07-09): the male-only reinforcements were overriding
# explicit female requests ("a lady singing" hip-hop came back as a man).
# Selected by _detect_gender in _seed_prompt.
HIPHOP_VOCAL_REINFORCEMENT_F = (
    "Black female MC, African American woman emcee, commanding Black female rap voice, "
    "PURE hip-hop in the Lauryn Hill / Queen Latifah / MC Lyte / Rapsody / "
    "Missy Elliott / Bahamadia / Little Simz lineage, confident gritty Black female "
    "rap delivery, scratched chorus samples, vinyl crackle warmth, "
    "NO electric guitar, NO rock drums, NO nu-metal, NO rap-rock fusion, NO punk, "
    "NOT white, NOT pop, NOT indie, NOT alt-rock, NOT rap-metal, NOT a male voice, "
    "raw Black conscious hip-hop authenticity"
)
REGGAE_VOCAL_REINFORCEMENT_F = (
    "Black Jamaican female lead vocalist with warm resonant chant-toned delivery "
    "in the Marcia Griffiths / Judy Mowatt / Rita Marley / Sister Nancy lineage, "
    "deep roots-reggae timbre, patois inflection, earthy organic Nyabinghi-rooted "
    "voice, NOT gospel melisma, NOT soul curls, NOT R&B runs, "
    "NOT a thin white pop voice, NOT pop-reggae, NOT a male voice — "
    "heavy roots authenticity"
)
BLACK_VOCAL_REINFORCEMENT_F = (
    "Black African American female lead vocalist with rich gospel-rooted timbre, "
    "natural blues-tinged melisma and grain (Mahalia Jackson, Aretha Franklin, "
    "Etta James voice type), NOT a thin white pop voice, NOT a male voice, "
    "NOT country, NOT country-gospel, NOT Nashville twang, NOT CCM, "
    "soulful church-trained delivery"
)

# Auto voice-swap registry. When a song's style matches one of these keyword
# clusters, the worker auto-queues a seed-vc voice-swap to retimbre the lead
# vocal onto a real Black artist's voice — because ACE-Step's default vocal
# distribution drifts white and prompt cues alone don't reliably override it.
# First matching entry wins. Drop new reference wavs in voice_refs/ and add
# rows here.
BLACK_VOICE_REGISTRY = [
    # Hip-hop / rap / drill — MALE. Added 2026-07-29 after Matt listened to a
    # drill render and said it "sounds like a white guy singing a rap song".
    # Cause: the registry had reggae/blues/soul/gospel entries and a female
    # rap fallback (lady_flow, gender-gate only) but NOTHING for male rap, so
    # hip-hop got prompt reinforcement and no timbre swap — and prompt words
    # alone do not move ACE-Step's timbre. black_thought.wav was already on
    # both nodes, unreferenced. Listed FIRST so rap never falls through to the
    # Sam Cooke gospel entry (its keyword list includes "soul"/"blues").
    {
        "gender": "male",
        "keywords": (
            "hip hop", "hip-hop", "hiphop", "rap", "rapper", "rapped",
            "drill", "uk drill", "trap", "boom bap", "boom-bap",
            "gangsta rap", "conscious rap", "mc ", "emcee",
            "kendrick", "nas", "jay-z", "biggie", "tupac", "2pac",
            "black thought", "the roots", "mos def", "talib kweli",
        ),
        "voice_name": "Black Thought",
        "voice_path": ROOT / "voice_refs" / "black_thought.wav",
    },
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
        # 2026-07-25: Matt picked Vaughn Benjamin (Midnite) as THE reggae voice
        # after rejecting Spear/Hill/Toots — pitch center 245Hz, narrow chant
        # band, dark. Ref cut from "Livity" isolated vocals. Spear stays on disk.
        "voice_name": "Vaughn Benjamin",
        "voice_path": ROOT / "voice_refs" / "vaughn_benjamin.wav",
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
    # Female soul/R&B lineage — Iman Europe (Matt 2026-07-25: "use more voices
    # like this style"). Warm 215Hz center, wide range; ref from "Kryptonite".
    # Listed BEFORE Mahalia so modern soul/R&B prompts land on her.
    {
        "gender": "female",
        "keywords": (
            "soul", "deep soul", "neo soul", "neo-soul", "r&b", "rnb",
            "rhythm and blues", "motown", "funk", "p-funk", "p funk",
        ),
        "voice_name": "Iman Europe",
        "voice_path": ROOT / "voice_refs" / "iman_europe.wav",
    },
    # Female gospel/blues lineage — Mahalia Jackson keeps the church and the
    # old blues; everything modern moved to Iman above.
    {
        "gender": "female",
        "keywords": (
            "gospel", "doo-wop", "doo wop", "doowop",
            "blues", "delta blues", "chicago blues", "jump blues",
        ),
        "voice_name": "Mahalia Jackson",
        "voice_path": ROOT / "voice_refs" / "mahalia_jackson.wav",
    },
]
# Genre gate map (2026-07-09): requested-genre keywords -> (CLAP label,
# drift enemies worth re-rendering over). Checked in order; first hit wins.
GENRE_GATE = [
    (("hip-hop", "hip hop", "rap", "boom-bap", "boom bap", "trap", "drill", "emcee", "rapper"),
     "hip hop music", ["rock music", "country music"]),
    (("gospel",), "gospel music", ["country music"]),
    (("reggaeton",), "latin music", ["rock music", "country music"]),
    (("reggae", "dancehall", "rocksteady", "ska", "dub reggae", "nyabinghi", "rasta"),
     "reggae music", ["rock music", "country music"]),
    (("blues", "delta blues", "chicago blues"), "blues music", ["country music"]),
    (("bluegrass",), "bluegrass music", ["rock music", "electronic dance music"]),
    (("soul", "r&b", "rnb", "motown", "funk", "disco", "doo-wop", "doo wop"),
     "soul music", ["country music", "rock music"]),
    (("gypsy jazz", "bebop", "swing jazz", "jazz"), "jazz music", ["rock music", "country music"]),
    (("house", "deep house", "tech house"), "house music", ["rock music", "country music", "folk music"]),
    (("techno", "trance", "dubstep", "drum and bass", "drum'n'bass", "dnb",
      "edm", "electro ", "electronic", "synthwave", "idm", "breakbeat"),
     "electronic dance music", ["rock music", "country music", "folk music"]),
    (("afrobeat", "afrobeats", "highlife", "amapiano"),
     "afrobeats music", ["rock music", "country music"]),
    (("latin", "salsa", "cumbia", "bachata", "mariachi", "bossa nova", "samba"),
     "latin music", ["rock music", "country music"]),
    (("folk", "americana", "singer-songwriter", "singer songwriter", "acoustic ballad"),
     "folk music", ["electronic dance music", "hip hop music", "rock music"]),
    (("country", "honky tonk", "honky-tonk", "outlaw country"),
     "country music", ["rock music", "electronic dance music"]),
    (("metal", "punk", "grunge", "hard rock", "rock"),
     "rock music", ["country music", "electronic dance music"]),
    (("classical", "orchestral", "symphony", "string quartet", "piano concerto"),
     "classical orchestral music", ["rock music", "electronic dance music", "pop music"]),
    (("ambient", "downtempo", "chillout", "chill-out", "lofi", "lo-fi beats"),
     "ambient music", ["rock music", "country music"]),
]
CLAP_PY = ROOT / "engines" / "ACE-Step-1.5" / ".venv" / "bin" / "python"
CLAP_SCRIPT = ROOT / "clap_genre_check.py"


def _requested_genre(style_l: str):
    for keys, label, enemies in GENRE_GATE:
        if any(k in style_l for k in keys):
            return label, enemies
    return None, None


AUTO_BLACKIFY = True  # default ON — songs auto-swap to a Black reference voice. Override with explicit voice_path or auto_voice_assist:false per-request.

# General-purpose fallback voices, used when a style matches no genre keyword
# (pop, rock, folk, EDM…). Matt 2026-07-29: "most if not all songs should be
# black male / and black female." Genre-specific registry entries still win —
# these are only the catch-all. Sam Cooke's soul timbre and Iman Europe's
# contemporary tone carry the widest range of material.
DEFAULT_BLACK_VOICES = ("Sam Cooke (Soul Stirrers)", "Iman Europe")


_FEM_PATTERNS = [
    r"\bfemale\s+(?:vocal|voice|lead|singer|tenor|alto|soprano|contralto|mc|rapper)",
    # 2026-07-09: "a lady singing" fell through and Matt's female hip-hop
    # request came back male — catch natural phrasings, not just tag pairs.
    r"\b(?:lady|ladies|woman|women|girl|female|she)\b[^,.]{0,20}\b(?:sing|rapp|rap\b|vocal|voice|mc|emcee)",
    r"\blad(?:y|ies)\S{0,2}s?\s+voice",
    r"\bfemale\b",
    r"\b(?:femcee|songstress|chanteuse)\b",
    r"\bwoman\s+(?:vocal|voice|singer|lead)",
    r"\b(?:her\s+(?:vocal|voice)|she\s+sings|diva|queen\s+of\s+soul|girl\s+(?:group|singer))",
]
_MALE_PATTERNS = [
    r"\bmale\s+(?:vocal|voice|lead|singer|tenor|baritone|bass|mc|rapper)",
    r"\b(?:man|guy|dude|male|he)\b[^,.]{0,20}\b(?:sing|rapp|rap\b|vocal|voice|mc|emcee)",
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
    # No vocal track, nothing to re-timbre. Matters since 2026-07-29, when the
    # catch-all fallback below started matching styles with no genre keyword —
    # without this an instrumental would queue a pointless voice swap.
    if "instrumental" in style_l or "no vocal" in style_l:
        return None
    # Hip-hop/rap USED to bail out here. The original reason was sound: swapping
    # a rap vocal onto a MELODIC gospel/blues reference smears formants and eats
    # diction. But that assumed the only references on disk were singers. As of
    # 2026-07-29 the registry has a rap reference (Black Thought), so rap swaps
    # onto a rap voice and the objection no longer applies. Matt listened to a
    # drill render that day and called it "a white guy singing a rap song" —
    # this early return was why no swap ever fired on hip-hop.
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
        # Matt 2026-07-29: "most if not all songs should be black male" (and
        # black female). Genre keywords only covered reggae/blues/soul/gospel/
        # hip-hop, so anything else — pop, rock, folk, country, EDM — fell
        # through to ACE-Step's default, which reads white. Fall back to a
        # general-purpose reference of the requested gender. The positive
        # ethnicity guard above still wins, so an explicit "Latin tenor" or
        # "Korean ballad" is still respected and skips the swap entirely.
        want = "female" if gender == "female" else "male"   # unknown/duet → male
        fallback = next(
            (e for e in BLACK_VOICE_REGISTRY
             if e["gender"] == want
             and e["voice_name"] in DEFAULT_BLACK_VOICES
             and e["voice_path"].is_file()),
            None,
        )
        if not fallback:
            return None
        candidates = [fallback]
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


LM_URL    = "http://127.0.0.1:9420/v1/chat/completions"
LM_MODEL  = "divinetribe/gemma-4-31b-it-abliterated-4bit-mlx"  # M5-local mlx_lm.server (~29s/song incl. reasoning)
LM_MODELS_URL = "http://127.0.0.1:9420/v1/models"


def _lm_first_available() -> Optional[str]:
    """First non-embedding model id LM Studio reports — lets lyrics survive
    model swaps without editing LM_MODEL by hand."""
    try:
        with urlopen(LM_MODELS_URL, timeout=5) as r:
            data = json.loads(r.read().decode())
        for m in data.get("data", []):
            if "embed" not in m.get("id", ""):
                return m["id"]
    except Exception:
        pass
    return None
LM_TIMEOUT = 600  # mini Gemma thinks 1-5 min; abandoning early creates ghost-queue pileup in mlx_lm.server  # Gemma 4 31B ~5–20s for ~400 tokens on M5 Max


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


# ----- A&R brief stage (Matt 2026-07-24) -------------------------------------
# A sound description is not a subject. Customers almost always describe the
# SOUND they want ("reggae, old style voice, jungle sounding") and the lyric
# model, given that as a theme, paints genre postcards — the cheese Matt heard.
# This stage invents the actual SONG first: a person, a want, an obstacle, one
# hook line. The lyric stage then writes THAT story. On any failure we return
# None and the caller proceeds exactly as before (single-stage).
BRIEF_SYSTEM = (
    "You invent the SONG behind a customer's sound request — because a sound is not a "
    "subject. If they named a subject, keep it and sharpen it; if not, invent one "
    "specific person in a specific jam, true to the genre's real tradition — reggae: "
    "rent, faith, police, small joys, wry defiance; rock and roll: cars, jobs, desire, "
    "Saturday night, getting out of town; country: work, family, loss, pride. Small "
    "and concrete beats big and abstract. Output exactly this, nothing else:\n"
    "WHO IS SINGING: <one line — a specific person with a situation>\n"
    "TALKING TO: <one line>\n"
    "WHAT HAPPENED: <2-3 sentences, concrete, small, true to the genre>\n"
    "THE ONE LINE: <the plain-spoken hook they would sing in the shower — words a "
    "person actually says, given a twist>"
)


def _songwriting_brief(customer_ask: str) -> Optional[str]:
    """Stage 1 of the lyric write. Returns the brief text or None (= skip)."""
    try:
        payload = {
            "model": LM_MODEL,
            "messages": [
                {"role": "system", "content": BRIEF_SYSTEM},
                {"role": "user", "content": f"Customer asked for: {customer_ask}"},
            ],
            "temperature": 1.0, "top_p": 0.92,
            "frequency_penalty": 0.4, "presence_penalty": 0.4,
            "max_tokens": 400,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = UrlRequest(LM_URL, data=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if "WHAT HAPPENED:" in msg and "THE ONE LINE:" in msg:
            return msg
    except Exception as e:
        print(f"[brief] {e}", flush=True)
    return None


SYSTEM_PROMPT = (
    "You write song lyrics the way great songwriters actually write: like a person "
    "TALKING, set to a beat. Marley, Chuck Berry, John Prine — plain speech that lands.\n"
    "HARD RULES:\n"
    "1. Output ONLY lyrics with [verse] and [chorus] tags. No commentary, no preamble.\n"
    "2. Every line is a real sentence or phrase a person would SAY out loud — subject, "
    "verb, attitude. FORBIDDEN: prop-stack lines (nouns piled up with no verb, like "
    "'cold coffee, shaking hand' or 'copper sun, emerald blade'). If a line has no "
    "verb, cut it.\n"
    "3. Plain everyday words. The power is in WHAT is said, not decoration. 'Them belly "
    "full, but we hungry' beats any pile of imagery.\n"
    "4. The singer is talking TO someone — a lover, a boss, the police, God, a friend. "
    "We should know who by verse one.\n"
    "5. Something HAPPENS. Verse two is later in the story than verse one.\n"
    "6. The chorus is one plain line that means more each time it comes back, plus one "
    "or two answering lines. Repetition is a feature. Keep chorus lines short and singable.\n"
    "7. Humor, anger, flirting, exhaustion — real registers. No noble-suffering poetry.\n"
    "8. Genre cadence matters: reggae talks in short calls with room to breathe and "
    "answers itself; rock and roll swaggers and brags in full sentences; country tells "
    "it straight with a twist in the last line.\n"
    "9. FORBIDDEN WORDS unless quoting someone: soul, spirit, freedom, rise up, neon, "
    "shadows, echoes, whispers, fire as a metaphor, 'feel the rhythm/music/beat'.\n"
    "10. No line that could sit in a greeting card or a tourism ad."
)


# Language request detection (2026-07-10). Phrase-anchored so instrument
# idioms never trip it: "spanish guitar" / "french horn" stay English.
_LANG_DETECT = [
    ("es", "Spanish", r"\b(?:in|en)\s+spanish\b|spanish\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+spanish|\bespañol\b|\bespanol\b"),
    ("fr", "French", r"\bin\s+french\b|french\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+french|\bfrançais\b|\bfrancais\b"),
    ("de", "German", r"\bin\s+german\b|german\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+german|\bauf\s+deutsch\b"),
    ("it", "Italian", r"\bin\s+italian\b|italian\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+italian"),
    ("pt", "Portuguese", r"\bin\s+portuguese\b|portuguese\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+portuguese|\bportuguês\b"),
    ("ja", "Japanese", r"\bin\s+japanese\b|japanese\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+japanese"),
    ("ko", "Korean", r"\bin\s+korean\b|korean\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+korean"),
    ("zh", "Chinese", r"\bin\s+(?:chinese|mandarin)\b|(?:chinese|mandarin)\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+(?:chinese|mandarin)"),
    ("ru", "Russian", r"\bin\s+russian\b|russian\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+russian"),
    ("ar", "Arabic", r"\bin\s+arabic\b|arabic\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+arabic|\bبالعربية\b|\bالعربية\b"),
    ("hi", "Hindi", r"\bin\s+hindi\b|hindi\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+hindi|\bbollywood\s+(?:lyrics|song|vocals?)\b"),
    ("bn", "Bengali", r"\bin\s+bengali\b|bengali\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+bengali"),
    ("th", "Thai", r"\bin\s+thai\b|thai\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+thai"),
    ("vi", "Vietnamese", r"\bin\s+vietnamese\b|vietnamese\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+vietnamese"),
    ("id", "Indonesian", r"\bin\s+indonesian\b|indonesian\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+indonesian"),
    ("tr", "Turkish", r"\bin\s+turkish\b|turkish\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+turkish"),
    ("nl", "Dutch", r"\bin\s+dutch\b|dutch\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+dutch"),
    ("pl", "Polish", r"\bin\s+polish\b|polish\s+(?:lyrics|vocals?|song|singing|language|version)|sings?\s+in\s+polish"),
]
LANG_NAMES = {c: n for c, n, _ in _LANG_DETECT}


def _detect_language(text_l: str) -> str:
    """Return an ISO code when the request names a lyric language, else ''."""
    import re as _re
    for code, _name, pat in _LANG_DETECT:
        if _re.search(pat, text_l):
            return code
    return ""


def _llm_lyrics(style: str = "", theme: str = "", banned: Optional[list] = None,
                duration: float = 0.0, language: str = "en") -> Optional[str]:
    """LM lyrics with patience. The LM server restarts itself under memory
    pressure (17GB model + ACE renders share RAM) — a cold reload takes
    30-90s. Template lyrics poison the whole vocal delivery, so WAIT for the
    LM to come back rather than settling: up to 3 attempts, pausing for the
    server between them. Returns None only after honest effort."""
    for attempt in range(3):
        out = _llm_lyrics_once(style=style, theme=theme, banned=banned,
                               duration=duration, language=language)
        if out:
            return out
        # Down or cold-loading? Poll /v1/models up to ~100s before retrying.
        for _ in range(10):
            try:
                with urlopen(LM_MODELS_URL, timeout=5):
                    break
            except Exception:
                time.sleep(10)
        print(f"[llm_lyrics] retry {attempt + 2}/3", flush=True)
    return None


def _llm_lyrics_once(style: str = "", theme: str = "", banned: Optional[list] = None,
                     duration: float = 0.0, language: str = "en") -> Optional[str]:
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

        lang_block = ""
        if language and language != "en":
            lang_block = ("\n\nLANGUAGE: write every lyric line in "
                          f"{LANG_NAMES.get(language, language)}. Keep the structure "
                          "tags like [chorus] and [verse] in English.")
        style_part = (style or "pop song").strip()
        theme_part = (f"\nTHEME (anchor the lyrics in this — use concrete details from it): {theme}." if theme else "").strip()
        # Short songs (≤90s) open COLD on the chorus hook — starting the sheet
        # with [chorus] pulls ACE's vocal entry way forward (a [verse]-first
        # sheet invited 20-30s instrumental intros on 60s songs, Matt
        # 2026-07-08). Long songs keep the classic verse-first shape.
        if duration and duration <= 15:
            structure = (
                "Structure:\n"
                "[chorus] 2 punchy lines only — a jingle hook, nothing else\n"
            )
            length_line = "Write a 10-second JINGLE — one irresistible hook, sung from the first beat.\n"
        elif duration and duration <= 45:
            structure = (
                "Structure:\n"
                "[chorus] 4 short lines — the hook; the song OPENS on this\n"
                "[verse]  4 short lines\n"
            )
            length_line = "Write a 30-second song that starts singing immediately.\n"
        elif duration and duration <= 90:
            structure = (
                "Structure:\n"
                "[chorus] 4 short lines — the hook; the song OPENS on this\n"
                "[verse]  4 short lines\n"
                "[chorus] same chorus repeated\n"
            )
            length_line = "Write a 1-minute song that starts singing right away.\n"
        else:
            # Long songs open on the hook too (2026-07-29, Matt's 3-min drill
            # song: verse-first sheet -> ACE ad-libbed "ye ye yea" for the
            # first 30s waiting for the verse. Same fix as short songs).
            structure = (
                "Structure:\n"
                "[chorus] 4 short lines — the hook; the song OPENS on this\n"
                "[verse]  4 short lines\n"
                "[chorus] same chorus repeated\n"
                "[verse]  4 short lines (DIFFERENT imagery from verse 1)\n"
                "[chorus] same chorus repeated\n"
            )
            length_line = "Write a 2-minute song that starts singing right away.\n"
        # Hip-hop asks were shipping FOLK-register lyrics (Matt's orca song,
        # 2026-07-13: heavy rap style prompt, but the sheet came out sung
        # nature quatrains and ACE followed the WORDS into soft-rock/country).
        # Rule 5's "short singable lines" is right for sung genres and wrong
        # for rap — verses must read as dense spoken BARS for the track to
        # land hip-hop. Chorus stays a short sung hook.
        rap_label, _rap_enemies = _requested_genre((style or "").lower())
        if rap_label == "hip hop music" and duration and duration > 15:
            if duration <= 45:
                structure = (
                    "Structure:\n"
                    "[chorus] 4 short lines — the sung hook; the song OPENS on this\n"
                    "[verse]  8 rapped bars\n"
                )
            elif duration <= 90:
                structure = (
                    "Structure:\n"
                    "[chorus] 4 short lines — the sung hook; the song OPENS on this\n"
                    "[verse]  8 rapped bars\n"
                    "[chorus] same chorus repeated\n"
                )
            else:
                # Hook-first here too — the long-form verse-first rap sheet is
                # what gave Matt 30s of "ye ye yea" on a 3-min drill song
                # (2026-07-29). Rap opening on the sung hook is classic form.
                structure = (
                    "Structure:\n"
                    "[chorus] 4 short lines — the sung hook; the song OPENS on this\n"
                    "[verse]  8 rapped bars\n"
                    "[chorus] same chorus repeated\n"
                    "[verse]  8 rapped bars (DIFFERENT imagery and rhymes from verse 1)\n"
                    "[chorus] same chorus repeated\n"
                )
            structure += (
                "\nRAP REGISTER — this is a hip-hop song; the verses are RAPPED, not sung:\n"
                "- Every verse line is a BAR: 8-14 words, written to be spit with flow.\n"
                "- End rhyme on every bar plus internal rhyme; multisyllable rhymes and wordplay.\n"
                "- Inside verses bars need density — long spoken lines are right here. The CHORUS stays short and singable.\n"
                "- Spoken rap cadence and swagger even on a nature or love theme — never a folk poem.\n"
            )
        # A&R stage: invent the actual song before writing lines (2026-07-24).
        _ask = style_part + (f" — about: {theme}" if theme and theme.strip() and theme.strip() != style_part else "")
        _brief = _songwriting_brief(_ask)
        brief_block = ""
        if _brief:
            brief_block = (
                "\nTHE SONG — the customer text above was the sound; THIS is the "
                "subject:\n" + _brief + "\n"
                "SONG RULES:\n"
                "- The singer is WHO IS SINGING, talking to TALKING TO, about WHAT "
                "HAPPENED. Stay in that voice the whole song.\n"
                "- Build the chorus around THE ONE LINE nearly word for word.\n"
                "- Verse 2 is later in the story than verse 1 — something moved.\n"
                "- Slant rhyme beats forced rhyme; never sacrifice the sentence to the rhyme.\n"
            )
        user_prompt = (
            length_line
            + f"STYLE: {style_part}.{theme_part}\n"
            + brief_block + "\n"
            + structure
            + f"{ban_block}{lang_block}\n\n"
            "Output the lyrics now."
        )
        payload = {
            "model": LM_MODEL,
            "messages": [
                # Brief-reasoning nudge: at temperature 0.95 Gemma's thinking
                # channel can ramble past the whole token budget and the lyric
                # sheet never gets emitted as content (bit us 2026-07-08).
                {"role": "system", "content": SYSTEM_PROMPT +
                 "\n7. Keep any private reasoning brief — under 150 words — then output the lyrics."},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature": 0.95,
            "top_p": 0.92,
            "frequency_penalty": 0.6,  # discourage repeating its own clichés
            "presence_penalty":  0.4,
            "max_tokens": 1400,
            # 2026-07-08: THE speed fix. With thinking on, Gemma reasons for
            # 1-6 min per attempt (often blowing the whole token budget →
            # empty content → retries → customer stares at a frozen screen).
            # enable_thinking=false: same model, same lyric quality, ~12s.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            req = UrlRequest(LM_URL, data=json.dumps(payload).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=LM_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            # Model name gone stale (LM Studio model swapped)? Ask what's
            # actually loaded and retry once with that.
            fallback = _lm_first_available()
            if not fallback or fallback == payload["model"]:
                raise
            payload["model"] = fallback
            req = UrlRequest(LM_URL, data=json.dumps(payload).encode("utf-8"),
                             headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=LM_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not msg or ("[verse]" not in msg.lower() and "[chorus]" not in msg.lower()):  # jingles are [chorus]-only
            # Thinking models sometimes draft the whole sheet inside the
            # reasoning channel and hit the token cap before re-emitting it as
            # content. Salvage the draft: from the first [verse] tag keep
            # tag/short-lyric lines, stop at the first long prose line.
            think = (data.get("choices", [{}])[0].get("message", {}).get("reasoning") or "")
            i = think.lower().find("[verse]")
            if i >= 0:
                keep = []
                for ln in think[i:].splitlines():
                    s = ln.strip()
                    if not s:
                        keep.append("")
                        continue
                    if len(s) > 80:          # prose commentary — sheet ended
                        break
                    keep.append(s)
                cand = "\n".join(keep).strip()
                if "[chorus]" in cand.lower() and cand.count("\n") >= 8:
                    msg = cand
                    print("[llm_lyrics] salvaged sheet from reasoning channel", flush=True)
        if not msg or ("[verse]" not in msg.lower() and "[chorus]" not in msg.lower()):  # jingles are [chorus]-only
            return None
        # Belt-and-braces for banned phrases. Two rules learned 2026-07-18 after
        # a paid customer song died with an auto-refund: (1) match WORD BOUNDARIES
        # only — banned 'rust' must not nuke every lyric containing 'trust';
        # (2) if a banned word genuinely slips in, swap it for a neutral word
        # instead of rejecting — 8 identical retries never converge and a
        # swapped word beats a dead song.
        import random as _rnd_mod
        slipped = [b for b in all_banned
                   if re.search(r"\b" + re.escape(b) + r"\b", msg, re.IGNORECASE)]
        if slipped:
            _swaps = ["smoke", "static", "shadow", "echo", "ember", "gravel"]
            _rnd = _rnd_mod.Random(len(msg))
            for b in slipped:
                msg = re.sub(r"\b" + re.escape(b) + r"\b", _rnd.choice(_swaps), msg, flags=re.IGNORECASE)
            print(f"[llm_lyrics] swapped banned phrase(s) {slipped!r} (word-boundary) instead of rejecting", flush=True)
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
    req_gender = _detect_gender(style_l)
    if req_gender == "female":
        # The user asked for a woman — leading tokens dominate ACE's caption
        # conditioning (jingle lesson), so the demand goes FIRST, before the
        # user's own style text, not appended after it.
        parts.insert(0, "female rapper, woman lead vocalist, solo female voice"
                        if any(k in style_l for k in HIPHOP_GENRE_KEYS)
                        else "woman lead vocalist, solo female voice")
        parts.append("expressive female vocal, female lead singer, a woman's voice, NOT a male voice, NO male lead")
    elif req_gender == "duet":
        parts.append("male and female duet vocals")
    elif not has_vocal_hint:
        parts.append("expressive male vocal")
    # Matt 2026-07-08: customers read a long instrumental intro as "half my
    # song is empty" — cue ACE to bring the singing in promptly. "first ten
    # seconds" still gave ~20s intros; be blunt. Skip for explicitly
    # instrumental requests.
    if "instrumental" not in style_l and "no vocal" not in style_l:
        parts.insert(1, "vocals start immediately")
        parts.append("no instrumental intro, singing from the very first bar")
        # "vocals start immediately" alone made ACE fill the intro with
        # yeah-yeah ad-libs when the sheet opened on a verse (Matt's 3-min
        # drill song, 2026-07-29) — demand real words, not vocalizations.
        parts.append("the first words sung are the actual opening lyrics, "
                     "no yeah-yeah ad-lib intro, no vocalization filler")

    # ACE-Step's vocal default skews toward white pop. For Black-rooted
    # genres, append explicit timbre + lineage cues UNLESS the user already
    # named a non-Black ethnicity/voice family (don't override deliberate
    # choices like "Latin tenor" or "Korean ballad").
    if any(k in style_l for k in BLACK_ROOTED_GENRE_KEYS):
        contradicts = any(k in style_l for k in (
            "white", "asian", "latin", "korean", "japanese", "chinese",
            "indian", "arabic", "celtic", "european",
        ))
        # 2026-07-24 (Matt: "why does the reggae sound like a white guy"):
        # saying "black man's voice" used to SKIP the reinforcement on the
        # theory the prompt already said it — but ACE doesn't map demographic
        # words to timbre; the reinforcement IS the translation. Skipping it
        # made explicit requests sound WHITER than implicit ones. Always
        # reinforce unless the style names a different ethnicity.
        if not contradicts:
            # Hip-hop/rap needs MC framing, not gospel-singer framing — the
            # default melodic reinforcement below makes rap drift sung/white.
            # Reggae/dancehall/dub needs chesty Rasta framing — the gospel
            # reinforcement makes reggae drift toward soul curls + white tenor
            # (Matt called the first 'Mountain in the Mist' too white 2026-05-13).
            fem = req_gender == "female"
            if any(k in style_l for k in HIPHOP_GENRE_KEYS):
                parts.append(HIPHOP_VOCAL_REINFORCEMENT_F if fem else HIPHOP_VOCAL_REINFORCEMENT)
            elif any(k in style_l for k in REGGAE_GENRE_KEYS):
                parts.append(REGGAE_VOCAL_REINFORCEMENT_F if fem else REGGAE_VOCAL_REINFORCEMENT)
            else:
                parts.append(BLACK_VOCAL_REINFORCEMENT_F if fem else BLACK_VOCAL_REINFORCEMENT)
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
    "private",  # customer-app job: never in Matt's library/Music.app/VPS page
    # Story Forge film scores. _is_library_song() has honoured these two since
    # they were written, but they were never PERSISTED — so a forge_server
    # restart dropped the flag and a 150s movie cue rejoined the library on the
    # duration rule. Matt, 2026-07-29: "when I'm listening to my songs, I don't
    # wanna hear any Hank and Doug songs or any songs that were made in story
    # forge for movies."
    "video_only", "is_song",
)


def _sidecar_path(jid: str) -> Path:
    return OUT / f"{jid}.json"


# --- VPS rating mirror (Forge → nicedreamzwholesale.com/songs) -------------

VPS_SITE_URL = "https://nicedreamzwholesale.com/songs"
VPS_SSH_HOST = "ineedhemp"
VPS_ADMIN_TOKEN_PATH = "/home/u701983700/domains/nicedreamzwholesale.com/public_html/songs/.admin_token"

# --- Library gate: keep the published songs page "sacred" -------------------
# Only real songs (>= 2 min) auto-publish to nicedreamzwholesale.com/songs and
# count as library tracks. Anything shorter is video/jingle music — it still
# generates and is usable for clips, but it never reaches the public songs page
# or the real library. Set a job's "video_only": True to force-exclude a long
# track too; set "is_song": True to force-keep a short one you really want up.
LIBRARY_MIN_DURATION = 120  # seconds (2:00)


def _is_library_song(j: dict) -> bool:
    """True only for real songs that belong on the public songs page / library."""
    if j.get("video_only") or j.get("private"):
        return False
    if j.get("is_song"):
        return True
    return float(j.get("duration") or 0) >= LIBRARY_MIN_DURATION


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
                    and _is_library_song(j)  # gate: real songs only, no video jingles
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


def _trim_long_intro(wav: Path, target: float = 0.0) -> Optional[float]:
    """Guarantee vocals land early (Matt 2026-07-08). ACE ignores 'no intro'
    prompt cues often enough that 60s songs shipped with 20-30s instrumental
    openings. Deterministic fix: whisper the first 45s, find the first segment
    with real words; if the vocal enters later than 12s, cut the head so it
    enters ~6s (0.6s fade-in). Returns seconds trimmed, or None if untouched.
    Instrumentals are inherently safe — no words means no onset, no trim."""
    try:
        if not (WHISPER_BIN.exists() and WHISPER_MODEL.exists()):
            return None
        import tempfile
        import re as _re
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-t", "45",
                            "-i", str(wav), "-ar", "16000", "-ac", "1", str(probe)],
                           capture_output=True, timeout=60)
            # Whisper on the full file merges intro+first line into one
            # segment stamped from 0:00, hiding a 25s intro. Probe 6s slices
            # instead: the first slice that transcribes to real words is where
            # the vocal actually lives.
            onset = None
            for win in range(0, 40, 4):
                sl = Path(td) / f"sl{win}.wav"
                subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                                "-ss", str(win), "-t", "5", "-i", str(probe),
                                str(sl)], capture_output=True, timeout=30)
                r = subprocess.run([str(WHISPER_BIN), "-m", str(WHISPER_MODEL),
                                    "-f", str(sl), "-np"],
                                   capture_output=True, text=True, timeout=60)
                txt = " ".join(r.stdout.splitlines())
                # strip [music], (soft guitar), ♪, timestamps, whitespace
                bare = _re.sub(r"\[.*?\]|\(.*?\)|♪|-->|[\d:.\s]", "", txt)
                if len(bare) >= 10:  # real words, not "Mm"/"Ooh"/fillers
                    onset = float(win)
                    break
            # window start under-reports the true onset by up to 5s, so a
            # detected onset of 10 means words at 10-15s — worth trimming.
            # Short deliveries (jingles / 30s tier) keep only the first
            # `target` seconds — vocals must enter almost immediately or the
            # exact-length cut ships intro-only audio (Lost Coast, 2026-07-09:
            # sang at 8s, 10s keep-window faded out right as singing began).
            short = 0 < target <= 30
            min_onset = 4.0 if short else 10.0
            lead = 1.0 if short else 3.0
            if onset is None or onset < min_onset or onset > 40.0:
                return None
            cut = max(0.0, onset - lead)
            trimmed = Path(td) / "trimmed.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", f"{cut:.2f}",
                            "-i", str(wav), "-af", "afade=t=in:st=0:d=0.6",
                            "-ar", "48000", "-ac", "2", "-sample_fmt", "s16",
                            str(trimmed)],
                           capture_output=True, timeout=120)
            if trimmed.is_file() and trimmed.stat().st_size > 100_000:
                shutil.copyfile(trimmed, wav)
                return cut
    except Exception as e:
        print(f"[introtrim] {e}", flush=True)
    return None


def _has_sung_vocals(wav: Path) -> bool:
    """True if whisper hears real words anywhere in the render. Same 5s
    slice-probe as _trim_long_intro (full-file whisper merges/misses sung
    onsets). Gates lyric jobs — ACE sometimes renders a lyric sheet fully
    instrumental (sparse jingle sheets + instrument-first styles)."""
    try:
        if not (WHISPER_BIN.exists() and WHISPER_MODEL.exists()):
            return True   # can't check — never block delivery on a missing tool
        import tempfile
        import re as _re
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-t", "45",
                            "-i", str(wav), "-ar", "16000", "-ac", "1", str(probe)],
                           capture_output=True, timeout=60)
            for win in range(0, 40, 4):
                sl = Path(td) / f"sl{win}.wav"
                subprocess.run([FFMPEG, "-y", "-loglevel", "error",
                                "-ss", str(win), "-t", "5", "-i", str(probe),
                                str(sl)], capture_output=True, timeout=30)
                if not sl.is_file() or sl.stat().st_size < 20_000:
                    continue   # past the end of a short render
                r = subprocess.run([str(WHISPER_BIN), "-m", str(WHISPER_MODEL),
                                    "-f", str(sl), "-np"],
                                   capture_output=True, text=True, timeout=60)
                txt = " ".join(r.stdout.splitlines())
                bare = _re.sub(r"\[.*?\]|\(.*?\)|♪|-->|[\d:.\s]", "", txt)
                if len(bare) >= 10:
                    return True
    except Exception as e:
        print(f"[vocalgate] {e}", flush=True)
        return True
    return False


def _lead_vocal_reads_male(wav: Path) -> Optional[bool]:
    """Demucs-isolate the OPENING 15s vocal and pitch-track it. Returns True
    (male-ish lead), False (female-ish), or None (can't tell / tooling absent).
    Opening window on purpose: the lead starts the song, backup singers arrive
    at the hook and fooled a whole-file pitch pool on 2026-07-09."""
    try:
        if not DEMUCS_BIN.is_file():
            return None
        import tempfile
        import numpy as np
        import wave as _wave
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            head = tdp / "head.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-t", "20",
                            "-i", str(wav), str(head)],
                           capture_output=True, timeout=60)
            r = subprocess.run([str(DEMUCS_BIN), "--two-stems", "vocals",
                                "-d", "mps", "-o", str(tdp), str(head)],
                               capture_output=True, text=True, timeout=300)
            vocals = tdp / "htdemucs" / "head" / "vocals.wav"
            if r.returncode != 0 or not vocals.is_file():
                return None
            mono = tdp / "mono.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(vocals),
                            "-ar", "16000", "-ac", "1", str(mono)],
                           capture_output=True, timeout=60)
            w = _wave.open(str(mono))
            y = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
            sr = w.getframerate()
            frame, hop = 1024, 512
            pts = []   # (time_sec, f0)
            for i in range(0, len(y) - frame, hop):
                seg = y[i:i + frame] * np.hanning(frame)
                if float(np.sqrt((seg ** 2).mean())) < 0.015:
                    continue
                ac = np.correlate(seg, seg, "full")[frame - 1:]
                ac[:sr // 500] = 0
                lag = int(np.argmax(ac[:sr // 80]))
                if lag > 0 and ac[lag] > 0.3 * ac[0]:
                    pts.append((i / sr, sr / lag))
            if len(pts) < 20:
                return None   # not enough voiced signal in the opening
            # SAFE-RANGE rule (Matt 2026-07-09: "not come even close"): judge
            # 4s segments so a male opener can't hide behind female hooks in
            # a pooled median. ANY voiced segment under 175 Hz fails.
            worst = None
            for s0 in range(0, 20, 4):
                seg_f0 = [f for t, f in pts if s0 <= t < s0 + 4]
                if len(seg_f0) < 15:
                    continue
                m = float(np.median(np.array(seg_f0)))
                worst = m if worst is None else min(worst, m)
                print(f"[gendergate] {s0}-{s0 + 4}s median f0 {m:.0f} Hz ({len(seg_f0)} frames)", flush=True)
            if worst is None:
                return None
            return worst < 175.0
    except Exception as e:
        print(f"[gendergate] {e}", flush=True)
        return None


def _fit_to_duration(wav: Path, target: float) -> Optional[float]:
    """Deliver EXACTLY the length the customer bought (Matt 2026-07-08: 'I
    want it to be exact if we can'). Renders run +20s headroom so the
    intro-trim has material to cut; whatever is left over comes off the TAIL
    here with a 1.5s fade-out. Returns the final length if trimmed."""
    try:
        if not target or target < 8:  # 8s floor covers 10s jingles
            return None
        ffprobe = FFMPEG.replace("ffmpeg", "ffprobe")
        r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(wav)],
                           capture_output=True, text=True, timeout=30)
        actual = float((r.stdout or "0").strip() or 0)
        if actual <= target + 1.5:
            return None  # already at/under the asked length — leave it
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fitted = Path(td) / "fitted.wav"
            subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(wav),
                            "-t", f"{target:.2f}",
                            "-af", f"afade=t=out:st={max(0.0, target - 1.5):.2f}:d=1.5",
                            "-ar", "48000", "-ac", "2", "-sample_fmt", "s16",
                            str(fitted)],
                           capture_output=True, timeout=120)
            if fitted.is_file() and fitted.stat().st_size > 100_000:
                shutil.copyfile(fitted, wav)
                return target
    except Exception as e:
        print(f"[fitdur] {e}", flush=True)
    return None


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
    """DISABLED 2026-06-19 (Matt's request): the Song Forge library is now the
    single source of truth. Copying every song into Apple Music created a second,
    drifting pile of duplicates outside the dashboard's control. New songs no
    longer duplicate into Apple Music — they live only in the Song Forge library."""
    return
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
        # A swap carries the source's lyrics and audio, so it MUST carry its
        # privacy too. Without this the child is private=None, which means
        # (1) _status_last never redacts it -- the unauthenticated LAN /api/status
        # hands out a customer's title/idea/lyrics/prompt AND the jid, and the jid
        # alone fetches /audio/<jid>.wav; and (2) songforge_sentry.sh purges on
        # `if s.get("private")`, so the swap NEVER expires and outlives the
        # never-store promise forever. Found 2026-07-15.
        "private": bool(src_job.get("private")),
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


def _adopt_swap_result(src_jid: str, swap_jid: str) -> None:
    """Gender enforcement: wait for the female voice swap and deliver it UNDER
    THE ORIGINAL job id — the customer keeps polling the same song. Mirrors
    swap stage/progress into the source job so the OwnATune stall watchdog
    sees movement instead of yanking the job mid-swap."""
    deadline = time.time() + 15 * 60
    src = None
    while time.time() < deadline:
        time.sleep(5)
        with JOBS_LOCK:
            swap = JOBS.get(swap_jid)
            src = JOBS.get(src_jid)
        if not src:
            return   # source deleted while we worked
        if not swap or swap.get("status") == "error":
            break
        if swap.get("status") == "done":
            swap_wav = OUT / f"{swap_jid}.wav"
            src_wav = OUT / f"{src_jid}.wav"
            try:
                if swap_wav.is_file():
                    shutil.copyfile(swap_wav, src_wav)
            except Exception as e:
                print(f"[gendergate] adopt copy failed: {e}", flush=True)
                break
            with JOBS_LOCK:
                src["status"] = "done"
                src["stage"] = "female lead delivered (voice swap)"
                src["audio"] = f"/audio/{src_jid}.wav"
                src["finished_at"] = time.time()
                JOBS.pop(swap_jid, None)   # internal artifact, not a song
            _save_sidecar(src)
            _notify_done(src)
            try:
                swap_wav.unlink()
            except Exception:
                pass
            print(f"[gendergate] {src_jid[:8]} delivered female-swapped lead", flush=True)
            return
        with JOBS_LOCK:
            src["stage"] = f"re-voicing the lead as a woman… ({swap.get('stage', '')})"
            src["progress"] = float(swap.get("progress") or 0.5)
    # swap failed or timed out — ship the last render rather than nothing
    with JOBS_LOCK:
        src = JOBS.get(src_jid)
        if src and src.get("status") != "done":
            src["status"] = "done"
            src["stage"] = "shipped best effort (voice swap failed)"
            src["audio"] = f"/audio/{src_jid}.wav"
            src["finished_at"] = time.time()
    if src:
        _save_sidecar(src)
        _notify_done(src)
    print(f"[gendergate] {src_jid[:8]} swap did not complete — shipped last render", flush=True)


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
        _notify_done(job)

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
_LLM_STATE = {"alive": False, "ts": 0.0}


def _ace_heartbeat_loop():
    """Background heartbeat. /api/status reads the cached flags — never blocks
    the request thread waiting on ACE while it's slammed by model downloads."""
    while True:
        try:
            _ace_get("/health", timeout=4)
            _ACE_STATE["alive"] = True
        except Exception:
            _ACE_STATE["alive"] = False
        _ACE_STATE["ts"] = time.time()
        # lyrics LLM too — a node whose LLM is down looks healthy on ace_up
        # alone, accepts jobs, then hangs at "writing your lyrics"
        try:
            with urlopen(LM_MODELS_URL, timeout=4):
                pass
            _LLM_STATE["alive"] = True
        except Exception:
            _LLM_STATE["alive"] = False
        _LLM_STATE["ts"] = time.time()
        time.sleep(3)


def _ace_alive() -> bool:
    return _ACE_STATE["alive"]


_WARM_CACHE = {"ts": 0.0, "warm": True}


def _engines_warm() -> bool:
    """True when the engines' weights are actually RESIDENT, not merely loaded.

    `ace_up` only says the process answers /health. macOS will happily page a
    fully "initialized" ACE down to a 1GB RSS, and a job routed to it then eats
    a ~3 minute page-in before a note plays — that is what stalled two customer
    songs on 2026-07-27. forge_guard (:8790) tracks resident size against each
    engine's own high-water mark, so ask it. No guard = say warm, so this can
    never make routing worse than it was."""
    now = time.time()
    if now - _WARM_CACHE["ts"] < 10:
        return _WARM_CACHE["warm"]
    warm = True
    try:
        with urlopen("http://127.0.0.1:8790/api/state", timeout=3) as r:
            warm = bool(json.loads(r.read().decode()).get("forge_ok", True))
    except Exception:
        pass
    _WARM_CACHE.update({"ts": now, "warm": warm})
    return warm


def _llm_alive() -> bool:
    return _LLM_STATE["alive"]


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
                            # Vocal gate (Matt 2026-07-09): a job WITH lyrics
                            # must actually sing them — never ship instrumental.
                            style_l2 = (job.get("style") or job.get("idea") or "").lower()
                            wants_vocals = bool((job.get("lyrics") or "").strip()) \
                                and "instrumental" not in style_l2 \
                                and "no vocal" not in style_l2
                            if wants_vocals:
                                job["stage"] = "checking the vocals landed…"
                            if wants_vocals and not _has_sung_vocals(local):
                                rescued = False
                                for alt in (job.get("ace_cache_files") or [])[1:]:
                                    ap2 = Path(alt)
                                    if ap2.is_file() and _has_sung_vocals(ap2):
                                        shutil.copyfile(ap2, local)
                                        rescued = True
                                        print(f"[vocalgate] {job['id'][:8]} variant 1 instrumental — shipped variant 2 (sings)", flush=True)
                                        break
                                if not rescued:
                                    tries = job.get("vocal_retries", 0)
                                    if tries < 2:
                                        with JOBS_LOCK:
                                            job["vocal_retries"] = tries + 1
                                            ap = job["ace_payload"]
                                            if tries == 0:
                                                # sparse sheets are the #1 cause —
                                                # double the sheet, lead with the singer
                                                ly = job.get("lyrics") or ""
                                                ap["lyrics"] = ly + "\n\n" + ly
                                                lead = ("catchy sung jingle" if float(job.get("duration") or 0) <= 30
                                                        else "vocal song")
                                                ap["prompt"] = f"{lead}, male lead vocal singing the tagline, " + ap.get("prompt", "")
                                            else:
                                                ap["prompt"] = "vocal-forward mix, loud clear singing voice front and center, " + ap.get("prompt", "")
                                            job["ace_task_id"] = None
                                            job["status"] = "queued"
                                            job["progress"] = 0.0
                                            job["stage"] = "vocals missing — re-forging with the singer up front"
                                        print(f"[vocalgate] {job['id'][:8]} no vocals heard — re-render {tries + 1}/2", flush=True)
                                        continue
                                    print(f"[vocalgate] {job['id'][:8]} still instrumental after 2 re-renders — shipping as-is", flush=True)
                            job["stage"] = "tightening the intro…"
                            target = float(job.get("duration") or 0)  # what the customer bought
                            cut = _trim_long_intro(local, target)
                            if cut:
                                print(f"[introtrim] {job['id'][:8]} cut {cut:.1f}s of instrumental intro", flush=True)
                            fitted = _fit_to_duration(local, target)
                            if fitted:
                                print(f"[fitdur] {job['id'][:8]} tail-trimmed to exactly {fitted:.0f}s", flush=True)
                            # Gender gate (2026-07-09): a female request whose
                            # delivered lead reads male gets re-rendered — ACE
                            # has no gender switch, caption pressure is all
                            # there is. Judged on the post-trim audio: that is
                            # exactly what the customer will hear first.
                            if _detect_gender((job.get("style") or job.get("idea") or "").lower()) == "female":
                                job["stage"] = "checking the lead voice…"
                                reads_male = _lead_vocal_reads_male(local)
                                gtries = job.get("gender_retries", 0)
                                if reads_male is True and gtries < 3:
                                    with JOBS_LOCK:
                                        job["gender_retries"] = gtries + 1
                                        ap = job["ace_payload"]
                                        ap["prompt"] = ("ONLY female voices, a woman raps and sings every word, "
                                                        "zero male vocals anywhere, " + ap.get("prompt", ""))
                                        job["ace_task_id"] = None
                                        job["status"] = "queued"
                                        job["progress"] = 0.0
                                        job["stage"] = "lead came out male — re-forging with a woman up front"
                                    print(f"[gendergate] {job['id'][:8]} male-range voice on a female request — re-render {gtries + 1}/3", flush=True)
                                    continue
                                if reads_male is True:
                                    # Prompt pressure exhausted — GUARANTEE the
                                    # request with the seed-vc female reference:
                                    # every voice on the track becomes female.
                                    # Hip-hop gets Lady Flow (Matt's pick, the
                                    # "Divine Tribe Hemp Anthem (Lady Flow)" lead,
                                    # 2026-07-13) — Mahalia's gospel timbre reads
                                    # wrong on a rap track. Everything else keeps
                                    # Mahalia.
                                    _fg_label, _fg_en = _requested_genre((job.get("style") or "").lower())
                                    lady = ROOT / "voice_refs" / "lady_flow.wav"
                                    if _fg_label == "hip hop music" and lady.is_file():
                                        fem_name, fem_ref = "Lady Flow", lady
                                    else:
                                        # 2026-07-25: Iman Europe is Matt's pick for the default
                                        # female voice (215Hz center, wide warm range, ref from
                                        # "Kryptonite"). Mahalia stays for explicit gospel via
                                        # the registry.
                                        fem_name, fem_ref = "Iman Europe", ROOT / "voice_refs" / "iman_europe.wav"
                                    if fem_ref.is_file() and DEMUCS_BIN.is_file():
                                        with JOBS_LOCK:
                                            job["ace_task_id"] = None
                                            job["status"] = "running"
                                            job["progress"] = 0.35
                                            job["stage"] = "re-voicing the lead as a woman…"
                                        swap_jid = _spawn_auto_voice_assist(job, {
                                            "voice_name": fem_name,
                                            "voice_path": fem_ref,
                                            "gender": "female"})
                                        if swap_jid:
                                            with JOBS_LOCK:
                                                if swap_jid in JOBS:
                                                    JOBS[swap_jid]["private"] = True
                                            threading.Thread(target=_adopt_swap_result,
                                                             args=(job["id"], swap_jid),
                                                             daemon=True).start()
                                            print(f"[gendergate] {job['id'][:8]} prompt pressure exhausted — female voice swap {swap_jid[:8]}", flush=True)
                                            continue
                                    print(f"[gendergate] {job['id'][:8]} still male, swap unavailable — shipping best effort", flush=True)
                            # Genre gate (2026-07-09): asked hip-hop, got
                            # rock'n'roll? CLAP-score the delivered audio and
                            # re-render on a confident wrong-genre verdict.
                            # Vocal tracks only — CLAP misreads instrumentals.
                            g_label, g_enemies = _requested_genre(style_l2)
                            if wants_vocals and g_label and CLAP_SCRIPT.is_file() and CLAP_PY.is_file():
                                job["stage"] = "checking the genre landed…"
                                try:
                                    gr = subprocess.run(
                                        [str(CLAP_PY), str(CLAP_SCRIPT), str(local), g_label] + g_enemies,
                                        capture_output=True, text=True, timeout=240)
                                    gv = json.loads((gr.stdout or "{}").strip().splitlines()[-1])
                                except Exception as e:
                                    gv = {"verdict": "skip", "reason": str(e)}
                                print(f"[genregate] {job['id'][:8]} asked {g_label} -> {gv}", flush=True)
                                if gv.get("verdict") == "fail":
                                    gtr = job.get("genre_retries", 0)
                                    if gtr < 2:
                                        wrong = (gv.get("top") or [["another genre", 0]])[0][0]
                                        with JOBS_LOCK:
                                            job["genre_retries"] = gtr + 1
                                            ap = job["ace_payload"]
                                            ap["prompt"] = (f"PURE {g_label}, authentic {g_label} rhythm section "
                                                            f"and instrumentation, absolutely NOT {wrong}, "
                                                            + ap.get("prompt", ""))
                                            job["ace_task_id"] = None
                                            job["status"] = "queued"
                                            job["progress"] = 0.0
                                            job["stage"] = "wrong genre came back — re-forging"
                                        print(f"[genregate] {job['id'][:8]} {wrong} on a {g_label} request — re-render {gtr + 1}/2", flush=True)
                                        continue
                                    print(f"[genregate] {job['id'][:8]} still off-genre after 2 re-renders — shipping best effort", flush=True)
                            # bookkeeping: store what actually ships
                            if cut and not fitted and target:
                                job["duration"] = max(15.0, target + 20.0 - cut) if target <= 220 else max(15.0, target - cut)
                            job["status"] = "done"
                            job["audio"] = f"/audio/{local.name}"
                            job["finished_at"] = time.time()
                            _save_sidecar(job)
                            if not job.get("private"):
                                # Customer-app songs stay out of Matt's
                                # exports/ and Music.app entirely.
                                _export_tagged_mp3(job)
                                _drop_into_music_auto_add(job)
                            _notify_done(job)
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
        # 2s→0.5s (Matt 2026-07-18): up to 4s of a 15s job was tick latency.
        time.sleep(0.5)


# ----- queue dispatcher + zombie reaper ---------------------------------------
# ACE-Step can only be trusted with ONE task at a time (two concurrent tasks
# wedge it into 502s). POST /api/song therefore only parks jobs in JOBS; this
# loop feeds ACE the oldest waiting job whenever nothing is in flight, and
# reaps any dispatched job that has sat in queued/running past REAP_SECONDS
# (the VAE-decode hang mode is "stuck forever", so a generous flat timeout is
# safe — normal renders finish in single-digit minutes).
REAP_SECONDS = 30 * 60
DISPATCH_MAX_ATTEMPTS = 3


def _dispatch_loop():
    while True:
        try:
            now = time.time()
            with JOBS_LOCK:
                inflight = [j for j in JOBS.values()
                            if j.get("ace_task_id")
                            and j.get("status") in ("queued", "running")]
                # Reap zombies first — a reaped job frees the ACE slot.
                for j in inflight:
                    started = j.get("dispatched_at") or j.get("created_at") or now
                    if now - started > REAP_SECONDS:
                        j["status"] = "error"
                        j["last_error"] = (
                            f"auto-reaped: no result after {int((now - started) / 60)} min "
                            "(ACE likely wedged; queue released)"
                        )
                        print(f"[reaper] reaped {j['id'][:8]} ({j.get('title') or 'untitled'})", flush=True)
                inflight = [j for j in inflight if j.get("status") in ("queued", "running")]
                nxt = None
                if not inflight:
                    waiting = [j for j in JOBS.values()
                               if j.get("status") == "queued"
                               and not j.get("ace_task_id")
                               and j.get("ace_payload")
                               and j.get("kind") != "swap"]
                    if waiting:
                        nxt = min(waiting, key=lambda j: j.get("created_at", 0))

            if nxt is not None and _ace_alive():
                try:
                    if nxt.get("needs_lyrics"):
                        # Write lyrics here in the background, not in the POST
                        # handler — clients get their job id instantly.
                        lyr = ""
                        for _ in range(3):
                            lyr = _llm_lyrics(style=nxt.get("style", ""),
                                              theme=nxt.get("title") or nxt.get("idea") or "",
                                              duration=float(nxt.get("duration") or 0),
                                              language=(nxt.get("ace_payload") or {}).get("vocal_language", "en")) or ""
                            if lyr:
                                break
                        if not lyr:
                            _theme = (nxt.get("title") or nxt.get("idea") or "").strip()
                            if _theme:
                                # Themed paid request but the lyric LLM never
                                # answered (even after retries). Do NOT ship
                                # generic _seed_lyrics() filler and bill for it —
                                # fail the job so app.py's auto-refund (status ==
                                # "error") kicks in and the customer can retry.
                                with JOBS_LOCK:
                                    if nxt["id"] in JOBS:
                                        nxt["status"] = "error"
                                        nxt["needs_lyrics"] = False
                                        nxt["stage"] = "lyric engine unavailable"
                                        nxt["last_error"] = ("Lyric engine was warming up — you "
                                                             "were not charged. Please try again in a minute.")
                                print("[lyrics] %s themed request but LLM down — failing for auto-refund" % nxt["id"][:8], flush=True)
                                time.sleep(2)
                                continue
                            lyr = _seed_lyrics()
                        with JOBS_LOCK:
                            nxt["lyrics"] = lyr
                            nxt["ace_payload"]["lyrics"] = lyr
                            nxt["needs_lyrics"] = False
                            nxt["stage"] = "lyrics done — sending to ACE"
                    resp = _ace_post("/release_task", nxt["ace_payload"], timeout=15)
                    ace_tid = (resp.get("data") or {}).get("task_id")
                    if not ace_tid:
                        raise RuntimeError(f"no task_id from ACE: {resp}")
                    with JOBS_LOCK:
                        # Job may have been DELETEd while we talked to ACE.
                        if nxt["id"] in JOBS:
                            nxt["ace_task_id"] = ace_tid
                            nxt["dispatched_at"] = time.time()
                            nxt["stage"] = "sent to ACE"
                    print(f"[dispatch] {nxt['id'][:8]} → ACE {ace_tid[:8] if isinstance(ace_tid, str) else ace_tid}", flush=True)
                except Exception as e:
                    with JOBS_LOCK:
                        nxt["dispatch_attempts"] = nxt.get("dispatch_attempts", 0) + 1
                        nxt["stage"] = f"ACE submit failed ({nxt['dispatch_attempts']}x), retrying"
                        if nxt["dispatch_attempts"] >= DISPATCH_MAX_ATTEMPTS:
                            nxt["status"] = "error"
                            nxt["last_error"] = f"ACE submit failed {DISPATCH_MAX_ATTEMPTS}x: {e}"
                    print(f"[dispatch] {nxt['id'][:8]} submit failed: {e}", flush=True)
        except Exception as e:
            print("[dispatch]", e, flush=True)
        # 2s→0.5s (Matt 2026-07-18): pick new jobs up near-instantly.
        time.sleep(0.5)


# ----- "song's done" text to Matt's phone -------------------------------------
IMSG_SEND = Path.home() / ".claude" / "imessage-send.sh"


def _lan_url() -> str:
    """Best-guess URL for reaching this forge from another device on the LAN."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return f"http://{ip}:{PORT}"
    except Exception:
        return f"http://localhost:{PORT}"


def _notify_done(job: Dict[str, Any]) -> None:
    """iMessage Matt that his song finished. Fires only for jobs submitted
    with notify:true (the UI sets that automatically on phones) and only
    inside the 9am–9pm quiet-hours window."""
    if not job.get("notify") or job.get("notified"):
        return
    hour = time.localtime().tm_hour
    if not (9 <= hour < 21):
        return
    if not IMSG_SEND.is_file():
        return
    job["notified"] = True
    title = job.get("title") or job.get("idea") or "your song"
    msg = f"🎶 \"{title}\" is done — {_lan_url()}"
    try:
        subprocess.Popen(["/bin/bash", str(IMSG_SEND), msg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[notify] {e}", flush=True)


# ----- scratch pruner ---------------------------------------------------------
# /api/status is unauthenticated and LAN-reachable (the server binds 0.0.0.0 so
# Matt's iPhone PWA can reach it). It must never hand out a customer-app song:
# not the lyrics, not the prompt, not the jid -- the jid alone fetches the audio
# from /audio/<jid>.wav, which is also unauthenticated. Status may say a job is
# running and how far along it is. Nothing more.
_STATUS_SAFE = ("status", "stage", "progress", "created_at", "finished_at",
                "duration", "private")


def _status_last(j):
    if not j:
        return None
    if not j.get("private"):
        return j
    return {k: j[k] for k in _STATUS_SAFE if k in j}


def _prune_scratch_loop():
    """Every 15 min: drop voice_swap_work/ dirs older than 7 days (failed swaps
    leave their demucs/seed-vc intermediates behind — ~25MB each) and ACE
    cache wavs older than 1h that no job references. Never touches
    outputs/ — that's the library.

    Cadence was 6h until 2026-07-15. The 1h age threshold is the never-store
    promise, but a 6h sweep meant an unreferenced ACE render — a raw copy of a
    customer's song — could sit on disk for up to 7h. The sweep is a few stats;
    run it often enough that the threshold is the real bound, not the loop."""
    while True:
        try:
            now = time.time()
            for d in SWAP_WORK.iterdir():
                try:
                    if d.is_dir() and now - d.stat().st_mtime > 7 * 86400:
                        shutil.rmtree(d)
                        print(f"[prune] swap scratch {d.name}", flush=True)
                except Exception:
                    pass
            referenced = set()
            with JOBS_LOCK:
                for j in JOBS.values():
                    for p in j.get("ace_cache_files") or []:
                        referenced.add(Path(p).resolve())
            if ACE_CACHE.is_dir():
                for f in ACE_CACHE.glob("*.wav"):
                    try:
                        if f.resolve() in referenced:
                            continue
                        if now - f.stat().st_mtime > 3600:
                            f.unlink()
                    except Exception:
                        pass
        except Exception as e:
            print("[prune]", e, flush=True)
        time.sleep(900)


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
        if u.path == "/manifest.json":
            return self._file(ROOT / "manifest.json", "application/manifest+json")

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
                "llm_up": _llm_alive(),
                "warm": _engines_warm(),
                "jobs_total": len(latest),
                "jobs_running": sum(1 for j in latest if j.get("status") in ("queued","running")),
                "last": _status_last(latest[0]) if latest else None,
                "download": _ace_download_status(),
            })
        if u.path == "/api/songs":
            # Matt's UI list — customer-app (private) jobs are hidden unless
            # explicitly requested with ?all=1.
            show_all = parse_qs(u.query).get("all", ["0"])[0] == "1"
            with JOBS_LOCK:
                rows = sorted(JOBS.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            if not show_all:
                rows = [j for j in rows if not j.get("private")]
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
            # Blank lyrics used to be written HERE, synchronously — 30-90s of
            # Gemma inside the POST handler. Clients (and Cloudflare) time out
            # long before that, orphaning rendered songs. Now the dispatcher
            # writes lyrics in the background; POST returns instantly.
            needs_lyrics = not lyrics
            try:
                bpm_in = float(body.get("bpm")) if body.get("bpm") else None
            except Exception:
                bpm_in = None
            prompt = _seed_prompt(style, idea, bpm_in)

            try:
                duration = float(body.get("duration") or 120.0)
            except Exception:
                duration = 120.0
            duration = max(8.0, min(duration, 240.0))  # 8s floor for 10s jingles

            language = (body.get("language") or "en").strip().lower() or "en"
            if language == "en":
                # Nothing explicit from the caller — honor "in Spanish"-style
                # requests written into the song description itself.
                _det = _detect_language(f"{style} {idea}".lower())
                if _det:
                    language = _det
                    print(f"[lang] detected {LANG_NAMES.get(_det, _det)} request", flush=True)
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
            ace_payload = {
                "prompt": prompt,
                "lyrics": lyrics,
                "vocal_language": language,
                "task_type": "text2music",
                "inference_steps": steps,
                "guidance_scale": gscale,
                "audio_format": "wav",
                # Vocal songs render with +20s headroom: ACE loves 20-30s
                # instrumental intros, _trim_long_intro cuts them, and without
                # the pad a 60s purchase came back 35s (2026-07-08). Trimmed
                # songs land near the requested length; untrimmed ones run a
                # little long — a bonus, never a shortfall.
                # NB: check the USER's style text, not the built prompt — the
                # prompt always contains "no instrumental intro" for vocal
                # songs, which made this condition always-true (2026-07-08).
                "audio_duration": duration if "instrumental" in (style or "").lower()
                                   else min(duration + 20.0, 240.0),
                # Force the MLX-native VAE decode instead of the legacy
                # PyTorch tiled decode. On unified-memory Macs the tiled
                # path (a VRAM-saver) intermittently HANGS forever at
                # "Decoding audio... 0.8", wedging the ACE worker and
                # cascading 502s on every subsequent submit. We have
                # 107GB unified RAM so tiling buys nothing. (2026-06-29)
                "use_tiled_decode": False,
            }
            # Jobs are NOT sent to ACE here. ACE-Step wedges into 502s when it
            # holds more than one task at a time, so every job waits in the
            # forge queue and _dispatch_loop feeds ACE exactly one in-flight
            # task. Submit as many as you like, back to back.
            try:
                count = int(body.get("count") or 1)
            except Exception:
                count = 1
            count = max(1, min(count, 10))
            notify = bool(body.get("notify"))
            private = bool(body.get("private"))
            # Story Forge asks for film music through this same endpoint. The
            # engine renders it exactly as normal; it just never counts as a
            # library song (see _is_library_song), so it stays out of Matt's
            # music library, Music.app and the public songs page.
            video_only = bool(body.get("video_only"))
            is_song = bool(body.get("is_song"))

            # Caller-supplied id (2026-07-09): the OwnATune app re-dispatches a
            # stalled job to another render node under the SAME id so the
            # client's polling never notices the move.
            jid = re.sub(r"[^0-9a-f]", "", str(body.get("id") or "").lower())[:32]
            if jid and jid in JOBS:
                # Re-dispatch collision (2026-07-09): the app only re-offers an
                # id when it thinks that job is lost (a failed cross-node
                # DELETE left our copy behind). If our copy is queued, running,
                # or already DONE, hand the same id straight back so the caller
                # re-attaches — minting a fresh uuid here stranded finished
                # songs behind an id the app refused to adopt. Only an errored
                # copy is cleared and rebuilt under its old id.
                with JOBS_LOCK:
                    old = JOBS.get(jid) or {}
                    if old.get("status") in ("queued", "running", "done"):
                        return self._json({"id": jid, "ids": [jid],
                                           "reattached": old.get("status"),
                                           "voice_assist": old.get("voice_assist")})
                    JOBS.pop(jid, None)
            if not jid:
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
            ids = []
            with JOBS_LOCK:
                for i in range(count):
                    j = jid if i == 0 else uuid.uuid4().hex
                    JOBS[j] = {
                        "id": j,
                        "status": "queued",
                        "needs_lyrics": needs_lyrics,
                        "stage": "writing your lyrics…" if needs_lyrics else "waiting in forge queue",
                        "ace_payload": dict(ace_payload),
                        "prompt": prompt,
                        "lyrics": lyrics,
                        "idea": idea,
                        "style": style,
                        "title": title if count == 1 else (f"{title} (take {i+1})" if title else title),
                        "duration": duration,
                        "bpm": int(bpm_in) if bpm_in else None,
                        "created_at": time.time() + i * 0.001,  # preserve submit order
                        "voice_assist": voice_assist,
                        "notify": notify,
                        "private": private,
                        "video_only": video_only,
                        "is_song": is_song,
                    }
                    ids.append(j)
            return self._json({"id": jid, "ids": ids, "voice_assist": voice_assist})

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
                    # Inherit privacy with the content -- see the note in
                    # _spawn_auto_voice_assist. A swap of a private song is a
                    # private song.
                    "private": bool(src.get("private")),
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
    threading.Thread(target=_dispatch_loop, daemon=True).start()
    threading.Thread(target=_prune_scratch_loop, daemon=True).start()
    threading.Thread(target=_ace_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_pull_vps_state_loop, daemon=True).start()
    threading.Thread(target=_auto_push_loop, daemon=True).start()
    # 0.0.0.0 (was 127.0.0.1) so Matt's iPhone can reach the forge over
    # LAN/Tailscale — the PWA remote-control setup. ACE itself stays loopback.
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[forge] http://127.0.0.1:{PORT}/  +  {_lan_url()}/  (ACE-Step at {ACE})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
