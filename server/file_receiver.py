# ═══════════════════════════════════════════════════
# VESPER FILE RECEIVER - Production v8
# Design:
#   /ingest      — async (Mac file_watcher, fast ACK)
#   /store_memory — SYNCHRONOUS (ingest scripts, 200 after write)
#   /query       — synchronous search
# Single threading.Lock serializes all ChromaDB writes.
# max_workers=2: one for /ingest, one spare.
# ═══════════════════════════════════════════════════

from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import threading
import sys, logging, uuid, time, ollama, chromadb

sys.path.append('/home/tanmay/vesper')
from config import (
    LOGS_PATH, FILE_RECEIVER_PORT,
    EMBED_MODEL, MEMORY_PATH
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(f"{LOGS_PATH}/file_receiver.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("file_receiver")

client = chromadb.PersistentClient(path=MEMORY_PATH)
collection = client.get_or_create_collection(
    name="vesper_life",
    metadata={"hnsw:space": "cosine"}
)

app = Flask(__name__)

# ── Async pool for /ingest only (Mac file_watcher) ────────────────
_executor = ThreadPoolExecutor(max_workers=2)

# ── Single threading.Lock for ALL ChromaDB operations ─────────────
_chroma_lock = threading.Lock()
_llm_sem = threading.Semaphore(1)  # one LLM inference at a time
_nomic_warm_lock = threading.Lock()  # one background nomic-warm thread at a time

def _locked_add(documents, embeddings, metadatas, ids):
    with _chroma_lock:
        collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )

def _locked_delete(where):
    with _chroma_lock:
        collection.delete(where=where)

def _locked_query(embedding, n, where=None):
    with _chroma_lock:
        return collection.query(
            query_embeddings=[embedding],
            n_results=n,
            where=where,
            include=["documents", "metadatas", "distances"]
        )

def _locked_count():
    with _chroma_lock:
        return collection.count()

# ── Constants ─────────────────────────────────────
MAX_EMBED_CHARS   = 1500
MAX_CHUNKS        = 8

def categorize(filepath, filename, file_type=""):
    fp = filepath.lower(); fn = filename.lower(); ft = file_type.lower()
    if ft in {".jpg",".jpeg",".png",".heic",".gif",".webp"}: return "image"
    if ft in {".mp4",".mov",".avi",".mkv"}: return "video"
    if ".md" in fn or "obsidian" in fp: return "note"
    if "document" in fp: return "document"
    if "desktop" in fp: return "desktop"
    if "download" in fp: return "download"
    if ".pdf" in fn: return "pdf"
    if any(e in fn for e in [".py",".js",".ts",".jsx",".tsx",".swift",
        ".go",".java",".kt",".cpp",".rs",".dart",".svelte",
        ".vue",".php",".rb",".scala"]): return "code"
    if any(e in fn for e in [".xlsx",".csv"]): return "spreadsheet"
    return "file"

def _embed_and_store(data: dict):
    """Async worker for /ingest — Mac file_watcher."""
    filepath = data.get("filepath", "")
    filename = data.get("filename", "unknown")
    folder   = data.get("folder", "")
    event    = data.get("event_type", "modified")
    chunks   = [c for c in data.get("chunks", []) if c and c.strip()][:MAX_CHUNKS]
    file_type = data.get("file_type", "")
    if not chunks:
        return
    category = categorize(filepath, filename, file_type)
    now = datetime.now()
    if filepath:
        try: _locked_delete(where={"source": filepath})
        except: pass
    stored = 0
    for chunk in chunks:
        text = f"[Mac {category.title()}] Name: {filename} | Folder: {folder} | Content: {chunk}"
        try:
            emb = ollama.embeddings(model=EMBED_MODEL, prompt=text[:MAX_EMBED_CHARS])["embedding"]
            _locked_add(
                documents=[text],
                embeddings=[emb],
                metadatas=[{"category": f"mac_{category}", "source": filepath,
                            "filename": filename, "timestamp": now.isoformat(),
                            "date": now.strftime("%Y-%m-%d")}],
                ids=[str(uuid.uuid4())]
            )
            stored += 1
        except Exception as e:
            log.error(f"Embed error [{filename}]: {e}")
    if stored: log.info(f"✅ {filename}: {stored}/{len(chunks)} | {event}")
    else: log.warning(f"⚠️  {filename}: 0 stored")

# ── Whisper GPU singleton ────────────────────────────────────────────────
import base64 as _b64, tempfile, shutil, os
from faster_whisper import WhisperModel

# ── SenseVoiceSmall — 3-5x faster than whisper (~100-200ms vs 500-700ms) ───────
_SENSE_VOICE_DIR = "/home/tanmay/vesper/models/sense_voice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_sense_voice_lock = threading.Lock()
_sense_voice_rec  = None  # sherpa_onnx.OfflineRecognizer

def _get_sense_voice():
    global _sense_voice_rec
    with _sense_voice_lock:
        if _sense_voice_rec is None:
            # Prefer int8 model (229M vs 895M — 4x faster CPU inference)
            int8_path   = os.path.join(_SENSE_VOICE_DIR, "model.int8.onnx")
            model_path  = int8_path if os.path.exists(int8_path) else os.path.join(_SENSE_VOICE_DIR, "model.onnx")
            tokens_path = os.path.join(_SENSE_VOICE_DIR, "tokens.txt")
            if os.path.exists(model_path) and os.path.exists(tokens_path):
                try:
                    import sherpa_onnx
                    _sense_voice_rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                        model=model_path, tokens=tokens_path,
                        num_threads=4, use_itn=True, debug=False, language="en",
                    )
                    log.info("SenseVoiceSmall loaded — fast STT active")
                except Exception as e:
                    log.warning(f"SenseVoice load failed: {e}")
                    _sense_voice_rec = False  # unavailable
    return _sense_voice_rec if _sense_voice_rec else None

def _sense_voice_transcribe(wav_bytes):
    """Transcribe 16kHz mono WAV using SenseVoiceSmall. Returns text or None."""
    rec = _get_sense_voice()
    if not rec: return None
    try:
        import sherpa_onnx, numpy as _np, wave as _wv, io as _sio
        with _wv.open(_sio.BytesIO(wav_bytes)) as wf:
            sr = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
        samples = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
        if sr != 16000:  # resample if needed
            import subprocess as _sp, tempfile as _tf2
            with _tf2.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                tmp.write(wav_bytes); tmp_path = tmp.name
            r16k = subprocess.run(['ffmpeg','-y','-i',tmp_path,'-ar','16000','-ac','1','-f','wav','pipe:1'],
                                   capture_output=True, timeout=5)
            os.unlink(tmp_path)
            if r16k.returncode != 0: return None
            with _wv.open(_sio.BytesIO(r16k.stdout)) as wf2:
                pcm = wf2.readframes(wf2.getnframes())
            samples = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
            sr = 16000
        stream = rec.create_stream()
        stream.accept_waveform(sample_rate=sr, waveform=samples)
        rec.decode_stream(stream)
        return stream.result.text.strip() or None
    except Exception as e:
        log.warning(f"SenseVoice transcribe error: {e}")
        return None

_whisper_lock  = threading.Lock()
_whisper_model = None
_whisper_name  = None

def _get_whisper(name="small"):
    global _whisper_model, _whisper_name
    with _whisper_lock:
        if _whisper_model is None or _whisper_name != name:
            if _whisper_model is not None:
                del _whisper_model
            log.info(f"Loading whisper-{name} on CPU int8…")
            _whisper_model = WhisperModel(name, device="cpu",  compute_type="int8")
            _whisper_name  = name
    return _whisper_model

# ── Whisper VOICE singleton (distil-small.en, separate lock) ──────────────────
_whisper_voice_lock  = threading.Lock()
_whisper_voice_model = None
_whisper_wake_lock   = threading.Lock()
_whisper_wake_model  = None
VOICE_MODEL          = "dolphin-voice"    # phi-2 arch, CPU-only (8 threads), uncensored
ASK_MODEL            = "qwen3-ask"        # qwen3 4B CPU, smart deliberate queries

