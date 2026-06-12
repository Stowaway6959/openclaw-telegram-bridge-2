#!/usr/bin/env python3
"""
smtp_listener.py — Reolink motion email → HTTP snapshot → Telegram

Optimizations:
  1. urllib only — no subprocess curl (saves ~200ms per alert)
  2. Snapshot pre-fetched on email receipt, before cooldown check (hides LAN latency)
  3. Stale snapshot sent instantly (<1s), then fresh snapshot follows
"""
import asyncio, os, time, threading, re, base64, urllib.request, urllib.parse
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
SNAP_STALE  = "/tmp/reolink_snap_stale.jpg"
SMTP_PORT   = 2525
COOLDOWN    = 12

_last_sent  = {}
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
    "person":  "🚶 PERSON",
    "vehicle": "🚗 VEHICLE",
    "motion":  "👁 MOTION",
}

# ── urllib helpers (no subprocess) ────────────────────────────────────────────

def fetch_snapshot(path, timeout=6):
    """Grab JPEG from camera over LAN. Returns True on success."""
    try:
        req = urllib.request.Request(
            CAMERA_URL,
            headers={"Authorization": "Basic " + base64.b64encode(
                f"{CAMERA_USER}:{CAMERA_PASS}".encode()).decode()}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if len(data) < 10000:
            return False
        with open(path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"  [{ts()}] [snap error] {e}", flush=True)
        return False

def send_telegram_photo(path, caption):
    """Multipart upload to Telegram sendPhoto. Returns send duration ms."""
    t0 = time.time()
    boundary = b"----TGBoundary"
    with open(path, "rb") as f:
        img_data = f.read()

    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="chat_id"\r\n\r\n' +
        CHAT_ID.encode() + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="caption"\r\n\r\n' +
        caption.encode() + b"\r\n"
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="photo"; filename="snap.jpg"\r\n'
        b"Content-Type: image/jpeg\r\n\r\n" +
        img_data + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  [{ts()}] [tg error] {e}", flush=True)
    return int((time.time() - t0) * 1000)

# ── core alert logic ───────────────────────────────────────────────────────────

def handle_alert(event_type, prefetched_path):
    """
    Called in a daemon thread. prefetched_path is already on disk (or None).
    Flow:
      1. Send stale snapshot immediately if one exists (perceived <1s)
      2. Check cooldown — drop if too recent
      3. Send fresh snapshot (already fetched in parallel with step 1+2)
    """
    label  = EVENT_EMOJI.get(event_type, "🚨")
    clock  = datetime.now().strftime("%I:%M:%S %p")

    # Step 1 — instant stale send if we have a previous snap
    stale_sent = False
    if os.path.exists(SNAP_STALE) and os.path.getsize(SNAP_STALE) > 10000:
        stale_ms = send_telegram_photo(SNAP_STALE, f"⚡ {label} — {clock} (live coming…)")
        stale_sent = True
        print(f"  [{ts()}] [stale:{event_type}] tg={stale_ms}ms", flush=True)

    # Step 2 — cooldown check (after stale send so user already has something)
    now = time.time()
    with _lock:
        last = _last_sent.get(event_type, 0)
        if now - last < COOLDOWN:
            remaining = int(COOLDOWN - (now - last))
            print(f"  [{ts()}] [cooldown:{event_type}] {remaining}s — skip fresh", flush=True)
            return
        _last_sent[event_type] = now

    # Step 3 — send fresh snapshot (pre-fetched while stale was sending)
    fresh_ok = prefetched_path and os.path.exists(prefetched_path) and os.path.getsize(prefetched_path) > 10000
    if not fresh_ok:
        # prefetch failed or wasn't done — fetch now as fallback
        t0 = time.time()
        fresh_ok = fetch_snapshot(SNAP_PATH)
        print(f"  [{ts()}] [snap fallback] {int((time.time()-t0)*1000)}ms", flush=True)

    if fresh_ok:
        # promote fresh → stale for next event
        import shutil
        shutil.copy2(SNAP_PATH, SNAP_STALE)
        caption = f"🚨 FRONT — {label} — {clock}"
        send_ms = send_telegram_photo(SNAP_PATH, caption)
        print(f"  [{ts()}] [sent:{event_type}] tg={send_ms}ms — {caption}", flush=True)
    else:
        print(f"  [{ts()}] [error] no fresh snapshot for {event_type}", flush=True)


def on_email(event_type):
    """
    Immediately starts two parallel tasks:
      A) Pre-fetch snapshot from camera (LAN, fast)
      B) Send stale + cooldown check + send fresh  (waits for A)
    Net effect: snapshot fetch overlaps with stale send + cooldown window.
    """
    snap_ready = threading.Event()
    snap_result = [None]

    def prefetch():
        t0 = time.time()
        ok = fetch_snapshot(SNAP_PATH)
        snap_result[0] = SNAP_PATH if ok else None
        elapsed = int((time.time() - t0) * 1000)
        print(f"  [{ts()}] [prefetch] {elapsed}ms {'ok' if ok else 'fail'}", flush=True)
        snap_ready.set()

    def alert():
        snap_ready.wait(timeout=7)   # wait up to 7s for prefetch
        handle_alert(event_type, snap_result[0])

    threading.Thread(target=prefetch, daemon=True).start()
    threading.Thread(target=alert,    daemon=True).start()


# ── SMTP handler ───────────────────────────────────────────────────────────────

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
            print(f"  [{ts()}] [skip] motion-track duplicate", flush=True)
            return "250 OK"

        # Kick off prefetch + alert immediately — don't block the SMTP handler
        threading.Thread(target=on_email, args=(event_type,), daemon=True).start()
        return "250 OK"

if __name__ == "__main__":
    from aiosmtpd.controller import Controller

    # warm the stale cache on boot if a previous snap exists
    if os.path.exists(SNAP_PATH) and os.path.getsize(SNAP_PATH) > 10000:
        import shutil; shutil.copy2(SNAP_PATH, SNAP_STALE)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    controller = Controller(
        MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
        authenticator=_Auth(), auth_required=False, auth_require_tls=False,
    )
    controller.start()
    print(f"[{ts()}] 📧 SMTP ready on port {SMTP_PORT} (cooldown={COOLDOWN}s/type, stale+fresh mode)", flush=True)
    loop.run_forever()
