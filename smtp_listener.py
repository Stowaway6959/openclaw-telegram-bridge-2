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
MIN_SNAP_BYTES  = 500_000   # real RTSP frame is 1.5-3MB; anything smaller = stale/failed
last_alert      = [0]
RTSP_URL        = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{CAMERA_IP}:554/h264Preview_01_main"
FFMPEG          = "/opt/homebrew/bin/ffmpeg"

def grab_and_send():
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown — skipping", flush=True)
        return
    last_alert[0] = now

    img = "/tmp/smtp_snap.jpg"

    # 1. Try RTSP (best quality, but camera may be busy during motion)
    try:
        os.remove(img)
    except FileNotFoundError:
        pass
    try:
        subprocess.run([
            FFMPEG, "-rtsp_transport", "tcp",
            "-i", RTSP_URL,
            "-vframes", "1", "-q:v", "3",
            "-update", "1", img, "-y"
        ], capture_output=True, timeout=12)
    except Exception:
        pass
    sz = os.path.getsize(img) if os.path.exists(img) else 0
    print(f"RTSP snap: {sz//1024}KB", flush=True)

    # 2. RTSP failed — fall back to HTTP snap + resize
    #    Raw HTTP snap is 40-180KB at 7680x2160 (too compressed, shows grey).
    #    Resize to 1280px wide via ffmpeg: downsampling 6x averages out artifacts
    #    and produces a clean displayable image even from a degraded source.
    if sz < MIN_SNAP_BYTES:
        cam_url = f"http://{CAMERA_IP}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"
        http_img = "/tmp/smtp_http.jpg"
        subprocess.run(["curl", "-s", "--max-time", "8", cam_url, "-o", http_img], capture_output=True)
        http_sz = os.path.getsize(http_img) if os.path.exists(http_img) else 0
        print(f"HTTP snap: {http_sz//1024}KB", flush=True)
        if http_sz > 10000:
            try:
                os.remove(img)
            except FileNotFoundError:
                pass
            subprocess.run([
                FFMPEG, "-i", http_img,
                "-vf", "scale=1280:-2",
                "-q:v", "4", img, "-y"
            ], capture_output=True, timeout=10)
            sz = os.path.getsize(img) if os.path.exists(img) else 0
            print(f"Resized: {sz//1024}KB", flush=True)

    if sz < 10000:
        print(f"All methods failed — skipping", flush=True)
        return

    label = "🚨 OUT FRONT 🚨"
    try:
        subprocess.run([
            "curl", "-s",
            "-F", f"chat_id={CHAT_ID}",
            "-F", f"photo=@{img}",
            "-F", f"caption={label}",
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        ], capture_output=True, timeout=30)
        print(f"{label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    except subprocess.TimeoutExpired:
        print("Telegram upload timed out", flush=True)


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
        threading.Thread(target=grab_and_send, daemon=True).start()
        return "250 OK"


print(f"📧 SMTP ready on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready — waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
