import os
import json
import time
import re
import threading
import queue
import requests
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from email.parser import BytesParser
from email.policy import default

PORT_DEFAULT = int(os.environ.get('PORT', '8002'))
MAX_PARALLEL = 4
CHUNK_TARGET = 3000          # caractere țintă per chunk
KEEP_ALIVE = "30m"
# Prefix pentru reverse proxy (ex. "/translator"). Gol pentru servire la root.
BASE_PATH = os.environ.get('BASE_PATH', '').rstrip('/')
JOB_QUEUES = {}
STOP_FLAGS = {}
OUTPUT_FILES = {}

try:
    from pypdf import PdfReader
    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    from langdetect import detect, DetectorFactory, LangDetectException
    DetectorFactory.seed = 0
    HAS_LANGDETECT = True
except ImportError:
    HAS_LANGDETECT = False

try:
    from deep_translator import GoogleTranslator
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

LANG_ISO_TO_NAME = {
    'ro': 'Romanian', 'la': 'Latin', 'el': 'Greek', 'en': 'English',
    'de': 'German', 'fr': 'French', 'it': 'Italian', 'es': 'Spanish',
    'pt': 'Portuguese', 'ru': 'Russian', 'pl': 'Polish', 'nl': 'Dutch',
    'sv': 'Swedish', 'tr': 'Turkish', 'ar': 'Arabic', 'zh-cn': 'Chinese',
    'ja': 'Japanese', 'ko': 'Korean', 'hu': 'Hungarian', 'cs': 'Czech',
    'bg': 'Bulgarian', 'uk': 'Ukrainian', 'sr': 'Serbian', 'hr': 'Croatian',
    # langdetect nu are model pentru latină — confundă Latin cu Catalan/Italian
    'ca': 'Catalan (possibly Latin)',
}

# Nume englez de limbă → cod ISO 639-1 pentru Google Translate
NAME_TO_GOOGLE_ISO = {
    'romanian': 'ro', 'latin': 'la', 'greek': 'el', 'english': 'en',
    'german': 'de', 'french': 'fr', 'italian': 'it', 'spanish': 'es',
    'portuguese': 'pt', 'russian': 'ru', 'polish': 'pl', 'dutch': 'nl',
    'swedish': 'sv', 'turkish': 'tr', 'arabic': 'ar', 'chinese': 'zh-CN',
    'japanese': 'ja', 'korean': 'ko', 'hungarian': 'hu', 'czech': 'cs',
    'bulgarian': 'bg', 'ukrainian': 'uk', 'serbian': 'sr', 'croatian': 'hr',
    'catalan': 'ca',
    'auto': 'auto',
}


def name_to_iso(name):
    """Normalizează un nume de limbă la cod ISO Google ('auto' dacă nu îl știe)."""
    if not name:
        return 'auto'
    key = name.strip().lower()
    if key in NAME_TO_GOOGLE_ISO:
        return NAME_TO_GOOGLE_ISO[key]
    # ex. "Catalan (possibly Latin)" → catalan
    for token in re.split(r'[\s()]+', key):
        if token in NAME_TO_GOOGLE_ISO:
            return NAME_TO_GOOGLE_ISO[token]
    return 'auto'


class OllamaClient:
    def __init__(self, base_url="http://127.0.0.1:11434"):
        self.base_url = base_url

    def set_url(self, ip, port):
        ip = ip.strip()
        port = port.strip()
        if not ip.startswith("http"):
            self.base_url = f"http://{ip}:{port}"
        else:
            self.base_url = f"{ip}:{port}"

    def get_models(self):
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code == 200:
                return [m['name'] for m in r.json().get('models', [])]
            return []
        except Exception:
            return []

    def generate(self, model, prompt):
        try:
            r = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": {"temperature": 0.2, "num_ctx": 8192},
                },
                timeout=600,
            )
            if r.status_code == 200:
                return r.json().get('response', "").strip()
            return f"Eroare HTTP {r.status_code}"
        except Exception as e:
            return f"Eroare: {e}"


def detect_lang(text):
    """Întoarce numele englez al limbii sau None dacă nu poate decide."""
    if not HAS_LANGDETECT:
        return None
    sample = text.strip()
    if len(sample) < 25:
        return None
    try:
        code = detect(sample)
        return LANG_ISO_TO_NAME.get(code, code)
    except LangDetectException:
        return None


def split_paragraphs(text):
    """Sparge pe linii goale; păstrează paragrafele non-vide."""
    parts = re.split(r'\n\s*\n+', text)
    return [p.strip() for p in parts if p.strip()]


