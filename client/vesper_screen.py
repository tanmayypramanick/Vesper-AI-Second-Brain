#!/usr/bin/env python3 -u
"""
VESPER SCREEN — Real-time screen + audio awareness (Omi-style)

Usage:
  python3 ~/vesper_agent/vesper_screen.py                    # interactive
  python3 ~/vesper_agent/vesper_screen.py "what's this about?"  # one-shot
  python3 ~/vesper_agent/vesper_screen.py --watch             # live OCR stream

Context sources:
  1. vesper_capture.py /recent_screen (primary — our Omi-style daemon)
  2. screenpipe localhost:3030 (fallback — only if running in meeting mode)

Sends context directly to /ask (bypasses embedding search for fresh screen data).
"""

import sys, re, requests, time, subprocess
from datetime import datetime, timedelta, timezone

SCREENPIPE  = "http://localhost:3030"
VESPER_LAN  = "http://10.0.0.120:5000"    # LAN primary
VESPER_TS   = "http://100.123.15.32:5000" # Tailscale fallback
FAST_MODEL  = "phi4-mini-cpu"
SLOW_MODEL  = "phi4-mini-cpu"   # qwen3 too slow on CPU (5GB, 2+ min); phi4-mini-cpu @ 12-20s

SKIP_APPS = {"Notification Center", "Dock", "WindowServer", "Control Centre"}

# ── resolve which Vesper URL is reachable ─────────────────────────────────────

def _vesper_url():
    for url in [VESPER_LAN, VESPER_TS]:
        try:
            r = requests.get(f"{url}/health", timeout=3)
            if r.status_code == 200:
                return url
        except:
            continue
    return None

VESPER = _vesper_url()

# ── context from vesper_capture /recent_screen (primary) ────────────────────

_MENU_NOISE = {
    "File","Edit","View","Go","Run","Terminal","Window","Help","Selection",
    "History","Bookmarks","Profiles","Tab","Format","Insert","Tools","Navigate",
    "Debug","Code","Search","App","Background","Activity","Storage","Screen",
}

# Patterns that indicate sidebar/chrome noise, not real content
_NOISE_RE = re.compile(
    r'^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s|'          # day-of-week prefix
    r'^\d{1,2}:\d{2}|'                              # bare time (19:47)
    r'^[IVl><+*v•]\s|'                              # file-tree arrows / bullets
    r'\.(json|py|md|vcf|sh|log|txt|zip|plist|png|jpg)\b|'  # filenames
    r'^<[a-zA-Z]|'                                  # XML/HTML tags (<task-noti…)
    r'^Home\s*[-–]\s*\w|'                           # browser "Home - Netflix/YouTube" tab
    r'^NVIDIA\s|'                                   # GPU status bar text
    r'^\d+\.\s*\d+\s*t/s|'                         # benchmark "10.4 t/s"
    r'^Prompt:\s*\d|'                               # "Prompt: 0.78s"
    r'^CPU:\s*\d|'                                  # "CPU: 10.4 t/s"
    r'^(EXPLORER|Customize|Recents|Favorites|Locations|Projects|Artifacts|'
    r'Newchat|Cowork|OPEN EDITORS|OUTLINE|TIMELINE|Accessibility|'
    r'Software Update|Available)$',
    re.IGNORECASE,
)
# App-name aliases for cleaner display
_APP_ALIAS = {
    "app_mode_loader": "Claude Code",
    "Electron":        "Claude Code",
}

def _clean_line(line):
    """Return True if this line has meaningful real content (not sidebar/chrome noise)."""
    s = line.strip()
    if len(s) < 10:                      # too short to be meaningful
        return False
    if _NOISE_RE.search(s):              # sidebar / chrome / benchmark noise
        return False
    words = s.split()
    if all(w in _MENU_NOISE or w.isdigit() or len(w) == 1 for w in words):
        return False
    # Need at least 35% alphabetic characters (filters symbol/number-heavy lines)
    alpha = sum(c.isalpha() for c in s)
    if alpha / len(s) < 0.35:
        return False
    return True

