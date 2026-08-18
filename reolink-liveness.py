#!/usr/bin/env python3
"""Reolink bridge LIVENESS watchdog.

The gap this fills: launchd KeepAlive restarts smtp_listener only when the
PROCESS EXITS. On 2026-08-17 a network flap (router reboot + SSID rename) left
the process alive and bound to :2525 but its accept loop dead -- the port
showed LISTEN, the job looked healthy, yet a test SMTP got "connection
unexpectedly closed" and no alert fired for 10 hours.

This checks the listener the way a camera would: open SMTP, read the greeting,
HELO, QUIT. If that fails twice in a row, kickstart both reolink jobs and
Telegram-notify that the bridge self-healed. No email is delivered, so this
never spams the channel.

Run every 3 min via com.reolink.liveness-watchdog.
"""
import os, sys, time, smtplib, subprocess
from datetime import datetime

HERE = "/Users/dc/Desktop/APPS/reolink-telegram-bridge-air2"
# launchd cannot read .env under ~/Desktop (macOS TCC). Secrets come from the
# plist EnvironmentVariables; .env is only a fallback for an interactive run and
# must never crash the healer, whose core job (detect wedge + restart) needs no
# secret at all. See memory launchd-desktop-tcc.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass
TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = 2525
JOBS = ["com.reolink.smtp-air2", "com.reolink.bridge-air2"]


def mac_ip():
    for iface in ("en0", "en1", "en5"):
        r = subprocess.run(["ipconfig", "getifaddr", iface],
                           capture_output=True, text=True)
        ip = r.stdout.strip()
        if ip:
            return ip
    return "127.0.0.1"


def listener_ok(host, timeout=8, port=PORT):
    """True iff the listener completes an SMTP greeting + HELO. That greeting
    read is exactly the step that failed on the 2026-08-17 wedge. No DATA is
    sent, so nothing is delivered to Telegram."""
    try:
        s = smtplib.SMTP(host, port, timeout=timeout)  # connect + read greeting
        s.helo("healthcheck")                          # expects 250
        s.quit()
        return True
    except Exception:
        return False


def kickstart():
    uid = os.getuid()
    for j in JOBS:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{j}"],
                       capture_output=True)


def notify(text):
    if not TOKEN or not CHAT_ID:
        return
    subprocess.run([
        "curl", "-4", "-s", "--max-time", "20",
        "-F", f"chat_id={CHAT_ID}", "-F", f"text={text}",
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    ], capture_output=True)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main():
    host = mac_ip()
    # Two strikes 5s apart, so a single transient blip never triggers a restart.
    if listener_ok(host) or (time.sleep(5) or listener_ok(host)):
        log(f"ok  listener alive on {host}:{PORT}")
        return
    log(f"DOWN  listener wedged on {host}:{PORT} -- kickstarting {JOBS}")
    kickstart()
    time.sleep(6)
    healed = listener_ok(host)
    log(f"restart {'succeeded' if healed else 'FAILED'}")
    notify(("\U0001F527 Camera bridge self-healed" if healed
            else "⚠️ Camera bridge DOWN, restart failed")
           + f" at {datetime.now():%H:%M} ({host}:{PORT})")


def demo():
    # self-check: an unused high port must read as DOWN, the live listener UP.
    assert listener_ok("127.0.0.1", timeout=1, port=59999) is False
    assert listener_ok(mac_ip(), timeout=5) is True  # real listener must answer
    print("demo: dead port -> DOWN, live listener -> UP -- OK")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        main()