def _get_whisper_voice():
    global _whisper_voice_model
    with _whisper_voice_lock:
        if _whisper_voice_model is None:
            try:
                log.info("Loading whisper-small on GPU (int8)...")
                _whisper_voice_model = WhisperModel(
                    "Systran/faster-distil-whisper-small.en",
                    device="cuda", compute_type="int8")
                log.info("whisper-small GPU loaded.")
            except Exception as _we:
                log.warning(f"whisper-small GPU failed ({_we}), using CPU")
                _whisper_voice_model = WhisperModel(
                    "Systran/faster-distil-whisper-small.en",
                    device="cpu", compute_type="int8")
    return _whisper_voice_model
def _get_whisper_wake():
    """Tiny model for wake word only - 0.3s vs 1.8s for small."""
    global _whisper_wake_model
    with _whisper_wake_lock:
        if _whisper_wake_model is None:
            log.info("Loading whisper-tiny (wake word, CPU int8)...")
            _whisper_wake_model = WhisperModel(
                "Systran/faster-whisper-tiny", device="cpu", compute_type="int8")
    return _whisper_wake_model

# ── Routes ────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "memories": _locked_count()})

@app.route("/ingest", methods=["POST"])
def ingest():
    """Async ingest for Mac file_watcher — returns 202 immediately."""
    data = request.json
    if not data:
        return jsonify({"error": "no data"}), 400
    chunks = [c for c in data.get("chunks", []) if c and c.strip()][:MAX_CHUNKS]
    if not chunks:
        return jsonify({"stored": 0})
    _executor.submit(_embed_and_store, data)
    return jsonify({"status": "accepted", "chunks": len(chunks)}), 202

@app.route("/delete", methods=["POST"])
def delete():
    data = request.json
    filepath = data.get("filepath", "")
    if filepath:
        try:
            _locked_delete(where={"source": filepath})
            log.info(f"🗑️  {Path(filepath).name}")
        except Exception as e:
            log.error(f"Delete error: {e}")
    return jsonify({"status": "ok"})

@app.route("/store_memory", methods=["POST"])
def store_memory_endpoint():
    """
    SYNCHRONOUS memory store for ingest scripts.
    Returns 200 only after embedding + ChromaDB write complete.
    POST: {"text": ..., "category": ..., "source": ...}
    """
    data = request.json or {}
    text     = data.get("text", "").strip()
    category = data.get("category", "general")
    source   = data.get("source", "unknown")
    if not text or len(text) < 10:
        return jsonify({"stored": 0})
    try:
        emb = ollama.embeddings(model=EMBED_MODEL, prompt=text[:MAX_EMBED_CHARS])["embedding"]
        _locked_add(
            documents=[text[:2000]],
            embeddings=[emb],
            metadatas=[{"category": category, "source": source,
                        "timestamp": datetime.now().isoformat(),
                        "date": datetime.now().strftime("%Y-%m-%d")}],
            ids=[str(uuid.uuid4())]
        )
        return jsonify({"stored": 1})
    except Exception as e:
        log.error(f"store_memory error: {e}")
        return jsonify({"stored": 0, "error": str(e)}), 500

@app.route("/delete_category", methods=["POST"])
def delete_category():
    data = request.json or {}
    category = data.get("category", "")
    if not category:
        return jsonify({"error": "no category"}), 400
    try:
        _locked_delete(where={"category": category})
        log.info(f"Deleted category: {category}")
    except Exception as e:
        log.error(f"delete_category error: {e}")
    return jsonify({"status": "ok"})

@app.route("/query", methods=["POST"])
def query_memories():
    """
    Semantic search. POST: {"query": ..., "n": 5, "category": null}
    """
    data = request.json or {}
    query    = data.get("query", "").strip()
    n        = int(data.get("n", 5))
    category = data.get("category", None)
    if not query:
        return jsonify({"results": [], "error": "no query"}), 400
    try:
        emb = ollama.embeddings(model=EMBED_MODEL, prompt=query[:MAX_EMBED_CHARS])["embedding"]
        where = {"category": category} if category else None
        results = _locked_query(emb, n, where)
        docs  = results["documents"][0] if results["documents"] else []
        metas = results["metadatas"][0] if results["metadatas"] else []
        dists = results["distances"][0] if results["distances"] else []
        hits = [
            {"text": d, "category": m.get("category"),
             "source": m.get("source"), "score": round(1 - dist, 3)}
            for d, m, dist in zip(docs, metas, dists)
        ]
        return jsonify({"results": hits, "count": len(hits)})
    except Exception as e:
        log.error(f"Query error: {e}")
        return jsonify({"results": [], "error": str(e)}), 500

@app.route("/command", methods=["POST"])
def send_command_to_mac():
    import requests as req
    data = request.json or {}
    try:
        r = req.post("http://10.0.0.169:5001", json=data, timeout=30)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"success": False, "result": str(e)}), 500








@app.route("/store_ocr", methods=["POST"])
def store_ocr():
    """
    Store OCR text (extracted locally on Mac by Apple Vision — no image needed).
    POST: {"text": "...", "app_name": "...", "window_name": "...", "chars": N}
    Returns: {"stored": bool, "id": "..."}
    """
    data      = request.json or {}
    text      = data.get("text", "").strip()
    app_name  = data.get("app_name", "screen")
    win_name  = data.get("window_name", "")
    chars     = data.get("chars", len(text))

    if not text or len(text) < 20:
        return jsonify({"stored": False, "reason": "too short"})

    try:
        label  = win_name[:60] if win_name else app_name
        memory = "[Screen OCR | {}] {}\n{}".format(app_name, label, text[:800])
        emb    = ollama.embeddings(model=EMBED_MODEL, prompt=memory[:MAX_EMBED_CHARS])["embedding"]
        mem_id = str(uuid.uuid4())
        _locked_add(
            documents=[memory],
            embeddings=[emb],
            metadatas=[{
                "category":  "screen_ocr",
                "source":    "screen:{}".format(app_name),
                "timestamp": datetime.now().isoformat(),
                "date":      datetime.now().strftime("%Y-%m-%d"),
            }],
            ids=[mem_id]
        )
        log.info("store_ocr: {} chars from {} stored".format(chars, app_name))
        return jsonify({"stored": True, "id": mem_id})
    except Exception as e:
        log.warning("store_ocr error: {}".format(e))
        return jsonify({"stored": False, "error": str(e)}), 500

@app.route("/recent_screen", methods=["GET"])
def recent_screen():
    """Return last N screen_ocr memories as plain text context block."""
    n = int(request.args.get("n", 6))
    try:
        # Only scan last 24h — fast regardless of total stored records
        cutoff_24h = (datetime.now() - timedelta(hours=24)).isoformat()[:16]
        with _chroma_lock:
            res = collection.get(
                where={"category": "screen_ocr"},
                include=["documents", "metadatas"],
            )
        items = []
        docs  = res.get("documents", [])
        metas = res.get("metadatas", [])
        # Filter last 24h, then sort by timestamp descending
        pairs = [
            (d, m) for d, m in zip(docs, metas)
            if str(m.get("timestamp", ""))[:16] >= cutoff_24h
        ]
        pairs.sort(key=lambda x: x[1].get("timestamp", ""), reverse=True)
        for doc, meta in pairs[:n]:
            ts  = str(meta.get("timestamp", ""))[:16]
            src = meta.get("source", "screen")
            items.append({"ts": ts, "source": src, "text": doc[:500]})
        return jsonify({"items": items, "count": len(items)})
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})


