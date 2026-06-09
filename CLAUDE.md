# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## MCP Tools: code-review-graph

**IMPORTANT: This project has a knowledge graph. ALWAYS use the
code-review-graph MCP tools BEFORE using Grep/Glob/Read to explore
the codebase.** The graph is faster, cheaper (fewer tokens), and gives
you structural context (callers, dependents, test coverage) that file
scanning cannot.

### When to use graph tools FIRST

- **Exploring code**: `semantic_search_nodes` or `query_graph` instead of Grep
- **Understanding impact**: `get_impact_radius` instead of manually tracing imports
- **Code review**: `detect_changes` + `get_review_context` instead of reading entire files
- **Finding relationships**: `query_graph` with callers_of/callees_of/imports_of/tests_for
- **Architecture questions**: `get_architecture_overview` + `list_communities`

Fall back to Grep/Glob/Read **only** when the graph doesn't cover what you need.

---

## Project: Vesper — Local Personal AI

Vesper is a fully private, self-hosted personal AI assistant running on a home server. It ingests data from 10+ sources (iMessages, Gmail, WhatsApp, screen OCR, calendar, contacts, browser history, etc.) into ChromaDB, and provides a voice interface (kiosk), WhatsApp bot, and web UI.

**This repo (`vesper_agent/`) is the Mac-side codebase.** The server-side code lives at `/home/tanmay/vesper/` on the server (`10.0.0.120`). Changes to `file_receiver.py`, pipeline scripts, and the WhatsApp bot require SSHing into the server.

---

## Two-Machine Architecture

| Machine | Role | IP |
|---------|------|----|
| Mac (M-series) | Data capture: OCR, audio, iMessages, Gmail, exports | 10.0.0.169 |
| Server (i5-8250U + MX130 GPU) | ChromaDB, LLM inference, Flask server, WhatsApp bots | 10.0.0.120 |
| Nord phone | Kiosk UI — Fully Kiosk Browser locked to `/ui` | desk display |

**Critical rule: ChromaDB has a single writer.** `file_receiver.py` is the only process that touches ChromaDB. All ingest scripts POST to its HTTP API. Never open a `PersistentClient` from any other process — this caused a filesystem corruption incident that required a full rebuild.

---

## Server-Side Key Files

```
/home/tanmay/vesper/
├── pipelines/
│   ├── file_receiver.py      # THE server — Flask, ChromaDB, voice pipeline, all endpoints
│   ├── ingest_*.py           # Data ingest scripts (POST to file_receiver, never write ChromaDB directly)
│   ├── morning_briefing.py   # Daily 8am WhatsApp summary
│   ├── proactive_alerts.py   # Every 30min: priority message/email alerts
│   └── night_batch.py        # 2am: re-transcribes audio with larger Whisper model
├── openclaw/
│   ├── bot.js                # Indian WhatsApp bot (Baileys) — ingests all, replies only to OWNER_JID
│   └── bot_us.js             # US WhatsApp bot — silent ingester only
├── models/                   # Piper TTS models, SenseVoice STT
├── cert.pem / key.pem        # TLS cert for HTTPS on port 5000
└── logs/                     # file_receiver.log, openclaw.log, openclaw_us.log, etc.
```

## Mac-Side Files (this repo)

```
vesper_agent/
├── vesper_capture.py     # Apple Vision OCR → POST /store_ocr to server
├── vesper_audio.py       # Continuous mic recording → saves to /mnt/hdd/vesper_audio/
├── vesper_screen.py      # Screen capture via screenpipe
├── gmail_realtime.py     # IMAP IDLE → real-time email ingestion
├── file_watcher.py       # Watches exports/ queue → POST /ingest to server
├── export_*.sh / .py     # One-shot data exporters (iMessages, browser, contacts, etc.)
├── frontend/kiosk.html   # Kiosk UI source (deployed to server)
└── exports/              # Local JSON/VCF export staging area
```

---

## Running / Operating the Server

