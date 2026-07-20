"""
ONE-TIME backfill: stamp `ingested_at` onto pre-existing points.

Points written before the worker change have no `ingested_at`, so the
range-filter purge would never match them. Their Qdrant `id` is a
microsecond epoch (id = int(time.time() * 1_000_000)), so we can derive
ingested_at = id // 1_000_000 and write it back.

Run this exactly once after deploying the worker change:

    docker compose run --rm qdrant-purge python backfill.py

After it reports "backfill complete", this file and its compose usage can
be safely deleted -- the worker stamps `ingested_at` on all new points.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import requests

QDRANT_BASE = os.getenv("QDRANT_BASE_URL", "http://qdrant:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "detection_events")
SCROLL_URL = f"{QDRANT_BASE}/collections/{COLLECTION}/points/scroll"
SET_PAYLOAD_URL = f"{QDRANT_BASE}/collections/{COLLECTION}/points/payload"

BATCH = int(os.getenv("BACKFILL_BATCH", "256"))

HEADERS = {
    "Content-Type": "application/json",
    "api-key": os.getenv("QDRANT_API_KEY"),
}


def derive_ingested_at(point_id) -> int | None:
    """id is microsecond epoch -> seconds. Returns None for non-numeric ids."""
    try:
        return int(point_id) // 1_000_000
    except (TypeError, ValueError):
        return None


def set_ingested_at(point_id, ingested_at: int) -> None:
    resp = requests.post(
        SET_PAYLOAD_URL,
        headers=HEADERS,
        params={"wait": "true"},
        json={"payload": {"ingested_at": ingested_at}, "points": [point_id]},
    )
    resp.raise_for_status()


def main() -> None:
    offset = None
    scanned = stamped = skipped = 0

    print(f"[backfill] collection={COLLECTION} base={QDRANT_BASE} batch={BATCH}")

    while True:
        body = {"limit": BATCH, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset

        resp = requests.post(SCROLL_URL, headers=HEADERS, json=body)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        points = result.get("points", [])

        for p in points:
            scanned += 1
            payload = p.get("payload") or {}
            if "ingested_at" in payload:
                skipped += 1
                continue
            ts = derive_ingested_at(p.get("id"))
            if ts is None:
                skipped += 1
                continue
            set_ingested_at(p["id"], ts)
            stamped += 1

        offset = result.get("next_page_offset")
        if offset is None:
            break

    print(
        f"[backfill] complete | scanned={scanned} stamped={stamped} "
        f"skipped(already-set/non-numeric)={skipped}"
    )


if __name__ == "__main__":
    main()
