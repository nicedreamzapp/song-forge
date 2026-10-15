# Song Forge press kit

Landing page: [`index.html`](index.html) in this folder. Screenshots and icon: [`ios/docs/appstore/`](../ios/docs/appstore/).

Anything marked **TODO-VERIFY** could not be confirmed from this repo and needs Matt to check before it goes out.

## Fact sheet

| | |
|---|---|
| **Name** | Song Forge (Android package and some internal names use "OwnATune") |
| **One line** | Type a vibe. Get a whole song. |
| **Platforms** | iPhone (iOS) and Android |
| **App Store** | https://apps.apple.com/us/app/id6788616929 |
| **Google Play** | https://play.google.com/store/apps/details?id=com.nicedreamz.ownatune |
| **Product page** | https://nicedreamzwholesale.com/software/song-forge/ |
| **Source** | https://github.com/nicedreamzapp/song-forge (MIT license for the code in this repo) |
| **Price** | Free download with 10 songs included. iPhone: 30 more songs for $2.99, a one-time consumable purchase (`com.nicedreamz.ownatune.songs30`). Android: in-app purchase not wired up yet, so Android users get the free songs only for now. No subscription on either platform. |
| **Song length** | 1, 2, 3 or 4 minutes, picked by the user |
| **Account required** | No |
| **Ads / tracking** | None (per [PRIVACY.md](../PRIVACY.md)) |
| **Age rating** | General audiences, 13+ per the privacy policy. **TODO-VERIFY** the exact App Store and Play ratings. |
| **Android build** | versionName 1.0, minSdk 26 (Android 8.0), targetSdk 35 |
| **iOS version / minimum iOS** | **TODO-VERIFY** (the Swift wrapper is not in this repo) |
| **Downloads** | **TODO-VERIFY**. The README shows a live combined badge from `nicedreamzwholesale.com/software/badge-song-forge.json`; no number is stored in the repo. Do not quote one without checking. |
| **Developer** | Matt Macosko / Nice Dreamz LLC |
| **Contact** | info@nicedreamzwholesale.com |

## Release history

From git history. There are no git tags or GitHub releases in the repo.

| Date | What happened |
|---|---|
| 2026-05-04 | First commit: local AI music generator with web UI and Three.js visualizer |
| 2026-05-05 | Custom mini player, ranged audio streaming, demo video |
| 2026-06-11 | Player overhaul: full library, continuous play, shuffle, karaoke lyrics |
| 2026-07-08 | iPhone app section and App Store screenshots added |
| 2026-07-12 | Repo split into `ios/` and `android/`; Android port added |
| 2026-07-14 | Privacy policy published; Android submitted to Google Play |
| 2026-07-15 | Live on the App Store |
| 2026-08-04 | Live on Google Play |
| 2026-08-06 | MIT license and CREDITS.md added |
| 2026-08-26 | Android release build switched to R8 shrinking |
| 2026-09-27 | Combined download count badge added to README |

## Description, 50 words

Song Forge turns one line of text into a finished song with lyrics, music and vocals. Type "sunset reggae with steel pan, 78 bpm", pick one to four minutes, and the song lands on your phone. Free on iPhone and Android with 10 songs. No account, no subscription, no ads. You own every song.

## Description, 150 words

Song Forge makes a whole song from a sentence. You describe what you want, the genre, the instruments, the voice, what it's about, and pick a length from one to four minutes. Song Forge writes the lyrics, generates the music and sung vocals, and delivers the finished track to your phone. Every song comes with full commercial rights.

The app is free on iPhone and Android and includes 10 songs. On iPhone, 30 more are $2.99, paid once. There is no account, no email and no subscription, and the app carries no ads or third-party analytics.

Songs are rendered on Nice Dreamz's own Apple Silicon hardware using open models: ACE-Step for music, Gemma for lyrics and Whisper for lyric timing, running on Apple's MLX. Matt Macosko built the server around them, including a lyric filter that rejects stock phrases and per-genre vocal cues so soul, gospel, hip-hop and reggae don't all come out sounding the same.

## Five features

- One line of text in, a finished song with lyrics, music and vocals out.
- Pick the length: 1, 2, 3 or 4 minutes.
- 10 free songs, then 30 for $2.99 on iPhone. No subscription.
- No account, no ads, no trackers. A random install ID counts your songs.
- You own the song outright, with full commercial rights.

## Credits

Song Forge stands on other people's work. Please credit these by name when writing about how it works:

- **ACE-Step** (ACE-Step team, Apache-2.0): generates the music and vocals
- **Gemma** (Google DeepMind, Gemma Terms of Use): writes the lyrics, served through LM Studio
- **Whisper** (OpenAI, MIT): lyric timing, run via mlx_whisper
- **MLX** (Apple ml-explore team, MIT): runs models on Apple Silicon
- **seed-vc** (Plachtaa, GPL-3.0): voice conversion in the voice swap pipeline
- **Demucs**, **ffmpeg**, **Three.js**
- Some repo maintenance was done with Anthropic's **Claude Code**

Matt built the render server (`ios/forge_server.py`), the lyric guardrails, the lyric to audio alignment, the voice swap pipeline, the web UI and visualizer, the launch tooling and the Android client. See [CREDITS.md](../CREDITS.md).

## Things not to claim

- Voice swap ("re-sing it in your voice") exists in the render engine. **TODO-VERIFY** whether it is reachable from the phone apps before mentioning it in coverage.
- "Full-length song in about a minute" comes from the app's own home screen. **TODO-VERIFY** against real render times before quoting it as a measurement.
- Screen reader support (VoiceOver, TalkBack) is not documented anywhere in the repo. **TODO-VERIFY** before pitching to accessibility communities as accessible.
- Android purchases are a stub that shows "Purchases coming soon".

## Pitch email template

Subject: Song Forge, a song from one sentence (iPhone + Android)

> Hi [name],
>
> Type "a slow soul song about 20 years married" into Song Forge and you get back a full song with lyrics and a sung vocal. That's the whole app.
>
> You describe the song in one line and pick one to four minutes. It writes the lyrics, makes the music and vocals, and puts the song on your phone. It's free with 10 songs, and on iPhone 30 more are $2.99 once. No account, no subscription, no ads.
>
> It runs on my own Apple Silicon machines with open models (ACE-Step, Gemma, Whisper on MLX), not a big AI cloud.
>
> iPhone: https://apps.apple.com/us/app/id6788616929
> Android: https://play.google.com/store/apps/details?id=com.nicedreamz.ownatune
> Screenshots and details: https://nicedreamzwholesale.com/software/song-forge/
>
> Happy to answer anything.
>
> Matt Macosko
> Nice Dreamz
> info@nicedreamzwholesale.com
