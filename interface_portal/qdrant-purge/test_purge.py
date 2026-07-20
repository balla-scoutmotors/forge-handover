"""
End-to-end test for the active purge system.

Proves two things against the REAL Qdrant collection, using only synthetic
points tagged with `_purge_test=true` (so real detection data is never touched):

  1. A point older than the retention window IS deleted by the purge.
  2. A point inside the retention window is KEPT.

It imports and runs the same `purge_once()` the daily service uses, so a pass
means the live mechanism works. The test always cleans up its own points.

Run it on demand:

    docker compose run --rm qdrant-purge python test_purge.py
"""

from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import uuid
import requests

import purge  # reuse the live purge logic + config

QDRANT_BASE = purge.QDRANT_BASE
COLLECTION = purge.COLLECTION
HEADERS = purge.HEADERS
RETENTION_HOURS = purge.RETENTION_HOURS

COLLECTION_URL = f"{QDRANT_BASE}/collections/{COLLECTION}"
UPSERT_URL = f"{COLLECTION_URL}/points"
COUNT_URL = f"{COLLECTION_URL}/points/count"
DELETE_URL = f"{COLLECTION_URL}/points/delete"

TEST_TAG = "_purge_test"


def get_vector_size() -> int:
    resp = requests.get(COLLECTION_URL, headers=HEADERS)
    resp.raise_for_status()
    vectors = resp.json()["result"]["config"]["params"]["vectors"]
    # Single unnamed vector: {"size": N, ...}. Named vectors: {"name": {"size": N}}.
    if "size" in vectors:
        return int(vectors["size"])
    first = next(iter(vectors.values()))
    return int(first["size"])


def upsert_point(point_id: int, ingested_at: int, dim: int) -> None:
    body = {
        "points": [
            {
                "id": point_id,
                "vector": [0.0] * dim,
                "payload": {TEST_TAG: True, "ingested_at": ingested_at},
            }
        ]
    }
    resp = requests.put(UPSERT_URL, headers=HEADERS, params={"wait": "true"}, json=body)
    resp.raise_for_status()


def point_exists(point_id: int) -> bool:
    body = {
        "exact": True,
        "filter": {"must": [{"has_id": [point_id]}]},
    }
    resp = requests.post(COUNT_URL, headers=HEADERS, json=body)
    resp.raise_for_status()
    return resp.json()["result"]["count"] > 0


def delete_test_points() -> None:
    """Remove every point this test could have created, by tag."""
    body = {"filter": {"must": [{"key": TEST_TAG, "match": {"value": True}}]}}
    resp = requests.post(DELETE_URL, headers=HEADERS, params={"wait": "true"}, json=body)
    resp.raise_for_status()


def main() -> int:
    now = int(time.time())
    # Unique ids well outside the worker's microsecond-epoch id space to avoid collisions.
    suffix = uuid.uuid4().int >> 96  # small-ish unique int
    old_id = 9_000_000_000_000_000 + suffix
    fresh_id = 9_100_000_000_000_000 + suffix

    old_ingested = now - int((RETENTION_HOURS + 1) * 3600)  # safely past retention
    fresh_ingested = now  # well within retention

    print(
        f"[test] collection={COLLECTION} retention={RETENTION_HOURS}h\n"
        f"[test] old_point id={old_id} ingested_at={old_ingested} (should be DELETED)\n"
        f"[test] fresh_point id={fresh_id} ingested_at={fresh_ingested} (should be KEPT)"
    )

    passed = False
    try:
        dim = get_vector_size()
        print(f"[test] vector dim={dim}")

        upsert_point(old_id, old_ingested, dim)
        upsert_point(fresh_id, fresh_ingested, dim)

        assert point_exists(old_id), "setup failed: old point not inserted"
        assert point_exists(fresh_id), "setup failed: fresh point not inserted"
        print("[test] both synthetic points inserted")

        # Run the exact logic the daily service runs.
        purge.purge_once()

        old_gone = not point_exists(old_id)
        fresh_kept = point_exists(fresh_id)

        print(f"[test] old_point deleted? {old_gone}")
        print(f"[test] fresh_point kept?  {fresh_kept}")

        passed = old_gone and fresh_kept
    finally:
        # Always clean up, even on failure/exception.
        try:
            delete_test_points()
            print("[test] cleaned up synthetic test points")
        except Exception as e:
            print(f"[test] WARNING cleanup failed: {e}")

    print("[test] RESULT:", "PASS ✅" if passed else "FAIL ❌")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
