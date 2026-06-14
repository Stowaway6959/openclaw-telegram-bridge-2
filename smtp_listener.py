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
SMTP_PORT       = 2525
COOLDOWN        = 20
last_alert      = [0]
RTSP_MAIN       = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554/h264Preview_01_main"
RTSP_SUB        = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554/h264Preview_01_sub"
FFMPEG          = "/opt/homebrew/bin/ffmpeg"


def is_complete_jpeg(data: bytes) -> bool:
    return len(data) > 1000 and data[-2:] == b"\xff\xd9"


def grab_and_send(email_img: Optional[bytes] = None):
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown — skipping", flush=True)
        return
    last_alert[0] = now

    img = "/tmp/smtp_snap.jpg"
    img_data = None

    # --- Path 1: image came in the email itself (fastest, no camera hit needed) ---
    if email_img and is_complete_jpeg(email_img):
        img_data = email_img
        print(f"Email attachment: {len(img_data)//1024}KB ✓", flush=True)

    # --- Path 2: HTTP snap from camera ---
    if img_data is None:
        cam_url = (f"http://{CAMERA_IP}/cgi-bin/api.cgi"
                   f"?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}")
        subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", img],
                       capture_output=True)
        if os.path.exists(img):
            raw = open(img, "rb").read()
            if is_complete_jpeg(raw):
                img_data = raw
                print(f"HTTP snap: {len(raw)//1024}KB ✓", flush=True)
            else:
                print(f"HTTP snap: {len(raw)//1024}KB ✗ (truncated)", flush=True)

    # --- Path 3: RTSP grab (camera finished recording) ---
    if img_data is None:
        def rtsp_grab(url, label):
            try:
                os.remove(img)
            except FileNotFoundError:
                pass
            try:
                subprocess.run([
                    FFMPEG, "-rtsp_transport", "tcp",
                    "-i", url, "-vframes", "1", "-q:v", "3",
                    "-update", "1", img, "-y"
                ], capture_output=True, timeout=12)
            except Exception:
                pass
            if os.path.exists(img):
                raw = open(img, "rb").read()
                ok = is_complete_jpeg(raw)
                print(f"{label}: {len(raw)//1024}KB {'✓' if ok else '✗'}", flush=True)
                return raw if ok else None
            print(f"{label}: 0KB ✗", flush=True)
            return None

        print("HTTP truncated — trying RTSP...", flush=True)
        for attempt in range(6):
            time.sleep(5)
            img_data = rtsp_grab(RTSP_SUB, f"Sub t+{5+attempt*5}s")
            if img_data:
                break
            img_data = rtsp_grab(RTSP_MAIN, f"Main t+{5+attempt*5}s")
            if img_data:
                break

    if img_data is None:
        print("All paths failed — skipping", flush=True)
        return

    with open(img, "wb") as f:
        f.write(img_data)

    # Resize to 1920px wide — drops 2.7MB panoramic to ~200KB for fast upload
    img_send = "/tmp/smtp_snap_small.jpg"
    subprocess.run(["sips", "--resampleWidth", "1920", img, "--out", img_send],
                   capture_output=True)
    if not os.path.exists(img_send) or os.path.getsize(img_send) < 5000:
        img_send = img  # fall back to original if resize fails

    label = "🚨 OUT FRONT 🚨"
    try:
        subprocess.run([
            "curl", "-s",
            "-F", f"chat_id={CHAT_ID}",
            "-F", f"photo=@{img_send}",
            "-F", f"caption={label}",
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        ], capture_output=True, timeout=30)
        sz_send = os.path.getsize(img_send)
        print(f"{label} sent {sz_send//1024}KB at {datetime.now().strftime('%H:%M:%S')}",
              flush=True)
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
        print(f"Email received: {subject}", flush=True)

        # Extract JPEG attachment if Reolink included one
        email_img = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in ("image/jpeg", "image/jpg", "image/png"):
                data = part.get_payload(decode=True)
                if data and len(data) > 5000:
                    email_img = data
                    print(f"Attachment found: {len(data)//1024}KB", flush=True)
                    break

        threading.Thread(target=grab_and_send, args=(email_img,), daemon=True).start()
        return "250 OK"


print(f"📧 SMTP ready on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
