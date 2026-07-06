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
import asyncio, os, time, subprocess, threading, base64, re, json
import urllib.request
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
# Two cameras: FRONT (.199) and BACK (.200). "air2" in this repo's name is
# the Mac running the bridge, not a camera. BACKYARD kept as a subject alias
# in case the Reolink app name is the longer form.
CAMERAS = {
    "FRONT": {
        "ip":    os.environ.get("CAMERA_HOST",  "192.168.1.199"),
        "label": "🚨 OUT FRONT 🚨",
    },
    "BACKYARD": {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACKYARD 🚨"},
    "BACK":     {"ip": os.environ.get("CAMERA2_HOST", "192.168.1.200"), "label": "🚨 BACK 🚨"},
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


# ponytail: persistent substream readers. A cold RTSP handshake takes 7-11s
# idle and >25s while the camera is busy with a motion event (53 ffmpeg
# timeouts in smtp.log). Keeping one session open per camera and writing the
# newest frame to disk 1x/sec turns the alert path into a file read.
# Both cams are on constant power (confirmed 07-06), so always-on is safe.
# BACK/BACKYARD alias the same camera -> same file; startup dedups readers
# by file path.
_BACK_IMG = "/tmp/smtp_stream_back.jpg"
STREAM_CAMS = {
    "FRONT":    "/tmp/smtp_stream_front.jpg",
    "BACK":     _BACK_IMG,
    "BACKYARD": _BACK_IMG,
}

def _stream_reader(cam_key: str, ip: str, out: str):
    rtsp = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{ip}:554/h264Preview_01_sub"
    # Orphaned readers from a previous listener survive launchd kickstart and
    # hold a camera RTSP slot, starving the new reader (observed 07-06).
    subprocess.run(["pkill", "-f", f"update 1 {out}"], capture_output=True)
    time.sleep(1)
    while True:
        proc = subprocess.Popen(
            # -timeout (rtsp socket I/O, us): a stalled RTSP socket otherwise
            # hangs ffmpeg forever with no output and no exit (observed
            # 07:44 07-06). 15s stall -> ffmpeg errors out -> loop restarts.
            # NOT -rw_timeout: this build's rtsp demuxer rejects it.
            ["/opt/homebrew/bin/ffmpeg", "-y", "-loglevel", "error",
             "-timeout", "15000000",
             "-rtsp_transport", "tcp", "-i", rtsp,
             "-vf", "fps=1", "-q:v", "3", "-update", "1", out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        # Watchdog: -timeout misses stalls where RTCP keepalives trickle in
        # but no frames arrive (33s frame gap observed 07-06 with ffmpeg
        # still "alive"). If the output file goes stale, kill and reconnect.
        # max(last, t0) gives a fresh spawn 25s of handshake grace.
        while proc.poll() is None:
            time.sleep(5)
            try:
                last = os.path.getmtime(out)
            except OSError:
                last = 0
            if time.time() - max(last, t0) > 25:
                proc.kill()
                proc.wait()
                print(f"[{cam_key}] stream stale >25s -- reader killed", flush=True)
                break
        print(f"[{cam_key}] stream reader exited -- restarting in 5s", flush=True)
        time.sleep(5)

def _fresh_stream_frame(cam_key: str, img: str) -> bool:
    """Copy the persistent reader's latest frame to img if it's <10s old.
    Retries once on a truncated JPEG (ffmpeg -update writes in place)."""
    out = STREAM_CAMS.get(cam_key)
    if not out:
        return False
    reason = "?"
    for _ in range(2):
        try:
            age = time.time() - os.path.getmtime(out)
            size = os.path.getsize(out)
            if age < 10 and size > 5_000:
                with open(out, "rb") as f:
                    data = f.read()
                if data[-2:] == b"\xff\xd9":
                    with open(img, "wb") as f:
                        f.write(data)
                    return True
                reason = "truncated jpeg"
            else:
                reason = f"age={age:.1f}s size={size}"
        except OSError as e:
            reason = repr(e)
            break
        time.sleep(0.3)
    print(f"[{cam_key}] stream frame rejected ({reason}) -- falling back to fetch", flush=True)
    return False


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
    # ponytail: pull one frame from the RTSP substream (1536x432, ~230KB)
    # instead of Snap CGI which pulls the 7680x2160 main stream (~3MB) and
    # truncates when the camera CPU is busy processing motion. Substream is
    # already being encoded continuously, so this adds ~0 camera CPU load.
    rtsp = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{ip}:554/h264Preview_01_sub"

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

    ok = _fresh_stream_frame(cam_key, img)
    if ok:
        print(f"[{cam_key}] frame from persistent stream", flush=True)
    else:
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

    caption = label
    if not ok:
        # Last resort before text-only: a stale stream frame up to 2 min old
        # still shows what's in the driveway. Caption flags the age.
        out = STREAM_CAMS.get(cam_key)
        if out:
            try:
                age = time.time() - os.path.getmtime(out)
                with open(out, "rb") as f:
                    data = f.read()
                if age < 120 and len(data) > 5_000 and data[-2:] == b"\xff\xd9":
                    with open(img, "wb") as f:
                        f.write(data)
                    ok = True
                    caption = f"{label} (frame {int(age)}s old)"
                    print(f"[{cam_key}] using stale stream frame ({int(age)}s old)", flush=True)
            except OSError:
                pass

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
                        "-F", f"photo=@{img}", "-F", f"caption={caption}",
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
_started = set()
for _key, _out in STREAM_CAMS.items():
    if _out in _started:
        continue
    _started.add(_out)
    threading.Thread(target=_stream_reader,
                     args=(_key, CAMERAS[_key]["ip"], _out),
                     daemon=True).start()
    print(f"[{_key}] persistent substream reader started -> {_out}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()


def _reset_cam_email_push(ip):
    """After our listener restarts, the camera's SMTP client is left with a
    stale TCP session and drops into a multi-minute backoff -- no motion
    emails arrive during that window. Toggling the camera's email-enable
    off/on forces it to reconnect immediately. (Restored from 3091d44;
    was lost in the cf5dea4 revert.)"""
    try:
        base = f"http://{ip}/api.cgi"
        login = json.dumps([{"cmd": "Login", "param": {"User": {"Version": "0",
                 "userName": CAMERA_USER, "password": CAMERA_PASSWORD}}}]).encode()
        r = urllib.request.urlopen(base + "?cmd=Login", login, timeout=5).read()
        tok = json.loads(r)[0]["value"]["Token"]["name"]
        p = json.dumps([{"cmd": "GetEmailV20", "action": 0,
                         "param": {"channel": 0}}]).encode()
        cfg = json.loads(urllib.request.urlopen(
            base + "?cmd=GetEmailV20&token=" + tok, p, timeout=5).read())[0]["value"]["Email"]
        for enable in (0, 1):
            cfg["enable"] = enable
            payload = json.dumps([{"cmd": "SetEmailV20", "param": {"Email": cfg}}]).encode()
            urllib.request.urlopen(base + "?cmd=SetEmailV20&token=" + tok, payload, timeout=5).read()
            time.sleep(2)
        print(f"[{ip}] email push reset", flush=True)
    except Exception as e:
        print(f"[{ip}] email reset err: {e}", flush=True)


for _ip in {c["ip"] for c in CAMERAS.values()}:
    threading.Thread(target=_reset_cam_email_push, args=(_ip,), daemon=True).start()

print("Ready -- waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
