from dotenv import load_dotenv
from html import escape
import json
import os
import re
from datetime import datetime, timezone, timedelta
from intent_parser import extract_intent as _spacy_extract_intent

import requests
import streamlit as st

load_dotenv("../.env")

st.set_page_config(layout="wide")
_hdr_title, _hdr_toggle = st.columns([8, 1])
_hdr_title.title("FORGE Detection Portal")
with _hdr_toggle:
    st.write("")
    st.toggle("📊 Charts", key="show_charts")
st.divider()


def load_css(file_name):
    css_path = os.path.join(os.path.dirname(__file__), file_name)
    with open(css_path) as css_file:
        st.markdown(f"<style>{css_file.read()}</style>", unsafe_allow_html=True)


load_css("styles.css")

OLLAMA_URL = "http://ollama:11434/api/generate"
OLLAMA_EMBED_URL = "http://ollama:11434/api/embeddings"
QDRANT_BASE = "http://qdrant:6333"
COLLECTION = "detection_events"



# Sends POST requests to nomic and qwen (ollama models) to warm up, 
# this process is only done once if there is nothing in cache
# Page runs immediately and starts warmup in background instantly

# Runs once per server startup so model should stay warm assuming session 
# remains the same 
@st.cache_resource(show_spinner=True)
def _warm_models():

    import contextlib
    import threading

    def _do_warm():
        with contextlib.suppress(Exception):
            requests.post(
                OLLAMA_EMBED_URL,
                json={"model": "nomic-embed-text", "prompt": "warmup"},
                timeout=120,
            )
        # Warm qwen2.5:7b with the same num_ctx as real calls so the KV cache
        # is sized correctly and the first user query pays no cold-load penalty.
        with contextlib.suppress(Exception):
            requests.post(
                OLLAMA_URL,
                json={"model": "qwen2.5:7b", "prompt": "warmup",
                      "stream": False, "options": {"num_ctx": 16384}},
                timeout=120,
            )

    threading.Thread(target=_do_warm, daemon=True).start()
    return True


_warm_models()

# Truth.json is static reference data — load it once at import time as a plain constant.
_TRUTH_PATH = os.path.join(os.path.dirname(__file__), "Truth.json")
try:
    with open(_TRUTH_PATH) as _f:
        TRUTH_DB: dict = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    TRUTH_DB = {}


