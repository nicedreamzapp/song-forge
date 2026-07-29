# Song Forge — TODO lives on the Mac mini

The authoritative open-items list is on the **Mac mini**:
`/Users/matthewmacosko/SongForgeProd/TODO.md`

A copy is alongside this note as `TODO.md` — it is a SNAPSHOT, taken 2026-07-15.
If it disagrees with the mini, the mini wins.

## Reaching the mini from here
The **USB-C cable is the only working path right now** (verified 2026-07-15):
- mini over USB-C link-local — check `arp -a | grep mini`, or ssh via the VPS (`2222`)
- **WiFi 192.168.1.x is NOT responding** — the M5's WiFi path timed out entirely today.
  If ssh hangs, it's the network, not the box.

## Heads up: this machine is the PRIMARY render node
The mini is only the backup. Capacity for any marketing push lands here first —
warm render 81s, cold 5-10 min. See the TODO's "real constraint" section.

## Also: the mini will delete songs out from under you
`songforge_sentry.sh` on the mini loops `:8767` and `:18767` and DELETEs any delivered
private song older than 1h (never-store policy). If you're debugging a song here and it
vanishes — that's the mini, not an M5 bug.
