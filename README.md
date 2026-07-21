# FORGE Detection Portal

**Purpose:** Detects manufacturing weld defects from camera images using a local YOLO vision app, transports detection events to a cloud-connected server stack, and exposes a conversational AI interface for engineers to query and analyze detections in natural language.

**Status:** In development  

**Project type:** Software

---

## Index

| Document | Location |
|---|---|
| Architecture & data flow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Components & workstreams | [docs/COMPONENTS.md](docs/COMPONENTS.md) |
| Processes & pipelines | [docs/PROCESSES.md](docs/PROCESSES.md) |
| Reference inputs (read-only) | [docs/SOURCES.md](docs/SOURCES.md) |
| Operational data stores | [docs/DATA_AND_SYSTEMS.md](docs/DATA_AND_SYSTEMS.md) |
| External integrations | [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) |
| Environment & credential register | [docs/CONFIG.md](docs/CONFIG.md) |
| Operations, diagnostics, recovery | [docs/RUNBOOK.md](docs/RUNBOOK.md) |
| Decision record | [docs/DECISIONS.md](docs/DECISIONS.md) |
| Owners & contacts | [docs/STAKEHOLDERS.md](docs/STAKEHOLDERS.md) |
| Version history | [docs/CHANGELOG.md](docs/CHANGELOG.md) |

---

## Environments / locations

| Environment | Location |
|---|---|
| Local vision app | Vision-processing host (shop floor) — see CONFIG.md |
| Server stack (EC2) | `g4dn.xlarge`, private subnet — SSH via `forge-server.intern-app-sbx-001.sctmtrs.xyz` |
| S3 bucket | `forge-project-data` (us-east-2) — see CONFIG.md |
| Dashboard UI | `https://forge-alb.intern-app-sbx-001.sctmtrs.xyz/` — Scout device + Zscaler required |

---

## On failure, begin at [docs/RUNBOOK.md](docs/RUNBOOK.md).
