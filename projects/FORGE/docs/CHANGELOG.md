# CHANGELOG.md — Version history

---

## 0.1.0 — 2026-07-21

**Initial documented version.**

### Local vision app
- YOLO-based weld defect detection (`burr`, `edge_weld`, `spatter`, `dark_weld_spot`) via `star-circle-line.pt` model.
- MES enrichment via `simple_mes.json` (body shop → station → camera → robot hierarchy).
- Defect-to-weld spatial correlation via `weld_match.json` (no distance cutoff; many-to-one).
- Per-event S3 upload: `detections/defect-json/<uuid>.json` + `detections/defect-md/<uuid>.md`.
- Local run artifacts: timestamped JSON + MD + stable `latest.json`.

### Cloud transport
- AWS Lambda (`lambda_function.py`) bridges S3 PutObject events to SQS.
- Dual-file pairing by correlation UUID; combined message `{ correlation_id, markdown, metadata }`.

### Server stack
- Embed worker: SQS consumer → `nomic-embed-text` (Ollama) → Qdrant upsert + S3 cold-storage write.
- Dashboard: spaCy intent parser (replaced `qwen2.5:3b`); five query modes (count, defect_id, filter, trend/aggregate, vector); `qwen2.5:7b` answer generation with Python pre-computed parameter assessment.
- Qdrant-purge: 30-day retention, 24-hour interval, server-side range delete.

### Documentation
- Full docs suite created per Scout Motors Project Documentation & Knowledge-Preservation Standard.
