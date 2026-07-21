# PROCESSES.md — Automations, pipelines, and manual procedures

---

## Local vision app

| ID | Process | Type | Trigger | Run / audit history |
|---|---|---|---|---|
| L1 | Credential loading | Automated | App import at startup | App stdout / Streamlit error screen |
| L2 | MES data loading | Automated | App startup | App stdout / Streamlit error screen |
| L3 | YOLO model loading | Automated (cached) | First Run click per session | App stdout |
| L4 | Per-image detection pipeline | Automated | User uploads images and clicks Run | Streamlit progress bar + event table in UI |
| L5 | Event publishing to S3 | Automated | Per event, within L4 | App stdout; S3 console `detections/` prefix |

---

## Server stack

| ID | Process | Type | Trigger | Run / audit history |
|---|---|---|---|---|
| P0 | Initial stack setup | Manual | First deploy | N/A |
| P1 | Detection ingest pipeline | Automated | S3 `PutObject` event | SQS queue depth; `docker compose logs embed-worker` |
| P2 | Qdrant hot-store purge | Automated | Container start, then every `PURGE_INTERVAL_HOURS` | `docker compose logs qdrant-purge` |
| P3 | Dashboard model warm-up | Automated | First Streamlit session after container start | `docker compose logs dashboard` |
| P4 | Dashboard rebuild after code change | Manual | On any change to `app.py`, `intent_parser.py`, `styles.css`, or `Truth.json` | `docker compose logs dashboard` after rebuild |
| P5 | Truth.json defect-type update | Manual | On defect taxonomy change | Requires P4 (image rebuild) to take effect |
| P6 | Full stack restart | Manual | On demand / after host reboot | `docker compose ps` |
| P7 | `ingested_at` backfill (one-time) | Manual | On recovery of pre-backfill historical points | `python backfill.py` stdout |
| P8 | EC2 instance start (after daily shutdown) | Manual | Next working day / on demand after 5 PM auto-shutdown | AWS EC2 console or CLI |
| P9 | Lambda deployment / update | Manual | On change to `lambda_function.py` | AWS Lambda console; CloudWatch logs |

---

## L1 — Credential loading

**Trigger:** Python import at app startup — fires before any UI is rendered.

**Steps:**
1. `load_runtime_env()` checks whether `python-dotenv` is installed.
2. Reads `.env` from the `forge/` directory (or path in `FORGE_ENV_FILE`).
3. Loads `S3_BUCKET_KEY_ID`, `S3_BUCKET_SECRET_ID`, and `S3_BUCKET_NAME` into process environment.
4. `validate_runtime_env()` raises `ValueError` if any required key is missing.

| Symptom | Cause | Recovery |
|---|---|---|
| Import error at startup | `.env` missing, unreadable, or missing required keys | Restore `.env`; verify all three keys are present |
| 403 on every S3 upload | Credentials present but expired or wrong | Re-issue IAM credentials; update `.env` |

---

## L4 — Per-image detection pipeline

**Trigger:** User uploads images and clicks Run in the Streamlit UI.

**Steps:**
1. Image loaded via PIL; EXIF orientation corrected via `ImageOps.exif_transpose`.
2. YOLO `model.predict()` runs at the configured confidence threshold; boxes filtered to the four supported classes.
3. `resolve_nearest_weld()` assigns each detection to the nearest weld coordinate from the weld map.
4. Reasoning overlay drawn: weld points (blue), defect boxes (green/red), distance lines.
5. Per-detection events constructed: 1 detection × 1 camera = 1 event.
6. `publish_event()` called per event → S3 JSON + MD upload.
7. Run-level JSON and MD artifacts written locally.

| Symptom | Cause | Recovery |
|---|---|---|
| No detections returned | Low image quality, wrong model, or confidence threshold too high | Lower threshold; verify model path |
| Weld correlation shows all red | Weld map missing or wrong part type / step selected | Check `weld_match.json`; select correct part type and step in sidebar |
| S3 upload fails | Missing or expired credentials | See L1; local artifacts still written |

---

## P0 — Initial stack setup (manual, one-time)

