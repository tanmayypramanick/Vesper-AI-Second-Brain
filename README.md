# VESPER — Your Personal Life AI

> *"An AI that knows everything about you — privately, locally, always-on."*

Vesper is a fully private, self-hosted personal AI built by **Tanmay Pramanick**. It ingests your entire digital life in real-time — every message, email, calendar event, browser visit, screen activity, voice recording, and phone call — stores it in a local vector database, and lets you query it through voice, WhatsApp, or a beautiful kiosk display.

**Everything runs on your own hardware. No data ever leaves your home network.**

---

## What Can You Ask Vesper?

These are real queries that work right now:

| Query | How it answers |
|-------|----------------|
| *"What was I working on yesterday afternoon?"* | Screen OCR → app/window activity log |
| *"What did Arpita say about the trip?"* | WhatsApp semantic search |
| *"What's the last email from Google?"* | Gmail realtime index |
| *"What meetings do I have this week?"* | Apple Calendar sync |
| *"What did I browse last Tuesday?"* | Chrome/Safari history |
| *"What's Harsh's phone number?"* | Contacts database |
| *"Summarize my last 10 messages from mom"* | iMessage export |
| *"Analyze my relationship with [person]"* | Cross-source sentiment analysis |
| *"What notes did I write about the project?"* | Apple Notes index |
| *"What's the weather right now?"* | wttr.in (anonymous) |
| *"What time is it?"* | <50ms, no LLM involved |