def _norm(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", s)
    return " ".join(s.lower().split())


def qdrant_headers():
    return {
        "Content-Type": "application/json",
        "api-key": os.getenv("QDRANT_API_KEY"),
    }

# creates lookup indexes for defect names and times- WITHIN
# qdrant. Quick place for qdrant to point to if looking for given
# point or time. 

# Useful at scale so qdrant doesnt have to scan entire collection 
# to get defect points + ingestion times
@st.cache_resource(show_spinner=False)
def _ensure_payload_indexes():
    index_url = f"{QDRANT_BASE}/collections/{COLLECTION}/index"
    headers = qdrant_headers()
    for field_name, field_schema in [("defect_name", "text"), ("ingested_at", "integer")]:
        try:
            requests.put(
                index_url,
                headers=headers,
                json={"field_name": field_name, "field_schema": field_schema},
                timeout=10,
            ).raise_for_status()
            print(f"[indexes] payload index ensured: {field_name} ({field_schema})")
        except Exception as e:
            print(f"[indexes] warning — could not create index for {field_name}: {e}")


_ensure_payload_indexes()


# Uses server-side order_by so Qdrant returns exactly limit points newest-first.
# Avoids the old over-fetch+sort approach which broke silently once the
# collection exceeded 200 points (scroll returns oldest IDs first by default).
def get_latest_points(limit=10):
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/scroll"
    body = {
        "limit": limit,
        "with_payload": True,
        "with_vector": False,
        "order_by": {"key": "ingested_at", "direction": "desc"},
    }
    response = requests.post(url, headers=qdrant_headers(), json=body, timeout=10)
    response.raise_for_status()
    return response.json().get("result", {}).get("points", [])


# gets back total number of points in the collection
def get_total_count() -> int:
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/count"
    resp = requests.post(url, headers=qdrant_headers(), json={"exact": True}, timeout=5)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("count", 0)


# helper function to display confidence val
def parse_confidence(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# Used for visual badge creation
def confidence_badge(confidence):
    if confidence is None:
        return "badge-low", "unknown"
    if confidence >= 0.8:
        return "badge-high", f"{confidence:.2f}"
    if confidence >= 0.5:
        return "badge-medium", f"{confidence:.2f}"
    return "badge-low", f"{confidence:.2f}"



# Convert ingested_at epoch seconds to local time string.
def format_ingested_at(epoch) -> str:
    try:
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "unknown"


# Helper method to capture the WHAT (defect name) of what 
# query is prompting. Strip away all text to find out
# WHICH defect to perform count_detections() on
def _extract_count_intent(query_text: str) -> list[str]:
    """Return every defect type the user is asking to count.

    Handles joined questions like 'how many burr and spatter detections' by
    scanning for a count trigger, then matching ALL known defect names from
    Truth.json present in the query. Falls back to a generic single-term regex
    when the mentioned defect isn't in Truth.json.
    """
    q = query_text.lower()

    # Only treat this as a count request if a counting trigger is present.
    if not re.search(r"\bhow many\b|\bcount\b|\bnumber of\b|\btotal\b", q):
        return []

    # Prefer exact matches against known defect names (skip the 'stations' block).
    found: list[str] = []
    for name in TRUTH_DB:
        if name == "stations":
            continue
        # Allow a trailing plural 's' so "burrs"/"spatters" still resolve to the
        # canonical Truth name (and therefore an exact count) instead of falling
        # through to the generic fallback and counting 0.
        if re.search(rf"\b{re.escape(name.lower())}s?\b", q):
            found.append(name.lower())
    if found:
        # De-dupe while preserving order.
        return list(dict.fromkeys(found))

    # Fallback: single generic term after the trigger word.
    patterns = [
        r"how many\s+(\w+)",
        r"count\s+(?:of\s+)?(\w+)",
        r"number\s+of\s+(\w+)",
        r"total\s+(\w+)\s+(?:defects?|detections?|events?)",
    ]
    skip = {"are", "is", "the", "a", "an", "of", "in", "there",
            "total", "detections", "defects", "events", "instances", "items", "results", "any", "all",
            # Adjectives/qualifiers the fallback used to mistake for a defect type
            # ("how many high confidence detections" -> counted 'high' -> 0).
            "high", "low", "medium", "confidence", "recent", "latest", "newest",
            "new", "old", "today", "yesterday", "many", "much", "more", "fewer",
            "less", "other", "same", "different", "first", "last", "current",
            "open", "active", "critical", "unique", "distinct", "were", "was", "do", "we", "have", "had"}
    for pattern in patterns:
        m = re.search(pattern, q)
        if m and m.group(1) not in skip:
            return [m.group(1)]
    return []

# Grabs numerical count for whatever defect name passed: "how many x"a
# extra_must lets a qualified count ("how many burr at station 2", "how many
# spatter today") AND the defect-name match with the same station/date/operator
# constraints the rest of the router uses, so the count is scoped, not global.
def count_detections(defect_name: str, extra_must: list | None = None) -> int:
    # catches numerical count for matches on specified defect name (case lower)
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/count"
    must = [{"key": "defect_name", "match": {"text": defect_name.lower()}}]
    if extra_must:
        must = must + list(extra_must)
    body = {
        "exact": True,
        # text match tokenises and is case-insensitive, so 'spatter' matches 'Spatter'/'SPATTER'
        "filter": {"must": must},
    }
    resp = requests.post(url, headers=qdrant_headers(), json=body, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {}).get("count", 0)


# embed the query put into the inital prompt and return that embedding
def embed_query(text):
    response = requests.post(
        OLLAMA_EMBED_URL,
        json={"model": "nomic-embed-text", "prompt": text},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["embedding"]

# Used only by semantic_search (vector fallback route). Restricts results to a
# 1-minute window around an HH:MM timestamp mentioned in the query, today's date.
def _extract_time_filter(query_text: str) -> dict | None:
    match = re.search(r"\b(\d{1,2}):(\d{2})\b", query_text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    now_local = datetime.now().astimezone()
    try:
        window_start = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    window_end = now_local.replace(hour=hour, minute=minute, second=59, microsecond=999999)
    return {
        "must": [{"key": "ingested_at", "range": {
            "gte": int(window_start.timestamp()),
            "lte": int(window_end.timestamp()),
        }}]
    }

# Fallback route: embeds the query with nomic-embed-text and runs ANN search
# against Qdrant. Applies an HH:MM time filter if the query contains a timestamp.
def semantic_search(query_text, limit=15, score_threshold=0.3):
    vector = embed_query(query_text)
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/search"
    body = {
        "vector": vector,
        "limit": limit,
        "score_threshold": score_threshold,
        "with_payload": True,
        "with_vector": False,
    }
    time_filter = _extract_time_filter(query_text)
    if time_filter:
        body["filter"] = time_filter
    response = requests.post(url, headers=qdrant_headers(), json=body, timeout=15)
    response.raise_for_status()
    return response.json().get("result", [])

# ------------------------------------------------Prompt Generation Helper Functions------------------------------------


# For each unique defect in the retrieved points, look up its Truth.json entry
# and format parameter ranges + corrective actions for injection into the LLM prompt.
def _build_truth_section(context_points: list) -> str:
    if not TRUTH_DB or not context_points:
        return ""

    # Case-insensitive lookup map: "spatter" -> "Spatter"
    truth_index = {k.lower(): k for k in TRUTH_DB}
    station_db = TRUTH_DB.get("stations", {})

    seen_defects: set[str] = set()
    seen_stations: set[str] = set()
    blocks = []

    for point in context_points:
        payload = point.get("payload", {})

        # Defect description block
        name = payload.get("defect_name", "")
        if name and name.lower() not in seen_defects:
            seen_defects.add(name.lower())
            truth_key = truth_index.get(name.lower())
            if truth_key and truth_key != "stations":
                entry = TRUTH_DB[truth_key]
                blocks.append(f"Defect: {truth_key}\nDescription: {entry.get('description', '')}")

        # Station / engineer block
        station = payload.get("station", "")
        if station and station not in seen_stations:
            seen_stations.add(station)
            entry = station_db.get(station)
            if entry:
                eng = entry.get("overseeing_engineer", "")
                sname = entry.get("name", station)
                if eng:
                    blocks.append(f"Station: {sname}\nOverseeing Engineer: {eng}")

    if not blocks:
        return ""

    return (
        "DEFECT KNOWLEDGE BASE:\n"
        + "\n\n".join(blocks)
        + "\n"
    )

# Compare the parameter values for given defects against the truth.json ranges
# in python so LLM doesn't handle any arithmetic. Ouputs IN RANGE or OUT OF RANGE
# Verdicts 
def _build_parameter_assessment(context_points: list) -> str:
    if not TRUTH_DB or not context_points:
        return ""

    truth_index = {k.lower(): k for k in TRUTH_DB}
    blocks = []

    for i, point in enumerate(context_points, 1):
        p = point.get("payload", {})
        defect_name = p.get("defect_name", "")
        if not defect_name:
            continue
        truth_key = truth_index.get(defect_name.lower())
        if not truth_key:
            continue

        entry = TRUTH_DB[truth_key]
        param_ranges = entry.get("parameter_ranges") or entry.get("parameter ranges", {})
        if not param_ranges:
            continue

        corrective = entry.get("corrective_actions") or entry.get("corrective actions", {})
        corrective_norm = {_norm(k): v for k, v in corrective.items()}

        # Build normalized lookup: normalized_field_name -> numeric value
        payload_norm: dict[str, float] = {}
        for k, v in p.items():
            try:
                payload_norm[_norm(k)] = float(v)
            except (TypeError, ValueError):
                pass

        # Range verdicts are decided HERE in Python only. The LLM must copy these
        # two explicit lists verbatim and never move a parameter between them.
        out_lines = []
        in_lines = []
        for truth_param, rng in param_ranges.items():
            norm_key = _norm(truth_param)
            val = payload_norm.get(norm_key)
            if val is None:
                continue

            lo, hi = rng.get("min"), rng.get("max")
            in_range = (lo is None or val >= lo) and (hi is None or val <= hi)
            if in_range:
                in_lines.append(f"    - {truth_param}: {val} \u2014 IN RANGE (normal: {lo}\u2013{hi})")
            else:
                line = f"    - {truth_param}: {val} \u2014 OUT OF RANGE \u26a0 (normal: {lo}\u2013{hi}, actual: {val})"
                action = corrective_norm.get(norm_key)
                if action:
                    line += f"\n      Corrective Action: {action}"
                out_lines.append(line)

        if out_lines or in_lines:
            ing = p.get("ingested_at")
            header = (
                f"Detection {i} ({defect_name}"
                + (f", received {format_ingested_at(ing)}" if ing else "")
                + "):"
            )
            section = [header, "  OUT OF RANGE parameters:"]
            section.extend(out_lines or ["    - (none \u2014 all parameters within normal range)"])
            section.append("  IN RANGE parameters:")
            section.extend(in_lines or ["    - (none)"])
            blocks.append("\n".join(section))

    if not blocks:
        return ""

    return (
        "PRE-COMPUTED PARAMETER ASSESSMENT "
        "(calculated in Python \u2014 use these verdicts and lists EXACTLY; never re-evaluate "
        "a range or move a parameter between the OUT OF RANGE and IN RANGE lists):\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _has_corrective_actions(context_points: list) -> bool:
    """True if at least one detection has an out-of-range parameter.

    Used to decide whether offering "recommended corrective actions" makes sense:
    if nothing is out of range (or there are no detections at all) there is nothing
    to correct, so the invite must be suppressed.
    """
    if not TRUTH_DB or not context_points:
        return False
    truth_index = {k.lower(): k for k in TRUTH_DB}
    for point in context_points:
        p = point.get("payload", {})
        truth_key = truth_index.get(str(p.get("defect_name", "")).lower())
        if not truth_key or truth_key == "stations":
            continue
        entry = TRUTH_DB[truth_key]
        param_ranges = entry.get("parameter_ranges") or entry.get("parameter ranges", {})
        payload_norm = {}
        for k, v in p.items():
            try:
                payload_norm[_norm(k)] = float(v)
            except (TypeError, ValueError):
                pass
        for tp, rng in param_ranges.items():
            val = payload_norm.get(_norm(tp))
            if val is None:
                continue
            lo, hi = rng.get("min"), rng.get("max")
            if not ((lo is None or val >= lo) and (hi is None or val <= hi)):
                return True
    return False


def _build_welding_data_tables(context_points: list) -> str:
    """Pre-format the complete Welding Data table (OOR + IN RANGE) for every detection.

    Used for defect_id lookup and follow-up turns so the model copies a literal
    table instead of deciding which parameters to include — eliminates the 7B's
    tendency to silently drop IN RANGE entries.
    """
    if not TRUTH_DB or not context_points:
        return ""

    truth_index = {k.lower(): k for k in TRUTH_DB}
    blocks = []

    for i, point in enumerate(context_points, 1):
        p = point.get("payload", {})
        defect_name = p.get("defect_name", "")
        if not defect_name:
            continue
        truth_key = truth_index.get(defect_name.lower())
        if not truth_key:
            continue

        entry = TRUTH_DB[truth_key]
        param_ranges = entry.get("parameter_ranges") or entry.get("parameter ranges", {})
        if not param_ranges:
            continue

        payload_norm: dict = {}
        for k, v in p.items():
            try:
                payload_norm[_norm(k)] = float(v)
            except (TypeError, ValueError):
                pass

        lines = []
        for truth_param, rng in param_ranges.items():
            val = payload_norm.get(_norm(truth_param))
            if val is None:
                continue
            lo, hi = rng.get("min"), rng.get("max")
            in_range = (lo is None or val >= lo) and (hi is None or val <= hi)
            if in_range:
                lines.append(f"- **{truth_param}:** {val} \u2014 IN RANGE")
            else:
                lines.append(f"- **{truth_param}:** {val} \u2014 OUT OF RANGE \u26a0 (normal: {lo}\u2013{hi})")

        if lines:
            blocks.append(
                f"Detection {i} ({defect_name}) \u2014 {len(lines)} parameters total:\n"
                + "\n".join(lines)
            )

    if not blocks:
        return ""

    return (
        "\nPRE-COMPUTED WELDING DATA TABLES"
        " (Python-generated \u2014 copy each table VERBATIM into the corresponding"
        " '#### Welding Data' section; include EVERY line, both IN RANGE and OUT OF RANGE):\n"
        + "\n\n".join(blocks)
        + "\n"
    )


# turns detection records into readable text block for LLM. detectinos get headers
# and corresponding fields
def build_chat_context(points):
    sections = []
    for i, point in enumerate(points, 1):
        payload = point.get("payload", {})
        defect = payload.get("defect_name", "unknown defect")
        # Use ingested_at for the header so it matches the "Received" time shown
        # on the dashboard. event_time (upstream camera timestamp) is still
        # included in the body so the LLM can reference it.
        ing = payload.get("ingested_at")
        received_label = f" received {format_ingested_at(ing)}" if ing else ""
        header = f"--- Detection {i}: {defect}{received_label} ---"

        # Always emit explicit key=value lines first so the LLM has unambiguous
        # field values and doesn't conflate similar-looking fields in the markdown.
        structured_parts = []
        for key in ("defect_name", "class_name", "station", "cell_number",
                    "assembly_number", "camera", "confidence", "operator",
                    "part_type", "station_part_name", "part_id", "event_time"):
            val = payload.get(key)
            if val is not None:
                structured_parts.append(f"{key}: {val}")
        structured_block = "\n".join(structured_parts)

        content = payload.get("content")
        if content:
            sections.append(f"{header}\n[STRUCTURED FIELDS]\n{structured_block}\n[MARKDOWN REPORT]\n{content}")
        else:
            causes = {k: v for k, v in payload.items() if k.startswith("causes_")}
            if causes:
                structured_parts.append("causes: " + ", ".join(f"{k[7:]}={v}" for k, v in causes.items()))
            body = "\n".join(structured_parts) if structured_parts else json.dumps(payload)
            sections.append(f"{header}\n{body}")
    return "\n\n".join(sections)

# if prompt contains a pronoun reference: it,them,those etc.
# prepend the PREVIOUS message alongside it for LLM context
def _resolve_pronouns(prompt: str) -> str:
    followup_words = {"them", "it", "those", "these", "they", "its", "their"}
    words = set(re.findall(r"\b\w+\b", prompt.lower()))
    if not (words & followup_words):
        return prompt
    messages = st.session_state.get("messages", [])
    for msg in reversed(messages[:-1]):
        if msg["role"] == "user":
            return f"{msg['content']} {prompt}"
    return prompt

# ---------------------------------------------Fundamental routing center for LLM reasoning ---------------------------------------------


def _extract_query_intent(prompt: str) -> dict:
    """Deterministic spaCy-based intent extraction. No LLM call."""
    try:
        return _spacy_extract_intent(prompt, now=datetime.now().astimezone())
    except Exception as e:
        print(f"[intent] spacy failed ({e}), defaulting to vector")
        return {"mode": "vector"}


def fetch_by_defect_id(defect_id) -> list:
    """Fetch every point whose defect_ID payload field matches the given hash(es).

    Accepts a single hash, a list of hashes, or a comma/space-separated string of
    hashes. Queries that reference two defects at once (e.g. "compare 38a3bba6 and
    8c3a23c6") make the extractor return both IDs joined together; splitting them
    and matching ANY of them returns a record for each defect instead of silently
    matching nothing.
    """
    if isinstance(defect_id, (list, tuple, set)):
        ids = [str(d).strip() for d in defect_id if str(d).strip()]
    else:
        ids = [tok for tok in re.split(r"[,\s]+", str(defect_id)) if tok]
    if not ids:
        return []
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/scroll"
    body = {
        "limit": len(ids),
        "with_payload": True,
        "with_vector": False,
        "filter": {"must": [{"key": "defect_ID", "match": {"any": ids}}]},
    }
    resp = requests.post(url, headers=qdrant_headers(), json=body, timeout=10)
    resp.raise_for_status()
    points = resp.json().get("result", {}).get("points", [])
    return [{"payload": p.get("payload", {}), "id": p.get("id"), "score": 1.0} for p in points]


# Low-cardinality payload fields (station, cell_number, operator) are stored in
# canonical forms ('station_2', 'CAM-2-1', 'ROBOT-002') that don't match what the
# user types ('2', 'station 2'). These helpers resolve user input onto the actual
# stored values so filters and scoped counts match correctly.
def _distinct_payload_values(field: str, cap: int = 2000) -> list[str]:
    """Return the distinct stored string values for a single payload field.

    Uses Qdrant selective payload (with_payload=[field]) so it stays cheap even
    though it scrolls the collection.
    """
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/scroll"
    seen: list[str] = []
    seen_set: set[str] = set()
    offset = None
    fetched = 0
    while fetched < cap:
        body: dict = {"limit": min(250, cap - fetched), "with_payload": [field], "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = requests.post(url, headers=qdrant_headers(), json=body, timeout=15)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        pts = result.get("points", [])
        for p in pts:
            v = p.get("payload", {}).get(field)
            if v is None:
                continue
            s = str(v)
            if s not in seen_set:
                seen_set.add(s)
                seen.append(s)
        fetched += len(pts)
        offset = result.get("next_page_offset")
        if not offset or not pts:
            break
    return seen


def _resolve_filter_value(field: str, value) -> list[str]:
    """Map a user-supplied filter value onto the real stored values for `field`.

    Strategy, in order: exact case-insensitive match -> match on the leading
    integer ('station 2'/'2' -> 'station_2', 'cell 2' -> 'CAM-2-1',
    'robot 2' -> 'ROBOT-002') -> loose substring. Falls back to the raw value so
    an explicitly non-existent filter still yields zero results rather than
    silently widening the query.
    """
    raw = str(value).strip()
    if not raw:
        return []
    try:
        distinct = _distinct_payload_values(field)
    except Exception:
        return [raw]
    if not distinct:
        return [raw]

    low = raw.lower()
    exact = [d for d in distinct if d.lower() == low]
    if exact:
        return exact

    user_nums = re.findall(r"\d+", raw)
    if not user_nums:
        # Word-numbers ("station one" -> station_1).
        for _w, _n in _WORD_TO_NUM.items():
            if re.search(rf"\b{_w}\b", low):
                user_nums = [str(_n)]
                break
    if user_nums:
        n = int(user_nums[0])
        by_first_int = []
        for d in distinct:
            dn = re.findall(r"\d+", d)
            if dn and int(dn[0]) == n:
                by_first_int.append(d)
        if by_first_int:
            return by_first_int

    subs = [d for d in distinct if low in d.lower() or d.lower() in low]
    if subs:
        return subs
    return [raw]


def _match_any_clause(key: str, values: list[str]) -> dict:
    """One match clause for a single resolved value, or a nested should for many."""
    if len(values) == 1:
        return {"key": key, "match": {"value": values[0]}}
    return {"should": [{"key": key, "match": {"value": v}} for v in values]}


def _non_defect_musts(qdrant_filter: dict | None) -> list:
    """Extract the non-defect_name must clauses (station/date/operator/...) so a
    count query can AND them onto the defect-name match."""
    if not qdrant_filter:
        return []
    out = []
    for c in qdrant_filter.get("must", []):
        if c.get("key") == "defect_name":
            continue
        should = c.get("should")
        if should and all(sc.get("key") == "defect_name" for sc in should):
            continue
        out.append(c)
    return out


def _build_qdrant_filter(intent: dict) -> dict | None:
    """Convert extracted intent fields into a Qdrant filter body."""
    must = []
    if intent.get("defect_type"):
        defect_type = intent["defect_type"]
        if isinstance(defect_type, list):
            types = [t.lower() for t in defect_type if isinstance(t, str)]
            if len(types) == 1:
                must.append({"key": "defect_name", "match": {"text": types[0]}})
            elif len(types) > 1:
                # Multiple types → OR condition nested inside must
                must.append({"should": [{"key": "defect_name", "match": {"text": t}} for t in types]})
        else:
            must.append({"key": "defect_name", "match": {"text": str(defect_type).lower()}})
    if intent.get("station"):
        must.append(_match_any_clause("station", _resolve_filter_value("station", intent["station"])))
    if intent.get("cell_number"):
        must.append(_match_any_clause("cell_number", _resolve_filter_value("cell_number", intent["cell_number"])))
    if intent.get("operator"):
        must.append(_match_any_clause("operator", _resolve_filter_value("operator", intent["operator"])))
    # Wrap confidence coercions: a non-numeric value would otherwise raise ValueError.
    try:
        if intent.get("confidence_min") is not None:
            must.append({"key": "confidence", "range": {"gte": float(intent["confidence_min"])}})
    except (TypeError, ValueError):
        pass
    try:
        if intent.get("confidence_max") is not None:
            must.append({"key": "confidence", "range": {"lt": float(intent["confidence_max"])}})
    except (TypeError, ValueError):
        pass

    now = datetime.now().astimezone()
    if intent.get("date_str"):
        try:
            # Interpret the date in the dashboard's LOCAL timezone (what the user
            # means by "today"/"July 16"), NOT UTC. Parsing as UTC midnight then
            # .astimezone() used to shift the day backward in a negative-offset tz
            # (e.g. "today" 2026-07-17 -> a window covering 2026-07-16 local), so
            # "how many today" returned yesterday's count.
            d = datetime.strptime(str(intent["date_str"]), "%Y-%m-%d")
            start = d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=now.tzinfo)
            end = d.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=now.tzinfo)
            must.append({"key": "ingested_at", "range": {
                "gte": int(start.timestamp()), "lte": int(end.timestamp())}})
        except (ValueError, TypeError):
            pass
    elif intent.get("days_back"):
        try:
            start_epoch = int((now - timedelta(days=int(intent["days_back"]))).timestamp())
            must.append({"key": "ingested_at", "range": {"gte": start_epoch}})
        except (TypeError, ValueError):
            pass

    return {"must": must} if must else None


def _scroll_with_filter(qdrant_filter: dict | None, limit: int = 500) -> list:
    """Paginated scroll — fetches up to limit points matching the filter."""
    url = f"{QDRANT_BASE}/collections/{COLLECTION}/points/scroll"
    results, offset = [], None
    while len(results) < limit:
        body: dict = {
            "limit": min(250, limit - len(results)),
            "with_payload": True,
            "with_vector": False,
            "order_by": {"key": "ingested_at", "direction": "desc"},
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        if offset is not None:
            body["offset"] = offset
        resp = requests.post(url, headers=qdrant_headers(), json=body, timeout=20)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        batch = result.get("points", [])
        results.extend(batch)
        next_offset = result.get("next_page_offset")
        if not next_offset or not batch:
            break
        offset = next_offset
    # order_by already returns newest-first; sort is kept as a safety net
    return [{"payload": p.get("payload", {}), "id": p.get("id"), "score": 1.0} for p in results]


def fetch_by_defect_types(defect_types: list, limit: int = 200, extra_must: list | None = None) -> list:
    """Fetch detections whose defect_name matches ANY of the given types.

    Used by the count route so a "how many burr and spatter" question can offer a
    follow-up parameter breakdown that covers BOTH types, instead of the newest
    few of an unfiltered mix. extra_must ANDs in station/date/operator scoping so
    the stored follow-up set matches the same records that were counted.
    """
    types = [str(t).lower() for t in defect_types if str(t).strip()]
    if not types:
        return []
    must = [{"should": [{"key": "defect_name", "match": {"text": t}} for t in types]}]
    if extra_must:
        must.extend(extra_must)
    return _scroll_with_filter({"must": must}, limit=limit)


def _build_aggregate_summary(points: list) -> str:
    """Compute statistics over a full filtered point set and return a compact summary string."""
    if not points:
        return "No detections found for this query."

    from collections import Counter
    total = len(points)
    defect_counts = Counter()
    station_counts = Counter()
    cell_counts = Counter()
    operator_counts = Counter()
    confidences = []
    cause_fields: dict[str, list] = {}

    for pt in points:
        p = pt.get("payload", {})
        if p.get("defect_name"):
            defect_counts[p["defect_name"]] += 1
        if p.get("station"):
            station_counts[str(p["station"])] += 1
        if p.get("cell_number"):
            cell_counts[str(p["cell_number"])] += 1
        if p.get("operator"):
            operator_counts[str(p["operator"])] += 1
        conf = parse_confidence(p.get("confidence"))
        if conf is not None:
            confidences.append(conf)
        # Collect cause/parameter fields (direct keys that aren't standard metadata)
        standard = {"defect_name", "class_name", "station", "cell_number", "camera",
                    "confidence", "operator", "part_type", "event_time", "ingested_at",
                    "defect_ID", "content", "bounding_box_x", "bounding_box_y",
                    "bounding_box_width", "bounding_box_height"}
        for k, v in p.items():
            if k not in standard and isinstance(v, (int, float, str)) and v not in ("", None):
                cause_fields.setdefault(k, []).append(v)

    lines = [f"AGGREGATE SUMMARY ({total} detections total):"]

    if defect_counts:
        lines.append("Defect breakdown: " + ", ".join(
            f"{d}: {c} ({round(100*c/total)}%)" for d, c in defect_counts.most_common()))

    if station_counts:
        lines.append("By station: " + ", ".join(
            f"station {s}: {c}" for s, c in station_counts.most_common()))

    if cell_counts and len(cell_counts) > 1:
        lines.append("By cell: " + ", ".join(
            f"cell {c}: {n}" for c, n in cell_counts.most_common(5)))

    if operator_counts:
        lines.append("By operator: " + ", ".join(
            f"{o}: {c}" for o, c in operator_counts.most_common(5)))

    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        lines.append(
            f"Confidence average (pre-computed \u2014 use this exact value verbatim, do not recalculate): {avg_conf:.2f}"
        )

    # Out-of-range parameter frequency across all detections
    truth_index = {k.lower(): k for k in TRUTH_DB}
    oor_counts: dict[str, int] = {}
    for pt in points:
        p = pt.get("payload", {})
        defect_name = p.get("defect_name", "")
        truth_key = truth_index.get(defect_name.lower()) if defect_name else None
        if not truth_key or truth_key == "stations":
            continue
        entry = TRUTH_DB[truth_key]
        param_ranges = entry.get("parameter_ranges") or entry.get("parameter ranges", {})
        payload_norm = {}
        for k, v in p.items():
            try:
                payload_norm[_norm(k)] = float(v)
            except (TypeError, ValueError):
                pass
        for tp, rng in param_ranges.items():
            val = payload_norm.get(_norm(tp))
            if val is None:
                continue
            lo, hi = rng.get("min"), rng.get("max")
            if not ((lo is None or val >= lo) and (hi is None or val <= hi)):
                oor_counts[tp] = oor_counts.get(tp, 0) + 1
    if oor_counts:
        sorted_oor = sorted(oor_counts.items(), key=lambda x: -x[1])
        lines.append("Out-of-range parameter frequency: " + ", ".join(
            f"{param}: {cnt}/{total} ({round(100*cnt/total)}%)"
            for param, cnt in sorted_oor
        ))

    # Include all points when dataset is small; cap at 10 otherwise
    sample_size = total if total <= 15 else 10
    sample = points[:sample_size]
    lines.append(f"\nSAMPLE DETECTIONS ({sample_size} most recent):")
    for i, pt in enumerate(sample, 1):
        p = pt.get("payload", {})
        lines.append(
            f"  {i}. {p.get('defect_name','?')} | station={p.get('station','?')} "
            f"cell={p.get('cell_number','?')} "
            f"received={format_ingested_at(p.get('ingested_at'))}"
        )

    return "\n".join(lines)

#------------------------------------------------------------ definitive prompt injection site------------------------------------------------------------

# Marker text ending a prompt that invites a follow-up ("yes", "corrective
# actions", "full data"). Either the detection-summary closing line OR the
# trend-analysis closing line qualifies.
_FOLLOWUP_PROMPT_MARKERS = (
    "Would you like the recommended corrective actions",
    "Would you like a full breakdown of the individual detections",
    "Would you like the parameter breakdown for these detections",
    "Would you like the full parameter breakdown for these detections",
)

# Short affirmatives / explicit requests that mean "act on the detections you just showed".
_FOLLOWUP_RE = re.compile(
    r"^\s*(yes|yea|yeah|yep|yup|sure|ok|okay|okey|yes please|please|both|all|"
    r"go ahead|do it|proceed|continue|absolutely|definitely|"
    r"corrective actions?|recommended actions?|recommendations?|follow[- ]?up actions?|"
    r"full (data|breakdown|details?)|breakdowns?|parameters?|"
    r"(tell me )?more( (data|details?|info|information|please))?|"
    r"(show|give|send|get|see|want|need)( me)?( the| a| all)? "
    r"(everything|all|both|breakdown|full breakdown|corrective actions?|"
    r"recommendations?|details?|data|info|information|parameters?|more))\b",
    re.IGNORECASE,
)

# Continuation keywords that, in a SHORT reply, signal "act on the last detections"
# even when the message doesn't open with an affirmative (e.g. "the breakdown",
# "corrective actions for both"). Kept separate so the length guard only applies here.
_FOLLOWUP_KEYWORDS = re.compile(
    r"\b(corrective actions?|recommended actions?|recommendations?|breakdown|"
    r"parameters?|full (data|details?|breakdown)|more( (data|details?|info|information))?|"
    r"info(rmation)?|everything)\b",
    re.IGNORECASE,
)


def _looks_like_followup(prompt: str) -> bool:
    """True if the prompt reads as a continuation of the previous detection turn.

    Two ways to qualify: (1) it opens with an affirmative / explicit request
    (_FOLLOWUP_RE), or (2) it is a short message that references a breakdown or
    corrective actions. This is only ever consulted alongside
    _is_followup_to_detection(), so a genuinely new question that merely contains
    one of these words won't be misrouted unless the previous turn actually
    invited a follow-up.
    """
    if _FOLLOWUP_RE.match(prompt):
        return True
    if len(prompt.split()) <= 6 and _FOLLOWUP_KEYWORDS.search(prompt):
        # A named defect type means the user is starting a NEW query about that
        # defect (e.g. "any more spatter events?"), not continuing the previous
        # detection turn — so don't treat it as a follow-up.
        low = prompt.lower()
        for name in TRUTH_DB:
            if name == "stations":
                continue
            if re.search(rf"\b{re.escape(name.lower())}s?\b", low):
                return False
        return True
    return False


def _is_followup_to_detection() -> bool:
    """True if the most recent assistant message ended with a follow-up invite."""
    for m in reversed(st.session_state.get("messages", [])):
        if m.get("role") == "assistant":
            content = m.get("content", "")
            return any(marker in content for marker in _FOLLOWUP_PROMPT_MARKERS)
    return False


def _wants_single_latest(prompt: str) -> bool:
    """True when the user asks about ONE most-recent item (singular phrasing).

    The parser maps bare recency phrases ('most recent', 'latest') to last_n=5.
    This check overrides that to 1 for singular phrasing: 'the latest detection' /
    'most recent burr' -> 1, while 'the last 3' or 'recent defects' (plural) are
    left to the explicit count.
    """
    q = prompt.lower()
    # Only a COUNT-style number ("last 3", "3 most recent", "top 5 detections")
    # should defer to the extractor. A bare number elsewhere (station 2, cell 1,
    # a date/time) must NOT disable singular detection.
    if re.search(r"\b(?:last|latest|newest|recent|most recent|top)\s+\d+\b", q) or \
       re.search(r"\b\d+\s+(?:most recent|latest|newest|recent|defects|detections|events)\b", q):
        return False
    if not re.search(r"\b(most recent|latest|newest|last)\b", q):
        return False
    # A plural noun after the recency word means the user wants several — keep default.
    if re.search(r"\b(most recent|latest|newest|last)\b[\w\s]*?\b"
                 r"(defects|detections|events|alerts|records|ones)\b", q):
        return False
    # Singular noun (defect/detection/... or a known defect type) -> single item.
    singular = "defect|detection|event|alert|record|one"
    defect_names = "|".join(re.escape(n.lower()) for n in TRUTH_DB if n != "stations")
    if defect_names:
        singular = f"{singular}|{defect_names}"
    return bool(re.search(rf"\b(most recent|latest|newest|last)\b[\w\s]*?\b({singular})\b", q))


def _build_engineer_referrals(context_points: list) -> str:
    """Deterministic station -> overseeing engineer lookup for the detections in
    context, so a follow-up / trend answer names the RIGHT engineer verbatim
    instead of relying on the LLM to cross-reference the knowledge base."""
    if not TRUTH_DB or not context_points:
        return ""
    stations = TRUTH_DB.get("stations", {})
    lines = []
    seen = set()
    for pt in context_points:
        p = pt.get("payload", {})
        st_key = str(p.get("station", "")).strip()
        if not st_key or st_key in seen:
            continue
        seen.add(st_key)
        entry = stations.get(st_key)
        sname = (entry or {}).get("name", st_key)
        eng = (entry or {}).get("overseeing_engineer")
        if eng:
            lines.append(f"- {sname} ({st_key}): overseeing engineer is {eng}")
        else:
            lines.append(f"- {sname} ({st_key}): NO overseeing engineer on record — escalate to the shift supervisor")
    if not lines:
        return ""
    return (
        "\nENGINEER REFERRALS (computed in Python — for EACH detection that has an out-of-range "
        "parameter you MUST add a referral naming the overseeing engineer for THAT detection's station "
        "EXACTLY as listed here; never invent or omit a name):\n"
        + "\n".join(lines) + "\n"
    )


# Word-to-digit mapping used by _resolve_filter_value to match stored
# canonical forms like 'station_1' from user input like 'station one'.
_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# User words -> normalized payload field name (via _norm) for order-by queries.
_ORDER_FIELD_SYNONYMS = {
    "confidence": "confidence",
    "voltage": "voltage",
    "current": "current", "amperage": "current", "amps": "current",
    "weld temperature": "weld temperature", "temperature": "weld temperature", "heat": "weld temperature",
    "weld time": "weld time", "welding time": "weld time",
    "electrode angle": "electrode angle", "angle": "electrode angle",
    "oxidation level": "oxidation level", "oxidation": "oxidation level",
    "edge distance": "edge distance",
    "electrode use cycle": "electrode use cycle",
}


def _detect_group_by(prompt: str):
    """Return the payload field to group by for questions like 'which station has
    the most defects', 'most common defect', 'busiest station' — or None."""
    q = prompt.lower()
    if re.search(r"\bstations?\b", q):
        dim = "station"
    elif re.search(r"\bcells?\b", q):
        dim = "cell_number"
    elif re.search(r"\b(operators?|robots?)\b", q):
        dim = "operator"
    elif re.search(r"\b(defects?|types?)\b", q):
        dim = "defect_name"
    elif re.search(r"\bbusiest\b", q):
        dim = "station"
    elif re.search(r"\bmost (common|frequent)\b", q):
        dim = "defect_name"
    else:
        return None
    has_super = re.search(r"\b(most|fewest|least|highest|lowest|common|frequent|top|busiest)\b", q)
    is_which = re.search(r"\b(which|what|whose|where|busiest)\b", q) or re.search(r"\bmost (common|frequent)\b", q)
    return dim if (has_super and is_which) else None


def _detect_ordering(prompt: str):
    """Return (norm_field, direction, n) for superlative / order-by queries such as
    'highest confidence detection', 'lowest voltage', 'worst defect'.

    direction is 'desc' (biggest first) or 'asc'. field '__oor__' ranks by the
    number of out-of-range parameters — the natural 'worst/best' when no metric is
    named. Returns None when the query isn't an ordering request.
    """
    q = prompt.lower()
    asc_words = r"lowest|least|min(?:imum)?|smallest|worst"
    desc_words = r"highest|most|max(?:imum)?|greatest|largest|top|best"
    if not re.search(rf"\b({asc_words}|{desc_words})\b", q):
        return None
    direction = "asc" if re.search(rf"\b({asc_words})\b", q) else "desc"

    field = None
    for syn in sorted(_ORDER_FIELD_SYNONYMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(syn)}\b", q):
            field = _ORDER_FIELD_SYNONYMS[syn]
            break
    if field is None:
        # 'worst/best defect' with no named metric -> rank by out-of-range severity.
        if re.search(r"\b(worst|best)\b", q) and re.search(r"\b(defects?|detections?|parts?|welds?)\b", q):
            field = "__oor__"
            direction = "desc" if re.search(r"\bworst\b", q) else "asc"
        else:
            return None

    n = 1  # superlatives are singular unless the user asks for top/bottom N
    m = re.search(r"\b(?:top|bottom|first|last)\s+(\d+)\b", q)
    if m:
        n = int(m.group(1))
    return (field, direction, n)


def _is_total_count_query(prompt: str) -> bool:
    """True for unqualified/total counts ('how many detections in total', 'how many
    defects today') that name no specific defect type, so they need a filtered
    total rather than the defect-name count route."""
    q = prompt.lower()
    return bool(re.search(
        r"\bhow many\b|\bnumber of\b|\bhow much\b|\btotal (number|count)\b|"
        r"\bcount (of |the )?(detections|defects|events|records|alerts)\b", q))


def _is_trend_query(prompt: str) -> bool:
    """True for trend/analysis phrasings ('trends', 'analyse', 'common issues',
    'distribution') so they deterministically hit the aggregate route instead of
    depending on the 3B classifying mode='aggregate'."""
    return bool(re.search(
        r"\b(trends?|analy[sz]e|analys[ie]s|common (issues?|problems?|defects?|faults?)|"
        r"commonalit|overview|distribution|patterns?|insights?|breakdown of all)\b",
        prompt.lower()))


def _payload_numeric(payload: dict, norm_field: str):
    """Numeric value of a payload field addressed by its normalized name."""
    for k, v in payload.items():
        if _norm(k) == norm_field:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _oor_count(payload: dict) -> int:
    """Number of out-of-range welding parameters for one detection (Truth ranges)."""
    truth_index = {k.lower(): k for k in TRUTH_DB}
    tk = truth_index.get(str(payload.get("defect_name", "")).lower())
    if not tk or tk == "stations":
        return 0
    entry = TRUTH_DB[tk]
    param_ranges = entry.get("parameter_ranges") or entry.get("parameter ranges", {})
    payload_norm = {}
    for k, v in payload.items():
        try:
            payload_norm[_norm(k)] = float(v)
        except (TypeError, ValueError):
            pass
    c = 0
    for tp, rng in param_ranges.items():
        val = payload_norm.get(_norm(tp))
        if val is None:
            continue
        lo, hi = rng.get("min"), rng.get("max")
        if not ((lo is None or val >= lo) and (hi is None or val <= hi)):
            c += 1
    return c


def fetch_ordered(norm_field: str, direction: str, n: int) -> list:
    """Scroll the full collection and return the top-N points ordered by a numeric
    field (or out-of-range severity for '__oor__')."""
    pts = _scroll_with_filter(None, limit=2000)
    scored = []
    for pt in pts:
        p = pt.get("payload", {})
        val = _oor_count(p) if norm_field == "__oor__" else _payload_numeric(p, norm_field)
        if val is not None:
            scored.append((val, pt))
    if not scored:
        return []
    scored.sort(key=lambda x: x[0], reverse=(direction == "desc"))
    return [pt for _, pt in scored[:max(1, n)]]


_SMALLTALK_RE = re.compile(
    r"^\s*("
    r"hi+|hello+|hey+( there)?|howdy|greetings|good (morning|afternoon|evening)|sup\b|yo\b|"
    r"thanks?( you)?|thank you|cheers|ok(ay)?|cool|great|sounds good|got it|perfect|"
    r"who are you|what are you|what can you do|what do you do|how do you work|"
    r"what('s| is) (this|that)|help( me)?|what('s| is) your (name|purpose)|"
    r"why\b|huh\b|what\?|hm+|hmm+|interesting|nice|wow"
    r")\s*[!?.]*\s*$",
    re.I,
)

_SMALLTALK_REPLY = (
    "I'm the FORGE Detection Assistant. I can help you query and analyse the "
    "defect detection data from this system. Try asking me things like:\n"
    "- *Show me burr detections from station 1*\n"
    "- *How many defects were detected today?*\n"
    "- *Which station has the most defects?*\n"
    "- *What were the trends this week?*\n"
    "- *Tell me about the most recent detection*"
)


def _is_smalltalk(prompt: str) -> bool:
    """True for greetings, thanks, meta/identity questions, and other non-detection
    inputs that should get a capability reply rather than a record dump."""
    return bool(_SMALLTALK_RE.match(prompt.strip()))


def build_grounded_prompt(prompt: str) -> str:
    is_count = False
    is_total = False
    is_defect_id_lookup = False
    order_spec = None
    group_dim = None
    # Non-defect constraints (station/date/operator) applied to a qualified count
    # so "how many burr at station 2" counts only station_2, not every burr.
    count_extra_must = None
    # Defect types named in a "how many X and Y" question — drives the count route
    # below and the exact-count facts injected into the prompt.
    # --- Smalltalk / meta short-circuit — return a capability reply immediately
    # without touching Qdrant or the LLMs. 'hi', 'what can you do', etc. used to
    # fall through to a vector search and dump 12 detections at the user (G9).
    if _is_smalltalk(prompt):
        print("[rag] route=smalltalk points=0")
        return _SMALLTALK_REPLY

    count_terms = _extract_count_intent(prompt)
    # --- Follow-up short-circuit: reuse the detections from the previous turn so a
    # bare "yes" / "corrective actions" / "full data" doesn't re-fetch unrelated
    # records (which loses the assessment context and bloats the prompt). ---
    stored = st.session_state.get("last_detection_points")
    if stored and _looks_like_followup(prompt) and _is_followup_to_detection():
        context_points = stored
        agg_summary = ""
        context = build_chat_context(context_points)
        is_followup = True
        print(f"[rag] route=followup_reuse points={len(context_points)}")
    else:
        is_followup = False
        resolved = _resolve_pronouns(prompt)
        intent = _extract_query_intent(resolved)
        print(f"[intent] {intent}")

        # Mode is already set by the spaCy parser; override for trend queries
        # that need aggregate regardless of what mode was inferred.
        mode = intent.get("mode") or "vector"

        # Trend/analysis wording always hits the aggregate route.
        if _is_trend_query(prompt):
            mode = "aggregate"

        raw_last_n = intent.get("last_n")
        try:
            last_n = int(raw_last_n) if raw_last_n is not None else None
            if last_n is not None and last_n <= 0:
                last_n = None
        except (TypeError, ValueError):
            # Safety fallback: non-numeric last_n value.
            last_n = 5 if mode in ("list", "lookup") else None

        # Singular "the most recent defect" — override the default last_n=5
        # to 1 so only the single requested detection is fetched.
        if mode in ("list", "lookup") and _wants_single_latest(prompt):
            last_n = 1

        qdrant_filter = _build_qdrant_filter(intent)
        has_filter = bool(qdrant_filter)

        # Deterministic analytic routes — only consulted when this isn't a count question.
        order_spec = None if count_terms else _detect_ordering(prompt)
        group_by = (not count_terms) and (order_spec is None) and _detect_group_by(prompt)
        is_total = (not count_terms) and (order_spec is None) and (not group_by) \
            and _is_total_count_query(prompt)

        # --- Route: exact count ("how many X and Y") — deterministic so the count
        # is exact AND a follow-up can show parameters for the SAME defect types.
        # Takes precedence over defect_id: a count question is never an ID lookup,
        # even when the 3B extractor hallucinates a defect_id for it. ---
        if count_terms:
            count_extra_must = _non_defect_musts(qdrant_filter)
            context_points = fetch_by_defect_types(count_terms, limit=200, extra_must=count_extra_must)
            print(f"[rag] route=count terms={count_terms} extra_must={count_extra_must} points={len(context_points)}")
            is_count = True
            agg_summary = ""
            context = ""

        # --- Route: order-by / superlative ('highest confidence', 'worst defect')
        # Ranked over the full collection so the result is exact. ---
        elif order_spec:
            field, direction, n = order_spec
            context_points = fetch_ordered(field, direction, n)
            print(f"[rag] route=order_by field={field} dir={direction} n={n} points={len(context_points)}")
            agg_summary = ""
            context = build_chat_context(context_points)

        # --- Route: group-by ('which station has the most defects') — force a
        # full-dataset aggregate; its summary already holds the per-group counts. ---
        elif group_by:
            group_dim = group_by
            all_points = _scroll_with_filter(None, limit=2000)
            print(f"[rag] route=group_by dim={group_dim} points={len(all_points)}")
            agg_summary = _build_aggregate_summary(all_points)
            context_points = all_points
            context = ""

        # --- Route: total / unqualified count ('how many detections today') —
        # an exact filtered total, not a defect-name count. ---
        elif is_total:
            context_points = _scroll_with_filter(qdrant_filter, limit=2000)
            print(f"[rag] route=total_count filter={qdrant_filter} points={len(context_points)}")
            agg_summary = ""
            context = ""

        # --- Route 1: defect ID lookup ---
        elif intent.get("defect_id"):
            context_points = fetch_by_defect_id(intent["defect_id"])
            print(f"[rag] route=defect_id points={len(context_points)}")
            is_defect_id_lookup = True
            agg_summary = ""
            context = build_chat_context(context_points)

        # --- Route 2: aggregate / trend analysis ---
        # Fires whenever mode=aggregate, with or without last_n.
        # If last_n is set, scroll only that many points; otherwise scroll all.
        elif mode == "aggregate":
            limit = int(last_n) if last_n else 2000
            all_points = _scroll_with_filter(qdrant_filter, limit=limit)
            print(f"[rag] route=aggregate points={len(all_points)}")
            agg_summary = _build_aggregate_summary(all_points)
            print(f"[agg] summary: {agg_summary[:300].replace(chr(10), ' ')}")
            # Keep the full analyzed set as context so truth_section sees every
            # station/defect involved (engineer referrals) and a follow-up
            # breakdown covers the same detections that were analyzed.
            context_points = all_points
            context = ""

        # --- Route 3: filtered list — only fires when structured filters are present ---
        elif has_filter:
            limit = int(last_n or 50)
            all_points = _scroll_with_filter(qdrant_filter, limit=limit)
            print(f"[rag] route=list filter={qdrant_filter} points={len(all_points)}")
            context_points = all_points
            agg_summary = ""
            context = build_chat_context(context_points)

        # --- Route 4: time-ordered last-N, OR mode=list with no filters (deterministic scroll) ---
        elif last_n or mode == "list":
            n = int(last_n) if last_n else 50
            raw = get_latest_points(limit=n)
            context_points = [{"payload": p.get("payload", {}), "id": p.get("id"), "score": 1.0} for p in raw]
            print(f"[rag] route=last_n n={n} points={len(context_points)}")
            agg_summary = ""
            context = build_chat_context(context_points)

        # --- Route 5: vector similarity fallback ---
        else:
            context_points = semantic_search(resolved, limit=15)
            print(f"[rag] route=vector_search points={len(context_points)}")
            agg_summary = ""
            context = build_chat_context(context_points)

    # Remember the detection set so the NEXT turn's follow-up can reuse it.
    # For a trend/aggregate turn, cap the stored set so a "show breakdown"
    # follow-up doesn't try to expand hundreds of detections, while still
    # covering the full set for small analyses (e.g. "trends in last 10").
    if context_points:
        # Cap the stored set for count/aggregate turns so a follow-up breakdown
        # doesn't try to expand hundreds of detections.
        st.session_state["last_detection_points"] = (
            context_points[:15] if (agg_summary or is_count or is_total) else context_points
        )

    truth_section = _build_truth_section(context_points)
    # A trend/count/total prompt reasons from the summary or exact counts, so the
    # per-detection parameter assessment is noise there — skip it to stay lean.
    param_assessment = "" if (agg_summary or is_count or is_total) else _build_parameter_assessment(context_points)

    # Exact count fact(s) for "how many X" queries — one line per defect type so
    # joined questions ("how many burr and spatter") get an exact count for each
    # instead of the LLM fabricating the missing number.
    count_fact = ""
    if count_terms:
        count_lines = []
        for term in count_terms:
            try:
                n = count_detections(term, extra_must=count_extra_must)
                count_lines.append(f"- There are {n} '{term}' detection(s) currently stored.")
            except Exception:
                pass
        if count_lines:
            count_fact = (
                "\nEXACT COUNTS (from full database query — use these numbers verbatim, "
                "do NOT estimate or invent any count):\n" + "\n".join(count_lines) + "\n"
            )

    # Exact total for unqualified/total count questions ('how many detections today').
    total_fact = ""
    if is_total:
        total_fact = (
            f"\nEXACT TOTAL (from database query — use this number verbatim, do NOT "
            f"estimate): {len(context_points)} detection(s) match this query.\n"
        )

    # Deterministic winner for order-by / superlative questions so the model states
    # the exact value instead of guessing from the record list.
    ordering_fact = ""
    if order_spec and context_points:
        _ofield, _odir, _ = order_spec
        _p0 = context_points[0].get("payload", {})
        if _ofield == "__oor__":
            _val = _oor_count(_p0)
            _label = "number of out-of-range parameters"
        else:
            _val = _payload_numeric(_p0, _ofield)
            _label = _ofield
        ordering_fact = (
            f"\nORDERING RESULT (computed in Python — use verbatim): the "
            f"{'highest' if _odir == 'desc' else 'lowest'} {_label} is {_val}, belonging "
            f"to the FIRST detection shown below. Answer using that detection.\n"
        )

    # Deterministic group tally for 'which station/defect/operator has the most'.
    group_fact = ""
    if group_dim and context_points:
        from collections import Counter as _Counter
        _c = _Counter()
        for _pt in context_points:
            _v = _pt.get("payload", {}).get(group_dim)
            if _v not in (None, ""):
                _c[str(_v)] += 1
        if _c:
            _want_min = bool(re.search(r"\b(fewest|least|lowest)\b", prompt.lower()))
            _ordered = _c.most_common()
            _winner = _ordered[-1] if _want_min else _ordered[0]
            group_fact = (
                f"\nGROUP RESULT (computed in Python — use verbatim): counts by {group_dim} are "
                + ", ".join(f"{k}: {n}" for k, n in _ordered)
                + f". The {'fewest' if _want_min else 'most'} is {_winner[0]} with {_winner[1]} detection(s).\n"
            )

    # Deterministic station -> engineer lookup so follow-up / trend answers name
    # the RIGHT overseeing engineer verbatim instead of relying on the LLM to
    # cross-reference. Only relevant where corrective actions / recommendations
    # (and thus referrals) are produced: follow-up breakdowns and trend analyses.
    referral_fact = ""
    if (is_followup or agg_summary or is_defect_id_lookup) and context_points:
        referral_fact = _build_engineer_referrals(context_points)

    # Pre-computed complete welding data tables (OOR + IN RANGE) for turns where
    # the full Rule-10 breakdown is shown immediately.  Injecting the formatted
    # table as a literal fact eliminates the 7B's habit of silently dropping IN
    # RANGE parameters from the Welding Data section.
    welding_data_tables = ""
    if (is_followup or is_defect_id_lookup) and context_points:
        welding_data_tables = _build_welding_data_tables(context_points)

    # Last 6 messages for follow-up context
    history_lines = []
    for msg in st.session_state.get("messages", [])[-6:]:
        role = "User" if msg["role"] == "user" else "Assistant"
        history_lines.append(f"{role}: {msg['content']}")
    history_section = ("\nCONVERSATION HISTORY:\n" + "\n".join(history_lines) + "\n") if history_lines else ""

    no_context_note = "No relevant detections found for this query." if not context_points else ""

    context_block = (
        f"AGGREGATE ANALYSIS:\n{agg_summary}\n" if agg_summary
        else f"DETECTION RECORDS:\n{context or no_context_note}\n"
    )

    # Turn-specific directive placed at the VERY END of the prompt (strongest
    # recency weight) so it overrides the earlier rules' "end with 'Would you
    # like…'" instructions when this turn is a follow-up breakdown.
    turn_directive = ""
    if is_followup or is_defect_id_lookup:
        _n = len(context_points)
        _ctx = "FOLLOW-UP" if is_followup else "DEFECT ID LOOKUP"
        turn_directive = (
            f"\nTHIS TURN IS A {_ctx}: Provide the complete, immediate full breakdown for all {_n} detection(s). "
            f"Follow RULE 10 EXACTLY. Output exactly {_n} '### Detection N' section(s) "
            f"(N = 1 through {_n}) and STOP immediately after "
            "the last detection's '#### Recommended Corrective Actions' section. "
            f"Do NOT stop after fewer than {_n} detections. "
            "Your response is the final breakdown — it MUST NOT contain any sentence starting with 'Would you like' "
            "and MUST NOT offer any further breakdown, corrective actions, or additional data.\n"
            "For the '#### Welding Data' section of EACH detection, copy the corresponding table VERBATIM "
            "from the PRE-COMPUTED WELDING DATA TABLES above — include EVERY parameter line, "
            "both '— IN RANGE' and '— OUT OF RANGE' lines. Do NOT omit any line.\n"
            "For EVERY detection that has at least one out-of-range parameter, you MUST end its "
            "'#### Recommended Corrective Actions' section with a referral line naming the overseeing engineer for "
            "THAT detection's station, taken EXACTLY from the ENGINEER REFERRALS list above. Do not skip it.\n"
        )
    elif (not agg_summary and not is_count and not is_total and not order_spec
          and not group_dim and len(context_points) > 1):
        # Multi-detection describe turn: the 7B sometimes stops after the first
        # detection. A tail directive with an explicit count forces it to cover all.
        turn_directive = (
            f"\nIMPORTANT — MULTI-DETECTION RESPONSE: The DETECTION RECORDS below contain "
            f"{len(context_points)} separate detections. You MUST output exactly "
            f"{len(context_points)} '### Detection N' sections (N = 1 through {len(context_points)}) "
            f"before writing the final 'Would you like...' line. "
            f"Do NOT stop or end your response after fewer than {len(context_points)} sections.\n"
        )

    # A count turn answers ONLY with the exact counts, then invites a parameter
    # follow-up whose stored detections cover every requested defect type.
    count_directive = ""
    if is_count:
        _ct = len(count_terms)
        count_directive = (
            f"\nTHIS TURN IS A COUNT QUERY. There are {_ct} defect type(s) to count: {', '.join(count_terms)}. "
            f"Using the EXACT COUNTS above, state the count for each of the {_ct} defect type(s) in one short "
            f"sentence each. Do NOT stop after fewer than {_ct} counts. "
            "Do NOT produce a trend analysis and do NOT list individual "
            "detections. End your ENTIRE response with this exact line and nothing after it:\n"
            "Would you like the parameter breakdown for these detections?\n"
        )

    # A total-count turn answers with just the exact total.
    total_directive = ""
    if is_total:
        total_directive = (
            "\nTHIS TURN IS A COUNT QUERY. State the EXACT TOTAL above in one short sentence. "
            "Do NOT list individual detections and do NOT produce a trend analysis. "
            "Do NOT ask any 'Would you like...' follow-up question.\n"
        )

    # A superlative / order-by turn answers with the ranked winner and its value.
    order_directive = ""
    if order_spec:
        _on = order_spec[2]
        if _on and _on > 1:
            order_directive = (
                f"\nTHIS TURN IS A RANKING QUERY. The {_on} detections below are ALREADY sorted best-first. "
                f"List all {_on} as a numbered list in that order; for each, state the ranked value plus the "
                "defect, station and received time. "
                f"Do NOT stop after fewer than {_on} items. "
                "Do NOT output a multi-section per-detection breakdown and "
                "do NOT ask any 'Would you like...' follow-up question.\n"
            )
        else:
            order_directive = (
                "\nTHIS TURN IS A SUPERLATIVE QUERY. Using the ORDERING RESULT above, answer in ONE short "
                "sentence: state the value and name the winning detection (defect, station, received time). "
                "Do NOT output a multi-section per-detection breakdown and do NOT ask any 'Would you like...' "
                "follow-up question.\n"
            )

    # A group-by turn names the top group directly from the GROUP RESULT.
    group_directive = ""
    if group_dim:
        group_directive = (
            "\nTHIS TURN IS A GROUPING QUERY. Using the GROUP RESULT above, answer in ONE or two short "
            "sentences: name the winning " + group_dim + " and its count, then optionally list the other "
            "groups' counts. Do NOT use the trend template, do NOT describe individual detections, and do "
            "NOT ask any 'Would you like...' follow-up question.\n"
        )

    # Suppress the "recommended corrective actions" invite when it makes no sense:
    # no detections matched, or none of them are out of range (nothing to correct).
    # Only applies to the fresh describe/list/vector/defect_id paths — the
    # aggregate, count, total, order, group and follow-up turns manage their own endings.
    ending_directive = ""
    if (not is_followup and not is_count and not is_total and not agg_summary
            and not order_spec and not group_dim):
        if not context_points:
            ending_directive = (
                "\nENDING RULE (overrides Rule 9): No detections matched this query. State that in one plain "
                "sentence and do NOT ask any 'Would you like...' follow-up question.\n"
            )
        elif not _has_corrective_actions(context_points):
            ending_directive = (
                "\nENDING RULE (overrides Rule 9): None of these detections have out-of-range parameters, so "
                "there are NO corrective actions. Do NOT ask 'Would you like the recommended corrective "
                "actions...'. End your ENTIRE response with exactly this line and nothing after it:\n"
                "Would you like the full parameter breakdown for these detections?\n"
            )

    return (

        "You are a detection assistant for a manufacturing defect system. "
        "IMPORTANT RULES:\n"
        "1. Each detection block is a separate, independent event — never merge them.\n"
        "2. Only discuss detections matching ALL criteria in the question. Silently exclude non-matching ones.\n"
        "3. Never alter or misrepresent detection attributes.\n"
        "4. If no detection matches the criteria, say so explicitly.\n"
        "5. If the answer is not in the context, say you do not know.\n"
        "6. The PRE-COMPUTED PARAMETER ASSESSMENT already decided every range verdict in Python. It gives two explicit lists per detection: 'OUT OF RANGE parameters' and 'IN RANGE parameters'. Copy those lists EXACTLY. NEVER compute a range yourself, and NEVER move a parameter from one list to the other. "
        "The [MARKDOWN REPORT] section (from the raw detection data) lists raw parameter values with NO range assessment — it is for narrative context only (event ID, station, timing). "
        "For parameter range verdicts you MUST use ONLY the PRE-COMPUTED PARAMETER ASSESSMENT block. If a parameter appears in the OUT OF RANGE list there, it IS out of range — even if its value looks close to the limit. Do NOT re-evaluate or skip it.\n"
        "7. Corrective actions apply ONLY to parameters in the 'OUT OF RANGE parameters' list. Each such parameter already has its 'Corrective Action:' text attached in the assessment — use only that. NEVER show a corrective action for an IN RANGE parameter.\n"
        "8. For aggregate / trend queries ('what are the trends', 'analyse', 'common issues', 'how many', 'which station', 'most frequent', etc.), "
        "reason ONLY from the AGGREGATE ANALYSIS section. Use EXACTLY this structure:\n"
        "## Trend Analysis\n\n"
        "### Overview\n"
        "State total detections, time span if available, and defect type breakdown from the summary.\n\n"
        "### Defect Distribution\n"
        "List each defect type with count and percentage from the summary.\n\n"
        "### Station & Cell Breakdown\n"
        "List stations and cells with counts. Note which is most affected.\n\n"
        "### Most Frequent Out-of-Range Parameters\n"
        "The AGGREGATE ANALYSIS has a single 'Out-of-range parameter frequency:' line containing comma-separated entries. "
        "Output ONE bullet for EVERY entry on that line — the number of bullets MUST equal the number of comma-separated entries. "
        "Do NOT omit, merge, or truncate any entry, even if there are many. Preserve their order. Format each as:\n"
        "- **PARAM_NAME**: out of range in X/TOTAL detections (XX%) — briefly state what this means operationally.\n"
        "If no 'Out-of-range parameter frequency:' line is present, write '- No parameter data available.'\n\n"
        "### Confidence Average:\n"
        "State the confidence average from the 'Confidence average (pre-computed)' line in the AGGREGATE ANALYSIS EXACTLY as given — copy the number verbatim. Do NOT calculate, estimate, or derive any confidence value yourself.\n\n"
        "### Root Causes\n"
        "Based on the highest-frequency out-of-range parameters, identify 1–3 likely root causes. Reference specific parameter names and their frequency. Do not invent causes not supported by the data.\n\n"
        "### Recommendations\n"
        "For each root cause identified, state a concrete corrective action drawn from the DEFECT KNOWLEDGE BASE where available. "
        "Then, for each root cause, determine which station(s) it occurred at by cross-referencing the SAMPLE DETECTIONS and the station breakdown in the AGGREGATE ANALYSIS. "
        "For each such station, find the overseeing engineer in the ENGINEER REFERRALS block above (NOT by searching the DEFECT KNOWLEDGE BASE — use ONLY the pre-computed ENGINEER REFERRALS list). "
        "Add a targeted referral in this format: "
        "'Please refer to [ENGINEER_NAME] at [STATION_NAME] regarding [SPECIFIC_ISSUE].' "
        "Use the engineer name EXACTLY as it appears in the ENGINEER REFERRALS block — do not alter, expand initials, or invent a name. "
        "If a station appears in the ENGINEER REFERRALS block as 'NO overseeing engineer on record', write: "
        "'No overseeing engineer is on record for [STATION_NAME]; escalate to the shift supervisor.' "
        "Only refer to an engineer if their station is actually linked to the root cause being described — do NOT list engineers for stations unrelated to that issue. "
        "After ALL referral lines, add ONE single line: 'If escalation is necessary or they are unavailable, contact their direct supervisor.' "
        "Do NOT repeat the escalation line — it appears exactly once at the very end of this section.\n"
        "Finally, output the following sentence VERBATIM as the last line of your response, with nothing after it. Output ONLY the sentence itself — do NOT output this instruction or any of the words before the colon:\n"
        "Would you like a full breakdown of the individual detections in this analysis?\n"
        "9. When asked to describe or explain one or more detections ('tell me about', 'describe', 'what happened', 'explain', 'most recent', 'last N'), "
        "start your response DIRECTLY with '### Detection 1' — do NOT write any introductory sentence before the first heading. "
        "Give a CONCISE summary for EACH detection using EXACTLY this structure — replace N with 1, 2, 3… and every PLACEHOLDER with real values, never output angle brackets literally:\n"
        "### Detection N\nDetected defect: DEFECT_NAME at STATION on RECEIVED_TIMESTAMP.\n\n"
        "**Out-of-Range Parameters:**\n"
        "Copy EVERY line from that detection's 'OUT OF RANGE parameters' list in the PRE-COMPUTED PARAMETER ASSESSMENT, and ONLY those lines, formatted as:\n"
        "- **PARAM_NAME:** PARAM_VALUE ⚠ (normal: MIN–MAX)\n"
        "If that detection's 'OUT OF RANGE parameters' list is '(none — all parameters within normal range)', write exactly this single line instead: '- ✅ All welding parameters are within normal range.'\n"
        "Never place an IN RANGE parameter in this section. In this summary do NOT show in-range parameters, location/identification details, operator details, or corrective actions.\n"
        "After the LAST detection, end your ENTIRE response with this exact line and nothing after it:\n"
        "'Would you like the recommended corrective actions and the full parameter breakdown for these detections?'\n"
        "10. FOLLOW-UP: if the user asks for corrective actions, full data, or says 'yes'/'both'/'sure'/'go ahead' after the detection summary, "
        "ALWAYS provide BOTH sections below for EACH detection. Start DIRECTLY with '### Detection N' — no intro sentence. "
        "Do NOT repeat the 'Would you like…' question at the end.\n"
        "For each detection use EXACTLY this structure:\n"
        "### Detection N — DEFECT_NAME at STATION, received RECEIVED_TIMESTAMP\n\n"
        "#### Location & Identification\n"
        "- **Cell Number:** CELL_NUMBER\n"
        "- **Assembly Number:** ASSEMBLY_NUMBER\n"
        "- **Station:** STATION\n"
        "- **Station Part:** STATION_PART (omit if absent)\n"
        "- **Part ID:** PART_ID (omit if absent)\n"
        "- **Camera:** CAMERA\n\n"
        "#### Operator & Source\n"
        "- **Operator:** OPERATOR\n"
        "- **Source Path:** SOURCE_PATH (omit if absent)\n\n"
        "#### Welding Data\n"
        "Copy EVERY entry from BOTH the 'OUT OF RANGE parameters' and 'IN RANGE parameters' lists for this detection, "
        "formatted as '- **PARAM_NAME:** PARAM_VALUE — IN RANGE' or '- **PARAM_NAME:** PARAM_VALUE — OUT OF RANGE ⚠ (normal: MIN–MAX)'. "
        "Do NOT include corrective action text in this section.\n\n"
        "#### Recommended Corrective Actions\n"
        "For each parameter in the 'OUT OF RANGE parameters' list output: '- **PARAM_NAME:** CORRECTIVE_ACTION_TEXT' "
        "using the 'Corrective Action:' text already attached to that parameter in the assessment. "
        "If no parameters are out of range write: '- ✅ No corrective actions needed.' and add no referral for that detection.\n"
        "NEVER list a corrective action for an IN RANGE parameter.\n"
        "For EACH detection that has at least one out-of-range parameter, add a referral line immediately after "
        "its corrective action bullets, naming the Overseeing Engineer for THAT detection's station from the "
        "DEFECT KNOWLEDGE BASE, in EXACTLY this format: "
        "'Please refer to [ENGINEER_NAME] at [STATION_NAME] regarding [SPECIFIC_ISSUE].' "
        "Use the engineer name EXACTLY as written in the DEFECT KNOWLEDGE BASE — do not expand initials or invent a name. "
        "If that station has no Overseeing Engineer listed in the DEFECT KNOWLEDGE BASE, write instead: "
        "'No overseeing engineer is on record for [STATION_NAME]; escalate to the shift supervisor.'\n"
        "If at least one detection had out-of-range parameters, after the LAST detection add ONE final line "
        "(exactly once): 'If escalation is necessary or they are unavailable, contact their direct supervisor.'\n"
        "This response IS the full breakdown, so it must END immediately after the last detection's "
        "'#### Recommended Corrective Actions' section (and the single escalation line, if present). "
        "Do NOT append any closing question. "
        "Specifically, NEVER output any sentence beginning with 'Would you like' and NEVER offer a further breakdown, "
        "corrective actions, or more data — the user already has everything.\n"
        "11. For all other queries, answer only what was asked.\n\n"
        f"{count_fact}"
        f"{total_fact}"
        f"{ordering_fact}"
        f"{group_fact}"
        f"{context_block}\n"
        f"{param_assessment}"
        f"{welding_data_tables}"
        f"{truth_section}"
        f"{referral_fact}"
        f"{history_section}\n"
        f"User question: {prompt}\n"
        f"{turn_directive}"
        f"{count_directive}"
        f"{total_directive}"
        f"{order_directive}"
        f"{group_directive}"
        f"{ending_directive}"
    )
# ------------------------------------------------End Prompt Tuning Section------------------------------------

# Used to stream in per word text as opposed to having pre loaded chunks posted, 
def call_llm_stream(prompt):
    grounded = build_grounded_prompt(prompt)
    # Smalltalk / capability replies are returned verbatim — no LLM needed.
    if grounded is _SMALLTALK_REPLY:
        yield _SMALLTALK_REPLY
        return
    body = {
        "model": "qwen2.5:7b",
        "prompt": grounded,
        "stream": True,
        "temperature": 0,
        # 16384 so large follow-up breakdowns (up to ~15 detections) fit without
        # hitting the context ceiling and getting truncated into a broken reply.
        "options": {"num_ctx": 16384},
    }

    with requests.post(OLLAMA_URL, json=body, stream=True, timeout=120) as response:
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue

            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            token = chunk.get("response", "")
            if token:
                yield token

            if chunk.get("done"):
                break



# Dashboard visual display compenents + logic
def render_charts():
    """Analytics panel: 4 plotly charts over all stored detections."""
    import plotly.express as px
    import pandas as pd

    try:
        points = _scroll_with_filter({}, limit=500)
    except Exception as e:
        st.error(f"Could not load chart data: {e}")
        return

    if not points:
        st.info("No detection data available for charts.")
        return

    st.markdown("<div class='section-header'>Analytics</div>", unsafe_allow_html=True)
    st.caption(f"Based on {len(points)} detections · {datetime.now().strftime('%H:%M:%S')}")

    _DC = {"Burr": "#818cf8", "Spatter": "#f87171", "Edge Weld": "#34d399"}

    rows = []
    for pt in points:
        p = pt.get("payload", {})
        rows.append({
            "Defect": p.get("defect_name", "Unknown"),
            "Station": p.get("station", "Unknown"),
            "Confidence": parse_confidence(p.get("confidence")),
        })
    df = pd.DataFrame(rows)

    # Chart 1 — defect type counts
    vc = df["Defect"].value_counts().reset_index()
    vc.columns = ["Defect", "Count"]
    fig1 = px.bar(vc, x="Count", y="Defect", orientation="h",
                  color="Defect", color_discrete_map=_DC, title="Defect Breakdown")
    fig1.update_layout(showlegend=False, margin=dict(l=0, r=0, t=36, b=0), height=180)
    st.plotly_chart(fig1, width="stretch")

    # Chart 2 — defects by station (stacked)
    grp = df.groupby(["Station", "Defect"]).size().reset_index(name="Count")
    fig2 = px.bar(grp, x="Station", y="Count", color="Defect",
                  color_discrete_map=_DC, title="Defects by Station", barmode="stack")
    fig2.update_layout(
        margin=dict(l=0, r=0, t=36, b=0), height=220,
        legend=dict(orientation="h", yanchor="bottom", y=-0.38, xanchor="center", x=0.5),
    )
    st.plotly_chart(fig2, width="stretch")

    # Chart 3 — confidence distribution
    conf_df = df.dropna(subset=["Confidence"])
    if not conf_df.empty:
        fig3 = px.histogram(conf_df, x="Confidence", color="Defect", nbins=20,
                            color_discrete_map=_DC, title="Confidence Distribution",
                            opacity=0.8, barmode="overlay")
        fig3.update_layout(
            margin=dict(l=0, r=0, t=36, b=0), height=220,
            legend=dict(orientation="h", yanchor="bottom", y=-0.38, xanchor="center", x=0.5),
        )
        st.plotly_chart(fig3, width="stretch")

    # Chart 4 — out-of-range parameter frequency
    oor_counts: dict = {}
    for pt in points:
        p = pt.get("payload", {})
        defect = p.get("defect_name", "")
        entry = TRUTH_DB.get(defect, {})
        for param, bounds in entry.get("parameter ranges", {}).items():
            val = p.get(param)
            if val is None:
                norm_p = _norm(param)
                for pk, pv in p.items():
                    if _norm(pk) == norm_p:
                        val = pv
                        break
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            lo = bounds.get("min", float("-inf"))
            hi = bounds.get("max", float("inf"))
            if fval < lo or fval > hi:
                short = re.sub(r"\s*\([^)]*\)", "", param).strip()
                oor_counts[short] = oor_counts.get(short, 0) + 1

    if oor_counts:
        oor_df = pd.DataFrame(
            sorted(oor_counts.items(), key=lambda x: x[1]),
            columns=["Parameter", "OOR Count"],
        )
        fig4 = px.bar(oor_df, x="OOR Count", y="Parameter", orientation="h",
                      title="Out-of-Range Parameter Frequency",
                      color_discrete_sequence=["#f97316"])
        fig4.update_layout(showlegend=False, margin=dict(l=0, r=0, t=36, b=0), height=220)
        st.plotly_chart(fig4, width="stretch")


def render_notifications():
    st.markdown("<div class='section-header'>Notifications</div>", unsafe_allow_html=True)
    try:
        points = get_latest_points(limit=15)
        total_count = get_total_count()
    except Exception as e:
        st.error(f"Could not load detections: {e}")
        return

    if not points:
        st.info("No detections yet.")
        return

    c1, c2, c3 = st.columns(3)
    high_alerts = sum(
        1 for pt in points
        if (parse_confidence(pt.get("payload", {}).get("confidence")) or 0) >= 0.8
    )

    latest_received = "unknown"
    if points:
        ing = points[0].get("payload", {}).get("ingested_at")
        latest_received = format_ingested_at(ing) if ing else "unknown"

    c1.metric("Total alerts", total_count)
    c2.metric("High confidence", high_alerts)
    c3.metric("Last received", latest_received)
    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")

    for point in points:
        payload = point.get("payload", {})
        defect_name = payload.get("defect_name", "unknown")
        cell_num = payload.get("cell_number", "unknown")
        station = payload.get("station", "unknown")
        camera = payload.get("camera", "unknown")
        confidence = parse_confidence(payload.get("confidence"))
        ing = payload.get("ingested_at")
        received_time = format_ingested_at(ing) if ing else "unknown"

        defect_id = payload.get("defect_ID", "")
        badge_class, conf_text = confidence_badge(confidence)
        st.markdown(
            (
                "<div class='alert-card'>"
                "<div class='alert-top'>"
                f"<div class='alert-title'>{escape(str(defect_name))}</div>"
                f"<span class='badge {badge_class}'>Confidence {escape(conf_text)}</span>"
                "</div>"
                "<div class='alert-meta'>"
                f"Cell: {escape(str(cell_num))} | Station: {escape(str(station))} | Camera: {escape(str(camera))}<br>"
                f"Received: {escape(received_time)}"
                + (f"<br>ID: {escape(str(defect_id))}" if defect_id else "")
                + "</div>"
                "</div>"
            ),
            unsafe_allow_html=True,
        )


if "messages" not in st.session_state:
    st.session_state.messages = []

# Split main area: alerts always on left; charts panel opens on right when toggled.
if st.session_state.get("show_charts"):
    _col_alerts, _col_charts = st.columns([1, 1])
else:
    _col_alerts, _col_charts = st.container(), None

with _col_alerts:
    if hasattr(st, "fragment"):
        @st.fragment(run_every=1)
        def notification_fragment():
            render_notifications()
        notification_fragment()
    else:
        render_notifications()

if _col_charts is not None:
    with _col_charts:
        if hasattr(st, "fragment"):
            @st.fragment(run_every=30)
            def charts_fragment():
                render_charts()
            charts_fragment()
        else:
            render_charts()

# Chat lives in the sidebar — single natural scroll, input pinned to bottom.
with st.sidebar:
    head_l, head_r = st.columns([3, 1])
    with head_l:
        st.markdown("<div class='section-header'>Chat</div>", unsafe_allow_html=True)
    with head_r:
        if st.button("Clear", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_prompt = st.chat_input("Ask about defects, cells, times, or stations…")

if user_prompt and user_prompt.strip():
    st.session_state.messages.append({"role": "user", "content": user_prompt})

    answer_parts = []
    with st.sidebar:
        with st.chat_message("user"):
            st.markdown(user_prompt)
        with st.chat_message("assistant"):
            stream_placeholder = st.empty()
            stream_placeholder.markdown(
                "<span style='opacity:0.75;font-style:italic'>Thinking"
                "<span class='thinking-dot'>.</span>"
                "<span class='thinking-dot'>.</span>"
                "<span class='thinking-dot'>.</span>"
                "</span>",
                unsafe_allow_html=True,
            )
            try:
                for token in call_llm_stream(user_prompt):
                    answer_parts.append(token)
                    stream_placeholder.markdown("".join(answer_parts) + "▌")
                stream_placeholder.markdown("".join(answer_parts).strip() or "No response returned.")
            except Exception as e:
                answer_parts = [f"Error calling model: {e}"]
                stream_placeholder.markdown(answer_parts[0])

    answer = "".join(answer_parts).strip() or "No response returned."
    st.session_state.messages.append({"role": "assistant", "content": answer})
    st.rerun()
