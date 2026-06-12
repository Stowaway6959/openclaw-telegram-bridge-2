#!/usr/bin/env python3
"""
smtp_listener.py — Reolink motion email → HTTP snapshot → Telegram
"""
import asyncio, os, subprocess, time, threading, re, base64
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER    = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASS    = os.environ["CAMERA_PASSWORD"]
CAMERA_HOST    = os.environ.get("CAMERA_HOST", "192.168.1.199")

CAMERA_URL  = f"http://{CAMERA_HOST}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASS}"
SNAP_PATH   = "/tmp/reolink_snap.jpg"
SMTP_PORT   = 2525
COOLDOWN    = 12          # was 30 — suppresses Reolink's ~10s follow-up but not new events

# per-event-type last-sent timestamp — prevents cross-type suppression
_last_sent  = {}
_lock       = threading.Lock()

def ts():
    return datetime.now().strftime("%H:%M:%S")

def decode_subject(raw):
    """Decode =?UTF-8?B?...?= encoded subjects from Reolink."""
    m = re.search(r'=\?UTF-8\?B\?([A-Za-z0-9+/=]+)\?=', raw, re.IGNORECASE)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return raw

def classify(subject):
    """
    Returns (event_type, is_motion_track).
    event_type: 'person' | 'vehicle' | 'motion'
    is_motion_track: True if this is Reolink's follow-up tracking email (always duplicate).
    """
    s = subject.lower()
    is_track = s.startswith("motion track:")
    if "person" in s:
        return "person", is_track
    if "vehicle" in s:
        return "vehicle", is_track
    return "motion", is_track

EVENT_EMOJI = {
    "person":  "🚶 PERSON",
    "vehicle": "🚗 VEHICLE",
    "motion":  "👁 MOTION",
}

def grab_and_send(event_type):
    now = time.time()
    with _lock:
        last = _last_sent.get(event_type, 0)
        if now - last < COOLDOWN:
            remaining = int(COOLDOWN - (now - last))
            print(f"  [{ts()}] [cooldown:{event_type}] {remaining}s — skip", flush=True)
            return
        _last_sent[event_type] = now

    t0 = time.time()
    subprocess.run(["curl", "-s", "--max-time", "6", CAMERA_URL, "-o", SNAP_PATH],
                   capture_output=True)
    snap_ms = int((time.time() - t0) * 1000)

    if not os.path.exists(SNAP_PATH) or os.path.getsize(SNAP_PATH) < 10000:
        print(f"  [{ts()}] [error] snapshot failed ({snap_ms}ms)", flush=True)
        return

    label   = EVENT_EMOJI.get(event_type, "🚨")
    clock   = datetime.now().strftime("%I:%M:%S %p")
    caption = f"🚨 FRONT — {label} — {clock}"

    t1 = time.time()
    subprocess.run([
        "curl", "-s",
        "-F", f"chat_id={CHAT_ID}",
        "-F", f"photo=@{SNAP_PATH}",
        "-F", f"caption={caption}",
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    ], capture_output=True, timeout=15)
    send_ms = int((time.time() - t1) * 1000)

    print(f"  [{ts()}] [sent:{event_type}] snap={snap_ms}ms tg={send_ms}ms — {caption}", flush=True)

class _Auth:
    def __call__(self, server, session, envelope, mechanism, auth_data):
        from aiosmtpd.smtp import AuthResult
        return AuthResult(success=True)

class MotionHandler:
    async def handle_DATA(self, server, session, envelope):
        content = envelope.content.decode("utf-8", errors="ignore")
        raw_subject = next(
            (l[8:].strip() for l in content.splitlines() if l.lower().startswith("subject:")),
            "motion"
        )
        subject = decode_subject(raw_subject)
        event_type, is_track = classify(subject)

        print(f"  [{ts()}] [smtp] {subject[:80]}", flush=True)

        if is_track:
            # Motion Track emails are always Reolink follow-ups — never new events
            print(f"  [{ts()}] [skip] motion-track duplicate", flush=True)
            return "250 OK"

        threading.Thread(target=grab_and_send, args=(event_type,), daemon=True).start()
        return "250 OK"

if __name__ == "__main__":
    from aiosmtpd.controller import Controller

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    controller = Controller(
        MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
        authenticator=_Auth(), auth_required=False, auth_require_tls=False,
    )
    controller.start()
    print(f"[{ts()}] 📧 SMTP ready on port {SMTP_PORT} (cooldown={COOLDOWN}s per type)", flush=True)
    loop.run_forever()
