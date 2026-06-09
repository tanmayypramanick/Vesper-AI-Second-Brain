#!/usr/bin/env python3
"""
vesper_phone.py — 24/7 life-logging from phone (Termux/Android or a-Shell/iOS)
Records 15s chunks, VAD energy filter, sends to /transcribe on server.
No Mac, no speechbrain. All non-silent audio stored as audio_life.

Setup (Termux):
  pkg install python portaudio libsndfile
  pip install sounddevice soundfile numpy requests

Setup (a-Shell iOS):
  pip install sounddevice soundfile numpy requests

Run:
  python3 vesper_phone.py
  python3 vesper_phone.py --server http://10.0.0.120:5000  # LAN
  python3 vesper_phone.py --server http://100.123.15.32:5000  # Tailscale (if port open)

Background (Termux):
  nohup python3 vesper_phone.py > ~/vesper_phone.log 2>&1 &
"""

import argparse, base64, io, logging, os, signal, sys, time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [phone] %(levelname)s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("vesper_phone")

# ── Config ────────────────────────────────────────────────────────────────
DEFAULT_SERVER  = "http://10.0.0.120:5000"
CHUNK_SECS      = 15        # seconds per audio chunk
SAMPLE_RATE     = 16000     # Hz
CHANNELS        = 1         # mono
VAD_THRESHOLD   = 0.008     # RMS energy floor (skip silence)
MAX_RETRIES     = 3
RETRY_DELAY     = 2.0       # seconds between retries

# ── Imports ───────────────────────────────────────────────────────────────
try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    import requests
except ImportError as e:
    log.error(f"Missing dependency: {e}")
    log.error("Install: pip install sounddevice soundfile numpy requests")
    sys.exit(1)

_running = True

def _signal_handler(sig, frame):
    global _running
    log.info("Shutting down…")
    _running = False

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def record_chunk(duration: float = CHUNK_SECS) -> np.ndarray:
    """Record `duration` seconds of mono audio at SAMPLE_RATE."""
    frames = int(duration * SAMPLE_RATE)
    audio  = sd.rec(frames, samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="float32", blocking=True)
    return audio.flatten()


def rms_energy(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio ** 2)))


def audio_to_b64(audio: np.ndarray) -> str:
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, subtype="PCM_16", format="WAV")
    return base64.b64encode(buf.getvalue()).decode()


def send_to_server(server: str, audio: np.ndarray, ts: str) -> bool:
    b64  = audio_to_b64(audio)
    body = {
        "audio_b64": b64,
        "source":    "audio_life",
        "ts":        ts,
        "store":     True,
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(f"{server}/transcribe", json=body, timeout=60)
            if resp.ok:
                data = resp.json()
                text = data.get("text", "").strip()
                if text:
                    log.info(f"✓ stored: \"{text[:80]}\"")
                else:
                    log.debug("stored (no speech detected)")
                return True
            else:
                log.warning(f"Server {resp.status_code} (attempt {attempt})")
        except requests.exceptions.ConnectionError:
            log.warning(f"Cannot reach {server} (attempt {attempt})")
        except Exception as e:
            log.warning(f"Send error: {e} (attempt {attempt})")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


def main():
    parser = argparse.ArgumentParser(description="Vesper phone life-logger")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="Server base URL")
    parser.add_argument("--chunk",  type=float, default=CHUNK_SECS, help="Chunk duration (s)")
    parser.add_argument("--vad",    type=float, default=VAD_THRESHOLD, help="VAD RMS threshold")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    log.info(f"Vesper phone life-logger → {args.server}")
    log.info(f"Recording {args.chunk}s chunks at {SAMPLE_RATE} Hz, VAD threshold={args.vad}")
    log.info("Press Ctrl+C to stop")

    # Test server connection
    try:
        resp = requests.get(f"{args.server}/health", timeout=5)
        data = resp.json()
        log.info(f"Server OK — {data.get('memories', '?')} memories")
    except Exception as e:
        log.warning(f"Server health check failed: {e} (will keep retrying)")

    failed_count = 0
    chunk_num    = 0

    while _running:
        try:
            chunk_num += 1
            ts = datetime.utcnow().isoformat() + "Z"
            audio = record_chunk(args.chunk)
            energy = rms_energy(audio)

            if energy < args.vad:
                log.debug(f"Chunk {chunk_num}: silent (RMS={energy:.4f}) — skipped")
                continue

            log.info(f"Chunk {chunk_num}: RMS={energy:.4f} → sending…")
            ok = send_to_server(args.server, audio, ts)
            if ok:
                failed_count = 0
            else:
                failed_count += 1
                if failed_count >= 5:
                    log.error("5 consecutive failures — sleeping 60s")
                    time.sleep(60)
                    failed_count = 0

        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(2)

    log.info("Stopped.")


if __name__ == "__main__":
    main()
