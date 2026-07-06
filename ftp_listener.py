#!/usr/bin/env python3
"""FTP receiver + Telegram forwarder.

Runs alongside smtp_listener.py. Reolink FTP delivery is a different code
path in the camera firmware; some users report FTP delivers clean JPEGs
when SMTP silently truncates. Camera pushes files to ftp_root/motion/,
we forward each new JPEG to Telegram and delete it.
"""
import os, time, subprocess, threading
from datetime import datetime
from dotenv import load_dotenv
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]
FTP_USER       = os.environ.get("FTP_USER", "reolink")
FTP_PASS       = os.environ.get("FTP_PASS", "reolink-drop-2026")
FTP_PORT       = int(os.environ.get("FTP_PORT", "2121"))
ROOT           = os.path.join(os.path.dirname(__file__), "ftp_root")
COOLDOWN       = 15
LABEL          = "🚨 OUT FRONT (FTP) 🚨"

os.makedirs(os.path.join(ROOT, "motion"), exist_ok=True)
_last_send = [0.0]
_seen = set()

def _send(path):
    now = time.time()
    if now - _last_send[0] < COOLDOWN:
        print(f"[FTP] cooldown -- deleting {os.path.basename(path)}", flush=True)
        try: os.remove(path)
        except OSError: pass
        return
    _last_send[0] = now
    is_video = path.lower().endswith((".mp4", ".mov"))
    # Telegram-side flags identical to smtp_listener.
    field   = "video" if is_video else "photo"
    api     = "sendVideo" if is_video else "sendPhoto"
    tg = ["curl", "-4", "-s", "--max-time", "60",
          "-F", f"chat_id={CHAT_ID}",
          "-F", f"{field}=@{path}",
          "-F", f"caption={LABEL}",
          f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{api}"]
    try:
        subprocess.run(tg, capture_output=True, timeout=90)
        kind = "video" if is_video else "photo"
        print(f"[FTP] {LABEL} {kind} sent at {datetime.now().strftime('%H:%M:%S')} ({os.path.getsize(path)}B)", flush=True)
    except subprocess.TimeoutExpired:
        print(f"[FTP] {field} timeout", flush=True)
    finally:
        try: os.remove(path)
        except OSError: pass


class Handler(FTPHandler):
    pass  # pyftpdlib on_file_received didn't fire in 2.2 test; use polling.


def _maybe_send(path):
    is_video = path.lower().endswith((".mp4", ".mov"))
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if is_video:
        # Basic sanity: reject <10KB (probably empty/aborted).
        if size < 10_000:
            print(f"[FTP] rejected {os.path.basename(path)} -- tiny video ({size}B)", flush=True)
            try: os.remove(path)
            except OSError: pass
            return
    else:
        # JPEG end-marker check.
        try:
            with open(path, "rb") as f:
                f.seek(-2, 2)
                end = f.read()
            if end != b"\xff\xd9":
                print(f"[FTP] rejected {os.path.basename(path)} -- truncated JPEG", flush=True)
                try: os.remove(path)
                except OSError: pass
                return
        except OSError:
            return
    _send(path)


def _folder_watcher():
    """Poll the drop folder for new .jpg files. Camera uploads via FTP; we
    detect + forward + delete. Waits until file mtime is stable (write done)
    before reading. Polls at 0.5s so latency stays under a second."""
    print(f"[FTP] watcher polling {ROOT}/motion/ every 0.5s", flush=True)
    while True:
        try:
            for root, _, files in os.walk(os.path.join(ROOT, "motion")):
                for f in files:
                    p = os.path.join(root, f)
                    if p in _seen:
                        continue
                    if not f.lower().endswith((".jpg", ".jpeg", ".mp4", ".mov")):
                        try: os.remove(p)
                        except OSError: pass
                        continue
                    # Stable-mtime check: skip if file was modified in last
                    # 0.3s (camera may still be writing).
                    try:
                        age = time.time() - os.path.getmtime(p)
                    except OSError:
                        continue
                    if age < 0.3:
                        continue
                    _seen.add(p)
                    threading.Thread(target=_maybe_send, args=(p,), daemon=True).start()
        except Exception as e:
            print(f"[FTP] watcher err: {e}", flush=True)
        time.sleep(0.5)


def _housekeeping():
    """Delete anything older than 60s -- catches leftover .mp4 clips and
    forgotten JPEGs so the drop folder doesn't grow forever."""
    while True:
        try:
            for root, _, files in os.walk(ROOT):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        if time.time() - os.path.getmtime(p) > 60:
                            os.remove(p)
                            _seen.discard(p)
                    except OSError:
                        pass
        except Exception as e:
            print(f"[FTP] housekeeping err: {e}", flush=True)
        time.sleep(30)


def main():
    auth = DummyAuthorizer()
    auth.add_user(FTP_USER, FTP_PASS, ROOT, perm="elradfmw")
    Handler.authorizer = auth
    Handler.banner = "reolink drop"
    srv = FTPServer(("0.0.0.0", FTP_PORT), Handler)
    print(f"📥 FTP listener on port {FTP_PORT} (user={FTP_USER}, root={ROOT})", flush=True)
    threading.Thread(target=_housekeeping, daemon=True).start()
    threading.Thread(target=_folder_watcher, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
