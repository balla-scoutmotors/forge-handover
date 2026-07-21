# SOURCES.md — Reference inputs (read-only)

These are inputs the project reads but does not own or write to.

---

| Source / Type | Location | Access / identity | Owner / cadence | Failure → recovery |
|---|---|---|---|---|
| YOLO model weights (`.pt` file) | Local filesystem on vision host — path configured in sidebar at runtime | Filesystem read | ML team — update manually | App shows model load error; restore file and restart |
| MES hierarchy (`simple_mes.json`) | `forge/simple_mes.json` in project source | Filesystem read | Engineering team — update manually | App halts and shows error if missing or malformed; restore from source control |
| Weld coordinate map (`weld_match.json`) | `forge/weld_match.json` (local fallback); S3 `weld-maps/<part_type>/step_<n>.json`; Jetson cache `/tmp/forge_weld_maps/` | Filesystem / S3 read | Engineering team — update per part type / capture step | Falls back through priority chain: Jetson cache → S3 → local file; if all fail, weld correlation is skipped and defects are marked unresolved |
| Defect reference data (`Truth.json`) | `trigger-detection/dashboard-frontend/Truth.json` — baked into dashboard Docker image at build time | Baked into image | Engineering team — requires `docker compose up -d --build dashboard` after changes | Dashboard renders without parameter ranges or corrective actions if absent; rebuild image |
