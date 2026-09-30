#!/usr/bin/env python3
"""LAN latency watchdog for Reolink camera.

Runs 4x/day via launchd. Pings the camera, alerts to Telegram if the LAN
is congested. Reboot the router when this fires -- verified 2026-07-06 that
gray photos are ALWAYS a symptom of high LAN latency between Mac and camera.

Uses the same TELEGRAM_TOKEN + TELEGRAM_CHAT_ID as smtp_listener.
"""
import os, subprocess, sys, socket
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN   = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_IP = os.environ.get("CAMERA_HOST", "192.168.1.199")

# Threshold: healthy wired LAN is <5ms. 50ms is a soft warning.
# 100ms means router is genuinely congested and gray photos are imminent.
WARN_MS = 100

def ping_avg_ms():
    """Return average round-trip in ms, or None if all packets dropped."""
    r = subprocess.run(
        ["ping", "-c", "5", "-t", "3", CAMERA_IP],
        capture_output=True, text=True)
    # Parse "round-trip min/avg/max/stddev = 4.6/11.6/25.4/9.7 ms" line
    for line in r.stdout.splitlines():
        if "min/avg/max" in line:
            avg = line.split("=")[1].split("/")[1]
            return float(avg)
    return None

def alert(text):
    # Tag identifies which Mac sent this; both Macs share one bot token + chat.
    text += f" · [{socket.gethostname().split('.')[0]}]"
    subprocess.run([
        "curl", "-4", "-s", "--max-time", "20",
        "-F", f"chat_id={CHAT_ID}",
        "-F", f"text={text}",
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    ], capture_output=True, timeout=30)

def main():
    now = datetime.now().strftime("%H:%M")
    avg = ping_avg_ms()
    if avg is None:
        alert(f"🚨 LAN watchdog {now}: camera {CAMERA_IP} unreachable. Reboot router.")
        print(f"{now} camera unreachable", flush=True)
        return
    if avg > WARN_MS:
        alert(f"🚨 LAN watchdog {now}: ping to camera is {avg:.0f}ms (healthy <20ms). Reboot the router -- gray photos are next.")
        print(f"{now} LAN sick: {avg:.1f}ms", flush=True)
    else:
        print(f"{now} LAN ok: {avg:.1f}ms", flush=True)

if __name__ == "__main__":
    main()
