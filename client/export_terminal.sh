#!/bin/bash
# Export recent terminal history and VS Code workspace to server
SERVER="https://10.0.0.120:5000"

python3 << 'PYEOF'
import requests, urllib3, os, subprocess, json, datetime
urllib3.disable_warnings()
SERVER = "https://10.0.0.120:5000"
TODAY  = datetime.date.today().strftime("%Y-%m-%d")

# ── Terminal history (zsh + bash) ─────────────────────────────────────────────
hist_files = [
    os.path.expanduser("~/.zsh_history"),
    os.path.expanduser("~/.bash_history"),
]
all_cmds = []
for hf in hist_files:
    if not os.path.exists(hf): continue
    with open(hf, 'rb') as f:
        raw = f.read().decode('utf-8', errors='ignore')
    # zsh history format: ": timestamp:0;command"
    for line in raw.split('\n'):
        line = line.strip()
        if line.startswith(':'):
            parts = line.split(';', 1)
            if len(parts) == 2: all_cmds.append(parts[1].strip())
        elif line and not line.startswith('#'):
            all_cmds.append(line)

# Get last 200 unique meaningful commands
meaningful = [c for c in all_cmds if len(c) > 5 and not c.startswith(('ls', 'cd', 'pwd', 'echo', 'cat /tmp'))]
recent_cmds = list(dict.fromkeys(meaningful))[-200:]

if recent_cmds:
    doc = f"[Terminal History] {TODAY}\nRecent commands (last 200):\n" + '\n'.join(f"  $ {c}" for c in recent_cmds[-100:])
    try:
        r = requests.post(f"{SERVER}/store_memory",
            json={"text": doc[:3000], "category": "terminal", "source": "terminal_history"},
            verify=False, timeout=15)
        print(f"Terminal history: {r.json()}")
    except Exception as e:
        print(f"Terminal error: {e}")

# ── VS Code recent files ───────────────────────────────────────────────────────
vscode_storage = os.path.expanduser("~/Library/Application Support/Code/User/globalStorage/storage.json")
recently_opened = []
if os.path.exists(vscode_storage):
    try:
        with open(vscode_storage) as f:
            data = json.load(f)
        entries = data.get('lastKnownMenubarData', {}).get('menus', {}).get('File', {}).get('items', [])
        # Try different key
        if not entries:
            # globalStorage may have recentPathsList
            pass
    except: pass

# Also check workspaces
ws_path = os.path.expanduser("~/Library/Application Support/Code/User/workspaceStorage")
ws_dirs = []
if os.path.exists(ws_path):
    for d in os.listdir(ws_path)[:30]:
        wp = os.path.join(ws_path, d, 'workspace.json')
        if os.path.exists(wp):
            try:
                with open(wp) as f:
                    ws_data = json.load(f)
                folder = ws_data.get('folder', '')
                if folder and '/Users/' in folder:
                    ws_dirs.append(folder.replace('file://', ''))
            except: pass

if ws_dirs:
    doc = f"[VS Code Workspaces] {TODAY}\nRecently opened projects:\n" + '\n'.join(f"  {d}" for d in ws_dirs[:50])
    try:
        r = requests.post(f"{SERVER}/store_memory",
            json={"text": doc, "category": "vscode", "source": "vscode_workspaces"},
            verify=False, timeout=15)
        print(f"VS Code workspaces: {r.json()}")
    except Exception as e:
        print(f"VS Code error: {e}")

print("Terminal export done")
PYEOF