All of this answered in **< 2 seconds**, spoken aloud, with a natural voice.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          YOUR HOME NETWORK                           │
│                                                                      │
│  ┌──────────────────┐        ┌──────────────────────────────────┐   │
│  │    MacBook Pro    │        │        VESPER SERVER             │   │
│  │  (data capture)  │        │      (Flask HTTPS + AI)          │   │
│  │                  │        │                                  │   │
│  │ ◆ vesper_capture ├──────► │ ◆ file_receiver.py               │   │
│  │   (screen OCR)   │  HTTP  │   THE ONLY ChromaDB writer       │   │
│  │ ◆ vesper_audio   │  POST  │                                  │   │
│  │   (24/7 mic)     │        │ ◆ ChromaDB (vesper_life)         │   │
│  │ ◆ Gmail IMAP     │        │   12,000+ memories               │   │
│  │   (real-time)    │        │   /mnt/hdd/vesper_memory/        │   │
│  │ ◆ iMessage cron  │        │                                  │   │
│  │ ◆ browser export │        │ ◆ Ollama LLM + Embeddings        │   │
│  │ ◆ calendar cron  │        │   dolphin-phi (22/32 GPU)        │   │
│  │ ◆ contacts sync  │        │   qwen3:4b (CPU)                 │   │
│  │ ◆ Apple Notes    │        │   nomic-embed-text (GPU)         │   │
│  └──────────────────┘        │                                  │   │
│                              │ ◆ Piper TTS (CPU, ~62-220ms)     │   │
│  ┌──────────────────┐        │ ◆ distil-whisper STT (CPU)       │   │
│  │   OnePlus Nord   │        │                                  │   │
│  │  (Kiosk Display) │        │ ◆ openclaw/bot.js (WhatsApp)     │   │
│  │                  │◄───────│   Indian # (bot) + US # (ingest) │   │
│  │ ◆ Fully Kiosk    │ HTTPS  │                                  │   │
│  │ ◆ Three.js Orb   │        └──────────────────────────────────┘   │
│  │ ◆ Wake word      │                        ▲                      │
│  │ ◆ AOD screen     │          WhatsApp ─────┘                      │
│  └──────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────┘
```

**Core design principle**: The server is the single source of truth and the **only** process that ever touches ChromaDB. This single-writer architecture was forced by a critical discovery early on — ChromaDB's hnswlib C++ extension segfaults when multiple processes write simultaneously, which caused filesystem corruption that required a full server rebuild. Everything now communicates via HTTP.

---

## Hardware

### Server (Primary AI Compute)

| Component | Spec |
|-----------|------|
| CPU | Intel Core i5-8250U (8th gen, 8 threads) |
| RAM | 15 GB DDR4 |
| GPU | NVIDIA MX130 (Maxwell, sm_5.0, 2 GB VRAM) |
| Storage | External HDD at `/mnt/hdd/` for ChromaDB |
| OS | Ubuntu Linux |

**Important GPU constraint**: The MX130 is Maxwell architecture (sm_5.0). Almost every modern CUDA AI library requires sm_6.0+. This shaped every model choice:
- Kokoro TTS → `cuDNN EXECUTION_FAILED` on Maxwell ❌
- CTranslate2 CUDA Whisper → broken on Maxwell ❌  
- Ollama LLM inference → works correctly ✅ (llama.cpp handles Maxwell)

So TTS and STT run on CPU. This forced aggressive optimization to still hit <2s latency.

### Mac (Data Capture)
- Apple Silicon MacBook Pro
- Role: screen capture, microphone recording, exporting iMessages/contacts/calendar/browser
- Purely a data collector — never runs inference

### OnePlus Nord (Kiosk Display)
- 6.44" screen, 1080×2400 px — permanently mounted on desk, always plugged in
- Runs Fully Kiosk Browser in locked kiosk mode
- Acts as a dedicated, always-on Vesper voice terminal and smart display

---

## Data Pipelines

All pipelines ingest to ChromaDB via `file_receiver.py`. Every memory has `category`, `source`, and `timestamp` metadata enabling time-filtered queries.

### Real-Time (Instant or Near-Instant)

| Source | Method | Latency | Category |
|--------|---------|---------|----------|
| Gmail | IMAP IDLE — server-push | < 5 seconds | `email_received` |
| WhatsApp (Indian number) | Baileys bot — real-time hook | Instant | `whatsapp` |
| WhatsApp Business (US number) | bot_us.js — ingest-only | Instant | `whatsapp` |
| Screen Activity | Apple Vision OCR — activity-triggered | ~2s | `screen_ocr` |
| Audio recording | `vesper_audio.py` — continuous capture | Ongoing | `audio` *(see below)* |

### Scheduled (Cron on Mac)

| Source | Schedule | Category |
|--------|----------|----------|
| iMessages | Every 15 min | `imessage` |
| Browser history | Every 30 min | `browser` |
| Calendar events | Hourly | `calendar` |
| Contacts | Daily 3 AM | `contact` |
| WhatsApp export | Every 30 min | `whatsapp` (backup) |

### Batch / Night Pipeline

| Job | When | Purpose |
|-----|------|---------|
| Audio re-transcription | 2 AM daily | Re-transcribes day's WAVs with larger whisper-medium model for quality |
| Morning briefing | 8 AM daily | Assembles and sends personalized WhatsApp summary |
| Proactive alerts | Every 30 min | Scans for urgent/important messages and emails — pushes WA notification |

---

## AI Models Stack

### LLM

| Model | Use | GPU Layers | Speed | Purpose |
|-------|-----|-----------|-------|---------|
| **dolphin-phi 2.7B** | Voice queries | 22/32 GPU | ~15.5 tok/s | Fast, uncensored (no refusals on personal questions) |
| **qwen3:4b** | Deep/analytical queries | 0 (CPU) | ~5 tok/s | Better reasoning, 32K context |

**Why dolphin-phi?** Microsoft Phi-2-based, uncensored, small enough to partially fit on the MX130 GPU. No hedging when you ask personal questions about relationships, health, or private matters.

**Why qwen3 for deep queries?** Used only for `/model qwen3` mode on WhatsApp — when you want a thorough analytical answer and don't need sub-2s latency.

### Embedding

| Model | Inference | VRAM | Notes |
|-------|-----------|------|-------|
| **nomic-embed-text** (768-dim) | GPU, always loaded | 555 MB | All semantic search — never evicted |

### Speech-to-Text

| Model | Inference | RTF | Notes |
|-------|-----------|-----|-------|
| **distil-whisper-small.en** | CPU int8 | 0.74x | Real-time queries — 3s audio → ~2.2s transcript |
| **whisper-medium** | CPU | ~1.8x | Night batch only — higher quality re-transcription |

### Text-to-Speech

| Model | Inference | Speed |
|-------|-----------|-------|
| **Piper** (hfc_female-medium) | CPU | 62–220ms per sentence |
| Supertonic-3 | CPU | 330–830ms (fallback) |

### VRAM Budget

```
nomic-embed-text:         555 MB  (always loaded)
dolphin-phi (22/32 GPU): ~1,100 MB  (loaded on first voice query)
──────────────────────────────────────────────────
Peak usage:              ~1,655 MB / 2,048 MB    (393 MB headroom)
```

---

## Voice Pipeline (< 2s First Word)

This was the hardest part to build. First iteration latency was 9–25 seconds. Current: **1.5–2.5s**.

### How It Works

```
User says "Hey Vesper"
  → Web Speech API (wake word, continuous, lightweight)
  → VAD detects speech start (RMS > 0.015)
  → MediaRecorder starts (WebM/Opus, 16kHz)
  → /voice_prepare called while user is still talking
       → server pre-embeds partial transcript
       → ChromaDB results cached
  → VAD detects 1.4s silence → recording stops
  → Full audio blob → base64 → POST /voice_fast
       → Whisper STT → question text
       → nomic-embed (use cached embed if ready)
       → ChromaDB cosine search
       → Routing logic (fast path / data path / LLM path)
       → dolphin-phi generates response
       → At every comma/period → Piper TTS chunk
       → SSE stream:
           data: T:<base64_text>   ← display
           data: A:<base64_wav>    ← play
  → AudioContext queues WAV chunks as they arrive
  → First word plays in < 1s from LLM start
