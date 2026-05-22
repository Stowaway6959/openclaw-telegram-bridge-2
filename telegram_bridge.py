#!/usr/bin/env python3
import subprocess, time, json, os, threading
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from EventKit import EKEventStore, EKEntityTypeEvent, EKEntityTypeReminder
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
import anthropic

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
CHAT_ID          = os.getenv("TELEGRAM_CHAT_ID")
CAMERA_HOST      = os.getenv("CAMERA_HOST", "192.168.1.199")
CAMERA_USER      = os.getenv("CAMERA_USER", "admin")
CAMERA_PASSWORD  = os.getenv("CAMERA_PASSWORD")
CAMERA_URL       = f"http://{CAMERA_HOST}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"
CAMERA_IMAGE     = "/tmp/camera_snapshot.jpg"
CAMERA2_HOST     = os.getenv("CAMERA2_HOST", "192.168.1.200")
CAMERA2_URL      = f"http://{CAMERA2_HOST}/cgi-bin/api.cgi?cmd=Snap&channel=0&user={CAMERA_USER}&password={CAMERA_PASSWORD}"
CAMERA2_IMAGE    = "/tmp/camera2_snapshot.jpg"
WEATHER_API_KEY  = os.getenv("WEATHER_API_KEY")
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "65802")
GOLDAPI_KEY      = os.getenv("GOLDAPI_KEY")
NTFY_TOPIC       = os.getenv("NTFY_TOPIC", "openclaw-sar")
NOTES_FILE       = os.path.join(os.path.dirname(__file__), "notes.txt")

ai_client  = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
store      = EKEventStore.alloc().init()
last_update_id   = 0
bot_start_time   = time.time()
last_motion_time = [0]

def request_calendar_access():
    store.requestAccessToEntityType_completion_(EKEntityTypeEvent, lambda g, e: None)
    store.requestAccessToEntityType_completion_(EKEntityTypeReminder, lambda g, e: None)
    time.sleep(2)

