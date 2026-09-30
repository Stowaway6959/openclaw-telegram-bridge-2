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
import os, sys, time, smtplib, subprocess, socket, json
import urllib.request
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
# Off-box deadman (healthchecks.io). The Telegram notify above cannot report that
# THIS Mac died, because it runs on the Mac it is watching. Set HC_REOLINK in .env
# to a ping URL; unset means the ping is skipped entirely.
HC_URL  = os.environ.get("HC_REOLINK", "")
PORT = 2525
JOBS = ["com.reolink.smtp-air2", "com.reolink.bridge-air2"]
# Cameras push TO this Mac, so a live listener is not the same as a working
# pipeline: if this Mac's IP moves and sync-cam-smtp-ip has not caught up, the
# cameras keep posting to the old address. Zero alerts arrive while the listener
# check says healthy. Verified 2026-09-30, when macOS Private Wi-Fi Address
# rotated the MAC and the IP went .107 -> .64.
CAMS      = os.environ.get("CAMS", "192.168.1.199 192.168.1.200").split()
CAM_USER  = os.environ.get("CAMERA_USER", "")
CAM_PASS  = os.environ.get("CAMERA_PASSWORD", "")
STRAY_FLAG = "/tmp/reolink-misdirected.flag"


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


def _cam_api(cam, path, payload=None, timeout=6):
    req = urllib.request.Request(
        f"http://{cam}/cgi-bin/api.cgi?{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def camera_targets():
    """{cam_ip: smtpServer} for cameras whose email push is ENABLED.

    Returns None when no camera could be read at all, so the caller can tell
    "nothing is misdirected" apart from "I could not check". An unreadable
    camera is UNKNOWN, never a failure: flipping the deadman red on a transient
    API blip would train the owner to ignore it.

    Cameras with enable=0 are skipped deliberately -- .200 is switched off on
    purpose (owner wants alerts from the one front camera), so its smtpServer
    is irrelevant and must never raise an alarm.
    """
    if not (CAM_USER and CAM_PASS):
        return None
    out, reachable = {}, False
    for cam in CAMS:
        try:
            tok = _cam_api(cam, "cmd=Login", [{"cmd": "Login", "param": {"User": {
                "Version": "0", "userName": CAM_USER, "password": CAM_PASS}}}]
            )[0]["value"]["Token"]["name"]
            em = _cam_api(cam, f"cmd=GetEmailV20&token={tok}")[0]["value"]["Email"]
            reachable = True
            if str(em.get("enable")) == "1":
                out[cam] = em.get("smtpServer")
        except Exception:
            continue
    return out if reachable else None


def misdirected(host):
    """Enabled cameras pointed somewhere other than this Mac. {} means fine."""
    targets = camera_targets()
    if targets is None:
        log("camera target check unavailable (no creds or unreachable) -- not a failure")
        return {}
    return {c: ip for c, ip in targets.items() if ip != host}


def kickstart():
    uid = os.getuid()
    for j in JOBS:
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{j}"],
                       capture_output=True)


def notify(text):
    if not TOKEN or not CHAT_ID:
        return
    # Tag identifies which Mac self-healed; both Macs share one bot token + chat.
    text += f" · [{socket.gethostname().split('.')[0]}]"
    subprocess.run([
        "curl", "-4", "-s", "--max-time", "20",
        "-F", f"chat_id={CHAT_ID}", "-F", f"text={text}",
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
    ], capture_output=True)


def hc_ping():
    """Tell healthchecks.io the listener is alive. Deliberately pinged ONLY on the
    healthy path: if the listener is wedged and cannot be healed, the ping stops,
    the check goes red after its grace period, and the owner hears about it
    off-box. Must never change this watchdog's own outcome, hence the blanket
    except -- a DNS failure or dead uplink is not a reason to skip a kickstart."""
    if not HC_URL:
        return
    try:
        subprocess.run(["curl", "-fsS", "--max-time", "10", "-o", "/dev/null", HC_URL],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def uptime_seconds():
    """Seconds since boot, via sysctl kern.boottime. -1 if unreadable."""
    try:
        import time as _t
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True).stdout
        boot = int(out.split("sec =")[1].split(",")[0].strip())
        return _t.time() - boot
    except Exception:
        return -1


