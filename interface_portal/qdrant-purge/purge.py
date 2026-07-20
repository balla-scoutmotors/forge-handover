"""
Daily Qdrant purge for the `detection_events` collection.

Runs as a long-lived container: purge once on startup, then every
PURGE_INTERVAL_HOURS thereafter. Deletes any point whose `ingested_at`
(epoch seconds, written by the embed worker) is older than the retention
window via a server-side range filter -- no scrolling, no client-side work.
"""

from dotenv import load_dotenv
load_dotenv()

import os
import sys
import time
import requests

QDRANT_BASE = os.getenv("QDRANT_BASE_URL", "http://qdrant:6333")
COLLECTION = os.getenv("QDRANT_COLLECTION", "detection_events")
DELETE_URL = f"{QDRANT_BASE}/collections/{COLLECTION}/points/delete"
COUNT_URL = f"{QDRANT_BASE}/collections/{COLLECTION}/points/count"

RETENTION_HOURS = float(os.getenv("PURGE_RETENTION_HOURS", "720"))  # 30 days
INTERVAL_HOURS = float(os.getenv("PURGE_INTERVAL_HOURS", "24"))

HEADERS = {
    "Content-Type": "application/json",
    "api-key": os.getenv("QDRANT_API_KEY"),
}


def _old_points_filter(cutoff_epoch: int) -> dict:
    """Match every point ingested strictly before the cutoff."""
    return {
        "must": [
            {"key": "ingested_at", "range": {"lt": cutoff_epoch}}
        ]
    }


def count_matching(filter_body: dict) -> int:
    resp = requests.post(COUNT_URL, headers=HEADERS, json={"filter": filter_body, "exact": True})
    resp.raise_for_status()
    return resp.json().get("result", {}).get("count", 0)


def purge_once() -> None:
    cutoff = int(time.time() - RETENTION_HOURS * 3600)
    flt = _old_points_filter(cutoff)

    try:
        doomed = count_matching(flt)
    except Exception as e:
        # Counting is best-effort logging only; never let it block the delete.
        print(f"[purge] count failed (continuing): {e}")
        doomed = -1

    print(
        f"[purge] retention={RETENTION_HOURS}h cutoff_epoch={cutoff} "
        f"points_to_delete={'unknown' if doomed < 0 else doomed}"
    )

    if doomed == 0:
        print("[purge] nothing to delete")
        return

    resp = requests.post(
        DELETE_URL,
        headers=HEADERS,
        params={"wait": "true"},
        json={"filter": flt},
    )
    if not resp.ok:
        print(f"[purge] delete failed: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    print(f"[purge] delete acknowledged: {resp.json().get('result', {})}")


def _wait_for_qdrant(timeout_s: int = 120, interval_s: int = 5) -> None:
    """Block until Qdrant is reachable or timeout expires.
    Prevents the startup race where purge fires before Qdrant is ready.
    """
    health_url = f"{QDRANT_BASE}/healthz"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(health_url, timeout=3)
            if r.ok:
                print("[purge] Qdrant is ready")
                return
        except Exception:
            pass
        print(f"[purge] waiting for Qdrant at {health_url} ...")
        time.sleep(interval_s)
    raise RuntimeError(f"[purge] Qdrant not reachable after {timeout_s}s")


def main() -> None:
    # One-shot mode: `python purge.py once` runs a single purge pass and exits.
    # Useful for an on-demand purge or from automated tests/cron.
    run_once = len(sys.argv) > 1 and sys.argv[1].lower() == "once"

    print(
        f"[purge] starting | collection={COLLECTION} base={QDRANT_BASE} "
        f"retention={RETENTION_HOURS}h interval={INTERVAL_HOURS}h "
        f"mode={'once' if run_once else 'loop'}"
    )

    # Wait for Qdrant to be ready before first run so EC2-restart
    # race conditions don't cause the startup purge to silently fail
    # and then sleep the full interval before retrying.
    _wait_for_qdrant()

    if run_once:
        purge_once()
        return

    while True:
        try:
            purge_once()
        except Exception as e:
            # On failure retry after a short interval rather than the full
            # INTERVAL_HOURS, so a transient Qdrant blip self-heals quickly.
            print(f"[purge] run errored, retrying in 10 min: {e}")
            time.sleep(600)
            continue
        time.sleep(INTERVAL_HOURS * 3600)
        time.sleep(INTERVAL_HOURS * 3600)


if __name__ == "__main__":
    main()
