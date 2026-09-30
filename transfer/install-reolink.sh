#!/bin/bash
# Installs the Reolink camera -> Telegram pipeline on THIS Mac.
#
# Why this exists: the cameras (192.168.1.199 / .200) PUSH motion alerts over
# SMTP to whichever Mac is listening on the LAN. That makes the whole pipeline
# LAN-bound, so it has to live on a machine that stays on the house network.
# Everything else in the fleet is cloud-only and does not need to move.
#
# The repo's older setup.sh / install_launchd.sh are STALE: they install
# com.openclaw.bridge2 / com.openclaw.smtp2, which are NOT the labels that
# actually run. The live labels are the five installed here.
#
# Safe to re-run. Rewrites the hardcoded /Users/dc paths for this machine.
set -uo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SRC/.." && pwd)"
OLD_HOME="/Users/dc"
OLD_REPO="/Users/dc/Desktop/APPS/reolink-telegram-bridge-air2"

echo "Installing Reolink bridge"
echo "  repo : $REPO"
echo "  home : $HOME"
[ "$HOME" = "$OLD_HOME" ] && echo "  (same home as source: paths unchanged)"

mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/reolink-bridge-air2"

rewrite() {  # rewrite OLD_REPO first, it is a superstring of OLD_HOME
  sed -e "s|$OLD_REPO|$REPO|g" -e "s|$OLD_HOME|$HOME|g" "$1"
}

# The liveness-watchdog plist needs the Telegram token in EnvironmentVariables.
# This repo is PUBLIC, so the committed copy carries placeholders and the real
# values are injected here from the gitignored .env at install time.
inject_secrets() {
  sed -e "s|__TELEGRAM_TOKEN__|${TELEGRAM_TOKEN:-}|g" \
      -e "s|__TELEGRAM_CHAT_ID__|${TELEGRAM_CHAT_ID:-}|g"
}

echo "-> wrappers"
for f in reolink-bridge-air2.sh reolink-smtp-air2.sh reolink-lan-watchdog.sh; do
  rewrite "$SRC/bin/$f" > "$HOME/$f" && chmod +x "$HOME/$f" && echo "   ~/$f"
done
for f in reolink-liveness.py sync-cam-smtp-ip.sh; do
  rewrite "$SRC/bin/$f" > "$HOME/bin/$f" && chmod +x "$HOME/bin/$f" && echo "   ~/bin/$f"
done

echo "-> python deps"
pip3 install --quiet anthropic python-dotenv aiosmtpd 2>/dev/null \
  || echo "   WARN pip3 install failed; install anthropic python-dotenv aiosmtpd manually"

if [ ! -f "$REPO/.env" ]; then
  echo
  echo "!! $REPO/.env is MISSING."
  echo "   It is gitignored on purpose (camera password, Telegram token, API keys)."
  echo "   Copy it across from the old Mac before loading the agents:"
  echo "     scp olddmac:$OLD_REPO/.env $REPO/.env && chmod 600 $REPO/.env"
  echo "   Stopping here: loading the agents without it just makes them crash-loop."
  exit 1
fi
chmod 600 "$REPO/.env"
set -a; . "$REPO/.env"; set +a   # for the token injection below

echo "-> launch agents"
for p in "$SRC"/plists/*.plist; do
  label="$(basename "$p" .plist)"
  dest="$HOME/Library/LaunchAgents/$label.plist"
  rewrite "$p" | inject_secrets > "$dest"
  chmod 600 "$dest"              # it can hold a live token
  if grep -q "__TELEGRAM" "$dest"; then
    echo "   WARN $label still has an unsubstituted placeholder: check .env has"
    echo "        TELEGRAM_TOKEN and TELEGRAM_CHAT_ID set"
  fi
  plutil "$dest" >/dev/null 2>&1 || { echo "   XML BAD  $label (skipped)"; continue; }
  launchctl unload "$dest" 2>/dev/null
  launchctl load "$dest" 2>/dev/null && echo "   loaded $label"
done

echo
echo "-> verify"
sleep 3
launchctl list | grep -E "com\.reolink\.|sync-cam-smtp-ip" \
  | awk '{printf "   %-38s pid=%-7s last_exit=%s\n", $3, $1, $2}'
echo
echo "   listeners (expect 2525, and 59999 once traffic flows):"
lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | grep -E ":(2525|59999)" | awk '{print "   ",$1,$9}' \
  || echo "    none yet (smtp_listener may still be starting)"
echo
echo "Done. Within 30 min sync-cam-smtp-ip reprograms the cameras to this Mac's"
echo "LAN IP automatically, so no manual camera reconfiguration is needed."
echo "Force it now with:  ~/bin/sync-cam-smtp-ip.sh"
