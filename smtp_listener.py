#!/usr/bin/env python3
"""Local SMTP server — receives Reolink motion emails and sends Telegram alerts."""
import asyncio, os, time, subprocess
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
    cam_url = f"http://{CAMERA_IP}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"
    label = "🚨 OUT FRONT 🚨"

    def is_good_snap(path, min_bytes=400000):
        """Valid JPEG magic + large enough to be a real frame (not a degraded camera-busy response)."""
        try:
            with open(path, "rb") as f:
                header = f.read(3)
            return header == b"\xff\xd8\xff" and os.path.getsize(path) >= min_bytes
        except Exception:
            return False

    # Camera returns degraded ~67KB JPEGs when busy recording motion.
    # Retry up to 4 times with 1s gap until we get a full-quality frame.
    good = False
    for attempt in range(4):
        subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img], capture_output=True)
        if is_good_snap(img):
            good = True
            break
        sz = os.path.getsize(img) if os.path.exists(img) else 0
        print(f"Snap attempt {attempt+1}: {sz} bytes — retrying", flush=True)
        time.sleep(1)

    if good:
        subprocess.run(["curl", "-s", "-F", f"chat_id={CHAT_ID}", "-F", f"photo=@{img}",
                        "-F", f"caption={label}",
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"],
                       capture_output=True, timeout=15)
        print(f"{label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    else:
        print(f"Snap degraded after 4 attempts — skipped at {datetime.now().strftime('%H:%M:%S')}", flush=True)

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
        grab_and_send()
        return "250 OK"

print(f"📧 SMTP listener on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
