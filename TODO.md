# Song Forge — open items

Read this first when picking the project back up. Last touched 2026-07-15 by Ohm.

App Store: id6788616929 (LIVE since 2026-07-15) · Repo: nicedreamzapp/song-forge
Page: nicedreamzwholesale.com/software/song-forge/ · Web app: songforge.nicedreamzwholesale.com

---

## PRIVACY — fixed 2026-07-15, read this before touching the forge

Melanie made a song in the app and Matt could see it. She was right to ask.

**What was actually broken:**
1. `/api/status` returned the newest job's FULL dict — prompt, lyrics, idea, jid — with
   `private: true` set and no auth. `/api/songs` correctly filtered private; whoever wrote
   it missed `/api/status`. The forge binds `0.0.0.0` (deliberate — Matt's iPhone PWA), so
   this was readable by anything on the LAN. Ohm pulled a customer song's audio with a plain
   curl: HTTP 200, 9.3MB, no token. **FIXED** — `_status_last()` strips content + the jid for
   private jobs. Nothing consumed `last` (the UI only reads `ace_up`/`download`), so nothing broke.
2. ACE scratch wavs survived the song's deletion by up to **3 days** — `_prune_scratch_loop`
   only swept unreferenced files older than 3d. **FIXED** — window is now 1h, which still
   protects an in-flight render (files are unreferenced for the ~81s-10min between ACE writing
   them and the job recording `ace_cache_files`). On restart it immediately swept 20 orphans,
   421MB -> 81MB.

**What was NOT broken (Ohm cried wolf on both — don't re-panic):**
- The sentry works. `forge_supervisor.sh:46` runs it every 5 min; it is NOT a launchd job,
  so `launchctl list | grep sentry` finds nothing. It purged Melanie's song at 12:52 today.
- The 2 six-day-old cache files are Matt's own songs, correctly retained forever.
- Do NOT "fix" the `0.0.0.0` bind to 127.0.0.1 — it's intentional, it's the iPhone PWA path,
  and changing it breaks Matt's phone remote. The leak was the endpoint, not the bind.

**Residual, honest:** a delivered private song still lives in `outputs/` for up to 1h (the
grace so the phone can finish downloading) and its scratch up to 1h more. So "we don't keep
copies" is true within ~2h, not literally zero. If that needs to be zero, delete on the
client's `/audio/` fetch instead of on a timer.

- [ ] **Decide if the 1h delivery grace is acceptable** vs delete-on-download. Marketing
      currently says "we don't keep copies."

## Needs Matt (decisions, or access Ohm doesn't have)

- [ ] **Pick a license for the song-forge repo.** It's public with NO license file, which
      legally means all-rights-reserved — nobody can use it. Bad look on the project whose
      pitch is "you own your songs, no fine print." Engine is open, app is commercial, so
      the split needs a human call. Not Ohm's to make.
- [ ] **Paste the new App Store copy into ASC.** Ohm has no App Store Connect access.
      Drafted 2026-07-15 (subtitle / promo text / keywords / description) — the subtitle
      field is currently EMPTY, which is free ASO keyword space going to waste. That's the
      single highest-leverage item on the listing.
- [ ] **Confirm ACE-Step's upstream license permits commercial output.** The whole "every
      song is 100% yours" claim rests on it. Never verified — only assumed. If it doesn't
      hold, the listing copy is wrong, not just optimistic.

## The real constraint (bigger than any copy tweak)

- [ ] **Render capacity is a laptop.** M5 is the PRIMARY node, mini is backup. Warm render
      = 81s, cold = 5-10 min. One good TikTok and the queue lands on a MacBook Pro that has
      to stay awake and caffeinated. Capacity is a PREREQUISITE for the marketing push, not
      a follow-up to it. Don't ship demo videos until this has an answer.

## Product

- [ ] **8 occasion templates** — birthday, wedding, roast, graduation, love song, pet
      tribute, retirement, new baby. This is the positioning, the ASO keywords, and the
      content pipeline all in one change. Roast is the one with a moat: the lyric model is
      gemma-4-31b-abliterated, so it will write a mean song. Suno refuses.
- [ ] **8 demo videos, one per template.** Eight, not thirty. Thirty is the number that
      quietly doesn't happen. Gated on render capacity above.

## Housekeeping

- [ ] **SongForgeProd has no git remote** and ~20 forge_server.py.bak-* files in the
      production directory. The GitHub copy is pushed from somewhere else.
- [ ] **Android** — submitted to Google Play, review in progress as of July 2026.

---

## Done 2026-07-15 (Ohm)

- Fixed truncated `<title>` on 3 live pages (song-forge, realtime-space, brainforest).
  All were dying mid-sentence in Google results. Backups: `index.html.bak-pre-titlefix-20260715`
  on Hostinger.
- `build_software_pages.py` — added `smart_trim()`, replaced the blind `tagline[:70]` slice
  that caused it. Fixes all 23 generated pages. Dry-run clean; deploys at the 08:00 syncer run.
- `watch.py` — GITHUB_TOKEN was blank, so the syncer ran at 60 req/hr unauthenticated. Now
  falls back to the `gh` keychain token (5000/hr). Build went 55s -> 22s. Deliberately NOT
  written to `.env` — no plaintext credential on disk.
- song-forge repo: set homepage + 13 topics (was bare, no discoverability).

## Notes for whoever picks this up

- **The mini CAN reach Hostinger** via the `ineedhemp` ssh alias (`~/.ssh/id_ed25519_ineedhemp`).
  Earlier belief that only the M5 could was wrong.
- `HANDS_OFF = {"song-forge"}` in build_software_pages.py:641 — the syncer skips this slug.
  The app marketing pages (song-forge, realtime-space, brainforest) are hand-built and NOT
  regenerated. Patching the generator does NOT fix them. Two bugs, one symptom.
- The occasion angle was already on the product page before anyone "suggested" it. Don't
  re-derive strategy that's already shipped — go read the page first.
