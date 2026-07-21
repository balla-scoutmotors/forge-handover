# INTEGRATIONS.md — External systems, APIs, and dependencies

---

| Integration | Type | Used by | Account / access | Failure → recovery |
|---|---|---|---|---|
| **AWS S3** (`forge-project-data`, us-east-2) | Object storage | Local vision app (write artifacts), Lambda (read both files), Embed worker (write cold-storage copy) | IAM access key — `S3_BUCKET_KEY_ID` / `S3_BUCKET_SECRET_ID` in `.env` | Local artifacts still written if S3 is unavailable; see RUNBOOK |
| **AWS SQS** (`forge-detection-events-queue`, us-east-2) | Message queue | Lambda (publish), Embed worker (consume + delete) | Same IAM credentials as S3; Lambda execution role needs `sqs:SendMessage` | Messages persist in queue; embed-worker retries on next poll; no data loss |
| **AWS Lambda** (`forge-detection-events-lambda`, us-east-2) | Serverless trigger | Bridges S3 → SQS; source in `embed-worker/lambda_function.py` | Lambda execution role: `s3:GetObject` on bucket, `sqs:SendMessage` on queue | Redeploy from source; check CloudWatch logs for errors |
| **Ollama** (`nomic-embed-text`, `qwen2.5:7b`) | Local LLM runtime | Embed worker (embeddings), Dashboard (embeddings + generation) | Internal Docker network `ai-stack` — `http://ollama:11434`; no external calls | `docker compose restart ollama`; re-pull models if weights lost; see RUNBOOK |
| **Qdrant** | Vector database | Embed worker (write), Dashboard (read/write/count), Qdrant-purge (delete) | Internal Docker network `ai-stack` — `http://qdrant:6333`; `QDRANT_API_KEY` in `.env` | `docker compose restart qdrant`; data on named volume `qdrant-data` survives restart |
| **Ultralytics YOLO** | ML inference library (Python package) | Local vision app | Python package install; model weights as `.pt` file on local filesystem | Restore `.pt` file from source; `pip install ultralytics` if package missing |
| **spaCy** (`en_core_web_sm`) | NLP library (Python package) | Dashboard intent parser | Python package in dashboard Docker image | Rebuilt into image; `pip install spacy && python -m spacy download en_core_web_sm` |
| **EC2** (`g4dn.xlarge`, us-east-2) | Cloud compute host | Runs the Docker Compose server stack (Ollama, Qdrant, embed-worker, dashboard, qdrant-purge) | IAM role `forge-instance-s3` attached to instance; PEM key for SSH — see CONFIG.md | SSH in via `forge-server.intern-app-sbx-001.sctmtrs.xyz`; `docker compose restart <service>`; see RUNBOOK |
| **ALB + Route 53** (internal, us-east-2) | HTTPS ingress + DNS | Routes external HTTPS traffic from Scout devices to the dashboard on port 8501 | Target group security group; private hosted zone; Zscaler `.sctmtrs.xyz` domain requirement | Verify target group health in AWS console; confirm Route 53 CNAME resolves; see RUNBOOK |
| **EventBridge Scheduler** | AWS managed scheduler | Shuts down EC2 instance daily at 5 PM to reduce costs | IAM role `event-bridge-scheduler` (EC2 full access) | Re-enable or adjust schedule in AWS EventBridge console; manually start instance if needed |
