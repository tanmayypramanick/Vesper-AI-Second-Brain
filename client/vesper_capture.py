#!/usr/bin/env python3
"""
VESPER CAPTURE — Omi-style lightweight screen awareness

Architecture:
  1. Screenshot every 3s (AC) / 15s (battery) via screencapture
  2. dHash perceptual hash — skip if screen unchanged (threshold=5, matches Omi)
  3. Apple Vision OCR — local, ~100-200ms, Apple Neural Engine (near-zero battery)
  4. Filter macOS menu bar noise
  5. Send TEXT only (~2KB) to server /store_ocr → embed → store in ChromaDB
  6. Multi-monitor: ALL connected displays captured and OCR'd
  7. On battery: slower interval (15s) but STILL stores — 2KB/15s is negligible
  8. Meeting auto-toggle: enable mic + BlackHole audio when meeting detected

Usage:
  python3 ~/vesper_agent/vesper_capture.py           # daemon mode
  python3 ~/vesper_agent/vesper_capture.py --once    # single capture + exit
  python3 ~/vesper_agent/vesper_capture.py --status  # stats
  python3 ~/vesper_agent/vesper_capture.py --test    # OCR current screen + print
"""

import sys, os, time, subprocess, json, re
from datetime import datetime
from pathlib import Path
from PIL import Image
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Config ───────────────────────────────────────────────────────────────────
VESPER_LAN   = "https://10.0.0.120:5000"
VESPER_TS    = "https://100.123.15.32:5000"
INTERVAL_AC  = 2      # seconds on AC power (poll loop)
INTERVAL_BAT = 10     # seconds on battery
DHASH_SIZE   = 8      # 64-bit hash
DHASH_THRESH = 3      # Lower threshold: catch text changes (was 5)
ACTIVITY_COOLDOWN = 4 # seconds between activity-forced captures
LOG_FILE     = Path.home() / "vesper_agent/logs/capture.log"
STATE_FILE   = Path("/tmp/vesper_capture_state.json")

# Up to 4 displays — screencapture only writes files for real displays
DISPLAY_PATHS  = ['/tmp/vesper_d1.png', '/tmp/vesper_d2.png',
                  '/tmp/vesper_d3.png', '/tmp/vesper_d4.png']
DISPLAY_LABELS = ['main', 'ext', 'ext2', 'ext3']

MEETING_APPS = ["zoom.us", "Zoom", "Microsoft Teams", "Webex", "Webex Meetings", "Loom"]
SCREENCAPTURE = "/usr/sbin/screencapture"
OSASCRIPT     = "/usr/bin/osascript"
PGREP         = "/usr/bin/pgrep"
PMSET         = "/usr/bin/pmset"
SKIP_APPS     = {"1Password", "Bitwarden", "Keychain Access", "Terminal", "Finder", "Dock"}

_MENU_NOISE = {
    "File", "Edit", "View", "Go", "Run", "Terminal", "Window", "Help",
    "Selection", "History", "Bookmarks", "Profiles", "Tab", "Format",
    "Insert", "Tools", "Navigate", "Debug", "Code", "Search",
    "App", "Background", "Activity", "Storage", "Screen", "Network",
}

# ── State ─────────────────────────────────────────────────────────────────────
last_hash       = None
last_app        = None
last_win_title  = None
last_mouse_pos  = None
last_force_time = 0.0
in_meeting      = False
total_frames    = 0
total_ocr       = 0
total_stored    = 0

def _get_mouse_pos():
    """Get current mouse position via Quartz — no Accessibility permission needed."""
    try:
        from Quartz import CGEventCreate, CGEventGetLocation
        event = CGEventCreate(None)
        pos = CGEventGetLocation(event)
        return (int(pos.x), int(pos.y))
    except:
        return None

def _is_active() -> bool:
    """Return True if mouse moved or window title changed since last check."""
    global last_mouse_pos, last_win_title
    active = False
    pos = _get_mouse_pos()
    if pos and pos != last_mouse_pos:
        last_mouse_pos = pos
        active = True
    return active

def load_state():
    global last_hash, total_frames, total_ocr, total_stored
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text())
            last_hash    = d.get("last_hash")
            total_frames = d.get("total_frames", 0)
            total_ocr    = d.get("total_ocr", 0)
            total_stored = d.get("total_stored", 0)
        except: pass