@app.route("/recall", methods=["POST"])
def recall_memories():
    """
    Return relevant memories WITHOUT running LLM — Mac handles inference.
    POST: {"question": "...", "n": 10, "min_score": 0.35}
    Returns: {"memories": [...], "count": N}
    """
    data     = request.json or {}
    question = data.get("question", "").strip()
    n        = int(data.get("n", 10))
    min_s    = float(data.get("min_score", 0.35))

    if not question:
        return jsonify({"memories": [], "error": "no question"}), 400
    try:
        emb     = ollama.embeddings(model=EMBED_MODEL, prompt=question[:MAX_EMBED_CHARS])["embedding"]
        results = _locked_query(emb, n, None)
        docs    = results["documents"][0] if results["documents"] else []
        metas   = results["metadatas"][0] if results["metadatas"] else []
        dists   = results["distances"][0]  if results["distances"]  else []

        memories = []
        for d, m, dist in zip(docs, metas, dists):
            score = round(1 - dist, 3)
            if score >= min_s:
                memories.append({
                    "text":     d[:400],
                    "category": m.get("category", ""),
                    "source":   m.get("source", ""),
                    "score":    score,
                })
        return jsonify({"memories": memories, "count": len(memories)})
    except Exception as e:
        log.error("recall error: {}".format(e))
        return jsonify({"memories": [], "error": str(e)}), 500

@app.route("/ask", methods=["POST"])
def ask_vesper_api():
    """
    Real-time AI answer. POST: {"question": "...", "context": "", "model": "phi4-mini", "n": 8}
    Combines screen/audio context (from Mac) + Vesper memories -> LLM answer.
    """
    import ollama as _ol
    data     = request.json or {}
    question = data.get("question", "").strip()
    ctx      = data.get("context", "").strip()
    model      = data.get("model", "qwen3-ask")
    n          = int(data.get("n", 8))
    max_tokens = int(data.get("max_tokens", 300)) # 300 allows qwen3 think+answer

    if not question:
        return jsonify({"answer": "", "error": "no question"}), 400

    try:
        # n=0 → screen-only question, skip ChromaDB (faster + avoids memory contamination)
        if n == 0:
            memories = []
        else:
            emb = _ol.embeddings(model=EMBED_MODEL, prompt=question[:MAX_EMBED_CHARS])["embedding"]
            results = _locked_query(emb, n, None)
            docs  = results["documents"][0] if results["documents"] else []
            metas = results["metadatas"][0] if results["metadatas"] else []
            dists = results["distances"][0]  if results["distances"]  else []
            memories = []
            for d, m, dist in zip(docs, metas, dists):
                score = round(1 - dist, 3)
                if score > 0.30:
                    cat = m.get("category", "")
                    memories.append("[{}] {}".format(cat, d[:100]))

        mem_block = "\n".join(memories[:4]) if memories else ""
        ctx_block = "\nScreen (live OCR): {}".format(ctx[:400]) if ctx else ""

        system_msg = (
            "You are Vesper, Tanmay's personal AI assistant with LIVE screen awareness. "
            "The 'Screen' block below is REAL OCR text captured from his screen RIGHT NOW — "
            "not historical data. Use it directly to answer. "
            "NEVER say 'I cannot access real-time data' — you CAN via these live captures. "
            "Be direct and concise: 1-2 sentences max."
        )
        if mem_block:
            user_msg = "{}\n\nRelevant memories:\n{}\n\nQuestion: {}".format(
                ctx_block.strip(), mem_block, question
            )
        else:
            user_msg = "{}\n\nQuestion: {}".format(
                ctx_block.strip(), question
            )


        with _llm_sem:  # one LLM inference at a time — prevents CPU thrashing
            chat_resp = _ol.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user",   "content": user_msg},
                ],
                options={
                    "num_predict": max_tokens,
                    "temperature": 0.2,
                    "num_gpu": 0,
                    "num_thread": 8,
                },
            )
        raw_answer = chat_resp["message"]["content"].strip()
        # Qwen3 thinking mode: content starts with thinking text, ends with </think>
        # then the actual answer follows. Split on </think> to extract real answer.
        if "</think>" in raw_answer:
            answer = raw_answer.split("</think>", 1)[1].strip()
        else:
            # Thinking didn't complete — extract last non-empty line as fallback
            lines = [l.strip() for l in raw_answer.split("\n") if l.strip()]
            answer = lines[-1] if lines else raw_answer.strip()

        return jsonify({"answer": answer, "model": model, "memories_used": len(memories)})

    except Exception as e:
        log.error("ask error: {}".format(e))
        return jsonify({"answer": "", "error": str(e)}), 500


# RapidOCR engine — lazy loaded once
_ocr_engine = None

def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


@app.route("/ocr", methods=["POST"])
def ocr_screenshot():
    """
    OCR a screenshot and store result in Vesper.
    POST: {"image": "<base64 JPEG>", "app_name": "...", "window_name": "...", "store": true}
    Returns: {"text": "...", "stored": bool}
    """
    import base64, io
    try:
        from PIL import Image as PILImage
    except ImportError:
        return jsonify({"error": "Pillow not installed on server"}), 500

    data      = request.json or {}
    img_b64   = data.get("image", "")
    app_name  = data.get("app_name", "screen")
    win_name  = data.get("window_name", "")
    do_store  = data.get("store", True)

    if not img_b64:
        return jsonify({"error": "no image"}), 400

    try:
        img_bytes = base64.b64decode(img_b64)
        img       = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")

        result, _ = _get_ocr()(img)

        if result:
            lines = [r[1] for r in result if len(r) > 1 and r[1].strip()]
            text  = "\n".join(lines)
        else:
            text = ""

        # Filter macOS menu bar noise (appears in every screenshot)
        _MENU_NOISE = {
            "File","Edit","View","Go","Run","Terminal","Window","Help","Selection",
            "History","Bookmarks","Profiles","Tab","Format","Insert","Tools","Navigate",
            "Debug","Code","Search","App","Background","Activity","Storage","Screen",
            "Network","Thu","Fri","Sat","Sun","Mon","Tue","Wed",
        }
        import re as _re
        def _clean(t):
            out = []
            for ln in t.split("\n"):
                s = ln.strip()
                if len(s) < 4: continue
                words = s.split()
                if all(w in _MENU_NOISE or w.isdigit() or (len(w)==1) for w in words): continue
                if _re.match(r"^[\d%:°]+$", s): continue
                out.append(s)
            return "\n".join(out)

        text_clean = _clean(text)

        stored = False
        if do_store and text_clean and len(text_clean) > 30:
            label = win_name[:60] if win_name else app_name
            memory = "[Screen OCR | {}] {}\n{}".format(app_name, label, text_clean[:700])
            try:
                emb = ollama.embeddings(model=EMBED_MODEL, prompt=memory[:MAX_EMBED_CHARS])["embedding"]
                _locked_add(
                    documents=[memory],
                    embeddings=[emb],
                    metadatas=[{
                        "category":  "screen_ocr",
                        "source":    "screen:{}".format(app_name),
                        "timestamp": datetime.now().isoformat(),
                        "date":      datetime.now().strftime("%Y-%m-%d"),
                    }],
                    ids=[str(uuid.uuid4())]
                )
                stored = True
            except Exception as emb_err:
                log.warning("OCR embed error: {}".format(emb_err))

        return jsonify({"text": text[:500], "chars": len(text), "stored": stored})

    except Exception as e:
        log.error("OCR error: {}".format(e))
        return jsonify({"error": str(e)}), 500

