# 🎙 Song Forge

Local AI music generator. Type a description, get a song. Runs entirely on your Mac — no cloud APIs.

## What it does

- **ACE-Step** generates the song (music + vocals) from a style description
- **Gemma 4 31B** (via LM Studio) writes lyrics in the appropriate genre
- **demucs + seed-vc + ffmpeg** voice-swap any song to a cloned voice (yours, your kid's, anyone's sample)
- Outputs wav + auto-tagged mp3 ready to drop into Music.app

## Stack

| Component | Role | Where it runs |
|---|---|---|
| `forge_server.py` | UI + orchestrator | port 8767 |
| ACE-Step 1.5 | Text-to-music model (MLX backend) | port 8001 |
| LM Studio + Gemma 4 31B | Lyric writer | port 1234 |
| demucs (`htdemucs`) | Vocal/instrumental split | invoked from worker |
| seed-vc (Plachta) | Zero-shot voice conversion | invoked from worker |
| ffmpeg | Audio mix | invoked from worker |

## Hardware

Built on Apple Silicon (M5 Max, 128 GB unified memory). Each song generation: ~15s on M5. Voice swap: ~1 min total.

## Layout

```
forge_server.py             # main HTTP server + worker threads
forge_supervisor.sh         # boots ACE-Step + forge_server
launch.applescript          # Desktop launcher (Brave app window)
index.html                  # the web UI
engines/                    # ACE-Step / seed-vc / RVC — install separately
outputs/                    # generated wavs + JSON sidecars (ignored)
exports/                    # auto-tagged mp3s (ignored)
```

## Endpoints

- `GET  /                    ` — UI
- `GET  /api/status          ` — engine health + jobs running
- `GET  /api/songs           ` — library (sidecar-hydrated, persists across restarts)
- `POST /api/song            ` — submit a generation job
- `PATCH /api/song/<id>      ` — rename
- `DELETE /api/song/<id>     ` — hard-delete (wav + sidecar + ACE cache variants)
- `GET  /api/random_lyrics   ` — Gemma writes lyrics for a given style
- `GET  /api/voices          ` — list available voice samples
- `POST /api/swap_voice/<id> ` — voice-swap an existing song
- `GET/POST /api/banned      ` — banned phrase list (cliché filter for Gemma)
- `POST /api/reveal/<id>     ` — open the wav in Finder
- `POST /api/purge_cache     ` — wipe orphan ACE cache files

## Voice swap pipeline

1. demucs splits the song into `vocals.wav` + `no_vocals.wav`
2. seed-vc converts the vocal stem to a target voice (zero-shot, just needs a sample wav)
3. ffmpeg mixes converted vocals over the instrumental, loudness-normalised to −14 LUFS
4. Optional `group_effect`: layers 4 pitched/timed copies for a "group of kids" sound

## Banned phrases

Gemma's lyric prompt explicitly forbids stock genre clichés. Edit the list in the UI under
🚫 Never use these words/phrases. Backed by `.banned_phrases.json` (gitignored).

## License

Personal project. No license assigned.
