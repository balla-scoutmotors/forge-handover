# DATA_AND_SYSTEMS.md — Operational data stores and systems

These are stores the project reads from and writes to.

---

| Store / Type | Location / Access | Schema | Owner / permissions | Failure → recovery |
|---|---|---|---|---|
| AWS S3 — detection artifacts | `forge-project-data` bucket, prefixes `detections/defect-json/` and `detections/defect-md/` (read/write) — pointer in CONFIG.md | Key pattern: `<prefix>/<uuid>.json` and `<prefix>/<uuid>.md`; JSON fields defined by `DetectionEventPublisher` | AWS account owner — IAM key in `.env` | See RUNBOOK; local run artifacts still written to disk if S3 is unavailable |
| AWS SQS — event transport queue | `forge-detection-events-queue` (us-east-2) — URL in CONFIG.md (read/write) | Message body: `{ correlation_id, markdown, metadata }` | AWS account owner | Messages persist in queue; embed-worker retries on next poll cycle; no data loss |
| Qdrant — vector store | `http://qdrant:6333`, collection `detection_events` (read/write) — pointer in CONFIG.md | Payload fields: `defect_name`, `ingested_at`, `defect_ID`, `content`, `confidence`, `station`, `cell_number`, `operator`, `part_type`, flattened cause parameters | Server stack operator — API key in `.env` as `QDRANT_API_KEY` | See RUNBOOK — Qdrant restart is non-destructive; data persisted on Docker volume `qdrant-data` |
| S3 — cold storage (embedded points) | `forge-project-data` bucket, prefix `detections/defect-points/` (write only) — pointer in CONFIG.md | `{ payload, vector }` per point; filename: `<YYYYMMDD>_<uuid>.json` | AWS account owner | Non-fatal if write fails — Qdrant write still succeeds; use for recovery rehydration only |
| Local run artifacts | Vision host Downloads folder (or configured path) — write only | Timestamped `streamlit_detected_events_<ts>.json`, `streamlit_detected_events_<ts>.md`, and stable `streamlit_detected_events.json` | Vision host operator | Not uploaded to S3; used for local audit and debugging only |
