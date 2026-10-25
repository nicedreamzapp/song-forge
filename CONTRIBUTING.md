# Contributing

Thanks for taking a look. This repo holds the render server and web UI behind Song Forge, plus the
Android app, so a bug report from the app or from your own server run is useful even if you never
send code.

## Reporting a problem

Open an [issue](https://github.com/nicedreamzapp/song-forge/issues/new/choose). The things that
make it quick to fix:

- whether it happened in the iPhone app, the Android app, or your own server run
- your phone model and OS version, or your Mac and macOS version
- the style and lyrics you typed, and what came back
- for a server run, the last lines of the server output and what `/api/status` returned

## Running it locally

The render server needs an Apple Silicon Mac with ACE-Step 1.5 at `engines/ACE-Step-1.5` (API on
port 8001), seed-vc at `engines/seed-vc` with its own `.venv`, LM Studio serving Gemma at
`http://127.0.0.1:1234`, and `ffmpeg`, `demucs` and `mlx_whisper` installed. Then:

```bash
bash ios/forge_supervisor.sh
# or run the server directly
python3 ios/forge_server.py
```

Open `http://127.0.0.1:8767/`. The supervisor and AppleScript have paths hardcoded to
`~/Desktop/PROJECTS/Song Forge`, so adjust those for your machine.

The Android app builds with JDK 17:

```bash
cd android && ./gradlew assembleDebug
```

## Tests

There are no automated tests yet. Before sending a change, start the server, make a song from the
web UI, and check that the lyrics, the audio and the karaoke timing come back right. For Android
changes, install the debug build and check that the app loads and a song downloads.

## Pull requests

Small and focused is easiest to review. For big changes, open an issue first so we can talk it
through. Say whether you tested on the server, an iPhone or an Android phone.

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).
