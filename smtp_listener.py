#!/usr/bin/env python3
"""Local SMTP server — receives Reolink motion emails and sends Telegram alerts."""
import asyncio, os, time, subprocess, threading
from datetime import datetime
from dotenv import load_dotenv
from aiosmtpd.controller import Controller

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER     = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]
CAMERA_IP       = os.environ.get("CAMERA_HOST", "192.168.1.199")
SMTP_PORT       = 2525
COOLDOWN        = 20
last_alert      = [0]

def grab_and_send():
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown — skipping", flush=True)
        return
    last_alert[0] = now

    img     = "/tmp/smtp_snap.jpg"
    img_out = "/tmp/smtp_snap_sm.jpg"
    cam_url = f"http://{CAMERA_IP}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"

    subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img], capture_output=True)

    if not os.path.exists(img) or os.path.getsize(img) < 10000:
        print(f"Snap failed at {datetime.now().strftime('%H:%M:%S')}", flush=True)
        return

    # Resize to 1280px wide — cuts upload from 2.6MB to ~150KB, prevents timeout
    r = subprocess.run(
        ["sips", "-Z", "1280", img, "--out", img_out],
        capture_output=True
    )
    send_img = img_out if r.returncode == 0 and os.path.exists(img_out) else img

    label = "🚨 OUT FRONT 🚨"
    try:
        subprocess.run(
            ["curl", "-s", "-F", f"chat_id={CHAT_ID}",
             "-F", f"photo=@{send_img}",
             "-F", f"caption={label}",
             f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"],
            capture_output=True, timeout=20
        )
        print(f"{label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    except subprocess.TimeoutExpired:
        print(f"Telegram upload timed out at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    except Exception as e:
        print(f"Send error: {e}", flush=True)

class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)

class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        subject = ""
        for line in envelope.content.decode("utf-8", errors="ignore").splitlines():
            if line.lower().startswith("subject:"):
                subject = line[8:].strip()
                break
        print(f"Email received: {subject}", flush=True)
        # Run in thread so SMTP handler never blocks or crashes on timeout
        threading.Thread(target=grab_and_send, daemon=True).start()
        return "250 OK"

print(f"📧 SMTP listener on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
