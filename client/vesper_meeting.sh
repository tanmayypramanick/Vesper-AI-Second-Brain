#!/bin/bash
# VESPER MEETING TOGGLE
# Runs every 2 minutes via launchd (com.vesper.screenpipe-meeting)
# Auto-detects meetings → enables mic + system audio in screenpipe
# When meeting ends → back to silent OCR-only mode (ultra-light)
#
# Meeting mode:  0.2 FPS + mic + BlackHole system audio
# Normal mode:   0.033 FPS + no audio (1 screenshot/30s)

PLIST="$HOME/Library/LaunchAgents/com.vesper.screenpipe.plist"
STATE_FILE="/tmp/vesper_in_meeting"
LOG="$HOME/vesper_agent/logs/meeting.log"

# ── Meeting app detection ─────────────────────────────────────────────────────
in_meeting() {
    # Check for common meeting app processes
    pgrep -x "zoom.us"           > /dev/null 2>&1 && return 0
    pgrep -x "Zoom"              > /dev/null 2>&1 && return 0
    pgrep -x "Microsoft Teams"   > /dev/null 2>&1 && return 0
    pgrep -x "Webex"             > /dev/null 2>&1 && return 0
    pgrep -x "Webex Meetings"    > /dev/null 2>&1 && return 0
    pgrep -x "Loom"              > /dev/null 2>&1 && return 0

    # Check if Fathom is running (records meetings)
    pgrep -x "Fathom"            > /dev/null 2>&1 && return 0

    # Check for Google Meet or other browser meetings via screenpipe OCR
    # (reads last OCR frame from screenpipe API)
    local app
    app=$(curl -s "http://localhost:3030/search?content_type=ocr&limit=2" 2>/dev/null | \
        python3 -c "
import json, sys
try:
    items = json.load(sys.stdin).get('data', [])
    apps = [i['content'].get('app_name','') for i in items]
    wins = [i['content'].get('window_name','') for i in items]
    all_text = ' '.join(apps + wins).lower()
    keywords = ['meet.google', 'zoom', 'teams', 'webex', 'whereby', 'discord', 'gather.town', 'fathom']
    print('YES' if any(k in all_text for k in keywords) else 'NO')
except:
    print('NO')
" 2>/dev/null)
    [ "$app" = "YES" ] && return 0

    # Check if microphone is actively being used by any app (macOS Sonoma+)
    # This catches any meeting/recording app using the mic
    local mic_users
    mic_users=$(ioreg -n IOHDACodecDriver 2>/dev/null | grep -c '"IOAudioStreamInputSampleRate"' || echo 0)
    # Alternative: check if mic is "in use" indicator is showing (no easy API, skip)

    return 1
}

# ── Write plist based on mode ─────────────────────────────────────────────────
write_meeting_plist() {
    cat > "$PLIST" << 'MEETINGPLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vesper.screenpipe</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/screenpipe</string>
        <string>--fps</string>
        <string>0.2</string>
        <string>--audio-transcription-engine</string>
        <string>whisper-large-v3-turbo</string>
        <string>--vad-engine</string>
        <string>silero</string>
        <string>--vad-sensitivity</string>
        <string>high</string>
        <string>--audio-chunk-duration</string>
        <string>30</string>
        <string>--audio-device</string>
        <string>MacBook Pro Microphone</string>
        <string>--audio-device</string>
        <string>BlackHole 2ch</string>
        <string>--disable-telemetry</string>
        <string>--ignored-windows</string>
        <string>1Password</string>
        <string>--ignored-windows</string>
        <string>Bitwarden</string>
        <string>--ignored-windows</string>
        <string>Keychain</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/tanmay/vesper_agent/logs/screenpipe.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/tanmay/vesper_agent/logs/screenpipe_err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/tanmay</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
MEETINGPLIST
}

write_normal_plist() {
    cat > "$PLIST" << 'NORMALPLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vesper.screenpipe</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/screenpipe</string>
        <string>--fps</string>
        <string>0.033</string>
        <string>--disable-audio</string>
        <string>--disable-telemetry</string>
        <string>--ignored-windows</string>
        <string>1Password</string>
        <string>--ignored-windows</string>
        <string>Bitwarden</string>
        <string>--ignored-windows</string>
        <string>Keychain</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/tanmay/vesper_agent/logs/screenpipe.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/tanmay/vesper_agent/logs/screenpipe_err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>/Users/tanmay</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
NORMALPLIST
}

restart_screenpipe() {
    launchctl kickstart -k "gui/$(id -u)/com.vesper.screenpipe" > /dev/null 2>&1
}

# ── Main logic ────────────────────────────────────────────────────────────────
NOW=$(date "+%H:%M")
WAS_IN_MEETING=false
[ -f "$STATE_FILE" ] && WAS_IN_MEETING=true

if in_meeting; then
    if ! $WAS_IN_MEETING; then
        echo "$NOW: 🎙 Meeting detected — enabling audio + 0.2 FPS" >> "$LOG"
        touch "$STATE_FILE"
        write_meeting_plist
        restart_screenpipe
    fi
else
    if $WAS_IN_MEETING; then
        echo "$NOW: ✅ Meeting ended — disabling audio, back to 0.033 FPS" >> "$LOG"
        rm -f "$STATE_FILE"
        write_normal_plist
        restart_screenpipe
    fi
fi
