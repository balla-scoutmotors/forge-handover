# ARCHITECTURE.md — End-to-end design 

## Overview 

The FORGE Detection Portal ingests per-event defect artifacts (JSON + Markdown) paired to synthetic MES data and produced by a local vision system. It then embeds them into a vector store via a cloud transport pipeline, and exposes a conversational AI interface so manufacturing engineers can query, count, and analyze defections in natural language. Inputs are camera frames processed on-premises; outputs are grounded natural-language answers and a live detection feed rendered in a Streamlit dashboard. 


---  

## Component diagram 
 
``` 

[Camera / image input] 

        │ 

        ▼ 

[Local vision app]  ──────────────────────────────────────────────────────────────────┐ 

  • YOLO detection engine                                                             │ 

  • Domain-enrichment layer (MES-like JSON hierarchy)                                 │ 

  • Spatial-correlation layer (weld map, optional)                                    │ 

  • Event publishing (per-event UUID.json + UUID.md → S3)                             │ 

        │                                                                             │  

        ▼                                                                             │ 

[AWS S3]  defect-json/<uuid>.json                                           (local UI: annotated 

           defect-md/<uuid>.md                                                overlays, config) 

        │ 

        │  S3 event notification 

        ▼ 

[AWS Lambda]  forge-detection-events-lambda 

  • Pairs JSON + MD by UUID 

  • Assembles combined message {correlation_id, markdown, metadata} 

        │ 

        ▼ 

[AWS SQS]  forge-detection-events-queue 

        │  long-poll (20 s) 

        ▼ 

[embed-worker]  (Docker Compose) 

  • nomic-embed-text via Ollama → dense vector 

  • Qdrant PUT + SQS delete 

  • S3 cold-storage write 

        │                        │ 

        ▼                        ▼ 

[S3 cold storage]         [Qdrant]  detection_events collection 

embedded/<date>/            • payload indexes: defect_name, ingested_at 

<uuid>.json                         │ 

                                    │ 

                                    ▼ 

                          [dashboard-frontend]  (Docker Compose on EC2) 

                            • spaCy intent parser 

                            • Qdrant retrieval (count / defect_id / filter / trend / vector) 

                            • qwen2.5:7b via Ollama → grounded answer 

                                    │ 

                                    ▼ 

                            [Engineer / operator] 

``` 

 
--- 
 
## Data / work flow 


### Local pipeline (vision app) 
 

1. The operator configures station, camera(s), model path, confidence threshold, part type, and capture step via a Streamlit UI on the local machine. 

2. Camera frames are fed to the **Ultralytics YOLO** engine (pretrained or fine-tuned weights) which returns raw bounding-box detections. 

3. A **domain-enrichment factory** converts each raw detection into a structured defect object: defect name, confidence, causes, operator/cell metadata, and station/camera path — sourced from a MES-like JSON hierarchy (`body shop → station → camera → robot`). 

4. When weld map data is available for the configured part type and step, a **spatial-correlation layer** assigns each defect to its nearest weld by Euclidean distance in image space. Welds are not detected by YOLO; their coordinates come from a JSON map (`weld_match.json` or S3). No distance cutoff — every defect always takes its nearest weld; many-to-one mapping is allowed. 

5. Each event is persisted as two files sharing a UUID suffix and uploaded to S3 in separate prefixes: 

   - `defect-json/<uuid>.json` — structured metadata 

   - `defect-md/<uuid>.md` — human-readable Markdown report 

6. Batch/run-level artifacts (aggregated JSON + Markdown report + latest-pointer JSON) are written locally but are not part of the S3 transport path. 

 
### Cloud transport (AWS) 

7. An **S3 event notification** fires on every new object upload, triggering the **AWS Lambda** (`lambda_function.py`). 

8. Lambda parses the incoming key to extract the UUID and file type, then derives the sibling key by swapping prefix and extension. Whichever file arrives first, Lambda fetches both from S3. 

9. Lambda assembles a single combined message `{ correlation_id, markdown, metadata }` and publishes it to the **SQS queue** (`forge-detection-events-queue`). The correlation UUID becomes the canonical `defect_ID` throughout the downstream system. 


### Ingest / embed (embed-worker) 

10. The **embed-worker** long-polls SQS (20 s wait, 1 message at a time). 

11. The Markdown content is sent to **`nomic-embed-text`** via Ollama to produce a dense embedding vector. 

12. Nested metadata fields are flattened: `causes` sub-keys are stored without prefix; all other nested keys are prefixed `<parent>_<child>`. 

13. A point is written to **Qdrant** (`detection_events` collection) with: embedding vector, all flattened metadata fields, `defect_ID` (correlation UUID), `content` (raw Markdown, used as LLM context), and `ingested_at` (Unix epoch integer). 

14. The SQS message is deleted on success. 

15. A cold-storage copy (`payload + vector`) is written to S3 under `detections/defect-points/<YYYYMMDD>_<uuid>.json` so historical data can be rehydrated without re-embedding. 
 

### Query / inference (dashboard-frontend) 

16. On startup the dashboard warms both Ollama models (`nomic-embed-text`, `qwen2.5:7b`) in a background thread and ensures the two Qdrant payload indexes (`defect_name` text, `ingested_at` integer) exist. 

17. A user's free-text query is passed to the **spaCy intent parser** (`intent_parser.py`) — a deterministic rule-based NLP layer (~5 ms) that extracts structured intent (defect type, station, robot, confidence band, date range, query class) without calling the LLM. 

18. Based on intent, one of five retrieval paths runs: 

    - **Count** — regex detects counting intent; Qdrant exact count filtered by `defect_name` (plus any station / date / operator scope). No LLM call for count-only queries. 

    - **Defect ID** — hex UUID detected in query; fetches the specific Qdrant point(s) by `defect_ID` payload field. 

    - **Filter** — structured fields known (station, defect type, date) but no count intent; Qdrant scroll with payload filter, no vector search. 

    - **Trend / aggregate** — keywords like "overview", "distribution", "trends" detected; filtered scroll aggregated in Python (defect breakdown, station counts, out-of-range parameter frequency, confidence average). 

    - **Vector** — fallback; query embedded with `nomic-embed-text`, cosine similarity search in Qdrant with optional payload filters. 

19. Retrieved points (Markdown `content` + metadata) are assembled into a grounded prompt and sent to **`qwen2.5:7b`** via Ollama for natural-language answer generation; the response streams to the UI. 

20. Separately, the **detection feed** renders the latest N events from Qdrant (`order_by ingested_at desc`) as styled cards with confidence badges, station/camera labels, and cause metadata enriched from `Truth.json`. 

 
--- 


## Design rationale 
 

The pipeline is split into a local vision stage and a cloud/server stage so that the computationally expensive YOLO inference runs close to the cameras on existing shop-floor hardware, while the embedding, storage, and query workloads run on a dedicated server. S3 + SQS acts as a durable, decoupled buffer: the local app can continue capturing if the server is temporarily unavailable, and messages are not lost. 

 
The dual-file (JSON + MD) approach means the embed-worker always has both machine-readable metadata and human-readable text. The Markdown is embedded — not the JSON — because natural-language text produces more semantically useful vectors for the kinds of conversational queries engineers ask. The JSON populates Qdrant payload fields for structured filtering. 


The spaCy intent parser replaced an earlier `qwen2.5:3b` LLM call for intent extraction because the 3b model hallucinated field names and was ~500 ms per call. The deterministic parser is ~5 ms, never hallucinates, and is fully reproducible. `qwen2.5:7b` is retained only for final answer generation where language quality matters. 
 

Detailed decision records are in [DECISIONS.md](DECISIONS.md). 