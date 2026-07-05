#!/usr/bin/env python3
"""Local SMTP server -- receives Reolink motion emails and sends Telegram alerts.

Same shape as the May 22 baseline that worked reliably. Layered on top:
  1. Uses SMTP attachment if the email carries one (skips camera round-trip).
  2. If Snap CGI fallback used, checks JPEG ends with 0xFFD9 -- one retry.
     (2026-07-05 gray-image bug: Reolink returned truncated JPEGs.)
  3. PIL shrink to ~300KB before upload -- sips inverts Reolink Duo color
     profile. Fixes rc=28 Telegram photo timeouts too.
  4. -4 (IPv4) on Telegram curl -- launchd IPv6 resolve hangs (2026-07-05).
  5. Multi-cam subject routing so Air 2 emails don't fetch from FRONT cam.
"""
import asyncio, os, time, subprocess, threading, base64, re, email
from datetime import datetime
from dotenv import load_dotenv
from aiosmtpd.controller import Controller

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER     = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]

CAMERAS = {
    "FRONT": {"ip": os.environ.get("CAMERA_HOST",  "192.168.1.199"), "label": "🚨 OUT FRONT 🚨"},
    "AIR2":  {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 AIR 2 🚨"},
    "BACK":     {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACK 🚨"},
    "BACKYARD": {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACKYARD 🚨"},
}
DEFAULT = {"ip": os.environ.get("CAMERA_HOST", "192.168.1.199"), "label": "🚨 MOTION 🚨"}

SMTP_PORT = 2525
COOLDOWN  = 60  # matches May 22 baseline -- 15s let motion bursts pile up
last_alert = {}


def _decode_subject(raw):
    m = re.search(r"=\?UTF-8\?B\?([^?]+)\?=", raw)
    if not m: return raw
    try: return base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
    except Exception: return raw


def _match_camera(subject):
    su = subject.upper()
    for k, cfg in CAMERAS.items():
        if k in su: return k, cfg
    return "DEFAULT", DEFAULT


def _valid_jpeg(path):
    if not os.path.exists(path) or os.path.getsize(path) < 10_000:
        return False
    with open(path, "rb") as f:
        f.seek(-2, 2)
        return f.read() == b"\xff\xd9"


def _extract_attachment(raw_bytes, out_path):
    try:
        msg = email.message_from_bytes(raw_bytes)
        for part in msg.walk():
            if (part.get_content_type() or "").lower().startswith("image/"):
                data = part.get_payload(decode=True) or b""
                if len(data) >= 10_000 and data[-2:] == b"\xff\xd9":
                    with open(out_path, "wb") as f: f.write(data)
                    return True
    except Exception as e:
        print(f"attach err: {e}", flush=True)
    return False


def grab_and_send(cam_key, cam_cfg, raw_bytes, subject_plain):
    now = time.time()
    if now - last_alert.get(cam_key, 0) < COOLDOWN:
        print(f"[{cam_key}] Cooldown -- skipping", flush=True)
        return
    last_alert[cam_key] = now

    ip, label = cam_cfg["ip"], cam_cfg["label"]
    img     = f"/tmp/smtp_snap_{cam_key.lower()}_{os.getpid()}_{int(now*1000)}.jpg"
    small   = img.replace('.jpg', '_small.jpg')
    cam_url = f"http://{ip}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"

    if _extract_attachment(raw_bytes, img):
        print(f"[{cam_key}] using SMTP attachment", flush=True)
    else:
        subprocess.run(["curl", "-s", "--max-time", "30", cam_url, "-o", img], capture_output=True)
        if not _valid_jpeg(img):
            # ponytail: one retry -- Reolink Snap CGI truncates when busy
            time.sleep(1)
            subprocess.run(["curl", "-s", "--max-time", "30", cam_url, "-o", img], capture_output=True)
            if not _valid_jpeg(img):
                # No image possible -- send text-only so event still surfaces.
                print(f"[{cam_key}] snap failed -- text-only alert", flush=True)
                # Strip the "Motion Track:" prefix + everything after "at" for
                # a compact one-liner.
                evt = re.sub(r"^Motion Track:", "", subject_plain)
                evt = re.sub(r"\s+at\s+.*$", "", evt).strip()
                subprocess.run(
                    ["curl", "-4", "-s", "--max-time", "20",
                     "-d", f"chat_id={CHAT_ID}",
                     "-d", f"text={label} (no image) — {evt}",
                     f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"],
                    capture_output=True, timeout=30
                )
                try: os.remove(img)
                except OSError: pass
                return

    upload = img
    try:
        from PIL import Image
        with Image.open(img) as im:
            if im.size[0] > 1600:
                im.thumbnail((1600, 1600), Image.LANCZOS)
            im.convert("RGB").save(small, "JPEG", quality=85, optimize=True)
        if os.path.getsize(small) > 5000:
            upload = small
    except Exception as e:
        print(f"[{cam_key}] shrink err: {e}", flush=True)

    print(f"[{cam_key}] uploading {os.path.getsize(upload)//1024}KB", flush=True)
    r = subprocess.run(
        ["curl", "-4", "-s", "--max-time", "60",
         "-F", f"chat_id={CHAT_ID}", "-F", f"photo=@{upload}", "-F", f"caption={label}",
         f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"],
        capture_output=True, timeout=75
    )
    body = r.stdout.decode("utf-8", errors="ignore")[:400]
    if r.returncode != 0:
        print(f"[{cam_key}] tg curl rc={r.returncode}", flush=True)
    elif '"ok":true' not in body:
        print(f"[{cam_key}] tg rejected: {body}", flush=True)
    else:
        print(f"[{cam_key}] {label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)

    for p in (img, small):
        try: os.remove(p)
        except OSError: pass


class Authenticator:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)


class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        subject_raw = ""
        for line in envelope.content.decode("utf-8", errors="ignore").splitlines():
            if line.lower().startswith("subject:"):
                subject_raw = line[8:].strip()
                break
        subject_plain = _decode_subject(subject_raw)
        cam_key, cam_cfg = _match_camera(subject_plain)
        print(f"Email received [{cam_key}]: {subject_plain}", flush=True)
        threading.Thread(target=grab_and_send,
                         args=(cam_key, cam_cfg, envelope.content, subject_plain),
                         daemon=True).start()
        return "250 OK"


print(f"📧 SMTP listener on port {SMTP_PORT}", flush=True)
print(f"Cameras loaded: {list(CAMERAS.keys())}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
print("Ready -- waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
