# 🎙 Song Forge

**Type a vibe. Get a whole song. On your own Mac. No cloud, no subscription, no API keys.**

You write something like *"sunset reggae with steel pan and a male vocalist who sounds like Burning Spear, 78 bpm"* — Song Forge writes lyrics for you (or you can write your own), an AI composes the music and the vocals, and a couple minutes later you've got a finished track in your Library. Drop your own voice sample in and it'll re-sing the song *in your voice* — or your kid's, or anyone you've recorded.

Everything happens on your Mac. Nothing ever leaves it.

---

## 📺 Watch it in action

[![Song Forge — FREE Local AI Music Generator (No Cloud)](https://img.youtube.com/vi/-o_4-Ka-H38/maxresdefault.jpg)](https://youtu.be/-o_4-Ka-H38)

A full walkthrough on YouTube — describing a vibe, generating the song, swapping in a cloned voice. Click the thumbnail above to watch.

---

## 🎬 What it actually does

- **You describe a song** — pick a genre preset, mix in mood / era / tempo / reference artists, fine-tune the vocal arrangement.
- **The AI writes the lyrics** for you in the right style (or paste your own — `[verse]`/`[chorus]` tags work).
- **It generates the song** — music *and* vocals — in about **15 seconds** on a beefy Mac (a couple minutes on a normal one).
- **Library** keeps every track you've made. Play, rename, save the WAV, delete.
- **Voice swap** any track into a cloned voice. Drop a 10-second sample of someone speaking into the voices folder, and Song Forge will re-sing the whole song in their voice. There's even a "group of kids" effect that layers 4 pitched copies for a children's choir feel.

---

## 💻 What kind of computer do I need?

**Apple Silicon Mac.** Sorry Intel folks — the music model uses Apple's MLX framework, which needs an M-series chip.

| Your Mac | What you'll get |
|---|---|
| **M1/M2 with 16 GB RAM** | Lyrics + music will work, but you'll have to skip Gemma (the lyric writer) or run a smaller model. Songs take ~2 min. Voice swap is tight on memory. |
| **M2/M3 Pro/Max, 32 GB RAM** | Sweet spot for most people. Run everything comfortably. Songs in 30–60 seconds. |
| **M3/M4 Max, 64 GB+ RAM** | Snappy. Songs in 20–30 seconds. Multiple jobs at once if you want. |
| **M5 Max, 128 GB RAM** *(my machine)* | About **15 seconds for a 2-minute song.** Voice swap in under a minute. Everything stays buttery. |

**Disk:** Plan for ~30 GB free for the AI models the first time you set it up. Generated songs are ~20 MB each — get a hundred and you've used 2 GB.

**Microphone (optional):** Only needed if you want to record your own voice samples for the voice-swap feature.

**Internet:** Required *once* to download the models. After that — totally offline. Take it on a plane.

---

## 🚀 Get started

**1. Install LM Studio** (for the lyric writer) → [lmstudio.ai](https://lmstudio.ai), pull a Gemma model, hit "Start Server."

**2. Set up the music engine** — clone [ACE-Step](https://github.com/ace-step/ACE-Step) somewhere on your machine. The supervisor script expects it.

**3. Install seed-vc and demucs** for the voice-swap pipeline (`pip install seed-vc demucs`).

**4. Run it:**

```bash
bash forge_supervisor.sh
```

Open [http://localhost:8767](http://localhost:8767) and hit **FORGE A SONG**.

---

## 🎛 What's under the hood

| Component | Role |
|---|---|
| **ACE-Step 1.5** | The music brain. Generates instrumentation + vocals from your style description. (MLX backend on Apple Silicon.) |
| **Gemma 4 31B** (via LM Studio) | The lyricist. Writes verse/chorus lyrics that fit your genre. |
| **Demucs** | Splits a finished song into vocals + instrumental. |
| **seed-vc** (Plachta) | Zero-shot voice cloning. Give it a 10-second sample of someone, it'll re-sing any vocal in their voice. |
| **ffmpeg** | Mixes the new vocals back over the instrumental, loudness-normalized to −14 LUFS so it sounds pro. |
| `forge_server.py` | The Python web server that ties it all together. Runs on port 8767. |
| `index.html` | The whole web UI — liquid-glass theme, drifting cosmic colors, big chunky buttons. |

---

## 🪄 Cool things to try

- **Generate the same lyrics in five different genres** — paste the same `[verse]`/`[chorus]` block, change the style preset, hit MAKE. You'll have five wildly different versions of your song in two minutes.
- **Drop a sample of yourself reading a paragraph into the voices folder.** Voice-swap any song to your voice. Yes, it's surreal.
- **Use the "group of kids" effect** for an instant children's-choir version of any chorus.
- **Type a banned-phrases list** (concrete jungle, one love, irie, etc.) — Gemma will refuse to use them in lyrics.
- **Open Fine Tune** — control era, mood, vocal character, tempo, reference artists, extra instruments. Each one stacks cleanly into the style description.

---

## ⚠️ Heads up

This is a **personal project**. No license assigned, no support promised, no warranty. Built it for myself and put it up because someone might learn from it.

Models are large. Setup takes a minute. But once it's running it's just… yours. Forever. Offline. No one tracking what you make. No subscription that disappears when the company pivots.

Have fun.
