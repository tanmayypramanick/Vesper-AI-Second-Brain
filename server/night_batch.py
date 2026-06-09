#!/usr/bin/env python3
"""
VESPER NIGHT BATCH — 02:00 cron job
  1. Load whisper-medium on CPU (better WER, runs at 2 AM when idle)
  2. Re-transcribe today's raw WAVs from /mnt/hdd/vesper_audio/YYYY-MM-DD/
  3. Delete old ChromaDB entries for those WAVs
  4. Insert turbo-quality transcriptions
  5. Delete raw WAV files (free HDD)
  6. Free VRAM

Cron line (server):
  0 2 * * * /usr/bin/python3 /home/tanmay/vesper/pipelines/night_batch.py >> /home/tanmay/vesper/logs/night_batch.log 2>&1
"""

import sys, logging, uuid, os, time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append('/home/tanmay/vesper')
from config import LOGS_PATH, EMBED_MODEL, MEMORY_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [NIGHT_BATCH] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("night_batch")

AUDIO_ROOT = Path("/mnt/hdd/vesper_audio")
WAV_KEEP_DAYS = 7   # safety: keep WAVs 7 days; delete after successful batch

def run():
    log.info("=== Night batch started ===")
    t0 = time.time()

    # ── Connect to ChromaDB ───────────────────────────────────────────────────
    import chromadb, ollama, threading

    client     = chromadb.PersistentClient(path=MEMORY_PATH)
    collection = client.get_or_create_collection(
        name="vesper_life",
        metadata={"hnsw:space": "cosine"},
    )
    chroma_lock = threading.Lock()

    def locked_add(docs, embs, metas, ids):
        with chroma_lock:
            collection.add(documents=docs, embeddings=embs, metadatas=metas, ids=ids)

    def locked_delete_ids(ids):
        with chroma_lock:
            collection.delete(ids=ids)

    def locked_get(where):
        with chroma_lock:
            return collection.get(where=where, include=["documents", "metadatas"])

    # ── Load whisper-large-v3-turbo on GPU ────────────────────────────────────
    log.info("Loading whisper-medium (CPU int8 — better WER than large-v3-turbo for conversational audio)…")
    from faster_whisper import WhisperModel
    # MX130 only supports float32 on CUDA — turbo float32 needs 3+ GB (too large)
    # CPU int8 is ~1.5 GB RAM; ~35 min for 30-min meeting, fine at 2 AM
    model = WhisperModel("medium", device="cpu", compute_type="int8")
    log.info("Whisper loaded.")

    # ── Find today's (yesterday's at 2 AM) audio folder ──────────────────────
    # Run at 02:00 → process yesterday's full day
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    processed_total = 0
    failed_total = 0

    for date_str in [yesterday, today]:
        date_dir = AUDIO_ROOT / date_str
        if not date_dir.exists():
            log.info(f"No audio folder for {date_str} — skipping")
            continue

        wav_files = sorted(date_dir.glob("*.wav"))
        if not wav_files:
            log.info(f"{date_str}: no WAV files — skipping")
            continue

        log.info(f"{date_str}: processing {len(wav_files)} WAV files")

        for wav in wav_files:
            fname = wav.name  # e.g. 2026-05-28_22-30-00_mic.wav
            # Parse source from filename: last part before .wav
            parts = fname.rsplit("_", 1)
            source = parts[1].replace(".wav", "") if len(parts) == 2 else "mic"

            # Reconstruct approximate timestamp from filename
            try:
                ts_part = fname[:19].replace("_", "T").replace("-", ":", 2)
                # e.g. 2026-05-28T22:30:00 (but dashes in date stay)
                # Actually: 2026-05-28_22-30-00 → we need 2026-05-28T22:30:00
                ts_part = fname[:10] + "T" + fname[11:19].replace("-", ":")
            except Exception:
                ts_part = datetime.utcnow().isoformat()[:19]

            try:
                segs, info = model.transcribe(
                    str(wav),
                    beam_size=5,
                    language="en",
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                text = " ".join(s.text.strip() for s in segs).strip()

                if not text:
                    log.info(f"  {fname}: silence/noise — skipped")
                    wav.unlink()
                    continue

                # Delete old small-model entry for this timestamp+source
                try:
                    res = locked_get(where={"source": f"audio:{source}"})
                    ids_to_del = []
                    for eid, meta in zip(res.get("ids", []), res.get("metadatas", [])):
                        if meta.get("timestamp", "").startswith(ts_part[:16]):
                            ids_to_del.append(eid)
                    if ids_to_del:
                        locked_delete_ids(ids_to_del)
                        log.info(f"  {fname}: deleted {len(ids_to_del)} old entry(ies)")
                except Exception as del_err:
                    log.warning(f"  {fname}: delete old error: {del_err}")

                # Insert turbo-quality transcription
                category = "audio_meeting" if source == "both" else "audio_life"
                mem = f"[Audio/{source} @ {ts_part}] {text}"
                emb = ollama.embeddings(model=EMBED_MODEL, prompt=mem[:1500])["embedding"]
                locked_add(
                    docs=[mem], embs=[emb],
                    metas=[{
                        "category":  category,
                        "source":    f"audio:{source}",
                        "timestamp": ts_part,
                        "date":      date_str,
                        "model":     "medium",
                    }],
                    ids=[str(uuid.uuid4())],
                )

                log.info(f"  {fname}: {len(text)}c stored (dur={info.duration:.1f}s, lang={info.language})")
                wav.unlink()
                processed_total += 1

            except Exception as e:
                log.error(f"  {fname}: ERROR — {e}")
                failed_total += 1

        # Remove date dir if now empty
        try:
            if not any(date_dir.iterdir()):
                date_dir.rmdir()
                log.info(f"{date_str}: directory cleaned up")
        except Exception:
            pass

    # ── Clean up old WAV folders (>WAV_KEEP_DAYS old, shouldn't exist but safety) ─
    for d in AUDIO_ROOT.iterdir():
        if not d.is_dir():
            continue
        try:
            folder_date = datetime.strptime(d.name, "%Y-%m-%d")
            age_days = (datetime.now() - folder_date).days
            if age_days > WAV_KEEP_DAYS:
                import shutil
                shutil.rmtree(d)
                log.info(f"Purged stale folder: {d.name} ({age_days} days old)")
        except ValueError:
            pass  # not a date-named folder

    # ── Free VRAM ─────────────────────────────────────────────────────────────
    del model
    log.info("Model released.")

    elapsed = time.time() - t0
    log.info(f"=== Night batch done: {processed_total} processed, {failed_total} failed, {elapsed:.1f}s ===")

if __name__ == "__main__":
    run()
