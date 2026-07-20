"""
Deterministic spaCy-based intent parser.

Replaces the qwen2.5:3b Ollama call in _extract_query_intent with a rule-based
NLP pipeline that:
  - Never hallucinates fields
  - Runs in ~5ms vs ~500ms for the 3B
  - Is fully reproducible (same input → same output always)
  - Handles word-numbers ("station one"), plurals ("burrs"), multi-word defects
    ("edge weld"), hex IDs, confidence bands, and date expressions via dateparser

The returned dict matches the schema previously returned by the 3B extractor so
all downstream routing in build_grounded_prompt is unchanged.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import dateparser
import spacy

# ---------------------------------------------------------------------------
# Word-number mapping for "station one" → 1, "robot two" → 2, etc.
# ---------------------------------------------------------------------------
_WORD_NUM: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM_WORDS = list(_WORD_NUM.keys())


def _tok_to_int(tok: str) -> int:
    """Convert a digit string or word-number token to an integer."""
    if tok.isdigit():
        return int(tok)
    return _WORD_NUM.get(tok, 1)


# ---------------------------------------------------------------------------
# Load Truth.json once at import time for defect name patterns
# ---------------------------------------------------------------------------
_TRUTH_PATH = Path(__file__).parent / "Truth.json"
try:
    with open(_TRUTH_PATH) as _f:
        _TRUTH = json.load(_f)
except Exception:
    _TRUTH = {}

# Canonical lowercase defect names (excludes the "stations" meta-key)
_DEFECT_NAMES: list[str] = [k.lower() for k in _TRUTH if k.lower() != "stations"]


# ---------------------------------------------------------------------------
# Trend / aggregate detection regex (mirrors _is_trend_query in app.py)
# ---------------------------------------------------------------------------
_TREND_RE = re.compile(
    r"\b(trends?|analy[sz]e|analys[ie]s|common\s+(issues?|problems?|defects?|faults?)|"
    r"commonalit|overview|distribution|patterns?|insights?|breakdown\s+of\s+all)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Lazy-initialised spaCy pipeline (one instance per process)
# ---------------------------------------------------------------------------
_nlp = None


def _build_nlp():
    """Build and cache the spaCy pipeline with custom EntityRuler patterns."""
    global _nlp
    if _nlp is not None:
        return _nlp

    nlp = spacy.load("en_core_web_sm", disable=["lemmatizer"])

    # entity_ruler runs BEFORE the statistical NER so our domain patterns win
    ruler = nlp.add_pipe("entity_ruler", before="ner", config={"overwrite_ents": True})

    patterns: list[dict] = []

    # --- Defect types (from Truth.json, singular + plural) ---
    for name in _DEFECT_NAMES:
        # phrase pattern – spaCy splits on whitespace so "edge weld" works fine
        patterns.append({"label": "DEFECT", "pattern": name})
        # plural: append 's' to last word
        words = name.split()
        plural = " ".join(words[:-1] + [words[-1] + "s"])
        if plural != name:
            patterns.append({"label": "DEFECT", "pattern": plural})

    # --- Station: "station 1", "station one" ---
    for kw in ("station",):
        patterns.append({"label": "STATION", "pattern": [
            {"LOWER": kw}, {"IS_DIGIT": True}]})
        patterns.append({"label": "STATION", "pattern": [
            {"LOWER": kw}, {"LOWER": {"IN": _NUM_WORDS}}]})

    # --- Cell: "cell 1", "cam 2", "camera 3" ---
    for kw in ("cell", "cam", "camera"):
        patterns.append({"label": "CELL", "pattern": [
            {"LOWER": kw}, {"IS_DIGIT": True}]})
        patterns.append({"label": "CELL", "pattern": [
            {"LOWER": kw}, {"LOWER": {"IN": _NUM_WORDS}}]})

    # --- Operator: "robot 1", "operator 2", "op 3" ---
    for kw in ("robot", "operator", "op"):
        patterns.append({"label": "OPERATOR", "pattern": [
            {"LOWER": kw}, {"IS_DIGIT": True}]})
        patterns.append({"label": "OPERATOR", "pattern": [
            {"LOWER": kw}, {"LOWER": {"IN": _NUM_WORDS}}]})

    # --- Defect IDs: lowercase hex hashes (6+ chars) ---
    patterns.append({"label": "DEFECT_ID", "pattern": [
        {"TEXT": {"REGEX": r"^[0-9a-f]{6,}$"}}]})

    # --- Confidence bands ---
    for adj in ("high", "highly"):
        patterns.append({"label": "CONF_BAND", "pattern": [
            {"LOWER": adj}, {"LOWER": {"IN": ["confidence", "confident"]}}]})
    for adj in ("medium", "moderate"):
        patterns.append({"label": "CONF_BAND", "pattern": [
            {"LOWER": adj}, {"LOWER": {"IN": ["confidence", "confident"]}}]})
    patterns.append({"label": "CONF_BAND", "pattern": [
        {"LOWER": "low"}, {"LOWER": {"IN": ["confidence", "confident"]}}]})

    ruler.add_patterns(patterns)
    _nlp = nlp
    return nlp


# ---------------------------------------------------------------------------
# Date resolution via dateparser
# ---------------------------------------------------------------------------
def _resolve_date(text: str, intent: dict, now: datetime) -> None:
    """Parse a DATE entity text and populate intent['date_str'] or ['days_back']."""
    try:
        parsed = dateparser.parse(
            text,
            settings={
                "PREFER_DAY_OF_MONTH": "first",
                # dateparser works best with a naive datetime as RELATIVE_BASE
                "RELATIVE_BASE": now.replace(tzinfo=None),
                "RETURN_AS_TIMEZONE_AWARE": False,
                "PREFER_DATES_FROM": "past",
            },
        )
    except Exception:
        return
    if parsed is None:
        return

    parsed_date = parsed.date()
    today = now.date()
    delta = (today - parsed_date).days

    if 0 <= delta <= 3650:
        intent["date_str"] = parsed_date.strftime("%Y-%m-%d")
    # Future dates or implausibly old dates are ignored


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def extract_intent(prompt: str, now: datetime | None = None) -> dict:
    """
    Extract structured query intent from a user prompt using spaCy.

    Returns a dict with the same keys as the old 3B extractor:
      mode, defect_type, station, cell_number, operator,
      confidence_min, confidence_max, last_n, defect_id, days_back, date_str

    No LLM call is made — the result is fully deterministic.
    """
    if now is None:
        now = datetime.now().astimezone()

    nlp = _build_nlp()
    doc = nlp(prompt)
    q = prompt.lower()

    intent: dict = {
        "mode": "vector",
        "defect_type": None,
        "station": None,
        "cell_number": None,
        "operator": None,
        "confidence_min": None,
        "confidence_max": None,
        "last_n": None,
        "defect_id": None,
        "days_back": None,
        "date_str": None,
    }

    defect_ids: list[str] = []

    for ent in doc.ents:
        label = ent.label_

        if label == "DEFECT":
            raw = ent.text.lower()
            # Normalise plural: strip trailing 's' from last word if needed
            words = raw.split()
            singular = " ".join(words[:-1] + [words[-1].rstrip("s")]) if words else raw
            # Prefer exact canonical name, then singular form
            if raw in _DEFECT_NAMES:
                intent["defect_type"] = raw
            elif singular in _DEFECT_NAMES:
                intent["defect_type"] = singular
            else:
                intent["defect_type"] = singular  # best guess

        elif label == "STATION":
            intent["station"] = str(_tok_to_int(ent[-1].text.lower()))

        elif label == "CELL":
            intent["cell_number"] = str(_tok_to_int(ent[-1].text.lower()))

        elif label == "OPERATOR":
            intent["operator"] = str(_tok_to_int(ent[-1].text.lower()))
            # Operator query — never also an ID lookup
            intent["defect_id"] = None

        elif label == "DEFECT_ID":
            defect_ids.append(ent.text.lower())

        elif label == "CONF_BAND":
            band = ent.text.lower()
            if any(w in band for w in ("high", "highly")):
                intent["confidence_min"] = 0.8
                intent["confidence_max"] = None
            elif any(w in band for w in ("medium", "moderate")):
                intent["confidence_min"] = 0.5
                intent["confidence_max"] = 0.8
            elif "low" in band:
                intent["confidence_min"] = None
                intent["confidence_max"] = 0.5

        elif label == "DATE":
            # Only populate date fields when no date has been set yet (first DATE
            # entity wins; avoids "last 3 days" producing two competing values)
            if intent["date_str"] is None and intent["days_back"] is None:
                _resolve_date(ent.text, intent, now)

    if defect_ids:
        intent["defect_id"] = ",".join(defect_ids)

    # ------------------------------------------------------------------
    # last_n: explicit numeric limit  ("last 3", "show me 5", "top 10")
    # ------------------------------------------------------------------
    m = re.search(
        r"\b(?:last|top|first|bottom|show\s+me|give\s+me)\s+(\d+)\b", q)
    if not m:
        m = re.search(
            r"\b(\d+)\s+(?:most\s+recent|latest|detections?|defects?|results?|records?)\b", q)
    if m:
        intent["last_n"] = int(m.group(1))
    elif re.search(r"\b(most\s+recent|latest|newest)\b", q):
        # bare recency phrase without an explicit count → default to 5
        intent["last_n"] = 5

    # ------------------------------------------------------------------
    # days_back: "last N days/weeks/months", "yesterday"
    # (only when no DATE entity already provided a window)
    # ------------------------------------------------------------------
    if intent["days_back"] is None and intent["date_str"] is None:
        m2 = re.search(r"\blast\s+(\d+)\s+(days?|weeks?|months?)\b", q)
        if m2:
            n, unit = int(m2.group(1)), m2.group(2)
            if "week" in unit:
                n *= 7
            elif "month" in unit:
                n *= 30
            intent["days_back"] = n
        elif re.search(r"\byesterday\b", q):
            yesterday = (now - timedelta(days=1)).date()
            intent["date_str"] = yesterday.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------
    if intent["defect_id"]:
        intent["mode"] = "lookup"
    elif _TREND_RE.search(q):
        intent["mode"] = "aggregate"
    elif any(intent[k] is not None for k in (
            "defect_type", "station", "cell_number", "operator",
            "date_str", "days_back", "confidence_min", "confidence_max")):
        intent["mode"] = "list"
    elif intent["last_n"]:
        intent["mode"] = "list"
    else:
        intent["mode"] = "vector"

    return intent