def build_chunks(text, target_size, auto_detect):
    """
    Returnează listă de (chunk_text, lang_name_or_None).
    Grupează paragrafe consecutive cu aceeași limbă până la target_size char.
    Dacă auto_detect=False, lang_name e None pe toate.
    """
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return []

    chunks = []
    cur_text = []
    cur_lang = None
    cur_len = 0

    for p in paragraphs:
        p_lang = detect_lang(p) if auto_detect else None
        # Dacă limba schimbă SAU chunk-ul e plin → flush
        if cur_text and (p_lang != cur_lang or cur_len + len(p) > target_size):
            chunks.append(("\n\n".join(cur_text), cur_lang))
            cur_text = []
            cur_len = 0
        cur_text.append(p)
        cur_lang = p_lang if p_lang else cur_lang
        cur_len += len(p) + 2

    if cur_text:
        chunks.append(("\n\n".join(cur_text), cur_lang))
    return chunks


def translate_chunk(idx, chunk_text, src_lang, tgt_lang, glossary, model, client, stop_flag):
    """Variantă Ollama (LLM local)."""
    if stop_flag():
        return idx, None, src_lang
    glossary_str = json.dumps(glossary, ensure_ascii=False)
    prompt = (
        f"You are a professional translator. Translate the following text "
        f"into {tgt_lang}. The detected source language is {src_lang}, but if "
        f"you recognize a different language (especially Latin, which auto-"
        f"detection often misclassifies), translate from the actual language. "
        f"Keep formatting, line breaks and any proper nouns intact. Apply this "
        f"glossary when applicable: {glossary_str}\n\n"
        f"--- TEXT START ---\n{chunk_text}\n--- TEXT END ---\n\n"
        f"Reply with ONLY the translated text, without explanations."
    )
    raw = client.generate(model, prompt)
    cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
    return idx, cleaned, src_lang


def apply_glossary(text, glossary):
    """Înlocuire post-traducere termen→traducere (Google nu suportă glossary nativ)."""
    if not glossary:
        return text
    for k, v in glossary.items():
        try:
            text = re.sub(re.escape(k), v, text, flags=re.IGNORECASE)
        except re.error:
            pass
    return text


def translate_chunk_google(idx, chunk_text, src_lang, tgt_lang, glossary, stop_flag):
    """Variantă Google Translate (cloud free, prin deep-translator)."""
    if stop_flag():
        return idx, None, src_lang
    src_iso = name_to_iso(src_lang)
    tgt_iso = name_to_iso(tgt_lang)
    if tgt_iso == 'auto':
        tgt_iso = 'ro'
    try:
        translator = GoogleTranslator(source=src_iso, target=tgt_iso)
        # Google Translate are limită ~5000 char; chunk-urile noastre sunt ~3000, OK
        translated = translator.translate(chunk_text) or ''
        translated = apply_glossary(translated, glossary)
        return idx, translated, src_lang
    except Exception as e:
        return idx, f"[Eroare Google {src_iso}→{tgt_iso}: {e}]", src_lang


def translate_worker(job_id, engine, model, source_lang, target_lang, chunks_with_lang, glossary, ollama_client):
    q = JOB_QUEUES[job_id]
    stop_flag = lambda: STOP_FLAGS.get(job_id, False)
    total = len(chunks_with_lang)
    label = f"engine={engine}" + (f", model={model}" if engine == 'ollama' else "")
    q.put(f"🚀 Job {job_id}: {total} chunks, {MAX_PARALLEL} workers paraleli, {label}\n")

    # Loguri rezumat limbi
    if source_lang.strip().lower() == 'auto':
        from collections import Counter
        langs_summary = Counter([l or 'unknown' for _, l in chunks_with_lang])
        q.put(f"🔍 Limbi detectate: {dict(langs_summary)}\n")

    results = [None] * total           # text tradus indexat
    streamed_up_to = [0]               # cât din rezultat am emis în ordine
    log_data = {"job_id": job_id, "model": model, "results": []}

    def flush_in_order():
        """Emite în UI tot ce e gata în ordine continuă."""
        while streamed_up_to[0] < total and results[streamed_up_to[0]] is not None:
            i = streamed_up_to[0]
            q.put(f"stream:{json.dumps(results[i] + chr(10)+chr(10))}\n")
            streamed_up_to[0] += 1

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        futures = {}
        for i, (chunk_text, detected_lang) in enumerate(chunks_with_lang):
            if source_lang.strip().lower() == 'auto':
                use_src = detected_lang or ('auto' if engine == 'google' else 'the source language')
            else:
                use_src = source_lang
            if engine == 'google':
                futures[pool.submit(
                    translate_chunk_google, i, chunk_text, use_src, target_lang,
                    glossary, stop_flag
                )] = i
            else:
                futures[pool.submit(
                    translate_chunk, i, chunk_text, use_src, target_lang,
                    glossary, model, ollama_client, stop_flag
                )] = i

        done_count = 0
        for fut in as_completed(futures):
            if stop_flag():
                q.put("🛑 OPRIT DE UTILIZATOR.\n")
                pool.shutdown(wait=False, cancel_futures=True)
                break
            try:
                idx, translated, used_src = fut.result()
            except Exception as e:
                idx = futures[fut]
                translated = f"[Eroare chunk {idx}: {e}]"
                used_src = source_lang
            if translated is None:
                continue
            results[idx] = translated
            log_data["results"].append({
                "index": idx,
                "source_lang": used_src,
                "original": chunks_with_lang[idx][0],
                "translated": translated,
            })
            done_count += 1
            q.put(f"✅ [{done_count}/{total}] chunk {idx+1} ({used_src})\n")
            flush_in_order()

    log_data["engine"] = engine
    full_text = "\n\n".join(r if r else "[lipsă]" for r in results)
    OUTPUT_FILES[job_id] = {
        "txt": full_text,
        "log": json.dumps(log_data, indent=2, ensure_ascii=False),
    }
    q.put("done")


class TranslateHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML_UI.replace('__BASE_PATH__', BASE_PATH).encode('utf-8'))
        elif parsed_path.path == '/models':
            qs = parse_qs(parsed_path.query)
            ip = qs.get('ip', ['127.0.0.1'])[0]
            port = qs.get('port', ['11434'])[0]
            client = OllamaClient()
            client.set_url(ip, port)
            models = client.get_models()
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(models).encode())
        elif parsed_path.path == '/status':
            jid = parse_qs(parsed_path.query).get('job_id', [''])[0]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self._send_cors_headers()
            self.end_headers()
            q = JOB_QUEUES.get(jid)
            if q:
                while True:
                    try:
                        msg = q.get(timeout=120)
                        if msg == "done":
                            self.wfile.write(b"event: done\ndata: \n\n")
                            break
                        if msg.startswith("stream:"):
                            self.wfile.write(f"event: stream\ndata: {msg[7:]}\n\n".encode())
                        else:
                            self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    except Exception:
                        break
        elif parsed_path.path == '/download_file':
            params = parse_qs(parsed_path.query)
            jid = params.get('job_id', [''])[0]
            t = params.get('type', ['txt'])[0]
            content = OUTPUT_FILES.get(jid, {}).get(t, "Fișier negăsit.")
            self.send_response(200)
            self.send_header('Content-Disposition', f'attachment; filename=traducere_{jid}.{t}')
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))

    def do_POST(self):
        if self.path == '/translate':
            ctype = self.headers.get('Content-Type')
            clen = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(clen)

            msg = BytesParser(policy=default).parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body
            )
            job_id = str(int(time.time() * 1000))

            engine = "ollama"
            model = "qwen2.5:14b"
            src = "auto"
            tgt = "Romanian"
            gloss = {}
            text = ""
            ollama_ip = "127.0.0.1"

            for part in msg.iter_parts():
                name = part.get_param("name", header="Content-Disposition")
                if name == "engine":
                    engine = part.get_payload(decode=True).decode().strip().lower()
                elif name == "model":
                    model = part.get_payload(decode=True).decode().strip()
                elif name == "source_lang":
                    src = part.get_payload(decode=True).decode().strip()
                elif name == "target_lang":
                    tgt = part.get_payload(decode=True).decode().strip()
                elif name == "ollama_ip":
                    ollama_ip = part.get_payload(decode=True).decode().strip()
                elif name == "glossary":
                    try:
                        gloss = json.loads(part.get_payload(decode=True).decode())
                    except Exception:
                        gloss = {}
                elif name == "file":
                    fn = part.get_filename()
                    payload = part.get_payload(decode=True)
                    if fn.endswith('.pdf') and HAS_PDF:
                        pdf = PdfReader(io.BytesIO(payload))
                        text = "\n\n".join([p.extract_text() or '' for p in pdf.pages])
                    elif fn.endswith('.docx') and HAS_DOCX:
                        doc = Document(io.BytesIO(payload))
                        text = "\n\n".join([p.text for p in doc.paragraphs])
                    else:
                        text = payload.decode('utf-8', errors='ignore')

            if not text or not text.strip():
                self.send_response(400)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"error":"text gol"}')
                return

            auto = src.strip().lower() == 'auto'
            chunks = build_chunks(text, CHUNK_TARGET, auto)
            if not chunks:
                self.send_response(400)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"error":"nu s-au putut forma chunks"}')
                return

            if engine == 'google' and not HAS_GOOGLE:
                self.send_response(400)
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"error":"deep-translator nu este instalat"}')
                return

            JOB_QUEUES[job_id] = queue.Queue()
            STOP_FLAGS[job_id] = False

            client = OllamaClient()
            client.set_url(ollama_ip, "11434")

            threading.Thread(
                target=translate_worker,
                args=(job_id, engine, model, src, tgt, chunks, gloss, client),
                daemon=True,
            ).start()

            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "job_id": job_id,
                "chunks": len(chunks),
                "auto_detect": auto,
                "engine": engine,
            }).encode())

        elif self.path == '/stop':
            clen = int(self.headers.get('Content-Length', 0))
            jid = json.loads(self.rfile.read(clen).decode()).get('job_id')
            if jid in STOP_FLAGS:
                STOP_FLAGS[jid] = True
            self.send_response(200)
            self._send_cors_headers()
            self.end_headers()


