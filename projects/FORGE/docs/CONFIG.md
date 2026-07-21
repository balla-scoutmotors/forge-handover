# CONFIG.md — Environment, access, and credential register

References and locations only. Secret values are never stored here. Secrets live in a vault or `.env` file; this register holds the key name and purpose, not the value.

---

## Local vision app — `.env` on vision host (`forge/`)

| Key | Purpose | Where used | Required |
|---|---|---|---|
| `S3_BUCKET_KEY_ID` | AWS IAM access key ID | S3 client in `detection_event_publisher.py` | Yes |
| `S3_BUCKET_SECRET_ID` | AWS IAM secret access key | S3 client in `detection_event_publisher.py` | Yes |
| `S3_BUCKET_NAME` | Target S3 bucket name (default: `forge-project-data`) | S3 upload paths | Yes |
| `FORGE_ENV_FILE` | Override path to `.env` file | `detection_event_publisher.py` startup | No |
| `FORGE_S3_PREFIX` | S3 key prefix for uploads (default: `detections`) | Upload path construction | No |
| `FORGE_S3_UPLOAD_ENABLED` | Enable / disable S3 upload (`true` / `false`, default: `true`) | Dashboard S3 toggle default | No |

---

## Server stack — `trigger-detection/.env`

| Key | Purpose | Where used | Required |
|---|---|---|---|
| `QDRANT_API_KEY` | Qdrant REST API authentication | All containers that call Qdrant (`embed-worker`, `dashboard`, `qdrant-purge`) | Yes |
| `S3_BUCKET` | S3 bucket name for cold-storage writes | Embed worker | Yes |
| `QDRANT_BASE_URL` | Qdrant host override (default: `http://qdrant:6333`) | Qdrant-purge | No |
| `QDRANT_COLLECTION` | Collection name override (default: `detection_events`) | Qdrant-purge | No |
| `PURGE_RETENTION_HOURS` | Point retention window in hours (default: `720` = 30 days) | Qdrant-purge | No |
| `PURGE_INTERVAL_HOURS` | Purge run frequency in hours (default: `24`) | Qdrant-purge | No |

---

## Docker Compose — ports and internal endpoints

| Service | External binding | Internal network address | Notes |
|---|---|---|---|
| Dashboard | `0.0.0.0:8501` | `http://dashboard:8501` | Accessible from outside the host |
| Ollama | `127.0.0.1:11434` | `http://ollama:11434` | Localhost only — not externally accessible |
| Qdrant HTTP | `127.0.0.1:6333` | `http://qdrant:6333` | Localhost only |
| Qdrant gRPC | `127.0.0.1:6334` | `qdrant:6334` | Localhost only |

---

## AWS resource identifiers

| Resource | Identifier | Region |
|---|---|---|
| AWS account | `749972935500` | — |
| S3 bucket | `forge-project-data` | us-east-2 |
| SQS queue | `forge-detection-events-queue` | us-east-2 |
| Lambda function | `forge-detection-events-lambda` | us-east-2 |
| SQS queue URL | `https://sqs.us-east-2.amazonaws.com/749972935500/forge-detection-events-queue` | us-east-2 |

---

## AWS infrastructure — EC2 server host

### Instance

| Property | Value |
|---|---|
| Instance type | `g4dn.xlarge` (NVIDIA T4 GPU — required for Ollama GPU inference) |
| EBS volume | 100 GB |
| VPC | `intern-app-sbx-001` |
| Subnet | Private — `intern-app-sbx-001-private-us-east-2a` (10.0.120.0/23) or `-2b` (10.0.122.0/23); no public IP |
| PEM key | `<key-name>.pem` — held by `<owner>`; required for SSH; see STAKEHOLDERS.md |

---

### Security groups

**EC2 instance security group:**

| Direction | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 22 | `10.0.0.0/8` | SSH from Scout-managed devices via Zscaler |
| Inbound | TCP | 8501 | Target group security group | Dashboard traffic forwarded from ALB |

**Target group / ALB security group:**

| Direction | Protocol | Port | Source | Purpose |
|---|---|---|---|---|
| Inbound | TCP | 80 | `10.0.0.0/8` | HTTP from Scout-managed devices |
| Inbound | TCP | 443 | `10.0.0.0/8` | HTTPS from Scout-managed devices |

> `10.0.0.0/8` is the IP range Scout devices connect through. Zscaler is configured to allow traffic only to `.sctmtrs.xyz` domains — both SSH and HTTPS must go through the Route 53 records below.

---

### Route 53 records (private hosted zone)

| Record name | Type | Points to | Purpose |
|---|---|---|---|
| `forge-server.intern-app-sbx-001.sctmtrs.xyz` | A | EC2 private IP | SSH access — Zscaler requires `.sctmtrs.xyz` domain |
| `forge-alb.intern-app-sbx-001.sctmtrs.xyz` | CNAME | ALB DNS name | Dashboard HTTPS access |

Both records are in a private hosted zone. Accessible only from Scout-managed devices (Zscaler enforces `.sctmtrs.xyz` restriction).

---

### Application Load Balancer

| Property | Value |
|---|---|
| Type | Internal (not internet-facing) |
| Listener | HTTPS : 443 |
| Target group | EC2 instance, port 8501 |
| Subnets | `intern-app-sbx-001-private-us-east-2a` (10.0.120.0/23), `intern-app-sbx-001-private-us-east-2b` (10.0.122.0/23) |
| Dashboard URL | `https://forge-alb.intern-app-sbx-001.sctmtrs.xyz/` |

---

### VPC networking

| Component | Name | Notes |
|---|---|---|
| VPC | `intern-app-sbx-001` | us-east-2; contains all project resources |
| Private route table | `intern-app-sbx-001-private` | 3 subnet associations, 9 routes; used by EC2 and ALB subnets |
| Default route table | `intern-app-sbx-001-default` | 3 subnet associations, 3 routes |
| NAT gateway | `intern-app-sbx-001-us-east-2a` | Public NAT, 1 ENI + 1 EIP in us-east-2a; provides outbound internet for private subnets (Docker pulls, Ollama model downloads, pip installs) |
| S3 VPC endpoint | `intern-app-sbx-001-s3-endpoint` | Gateway endpoint — S3 traffic from private subnets routes through the VPC, not over the NAT gateway |
| DynamoDB VPC endpoint | `intern-app-sbx-001-dynamodb-endpoint` | Gateway endpoint — not used by this project but present in the shared VPC |

---

### IAM identities

| Identity | Type | Permissions | Used by |
|---|---|---|---|
| S3 upload user | IAM user (access key) | `s3:PutObject` on `forge-project-data` | Local vision app — credentials in `forge/.env` as `S3_BUCKET_KEY_ID` / `S3_BUCKET_SECRET_ID` |
| `lambda-trigger-role` | IAM role | S3 read-only, `AWSLambdaBasicExecutionRole`, custom SQS send policy | AWS Lambda — reads S3 artifacts, publishes to SQS |
| `forge-instance-s3` | IAM role | S3 full access, SQS full access | EC2 instance — used by embed-worker to receive SQS messages and write S3 cold storage |
| `event-bridge-scheduler` | IAM role | EC2 full access | EventBridge Scheduler — shuts down EC2 instance daily at 5 PM |