```

### The Key Optimization: Comma-Split TTS Streaming

Instead of waiting for the full LLM response before synthesizing speech, Vesper fires TTS at every natural pause (comma, period, semicolon). The first audio chunk plays the moment the LLM produces its first complete phrase — typically 600ms after generation starts.

This alone dropped memory-query latency from **5.3s → 2.0s**.

### Query Routing Hierarchy (`/voice_fast`)

1. **Fast paths** (zero ChromaDB, zero LLM) — < 50ms:
   - Time, date, weather
   - Identity ("who are you")
   - Date of birth (hardcoded for instant answer)
   - Greetings
   - Memory count
   - Capability questions

2. **Data lookup paths** (ChromaDB, no LLM) — 200–800ms:
   - Message lookup (last text/WhatsApp from someone)
   - Email lookup
   - Contact lookup (phone, email, address)
   - Calendar (upcoming events)
   - Screen activity
   - Location (from stored GPS data)

3. **LLM synthesis paths** (ChromaDB + LLM) — 1.5–2.5s:
   - Personal complex queries → dolphin-phi (uncensored)
   - General knowledge → qwen3-ask
   - Web search → DuckDuckGo + LLM

4. **Anti-hallucination guard**: If a personal-life question (meals, bank, gym, sleep) has zero matching memories → explicit refusal. Vesper will not make up facts about your life.

### Latency Journey

| Milestone | First-word latency | What changed |
|-----------|--------------------|--------------|
| Initial build (phi4-mini CPU) | 9–25s | Baseline |
| Moved LLM to GPU (dolphin 22/32 layers) | 3.5s | |
| Shorter system prompt (25 tokens → 9) | 2.8s | |
| Comma-split TTS streaming | 2.0s | Biggest single gain |
| Fast paths for common queries | < 0.5s | No LLM at all |
| `/voice_prepare` pre-computation | 1.7s | Embed while user speaks |

---

## WhatsApp Bot (OpenClaw)

Vesper lives in your WhatsApp. Built on **Baileys** (Node.js unofficial WhatsApp Web protocol).

### Two-Bot Architecture

**Indian Number Bot** (`server/openclaw/bot.js`):
- Connected to personal Indian WhatsApp
- Has an `OWNER_JID` gate — **only responds to messages from the US number** (prevents replying to family/friends)
- Silently ingests ALL received messages to ChromaDB
- Runs an HTTP API on port 5001 for sending outbound messages

**US Number Bot** (`server/openclaw/bot_us.js`):
- Connected to WhatsApp Business (US number)
- Ingest-only — never replies to anyone
- Captures all US-side conversations to ChromaDB

### Bot Capabilities

| Command | What it does |
|---------|-------------|
| Just send a message | Vesper answers via `/voice_fast` |
| Send a voice note | Transcribed by Whisper → Vesper answers |
| Send a photo | Described by LLM vision |
| *"remember [fact]"* | Stored directly to ChromaDB |
| `/remember [fact]` | Explicit memory store |
| `/model qwen3` | Switch to smarter (but slower) model |
| `/model dolphin` | Switch back to fast model |
| `/clear` | Clear conversation history |

Conversation history (last 6 exchanges) is persisted per-JID so multi-turn conversations work across session restarts.

---

## Kiosk Frontend (`client/frontend/kiosk.html`)

A single self-contained HTML file. Served from the Flask server. Runs in Fully Kiosk Browser on the Nord permanently.

### The 3D Orb

Built with Three.js (r128) and a custom GLSL fragment shader:
- **Inner ocean**: Procedural sine-wave displacement simulates water surface
- **Color shifts**: Deep blue → cyan rim glow at rest; green/teal during listening; white burst on wake
- **Wireframe overlay**: Subtle grid at 9% opacity
- **Particle cloud**: 220 floating dots orbiting the sphere
- **Breathing animation**: 0.6Hz pulse (scale 1.0–1.012)
- **Audio-reactive**: Scale increases with microphone RMS during recording

### Pages

| Page | Content |
|------|---------|
| **Home** | 3D orb, clock, weather, upcoming events, ambient context feed |
| **Memory** | Live ChromaDB count, breakdown by source |
| **System** | Server health, model status, pipeline indicators |
| **Settings** | All feature toggles — voice, wake word, TTS, AOD, DND |

### Always-On Display (AOD)

After 10 minutes idle, transitions to a minimal AOD:
- Pure black background (minimal OLED power draw)
- Large ultra-thin clock
- Breathing Vesper orb (48px, subtle glow)
- "say vesper to wake" pulsing hint

Wake word detection continues in AOD mode. Saying "Vesper" immediately brings the screen back and starts recording.

---

## Cron Jobs (Server)

All as user `tanmay`. The watchdog is the most important:

```
*/5 * * * *   curl /health or kill+restart file_receiver.py
*/2 * * * *   pgrep bot.js || start bot.js
*/15 * * * *  ingest_imessages.py
*/30 * * * *  ingest_gmail.py, ingest_browser.py, proactive_alerts.py
0  * * * *    ingest_calendar.py
0  8 * * *    morning_briefing.py
0  2 * * *    night_batch.py (whisper-medium re-transcription)
0  3 1 * *    Tailscale cert renewal + server restart
```

The watchdog uses `curl -sk --max-time 4 https://127.0.0.1:5000/health` — not just port binding. This detects deadlocks where the port is LISTEN but the server is frozen.