@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    """
    Transcribe audio via whisper-small on GPU. Stores to ChromaDB + saves raw WAV for night batch.
    POST: {"audio_b64": "<base64 wav>", "source": "mic", "ts": "ISO-8601", "store": true}
    Returns: {"text": "...", "duration_s": N, "language": "en", "stored": bool}
    """
    data   = request.json or {}
    b64    = data.get("audio_b64", "")
    source = data.get("source", "mic")
    ts     = data.get("ts", datetime.utcnow().isoformat())
    store  = data.get("store", True)

    if not b64:
        return jsonify({"error": "no audio"}), 400

    audio_bytes = _b64.b64decode(b64)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(audio_bytes)
        tmp = tf.name

    try:
        m = _get_whisper()  # default=small (CPU int8, 2.5s per 15s chunk)
        with _whisper_lock:
            segs, info = m.transcribe(
                tmp, beam_size=5, language="en",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            text = " ".join(s.text.strip() for s in segs).strip()

        stored = False
        if store and text:
            # Save raw WAV for night re-transcription (7-day rolling, deleted after batch)
            date_dir = Path("/mnt/hdd/vesper_audio") / datetime.utcnow().strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)
            safe_ts = ts.replace(":", "-").replace("T", "_")[:19]
            shutil.copy(tmp, date_dir / f"{safe_ts}_{source}.wav")

            category = "audio_meeting" if source == "both" else "audio_life"
            mem = f"[Audio/{source} @ {ts}] {text}"
            emb = ollama.embeddings(model=EMBED_MODEL, prompt=mem[:MAX_EMBED_CHARS])["embedding"]
            _locked_add(
                documents=[mem],
                embeddings=[emb],
                metadatas=[{
                    "category":  category,
                    "source":    f"audio:{source}",
                    "timestamp": ts,
                    "date":      ts[:10],
                }],
                ids=[str(uuid.uuid4())],
            )
            stored = True
            log.info(f"transcribe: {len(text)}c from {source} stored (dur={info.duration:.1f}s)")

        return jsonify({
            "text":       text,
            "duration_s": round(info.duration, 2),
            "language":   info.language,
            "stored":     stored,
        })

    except Exception as e:
        log.error(f"transcribe error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass



# ── TTS: ElevenLabs (human, optional) → Piper (fast local, always on) ──────────
import io as _io, wave as _wave

# ── Piper — ultra-fast local TTS, ~100ms/sentence ──────────────────────────────
_piper_lock   = threading.Lock()
_piper_female = None
_piper_male   = None
PIPER_FEMALE  = "/home/tanmay/vesper/models/piper/en_US-hfc_female-medium.onnx"
PIPER_MALE    = "/home/tanmay/vesper/models/piper/en_US-ryan-high.onnx"

def _get_piper(male=False):
    global _piper_female, _piper_male
    from piper.voice import PiperVoice
    with _piper_lock:
        if male:
            if _piper_male is None:
                path = PIPER_MALE if os.path.exists(PIPER_MALE) else PIPER_FEMALE
                _piper_male = PiperVoice.load(path, use_cuda=False)
            return _piper_male
        else:
            if _piper_female is None:
                _piper_female = PiperVoice.load(PIPER_FEMALE, use_cuda=False)
            return _piper_female

def _piper_tts(text, male=False, length_scale=1.15):
    from piper.config import SynthesisConfig
    text = text.strip()
    if not text: return None
    try:
        v = _get_piper(male=male)
        buf = _io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(v.config.sample_rate)
            v.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=length_scale))
        buf.seek(0); return buf.read()
    except Exception as e:
        log.warning(f"Piper TTS error: {e}"); return None

# ── ElevenLabs — human voice, ~75-200ms (API key optional) ─────────────────────
# Put your key in /home/tanmay/vesper/.elevenlabs_key or set ELEVENLABS_API_KEY env
_EL_KEY = ""
try:
    with open("/home/tanmay/vesper/.elevenlabs_key") as _f: _EL_KEY = _f.read().strip()
except: pass
if not _EL_KEY: _EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

_el_client = None
_el_lock   = threading.Lock()
EL_VOICE_F = "21m00Tcm4TlvDq8ikWAM"  # Rachel (female, American, natural)
EL_VOICE_M = "TxGEqnHWrfWFTfGW9XjX"  # Josh (male, American, natural)

def _get_el():
    global _el_client
    if not _EL_KEY: return None
    with _el_lock:
        if _el_client is None:
            try:
                from elevenlabs.client import ElevenLabs
                _el_client = ElevenLabs(api_key=_EL_KEY)
            except ImportError:
                log.warning("elevenlabs package not installed — pip install elevenlabs")
    return _el_client

def _el_tts(text, male=False):
    """ElevenLabs flash TTS: human-sounding, ~75-200ms. Returns WAV bytes or None."""
    cl = _get_el()
    if not cl: return None
    text = text.strip()
    if not text: return None
    try:
        from elevenlabs import VoiceSettings
        audio_iter = cl.text_to_speech.convert(
            voice_id=EL_VOICE_M if male else EL_VOICE_F,
            text=text,
            model_id="eleven_flash_v2_5",
            output_format="pcm_22050",
            voice_settings=VoiceSettings(stability=0.4, similarity_boost=0.8, style=0.3)
        )
        pcm = b"".join(audio_iter)
        buf = _io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
            wf.writeframes(pcm)
        buf.seek(0); return buf.read()
    except Exception as e:
        log.warning(f"ElevenLabs TTS: {e}"); return None


# ── Supertonic-3 — neural diffusion TTS, human voice, ~230-640ms ───────────────
_supertonic_lock   = threading.Lock()
_supertonic_model  = None
_supertonic_f      = None  # female F1 style
_supertonic_m      = None  # male M1 style

def _get_supertonic():
    global _supertonic_model, _supertonic_f, _supertonic_m
    with _supertonic_lock:
        if _supertonic_model is None:
            try:
                from supertonic import TTS as _SuperTTS
                _supertonic_model = _SuperTTS()
                _supertonic_f = _supertonic_model.get_voice_style('F1')
                _supertonic_m = _supertonic_model.get_voice_style('M1')
                log.info("Supertonic-3 loaded (F1/M1)")
            except Exception as _se:
                log.warning(f"Supertonic load failed: {_se}")
                _supertonic_model = False  # mark as unavailable
    return (_supertonic_model if _supertonic_model else None), _supertonic_f, _supertonic_m

def _supertonic_tts(text, male=False, steps=4, speed=1.0):
    """Supertonic-3 neural diffusion TTS. steps=4 speed=1.0: natural pacing, higher quality (~650ms)."""
    import numpy as _np
    text = text.strip()
    if not text: return None
    try:
        model, sf, sm = _get_supertonic()
        if not model: return None
        style = sm if male else sf
        audio, meta_dur = model.synthesize(text, style, total_steps=steps, speed=speed)
        audio_1d = audio[0] if audio.ndim > 1 else audio
        sr = int(round(len(audio_1d) / meta_dur[0]))
        audio_i16 = (_np.clip(audio_1d * 32767, -32768, 32767)).astype(_np.int16)
        buf = _io.BytesIO()
        with _wave.open(buf, 'wb') as wf:
            wf.setnchannels(1); wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(audio_i16.tobytes())
        buf.seek(0); return buf.read()
    except Exception as e:
        log.warning(f"Supertonic TTS error: {e}"); return None

def _smart_tts(text, male=False):
    """ElevenLabs (key) → Supertonic-3 3-step (natural human voice) → Piper (fallback). 100% local."""
    wav = _el_tts(text, male=male)
    if wav: return wav
    wav = _supertonic_tts(text, male=male)  # 3-step speed=0.95: natural, not pressed
    if wav: return wav
    return _piper_tts(text, male=male)

# Pre-generated instant thinking sound (Piper, always local)
_THINK_WAV      = None
_think_wav_lock = threading.Lock()

def _get_think_wav():
    global _THINK_WAV
    with _think_wav_lock:
        if _THINK_WAV is None:
            _THINK_WAV = _piper_tts("Hmm...", male=False) or b""
    return _THINK_WAV

@app.route("/speak", methods=["POST"])
def speak_tts():
    from flask import Response
    data  = request.json or {}
    text  = data.get("text", "").strip()
    voice = data.get("voice", "af_heart")
    speed = float(data.get("speed", 0.88))

    if not text:
        return jsonify({"error": "no text"}), 400

    try:
        use_male = voice in ("am_adam", "am_michael", "male")
        wav = _smart_tts(text, male=use_male)
        if not wav:
            return jsonify({"error": "TTS failed"}), 500
        return Response(wav, mimetype="audio/wav")
    except Exception as e:
        log.error("speak error: {}".format(e))
        return jsonify({"error": str(e)}), 500


