#!/usr/bin/env python3
"""Local SMTP server -- receives Reolink motion emails and sends Telegram alerts."""
import asyncio, os, time, subprocess, threading, email, json
import urllib.request
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
COOLDOWN        = 15
last_alert      = [0]

def _extract_jpeg(raw_bytes):
    """Reolink attaches a JPEG to plain 'Person/Vehicle Detected' emails.
    Returns bytes of first valid image/jpeg attachment, else None. Motion
    Track events have no attachment."""
    try:
        msg = email.message_from_bytes(raw_bytes)
        for part in msg.walk():
            if (part.get_content_type() or "").lower().startswith("image/"):
                data = part.get_payload(decode=True)
                if data and len(data) > 10_000 and data[:2] == b"\xff\xd8":
                    return data
    except Exception:
        pass
    return None


def grab_and_send(subject, attached=None):
    now = time.time()
    if now - last_alert[0] < COOLDOWN:
        print("Cooldown -- skipping", flush=True)
        return
    last_alert[0] = now

    img     = "/tmp/smtp_snap.jpg"
    img_out = "/tmp/smtp_snap_small.jpg"
    cam_url = f"http://{CAMERA_IP}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"
    label   = "🚨 OUT FRONT 🚨"

    if attached:
        # ponytail: use the JPEG the camera attached to the email. Saves the
        # Snap CGI roundtrip (~1-2s) AND shows the actual motion-moment frame
        # instead of a delayed "now" snapshot.
        with open(img, "wb") as f:
            f.write(attached)
    else:
        subprocess.run(["curl", "-s", "--max-time", "15", cam_url, "-o", img], capture_output=True)

    if os.path.exists(img) and os.path.getsize(img) > 10_000:
        subprocess.run(["sips", "--resampleWidth", "1280", img, "--out", img_out], capture_output=True)
        send = img_out if os.path.exists(img_out) and os.path.getsize(img_out) > 5000 else img
        subprocess.run(["curl", "-s", "-F", f"chat_id={CHAT_ID}", "-F", f"photo=@{send}",
                        "-F", f"caption={label}",
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"],
                       capture_output=True, timeout=30)
        print(f"{label} sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)
    else:
        # ponytail: 2-weeks-ago behavior dropped the alert silently when Snap CGI
        # failed. Text-only fallback so the event still surfaces.
        subprocess.run(["curl", "-s", "-F", f"chat_id={CHAT_ID}",
                        "-F", f"text={label} (no image)\n{subject}",
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"],
                       capture_output=True, timeout=15)
        print(f"{label} text-only sent at {datetime.now().strftime('%H:%M:%S')}", flush=True)


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
        attached = _extract_jpeg(envelope.content)
        print(f"Email received: {subject}"
              + (f"  (attached {len(attached)}B)" if attached else "  (no attach)"),
              flush=True)
        threading.Thread(target=grab_and_send, args=(subject, attached), daemon=True).start()
        return "250 OK"


def _reset_email_push():
    """Toggle camera email-enable off/on to break SMTP backoff. A listener
    restart leaves the camera's SMTP client in a multi-minute backoff --
    without this reset, real motion events get missed for 3-10 minutes
    after every restart."""
    try:
        base = f"http://{CAMERA_IP}/cgi-bin/api.cgi"
        login = json.dumps([{"cmd": "Login", "param": {"User": {"Version": "0",
                 "userName": CAMERA_USER, "password": CAMERA_PASSWORD}}}]).encode()
        tok = json.loads(urllib.request.urlopen(base + "?cmd=Login", login, timeout=5).read())[0]["value"]["Token"]["name"]
        # camera rejects GetEmailV20 with any "param" field ("param error" rspCode -4)
        p = json.dumps([{"cmd": "GetEmailV20", "action": 0}]).encode()
        cfg = json.loads(urllib.request.urlopen(
            base + "?cmd=GetEmailV20&token=" + tok, p, timeout=5).read())[0]["value"]["Email"]
        for enable in (0, 1):
            cfg["enable"] = enable
            payload = json.dumps([{"cmd": "SetEmailV20", "param": {"Email": cfg}}]).encode()
            urllib.request.urlopen(base + "?cmd=SetEmailV20&token=" + tok, payload, timeout=5).read()
            time.sleep(2)
        print("email push reset -- backoff cleared", flush=True)
    except Exception as e:
        print(f"email reset err: {e}", flush=True)


print(f"📧 SMTP listener on port {SMTP_PORT}", flush=True)
controller = Controller(MotionHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                        authenticator=Authenticator(), auth_required=False,
                        auth_require_tls=False)
controller.start()
threading.Thread(target=_reset_email_push, daemon=True).start()
print("Ready -- waiting for camera emails...", flush=True)
asyncio.get_event_loop().run_forever()
