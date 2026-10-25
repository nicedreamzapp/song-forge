# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report them privately through GitHub: go to the **Security** tab of this repo and click **Report a vulnerability** ([direct link](https://github.com/nicedreamzapp/song-forge/security/advisories/new)). Only the maintainer sees the report.

Helpful details: what you found, how to reproduce it, which commit or app version, and whether it affects the render server, the iPhone app or the Android app.

I aim to reply within 3 days and to ship a fix or a clear plan within 30 days, depending on severity. You will be credited in the advisory unless you ask not to be.

## Supported versions

Fixes go into the latest App Store and Google Play builds and the server code on `main`. Please update the app before reporting if you are on an older build.

## What matters most here

The parts where a mistake could hurt someone:

- `ios/forge_server.py`, the HTTP server on port 8767 that takes song requests, serves the JSON API and song files, and runs ffmpeg, Demucs and seed-vc on the results
- the voice swap pipeline (`_run_swap_impl` in `forge_server.py`), which handles voice samples
- `android/app/src/main/java/com/nicedreamz/ownatune/MainActivity.kt`, a WebView that loads its URL from a remote pointer file and exposes a JavaScript bridge (`AndroidBuy`) to the page
- the random per-install ID the apps send with each request (see [PRIVACY.md](PRIVACY.md))
