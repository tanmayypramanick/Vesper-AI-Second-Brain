#!/usr/bin/env python3
"""
Vesper Morning Briefing — sends a crafted daily message at 8 AM via WhatsApp
Covers: yesterday's activity, today's calendar, important emails, messages, news, weather
"""
import requests, urllib3, json, datetime, os, base64, time
urllib3.disable_warnings()

SERVER    = "https://127.0.0.1:5000"
WA_API    = "http://127.0.0.1:5001"   # OpenClaw HTTP API
SELF_JID_FILE = "/tmp/vesper_wa_self_jid.txt"

def ask(question):
    """Query Vesper ChromaDB directly for data."""
    body = json.dumps({"question": question, "voice": "af_heart", "history": []})
    ans = ""
    try:
        r = requests.post(f"{SERVER}/voice_fast", data=body,
            headers={"Content-Type": "application/json"},
            stream=True, verify=False, timeout=45)
        for line in r.iter_lines():
            if not line: continue
            line = line.decode()
            if not line.startswith("data: "): continue
            p = line[6:]
            if p == "END": break
            if p.startswith("T:"): ans += base64.b64decode(p[2:]).decode() + " "
    except Exception as e:
        ans = str(e)
    return ans.strip()

def get_weather():
    try:
        r = requests.get("https://wttr.in/?format=%C+%t+%h+humidity", timeout=5)
        return r.text.strip()
    except:
        return ""

def get_news():
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.news("India today top news", max_results=2, timelimit="d"):
                results.append(r.get("title",""))
            for r in ddgs.news("US world news today", max_results=1, timelimit="d"):
                results.append(r.get("title",""))
        return results
    except Exception as e:
        return [f"News unavailable: {e}"]

def send_wa(message):
    """Send message via OpenClaw HTTP API."""
    # Priority: config morning_jid > config owner_jid > self jid (fallback)
    jid = ''
    config_path = '/home/tanmay/vesper/openclaw/config.json'
    try:
        import json as _json
        cfg = _json.load(open(config_path))
        jid = cfg.get('morning_jid') or cfg.get('owner_jid') or ''
    except Exception:
        pass
    if not jid:
        if not os.path.exists(SELF_JID_FILE):
            print(f'[briefing] No WA JID found at {SELF_JID_FILE}')
            return False
        jid = open(SELF_JID_FILE).read().strip()
    if not jid:
        return False
    print(f'[briefing] Sending to JID: {jid}')
    try:
        r = requests.post(f"{WA_API}/send",
            json={"jid": jid, "message": message},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[briefing] WA send error: {e}")
        return False

def build_briefing():
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    day_name = today.strftime("%A, %B %-d")

    lines = []
    lines.append(f"☀️ Good morning, Tanmay! It's {day_name}.")
    lines.append("")

    # Weather
    weather = get_weather()
    if weather:
        lines.append(f"🌤️ Weather: {weather}")
        lines.append("")

    # Yesterday's work summary
    yesterday_str = yesterday.strftime("%B %-d")
    yesterday_q = f"What was I working on and doing on {yesterday_str}? Screen activity."
    yesterday_ans = ask(yesterday_q)
    if yesterday_ans and "no screen" not in yesterday_ans.lower():
        lines.append(f"📊 Yesterday's work:")
        lines.append(f"  {yesterday_ans}")
        lines.append("")

    # Today's calendar
    cal_ans = ask("What meetings or events do I have today on my calendar?")
    if cal_ans and "no " not in cal_ans.lower()[:20]:
        lines.append(f"📅 Today's schedule:")
        lines.append(f"  {cal_ans}")
        lines.append("")

    # Unread/recent emails
    email_ans = ask("What are my most recent important emails?")
    if email_ans and len(email_ans) > 20:
        lines.append(f"📧 Recent emails:")
        lines.append(f"  {email_ans[:300]}")
        lines.append("")

    # Important messages
    msg_ans = ask("Any important WhatsApp or iMessages from yesterday?")
    if msg_ans and len(msg_ans) > 20:
        lines.append(f"💬 Messages:")
        lines.append(f"  {msg_ans[:200]}")
        lines.append("")

    # News
    news = get_news()
    if news:
        lines.append(f"🗞️ News today:")
        for n in news[:3]:
            lines.append(f"  • {n}")
        lines.append("")

    lines.append("Have a great day! 🚀")

    return "\n".join(lines)

if __name__ == "__main__":
    print("[briefing] Building morning briefing...")
    msg = build_briefing()
    print("[briefing] Message:\n" + msg)
    sent = send_wa(msg)
    print(f"[briefing] WhatsApp send: {'✅ sent' if sent else '❌ failed (no WA connection?)'}")
    # Also log to file
    log_dir = "/home/tanmay/vesper/logs"
    os.makedirs(log_dir, exist_ok=True)
    with open(f"{log_dir}/morning_briefing.log", "a") as f:
        f.write(f"\n\n=== {datetime.datetime.now()} ===\n{msg}\n")