def send_telegram(message, photo_path=None):
    try:
        if photo_path:
            subprocess.run(['curl', '-s', '-F', f'chat_id={CHAT_ID}', '-F', f'photo=@{photo_path}',
                            '-F', f'caption={message}',
                            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto'],
                           capture_output=True)
        else:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': message}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_ntfy(title, message, priority="default"):
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode(),
            headers={"Title": title, "Priority": priority}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy error: {e}")

def _arrow(chg):
    return f"↗️ +{chg:.2f}%" if chg > 0 else f"↘️ {chg:.2f}%"

def _yahoo_price_change(ticker):
    r = subprocess.run(["curl", "-s", "-H", "User-Agent: Mozilla/5.0",
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"],
                       capture_output=True, text=True, timeout=10)
    meta  = json.loads(r.stdout)['chart']['result'][0]['meta']
    price = meta['regularMarketPrice']
    prev  = meta.get('chartPreviousClose') or meta.get('previousClose', price)
    chg   = (price - prev) / prev * 100
    return price, chg

def get_markets():
    result = ""
    try:
        r = subprocess.run(["curl", "-s", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true"],
                           capture_output=True, text=True, timeout=10)
        d = json.loads(r.stdout)
        p, c = d['bitcoin']['usd'], d['bitcoin']['usd_24h_change']
        result += f"₿ Bitcoin: ${p:,.2f} {_arrow(c)}\n"
    except:
        result += "₿ Bitcoin: Unavailable\n"

    try:
        if GOLDAPI_KEY:
            req  = urllib.request.Request("https://www.goldapi.io/api/XAU/USD",
                                          headers={"x-access-token": GOLDAPI_KEY, "Content-Type": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            chg  = data.get('ch_percentage', 0)
            result += f"🥇 Gold: ${data['price']:,.2f}/oz {_arrow(chg)}\n"
        else:
            price, chg = _yahoo_price_change("GC=F")
            result += f"🥇 Gold: ${price:,.2f}/oz {_arrow(chg)}\n"
    except:
        result += "🥇 Gold: Unavailable\n"

    try:
        price, chg = _yahoo_price_change("SI=F")
        result += f"🥈 Silver: ${price:,.2f}/oz {_arrow(chg)}"
    except:
        result += "🥈 Silver: Unavailable"

    return result.strip()

def get_weather(location=None, days=1):
    if not location:
        location = DEFAULT_LOCATION
    try:
        url    = f"http://api.weatherapi.com/v1/forecast.json?key={WEATHER_API_KEY}&q={location}&days={days}"
        result = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
        data   = json.loads(result.stdout)
        current, loc = data['current'], data['location']
        if days == 1:
            forecast = data['forecast']['forecastday'][0]['day']
            return (f"🌤 {loc['name']}, {loc['region']}:\n\n"
                    f"Current: {current['temp_f']}°F, {current['condition']['text']}\n"
                    f"High: {forecast['maxtemp_f']}°F, Low: {forecast['mintemp_f']}°F\n"
                    f"Rain: {forecast['daily_chance_of_rain']}%")
        else:
            r = "🌤 7-Day Forecast:\n\n"
            for day in data['forecast']['forecastday']:
                ds  = datetime.strptime(day['date'], '%Y-%m-%d').strftime('%a %m/%d')
                dd  = day['day']
                r  += f"{ds}: {dd['maxtemp_f']}°/{dd['mintemp_f']}°F, {dd['condition']['text']}\n"
            return r
    except:
        return "Weather error"

def get_sun_times():
    try:
        url  = f"http://api.weatherapi.com/v1/astronomy.json?key={WEATHER_API_KEY}&q={DEFAULT_LOCATION}"
        r    = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
        data = json.loads(r.stdout)
        astro = data['astronomy']['astro']
        return f"🌅 Sunrise: {astro['sunrise']}  🌇 Sunset: {astro['sunset']}"
    except:
        return ""

def get_news():
    try:
        req  = urllib.request.Request("https://feeds.reuters.com/reuters/topNews",
                                      headers={"User-Agent": "Mozilla/5.0"})
        raw  = urllib.request.urlopen(req, timeout=10).read()
        root = ET.fromstring(raw)
        items = root.findall('./channel/item')[:5]
        result = "📰 Top News:\n\n"
        for item in items:
            title = item.findtext('title', '').strip()
            if title:
                result += f"• {title}\n"
        return result.strip()
    except:
        return ""

def get_upcoming_reminders(days=7):
    try:
        calendars = store.calendarsForEntityType_(EKEntityTypeReminder)
        predicate = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, calendars)
        all_rem   = []
        def cb(rem):
            if rem: all_rem.extend(rem)
        store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
        time.sleep(3)
        today    = date.today()
        cutoff   = today + timedelta(days=days)
        upcoming = []
        for r in all_rem:
            dc = r.dueDateComponents()
            if dc is None: continue
            try:
                rd = date(dc.year(), dc.month(), dc.day())
                if today <= rd <= cutoff:
                    upcoming.append((rd, r.title()))
            except:
                continue
        upcoming.sort()
        if not upcoming:
            return ""
        result = "⏰ Coming up:\n\n"
        for rd, title in upcoming:
            delta = (rd - today).days
            label = "today" if delta == 0 else f"in {delta}d"
            result += f"• {title} ({label})\n"
        return result.strip()
    except:
        return ""

def get_public_ip():
    try:
        r = subprocess.run(["curl", "-s", "https://api.ipify.org"], capture_output=True, text=True, timeout=10)
        return f"🌐 Public IP: {r.stdout.strip()}"
    except:
        return "IP unavailable"

def get_status():
    uptime_secs = int(time.time() - bot_start_time)
    h, m        = divmod(uptime_secs // 60, 60)
    uptime_str  = f"{h}h {m}m" if h else f"{m}m"
    if last_motion_time[0]:
        ago = int((time.time() - last_motion_time[0]) / 60)
        motion_str = f"{ago}m ago"
    else:
        motion_str = "none"
    try:
        url    = f"http://api.weatherapi.com/v1/current.json?key={WEATHER_API_KEY}&q={DEFAULT_LOCATION}"
        r      = subprocess.run(["curl", "-s", url], capture_output=True, text=True)
        data   = json.loads(r.stdout)
        w      = f"{data['current']['temp_f']}°F, {data['current']['condition']['text']}"
    except:
        w = "unavailable"
    return f"🤖 Bridge 2 AIR\n\nUptime: {uptime_str}\nLast motion: {motion_str}\nWeather: {w}"

def save_note(text):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(NOTES_FILE, "a") as f:
        f.write(f"[{ts}] {text}\n")
    return f"✅ Note saved"

def get_notes():
    if not os.path.exists(NOTES_FILE):
        return "📝 No notes yet"
    with open(NOTES_FILE) as f:
        lines = f.readlines()
    if not lines:
        return "📝 No notes yet"
    result = f"📝 Notes ({len(lines)}):\n\n"
    for line in lines[-10:]:
        result += line
    return result.strip()

def get_reminders_today():
    calendars  = store.calendarsForEntityType_(EKEntityTypeReminder)
    predicate  = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, calendars)
    all_rem    = []
    def cb(rem):
        if rem:
            all_rem.extend(rem)
    store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
    time.sleep(3)
    today     = date.today()
    today_rem = []
    for r in all_rem:
        dc = r.dueDateComponents()
        if dc is None:
            continue
        try:
            if date(dc.year(), dc.month(), dc.day()) == today:
                today_rem.append(r)
        except:
            continue
    if not today_rem:
        return "✅ No reminders due today"
    result = f"✅ Today's reminders ({len(today_rem)}):\n\n"
    for r in today_rem:
        result += f"• {r.title()}\n"
    return result.strip()

def get_reminders_tomorrow():
    calendars  = store.calendarsForEntityType_(EKEntityTypeReminder)
    predicate  = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(None, None, calendars)
    all_rem    = []
    def cb(rem):
        if rem:
            all_rem.extend(rem)
    store.fetchRemindersMatchingPredicate_completion_(predicate, cb)
    time.sleep(3)
    tomorrow  = date.today() + timedelta(days=1)
    tmrw_rem  = []
    for r in all_rem:
        dc = r.dueDateComponents()
        if dc is None:
            continue
        try:
            if date(dc.year(), dc.month(), dc.day()) == tomorrow:
                tmrw_rem.append(r)
        except:
            continue
    if not tmrw_rem:
        return "✅ No reminders due tomorrow"
    result = f"✅ Tomorrow's reminders ({len(tmrw_rem)}):\n\n"
    for r in tmrw_rem:
        result += f"• {r.title()}\n"
    return result.strip()

def get_calendar_today():
    now   = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    events = store.eventsMatchingPredicate_(
        store.predicateForEventsWithStartDate_endDate_calendars_(start, end, None))
    if not events:
        return "📅 No events today"
    result = f"📅 Today ({len(events)} events):\n\n"
    for e in events:
        dt = datetime.fromtimestamp(e.startDate().timeIntervalSince1970())
        result += f"• {dt.strftime('%I:%M %p')} - {e.title()}\n"
    return result.strip()

def capture_camera():
    try:
        subprocess.run(["curl", "-u", f"{CAMERA_USER}:{CAMERA_PASSWORD}", CAMERA_URL, "-o", CAMERA_IMAGE],
                       capture_output=True)
        if os.path.exists(CAMERA_IMAGE) and os.path.getsize(CAMERA_IMAGE) > 10000:
            return CAMERA_IMAGE
        return None
    except:
        return None

def capture_camera2():
    try:
        subprocess.run(["curl", "-u", f"{CAMERA_USER}:{CAMERA_PASSWORD}", CAMERA2_URL, "-o", CAMERA2_IMAGE],
                       capture_output=True)
        if os.path.exists(CAMERA2_IMAGE) and os.path.getsize(CAMERA2_IMAGE) > 10000:
            return CAMERA2_IMAGE
        return None
    except:
        return None

def ask_ai(question):
    try:
        response = ai_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text
    except Exception as e:
        return f"AI error: {e}"

def get_briefing():
    m    = get_markets()
    w    = get_weather(DEFAULT_LOCATION, 1)
    sun  = get_sun_times()
    e    = get_calendar_today()
    r    = get_reminders_today()
    up   = get_upcoming_reminders(7)
    news = get_news()
    parts = [f"☀️ Good Morning!\n\n{m}\n\n{w}"]
    if sun:  parts.append(sun)
    parts.append(e)
    parts.append(r)
    if up:   parts.append(up)
    if news: parts.append(news)
    return "\n\n".join(parts)

def get_evening_briefing():
    m  = get_markets()
    w  = get_weather(DEFAULT_LOCATION, 2)
    r  = get_reminders_tomorrow()
    up = get_upcoming_reminders(7)
    parts = [f"🌙 Good Evening!\n\n{m}\n\n{w}\n\n{r}"]
    if up: parts.append(up)
    return "\n\n".join(parts)

def send_auto_briefing(btype):
    if btype == "morning":
        request_calendar_access()
        msg = get_briefing()
        send_telegram(msg)
        send_ntfy("Morning Briefing", msg[:200], "default")
        print("Morning briefing sent!")
    elif btype == "evening":
        request_calendar_access()
        msg = get_evening_briefing()
        send_telegram(msg)
        send_ntfy("Evening Briefing", msg[:200], "default")
        print("Evening briefing sent!")
    elif btype == "hourly_camera":
        img = capture_camera()
        if img:
            ts = datetime.now().strftime("%I:%M %p")
            send_telegram(f"📷 {ts}", img)
            send_ntfy("Camera Snapshot", f"Snapshot taken at {ts}", "low")
            print("Camera sent!")
    elif btype == "market_open":
        send_telegram(f"🔔 Market Open\n\n{get_markets()}")
        print("Market open sent!")
    elif btype == "market_close":
        send_telegram(f"🔔 Market Close\n\n{get_markets()}")
        print("Market close sent!")

def scheduler():
    TZ                = ZoneInfo("America/Chicago")
    last_morning_sent = None
    last_evening_sent = None
    while True:
        now   = datetime.now(TZ)
        today = now.date()
        if now.hour == 6 and now.minute == 0 and last_morning_sent != today:
            send_auto_briefing("morning")
            last_morning_sent = today
        if now.hour == 18 and now.minute == 0 and last_evening_sent != today:
            send_auto_briefing("evening")
            last_evening_sent = today
        time.sleep(30)

def listen_for_telegram():
    global last_update_id
    print("🤖 Telegram bridge #2 running (Claude AI)")
    threading.Thread(target=scheduler, daemon=True).start()
    print("⏰ Scheduler active: 6:30am morning, 9:30am market open, 4pm market close, 6pm evening")
    request_calendar_access()
    while True:
        try:
            url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=30"
            data = json.loads(urllib.request.urlopen(urllib.request.Request(url), timeout=35).read())
            if data['ok'] and data['result']:
                for update in data['result']:
                    last_update_id = update['update_id']
                    if 'message' in update and 'text' in update['message']:
                        msg = update['message']['text']
                        print(f"📨 {msg}")
                        cmd = msg.lower().strip()
                        if cmd in ['camera', 'snapshot', 'cam']:
                            img1 = capture_camera()
                            img2 = capture_camera2()
                            if img1: send_telegram("📷 Front", img1)
                            if img2: send_telegram("📷 Back", img2)
                            print("✅ Both cameras")
                        elif cmd == 'front':
                            img = capture_camera()
                            if img:
                                send_telegram("📷 Front", img)
                                print("✅ Front camera")
                        elif cmd == 'back':
                            img = capture_camera2()
                            if img:
                                send_telegram("📷 Back", img)
                                print("✅ Back camera")
                        elif cmd in ['markets', 'prices', 'btc']:
                            send_telegram(get_markets())
                            print("✅ Markets")
                        elif cmd in ['weather', 'today weather']:
                            send_telegram(get_weather(DEFAULT_LOCATION, 1))
                            print("✅ Weather")
                        elif cmd in ['week weather']:
                            send_telegram(get_weather(DEFAULT_LOCATION, 7))
                            print("✅ Week weather")
                        elif cmd.startswith('weather '):
                            loc = msg[8:].strip()
                            send_telegram(get_weather(loc, 1))
                            print(f"✅ Weather {loc}")
                        elif cmd in ['calendar', 'today calendar']:
                            send_telegram(get_calendar_today())
                            print("✅ Calendar")
                        elif cmd in ['reminders', 'today reminders']:
                            send_telegram(get_reminders_today())
                            print("✅ Reminders")
                        elif cmd in ['briefing', 'daily']:
                            send_telegram(get_briefing())
                            print("✅ Briefing")
                        elif cmd == 'status':
                            send_telegram(get_status())
                            print("✅ Status")
                        elif cmd == 'ip':
                            send_telegram(get_public_ip())
                            print("✅ IP")
                        elif cmd == 'news':
                            n = get_news()
                            send_telegram(n if n else "No news available")
                            print("✅ News")
                        elif cmd == 'notes':
                            send_telegram(get_notes())
                            print("✅ Notes")
                        elif cmd.startswith('note '):
                            send_telegram(save_note(msg[5:].strip()))
                            print("✅ Note saved")
                        else:
                            print("🤖 Asking Claude...")
                            send_telegram(ask_ai(msg))
                            print("✅ AI sent")
            time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        send_auto_briefing(sys.argv[1])
    else:
        listen_for_telegram()