def get_vesper_screen_context(n=4):
    """Pull last N screen captures — return clean, content-dense context for LLM."""
    if not VESPER:
        return ""
    try:
        r = requests.get(f"{VESPER}/recent_screen", params={"n": n}, timeout=5)
        items = r.json().get("items", [])
        blocks = []
        seen   = set()
        now_ts = datetime.now().strftime("%Y-%m-%dT%H:%M")
        for item in items:
            raw_src = item.get("source", "screen").replace("screen:", "")
            src     = _APP_ALIAS.get(raw_src, raw_src)
            ts      = item.get("ts", "")[:16]
            raw     = item.get("text", "")
            # Skip captures older than 15 minutes (stale context)
            try:
                age_min = (datetime.strptime(now_ts, "%Y-%m-%dT%H:%M") -
                           datetime.strptime(ts,     "%Y-%m-%dT%H:%M")).seconds // 60
                if age_min > 15:
                    continue
            except Exception:
                pass
            # Filter meaningful lines (strip OCR header)
            lines = [
                l.strip() for l in raw.split("\n")
                if _clean_line(l) and not l.strip().startswith("[Screen")
            ]
            # Deduplicate across captures
            deduped = [l for l in lines if l not in seen and not seen.add(l)]
            if deduped:
                # Sort by length descending (longer = more content-dense)
                deduped.sort(key=len, reverse=True)
                content = " | ".join(deduped[:5])
                blocks.append(f"[{src} @ {ts}] {content[:350]}")
        return "\n".join(blocks)
    except Exception:
        return ""

# ── context from screenpipe (fallback, only in meeting mode) ─────────────────

def sp_health():
    try:
        d = requests.get(f"{SCREENPIPE}/health", timeout=2).json()
        return d.get("frame_status") == "ok"
    except:
        return False

