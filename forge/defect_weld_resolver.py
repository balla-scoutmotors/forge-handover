"""
defect_weld_resolver.py
=======================
Reasoning layer for the star-circle-line DEFECT model.

Unlike weld_resolver.py (which confirms a *detected weld* against a map),
this module anchors on the DEFECT:

    1. YOLO detects defects (e.g. circle / star / line).
    2. Welds are NOT detected — their positions come from a JSON map.
    3. Each defect is correlated to its NEAREST weld coordinate.

Rules (per product decision):
    * No distance cutoff — every defect always takes its nearest weld.
    * Many-to-one allowed — several defects may share the same nearest weld.
    * No coordinate transform — defect detections and weld coordinates are
      assumed to be in the same image space (identity mapping).

The weld map is read from the local weld_match.json (one entry per
(part_type, step)). S3 is used as an optional source when available.
"""

import json
import math
import os
from functools import lru_cache
from typing import Optional

# ── Configuration ────────────────────────────────────────────────────────────

S3_BUCKET = "forge-project-data"
WELD_MAP_PREFIX = "weld-maps"
LOCAL_CACHE_DIR = "/tmp/forge_weld_maps"   # Jetson local cache

# Project-local weld map used for offline / development matching.
LOCAL_WELD_MATCH_PATH = os.path.join(os.path.dirname(__file__), "weld_match.json")


# ── Weld map loading ──────────────────────────────────────────────────────────

def _local_cache_path(part_type: str, step: int) -> str:
    return os.path.join(LOCAL_CACHE_DIR, part_type, f"step_{step:02d}.json")


def _load_from_s3(part_type: str, step: int) -> Optional[dict]:
    """Pull weld map from S3 and cache locally. Returns None if unavailable."""
    try:
        import boto3  # imported lazily so the module loads without boto3
    except ImportError:
        return None
    try:
        os.makedirs(os.path.join(LOCAL_CACHE_DIR, part_type), exist_ok=True)
        s3 = boto3.client("s3")
        key = f"{WELD_MAP_PREFIX}/{part_type}/step_{step:02d}.json"
        local = _local_cache_path(part_type, step)
        s3.download_file(S3_BUCKET, key, local)
        with open(local) as f:
            return json.load(f)
    except Exception:
        return None

def _read_local_weld_match() -> list[dict]:
    """Read the project-local weld_match.json (list of per-step entries)."""
    if not os.path.exists(LOCAL_WELD_MATCH_PATH):
        return []
    with open(LOCAL_WELD_MATCH_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]

def _load_from_local_weld_match(part_type: str, step: int) -> Optional[list[dict]]:
    """Return welds for (part_type, step) from the project-local weld_match.json."""
    for entry in _read_local_weld_match():
        if entry.get("part_type") == part_type and entry.get("step") == step:
            return entry.get("welds", [])
    return None

def _filter_valid_welds(welds: list[dict]) -> list[dict]:
    """Drop welds without resolved x/y coordinates (e.g. pending captures)."""
    return [
        w for w in welds
        if w.get("x") is not None and w.get("y") is not None
    ]

def list_local_weld_map_steps() -> list[tuple[str, int]]:
    """Return available (part_type, step) pairs from the local weld_match.json."""
    pairs = []
    for entry in _read_local_weld_match():
        part_type = entry.get("part_type")
        step = entry.get("step")
        if part_type is not None and step is not None:
            pairs.append((part_type, step))
    return pairs

@lru_cache(maxsize=32)
def load_weld_map(part_type: str, step: int) -> list[dict]:
    """
    Load weld coordinates for (part_type, step).
    Priority: local cache → S3 → project-local weld_match.json.
    Welds without coordinates are filtered out. Cached for the session.
    """
    local = _local_cache_path(part_type, step)

    # 1. Local cache hit (Jetson has it from a previous sync)
    if os.path.exists(local):
        with open(local) as f:
            return _filter_valid_welds(json.load(f).get("welds", []))

    # 2. S3 (optional)
    data = _load_from_s3(part_type, step)
    if data is not None:
        return _filter_valid_welds(data.get("welds", []))

    # 3. Project-local weld_match.json (development / offline use)
    local_welds = _load_from_local_weld_match(part_type, step)
    if local_welds is not None:
        return _filter_valid_welds(local_welds)

    return []

