# VESPER — Your Personal Life AI

> *"An AI that knows everything about you — privately, locally, always-on."*

Vesper is a fully private, self-hosted personal AI built by Tanmay Pramanick. It ingests your entire digital life in real-time — every message, email, calendar event, browser visit, screen activity, and phone call — stores it in a local vector database, and lets you query it through voice, WhatsApp, or a beautiful kiosk display. Everything runs on your own hardware. No data ever leaves your home network.

---

## Table of Contents

1. [The Motivation](#the-motivation)
2. [System Architecture](#system-architecture)
3. [Hardware](#hardware)
4. [Data Pipelines — What Gets Ingested](#data-pipelines)
5. [The Server — file_receiver.py](#the-server)
6. [AI Models Stack](#ai-models-stack)
7. [Voice Pipeline](#voice-pipeline)
8. [WhatsApp Bot (OpenClaw)](#whatsapp-bot)
9. [Kiosk Frontend](#kiosk-frontend)
10. [Mac-Side Files](#mac-side-files-vesper_agent)
11. [Server-Side Files](#server-side-files-homtanmayvesper)
12. [Performance & Optimizations](#performance--optimizations)
13. [Features](#features)
14. [Known Issues & Limitations](#known-issues--limitations)
15. [Future Scope](#future-scope)

---

## The Motivation

The project started from a simple, profound frustration: every AI assistant you talk to knows nothing about you. You ask it what you were working on last Tuesday — it doesn't know. You ask it about a person you've been messaging — it doesn't know. You ask it what that email said last week — it doesn't know.

Cloud AI products (ChatGPT, Siri, Google Assistant) are completely stateless or only know what you explicitly tell them in the moment. And even when they do store data, it's on their servers, in their data centers, trained on your conversations to improve their products.

Tanmay wanted something different:

- **Privacy first**: Your messages, emails, location data, conversations — none of this should leave your home. Ever.
- **True personalization**: An AI that has actually read your messages, knows who Arpita is, knows you work in Chicago, knows what you were coding last week.
- **Always available**: Works on WhatsApp (from anywhere), via voice at your desk, on a dedicated display — not just on one device or app.
- **Growing memory**: Every day that passes, it knows more. It accumulates. It gets smarter about *you* specifically.

The vision: a personal AI that feels less like a search engine and more like a brilliant assistant who has been quietly observing and remembering everything about your life for months — and can recall any of it instantly.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          YOUR HOME NETWORK                           │
│                                                                      │
│  ┌──────────────────┐        ┌──────────────────────────────────┐   │
│  │    MacBook Pro    │        │        VESPER SERVER             │   │
│  │  (10.0.0.169)     │        │      (10.0.0.120:5000 HTTPS)     │   │
│  │                  │        │                                  │   │
│  │ ◆ vesper_capture ├──────► │ ◆ file_receiver.py (Flask)       │   │
│  │   (screen OCR)   │  SCP/  │   THE ONLY ChromaDB writer       │   │
│  │ ◆ vesper_audio   │  HTTP  │                                  │   │
│  │   (24/7 mic)     │        │ ◆ ChromaDB (vesper_life)         │   │
│  │ ◆ Gmail IMAP     │        │   12,000+ memories                │   │
│  │   (real-time)    │        │   /mnt/hdd/vesper_memory/        │   │
│  │ ◆ iMessage cron  │        │                                  │   │
│  │ ◆ browser export │        │ ◆ Ollama (LLM + Embeddings)      │   │
│  │ ◆ calendar cron  │        │   dolphin-voice (22/32 GPU)      │   │
│  │ ◆ contacts cron  │        │   qwen3-ask (CPU)                │   │
│  │ ◆ WhatsApp export│        │   nomic-embed-text (GPU)         │   │
│  └──────────────────┘        │                                  │   │
│                              │ ◆ Piper TTS (CPU)                │   │
│  ┌──────────────────┐        │ ◆ distil-whisper STT (CPU)       │   │
│  │   OnePlus Nord   │        │                                  │   │
│  │  (Kiosk Display) │        │ ◆ openclaw/bot.js (WhatsApp)     │   │
│  │                  │◄───────│   Indian # (bot) + US # (ingest) │   │
│  │ ◆ Fully Kiosk    │ HTTPS  │                                  │   │
│  │ ◆ kiosk.html     │        └──────────────────────────────────┘   │
│  │ ◆ Three.js Orb   │                        ▲                      │
│  │ ◆ Wake word      │          WhatsApp ─────┘                      │
│  │ ◆ AOD screen     │                                               │
│  └──────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────┘
```

**Design Principle**: The server is the single source of truth. All data flows TO the server. All queries come FROM the server. No client directly touches ChromaDB. This prevents the race conditions and segfaults that plagued early versions.

---

## Hardware

### Server (Primary AI Compute)
| Component | Spec |
|-----------|------|
| CPU | Intel Core i5-8250U (Kaby Lake-R, 8th gen) |
| Cores | 4 cores / 8 threads, 1.6 GHz base / 3.4 GHz boost |
| RAM | 15 GB DDR4 |
| GPU | NVIDIA MX130 (Maxwell architecture, sm_5.0) |
| VRAM | 2,048 MB |
| GPU Memory BW | 28.8 GB/s |
| Storage | External HDD at `/mnt/hdd/` for ChromaDB |
| OS | Ubuntu Linux |
| Network | LAN 10.0.0.120, Tailscale 100.123.15.32 |

**Important GPU constraint**: The MX130 uses Maxwell architecture (compute capability sm_5.0). Many modern CUDA libraries (cuDNN used by TTS/Whisper) require sm_6.0+. This meant:
- Kokoro TTS (desired) → EXECUTION_FAILED on Maxwell — **cannot use**
- CTranslate2 CUDA for Whisper → broken on Maxwell — **cannot use**
- Ollama's LLM inference → **works** (handles Maxwell correctly)

This forced CPU fallbacks for Whisper and TTS, which shaped the entire performance optimization strategy.

### Mac (Data Capture)
- Apple Silicon MacBook Pro (M-series)
- Role: Screen capture, microphone recording, exporting iMessages/contacts/calendar/browser
- All data is pushed to the server via SCP or HTTP POST
- Never runs inference — purely a data collector

### OnePlus Nord (Kiosk Display)
- 6.44" screen, 1080×2400 px
- Permanently mounted on desk in landscape orientation
- Always plugged in
- Runs Fully Kiosk Browser (free)
- Tailscale connected
- Acts as a dedicated Vesper voice terminal / smart display

---

## Data Pipelines

All data flows into ChromaDB via `file_receiver.py`. Each pipeline produces a memory with a category, source, and text document.

### Real-Time Pipelines (Live / Near-Live)

| Source | Method | Frequency | Category | Count (est.) |
|--------|---------|-----------|----------|--------------|
| Gmail | IMAP IDLE + 30min backup | Instant + 30min | email_received, email_sent | ~1,300 |
| iMessages | Mac cron → SCP | Every 15 min | imessage | ~240+ |
| WhatsApp (Indian) | Baileys bot (real-time) | Instant | whatsapp | ~1,700+ |
| WhatsApp Business (US) | bot_us.js (real-time) | Instant | whatsapp | growing |
| Screen Activity | vesper_capture.py → OCR | Activity-triggered | screen_ocr | ~1,100+ |

### Scheduled Pipelines (Cron)

| Source | Schedule | Category | Notes |
|--------|----------|----------|-------|
| Browser History | Every 30 min | browser | Chrome + Safari |
| Calendar | Hourly | calendar | Apple Calendar |
| Contacts | Daily at 3 AM | contact | 908 contacts |
| WhatsApp export | Every 30 min | whatsapp | Backup / history |
| Instagram | Every 6 hours | *(broken)* | JSON corrupted |

### Batch / Night Pipeline

| Source | Schedule | Purpose |
|--------|----------|---------|
| Audio re-transcription | 2 AM daily | Re-transcribes day's WAV files with larger Whisper model for quality |
| Proactive alerts | Every 30 min | Checks for important messages/emails, sends WhatsApp push |

### Morning Briefing
Every day at **8:00 AM**, `morning_briefing.py` assembles a personalized briefing:
- Yesterday's screen activity summary
- Today's calendar events
- Recent important emails
- WhatsApp message highlights
- Top news (via DuckDuckGo)
- Weather (wttr.in)

Sent via WhatsApp to US number (appears on left side of WhatsApp Business app as an incoming message).

---

## The Server

`file_receiver.py` is the heart of Vesper. It is a Flask application (~2,500+ lines) running on port 5000 with HTTPS (self-signed cert).

**It is the ONLY process that writes to ChromaDB.** This single-writer architecture was forced by a critical discovery: ChromaDB's `hnswlib` C++ extension segfaults when multiple processes (or even multiple threads) write to the same database simultaneously. The solution was complete centralization.

### Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Returns `{"status":"ok","memories":N}` |
| `/ingest` | POST | Async memory ingestion (202, no wait) |
| `/store_memory` | POST | Synchronous memory write (200 after write) |
| `/store_ocr` | POST | Screen capture OCR storage |
| `/transcribe` | POST | Base64 WAV → Whisper text |
| `/transcribe_partial` | POST | Partial/streaming Whisper (live display) |
| `/voice_prepare` | POST | Pre-embed query + ChromaDB fetch (called while user still talking) |
| `/voice_fast` | POST | **Main voice endpoint** — SSE stream of text+audio chunks |
| `/voice_greet` | POST | Random casual greeting WAV |
| `/ask` | POST | JSON endpoint for smart qwen3 queries (WhatsApp /model qwen3) |
| `/recall` | POST | Query ChromaDB without LLM (returns raw memories) |
| `/query` | POST | Direct ChromaDB query |
| `/ui` | GET | Serves voice_ui.html (chat UI) |
| `/kiosk` | GET | Serves kiosk.html (kiosk frontend) |
| `/cert` | GET | Serves SSL cert for Android installation |

### Threading Architecture

- `_chroma_lock` (`threading.Lock()`) — ALL ChromaDB reads and writes go through this lock. One operation at a time.
- `_llm_sem` (`threading.Semaphore(1)`) — Only in `/ask` endpoint, one LLM inference at a time
- `_executor` (`ThreadPoolExecutor(max_workers=2)`) — For async `/ingest` operations
- `_nomic_warm_lock` — Prevents nomic-embed from being unloaded during requests

### Voice Fast Endpoint — Query Routing Logic

`/voice_fast` is the most complex piece of code in Vesper. It receives a question and returns a Server-Sent Events (SSE) stream of interleaved text chunks (`T:<base64>`) and audio WAV chunks (`A:<base64>`).

**Query routing hierarchy** (first match wins):
1. **Fast paths** (zero ChromaDB, zero LLM):
   - Wake word / identity / "who are you"
   - DOB fast path ("my date of birth" → instant answer)
   - Greetings ("hi", "hello", "hey")
   - Time/Date
   - Weather (wttr.in)
   - Meta-memory count
   - Capability questions ("what can you do")
2. **Data-lookup paths** (ChromaDB, no LLM):
   - Message lookup (`_is_msg`)
   - Email lookup (`_is_email`)
   - Contact lookup (`_is_contact`)
   - Calendar lookup (`_is_calendar_q`)
   - Location lookup
   - Notes lookup
   - Screen activity
   - Relationship analysis
3. **LLM paths** (ChromaDB + LLM):
   - Personal complex queries → dolphin-voice (uncensored)
   - General complex queries → qwen3-ask (smarter)
   - Web search queries → DuckDuckGo + LLM
4. **Anti-hallucination guards**:
   - Personal-life questions with no memory → refuse ("I don't have that recorded")
   - Meal/food queries → always refuse
   - Bank/financial → always refuse

---

## AI Models Stack

### LLM — Inference

| Model | Use | GPU Layers | Speed | VRAM |
|-------|-----|-----------|-------|------|
| **dolphin-voice** (dolphin-phi 2.7B) | Voice/casual queries | 22/32 | ~15.5 tok/s | ~1,100 MB |
| **qwen3-ask** (Qwen3 4B) | Complex/analytical queries | 0 (CPU) | ~5 tok/s | 0 |

**Why dolphin-phi for voice?**
- Based on Microsoft Phi-2, an excellent small model with 58.4% MMLU
- **Uncensored** — critical for personal questions about relationships, health, private matters
- Fits partially on the MX130 GPU at 22/32 layers
- No refusals on personal questions (unlike Qwen3 which hedges heavily)

**Why Qwen3 for smart queries?**
- Better reasoning, 32K context window (vs phi-2's 2K)
- More accurate for analytical/summarization tasks
- Runs fine on CPU since it's used only for /ask endpoint (non-realtime)

### Embedding

| Model | Inference | VRAM | Purpose |
|-------|-----------|------|---------|
| **nomic-embed-text** | GPU (always loaded) | 555 MB | All ChromaDB embed/query |

Always resident in GPU memory. Never evicted. All semantic search in Vesper goes through this model.

### Speech-to-Text

| Model | Inference | Speed | Notes |
|-------|-----------|-------|-------|
| **distil-whisper-small.en** | CPU (int8) | RTF ~0.74x | Cannot use GPU (Maxwell) |

RTF 0.74x means a 3-second audio clip takes ~2.2 seconds to transcribe. Fast enough for kiosk and WhatsApp voice notes.

Night batch uses **whisper-medium** (CPU, runs at 2 AM when idle) for higher-quality re-transcription of the day's audio diary recordings.

### Text-to-Speech

| Model | Inference | Speed | Notes |
|-------|-----------|-------|-------|
| **Piper** (hfc_female-medium) | CPU | 62–220ms/sentence | PRIMARY |
| **Supertonic-3** | CPU | 330–830ms/sentence | FALLBACK |

Kokoro TTS (the desired model) was tested and failed: `cuDNN EXECUTION_FAILED` on Maxwell sm_5.0. Piper was the best CPU-native alternative found.

### VRAM Budget

```
nomic-embed-text:        555 MB  (always loaded)
dolphin-voice (22/32):  ~1,100 MB  (loaded on first voice query)
─────────────────────────────────
Total peak:             ~1,655 MB / 2,048 MB    (393 MB headroom)
```

---

## Voice Pipeline

The voice pipeline was the most technically challenging and most refined part of the project. Initial latency was 9–25 seconds from question to first word. Current latency is **1.5–2.5 seconds**.

### Pipeline Stages (Kiosk)

```
User speaks → MediaRecorder (WebM/Opus, 16kHz)
           → VAD silence detection (1.4s quiet → stop)
           → Partial transcription via /transcribe_partial (live display)
           → Full audio blob → base64 → POST /voice_fast
                → Server: Whisper STT → question text
                → Embed with nomic-embed-text
                → ChromaDB query (cosine similarity)
                → Routing logic (fast path / data path / LLM path)
                → Generate response (dolphin or qwen3 via Ollama)
                → Stream response as SSE:
                    data: T:<base64_text_chunk>
                    data: A:<base64_wav_chunk>
                    data: END
           → Browser: decode text + play WAV chunks as they arrive
           → First audio fires at first comma/period (< 1s from generation start)
```

### Key Optimization: Comma-Split Streaming

The biggest latency win was "comma-split TTS streaming". Instead of waiting for the entire LLM response before sending audio, Vesper generates TTS at every natural pause (comma, period, semicolon). The first audio chunk fires the moment the LLM produces its first complete phrase — typically within 600ms of the LLM starting.

This dropped memory query latency from **5.3s → 2.0s**.

### Voice Pipeline (WhatsApp)

WhatsApp uses the same `/voice_fast` endpoint but with `source: "whatsapp"`:
- Audio: WhatsApp voice notes → Baileys downloads → base64 → `/voice_fast`
- Text: Direct text message → `/voice_fast` or `/ask` (for /model qwen3)
- Response: SSE stream → bot collects all T: chunks → sends as WhatsApp text message

### Pre-Computation via /voice_prepare

While the user is still speaking, the Mac (or browser VAD) calls `/voice_prepare` to:
1. Start embedding the partial transcript
2. Pre-fetch relevant memories from ChromaDB
3. Cache the results

When `/voice_fast` arrives with the full question, the embedding and ChromaDB results are already done — saving ~300ms.

---

## WhatsApp Bot

The WhatsApp integration uses **Baileys** (Node.js, `@whiskeysockets/baileys`) for unofficial WhatsApp Web protocol access.

### Two-Bot Architecture

**Indian Number Bot** (`bot.js`):
- Connected to Tanmay's personal Indian WhatsApp number
- **Only responds to messages from the US number** (OWNER_JID gate)
- Ingests ALL received messages to ChromaDB silently (family, friends, contacts)
- Uses API server on port 5001 for outbound messages

**US Number Bot** (`bot_us.js`):
- Connected to WhatsApp Business (US number)
- **Ingest-only** — never replies to anyone
- Stores all WA Business messages to ChromaDB
- This is how Tanmay's professional/US conversations get recorded

### Bot Features

| Feature | How |
|---------|-----|
| Talk to Vesper | Message Indian # from US # |
| Voice notes | Bot downloads → Whisper → /voice_fast |
| Photo analysis | Bot downloads → LLM vision |
| Natural "remember X" | Regex → /store_memory |
| /remember fact | Command → /store_memory |
| /model qwen3 | Routes to /ask (smarter, 90s timeout) |
| /model dolphin | Forces dolphin-voice |
| /model auto | Resets to automatic routing |
| /clear | Clears conversation history |
| Morning briefing | 8 AM daily → sent to US number |
| Retry logic | 30s timeout + 1 retry + 10s gap |
| Anti-deadlock | Cron checks /health, kills+restarts within 5 min |

### Conversation History

Per-JID conversation history (last 6 exchanges) is maintained in `history.json` and passed to the LLM for context in multi-turn conversations.

---

## Kiosk Frontend

The kiosk is a single self-contained HTML file (`kiosk.html`) served at `https://10.0.0.120:5000/kiosk`. It runs in Fully Kiosk Browser on the Nord permanently.

### Pages

| Page | Content |
|------|---------|
| **Home** | 3D animated orb, clock, greeting, weather panel, Up Next events, Quick Controls |
| **Memory** | Total memory count + breakdown by source |
| **System** | Server status, model info, pipeline health |
| **Settings** | All configuration toggles |

### The 3D Orb

Built with Three.js (r128). Uses a custom GLSL fragment shader to simulate an ocean-in-a-sphere effect:

- **Inner ocean**: Procedural sine-wave displacement creates a subtle ocean surface with depth
- **Color**: Deep blue with cyan rim glow; shifts to green/teal during listening
- **Wireframe overlay**: Subtle blue grid at 9% opacity
- **Particle cloud**: 220 floating dots orbiting the sphere
- **Equatorial ring**: Glowing line at the equator
- **Breathing animation**: Subtle 0.6Hz pulse (scale 1.0–1.012)
- **Audio-reactive**: Scale increases proportional to microphone volume during recording

### Voice Interaction

Unlike basic implementations, the kiosk uses the same high-quality pipeline as the main voice UI:
- **Recording**: `MediaRecorder` (WebM/Opus) — NOT Web Speech API (which gives bad quality)
- **STT**: Audio blob → `/transcribe_partial` (live display) + `/voice_fast` (full)
- **Wake word**: Web Speech API in **continuous mode** (lightweight, only listens for "vesper")
- **VAD**: Web Audio API analyser with RMS threshold (0.015) — auto-stops after 1.4s silence
- **Playback**: Audio chunks queued as `<audio>` elements, played sequentially

### Always-On Display (AOD)

After 10 minutes of inactivity (configurable: 5m/10m/15m), the screen transitions to the Vesper AOD:
- Pure black background (minimal power draw)
- Large clock (font-weight 100, very elegant)
- Date, subtle weather
- Breathing Vesper orb (48px, subtle glow)
- "say vesper to wake" pulsing hint

When wake word is detected or screen is tapped:
- AOD fades out (1.2s transition)
- Home page returns instantly
- Recording starts automatically if wake word triggered

### Settings Toggles

- Voice interaction on/off
- Wake word on/off
- TTS audio playback on/off
- Do Not Disturb (mutes audio)
- 3D Animations on/off
- Weather panel on/off
- Calendar events on/off
- AOD mode on/off + timeout
- Server URL (change to Tailscale IP when off-network)
- Install SSL Cert (one-tap download of server cert)
- Connection Test

---

## Mac-Side Files (`/Users/tanmay/vesper_agent/`)

| File | Purpose |
|------|---------|
| `vesper_capture.py` | Screen capture daemon — uses Apple Vision OCR, triggers on screen activity, sends to server |
| `vesper_audio.py` | 24/7 microphone recorder — saves WAV chunks, sends to /transcribe |
| `vesper_screen.py` | Secondary screen capture helper |
| `vesper_phone.py` | Phone call detection and recording |
| `vesper_meeting.sh` | Meeting mode — better audio processing |
| `gmail_realtime.py` | IMAP IDLE listener — instant Gmail ingestion |
| `file_watcher.py` | Watches export queue, SCP's to server |
| `screenpipe_sync.py` | Syncs screenpipe-rs output to server |
| `export_imessages.sh` | AppleScript export of iMessage database |
| `export_gmail.sh/py` | Gmail export via IMAP |
| `export_browser.sh` | Safari/Chrome history export |
| `export_calendar.sh` | Apple Calendar export via osascript |
| `export_contacts.sh` | Contacts export via osascript |
| `export_whatsapp.sh/py` | WhatsApp chat history export |
| `export_instagram.py` | Instagram data export parser |
| `export_notes.sh` | Apple Notes export |
| `export_reminders.sh` | Reminders export |
| `export_terminal.sh` | Terminal command history export |
| `send_email.py` | Sends emails via AppleScript (Vesper can send emails on your behalf) |
| `imessage_realtime.sh` | Near-real-time iMessage monitor |
| `com.vesper.gmail_realtime.plist` | LaunchAgent for gmail_realtime.py |
| `indexed_state.json` | Tracks which files have been ingested |
| `screenpipe_state.json` | Screenpipe sync state |
| `frontend/kiosk.html` | Kiosk web app source |
| `frontend/deploy.sh` | Deploy kiosk to server |

---

## Server-Side Files (`/home/tanmay/vesper/`)

### `pipelines/`

| File | Purpose |
|------|---------|
| `file_receiver.py` | **THE MAIN SERVER** — Flask HTTPS API, ChromaDB writer, all AI inference |
| `voice_ui.html` | Browser-based chat UI (served at /ui) — full chat interface with history |
| `kiosk.html` | Kiosk frontend (served at /kiosk) |
| `morning_briefing.py` | 8 AM daily WhatsApp briefing assembler |
| `night_batch.py` | 2 AM audio re-transcription with larger Whisper |
| `proactive_alerts.py` | 30-min check — pushes important notifications |
| `memory_client.py` | HTTP client library for all ingest scripts |
| `memory.py` | Legacy memory utilities |
| `ingest_browser.py` | Browser history → ChromaDB |
| `ingest_calendar.py` | Calendar events → ChromaDB |
| `ingest_contacts.py` | Address book → ChromaDB |
| `ingest_email.py` | Email → ChromaDB (IMAP-based) |
| `ingest_gmail.py` | Gmail backup ingestion |
| `ingest_imessages.py` | iMessage database → ChromaDB |
| `ingest_whatsapp.py` | WhatsApp export → ChromaDB |
| `ingest_instagram.py` | Instagram DMs/posts → ChromaDB (broken) |
| `ingest_instagram_smart.py` | Improved Instagram ingestion |
| `ingest_files.py` | File system scan → ChromaDB |
| `parse_instagram.py` | Parse Instagram ZIP export |
| `ask_vesper.py` | CLI tool to query Vesper |
| `email_tools.py` | Email sending utilities |
| `screenpipe_sync.py` | Receives screenpipe data from Mac |

### `openclaw/`

| File | Purpose |
|------|---------|
| `bot.js` | Main WhatsApp bot (Indian number) |
| `bot_us.js` | WhatsApp Business ingest bot (US number) |
| `config.json` | Indian bot config: phone, owner_jid, morning_jid |
| `config_us.json` | US bot config: phone |
| `history.json` | Persisted conversation histories per JID |
| `auth/` | Baileys session keys for Indian number |
| `auth_us/` | Baileys session keys for US number |

---

## Performance & Optimizations

### The Latency Journey

| Milestone | First Word Latency | What Changed |
|-----------|-------------------|--------------|
| Initial (phi4-mini CPU) | 9–25s | Baseline |
| Moved to dolphin-phi GPU | 3.5s | 22/32 layers on MX130 |
| Short system prompt | 2.8s | Cut from 25 tokens → 9 tokens |
| Comma-split TTS streaming | 2.0s | Fire audio at every pause, not end |
| Fast paths for common queries | <0.5s | No ChromaDB/LLM for time, identity, DOB |
| Voice prepare pre-compute | 1.7s | Embed while user still speaking |

### The Hardest Technical Problems

**1. ChromaDB Race Conditions / Filesystem Corruption**

The first architecture had multiple Python processes writing to ChromaDB simultaneously. This caused `hnswlib` segfaults that corrupted the EXT4 filesystem into read-only mode. The entire server had to be rebuilt.

*Solution*: Single-writer architecture. `file_receiver.py` is the only process that touches ChromaDB. A `threading.Lock()` serializes every operation. All other processes communicate via HTTP.

**2. Maxwell GPU (sm_5.0) CUDA Incompatibilities**

The server's MX130 GPU is Maxwell architecture. Almost every modern CUDA AI library requires sm_6.0+ (Pascal). Tested and confirmed broken:
- Kokoro TTS: `cuDNN EXECUTION_FAILED`
- CTranslate2 Whisper with CUDA: broken
- PyTorch GPU inference: limited

*Solution*: Adopted Ollama (which handles Maxwell correctly via llama.cpp), CPU Piper TTS, CPU faster-whisper. Accepted the speed tradeoffs.

**3. Server Deadlock (Port Bound but Not Responding)**

The server would occasionally deadlock — port 5000 was bound and showed LISTEN, but HTTP requests hung forever. The original watchdog checked only port binding (`ss -tlnp | grep :5000`), which passed even when deadlocked.

*Solution*: Changed watchdog to `curl -sk --max-time 4 https://127.0.0.1:5000/health`. If this fails (including timeout), the watchdog kills and restarts. Detects deadlocks within 5 minutes.

**4. WhatsApp Bot Auto-Replying to Contacts**

The bot was connected to the Indian number (personal number). With no owner check, every message from any contact (Arpita, family, friends) got an AI-generated reply. This was discovered when the bot sent "Yeah, I'm right here!" and "Alright, have a great day!" to Arpita mid-conversation.

*Solution*: `OWNER_JID` gate in `handleMessage`. If `OWNER_JID` is set, only messages from that JID get LLM responses. Everyone else is silently ingested. Default-deny when no owner configured.

**5. LLM Returning Raw ChromaDB Dumps**

The `/voice_fast` routing logic had `_is_msg` keywords including `"tell me"`, `"what did"`, `"whatsapp"`, `"text"` — words that appear in completely unrelated questions. "What else can you do in WhatsApp?" matched `_is_msg` → ChromaDB returned a screen OCR document → bot responded with raw `[Screen OCR | Electron] Debug Vesper local AI...`.

*Solution*: Massively tightened `_is_msg` to only explicit message-lookup phrases ("last text", "texted me", "message from"). Added capability question fast path. Made "Last message:" prefix conditional on the user actually asking for the last message.

---

## Features

### Currently Working

| Feature | Description |
|---------|-------------|
| **Voice conversation** | < 2s first word, natural streaming audio |
| **WhatsApp chat** | Full Vesper on WhatsApp from US number |
| **Morning briefing** | Daily 8 AM personalized summary |
| **Proactive alerts** | Notifies on important messages/emails |
| **Real-time Gmail** | Emails arrive in memory the moment they're received |
| **iMessage history** | Last 15 min latency, full conversation memory |
| **Screen activity memory** | What apps/windows you were using and when |
| **Contact lookup** | "What's Arpita's number?" → instant |
| **Calendar awareness** | "What events do I have?" → formatted upcoming events |
| **Location memory** | Stores GPS/place names from WhatsApp live location |
| **Anti-hallucination** | Refuses to answer personal questions without data |
| **DOB fast path** | "When was I born?" → instant, no LLM |
| **Relationship analysis** | "Analyze my relationship with Harsh" |
| **Voice notes** | WhatsApp voice notes → transcribed + answered |
| **Photo analysis** | WhatsApp photos → described by LLM |
| **Notes ingestion** | 23 Apple Notes stored and queryable |
| **Browser history** | What you read/watched/visited |
| **AOD kiosk** | Always-on display with clock, dims to black after idle |
| **Wake word** | "Hey Vesper" wakes kiosk from AOD |
| **Settings panel** | Toggle any feature via sidebar |
| **Memory count** | Live ChromaDB count display |
| **Pipeline status** | Visual health of all data pipelines |
| **SSL cert download** | One-tap cert installation for Android |
| **Night batch** | 2 AM audio quality improvement run |

### Memory Statistics (as of Session 16)

| Category | Approximate Count |
|----------|------------------|
| WhatsApp | ~1,700 |
| iMessages | ~240+ |
| Email (Gmail) | ~1,300 |
| Browser History | ~2,000 |
| Contacts | ~908 |
| Screen OCR | ~1,100 |
| Calendar | ~15 |
| Notes | ~25 |
| Manual / Location | ~50 |
| **Total** | **~12,000+** |

---

## Known Issues & Limitations

| Issue | Status | Notes |
|-------|--------|-------|
| Instagram ingest | ❌ Broken | JSON file corrupted at line 356333 |
| Calendar June queries | ⚠️ Partial | Month-aware filtering needed |
| "What has mom been texting" | ⚠️ Wrong | Returns openclaw group, not Maa's personal chats |
| MX130 TTS GPU | ❌ Won't fix | Maxwell incompatible |
| Occasional server deadlock | ⚠️ Managed | 5-min watchdog auto-recovers |
| WhatsApp Baileys ToS risk | ⚠️ Known | Unofficial API, could be blocked |
| SSL cert manual install | ⚠️ Workaround | Self-signed, needs per-device install |

---

## Future Scope

### Immediate (Next Few Sessions)
- Fix Instagram ingest (re-download or robust JSON parsing)
- Fix calendar month-aware queries
- Fix Maa WhatsApp source filter
- Tailscale HTTPS for fully-trusted cert (no manual install)

### Short-Term
- **Audio diary**: Process vesper_audio.py recordings → tagged memories by context
- **Proactive memory**: Vesper surfaces relevant memories unprompted ("You have a meeting in 30 min")
- **Arpita birthday note**: Format raw birthday data through LLM → natural language
- **Multi-turn voice on kiosk**: Keep conversation context across multiple exchanges
- **Better activity queries**: More accurate "what have I been working on" using recent screen data

### Long-Term Vision
- **Upgrade hardware**: GTX 1650+ GPU → Kokoro TTS on GPU, Whisper medium on GPU → <1s latency
- **Local document RAG**: PDFs, books, notes → searchable via Vesper
- **Relationship graphs**: Who do you talk to most? Who's drifting away?
- **Health integration**: Sleep, steps, location patterns
- **Predictive memory**: "You usually order food on Thursdays when you work late"
- **Multi-user support**: Shared Vesper for household
- **iOS/Android native app**: Better wake word, background listening
- **Emotional context**: Track mood patterns from message sentiment over time

---

## Security & Privacy

- All data stays on your home network (LAN 10.0.0.120)
- HTTPS with self-signed cert (Tailscale-trusted cert in progress)
- ChromaDB data stored on local HDD at `/mnt/hdd/vesper_memory/`
- No telemetry, no cloud storage, no third-party APIs except:
  - wttr.in (weather, anonymous)
  - DuckDuckGo search (for web queries, anonymous)
  - Google (Web Speech API wake word detection — only the trigger word, not your query)
  - WhatsApp (messages routed through Meta's servers — this is a known tradeoff)

---

## Quick Reference

```bash
# SSH to server
ssh -i ~/.ssh/vesper_key tanmay@10.0.0.120

# Server status
curl -sk https://127.0.0.1:5000/health

# Server logs
tail -f /home/tanmay/vesper/logs/file_receiver.log

# Restart server (graceful)
kill -15 $(ss -tlnp | grep :5000 | grep -oP 'pid=\K[0-9]+' | head -1)
sleep 5
nohup python3 /home/tanmay/vesper/pipelines/file_receiver.py >> /home/tanmay/vesper/logs/file_receiver.log 2>&1 &

# Bot status
tail -f /home/tanmay/vesper/logs/openclaw.log
tail -f /home/tanmay/vesper/logs/openclaw_us.log

# Morning briefing test
python3 /home/tanmay/vesper/pipelines/morning_briefing.py

# Kiosk URL
https://10.0.0.120:5000/kiosk

# Voice UI URL
https://10.0.0.120:5000/ui
```

---

*Built by Tanmay Pramanick — a personal AI for one person, running privately on hardware you own.*
