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
SMTP_PORT  = 2525
COOLDOWN   = 20
RTSP_SUB   = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554/h264Preview_01_sub"
FFMPEG     = "/opt/homebrew/bin/ffmpeg"

last_alert   = [0]
frame_lock   = threading.Lock()
latest_frame = [None]   # bytes of last good RTSP frame


def is_complete_jpeg(data: bytes) -> bool:
    return len(data) > 10_000 and data[-2:] == b"\xff\xd9"


# ── Background RTSP poller ──────────────────────────────────────────────────
def rtsp_poller():
    """Grab one frame from sub-stream every 8s and cache it."""
    tmp = "/tmp/rtsp_cache.jpg"
    while True:
        try:
            result = subprocess.run([
                FFMPEG, "-rtsp_transport", "tcp", "-i", RTSP_SUB,
                "-vframes", "1", "-q:v", "3", "-update", "1", tmp, "-y"
            ], capture_output=True, timeout=5)
            if os.path.exists(tmp):
                raw = open(tmp, "rb").read()
                ok = is_complete_jpeg(raw)
                print(f"[cache] {len(raw)//1024}KB {'✓' if ok else '✗'} rc={result.returncode}", flush=True)
                if ok:
                    with frame_lock:
                        latest_frame[0] = raw
            else:
                print(f"[cache] no file, rc={result.returncode}", flush=True)
        except Exception:
            pass
        time.sleep(8)


threading.Thread(target=rtsp_poller, daemon=True).start()


# ── Alert sender ────────────────────────────────────────────────────────────
def send_alert(email_img: Optional[bytes]):
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown — skipping", flush=True)
        return
    last_alert[0] = now

    # Priority 1: email attachment (vehicle detections always have one)
    if email_img and is_complete_jpeg(email_img):
        img_data = email_img
        src = "email"
    else:
        # Priority 2: cached RTSP frame (captured seconds before motion)
        with frame_lock:
            img_data = latest_frame[0]
        src = "cache"

    if not img_data:
        print("No image available — skipping", flush=True)
        return

    print(f"Using {src} frame: {len(img_data)//1024}KB", flush=True)

    img     = "/tmp/smtp_snap.jpg"
    img_out = "/tmp/smtp_snap_small.jpg"
    with open(img, "wb") as f:
        f.write(img_data)

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


# ── SMTP handler ────────────────────────────────────────────────────────────
class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)


class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        msg = email_lib.message_from_bytes(envelope.content, policy=email_policy.default)
        subject = str(msg.get("subject", ""))
        print(f"Email received: {subject}", flush=True)

        email_img = None
        for part in msg.walk():
            if part.get_content_type() in ("image/jpeg", "image/jpg", "image/png"):
                data = part.get_payload(decode=True)
                if data and len(data) > 5000:
                    email_img = data
                    print(f"Attachment: {len(data)//1024}KB", flush=True)
                    break

        threading.Thread(target=send_alert, args=(email_img,), daemon=True).start()
        return "250 OK"


print(f"📧 SMTP ready on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