---

## Currently Building

These features are actively in development or partially implemented:

### 24/7 Audio Diary (`client/vesper_audio.py`)

`vesper_audio.py` is already running — it records a continuous audio stream from the Mac microphone, splitting into 30-second WAV chunks saved to `/mnt/hdd/vesper_audio/`.

**What's done**: Recording, storage, night-batch re-transcription with whisper-medium.

**What's being built**: The pipeline to turn these recordings into queryable memories:
- Speaker diarization (pyannote) to separate who is talking
- Context tagging (was this a meeting? a call? background TV?)
- Semantic chunking — group sentences into meaningful memories, not just fixed-size chunks
- Store as `category: audio_diary` with speaker, context, timestamp metadata
- Enable queries like: *"What did I say about the project during the call on Monday?"*, *"What conversations did I have this week?"*, *"Who called me yesterday?"*

This will be the most personal and powerful data source — a complete record of your spoken life.

### Proactive Memory Surface

Vesper currently only responds to queries. The next major mode is **proactive** — Vesper surfaces relevant memories without being asked:
- *"You have a call in 20 minutes (from your calendar)"*
- *"Priya texted twice asking about [topic] — you haven't replied"*
- *"Based on your screen activity, you've been working on [project] for 3 hours"*

`proactive_alerts.py` is the early version of this — it already scans for urgent/unanswered messages every 30 minutes and pushes WhatsApp alerts.