**Steps:**
1. Provision Ubuntu host; install Docker and Docker Compose.
2. Clone project source to the host.
3. Create `trigger-detection/.env` with all required values (see CONFIG.md).
4. `docker compose up -d`
5. Pull Ollama models:
   ```
   docker compose exec ollama ollama pull nomic-embed-text
   docker compose exec ollama ollama pull qwen2.5:7b
   ```
6. Verify: `docker compose ps` — all five services show `Up`.

> **Critical:** If Ollama models are not pulled before other services start, `embed-worker` and `dashboard` will crash-loop.

---

## P1 — Detection ingest pipeline (automated)

**Trigger:** S3 `PutObject` event on `forge-project-data` bucket, fires per uploaded `.json` or `.md` file.

**Steps:**
1. Lambda fires; extracts UUID and file type from the S3 key.
2. Lambda derives the sibling key by swapping prefix and extension (`defect-json/` ↔ `defect-md/`, `.json` ↔ `.md`).
3. Lambda fetches both files from S3; assembles `{ correlation_id, markdown, metadata }`.
4. Lambda publishes message to SQS queue.
5. Embed-worker receives message on next poll cycle; sends Markdown to `nomic-embed-text`.
6. Embed-worker flattens metadata; upserts Qdrant point; writes S3 cold-storage copy.
7. Embed-worker deletes SQS message on success.

| Symptom | Cause | Recovery |
|---|---|---|
| Messages accumulate in SQS | Embed-worker down or Qdrant / Ollama unavailable | `docker compose restart embed-worker`; check downstream services |
| Lambda throws `FileNotFoundError` | Sibling file not yet uploaded when Lambda fired | Self-resolving — Lambda retries on the sibling's arrival event |
| Points appear in Qdrant but cold-storage write fails | S3 credentials or bucket misconfigured | Non-fatal; Qdrant write succeeded; fix S3 config for future events |

---

## P4 — Dashboard rebuild after code change (manual)

**Steps:**
1. Edit source file(s) on the host.
2. `docker compose up -d --build dashboard`
3. Verify dashboard loads at `https://forge-alb.intern-app-sbx-001.sctmtrs.xyz/`

> The dashboard image uses `COPY`, not bind mounts. Changes to `app.py`, `intent_parser.py`, `styles.css`, or `Truth.json` have no effect until a rebuild.

---

## P8 — EC2 instance start (manual)

**Trigger:** On demand — the EventBridge Scheduler shuts the instance down daily at 5 PM. Start it the next morning or whenever needed.

**Steps:**
1. AWS console → EC2 → Instances → select the instance → Instance state → Start.
   Or via CLI: `aws ec2 start-instances --instance-ids <instance-id> --region us-east-2`
2. Wait for instance state to reach `running` (~1–2 min).
3. SSH in and verify the stack is up: `docker compose ps` — all five services should be `Up`.
4. If any service is not running: `docker compose up -d`

> Docker Compose is configured with `restart: unless-stopped` / `restart: always` on all services, so containers resume automatically after an instance start without a manual `docker compose up`.

---

## P9 — Lambda deployment / update (manual)

**Trigger:** On any change to `embed-worker/lambda_function.py`.

**Steps:**
1. Zip the updated function: `zip lambda.zip lambda_function.py`
2. Upload via AWS console → Lambda → `forge-detection-events-lambda` → Upload from → .zip file.
   Or via CLI: `aws lambda update-function-code --function-name forge-detection-events-lambda --zip-file fileb://lambda.zip --region us-east-2`
3. Verify in CloudWatch Logs (`/aws/lambda/forge-detection-events-lambda`) that the next S3 trigger executes without error.

| Symptom | Cause | Recovery |
|---|---|---|
| Lambda throws `Invalid filename format` | S3 key pattern changed in vision app | Update regex in `lambda_function.py`; redeploy |
| Lambda throws `FileNotFoundError` on sibling | One file arrived; other not yet in S3 | Self-resolving on sibling's arrival event |
| Lambda not triggering at all | S3 event notification missing or misconfigured | Verify S3 bucket → Properties → Event notifications points to `forge-detection-events-lambda` |