**SSH access:**
```bash
ssh -i ~/.ssh/vesper_key tanmay@10.0.0.120      # LAN
ssh -i ~/.ssh/vesper_key tanmay@100.123.15.32   # Tailscale (off-network)
```

**Check everything is healthy:**
```bash
curl -sk https://127.0.0.1:5000/health          # {"status":"ok","memories":N}
ps aux | grep -E 'file_receiver|bot\.js|bot_us'
ss -tlnp | grep 5000
```

**Restart server (safe — avoids SSH drop):**
```bash
kill -15 $(pgrep -f file_receiver.py | head -1)
sleep 3
rm -f /tmp/vesper_receiver.lock
nohup python3 /home/tanmay/vesper/pipelines/file_receiver.py >> /home/tanmay/vesper/logs/file_receiver.log 2>&1 &
```
Do NOT use `pkill -f file_receiver.py` — it drops the SSH session.

**Start WhatsApp bots** (requires NVM; node binary at `~/.nvm/versions/node/v20.20.2/bin/node`):
```bash
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
cd /home/tanmay/vesper/openclaw
nohup node bot.js >> /home/tanmay/vesper/logs/openclaw.log 2>&1 &
nohup node bot_us.js >> /home/tanmay/vesper/logs/openclaw_us.log 2>&1 &
```

**Query Vesper from CLI (test a voice_fast question):**
```bash
# On server:
curl -sk -X POST https://127.0.0.1:5000/voice_fast \
  -H 'Content-Type: application/json' \
  -d '{"question":"what time is it","voice":"af_heart","history":[],"source":"test"}' \
  | grep '^data: T:' | sed 's/data: T://' | base64 -d
```

---

## The voice_fast Pipeline (most complex code)

`POST /voice_fast` in `file_receiver.py` is the main AI endpoint. It streams SSE with three chunk types:
- `data: Q:<b64>` — echo of the question
- `data: T:<b64>` — text response chunk
- `data: A:<b64>` — audio WAV chunk (base64, from Piper TTS)

The routing hierarchy inside `_generate()` (the SSE generator):
1. **Early fast paths** — time, greetings, math, DOB, phone number, email, Arpita, tools — return in <200ms
2. **ChromaDB query** — embed question → cosine search in `vesper_life` collection
3. **Temporal filter** — "yesterday/last week" queries add `where` filter by timestamp
4. **Routing flags** — `_is_msg`, `_is_email`, `_is_contact`, `_is_activity`, `_is_web` determine which path runs
5. **Data fast paths** — message lookup, email format, contact lookup, screen activity — bypass LLM
6. **Anti-hallucination guard** — meal/bank/gym/sleep questions that have no data → explicit refusal
7. **LLM synthesis** — dolphin-voice (22 GPU layers) with comma-split TTS streaming

**Key invariants:**
- `_is_contact` path checks `_to_name` against ChromaDB contacts; always returns full entry if exact match found
- `_is_activity` fast path must map `[Screen OCR | appname]` prefix → display app name (see `_app_map_act` dict)
- `_is_web` flag (set when question contains general knowledge keywords) bypasses personal data lookup
- Threshold is tighter for WhatsApp queries (`0.78`) than screen/activity (`0.60`) to prevent hallucination

---

## ChromaDB Schema

Single collection `vesper_life` at `/mnt/hdd/vesper_memory/`. Every document has:
```python
metadata = {
    "category": str,   # e.g. "whatsapp", "email_received", "screen", "notes", "contact"
    "source":   str,   # e.g. "whatsapp:Arpita", "screen:Electron", "openclaw_realtime"
    "timestamp": int   # Unix timestamp
}
```
Embedding model: `nomic-cpu` (nomic-embed-text, 768-dim, stays GPU-loaded permanently).

---

## LLM / Model Stack

| Model | Ollama name | Use | Notes |
|-------|-------------|-----|-------|
| dolphin-phi 2.7B | `dolphin-voice` | Voice queries | 22/32 GPU layers, ~15.5 tok/s, uncensored |
| qwen3:4b | `qwen3-ask` | WhatsApp `/model qwen3` | CPU only, prefix `/no_think\n` to suppress empty thinking |
| nomic-embed-text | `nomic-cpu` | Embeddings | GPU, always loaded, 50-80ms per embed |