def get_sp_context(minutes=5, limit=40):
    """Get OCR + audio context from screenpipe (only when in meeting mode)."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(minutes=minutes)
    try:
        r = requests.get(f"{SCREENPIPE}/search", params={
            "start_time":   start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_time":     now.strftime("%Y-%m-%dT%H:%M:%S"),
            "content_type": "all",
            "limit":        limit,
        }, timeout=8)
        items = r.json().get("data", [])
    except:
        return ""

    lines = []
    seen  = set()
    for item in items:
        c    = item.get("content", {})
        kind = item.get("type", "")
        if kind == "OCR":
            app  = c.get("app_name", "")
            if app in SKIP_APPS:
                continue
            text = c.get("text", "").strip()
            if not text or len(text) < 15:
                continue
            key = text[:50]
            if key in seen:
                continue
            seen.add(key)
            win = c.get("window_name", "")
            lines.append(f"[{app}] {win[:30]}: {text[:300]}")
        elif kind == "AUDIO":
            t = c.get("transcription", "").strip()
            if not t or len(t) < 10:
                continue
            dev = c.get("device_name", "")
            src = "🎙 Mic" if "micro" in dev.lower() else "🔊 System"
            lines.append(f"[Audio/{src}] {t[:500]}")

    return "\n".join(lines[-30:])

def get_context():
    """Get the best available screen context."""
    # Primary: our vesper_capture OCR data
    ctx = get_vesper_screen_context(n=4)
    if ctx:
        return ctx
    # Fallback: screenpipe (only active in meeting mode)
    if sp_health():
        return get_sp_context()
    return ""

# ── inference via Vesper /ask endpoint ───────────────────────────────────────

_HEAVY_WORDS  = {"summarize","explain","analyze","translate","list all","tell me everything","what happened"}
# Present-tense "what am I doing NOW" — bypass ChromaDB, use screen context only
_PRESENT_TENSE = re.compile(
    r"\b(right now|currently|at the moment|on my screen"
    r"|what (am|are) (i|you) (doing|working on|looking at|reading|watching|using)"
    r"|what (app|application)|what('s| is) (on|open)|what do you see)\b",
    re.IGNORECASE,
)

def is_heavy(question):
    q = question.lower()
    return any(w in q for w in _HEAVY_WORDS) or len(question.split()) > 10

def is_screen_only(question):
    """True when question is about the current moment — skip ChromaDB, use live OCR only."""
    return bool(_PRESENT_TENSE.search(question))

def ask(question, ctx="", model=None):
    if not model:
        model = FAST_MODEL
    heavy      = is_heavy(question)
    screen_now = is_screen_only(question) and bool(ctx)  # bypass memories if we have live OCR
    max_tokens = 150 if heavy else 80   # heavy=~35s, quick=~18s on phi4-mini-cpu
    n_memories = 0 if screen_now else 5  # 0 = no ChromaDB lookup for "right now" questions
    if not VESPER:
        return "⚠️  Vesper server unreachable", model
    try:
        r = requests.post(f"{VESPER}/ask", json={
            "question":   question,
            "context":    ctx,
            "model":      model,
            "n":          n_memories,
            "max_tokens": max_tokens,
        }, timeout=120)  # phi4-mini-cpu: ~18-35s depending on token budget
        d = r.json()
        mem = d.get("memories_used", 0)
        ans = d.get("answer") or d.get("error", "No answer")
        tag = "live screen" if screen_now else f"{mem} memories"
        return f"{ans}  [{tag}]", model
    except Exception as e:
        return f"Error: {e}", model

# ── live watch mode ───────────────────────────────────────────────────────────

def watch_mode():
    """Stream screen captures as they arrive — Omi-style live feed."""
    print("🔴 VESPER LIVE  (Ctrl+C to stop)\n")
    seen_ids = set()

    while True:
        try:
            ctx = get_vesper_screen_context(n=3)
            if ctx:
                for line in ctx.split("\n"):
                    if line not in seen_ids:
                        seen_ids.add(line)
                        print(line)
            # Also show screenpipe audio if in meeting
            if sp_health():
                sp = get_sp_context(minutes=1, limit=10)
                for line in sp.split("\n"):
                    if "Audio" in line and line not in seen_ids:
                        seen_ids.add(line)
                        print(line)
            time.sleep(3)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception:
            time.sleep(5)

# ── volume (bypass Multi-Output Device limitation) ────────────────────────────

def _run_script(*lines):
    script = "\n".join(lines)
    subprocess.run(["osascript", "-e", script], capture_output=True)

def vol_up():
    _run_script("set v to output volume of (get volume settings)",
                "set volume output volume (v + 10)")
    print("🔊 Volume up")

def vol_down():
    _run_script("set v to output volume of (get volume settings)",
                "set volume output volume (v - 10)")
    print("🔉 Volume down")

def set_vol(n):
    _run_script(f"set volume output volume {n}")
    print(f"🔊 Volume: {n}%")

# ── interactive ───────────────────────────────────────────────────────────────

def interactive():
    sp_ok = sp_health()
    v_ok  = VESPER is not None
    ctx   = get_vesper_screen_context(n=1)

    print("🧠 VESPER SCREEN")
    print(f"   Screen context  : {'✅ vesper_capture active' if ctx else '⚪ no recent captures'}")
    print(f"   Screenpipe audio: {'✅ meeting mode' if sp_ok else '⚪ idle (activates in meetings)'}")
    print(f"   Vesper server   : {'✅ ' + VESPER if v_ok else '❌ unreachable'}")
    print("   Commands: /watch  /vol+  /vol-  /vol50  /ctx  /quit\n")

    while True:
        try:
            q = input("Ask: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not q:
            continue
        if q == "/quit":
            break
        if q == "/watch":
            watch_mode()
            continue
        if q == "/ctx":
            ctx = get_context()
            print(f"\n--- Screen context ({len(ctx)} chars) ---")
            print(ctx[:800] or "(none)")
            print("---\n")
            continue
        if q == "/vol+":
            vol_up(); continue
        if q == "/vol-":
            vol_down(); continue
        if q.startswith("/vol"):
            try:
                set_vol(int(q.replace("/vol", "").strip()))
            except Exception:
                print("Usage: /vol50")
            continue

        print("…", end=" ", flush=True)
        ctx      = get_context()
        ans, mdl = ask(q, ctx)
        print(f"\r[{mdl}] {ans}\n")

# ── main ─────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        interactive()
    elif args[0] == "--watch":
        watch_mode()
    elif args[0] == "--ctx":
        print(get_context() or "(no screen context)")
    else:
        question = " ".join(args)
        ctx      = get_context()
        ans, _   = ask(question, ctx)
        print(ans, flush=True)

if __name__ == "__main__":
    main()
