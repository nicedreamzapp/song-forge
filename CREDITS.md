# 🙏 Credits

None of this starts from scratch. Here's whose work this is built on, and under what terms.

| Project | What it does here | By | License |
|---|---|---|---|
| 🎼 [ACE-Step](https://github.com/ace-step/ACE-Step) | Generates the music itself | The ACE-Step team | Apache-2.0 |
| 🎙️ [seed-vc](https://github.com/Plachtaa/seed-vc) | Zero-shot voice conversion for the vocal swap | [Plachtaa](https://github.com/Plachtaa) | GPL-3.0 |
| 👂 [Whisper](https://github.com/openai/whisper) | Transcription and timing checks | OpenAI | MIT |
| 🟢 [Gemma](https://blog.google/technology/developers/gemma-open-models/) | Writes and shapes the lyrics | Google DeepMind | Gemma Terms of Use |
| 🍎 [MLX](https://github.com/ml-explore/mlx) | Runs models on Apple Silicon | Apple's ml-explore team | MIT |

## How this repo relates to those licenses

This repository contains **only the iOS and Android client apps**, which are mine and MIT
licensed. None of the engines above are redistributed here.

Rendering happens on my own machines — you send a request, you get audio back. seed-vc is
GPL-3.0, which permits commercial use and attaches its obligations to *distribution*.
Because it is never shipped to users, those obligations aren't triggered. Saying so plainly
rather than leaving it implied.

---

If your work is listed here and you'd like the wording changed, or if something's
missing or wrong, open an issue and I'll fix it.