HTML_UI = """<!DOCTYPE html><html><head><meta charset="UTF-8"><title>AI Translator RTX 5090</title>
<style>
    body { font-family: sans-serif; background: #0f172a; color: #f1f5f9; padding: 20px; }
    .box { max-width: 900px; margin: auto; background: #1e293b; padding: 20px; border-radius: 8px; }
    input, select, textarea, button { width: 100%; padding: 10px; margin: 5px 0; border-radius: 4px; border: 1px solid #334155; background: #0f172a; color: white; font-size: 14px; }
    button { background: #0284c7; cursor: pointer; font-weight: bold; border: none; }
    button:hover { background: #0369a1; }
    #log { height: 180px; overflow-y: auto; background: #000; padding: 10px; font-family: monospace; font-size: 12px; margin-top: 10px; border: 1px solid #334155; border-radius: 4px; white-space: pre-wrap; }
    #output { height: 280px; overflow-y: auto; background: #020617; padding: 10px; font-family: 'Georgia', serif; font-size: 14px; margin-top: 10px; border: 1px solid #334155; border-radius: 4px; white-space: pre-wrap; line-height: 1.5; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
    .tag { display: inline-block; padding: 2px 8px; background: #0369a1; border-radius: 4px; font-size: 11px; margin-right: 4px; }
    .stop { background: #dc2626 !important; }
    h2 { margin: 0 0 12px 0; }
    label { font-size: 12px; color: #94a3b8; }
</style></head>
<body>
<div class="box">
    <h2>🚀 Traducător Documente Multi-Limbă · Port 8002</h2>
    <p style="color:#94a3b8;font-size:13px;margin:0 0 10px 0;">
        <span class="tag">paralelism ×4</span>
        <span class="tag">auto-detect /paragraf</span>
        <span class="tag">chunks ~3000 char</span>
        <span class="tag">keep_alive 30m</span>
    </p>

    <label>Engine traducere</label>
    <select id="engine" onchange="onEngineChange()">
        <option value="ollama" selected>🖥️ Ollama (local, privat, RTX 5090)</option>
        <option value="google">☁️ Google Translate (cloud free, rapid, 100+ limbi)</option>
    </select>

    <div id="ollamaConfig">
        <div class="grid">
            <div>
                <label>IP Ollama (Tailscale)</label>
                <input type="text" id="ip" value="100.67.104.36">
            </div>
            <div>
                <label>&nbsp;</label>
                <button onclick="fetchModels()">1. Scanare Modele</button>
            </div>
        </div>

        <label>Model</label>
        <select id="model"><option>Scanează mai întâi...</option></select>
    </div>

    <div class="grid">
        <div>
            <label>Limba sursă</label>
            <select id="src">
                <option value="auto" selected>🔍 Auto-detect per paragraf</option>
                <option value="English">English</option>
                <option value="German">German</option>
                <option value="French">French</option>
                <option value="Italian">Italian</option>
                <option value="Spanish">Spanish</option>
                <option value="Portuguese">Portuguese</option>
                <option value="Latin">Latin</option>
                <option value="Greek">Greek</option>
                <option value="Russian">Russian</option>
                <option value="Polish">Polish</option>
                <option value="Hungarian">Hungarian</option>
                <option value="Romanian">Romanian</option>
            </select>
        </div>
        <div>
            <label>Limba țintă</label>
            <select id="tgt">
                <option value="Romanian" selected>Română</option>
                <option value="English">English</option>
                <option value="German">German</option>
                <option value="French">French</option>
                <option value="Italian">Italian</option>
                <option value="Spanish">Spanish</option>
            </select>
        </div>
    </div>

    <label>Fișier (.pdf / .docx / .txt)</label>
    <input type="file" id="file" accept=".pdf,.docx,.txt">

    <label>Glosar JSON (opțional)</label>
    <textarea id="gloss" rows="2">{}</textarea>

    <div class="grid">
        <button id="startBtn" onclick="start()" style="background:#10b981">START TRADUCERE</button>
        <button id="stopBtn" onclick="stop()" class="stop">STOP</button>
    </div>

    <div id="status">Status: Gata.</div>
    <label>Log:</label>
    <div id="log"></div>
    <label>Traducere (live, în ordine):</label>
    <div id="output"></div>

    <div class="grid">
        <button onclick="download('txt')">Descarcă TXT</button>
        <button onclick="download('log')">Descarcă LOG JSON</button>
    </div>
</div>
<script>
    const BASE = '__BASE_PATH__';
    let currentJobId = null;
    async function fetchModels() {
        const ip = document.getElementById('ip').value;
        try {
            const res = await fetch(`${BASE}/models?ip=${ip}&port=11434`);
            const models = await res.json();
            const sel = document.getElementById('model');
            sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
            // selectează implicit qwen2.5:14b dacă există
            const def = models.find(m => m.includes('qwen2.5:14b')) || models.find(m => m.includes('gemma3:12b')) || models[0];
            if (def) sel.value = def;
            document.getElementById('status').innerText = `${models.length} modele.`;
        } catch(e) { alert("Eroare: Verifică Ollama (OLLAMA_HOST=0.0.0.0, OLLAMA_ORIGINS=*)"); }
    }
    function onEngineChange() {
        const eng = document.getElementById('engine').value;
        document.getElementById('ollamaConfig').style.display = (eng === 'ollama') ? '' : 'none';
    }
    async function start() {
        const file = document.getElementById('file').files[0];
        if(!file) return alert("Pune un fișier!");
        document.getElementById('log').innerText = '';
        document.getElementById('output').innerText = '';
        const eng = document.getElementById('engine').value;
        const fd = new FormData();
        fd.append('engine', eng);
        fd.append('model', document.getElementById('model').value);
        fd.append('source_lang', document.getElementById('src').value);
        fd.append('target_lang', document.getElementById('tgt').value);
        fd.append('ollama_ip', document.getElementById('ip').value);
        fd.append('glossary', document.getElementById('gloss').value);
        fd.append('file', file);

        const res = await fetch(`${BASE}/translate`, { method: 'POST', body: fd });
        const data = await res.json();
        if (data.error) { alert(data.error); return; }
        currentJobId = data.job_id;
        document.getElementById('status').innerText = `Job ${data.job_id} — ${data.chunks} chunks · ${data.engine} ${data.auto_detect ? '(auto-detect)' : ''}`;

        const es = new EventSource(`${BASE}/status?job_id=${currentJobId}`);
        es.onmessage = (e) => {
            const log = document.getElementById('log');
            log.innerText += e.data + "\\n";
            log.scrollTop = log.scrollHeight;
        };
        es.addEventListener('stream', (e) => {
            const txt = JSON.parse(e.data);
            const out = document.getElementById('output');
            out.innerText += txt;
            out.scrollTop = out.scrollHeight;
        });
        es.addEventListener('done', () => { es.close(); document.getElementById('status').innerText = "Gata!"; });
    }
    async function stop() {
        if(!currentJobId) return;
        await fetch(`${BASE}/stop`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_id: currentJobId})});
    }
    function download(t) { if(currentJobId) window.location.href=`${BASE}/download_file?job_id=${currentJobId}&type=${t}`; }
    window.addEventListener('load', () => { onEngineChange(); fetchModels(); });
</script>
</body></html>
"""


def run():
    print(f"--- SERVER TRADUCERE ACTIV PE PORTUL {PORT_DEFAULT} ---")
    print(f"    paralelism={MAX_PARALLEL}, chunk_target={CHUNK_TARGET}, keep_alive={KEEP_ALIVE}")
    print(f"    langdetect={'YES' if HAS_LANGDETECT else 'NO'}")
    print(f"    base_path={BASE_PATH or '(root)'}")
    server = ThreadingHTTPServer(('0.0.0.0', PORT_DEFAULT), TranslateHandler)
    server.serve_forever()


if __name__ == "__main__":
    run()
