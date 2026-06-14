#!/usr/bin/env python3
"""Local SMTP server — receives Reolink motion emails and sends Telegram alerts."""
import asyncio, os, time, subprocess, threading, email as email_lib
from email import policy as email_policy
from typing import Optional
from datetime import datetime
from dotenv import load_dotenv
from aiosmtpd.controller import Controller

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER     = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]
CAMERA_IP       = os.environ.get("CAMERA_HOST", "192.168.1.199")
SMTP_PORT = 2525
COOLDOWN  = 30
last_alert = [0]
alert_lock = threading.Lock()


def snap_and_send():
    """Fallback: HTTP snap after waiting for recording to finish."""
    time.sleep(16)
    cam_url = (f"http://{CAMERA_IP}/cgi-bin/api.cgi"
               f"?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}")
    img = "/tmp/smtp_snap.jpg"
    subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img],
                   capture_output=True)
    sz = os.path.getsize(img) if os.path.exists(img) else 0
    print(f"HTTP snap: {sz//1024}KB", flush=True)
    if sz > 10_000:
        send_alert(open(img, "rb").read())


def send_alert(img_data: bytes):
    with alert_lock:
        now = time.time()
        if now - last_alert[0] < COOLDOWN:
            print("Cooldown — skipping", flush=True)
            return
        last_alert[0] = now

    print(f"Sending {len(img_data)//1024}KB image...", flush=True)

    img     = "/tmp/smtp_snap.jpg"
    img_out = "/tmp/smtp_snap_small.jpg"

    with open(img, "wb") as f:
        f.write(img_data)

    # Resize 7680x2160 panoramic to 1280px wide (~200KB) — fast upload, no timeout
    subprocess.run(["sips", "--resampleWidth", "1280", img, "--out", img_out],
                   capture_output=True)
    if not os.path.exists(img_out) or os.path.getsize(img_out) < 5000:
        img_out = img

    label = "🚨 OUT FRONT 🚨"
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


class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)


class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        msg = email_lib.message_from_bytes(envelope.content, policy=email_policy.default)
        subject = str(msg.get("subject", ""))
        print(f"Email: {subject}", flush=True)

        # Extract JPEG attached by Reolink — this is the clean, full-quality image
        for part in msg.walk():
            if part.get_content_type() in ("image/jpeg", "image/jpg", "image/png"):
                data = part.get_payload(decode=True)
                if data and len(data) > 500_000 and data[-2:] == b"\xff\xd9":
                    print(f"Attachment: {len(data)//1024}KB ✓", flush=True)
                    threading.Thread(target=send_alert, args=(data,), daemon=True).start()
                    return "250 OK"

        # No attachment — fall back to HTTP snap after recording ends
        threading.Thread(target=snap_and_send, daemon=True).start()
        return "250 OK"


print(f"📧 SMTP ready on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