### Multi-Turn Voice Conversation

Currently each kiosk interaction is stateless. Building persistent conversation history into the kiosk so you can say:
- *"Tell me about my emails from last week"*
- *"Which ones are from Google?"* (follow-up, without restating context)
- *"Summarize that one"*

### Relationship Memory Graph

A structured view of your relationship with each person in your life — built from all their messages, iMessages, WhatsApp chats, emails:
- Message frequency over time
- Common topics
- Sentiment trend
- "You haven't talked to [person] in 3 weeks"

### Hardware Upgrade Path

The MX130 GPU is the biggest bottleneck. With a GTX 1650 or better:
- Kokoro TTS on GPU → <100ms synthesis, human-quality voice
- Whisper medium on GPU → <0.5s transcription
- Entire voice pipeline → **< 1 second** total latency

---

## The Hardest Technical Problems Solved

### 1. ChromaDB Race Conditions / Filesystem Corruption

First architecture: multiple Python processes writing to ChromaDB simultaneously. `hnswlib` segfaulted mid-write, corrupting the EXT4 filesystem to read-only. The entire server had to be rebuilt from scratch.

**Solution**: Single-writer architecture. `file_receiver.py` is the only process that ever touches ChromaDB. A `threading.Lock()` serializes every operation. All other scripts POST to the HTTP API.

### 2. Maxwell GPU CUDA Incompatibility

Almost every modern CUDA AI library requires sm_6.0+ (Pascal). The MX130 is sm_5.0. Every "obvious" choice (Kokoro TTS, CTranslate2 Whisper) failed with CUDA errors.

**Solution**: Adopted Ollama (llama.cpp handles Maxwell correctly) for LLM inference. CPU Piper for TTS. CPU faster-whisper for STT. Accepted the tradeoffs and optimized around them.

### 3. Server Deadlock Detection

The server would deadlock — port 5000 bound and LISTEN, but every HTTP request hung indefinitely. Original watchdog checked `ss -tlnp | grep :5000`, which passed even when deadlocked.

**Solution**: Changed watchdog to `curl -sk --max-time 4 https://127.0.0.1:5000/health`. Timeout → kill + restart. Now detects deadlocks within 5 minutes.

### 4. AudioContext Suspension (Chrome Android)

The kiosk's `AudioContext` was created inside the Web Speech API wake-word callback — outside a direct user gesture. Chrome Android creates AudioContext in `suspended` state in this case. WAV chunks were decoded and scheduled but the audio clock never advanced — complete silence.

**Solution**: Pre-warm `AudioContext` during `onFirstTouch` (the first tap to enter fullscreen). Both `queueWav()` and `playTone()` explicitly `await ac.resume()` before scheduling audio sources.

### 5. WhatsApp Bot Auto-Replying to Contacts

With no owner gate, every message from any contact — family, friends, girlfriend — got an AI-generated reply. Discovered when the bot responded to a personal conversation with "Yeah, I'm right here!" and "Alright, have a great day!" to Arpita.

**Solution**: `OWNER_JID` gate in `handleMessage()`. Only messages from the designated US number get LLM responses. Everyone else is silently ingested. Hardened default: deny-all when no owner configured.

---

## Memory Statistics

| Category | Count |
|----------|-------|
| WhatsApp | ~1,700 |
| Email (Gmail) | ~1,300 |
| Browser History | ~2,000 |
| Contacts | ~908 |
| Screen OCR | ~1,100+ |
| iMessages | ~240+ |
| Notes | ~25 |
| Calendar | ~15 |
| Manual / Location | ~50 |
| **Total** | **~12,000+** |

