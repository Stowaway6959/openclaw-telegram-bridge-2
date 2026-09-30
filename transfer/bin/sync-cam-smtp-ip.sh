#!/bin/bash
# Sync Mac's current LAN IP into Reolink cameras' SMTP config so motion emails
# never get stranded when DHCP hands the Mac a new address on reboot.
# Runs at boot + every 30 min via com.dc.sync-cam-smtp-ip launchd job.
# ponytail: idempotent — no-op when cam config already matches Mac IP.

set -u
LOG=/Users/dc/Library/Logs/reolink-bridge-air2/sync-cam-ip.log
ENV=/Users/dc/Desktop/APPS/reolink-telegram-bridge-air2/.env
CAMS="192.168.1.199 192.168.1.200"

exec >> "$LOG" 2>&1
echo "[$(date '+%F %T')] tick"

[ -r "$ENV" ] || { echo "  no .env"; exit 0; }
# shellcheck disable=SC1090
set -a; source "$ENV"; set +a

IP=""
for IF in en0 en1 en5; do
    IP=$(ipconfig getifaddr "$IF" 2>/dev/null) && [ -n "$IP" ] && break
done
[ -z "$IP" ] && { echo "  no LAN IP yet"; exit 0; }
echo "  mac_ip=$IP"

for CAM in $CAMS; do
    TOK=$(curl -s --max-time 5 -X POST "http://$CAM/api.cgi?cmd=Login" \
        -H "Content-Type: application/json" \
        -d "[{\"cmd\":\"Login\",\"param\":{\"User\":{\"Version\":\"0\",\"userName\":\"$CAMERA_USER\",\"password\":\"$CAMERA_PASSWORD\"}}}]" \
        | /usr/bin/python3 -c 'import json,sys;d=json.load(sys.stdin);print(d[0]["value"]["Token"]["name"] if d[0].get("value") else "")' 2>/dev/null)
    [ -z "$TOK" ] && { echo "  $CAM: login fail"; continue; }

    CUR=$(curl -s --max-time 5 -X POST "http://$CAM/api.cgi?cmd=GetEmailV20&token=$TOK" \
        -H "Content-Type: application/json" \
        -d '[{"cmd":"GetEmailV20","action":0,"param":{"channel":0}}]')

    NOW_IP=$(echo "$CUR" | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)[0]["value"]["Email"]["smtpServer"])' 2>/dev/null)
    # Read failed (cam returns a body with no "value" every other poll): the PATCH below is
    # built from that same unparseable body and would POST garbage to the camera. Skip the
    # tick instead. Root cause of the KeyError traceback in sync-cam-ip.log since 2026-07-17.
    [ -z "$NOW_IP" ] && { echo "  $CAM: read fail, skip"; continue; }

    if [ "$NOW_IP" = "$IP" ]; then
        echo "  $CAM: already $IP, skip"
        continue
    fi

    PATCH=$(echo "$CUR" | IP="$IP" /usr/bin/python3 -c '
import json,sys,os
d=json.load(sys.stdin)
e=d[0]["value"]["Email"]
e["smtpServer"]=os.environ["IP"]
print(json.dumps([{"cmd":"SetEmailV20","action":0,"param":{"Email":e}}]))')

    RES=$(curl -s --max-time 5 -X POST "http://$CAM/api.cgi?cmd=SetEmailV20&token=$TOK" \
        -H "Content-Type: application/json" -d "$PATCH" \
        | /usr/bin/python3 -c 'import json,sys;d=json.load(sys.stdin);print(d[0].get("code"))' 2>/dev/null)
    echo "  $CAM: $NOW_IP -> $IP (rc=$RES)"
done
