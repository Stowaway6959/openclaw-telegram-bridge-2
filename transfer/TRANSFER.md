# Moving the Reolink pipeline to another Mac

Written 2026-09-30, after auditing all 45 active launchd jobs on the primary Mac
to work out which ones actually need the house LAN.

## The short version

**Exactly 5 of 45 jobs need to move.** All five are this one subsystem. Every
other job on that Mac (ASCEND, CoA scraping, PostHog, Stripe, RevenueCat,
App Store Connect, GitHub, Janoshik, Search Console, all the Telegram
reminders) talks only to cloud APIs and keeps working from any network.

| job | type | why it is LAN-bound |
|---|---|---|
| `com.reolink.smtp-air2` | resident | `smtp_listener.py`, SMTP server on **port 2525 that the cameras push motion alerts to** |
| `com.reolink.bridge-air2` | resident | `telegram_bridge.py`, relays alerts out to Telegram |
| `com.dc.sync-cam-smtp-ip` | every 30m | reads this Mac's LAN IP and **reprograms cameras 192.168.1.199/.200** to send to it |
| `com.reolink.lan-watchdog` | 3x/day | keeps the above reachable on the LAN |
| `com.reolink.liveness-watchdog` | every 3m | health-checks ports 2525/59999, self-heals a wedged bridge |

**The direction of traffic is what makes this non-negotiable: the cameras send
TO the Mac.** Take the Mac off the network and the cameras have nowhere to
deliver. No VPN or port-forward fixes it cleanly, because `sync-cam-smtp-ip`
is feeding the cameras a literal `192.168.1.x` address every 30 minutes.

## Install on the new Mac

```bash
git clone https://github.com/Stowaway6959/openclaw-telegram-bridge-2.git
cd openclaw-telegram-bridge-2
# .env is gitignored on purpose. Copy it from the old Mac first:
#   scp oldmac:~/Desktop/APPS/reolink-telegram-bridge-air2/.env .env
./transfer/install-reolink.sh
```

The installer rewrites the hardcoded `/Users/dc` paths for whatever `$HOME` and
repo location it lands in, installs the 5 wrappers and 5 plists, loads them,
and prints the resulting pids and listening ports. It is safe to re-run.

## Two traps

1. **Do NOT run the repo's older `setup.sh` / `install_launchd.sh`.** They
   install `com.openclaw.bridge2` and `com.openclaw.smtp2`, which are not the
   labels that actually run. The live labels are the five in `transfer/plists/`.
2. **`.env` is not in git** (camera password, Telegram token, Anthropic key).
   The installer refuses to load the agents without it rather than leaving
   five crash-looping jobs behind.

## After install

- `sync-cam-smtp-ip` reprograms the cameras automatically within 30 minutes, so
  there is **no manual camera reconfiguration**. Force it with
  `~/bin/sync-cam-smtp-ip.sh`.
- Both resident jobs already wrap themselves in `caffeinate -i`, so they hold
  the new Mac awake on their own.
- Check: ports **2525** and **59999** must be free, and the Mac must not be on
  a network that isolates clients from each other (guest-mode AP), or the
  cameras still cannot reach it.
- Logs land in `~/Library/Logs/reolink-bridge-air2/`.

## Turning it off on the old Mac

Only once the new Mac is confirmed receiving alerts:

```bash
for l in com.reolink.smtp-air2 com.reolink.bridge-air2 com.reolink.lan-watchdog \
         com.reolink.liveness-watchdog com.dc.sync-cam-smtp-ip; do
  launchctl unload ~/Library/LaunchAgents/$l.plist
  mv ~/Library/LaunchAgents/$l.plist ~/Library/LaunchAgents/$l.plist.disabled-$(date +%F)
done
```

Renaming to `.disabled-YYYY-MM-DD` rather than deleting matches the convention
the rest of that machine uses, and keeps the restart audit from trying to
re-bootstrap them.

**Do not run both Macs at once for long.** They would fight over the cameras:
each machine's `sync-cam-smtp-ip` rewrites the cameras' SMTP target to its own
IP every 30 minutes, so alerts would flip between them unpredictably. Brief
overlap while verifying is fine.
