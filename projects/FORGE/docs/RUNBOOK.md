# RUNBOOK.md — Operations, diagnostics, recovery

**On any failure, start here.**

---

## Incident diagnosis (in order)

1. Check the affected process's run / audit history — see PROCESSES.md for where each process logs.
2. Verify connections and access are valid (`docker compose ps`; AWS console for Lambda / SQS).
3. Verify location pointers (S3 bucket name, SQS URL, Qdrant host) match CONFIG.md.
4. Re-authenticate affected sources — refresh `.env` credentials; `docker compose restart <service>`.
5. Confirm permissions / licensing — IAM policy on AWS key; `QDRANT_API_KEY`; GPU driver for Ollama.

---

## Common operations

### SSH into the EC2 host
```
ssh -i <path-to-pem-key>.pem ubuntu@forge-server.intern-app-sbx-001.sctmtrs.xyz
```
> Must be on a Scout-managed device with Zscaler active. Zscaler enforces the `.sctmtrs.xyz` domain restriction — SSH will not reach the instance from a non-Scout device or without Zscaler.
> PEM key location and holder: see STAKEHOLDERS.md.

### Access the dashboard
Open in browser (Scout device + Zscaler required):
```
https://forge-alb.intern-app-sbx-001.sctmtrs.xyz/
```
> Traffic routes: browser → Route 53 CNAME → internal ALB (HTTPS:443) → target group → EC2 port 8501.

### Start the server stack
```
cd trigger-detection/
docker compose up -d
docker compose ps          # verify all five services are Up
```

### Stop the server stack
```
docker compose down
```

### Pull Ollama models (required on first run and after model loss)
```
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec ollama ollama pull qwen2.5:7b
docker compose exec ollama ollama list    # confirm both are present
```
> If skipped, `embed-worker` and `dashboard` crash-loop immediately.

### Rebuild dashboard after code changes
```
docker compose up -d --build dashboard
```
> Required after any edit to `app.py`, `intent_parser.py`, `styles.css`, or `Truth.json`. The image uses `COPY` — changes on disk have no effect until a rebuild.

### View live logs
```
docker compose logs -f embed-worker
docker compose logs -f dashboard
docker compose logs -f qdrant-purge
docker compose logs -f ollama
```

---

## Failure scenarios

### Dashboard shows no detections / detection feed is empty
1. `docker compose logs embed-worker` — look for Qdrant write errors or Ollama timeouts.
2. Check SQS queue depth in AWS console — messages may be accumulating.
3. Verify `QDRANT_API_KEY` in `.env` is correct.
4. `docker compose restart qdrant embed-worker`

### S3 uploads failing from local vision app
1. Check Streamlit UI error panel or app stdout for 403 / credential errors.
2. Verify `S3_BUCKET_KEY_ID`, `S3_BUCKET_SECRET_ID`, `S3_BUCKET_NAME` in `forge/.env`.
3. Check IAM policy — key needs `s3:PutObject` on `forge-project-data/*`.
4. Local run artifacts (timestamped JSON + MD in Downloads) are still written regardless of S3 status.

### Embed-worker not processing messages
1. `docker compose logs embed-worker` — identify whether the failure is Ollama (embed timeout) or Qdrant (write error).
2. Verify Ollama models are loaded: `docker compose exec ollama ollama list`
3. `docker compose restart embed-worker`
4. Messages remain safely in SQS until successfully processed; no data loss.

### Qdrant unavailable / returns errors
1. `docker compose restart qdrant`
2. Data is persisted on Docker volume `qdrant-data` — restart is non-destructive.
3. If volume is corrupted: cold-storage copies exist in S3 under `detections/defect-points/`; run `backfill.py` to rehydrate (see P7 in PROCESSES.md).

### Ollama models missing after server restart
```
docker compose exec ollama ollama list
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec ollama ollama pull qwen2.5:7b
```
> Model weights are stored in the Ollama container's volume at `/home/ubuntu/.ollama` — they survive `docker compose down` / `up` but not a volume wipe.

### Lambda not triggering on S3 uploads
1. Verify S3 event notification is configured on `forge-project-data` to invoke the Lambda on `s3:ObjectCreated:*` for prefix `detections/`.
2. Check Lambda logs in AWS CloudWatch (`/aws/lambda/forge-detection-events-lambda`).
3. Confirm Lambda execution role has `s3:GetObject` on `forge-project-data` and `sqs:SendMessage` on `forge-detection-events-queue`.
4. Redeploy Lambda from source: `embed-worker/lambda_function.py`.

### Dashboard query returns wrong or stale results
1. Verify `Truth.json` inside the running image is current: `docker compose exec dashboard cat /app/Truth.json | head -20`
2. If stale — rebuild: `docker compose up -d --build dashboard`
3. Qdrant payload indexes (`defect_name`, `ingested_at`) are auto-created on startup — no manual step needed.

---

## Post-release reconciliation checklist

Run after every deployment:
- [ ] Verify `.env` values on both vision host and server host match CONFIG.md
- [ ] `docker compose ps` — all five services `Up`
- [ ] Pull Ollama models if server was reprovisioned
- [ ] Rebuild dashboard image if any frontend file changed
- [ ] Confirm S3 event notification still points to the correct Lambda
- [ ] Submit one test image through the local vision app; verify the detection appears in the dashboard within ~60 seconds
