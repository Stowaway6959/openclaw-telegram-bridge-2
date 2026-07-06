#!/usr/bin/env python3
"""Local SMTP server -- receives Reolink motion emails and sends Telegram alerts.

Multi-camera aware:
  - Parses Subject line to detect which camera triggered.
  - Routes to per-camera IP + label from .env (CAMERA_HOST + CAMERA2_HOST).
  - Falls back to CAMERA_HOST + generic label if subject doesn't match.

Reolink subject convention (base64-encoded UTF-8):
  "MotionTrack:Person Detected from FRONT at 2026/7/5 12:13:30"
  "Person Detected from FRONT at 2026/7/5 12:13:30"
  "MotionTrack:Vehicle Detected from BACKYARD at ..."
The word between "from" and "at" is the camera name set in the Reolink app.
"""
import asyncio, os, time, subprocess, threading, base64, re
from datetime import datetime
from dotenv import load_dotenv
from aiosmtpd.controller import Controller

load_dotenv()

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CHAT_ID         = os.environ["TELEGRAM_CHAT_ID"]
CAMERA_USER     = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]

# Per-camera config. Key = uppercase substring matched against the Reolink
# subject line. First match wins. Order matters -- more specific names first
# (e.g. "AIR2" before "AIR"). If no key matches, DEFAULT is used.
CAMERAS = {
    "FRONT": {
        "ip":    os.environ.get("CAMERA_HOST",  "192.168.1.199"),
        "label": "🚨 OUT FRONT 🚨",
    },
    "AIR2": {
        "ip":    os.environ.get("CAMERA2_HOST", "192.168.1.200"),
        "label": "🚨 AIR 2 🚨",
    },
    # Aliases -- add whatever name you gave Air 2 in the Reolink app here.
    # Multiple keys can point at the same config.
    "BACK":     {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACK 🚨"},
    "BACKYARD": {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACKYARD 🚨"},
}
DEFAULT = {
    "ip":    os.environ.get("CAMERA_HOST", "192.168.1.199"),
    "label": "🚨 MOTION 🚨",
}

SMTP_PORT = 2525
COOLDOWN  = 15
# Per-camera cooldown so a burst on one cam doesn't silence the other.
last_alert = {}
# Locks so two threads racing on the same camera don't both pass the cooldown
# check + both deliver. Reolink sometimes fires 2-3 emails within a second
# for a single motion event -- without a lock the check-and-set is racy.
alert_locks = {}
alert_locks_master = threading.Lock()

def _lock_for(cam_key):
    with alert_locks_master:
        if cam_key not in alert_locks:
            alert_locks[cam_key] = threading.Lock()
        return alert_locks[cam_key]


def _decode_subject(raw: str) -> str:
    """Reolink encodes subjects as `=?UTF-8?B?<base64>?=`. Decode to plain."""
    m = re.search(r"=\?UTF-8\?B\?([^?]+)\?=", raw)
    if not m:
        return raw
    try:
        return base64.b64decode(m.group(1)).decode("utf-8", errors="ignore")
    except Exception:
        return raw


def _match_camera(subject: str):
    """Return (cam_key, cam_cfg) based on subject. Falls back to DEFAULT."""
    subj_upper = subject.upper()
    for key, cfg in CAMERAS.items():
        if key in subj_upper:
            return key, cfg
    return "DEFAULT", DEFAULT


def grab_and_send(cam_key: str, cam_cfg: dict, subject_plain: str):
    # Serialize per-camera work so racing threads don't both pass the cooldown
    # check + both fire an alert. Also ensures /tmp/smtp_snap_<cam>.jpg isn't
    # being read + rewritten by two threads at once.
    with _lock_for(cam_key):
        now = time.time()
        if now - last_alert.get(cam_key, 0) < COOLDOWN:
            print(f"[{cam_key}] Cooldown -- skipping", flush=True)
            return
        last_alert[cam_key] = now

    ip    = cam_cfg["ip"]
    label = cam_cfg["label"]
    img   = f"/tmp/smtp_snap_{cam_key.lower()}.jpg"
    # ponytail: RTSP main stream (7680x2160, ~3MB) after we lowered its
    # bitrate 10240 -> 4096 kbps in the camera config on 2026-07-06.
    # Substream grab was consistently 8-25s and hitting the timeout;
    # main grab is 4-5s cold with room to spare, AND is 8K quality.
    # Revert to _sub if main starts truncating (i.e. camera CPU catches up
    # to the lower bitrate).
    rtsp = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{ip}:554/h264Preview_01_main"

    def _fetch_ok():
        try:
            r = subprocess.run(
                ["/opt/homebrew/bin/ffmpeg", "-y", "-rtsp_transport", "tcp",
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 "-analyzeduration", "500000", "-probesize", "500000",
                 "-i", rtsp, "-frames:v", "1", "-q:v", "3", img],
                capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            print(f"[{cam_key}] ffmpeg timeout", flush=True)
            return False
        if r.returncode != 0 or not os.path.exists(img) or os.path.getsize(img) < 5_000:
            return False
        with open(img, "rb") as f:
            f.seek(-2, 2)
            return f.read() == b"\xff\xd9"

    ok = False
    for attempt in range(2):
        if _fetch_ok():
            ok = True
            if attempt: print(f"[{cam_key}] RTSP-sub OK on attempt {attempt+1}", flush=True)
            break
        time.sleep(1)
    # Curl flags rationale:
    #   -4              force IPv4 -- under launchd context the IPv6 resolve
    #                   for api.telegram.org would hang past 30s while the
    #                   same shell invocation returned in 3s (2026-07-05).
    #   --max-time 20   curl-side wall so a stuck TCP doesn't ride out to
    #                   Python's 45s subprocess timeout.
    #   NO --retry -- earlier version had `--retry 2 --retry-delay 2` which
    #   compounded to 79s worst-case, blowing past the Python 60s timeout
    #   and dropping every alert as TimeoutExpired (2026-07-05 fire).
    tg_flags = ["-4", "-s", "--max-time", "20"]

    if not ok:
        # Air 2 (battery) may not respond to Snap CGI when asleep. Send
        # text-only alert so the event still surfaces.
        print(f"[{cam_key}] Snap failed -- sending text-only alert", flush=True)
        try:
            subprocess.run(["curl", *tg_flags, "-F", f"chat_id={CHAT_ID}",
                            "-F", f"text={label} (no image)\n{subject_plain}",
                            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"],
                           capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            print(f"[{cam_key}] text-only timeout -- Telegram unreachable", flush=True)
            return
        print(f"[{cam_key}] text-only sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)
        return

    try:
        subprocess.run(["curl", *tg_flags, "-F", f"chat_id={CHAT_ID}",
                        "-F", f"photo=@{img}", "-F", f"caption={label}",
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"],
                       capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        print(f"[{cam_key}] photo timeout -- Telegram unreachable", flush=True)
        return
    print(f"[{cam_key}] {label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)


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
                         args=(cam_key, cam_cfg, subject_plain),
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
