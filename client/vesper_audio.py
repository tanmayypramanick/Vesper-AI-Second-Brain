#!/usr/bin/env python3 -u
"""
VESPER AUDIO — 24/7 life audio daemon

Modes:
  python3 vesper_audio.py              # continuous 24/7 life logging (daemon)
  python3 vesper_audio.py --enroll     # one-time voice enrollment (speak ~30s)
  python3 vesper_audio.py --test       # VAD calibration (shows RMS values)
  python3 vesper_audio.py --voice      # voice query mode (press Enter to start)

Architecture:
  • Records in 15s chunks (16 kHz mono)
  • Energy VAD skips silence (~70% of day)
  • speechbrain ECAPA-TDNN speaker verification:
      sim > 0.75  → audio_life    (your voice, direct memories)
      0.4 – 0.75  → audio_context (others talking; contextual weight)
      < 0.4       → DISCARD       (TV, strangers, ambient)
  • Conversational window: 120s after you speak → adjacent voices kept as audio_context
  • POSTs audio_b64 to server /transcribe (GPU whisper-small, 0.3s per 15s chunk)
  • Voice query mode: 3s streaming chunks → assemble utterance → /ask → speak response

Server URLs:
  LAN   http://10.0.0.120:5000  (primary)
  TS    http://100.123.15.32:5000 (Tailscale fallback)
"""

import sys, os, io, time, json, base64, logging, threading, math
import requests
import numpy as np
import sounddevice as sd
import soundfile as sf
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
SAMPLE_RATE    = 16000
CHUNK_SECS     = 15          # life-logging chunk length
VOICE_CHUNK    = 3           # voice-query streaming chunk length (seconds)
VAD_THRESHOLD  = 0.008       # RMS below this = silence; calibrate with --test
ENROLL_SECS    = 30          # seconds of speech to record for enrollment
PROFILE_PATH   = Path.home() / ".vesper_voice_profile.npy"
LOG_DIR        = Path.home() / "vesper_agent" / "logs"

VESPER_LAN     = "http://10.0.0.120:5000"
VESPER_TS      = "http://100.123.15.32:5000"

SIM_SELF       = 0.75        # above → audio_life
SIM_CONTEXT    = 0.40        # above → audio_context (within convo window)
CONVO_WINDOW   = 120         # seconds: others' voices within window = keep as context

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [audio] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "audio.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("vesper_audio")

# ── Server URL discovery ──────────────────────────────────────────────────────
_vesper_url = None

def get_vesper():
    global _vesper_url
    for url in [VESPER_LAN, VESPER_TS]:
        try:
            r = requests.get(f"{url}/health", timeout=3)
            if r.status_code == 200:
                _vesper_url = url
                return url
        except Exception:
            continue
    _vesper_url = None
    return None

# ── Audio helpers ─────────────────────────────────────────────────────────────

def record_chunk(secs: float) -> np.ndarray:
    """Record `secs` of audio as float32 mono at SAMPLE_RATE."""
    frames = int(SAMPLE_RATE * secs)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocking=True)
    return audio.flatten()

def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2)))

def encode_wav(audio: np.ndarray) -> str:
    """Encode float32 → PCM int16 WAV → base64 string."""
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, subtype="PCM_16", format="WAV")
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

# ── Speaker verification (speechbrain ECAPA-TDNN, CPU-only) ──────────────────
_sb_model = None
_sb_lock  = threading.Lock()

def _load_sb():
    global _sb_model
    if _sb_model is None:
        import speechbrain as sb
        from speechbrain.inference.speaker import EncoderClassifier
        _sb_model = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=str(Path.home() / ".cache" / "speechbrain"),
            run_opts={"device": "cpu"},
        )
    return _sb_model

def embed_voice(audio: np.ndarray) -> np.ndarray:
    """Return 192-dim ECAPA embedding for a mono float32 chunk."""
    import torch
    with _sb_lock:
        model = _load_sb()
        tensor = torch.tensor(audio).unsqueeze(0)  # [1, T]
        emb = model.encode_batch(tensor)            # [1, 1, 192]
        return emb.squeeze().numpy()

def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    a = a / (np.linalg.norm(a) + 1e-8)
    b = b / (np.linalg.norm(b) + 1e-8)
    return float(np.dot(a, b))

def load_profile() -> np.ndarray | None:
    if PROFILE_PATH.exists():
        return np.load(PROFILE_PATH)
    return None

