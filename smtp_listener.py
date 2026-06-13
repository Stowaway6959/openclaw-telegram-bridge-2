#!/usr/bin/env python3
"""
smtp_listener.py — Reolink motion email → HTTP snapshot → Telegram
"""
import asyncio, os, subprocess, time, threading, re, base64, urllib.request
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
COOLDOWN    = 12          # per-type cooldown
TRACK_GAP   = 25          # allow Motion Track to fire if gap since last alert >= this

_last_sent  = {}          # {event_type: timestamp}
_last_any   = [0]         # timestamp of most recent sent alert (any type)
_lock       = threading.Lock()

def ts():
    return datetime.now().strftime("%H:%M:%S")

def decode_subject(raw):
    m = re.search(r'=\?UTF-8\?B\?([A-Za-z0-9+/=]+)\?=', raw, re.IGNORECASE)
    if m:
        try:
            return base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return raw

def classify(subject):
    s = subject.lower()
    is_track = s.startswith("motion track:")
    if "person" in s:
        return "person", is_track
    if "vehicle" in s:
        return "vehicle", is_track
    return "motion", is_track

EVENT_EMOJI = {
    "person":  "PERSON",
    "vehicle": "VEHICLE",
    "motion":  "MOTION",
}

def is_valid_jpeg(path):
    """Check file starts with JPEG magic bytes — rejects camera error responses."""
    try:
        with open(path, "rb") as f:
            return f.read(3) == b"\xff\xd8\xff"
    except Exception:
        return False

def fetch_snapshot(path, timeout=6):
    """Grab JPEG from camera over LAN via curl. Returns True on success."""
    try:
        subprocess.run(
            ["curl", "-s", "--max-time", str(timeout), CAMERA_URL, "-o", path],
            capture_output=True
        )
        if not os.path.exists(path) or os.path.getsize(path) < 10000:
            return False
        if not is_valid_jpeg(path):
            print(f"  [{ts()}] [snap error] not a valid JPEG (camera returned error response)", flush=True)
            return False
        return True
    except Exception as e:
        print(f"  [{ts()}] [snap error] {e}", flush=True)
        return False

def send_telegram_photo(path, caption):
    """Send photo to Telegram via curl. Returns send duration ms."""
    t0 = time.time()
    result = subprocess.run([
        "curl", "-s",
        "-F", f"chat_id={CHAT_ID}",
        "-F", f"photo=@{path}",
        "-F", f"caption={caption}",
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    ], capture_output=True, timeout=15)
    ms = int((time.time() - t0) * 1000)
    if b'"ok":false' in result.stdout:
        print(f"  [{ts()}] [tg error] {result.stdout[:120]}", flush=True)
    return ms

def handle_alert(event_type, prefetched_ok):
    now = time.time()
    with _lock:
        last = _last_sent.get(event_type, 0)
        if now - last < COOLDOWN:
            remaining = int(COOLDOWN - (now - last))
            print(f"  [{ts()}] [cooldown:{event_type}] {remaining}s — skip", flush=True)
            return
        _last_sent[event_type] = now
        _last_any[0] = now

    if not prefetched_ok:
        t0 = time.time()
        prefetched_ok = fetch_snapshot(SNAP_PATH)
        print(f"  [{ts()}] [snap fallback] {int((time.time()-t0)*1000)}ms", flush=True)

    if prefetched_ok:
        label   = EVENT_EMOJI.get(event_type, "MOTION")
        clock   = datetime.now().strftime("%I:%M:%S %p")
        caption = f"FRONT - {label} - {clock}"
        send_ms = send_telegram_photo(SNAP_PATH, caption)
        print(f"  [{ts()}] [sent:{event_type}] tg={send_ms}ms — {caption}", flush=True)
    else:
        print(f"  [{ts()}] [error] no valid snapshot for {event_type}", flush=True)

def on_email(event_type, is_track):
    snap_ready  = threading.Event()
    snap_ok     = [False]

    def prefetch():
        t0 = time.time()
        ok = fetch_snapshot(SNAP_PATH)
        snap_ok[0] = ok
        print(f"  [{ts()}] [prefetch] {int((time.time()-t0)*1000)}ms {'ok' if ok else 'fail'}", flush=True)
        snap_ready.set()

    def alert():
        # Motion Track: only fire if enough time has passed since last alert
        if is_track:
            gap = time.time() - _last_any[0]
            if gap < TRACK_GAP:
                print(f"  [{ts()}] [skip] motion-track too soon ({int(gap)}s < {TRACK_GAP}s)", flush=True)
                return
            print(f"  [{ts()}] [track] motion-track gap={int(gap)}s — firing", flush=True)

        snap_ready.wait(timeout=7)
        handle_alert(event_type, snap_ok[0])

    threading.Thread(target=prefetch, daemon=True).start()
    threading.Thread(target=alert,    daemon=True).start()


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
        subject    = decode_subject(raw_subject)
        event_type, is_track = classify(subject)

        print(f"  [{ts()}] [smtp] {subject[:80]}", flush=True)
        threading.Thread(target=on_email, args=(event_type, is_track), daemon=True).start()
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
    print(f"[{ts()}] SMTP ready on port {SMTP_PORT} (cooldown={COOLDOWN}s/type, track-gap={TRACK_GAP}s)", flush=True)
    loop.run_forever()
