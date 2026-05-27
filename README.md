# AI Translator

Traducător de documente multi-limbă cu paralelism, auto-detect per paragraf și două motoare la alegere:

- **Ollama (local, privat)** — LLM pe GPU (testat pe RTX 5090), prin Tailscale
- **Google Translate** (cloud free, prin `deep-translator`) — 100+ limbi

Server HTTP single-file (`stdlib` + câteva dependențe opționale), UI web inclus.

## Caracteristici

- **Paralelism ×4** chunks în lucru simultan (`ThreadPoolExecutor`)
- **Auto-detect** limbă per paragraf (`langdetect`) → grupare în chunks omogene ~3000 caractere
- **Glossary** termen→traducere (Ollama: în prompt, Google: post-traducere)
- **Streaming SSE** rezultat în ordine + log de progres
- **Stop** la cerere, **download** TXT + JSON log
- **Citire fișiere**: TXT, PDF (`pypdf`), DOCX (`python-docx`)
- `keep_alive=30m` pe Ollama (modelul rămâne încărcat între cereri)
- Workaround pentru limba latină (langdetect o confundă des cu Catalan/Italian) — instrucțiune explicită în prompt

## Instalare

```bash
pip install requests deep-translator langdetect pypdf python-docx
```

Doar `requests` este obligatoriu; restul activează capabilități extra (Google, auto-detect, PDF, DOCX).

## Pornire

```bash
python3 translatev5.py
```

Server pe `http://0.0.0.0:8002`.

## Configurare Ollama

Implicit conectează la `http://127.0.0.1:11434`. În UI poți schimba IP-ul (ex. nod Tailscale `100.67.104.36`) și apoi `Scanare modele`.

## Endpoint-uri HTTP

| Metodă | Path | Rol |
|---|---|---|
| GET | `/` | UI web |
| GET | `/models?ip=&port=` | Listă modele Ollama |
| POST | `/translate` | Pornește job (multipart: file, source_lang, target_lang, engine, model, ollama_ip, glossary) |
| GET | `/status?job_id=` | SSE: log + `event: stream` (text tradus) + `event: done` |
| POST | `/stop` | Oprire job |
| GET | `/download_file?job_id=&type=txt|log` | Download rezultat |

## Configurație internă

În capul fișierului `translatev5.py`:

| Constantă | Default | Rol |
|---|---|---|
| `PORT_DEFAULT` | `8002` | Portul HTTP |
| `MAX_PARALLEL` | `4` | Workers paraleli per job |
| `CHUNK_TARGET` | `3000` | Țintă caractere per chunk |
| `KEEP_ALIVE` | `30m` | Cât ține Ollama modelul în VRAM |

## Note

- Fără limită explicită pe job — depinde de RAM și throughput Ollama/Google.
- Google Translate are limită ~5000 char/cerere; chunk-urile ~3000 sunt sub prag.
- Glossary la Google e aplicat **post-traducere** (regex case-insensitive) pentru că API-ul gratuit nu suportă glossary nativ.

## Licență

Uz personal / cercetare.