def classify_speaker(audio: np.ndarray, profile: np.ndarray | None,
                     last_you_ts: float) -> str | None:
    """
    Returns:
      'audio_life'    — your voice
      'audio_context' — someone talking to you (within conversational window)
      None            — discard (TV/strangers/ambient)
    """
    if profile is None:
        return "audio_life"   # no profile enrolled → store everything

    emb = embed_voice(audio)
    sim = cosine_sim(emb, profile)

    if sim >= SIM_SELF:
        return "audio_life"

    in_convo_window = (time.time() - last_you_ts) < CONVO_WINDOW
    if sim >= SIM_CONTEXT and in_convo_window:
        return "audio_context"

    # < SIM_CONTEXT or outside window → discard
    return None

# ── Server comms ──────────────────────────────────────────────────────────────

def send_chunk(audio: np.ndarray, source: str, ts: str,
               url: str, category_hint: str | None = None):
    """
    POST audio chunk to /transcribe. Runs in daemon thread.
    category_hint is stored as a tag so the server can route it correctly.
    """
    try:
        b64 = encode_wav(audio)
        payload = {
            "audio_b64": b64,
            "source":    source,
            "ts":        ts,
            "store":     True,
        }
        r = requests.post(f"{url}/transcribe", json=payload, timeout=60)
        d = r.json()
        text = d.get("text", "")
        if text:
            log.info(f"[{source}] {text[:120]}")
        else:
            log.debug(f"[{source}] silence/noise (dur={d.get('duration_s','?')}s)")
    except Exception as e:
        log.warning(f"send_chunk error: {e}")

# ── Enrollment ────────────────────────────────────────────────────────────────

def enroll():
    print(f"\n🎙  VESPER VOICE ENROLLMENT")
    print(f"   Speak naturally for {ENROLL_SECS} seconds.")
    print(f"   This creates your voice fingerprint at {PROFILE_PATH}")
    print(f"   (Run again to overwrite.)\n")
    input("   Press Enter to start recording…")
    print(f"   Recording {ENROLL_SECS}s… speak!")

    audio = record_chunk(ENROLL_SECS)
    r = rms(audio)
    if r < VAD_THRESHOLD * 2:
        print(f"⚠️  RMS={r:.4f} — very quiet. Check your microphone.")

    print("   Generating voice embedding…")
    emb = embed_voice(audio)
    np.save(PROFILE_PATH, emb)
    print(f"✅  Voice profile saved → {PROFILE_PATH}")
    print(f"   RMS during enrollment: {r:.4f}")

# ── VAD calibration ───────────────────────────────────────────────────────────

def test_vad():
    print("\n🔉  VAD CALIBRATION")
    print("   Round 1: stay silent for 5 seconds…")
    silent = record_chunk(5)
    r_silent = rms(silent)
    print(f"   Silent RMS: {r_silent:.5f}")

    print("   Round 2: speak normally for 5 seconds…")
    speaking = record_chunk(5)
    r_speaking = rms(speaking)
    print(f"   Speaking RMS: {r_speaking:.5f}")

    suggested = (r_silent * 3 + r_speaking) / 4
    print(f"\n   Suggested VAD_THRESHOLD: {suggested:.5f}")
    print(f"   Current   VAD_THRESHOLD: {VAD_THRESHOLD:.5f}")
    if r_speaking < VAD_THRESHOLD:
        print("   ⚠️  Your voice is below threshold — lower VAD_THRESHOLD in the script!")
    elif r_silent > VAD_THRESHOLD * 0.5:
        print("   ⚠️  Background noise is high — consider raising VAD_THRESHOLD.")
    else:
        print("   ✅  Current threshold looks good.")

# ── Voice query mode ──────────────────────────────────────────────────────────