---

## Known Issues

| Issue | Status |
|-------|--------|
| Instagram ingest | ❌ Broken — JSON export corrupted at line 356333 |
| Calendar month-specific queries | ⚠️ Partial — needs month-aware timestamp filter |
| MX130 GPU TTS | ❌ Won't fix — Maxwell incompatible with cuDNN |
| WhatsApp Baileys ToS | ⚠️ Known risk — unofficial API |
| SSL cert manual install | ⚠️ Workaround — self-signed, per-device install needed |

---

## Security & Privacy

- All data stays on your home LAN. Zero cloud.
- HTTPS with self-signed cert (Tailscale-trusted cert in progress)
- ChromaDB data stored locally at `/mnt/hdd/vesper_memory/`
- No telemetry, no external AI APIs
- Only external calls: wttr.in weather (anonymous), DuckDuckGo search (anonymous), Google Web Speech API for wake word detection only (not your query), WhatsApp (Meta's servers — known tradeoff for convenience)

---

## Repo Structure

```
vesper/
├── client/                    # Mac-side code
│   ├── vesper_capture.py      # Screen OCR → server
│   ├── vesper_audio.py        # 24/7 mic recording
│   ├── vesper_screen.py       # Screen capture helper
│   ├── vesper_phone.py        # Phone call detection
│   ├── gmail_realtime.py      # IMAP IDLE email watcher
│   ├── file_watcher.py        # Export queue watcher
│   ├── export_*.sh/py         # One-shot data exporters
│   ├── com.vesper.gmail_realtime.plist   # LaunchAgent
│   └── frontend/
│       └── kiosk.html         # Complete kiosk UI (Three.js, SSE, VAD)
│
└── server/                    # Server-side code
    ├── file_receiver.py       # Flask server, ChromaDB, all AI inference (~2,600 lines)
    ├── morning_briefing.py    # Daily 8 AM WhatsApp summary
    ├── night_batch.py         # 2 AM audio quality re-run
    ├── proactive_alerts.py    # Priority message/email push
    ├── memory_client.py       # HTTP client for ingest scripts
    ├── ingest_*.py            # Per-source ingest scripts
    └── openclaw/
        ├── bot.js             # Indian number WhatsApp bot (Baileys)
        └── bot_us.js          # US number ingest bot
```

---

## Quick Start (Adapting for Your Own Use)

This is a personal system built around specific hardware and data sources. To adapt it:

1. **Server**: Any Linux machine with 8+ GB RAM and an Nvidia GPU (sm_6.0+ recommended). Install Ollama, pull `nomic-embed-text` and `dolphin-phi`. Run `file_receiver.py`.

2. **Mac client**: macOS required for iMessage/Contacts/Calendar exports. Set `VESPER_URL` to your server IP in each script.

3. **WhatsApp**: Requires a Baileys session (scan QR code). Adjust `OWNER_JID` in `config.json` to your phone number.

4. **Kiosk**: Any device with Chrome. Navigate to `https://<server>:5000/kiosk`. Android with Fully Kiosk Browser for permanent desk display.

---

## Vision

The long-term goal is an AI that knows you better than any cloud product ever could — because it has actually lived alongside you.

With a better GPU and the audio diary pipeline complete, Vesper will have:
- A complete transcript of everything you said, every call, every meeting — all queryable and private
- Real-time awareness of your emotional state, work patterns, relationship health
- Proactive nudges: *"You've been heads-down for 4 hours — Arpita texted"*, *"You have a flight in 3 hours and traffic is bad"*
- Predictive patterns: *"You usually feel productive after your morning walk"*, *"Your busiest coding days are Tuesdays"*
- Complete relationship graphs — not just "what did they say" but "how has this relationship evolved over 6 months"

This is what personal AI should be: not a generic assistant trained on everyone's data, but a deeply personal one trained exclusively on yours, running on hardware you own, that grows smarter about you specifically with every passing day.

---

*Built by Tanmay Pramanick — a personal AI for one person, running privately on hardware you own.*