@app.route("/transcribe_partial", methods=["POST"])
def transcribe_partial():
    """Transcribe audio chunk without storing — used for rolling pre-transcription."""
    data   = request.json or {}
    b64_in = data.get("audio_b64", "")
    if not b64_in:
        return jsonify({"error": "no audio"}), 400
    audio_bytes = _b64.b64decode(b64_in)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        tf.write(audio_bytes); tmp = tf.name
    try:
        m = _get_whisper_wake()
        with _whisper_wake_lock:
            segs, _ = m.transcribe(tmp, beam_size=1, language="en",
                                   initial_prompt="Hey Vesper, okay Vesper, Vesper.",
                                   condition_on_previous_text=False,
                                   vad_filter=True,
                                   vad_parameters={"min_silence_duration_ms": 150})
            text = " ".join(s.text.strip() for s in segs).strip()
        return jsonify({"text": text})
    except Exception as e:
        log.error(f"transcribe_partial error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        try: os.unlink(tmp)
        except: pass


# Pre-computed context cache: session_id -> (good_mems list, timestamp)
import threading as _threading
_prepare_cache  = {}
_prepare_lock   = _threading.Lock()

@app.route("/voice_prepare", methods=["POST"])
def voice_prepare():
    """
    Pre-compute embedding + ChromaDB while user is still speaking.
    POST: {"question": "<partial transcript>"}
    Returns: {"session": "<8-char id>"}
    Client passes session back to /voice_fast to skip embed+query (~0.3s saved).
    """
    import uuid, time as _time
    data     = request.json or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"session": None})
    session_id = uuid.uuid4().hex[:8]

    def _compute():
        try:
            emb   = ollama.embeddings(model=EMBED_MODEL, prompt=question[:MAX_EMBED_CHARS],
                                      keep_alive="24h")["embedding"]
            res   = _locked_query(emb, n=10)
            docs  = res["documents"][0] if res.get("documents") else []
            dists = res["distances"][0]  if res.get("distances")  else []
            def _ex(doc):
                doc = doc.strip()
                if doc.startswith("[Screen OCR"):
                    lines = [l.strip() for l in doc.split("\n")
                             if len(l.strip()) > 10
                             and not l.strip().startswith("O Q")
                             and l.strip() not in ("", "Chrome", "Safari")]
                    return " · ".join(lines[:2])[:200] if lines else doc[:150]
                idx = doc.find("]: ")
                return (doc[idx+3:idx+200] if idx != -1 else doc[:200]).strip()
            def _cat(doc):
                if doc.startswith("["):
                    end = doc.find("]")
                    return doc[1:end].strip() if end > 1 else "gen"
                return "gen"
            def _is_sp(doc):
                _sw = ("unsubscribe", "% off", "attn.tv", "bplist00",
                       "fragrancenet", "myprotein", "astrotalk", "coupon")
                return any(s in doc.lower() for s in _sw)
            scored = [(d, 1-dist) for d, dist in zip(docs, dists)
                      if (1-dist) >= 0.60 and _ex(d) and not _is_sp(d)]
            scored.sort(key=lambda x: -x[1])
            seen_cats, good = set(), []
            for d, _ in scored:
                c = _cat(d)
                if c not in seen_cats:
                    seen_cats.add(c)
                    good.append(_ex(d))
                if len(good) >= 3:
                    break
            with _prepare_lock:
                _prepare_cache[session_id] = (good, _time.time())
                old = [k for k, (_, ts) in _prepare_cache.items() if _time.time()-ts > 30]
                for k in old: del _prepare_cache[k]
            # CPU embed (nomic-cpu) does NOT evict dolphin GPU kernel cache
            # No dolphin re-warm needed here — dolphin stays warm on its own.
        except Exception as e:
            log.warning(f"voice_prepare failed: {e}")

    _threading.Thread(target=_compute, daemon=True, name="prepare").start()
    return jsonify({"session": session_id})



@app.route("/voice_greet", methods=["POST"])
def voice_greet():
    """Casual personalized greeting on wake word — returns WAV."""
    from flask import Response
    import random as _rand, io as _io2
    greetings = [
        "Hey Tanmay... what do you need?",
        "Yo, what's up?",
        "Hey! Go ahead.",
        "What's good?",
        "Talk to me.",
        "Yeah? What's on your mind?",
        "I'm here, what do you need?",
        "Hey Tanmay, what do you got?",
        "I'm listening.",
        "Yep, right here. What's up?",
        "What do you need from me?",
        "What's going on?",
        "Tell me.",
        "Alright, I'm here.",
        "Yeah, go ahead.",
    ]
    text = _rand.choice(greetings)
    data = request.get_json(silent=True) or {}
    _greet_voice = data.get("voice", "af_heart")
    _greet_speed = float(data.get("speed", 0.92))
    try:
        use_male = _greet_voice in ("am_adam", "am_michael", "male")
        wav = _smart_tts(text, male=use_male)
        return Response(wav or b"", mimetype="audio/wav")
    except Exception as e:
        log.warning(f"voice_greet: {e}")
        return Response(b"", mimetype="audio/wav")