def voice_query_mode(url: str):
    """
    Press Enter to start a voice query.
    Records until 1.5s silence detected.
    Sends full utterance to /transcribe then /ask.
    """
    print("\n🎙  VESPER VOICE QUERY MODE  (Ctrl+C to exit)")
    print("   Press Enter to speak, stay quiet for 1.5s when done.\n")

    SILENCE_SECS   = 1.5
    MAX_QUERY_SECS = 30

    while True:
        try:
            input("   [Enter to speak] ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        print("   🔴 Listening…")
        all_chunks = []
        silence_chunks = 0
        SILENCE_CHUNKS_NEEDED = math.ceil(SILENCE_SECS / VOICE_CHUNK)

        start = time.time()
        while (time.time() - start) < MAX_QUERY_SECS:
            chunk = record_chunk(VOICE_CHUNK)
            r = rms(chunk)
            all_chunks.append(chunk)

            if r < VAD_THRESHOLD:
                silence_chunks += 1
                if silence_chunks >= SILENCE_CHUNKS_NEEDED:
                    break
            else:
                silence_chunks = 0

        if not all_chunks:
            continue

        full_audio = np.concatenate(all_chunks)
        dur = len(full_audio) / SAMPLE_RATE
        print(f"   Captured {dur:.1f}s — transcribing…")

        # Transcribe (store=False — voice queries are not stored as life-audio)
        try:
            b64 = encode_wav(full_audio)
            r = requests.post(f"{url}/transcribe",
                              json={"audio_b64": b64, "source": "voice_query",
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "store": False},
                              timeout=30)
            d = r.json()
            question = d.get("text", "").strip()
        except Exception as e:
            print(f"   Transcribe error: {e}")
            continue

        if not question:
            print("   (nothing detected)")
            continue

        print(f"   You: {question}")
        print("   Asking Vesper…")

        # Ask
        try:
            r = requests.post(f"{url}/ask",
                              json={"question": question, "model": "phi4-mini-cpu",
                                    "n": 5, "max_tokens": 120},
                              timeout=120)
            answer = r.json().get("answer", "(no answer)")
        except Exception as e:
            answer = f"(error: {e})"

        print(f"   Vesper: {answer}\n")
        # TTS: if /speak endpoint available use it; otherwise print only
        try:
            r = requests.post(f"{url}/speak",
                              json={"text": answer}, timeout=30)
            # Server returns audio — play via sounddevice
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("audio"):
                buf = io.BytesIO(r.content)
                data, sr = sf.read(buf, dtype="float32")
                sd.play(data, sr, blocking=True)
        except Exception:
            pass  # /speak not yet deployed — text output is fine

# ── Main daemon ───────────────────────────────────────────────────────────────

def daemon():
    log.info("=== VESPER AUDIO DAEMON starting ===")
    log.info(f"Chunk: {CHUNK_SECS}s | VAD threshold: {VAD_THRESHOLD} | "
             f"Profile: {'✅' if PROFILE_PATH.exists() else '⚠️  not enrolled'}")

    url = get_vesper()
    if not url:
        log.error("Vesper server unreachable — will retry every 30s")

    profile = load_profile()
    if profile is None:
        log.warning("No voice profile — run --enroll first. Storing all audio as audio_life.")

    last_you_ts = 0.0       # timestamp of last chunk classified as audio_life
    url_refresh_ts = 0.0    # periodically re-check server URL

    while True:
        # Refresh server URL every 5 min
        if time.time() - url_refresh_ts > 300:
            url = get_vesper()
            url_refresh_ts = time.time()

        if not url:
            time.sleep(30)
            url = get_vesper()
            continue

        ts = datetime.now(timezone.utc).isoformat()
        try:
            audio = record_chunk(CHUNK_SECS)
        except Exception as e:
            log.warning(f"Record error: {e}")
            time.sleep(2)
            continue

        r = rms(audio)
        log.debug(f"RMS={r:.4f}")

        if r < VAD_THRESHOLD:
            log.debug("Silence — skipped")
            continue

        # Speaker classification
        try:
            category = classify_speaker(audio, profile, last_you_ts)
        except Exception as e:
            log.warning(f"Speaker classify error: {e}")
            category = "audio_life"   # fail-open: store rather than discard

        if category is None:
            log.debug("Not your voice + outside convo window — discarded")
            continue

        if category == "audio_life":
            last_you_ts = time.time()

        # Send in background thread so recording rhythm never slips
        threading.Thread(
            target=send_chunk,
            args=(audio, "mic", ts, url),
            kwargs={"category_hint": category},
            daemon=True,
        ).start()

def main():
    args = sys.argv[1:]

    if "--enroll" in args:
        enroll()
    elif "--test" in args:
        test_vad()
    elif "--voice" in args:
        url = get_vesper()
        if not url:
            print("❌ Vesper server unreachable")
            sys.exit(1)
        voice_query_mode(url)
    else:
        daemon()

if __name__ == "__main__":
    main()
