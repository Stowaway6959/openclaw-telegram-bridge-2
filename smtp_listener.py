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
RTSP_MAIN  = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554/h264Preview_01_main"
FFMPEG     = "/opt/homebrew/bin/ffmpeg"

last_alert = [0]


def is_complete_jpeg(data: bytes) -> bool:
    return len(data) > 50_000 and data[-2:] == b"\xff\xd9"


def send_alert(email_img: Optional[bytes]):
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown — skipping", flush=True)
        return
    last_alert[0] = now

    img = "/tmp/smtp_snap.jpg"
    img_data = None

    # Path 1: JPEG attached to email (Vehicle/Person Detected emails always have one)
    if email_img and is_complete_jpeg(email_img):
        img_data = email_img
        print(f"Email attachment: {len(img_data)//1024}KB", flush=True)

    # Path 2: RTSP grab (works when camera is idle between recordings)
    if img_data is None:
        try:
            if os.path.exists(img):
                os.remove(img)
            subprocess.run([
                FFMPEG, "-rtsp_transport", "tcp", "-i", RTSP_MAIN,
                "-vframes", "1", "-q:v", "3", "-update", "1", img, "-y"
            ], capture_output=True, timeout=15)
            if os.path.exists(img):
                raw = open(img, "rb").read()
                if is_complete_jpeg(raw):
                    img_data = raw
                    print(f"RTSP grab: {len(raw)//1024}KB", flush=True)
                else:
                    print(f"RTSP: {len(raw)//1024}KB (too small/incomplete)", flush=True)
        except subprocess.TimeoutExpired:
            print("RTSP timed out (camera busy recording)", flush=True)
        except Exception as e:
            print(f"RTSP error: {e}", flush=True)

    if img_data is None:
        print("No image — skipping", flush=True)
        return

    with open(img, "wb") as f:
        f.write(img_data)

    # Resize panoramic from 7680x2160 down to 1280px wide (~200KB) for fast upload
    img_out = "/tmp/smtp_snap_small.jpg"
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

        email_img = None
        for part in msg.walk():
            if part.get_content_type() in ("image/jpeg", "image/jpg", "image/png"):
                data = part.get_payload(decode=True)
                if data and len(data) > 50_000:
                    email_img = data
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
