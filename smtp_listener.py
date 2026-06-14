#!/usr/bin/env python3
"""
Reolink → Telegram bridge.
Primary: polls GetAiState every 5s — no email delay, instant detection.
Backup: SMTP handler catches anything the poller misses.
"""
import asyncio, os, time, subprocess, threading, email as email_lib
from email import policy as email_policy
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv
from aiosmtpd.controller import Controller
import urllib.request, json

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER     = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]
CAMERA_IP       = os.environ.get("CAMERA_HOST", "192.168.1.199")
SMTP_PORT  = 2525
COOLDOWN   = 25
FFMPEG     = "/opt/homebrew/bin/ffmpeg"

last_alert  = [0]
alert_lock  = threading.Lock()


def send_to_telegram(img_data: bytes, label: str):
    img     = "/tmp/smtp_snap.jpg"
    img_out = "/tmp/smtp_snap_small.jpg"
    with open(img, "wb") as f:
        f.write(img_data)
    subprocess.run(["sips", "--resampleWidth", "1280", img, "--out", img_out],
                   capture_output=True)
    if not os.path.exists(img_out) or os.path.getsize(img_out) < 5000:
        img_out = img
    try:
        subprocess.run([
            "curl", "-s",
            "-F", f"chat_id={CHAT_ID}",
            "-F", f"photo=@{img_out}",
            "-F", f"caption={label}",
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        ], capture_output=True, timeout=60)
        print(f"{label} sent {os.path.getsize(img_out)//1024}KB "
              f"at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    except subprocess.TimeoutExpired:
        print("Telegram upload timed out", flush=True)


def grab_snap_and_send(label: str, email_img: Optional[bytes] = None):
    """Grab best available image and send. Called from a daemon thread."""
    with alert_lock:
        now = time.time()
        if now - last_alert[0] < COOLDOWN:
            print("Cooldown — skipping", flush=True)
            return
        last_alert[0] = now

    img_data = None

    # Best: clean JPEG from email attachment (no grey ever)
    if email_img and len(email_img) > 50_000 and email_img[-2:] == b"\xff\xd9":
        img_data = email_img
        print(f"Email attachment: {len(img_data)//1024}KB", flush=True)

    # Good: wait for recording to end then HTTP snap (clean image)
    if img_data is None:
        print("Waiting 16s for recording to end...", flush=True)
        time.sleep(16)
        cam_url = (f"http://{CAMERA_IP}/cgi-bin/api.cgi"
                   f"?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}")
        img = "/tmp/smtp_snap.jpg"
        subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img],
                       capture_output=True)
        sz = os.path.getsize(img) if os.path.exists(img) else 0
        if sz > 500_000:
            img_data = open(img, "rb").read()
            print(f"HTTP snap: {sz//1024}KB ✓", flush=True)
        elif sz > 10_000:
            # Still truncated — try once more after another 5s
            time.sleep(5)
            subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img],
                           capture_output=True)
            sz = os.path.getsize(img) if os.path.exists(img) else 0
            if sz > 10_000:
                img_data = open(img, "rb").read()
                print(f"HTTP snap retry: {sz//1024}KB", flush=True)

    if not img_data:
        print("No image — skipping", flush=True)
        return

    send_to_telegram(img_data, label)


# ── AI state poller — no email delay ───────────────────────────────────────
def ai_poller():
    url = (f"http://{CAMERA_IP}/cgi-bin/api.cgi"
           f"?cmd=GetAiState&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}")
    prev = {"people": 0, "vehicle": 0, "dog_cat": 0}
    while True:
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                data = json.load(r)[0]["value"]
            cur = {k: data.get(k, {}).get("alarm_state", 0)
                   for k in ("people", "vehicle", "dog_cat")}

            for kind, state in cur.items():
                if state == 1 and prev.get(kind, 0) == 0:
                    label = "🚨 OUT FRONT 🚨"
                    print(f"[poll] {kind} detected → {label}", flush=True)
                    threading.Thread(target=grab_snap_and_send,
                                     args=(label,), daemon=True).start()
                    break  # one alert per poll cycle

            prev = cur
        except Exception:
            pass
        time.sleep(5)

threading.Thread(target=ai_poller, daemon=True).start()
print("AI poller started (5s interval)", flush=True)


# ── SMTP backup — catches delayed emails ────────────────────────────────────
class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)


class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        msg = email_lib.message_from_bytes(envelope.content, policy=email_policy.default)
        subject = str(msg.get("subject", ""))
        print(f"Email: {subject}", flush=True)

        email_img = None
        for part in msg.walk():
            if part.get_content_type() in ("image/jpeg", "image/jpg", "image/png"):
                data = part.get_payload(decode=True)
                if data and len(data) > 50_000:
                    email_img = data
                    break

        # Only use email if it has a clean attachment — otherwise poller already handled it
        if email_img:
            threading.Thread(target=grab_snap_and_send,
                             args=("🚨 OUT FRONT 🚨", email_img), daemon=True).start()
        return "250 OK"


print(f"📧 SMTP ready on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