def main():
    host = mac_ip()
    # Boot grace: right after a reboot the smtp job may still be starting under
    # launchd. Skip one cycle so we never "heal" a job that was merely slow and
    # never fire a false self-heal ping on every boot. Next tick (180s) covers it.
    up = uptime_seconds()
    if 0 <= up < 90:
        log(f"boot grace ({up:.0f}s uptime) -- skip this tick")
        return
    # Two strikes 5s apart, so a single transient blip never triggers a restart.
    if listener_ok(host) or (time.sleep(5) or listener_ok(host)):
        log(f"ok  listener alive on {host}:{PORT}")
        # A live listener is NOT a working pipeline. This is the one outage
        # shape the deadman could not previously see: IP moves, cameras keep
        # posting to the old address, no alerts arrive, check stays green.
        stray = misdirected(host)
        if stray:
            detail = ", ".join(f"{c} -> {ip}" for c, ip in sorted(stray.items()))
            log(f"MISDIRECTED  {detail}  (this Mac is {host}) -- withholding ping")
            # Withholding the ping is the real alarm: healthchecks goes red after
            # its grace. Telegram fires ONCE per episode so a 3-minute tick does
            # not turn a real outage into noise the owner learns to swipe away.
            if not os.path.exists(STRAY_FLAG):
                try:
                    open(STRAY_FLAG, "w").close()
                except Exception:
                    pass
                notify(f"⚠️ Cameras posting to the wrong address: {detail}. "
                       f"This Mac is {host}. Alerts are being LOST. "
                       f"sync-cam-smtp-ip should correct it within 30 min.")
            return
        if os.path.exists(STRAY_FLAG):
            try:
                os.remove(STRAY_FLAG)
            except Exception:
                pass
            notify(f"✅ Cameras are posting to {host} again. Alerts restored.")
        hc_ping()
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


def selftest():
    """Pure-logic checks for the misdirection detector. No network, no live
    listener, so this runs anywhere -- including a machine that has been stood
    down. Guards the three ways this check could do harm: a false alarm when
    cameras are unreadable, an alarm on a deliberately disabled camera, and a
    missed alarm when the IP really has moved."""
    global camera_targets, _cam_api
    orig_targets, orig_api = camera_targets, _cam_api

    # 1. unreadable cameras must NOT be reported as misdirected
    camera_targets = lambda: None
    assert misdirected("192.168.1.64") == {}, "unreadable must not alarm"

    # 2. pointed at us -> clean
    camera_targets = lambda: {"192.168.1.199": "192.168.1.64"}
    assert misdirected("192.168.1.64") == {}, "correct target must not alarm"

    # 3. pointed at a stale address -> detected
    camera_targets = lambda: {"192.168.1.199": "192.168.1.107"}
    assert misdirected("192.168.1.64") == {"192.168.1.199": "192.168.1.107"}, \
        "stale target must be detected"

    # 4. a DISABLED camera must be excluded entirely, even if misdirected.
    #    .200 is off on purpose; alarming on it would be a permanent false red.
    camera_targets = orig_targets
    def fake_api(cam, path, payload=None, timeout=6):
        if "Login" in path:
            return [{"value": {"Token": {"name": "t"}}}]
        enabled = "1" if cam.endswith(".199") else "0"
        return [{"value": {"Email": {"enable": enabled,
                                     "smtpServer": "192.168.1.107"}}}]
    _cam_api = fake_api
    os.environ["CAMERA_USER"], os.environ["CAMERA_PASSWORD"] = "u", "p"
    globals()["CAM_USER"], globals()["CAM_PASS"] = "u", "p"
    got = camera_targets()
    assert got == {"192.168.1.199": "192.168.1.107"}, f"enable filter wrong: {got}"

    camera_targets, _cam_api = orig_targets, orig_api
    print("selftest: unreadable->quiet, match->quiet, stale->detected, "
          "disabled-cam->ignored -- OK")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    elif "--selftest" in sys.argv:
        selftest()
    else:
        main()