def get_step_weld_points(part_type: str, step: int) -> list[dict]:
    """
    Return every weld for (part_type, step) in image space.

    No transform is applied — weld coordinates are used as-is.
    Each item: {"weld_id", "label", "x", "y"}.
    """
    return [
        {
            "weld_id": w["weld_id"],
            "label": w.get("label", ""),
            "x": float(w["x"]),
            "y": float(w["y"]),
        }
        for w in load_weld_map(part_type, step)
    ]

# ── Bounding box helper ───────────────────────────────────────────────────────

def parse_bbox(box) -> tuple[float, float, float, float]:
    """
    Convert an Ultralytics detection box into (x, y, w, h) in pixels,
    where (x, y) is the TOP-LEFT corner. Expects a box exposing `.xyxy`.
    """
    xyxy = box.xyxy[0]
    x1, y1, x2, y2 = (float(v) for v in (xyxy.tolist() if hasattr(xyxy, "tolist") else xyxy))
    return x1, y1, x2 - x1, y2 - y1

# ── Core: nearest weld per defect ─────────────────────────────────────────────

def nearest_weld(
    cx: float,
    cy: float,
    welds: list[dict],
) -> tuple[Optional[dict], Optional[float]]:
    """Return (weld, distance_px) for the weld nearest to point (cx, cy)."""
    if not welds:
        return None, None

    def dist(w: dict) -> float:
        return math.sqrt((float(w["x"]) - cx) ** 2 + (float(w["y"]) - cy) ** 2)

    best = min(welds, key=dist)
    return best, dist(best)

def resolve_nearest_weld(
    detections: list[dict],
    part_type: str,
    step: int,
) -> list[dict]:
    """
    Correlate each defect detection to its NEAREST weld coordinate.

    No threshold and no exclusivity: every defect is labelled with the
    closest weld; multiple defects may map to the same weld.

    Args:
        detections : list of dicts, each with "bounding_box": {x, y, w, h}.
        part_type  : e.g. "test_surface"
        step       : capture step number

    Returns:
        The same list, each detection annotated in place with:
            weld_id, weld_stud_location, weld_match_distance_px, weld_matched,
            center_x, center_y, weld_cap_x, weld_cap_y
    """
    welds = load_weld_map(part_type, step)

    for det in detections:
        bb = det.get("bounding_box", {})
        cx = bb.get("x", 0.0) + bb.get("w", 0.0) / 2.0
        cy = bb.get("y", 0.0) + bb.get("h", 0.0) / 2.0
        det["center_x"] = cx
        det["center_y"] = cy

        weld, d = nearest_weld(cx, cy, welds)
        if weld is None:
            det["weld_id"] = "unresolved"
            det["weld_stud_location"] = ""
            det["weld_match_distance_px"] = None
            det["weld_matched"] = False
            det["weld_cap_x"] = None
            det["weld_cap_y"] = None
        else:
            det["weld_id"] = weld["weld_id"]
            det["weld_stud_location"] = weld.get("label", "")
            det["weld_match_distance_px"] = round(d, 1)
            det["weld_matched"] = True
            det["weld_cap_x"] = float(weld["x"])
            det["weld_cap_y"] = float(weld["y"])

    return detections

# ── CLI: quick self-test ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Available (part_type, step):", list_local_weld_map_steps())
    demo = [
        {"bounding_box": {"x": 380, "y": 610, "w": 20, "h": 20}},
        {"bounding_box": {"x": 1300, "y": 550, "w": 20, "h": 20}},
        {"bounding_box": {"x": 2300, "y": 600, "w": 20, "h": 20}},
    ]
    resolve_nearest_weld(demo, "test_surface", 1)
    for d in demo:
        print(d["weld_id"], d["weld_match_distance_px"], d["weld_stud_location"])