def save_state():
    STATE_FILE.write_text(json.dumps({
        "last_hash": last_hash, "total_frames": total_frames,
        "total_ocr": total_ocr, "total_stored": total_stored,
        "last_run":  datetime.now().isoformat(),
    }))

# ── Utilities ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + "\n")
    except: pass

def is_on_battery():
    try:
        r = subprocess.run([PMSET, '-g', 'batt'], capture_output=True, text=True)
        return 'discharging' in r.stdout.lower()
    except: return False

def is_in_meeting():
    for app in MEETING_APPS:
        r = subprocess.run([PGREP, '-x', app], capture_output=True)
        if r.returncode == 0:
            return True
    return False

def get_active_app():
    try:
        r = subprocess.run([OSASCRIPT, '-e', '''
tell application "System Events"
    set fa to first application process whose frontmost is true
    set an to name of fa
    try
        set wt to title of front window of fa
    on error
        set wt to ""
    end try
    return an & "|" & wt
end tell'''], capture_output=True, text=True, timeout=2)
        parts = r.stdout.strip().split('|', 1)
        return parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
    except: return (last_app or "screen"), ""

def dhash(img: Image.Image, size: int = DHASH_SIZE) -> int:
    """Perceptual hash matching Omi's dHash (Hamming threshold=5)."""
    small = img.convert('L').resize((size + 1, size), Image.LANCZOS)
    pixels = list(small.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            idx = row * (size + 1) + col
            if pixels[idx] > pixels[idx + 1]:
                bits |= (1 << (row * size + col))
    return bits

def hamming(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count('1')

def take_screenshots() -> list:
    """
    Capture ALL connected displays.
    screencapture creates one file per physical display when given multiple paths.
    Returns [(Image, path, label), ...] for each display found.
    """
    subprocess.run([SCREENCAPTURE, '-x'] + DISPLAY_PATHS,
                   capture_output=True, timeout=5)
    results = []
    for path, label in zip(DISPLAY_PATHS, DISPLAY_LABELS):
        p = Path(path)
        if p.exists() and p.stat().st_size > 5000:   # >5KB = real display content
            try:
                img = Image.open(path)
                img.load()   # force decode now while file is valid
                results.append((img, path, label))
            except: pass
    return results

# ── Apple Vision OCR (local, ANE-accelerated) ─────────────────────────────────

_vision_available = None

def _check_vision():
    global _vision_available
    if _vision_available is None:
        import importlib.util
        _vision_available = importlib.util.find_spec("Vision") is not None
    return _vision_available

def local_ocr(image_path: str) -> str:
    """
    Apple Vision OCR — same as Omi's VNRecognizeTextRequest (fast mode).
    Runs on Apple Neural Engine: ~100-200ms, near-zero battery draw.
    """
    if not _check_vision():
        return ""
    try:
        import Vision
        from Foundation import NSURL
        url     = NSURL.fileURLWithPath_(image_path)
        req     = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
        req.setUsesLanguageCorrection_(False)
        req.setRecognitionLanguages_(["en-US"])
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        success, _ = handler.performRequests_error_([req], None)
        if not success:
            return ""
        lines = []
        for obs in (req.results() or []):
            candidates = obs.topCandidates_(1)
            if candidates:
                t = str(candidates[0].string()).strip()
                if t:
                    lines.append(t)
        return "\n".join(lines)
    except Exception:
        return ""

def clean_ocr_text(text: str) -> str:
    """Remove macOS menu bar noise, keep meaningful content."""
    lines = text.split("\n")
    clean = []
    for line in lines:
        s = line.strip()
        if len(s) < 3:
            continue
        words = s.split()
        if all(w in _MENU_NOISE or w.isdigit() or len(w) == 1 for w in words):
            continue
        if re.match(r'^[\d%:°\s]+$', s):
            continue
        clean.append(s)
    return "\n".join(clean)

# ── Server communication ───────────────────────────────────────────────────────

_vesper_url = None

def get_vesper():
    global _vesper_url
    if _vesper_url:
        try:
            requests.get(f"{_vesper_url}/health", timeout=2, verify=False)
            return _vesper_url
        except: _vesper_url = None
    for url in [VESPER_LAN, VESPER_TS]:
        try:
            requests.get(f"{url}/health", timeout=3, verify=False)
            _vesper_url = url
            log(f"Connecting to Vesper: {url}")
            return url
        except: continue
    return None

def store_ocr_text(text: str, app_name: str, win_name: str) -> dict:
    """Send OCR text to server for embedding + storage (~2KB, not image)."""
    vesper = get_vesper()
    if not vesper:
        return {"stored": False, "error": "server unreachable"}
    try:
        r = requests.post(f"{vesper}/store_ocr", json={
            "text": text, "app_name": app_name,
            "window_name": win_name, "chars": len(text),
        }, timeout=10, verify=False)
        return r.json()
    except Exception as e:
        return {"stored": False, "error": str(e)}

# ── Meeting audio toggle ───────────────────────────────────────────────────────

PLIST         = str(Path.home() / "Library/LaunchAgents/com.vesper.screenpipe.plist")
MEETING_STATE = Path("/tmp/vesper_meeting_state")

MEETING_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.vesper.screenpipe</string>
    <key>ProgramArguments</key><array>
        <string>/opt/homebrew/bin/screenpipe</string>
        <string>--fps</string><string>0.033</string>
        <string>--audio-transcription-engine</string><string>whisper-large-v3-turbo</string>
        <string>--vad-engine</string><string>silero</string>
        <string>--vad-sensitivity</string><string>high</string>
        <string>--audio-chunk-duration</string><string>30</string>
        <string>--audio-device</string><string>MacBook Pro Microphone</string>
        <string>--audio-device</string><string>BlackHole 2ch</string>
        <string>--disable-vision</string>
        <string>--disable-telemetry</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/Users/tanmay/vesper_agent/logs/screenpipe.log</string>
    <key>StandardErrorPath</key><string>/Users/tanmay/vesper_agent/logs/screenpipe_err.log</string>
    <key>EnvironmentVariables</key><dict>
        <key>HOME</key><string>/Users/tanmay</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict></plist>"""

IDLE_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.vesper.screenpipe</string>
    <key>ProgramArguments</key><array>
        <string>/bin/sh</string><string>-c</string>
        <string>while true; do sleep 3600; done</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
    <key>StandardOutPath</key><string>/dev/null</string>
    <key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>"""

def enable_meeting_audio():
    with open(PLIST, 'w') as f: f.write(MEETING_PLIST)
    subprocess.run(['launchctl', 'kickstart', '-k',
                    f'gui/{os.getuid()}/com.vesper.screenpipe'], capture_output=True)
    MEETING_STATE.touch()
    log("🎙 Meeting: audio enabled (mic + BlackHole, vision=off)")

def disable_meeting_audio():
    with open(PLIST, 'w') as f: f.write(IDLE_PLIST)
    subprocess.run(['launchctl', 'kickstart', '-k',
                    f'gui/{os.getuid()}/com.vesper.screenpipe'], capture_output=True)
    MEETING_STATE.unlink(missing_ok=True)
    log("✅ Meeting ended: audio disabled")

# ── Main loop ─────────────────────────────────────────────────────────────────

def capture_once(force: bool = False):
    global last_hash, last_app, last_win_title, last_force_time, total_frames, total_ocr, total_stored

    total_frames += 1

    # Capture ALL connected displays
    displays = take_screenshots()
    if not displays:
        return

    # Active app — always check (window title change also forces a capture)
    app_name, win_name = get_active_app()
    win_changed = win_name and win_name != last_win_title
    last_win_title = win_name
    last_app = app_name

    if app_name in SKIP_APPS:
        return

    # Activity-forced capture: bypass dHash when user moved mouse or switched window
    # Debounce: at most one forced capture every ACTIVITY_COOLDOWN seconds
    now = time.time()
    activity = force or win_changed or _is_active()
    if activity and (now - last_force_time) >= ACTIVITY_COOLDOWN:
        last_force_time = now
        # Still update the hash so the NEXT timer-based check has a fresh baseline
        main_img = displays[0][0]
        last_hash = dhash(main_img)
    else:
        # dHash dedup on primary display
        main_img = displays[0][0]
        current_hash = dhash(main_img)
        if last_hash is not None and hamming(current_hash, last_hash) <= DHASH_THRESH:
            return  # screen unchanged, no activity
        last_hash = current_hash

    # OCR ALL displays (ANE-accelerated, near-zero battery)
    total_ocr += 1
    text_parts = []
    for _, path, label in displays:
        raw = local_ocr(path)
        if not raw:
            continue
        clean = clean_ocr_text(raw)
        if len(clean) < 15:
            continue
        # Label external display content; main display needs no prefix
        prefix = f"[{label.upper()}] " if label != 'main' else ""
        text_parts.append(prefix + clean)

    ocr_text = "\n".join(text_parts)
    if len(ocr_text) < 30:
        return

    # Store on server — battery mode just uses slower interval, never skips.
    # 2KB of text every 15s is ~0.1% of WiFi capacity, negligible battery impact.
    result = store_ocr_text(ocr_text, app_name, win_name)
    if result.get("stored"):
        total_stored += 1
        win_show = win_name[:40] if win_name else "(no title)"
        n = len(displays)
        bat = " 🔋" if is_on_battery() else ""
        log(f"📸 [{app_name}] {win_show} | {len(ocr_text)}c | {n}🖥{bat} → stored")
    elif result.get("error") and "unreachable" not in result.get("error", ""):
        log(f"⚠️  Store error: {result['error'][:60]}")

def run():
    global in_meeting
    load_state()

    vision_ok = _check_vision()
    log(f"VESPER CAPTURE — Apple Vision: {'✅' if vision_ok else '❌ (pip install pyobjc-framework-Vision)'}")
    log(f"Frames: {total_frames} | Stored: {total_stored} | Server: {get_vesper() or 'not found'}")

    meeting_counter = 0
    while True:
        try:
            meeting_counter += 1
            if meeting_counter >= 20:
                meeting_counter = 0
                now_meeting = is_in_meeting()
                if now_meeting and not in_meeting:
                    in_meeting = True; enable_meeting_audio()
                elif not now_meeting and in_meeting:
                    in_meeting = False; disable_meeting_audio()

            capture_once()
            interval = INTERVAL_BAT if is_on_battery() else INTERVAL_AC
            if in_meeting: interval = min(interval, INTERVAL_AC)
            save_state()
            time.sleep(interval)
        except KeyboardInterrupt:
            log("Stopped"); save_state(); break
        except Exception as e:
            log(f"Error: {e}"); time.sleep(5)

def test_ocr():
    """--test: OCR all connected displays and show results."""
    import time as _t
    print("📸 Screenshot (all displays)...")
    displays = take_screenshots()
    if not displays:
        print("❌ No displays captured"); return

    for img, path, label in displays:
        print(f"\n🖥  Display [{label}]: {img.size[0]}×{img.size[1]}")
        print(f"   dHash: {dhash(img)}")
        print(f"⚡ Apple Vision OCR...")
        t0 = _t.time()
        raw = local_ocr(path)
        ms  = int((_t.time()-t0)*1000)
        clean = clean_ocr_text(raw)
        print(f"   {ms}ms | {len(raw)}c raw → {len(clean)}c clean")
        print(f"--- OCR [{label}] ---")
        print(clean[:600])
        print("---")

    app, win = get_active_app()
    print(f"\nActive App: {app!r} | Window: {win[:60]!r}")

def print_status():
    load_state()
    # Detect connected displays
    displays = take_screenshots()
    n_displays = len(displays)
    display_info = " + ".join(f"{img.size[0]}×{img.size[1]} [{lbl}]"
                               for img, _, lbl in displays) or "none"
    bat = is_on_battery()
    print(f"Apple Vision OCR: {'✅' if _check_vision() else '❌'}")
    print(f"Frames checked:   {total_frames}")
    print(f"OCR calls:        {total_ocr}  ({100*total_ocr//max(total_frames,1)}% of frames)")
    print(f"Stored:           {total_stored}")
    print(f"Server:           {get_vesper() or 'unreachable'}")
    print(f"Displays:         {n_displays} — {display_info}")
    print(f"Power:            {'🔋 Battery (15s interval, still storing)' if bat else '⚡ AC (3s interval)'}")
    print(f"In meeting:       {'yes' if MEETING_STATE.exists() else 'no'}")

if __name__ == "__main__":
    os.makedirs(LOG_FILE.parent, exist_ok=True)
    if "--status" in sys.argv:
        print_status()
    elif "--once" in sys.argv:
        load_state(); capture_once(); save_state()
    elif "--test" in sys.argv:
        test_ocr()
    else:
        run()
