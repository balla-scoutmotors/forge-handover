# COMPONENTS.md — Components, modules, and workstreams

---

## Local vision app *(vision-processing host)*

| Component | Trigger / entry | Function | Depends on | Inputs → Outputs | Hands off to |
|---|---|---|---|---|---|
| `streamlit_defect_app.py` — UI & orchestrator | Image upload + Run button | Runs the full local pipeline: loads MES, runs YOLO inference, correlates welds, builds and publishes events | `defect_classes.py`, `defect_weld_resolver.py`, `detection_event_publisher.py`, `simple_mes.json`, `.pt` weights | Images + sidebar config → event dicts, annotated overlays, run artifacts | `detection_event_publisher.py` |
| `defect_classes.py` — classification & enrichment | Called per detected box | Converts YOLO class name to typed defect object (`Burr`, `EdgeWeld`, `Spatter`, `DarkWeldSpot`); attaches causes and MES metadata via `DefectFactory` | `simple_mes.json` | Class name + camera path → typed `Defect` with causes, operator, cell, assembly fields | Orchestrator |
| `defect_weld_resolver.py` — weld correlation | Called after detection, per image | Loads weld map; assigns each defect to nearest weld by Euclidean distance; no cutoff; many-to-one allowed | `weld_match.json` (local fallback), S3 weld-map, or Jetson cache | Bounding boxes + part type / step → `weld_id`, location, distance px, `weld_matched` flag per defect | Orchestrator |
| `detection_event_publisher.py` — event publishing | Called per event | Builds per-event JSON + MD; uploads both to S3; degrades to local-only if credentials absent | `.env` credentials, `streamlit_markdown_converter.py`, boto3 | Event dict → `detections/defect-json/<uuid>.json` + `detections/defect-md/<uuid>.md` in S3 | AWS S3 |
| `streamlit_markdown_converter.py` — format conversion | Called by publisher and run-output writer | Converts event dicts to human-readable Markdown for per-event and run-level reports | Event dict | Event dict → Markdown string | Publisher (per-event MD), run-output writer (run-level MD) |

**Static assets:** `.pt` YOLO model weights · `simple_mes.json` (MES hierarchy — drives all dropdowns) · `weld_match.json` (local fallback weld map)

---

## Cloud transport *(AWS — external to Docker stack)*

| Component | Trigger / entry | Function | Depends on | Inputs → Outputs | Hands off to |
|---|---|---|---|---|---|
| **AWS Lambda** (`lambda_function.py`) | S3 `PutObject` event on `forge-project-data` | Extracts UUID from incoming S3 key; derives sibling key by swapping prefix and extension (`defect-json/` ↔ `defect-md/`); fetches both files; assembles combined message; publishes to SQS | S3 (read), SQS (write); IAM role `lambda-trigger-role` | S3 event → `{ correlation_id, markdown, metadata }` on SQS | SQS → embed-worker |

---

## Server stack *(Docker Compose on EC2)*

| Component | Trigger / entry | Function | Depends on | Inputs → Outputs | Hands off to |
|---|---|---|---|---|---|
| **Embed worker** (`embed-json.py`) | SQS long-poll (20 s, 1 message at a time) | Embeds Markdown via `nomic-embed-text`; flattens metadata; upserts point to Qdrant; writes cold-storage copy to S3; deletes SQS message on success | SQS, Ollama, Qdrant, S3 | SQS message → Qdrant point + S3 `detections/defect-points/<date>_<uuid>.json` | Qdrant |
| **Qdrant** | REST API | Stores and serves vector points for `detection_events` collection; enforces payload indexes on `defect_name` (text) and `ingested_at` (integer) | Docker volume `qdrant-data` | Upsert / query / scroll / count → points / search results | Dashboard, Qdrant-purge |
| **Ollama** | REST API | Serves `nomic-embed-text` (dense embeddings) and `qwen2.5:7b` (answer generation) | NVIDIA GPU; model weights on host volume at `/home/ubuntu/.ollama` | Text → embedding vector or streamed generated text | Embed worker, Dashboard |
| **Dashboard** (`app.py`) | HTTP request to `:8501` | Streamlit query interface; routes intent across 5 modes (count, defect_id, filter, trend/aggregate, vector); pre-computes parameter assessments against `Truth.json`; generates grounded LLM answers; renders live detection feed | Qdrant, Ollama, `Truth.json`, `intent_parser.py` | User query → detection cards + streamed LLM answer | Engineer / operator |
| **Intent parser** (`intent_parser.py`) | Called per user query by Dashboard | Deterministic spaCy `EntityRuler` NLP (~5 ms); extracts defect type, station, operator / robot ID, confidence band (min/max), date range, and query mode — never calls the LLM | spaCy `en_core_web_sm`, `dateparser`, `Truth.json` | Raw query string → structured intent dict (`defect_type`, `station`, `operator`, `confidence_min/max`, `date_str`, `days_back`, `mode`, `defect_id`) | `app.py` routing logic |
| **Qdrant-purge** (`purge.py`) | Container start, then every `PURGE_INTERVAL_HOURS` | Deletes all points where `ingested_at` is older than the retention window via a server-side range filter — no client-side scrolling | Qdrant | Timer → deleted points | — |

---

## Orchestration / sequence

```
[Image upload]
      │
      ▼
[streamlit_defect_app.py] ── simple_mes.json
      │
      ├── model.predict()                         (YOLO detection)
      ├── resolve_nearest_weld()                  (weld correlation)
      ├── build_reasoning_overlay()               (explainability overlay)
      ├── classify_defect_for_path()              (MES enrichment → event dict)
      └── publish_event() → detection_event_publisher
                └── streamlit_markdown_converter
                        │
                        ▼
                   [AWS S3]  detections/defect-json/ + defect-md/
                        │  S3 PutObject event
                        ▼
                   [AWS Lambda]  (AWS-managed, not Docker)
                   pairs files by UUID → SQS message
                        │
                        ▼
                   [embed-worker]  long-polls SQS
                        │
                        ├── nomic-embed-text (Ollama) → embedding vector
                        ├── Qdrant PUT (point + payload)
                        └── S3 PUT (cold-storage copy)
                                    │
                                    ▼
                              [Qdrant]  detection_events
                                    │
                        [Engineer] → [Dashboard :8501]
                                      ├─ intent_parser (spaCy, ~5 ms)
                                      ├─ Qdrant retrieval (count / defect_id / filter / trend / vector)
                                      ├─ parameter assessment (Python, Truth.json)
                                      └─ qwen2.5:7b (Ollama) → streamed answer
```

**Guardrails:**
- Dashboard controls hidden if no valid station / camera path resolves in `simple_mes.json`
- Embed-worker skips SQS messages with empty Markdown content
- S3 cold-storage failure is non-fatal — Qdrant write still proceeds
- Local vision app degrades to local-only artifact writing if S3 credentials are absent
- Dashboard image uses `COPY` — source changes require `docker compose up -d --build dashboard`