@app.route("/voice_fast", methods=["POST"])
def voice_fast():
    """
    Streaming voice pipeline.  Fast paths checked BEFORE embed to avoid Ollama scheduling conflicts.
    POST {audio_b64: <base64 WAV, optional>, question: <text, optional>, session: <id, optional>}
    SSE: data: Q:<b64>  data: T:<b64>  data: A:<b64_WAV>  data: END
    """
    from flask import Response, stream_with_context
    import datetime as _dt2
    data     = request.json or {}
    b64_in   = data.get("audio_b64", "")
    question = data.get("question", "").strip()

    # 1. Transcribe the tail audio (if provided)
    _partial_words = len(question.split()) if question else 0
    if b64_in and _partial_words < 5:
        audio_bytes = _b64.b64decode(b64_in)
        tail = None
        try:
            # SenseVoiceSmall: 3-5x faster than whisper (~100-200ms vs 500-700ms)
            tail = _sense_voice_transcribe(audio_bytes)
            if tail is None:
                # Fallback: whisper-tiny (1-4 words partial) or distil-small (cold)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                    tf.write(audio_bytes); tmp = tf.name
                try:
                    m = _get_whisper_wake() if _partial_words >= 1 else _get_whisper_voice()
                    lock = _whisper_wake_lock if _partial_words >= 1 else _whisper_voice_lock
                    with lock:
                        segs, _ = m.transcribe(tmp, beam_size=1, language="en",
                                               initial_prompt="Hey Vesper. What time is it? What was I doing?",
                                               condition_on_previous_text=False, vad_filter=False)
                        tail = " ".join(s.text.strip() for s in segs).strip() or None
                finally:
                    try: os.unlink(tmp)
                    except: pass
        except Exception as e:
            log.warning(f"voice_fast transcribe: {e}")
        if tail:
            q_low2 = question.lower().strip() if question else ""
            t_low2 = tail.lower().strip()
            if q_low2 and (q_low2 in t_low2 or t_low2 in q_low2):
                question = tail if len(tail) >= len(question) else question
            elif q_low2 and sum(1 for w in tail.lower().split() if w in q_low2) > len(tail.split()) * 0.5:
                question = tail
            else:
                question = (question + " " + tail).strip()

    if not question:
        return jsonify({"error": "no speech detected"}), 200

    q_low = question.lower().strip()

    # 2. TTS helpers
    _voice_name = data.get("voice", "af_heart")
    _use_male   = _voice_name in ("am_adam", "am_michael", "male")

    _is_first_tts = [True]  # Piper for first chunk (60ms), Supertonic for rest (natural voice)

    def _tts_ev(txt):
        txt = txt.strip()
        if not txt: return None
        try:
            if _is_first_tts[0]:
                _is_first_tts[0] = False
                wav = _piper_tts(txt, male=_use_male)  # ~60ms — user hears something immediately
            else:
                wav = _smart_tts(txt, male=_use_male)
            if not wav: return None
            return (f"data: T:{_b64.b64encode(txt.encode()).decode()}\n\n"
                    + f"data: A:{_b64.b64encode(wav).decode()}\n\n")
        except Exception as e:
            log.warning(f"TTS: {e}"); return None

    def _fast_stream(fast_text):
        """Fast-path TTS: Supertonic-3 3-step speed=0.95 (~500ms, natural) → Piper fallback."""
        def _g():
            yield f"data: Q:{_b64.b64encode(question.encode()).decode()}\n\n"
            wav = _supertonic_tts(fast_text, male=_use_male) or _piper_tts(fast_text, male=_use_male)
            if wav:
                yield (f"data: T:{_b64.b64encode(fast_text.encode()).decode()}\n\n"
                       + f"data: A:{_b64.b64encode(wav).decode()}\n\n")
            yield "data: END\n\n"
        return Response(stream_with_context(_g()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 3. FAST PATHS — checked BEFORE any embed/LLM (no Ollama interaction)
    _wake = {"hey vesper","hi vesper","vesper","ok vesper","okay vesper",
             "hello vesper","hey","yo vesper","yo","hello","hi"}
    if q_low in _wake or len(q_low) <= 3:
        return _fast_stream("Yeah, I'm right here!")

    if any(k in q_low for k in ("who are you","what are you","introduce yourself",
                                  "who is vesper","what is vesper","your name")):
        return _fast_stream("I'm Vesper, your personal AI.")

    if any(k in q_low for k in ("about me","tell me about me","know about me",
                                  "something about me","what do you know")):
        return _fast_stream("I have your messages, browsing and calendar. Ask me something specific!")

    if any(k in q_low for k in ("how are you","how's it going","how do you do",
                                  "you doing","you okay","sup vesper","what's up",
                                  "good morning","good evening","good afternoon")):
        import random as _rand
        _greet_replies = [
            "Doing great, what do you need?",
            "All good here! What's up?",
            "Ready to go. What do you need?",
            "Good! What can I help with?",
            "Pretty good, thanks! What's on your mind?",
        ]
        return _fast_stream(_rand.choice(_greet_replies))

    if any(k in q_low for k in ("what time","current time","what is the time",
                                  "tell me the time","what's the time")):
        return _fast_stream(f"It's {_dt2.datetime.now().strftime('%-I:%M %p')}.")

    if any(k in q_low for k in ("what day","what date","today's date",
                                  "what is today","what's today","day is it")):
        return _fast_stream(f"Today is {_dt2.datetime.now().strftime('%A, %B %-d')}.")

    if any(k in q_low for k in ("weather","how hot","how cold","forecast",
                                  "temperature outside","weather outside")):
        try:
            import requests as _req
            r = _req.get("https://wttr.in/?format=%C,+%t", timeout=3)
            txt = r.text.strip() if r.status_code == 200 else None
            return _fast_stream(f"Right now it's {txt}." if txt else "Can't reach weather right now.")
        except Exception:
            return _fast_stream("Can't reach weather right now.")

    if any(k in q_low for k in ("what do i do","what is my job","my profession",
                                   "what do you know about my work","am i a developer",
                                   "what kind of developer","what kind of engineer")):
        return _fast_stream("You're a software engineer, full-stack dev. I've seen you working in VS Code, building with Electron and web stuff.")

    if any(k in q_low for k in ("who is arpita","arpita who","tell me about arpita")):
        return _fast_stream("Arpita's one of your close contacts, you two message a lot.")

    if any(k in q_low for k in ("what am i using","what tools do i use","what apps do i use",
                                  "what do i use for coding","what editor","what ide")):
        return _fast_stream("You're using VS Code, Claude Code, Chrome, and Electron from what I've seen on your screen.")

    if any(k in q_low for k in ("who is tanmay","who am i","tell me about me","about me",
                                  "what do you know about me","describe me")):
        return _fast_stream("You're Tanmay Pramanick, software engineer. Building Electron apps, using VS Code and Claude Code, working on AI projects like Vesper.")

    if any(k in q_low for k in ("what is vesper","what can you do","what are your capabilities")):
        return _fast_stream("I'm Vesper, your personal AI with access to your screen, messages, and browsing.")

    if any(k in q_low for k in ("saving context","save context","saving conversation",
                                  "what model","which model","what llm","what ai model",
                                  "what language model","what stt","what tts","how do you work",
                                  "how are you built","your architecture")):
        stt_name = "SenseVoice" if _get_sense_voice() else "distil-whisper"
        return _fast_stream(f"I save our last 14 messages in your browser. I run dolphin-phi for language, {stt_name} for speech, Supertonic for voice. All local.")

    # 4. SLOW PATH: embed + memory recall (only for non-fast-path queries)
    _session  = data.get("session", "")
    _cached_mems = None
    _vf_docs, _vf_dists = [], []
    if _session:
        with _prepare_lock:
            _cached = _prepare_cache.pop(_session, None)
        if _cached:
            _cached_mems = _cached[0]

    _activity_keys = ("what did i", "what was i", "what have i", "what am i work",
                      "what project", "what youtube", "what video", "what website",
                      "what i was do", "what i watch", "what screen",
                      "what app", "what i work", "show me", "tell me what i")
    _is_activity = any(k in q_low for k in _activity_keys)
    _n_query = 10 if _is_activity else 5

    _ran_embed = False
    if _cached_mems is None:
        _ran_embed = True
        _screen_keys = ("screen", "laptop", "computer", "what did i", "what was i",
                         "what have i", "what i was", "recently", "right now", "just now")
        _is_screen_q = _is_activity and any(k in q_low for k in _screen_keys)
        _embed_prompt = (f"screen capture recent activity: {question}" if _is_screen_q
                        else question)[:MAX_EMBED_CHARS]
        emb       = ollama.embeddings(model=EMBED_MODEL, prompt=_embed_prompt,
                                      keep_alive="24h")["embedding"]
        results   = _locked_query(emb, n=_n_query)
        _vf_docs  = results["documents"][0] if results.get("documents") else []
        _vf_dists = results["distances"][0]  if results.get("distances")  else []

    # Web search detection
    _web_keys = ("what's happening","latest news","news today","current events",
                 "what happened today","in the news","breaking news","latest on",
                 "who won","what's the score","stock market","bitcoin","crypto price",
                 "update on","situation in","what's going on in","headlines","top news")
    _is_web = any(k in q_low for k in _web_keys)

    def _extract(doc):
        doc = doc.strip()
        if doc.startswith("[Screen OCR"):
            lines = [l.strip() for l in doc.split("\n")
                     if len(l.strip()) > 10
                     and not l.strip().startswith("O Q")
                     and l.strip() not in ("", "Chrome", "Safari", "Firefox")]
            return " · ".join(lines[:2])[:200] if lines else doc[:150]
        idx = doc.find("]: ")
        return (doc[idx+3:idx+200] if idx != -1 else doc[:200]).strip()

    def _cat(doc):
        if doc.startswith("["):
            end = doc.find("]")
            return doc[1:end].strip() if end > 1 else "gen"
        return "gen"

    def _is_spam(doc):
        _sw = ("unsubscribe", "% off", "promo code", "click here", "attn.tv",
               "fragrancenet", "myprotein", "astrotalk", "free chat", "sale ends",
               "discount", "bplist00", "fragrance", "coupon")
        return any(s in doc.lower() for s in _sw)

    _threshold = 0.60 if _is_activity else 0.75

    if _cached_mems is not None:
        good_mems = _cached_mems
    else:
        _platforms = [w for w in q_low.split()
                       if w in ("youtube","netflix","spotify","instagram","twitter",
                                "reddit","whatsapp","telegram","slack","discord",
                                "github","figma","vscode","chrome","safari","linear")]
        def _boost(d, s):
            dl = d.lower()
            # Always prioritize screen captures (screenpipe data) above all else
            if d.startswith("[Screen OCR"): return s + 0.08
            if d.startswith("[Mac File"): return s + 0.03
            # Exclude Contact entries entirely (phone numbers/emails not useful for answering)
            if d.startswith("[Contact"): return -1.0
            if not _is_activity: return s
            # Activity queries: penalize low-signal noisy sources
            boost = -0.12 if d.startswith("[iMessage") or d.startswith("[Email") else 0
            if _platforms and any(p in dl for p in _platforms): boost += 0.15
            return s + boost
        scored = [(d, bs) for d, dist in zip(_vf_docs, _vf_dists)
                  for bs in [_boost(d, 1-dist)]
                  if bs >= _threshold and _extract(d) and not _is_spam(d)]
        scored.sort(key=lambda x: -x[1])
        seen_cats, good_mems = set(), []
        for d, _ in scored:
            c = _cat(d)
            if c not in seen_cats:
                seen_cats.add(c); good_mems.append(_extract(d))
            if len(good_mems) >= 6: break

        # For platform queries: if no platform-matching docs found, say so
        if _platforms and _is_activity and good_mems:
            platform_mems = [m for m in good_mems if any(p in m.lower() for p in _platforms)]
            if not platform_mems:
                good_mems = []  # No matching platform data → honest "no data" response

    def _generate():
        yield f"data: Q:{_b64.b64encode(question.encode()).decode()}\n\n"

        # No-data fast path (after embed confirms nothing relevant)
        _nodata_keys = ("what app","which app","what am i doing","what did i work",
                        "what have i been work","what am i working","what language do i",
                        "what programming","what do i use for","what software","which software")
        _no_platform_data = (_platforms and _is_activity and not good_mems)
        if not good_mems and not _is_web and (any(k in q_low for k in _nodata_keys) or _no_platform_data):
            platform_name = _platforms[0].capitalize() if _platforms else None
            msg = f"I don't have recent {platform_name} data." if platform_name else "I don't have that info right now."
            ev = _tts_ev(msg)
            if ev: yield ev
            yield "data: END\n\n"
            return

        # Web search
        web_ctx = None
        if _is_web:
            try:
                from duckduckgo_search import DDGS
                with DDGS() as ddgs:
                    res = list(ddgs.text(question, max_results=1, timelimit="d"))
                if res: web_ctx = res[0].get("body","")[:150]
            except Exception as e:
                log.warning(f"DDG: {e}")

        system = "You're Vesper — Tanmay's personal AI. Talk like a smart, chill friend. Be direct and natural. Use his data freely. Never say \"As an AI\" or \"I don't have access to\" — just answer."
        if web_ctx:
            user_msg = f"[Web: {web_ctx}]\n{question}"
        elif good_mems:
            mems_str = " | ".join(m[:140] for m in good_mems[:5])
            user_msg = f"[Tanmay data: {mems_str}]\n{question}"
        else:
            user_msg = question
        if _nomic_warm_lock.acquire(blocking=False):
            import threading as _th3
            def _warm_r():
                try: ollama.embeddings(model=EMBED_MODEL, prompt="w", keep_alive="24h")
                finally: _nomic_warm_lock.release()
            _th3.Thread(target=_warm_r, daemon=True, name="nomic-warm").start()

        _hist = [{"role": h["role"], "content": str(h.get("content",""))[:120]}
                 for h in data.get("history", [])[-6:]  # 3 prior exchanges
                 if h.get("role") in ("user","assistant") and h.get("content","").strip()]
        _msgs = [{"role":"system","content":system}] + _hist + [{"role":"user","content":user_msg}]

        import re as _re
        def _clean_text(t):
            t = _re.sub(r'[🌀-🿿☀-➿🀀-🛿]+', '', t)
            return _re.sub(r'\*+', '', t).strip().lstrip('.:,- ')

        _t0 = time.time()
        _stream = ollama.chat(
            model=VOICE_MODEL, stream=True, keep_alive="24h",
            options={"num_predict": 120, "temperature": 0.68,
                     "num_gpu": 22, "num_ctx": 256, "num_thread": 4,
                     "stop": ["\n\n", "User:", "Q:", "<|im_start|>", "<|im_end|>",
                              "I do not have", "I am unable", "As an AI",
                              "As an artificial", "I apologize", "Please provide",
                              "Based on the information", "Please let me know",
                              "I would need", "Could you please", "I cannot assist"]},
            messages=_msgs)

        _buf = ""
        _full = ""
        _WORD_SPLIT = 5  # force-split after 5 words — faster first audio

        for _chunk in _stream:
            _delta = (getattr(getattr(_chunk, 'message', None), 'content', '') or '')
            if not _delta:
                continue
            _buf += _delta
            _full += _delta
            _words = _buf.split()
            _wcount = len(_words)

            # Don't split until we have at least 10 words — avoids tiny robotic micro-clips
            if _wcount >= 4:
                split_at = -1
                for _i, _c in enumerate(_buf):
                    if _c in '.!?' and (_i + 1 >= len(_buf) or _buf[_i + 1] in (' ', '\n')):
                        split_at = _i + 1; break

                                # Force split at word boundary after _WORD_SPLIT words
                if split_at < 0 and _wcount > _WORD_SPLIT:  # > not >= ensures last word token is complete
                    _split_words = _words[:_WORD_SPLIT]
                    split_at = len(' '.join(_split_words))

                if split_at >= 0:
                    _sent = _clean_text(_buf[:split_at])
                    _buf = _buf[split_at:].lstrip()
                    if _sent:
                        ev = _tts_ev(_sent)
                        if ev:
                            yield ev

        # TTS any remaining text (last sentence without terminal punctuation)
        _tail = _clean_text(_buf)
        if _tail:
            ev = _tts_ev(_tail)
            if ev:
                yield ev

        if not _clean_text(_full):
            ev = _tts_ev("Not sure about that one.")
            if ev:
                yield ev

        yield "data: END\n\n"

        import threading as _thsav
        def _do_save(_q=question, _a=_full, _ts=int(time.time())):
            try:
                _doc = "[Voice Q&A]\nQ: " + _q + "\nA: " + _a
                _e2 = ollama.embeddings(model=EMBED_MODEL, prompt=_doc[:1200],
                                        keep_alive="24h")["embedding"]
                _locked_add([_doc], [_e2],
                    [{"category":"conversation","source":"voice_kiosk","timestamp":_ts}],
                    [f"conv_{_ts}_{abs(hash(_q))&0xffff:04x}"])
            except:
                pass
        _thsav.Thread(target=_do_save, daemon=True, name="conv-save").start()
        log.info(f"voice_fast stream={int((time.time()-_t0)*1000)}ms words={len(_full.split())} q='{question[:40]}'")

    return Response(stream_with_context(_generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


import pathlib as _pathlib

_KIOSK_PATH = _pathlib.Path("/home/tanmay/vesper/pipelines/kiosk.html")

@app.route("/kiosk")
def kiosk_ui():
    if not _KIOSK_PATH.exists():
        return "Kiosk not found.", 404
    return _KIOSK_PATH.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}

_UI_PATH = _pathlib.Path("/home/tanmay/vesper/pipelines/voice_ui.html")

@app.route("/ui")
def voice_ui():
    if not _UI_PATH.exists():
        return "Voice UI not found.", 404
    return _UI_PATH.read_text(), 200, {"Content-Type": "text/html; charset=utf-8"}



@app.route("/ambient_context", methods=["GET"])
def ambient_context():
    """Recent personal context for kiosk ambient display."""
    try:
        emb = ollama.embeddings(model=EMBED_MODEL,
                                prompt="recent messages activity today",
                                keep_alive="24h")["embedding"]
        def _pull(where, n):
            try:
                r = _locked_query(emb, n=n, where=where)
                docs = r.get("documents", [[]])[0]
                dists = r.get("distances", [[]])[0]
                out = []
                for doc, dist in zip(docs, dists):
                    if 1 - dist > 0.25:
                        idx = doc.find("]: ")
                        snip = (doc[idx+3:idx+100] if idx != -1 else doc[:100]).strip()
                        if snip:
                            out.append(snip)
                return out
            except:
                return []
        wa = _pull({"category": {"$eq": "whatsapp"}}, 3)
        em = _pull({"category": {"$in": ["email_received", "email_sent"]}}, 2)
        sc = _pull({"category": {"$eq": "screen"}}, 2)
        return jsonify({"whatsapp": wa[:2], "email": em[:1], "screen": sc[:1]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _keepalive():
    """Ping nomic-cpu (CPU) + dolphin-voice (GPU) every 4.5 min to prevent Ollama eviction.
    CPU embed does NOT evict GPU kernel cache — both pings are safe to run sequentially.
    """
    import time as _time, threading as _threading
    def _ping():
        while True:
            _time.sleep(270)   # 4.5 min — within Ollama 5-min eviction window
            try:
                ollama.embeddings(model=EMBED_MODEL, prompt="ping", keep_alive="24h")
                log.debug("[keepalive] nomic-cpu pinged")
            except Exception as e:
                log.warning(f"[keepalive] nomic-cpu ping failed: {e}")
            try:
                ollama.chat(model=VOICE_MODEL, stream=False, keep_alive="24h",
                            messages=[{"role":"user","content":"ping"}],
                            options={"num_predict": 1, "num_gpu": 22,
                                     "num_ctx": 256, "num_thread": 8})
                log.debug("[keepalive] dolphin-voice pinged")
            except Exception as e:
                log.warning(f"[keepalive] dolphin ping failed: {e}")
    _threading.Thread(target=_ping, daemon=True, name="keepalive").start()

def _warmup():
    """Pre-load Kokoro, distil-whisper, and phi4-mini-fast on startup so first voice query is fast."""
    import threading
    def _load():
        # 0. SenseVoiceSmall STT (3-5x faster than whisper — primary STT if available)
        try:
            sv = _get_sense_voice()
            if sv:
                log.info("[warmup] SenseVoiceSmall STT ready (fast mode active).")
            else:
                log.info("[warmup] SenseVoice not available — using whisper fallback.")
        except Exception as e:
            log.warning(f"[warmup] SenseVoice check failed: {e}")
        # 1. Supertonic-3 TTS (neural diffusion, human voice — primary TTS)
        try:
            log.info("[warmup] Loading Supertonic-3 TTS…")
            _get_supertonic()
            log.info("[warmup] Supertonic-3 ready (F1/M1 styles loaded).")
        except Exception as e:
            log.warning(f"[warmup] Supertonic-3 failed: {e}")
        # 1b. Piper TTS (local fallback)
        try:
            log.info("[warmup] Loading Piper TTS (fallback)…")
            _get_piper(male=False)
            log.info("[warmup] Piper ready.")
        except Exception as e:
            log.warning(f"[warmup] Piper failed: {e}")
        # 1c. ElevenLabs warmup (human voice, optional — needs API key)
        if _EL_KEY:
            try:
                log.info("[warmup] ElevenLabs key found — warming up connection…")
                _el_tts("hey", male=False)
                log.info("[warmup] ElevenLabs ready (human voice active).")
            except Exception as e:
                log.warning(f"[warmup] ElevenLabs warmup failed: {e}")
        else:
            log.info("[warmup] No ElevenLabs key — using Piper TTS.")
        # 2a. whisper-tiny (wake word)
        try:
            log.info("[warmup] Loading whisper-tiny…")
            _get_whisper_wake()
            log.info("[warmup] whisper-tiny ready.")
        except Exception as e:
            log.warning(f"[warmup] whisper-tiny failed: {e}")
        # 2b. whisper-small GPU/CPU (voice queries — 3-5x better accuracy than small)
        try:
            log.info("[warmup] Loading whisper-medium…")
            _get_whisper_voice()
            log.info("[warmup] whisper-medium ready.")
        except Exception as e:
            log.warning(f"[warmup] whisper-large-v3-turbo failed: {e}")
        # 3. dolphin-voice LLM — load with explicit 22 GPU layers + optimal settings
        try:
            log.info("[warmup] Pre-loading dolphin-voice (22GPU, 256ctx, 8T)...")
            r = ollama.chat(model=VOICE_MODEL, stream=False,
                        keep_alive="24h",
                        messages=[{"role":"user","content":"What is 2+2?"}],
                        options={"num_predict": 5, "num_gpu": 22,
                                 "num_ctx": 256, "num_thread": 8})
            # Log actual tok/s to verify GPU layer count is effective
            _tps = getattr(r, 'eval_count', 0) / max(getattr(r, 'eval_duration', 1) / 1e9, 0.001)
            log.info(f"[warmup] dolphin-voice ready. Warmup tok/s: {_tps:.1f}")
        except Exception as e:
            log.warning(f"[warmup] dolphin-voice failed: {e}")
        # 4. qwen3-ask (CPU model — warm after dolphin to avoid resource contention)
        try:
            log.info("[warmup] Pre-loading qwen3-ask into Ollama...")
            ollama.chat(model=ASK_MODEL, stream=False,
                        keep_alive="24h",
                        messages=[{"role":"user","content":"hi"}],
                        options={"num_predict": 1})
            log.info("[warmup] qwen3-ask ready.")
        except Exception as e:
            log.warning(f"[warmup] qwen3-ask failed: {e}")
        # 5. nomic-cpu warm (CPU embed — pre-compile CPU kernels, 383ms first call)
        # CPU embed does NOT evict dolphin's GPU kernel cache (different compute units)
        try:
            log.info("[warmup] Warming nomic-cpu (CPU embed)...")
            ollama.embeddings(model=EMBED_MODEL, prompt="warmup", keep_alive="24h")
            ollama.embeddings(model=EMBED_MODEL, prompt="ready", keep_alive="24h")
            log.info("[warmup] nomic-cpu ready. Vesper online.")
        except Exception as e:
            log.warning(f"[warmup] nomic-cpu failed: {e}")
    threading.Thread(target=_load, daemon=True, name="warmup").start()

if __name__ == "__main__":
    log.info(f"File receiver v8 on port {FILE_RECEIVER_PORT}")
    import os as _os
    _cert = "/home/tanmay/vesper/cert.pem"
    _key  = "/home/tanmay/vesper/key.pem"
    _ssl  = (_cert, _key) if (_os.path.exists(_cert) and _os.path.exists(_key)) else None
    if _ssl:
        log.info("Starting with HTTPS (self-signed cert)")

    # Bounded Werkzeug server: no fork, no gunicorn GIL deadlock, max 8 concurrent connections.
    # Main thread runs serve_forever() (GIL released during select()), warmup daemon threads
    # can run freely without competing with the server's accept loop.
    from werkzeug.serving import make_server as _make_server, WSGIRequestHandler as _WRH

    _warmup()
    _keepalive()

    _srv = _make_server("0.0.0.0", FILE_RECEIVER_PORT, app, threaded=True,
                        request_handler=_WRH, ssl_context=_ssl)

    # Monkey-patch ThreadingMixIn.process_request to cap concurrent connections at 8.
    # Without this, each browser retry spawns a new thread (unbounded) → thread exhaustion.
    _conn_sem = threading.Semaphore(8)
    _orig_process_request = _srv.__class__.process_request

    def _bounded_process_request(self, request, client_address):
        if _conn_sem.acquire(blocking=False):
            def _handle():
                try:
                    self.finish_request(request, client_address)
                except Exception:
                    self.handle_error(request, client_address)
                finally:
                    self.shutdown_request(request)
                    _conn_sem.release()
            threading.Thread(target=_handle, daemon=True).start()
        else:
            # All 8 slots busy — drop connection so client retries immediately
            try: self.shutdown_request(request)
            except Exception: pass

    _srv.__class__.process_request = _bounded_process_request
    log.info(f"Server ready (bounded 8 threads, HTTPS={_ssl is not None})")
    _srv.serve_forever()