TTS: Piper (`hfc_female-medium` / `ryan-high`) — 62–220ms, CPU, primary.  
STT: faster-whisper distil-small (int8, CPU) for real-time; whisper-medium for night batch.

---

## Cron Jobs (Server)

All jobs run as user `tanmay`. The watchdog is critical — it detects deadlocks via `/health` (not just port binding):
```
*/5 * * * *  curl -sk --max-time 4 https://127.0.0.1:5000/health || { kill+restart file_receiver.py }
*/2 * * * *  pgrep bot.js || start bot.js     # bot keepalive
*/15 * * * * ingest_imessages.py
*/30 * * * * ingest_gmail.py, ingest_browser.py, proactive_alerts.py
0 * * * *    ingest_calendar.py
0 2 * * *    night_batch.py
0 8 * * *    morning_briefing.py
0 3 1 * *    tailscale cert renewal + restart
```

---

## Known Recurring Issues & Fixes

**Server won't start (port 5000 not binding):**
- Check `grep -v '\[INFO\]' /home/tanmay/vesper/logs/file_receiver.log | tail -20`
- "Port 8080 is in use" → `except BaseException` fix already applied (line ~2557); re-check if regressed
- ChromaDB import error → `pip3 install --upgrade numpy --break-system-packages`
- Lock file stale → `rm -f /tmp/vesper_receiver.lock`

**WhatsApp bot silent:**
1. `ss -tlnp | grep 5000` — if empty, server is down, restart it
2. `ps aux | grep 'node bot'` — if empty, bots aren't running
3. Node binary corrupted → `ls -la ~/.nvm/versions/node/v20.20.2/bin/node` (should not be 0 bytes)
4. If 0 bytes: reinstall NVM (`rm -rf ~/.nvm && curl -o- https://nvm.sh | bash && nvm install 20`)
5. After NVM reinstall, `cd /home/tanmay/vesper/openclaw && npm install` to restore node_modules

**Bot cron uses NVM path** — cron sources `~/.nvm/nvm.sh` before running node. If NVM is broken, bots won't auto-restart. Update cron to use full path after reinstall: `/home/tanmay/.nvm/versions/node/v20.XX.X/bin/node`.

**Server deadlock (port bound but /health hangs):** Watchdog detects this within 5 min and restarts.

---

## Data Flow: Mac → Server

```
Mac capture (OCR/audio/exports)
    → exports/ JSON files OR direct HTTP POST
    → file_watcher.py / export scripts
    → POST https://10.0.0.120:5000/ingest (or /store_memory, /store_ocr)
    → file_receiver.py embeds (nomic-cpu) + stores (ChromaDB)
```

iMessages, Gmail, browser, calendar, contacts — all via cron on the **Mac**, exporting to `exports/` then POSTing to server.

Gmail real-time: `gmail_realtime.py` uses IMAP IDLE (LaunchAgent: `com.vesper.gmail_realtime.plist`).

Screen OCR: `vesper_capture.py` uses Apple Vision framework — triggers on screen change, sends raw text + app_name to `/store_ocr`.

---

## UI / Kiosk

- **Web UI**: `https://10.0.0.120:5000/ui` (full chat + voice orb)
- **Kiosk**: `https://10.0.0.120:5000/kiosk` (Nord phone, Fully Kiosk Browser)
- **HTTP kiosk** (no cert, port 8080): only if port not taken by open_webui
- Self-signed cert: `cert.pem` / `key.pem` in `/home/tanmay/vesper/`; Android needs manual install
- Tailscale hostname: `vesper-server.tail614590.ts.net:5000` for off-network access

The kiosk uses Three.js (3D ocean orb), Web Audio API VAD (stops after 1.4s silence), Web Speech API wake word ("vesper"), SSE streaming for text+audio response.
