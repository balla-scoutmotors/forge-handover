# DECISIONS.md — Decision record

---

## ADR-1: spaCy replaces qwen2.5:3b for intent extraction
**Date:** 2026-06  **Status:** Accepted

**Context:** The original query intent parser called `qwen2.5:3b` via Ollama to extract structured fields (defect type, station, date range) from a user's natural-language query. This added ~500 ms per query, hallucinated field names that did not exist in the payload, and produced different output for identical inputs — making routing non-deterministic.

**Decision:** Replaced with a deterministic spaCy `EntityRuler` pipeline that uses domain patterns derived from `Truth.json`. Runs in ~5 ms, never hallucinates, and produces identical output for identical input.

**Consequences:** `qwen2.5:3b` is no longer required and can be removed from Ollama to save memory. spaCy and `dateparser` are added as runtime dependencies. The intent parser must be updated whenever new defect types or station naming patterns are added to the project.

---

## ADR-2: Markdown is embedded into Qdrant; JSON populates payload fields
**Date:** 2026-06  **Status:** Accepted

**Context:** Each detection event produces both a structured JSON file and a human-readable Markdown report. Either could be used as the text source for embedding into the vector store.

**Decision:** The Markdown report is sent to `nomic-embed-text` for embedding. The JSON populates Qdrant payload fields used for structured filtering, counting, and aggregation. The Markdown is also stored verbatim in the `content` payload field so the LLM receives the original report text as context.

**Consequences:** Vector similarity search reflects semantic content that matches the kinds of conversational questions engineers ask. JSON fields remain available for exact-match filters and counts without requiring vector search. The embedding model sees natural language rather than raw key-value pairs, producing more useful vectors.

---

## ADR-3: No distance cutoff for defect-to-weld correlation
**Date:** 2026-06  **Status:** Accepted

**Context:** The weld resolver assigns each detected defect to its nearest weld coordinate by Euclidean distance in image space. A cutoff distance could reject matches that exceed a threshold (treating them as unresolvable).

**Decision:** No cutoff. Every defect always receives its nearest weld assignment; many-to-one mapping is allowed (multiple defects may share the same nearest weld).

**Consequences:** Every defect is guaranteed a weld assignment, simplifying downstream event construction and ensuring no detection is silently dropped. A geometrically distant match indicates a data quality issue (wrong part type, wrong image framing) rather than a system error — and is visible in the reasoning overlay.

---

## ADR-4: S3 + SQS as decoupled transport between vision host and server stack
**Date:** 2026-06  **Status:** Accepted

**Context:** The local vision app runs on shop-floor hardware with potentially intermittent connectivity. Detection events need to reach the server stack reliably without a direct persistent connection.

**Decision:** Local app writes detection artifacts to S3. An S3 event notification triggers a Lambda that bridges S3 to SQS. The embed-worker consumes from SQS. No direct connection between the vision host and the server.

**Consequences:** The local app can continue capturing and uploading if the server is temporarily unavailable — messages accumulate in SQS and are processed when the server recovers. S3 provides permanent artifact storage independent of the queue. The dual-file pairing (Lambda fetches both JSON and MD by UUID) creates a brief window where only one file exists; Lambda self-resolves when the sibling arrives.

---

## ADR-5: Parameter assessment pre-computed in Python, not delegated to the LLM
**Date:** 2026-06  **Status:** Accepted

**Context:** `qwen2.5:7b` was generating inconsistent in-range / out-of-range verdicts when asked to compare numeric cause parameters against `Truth.json` reference ranges. Numeric reasoning is a known weak point for 7B-class models.

**Decision:** Before calling the LLM, the dashboard computes the in-range / out-of-range verdict for every cause parameter in Python by comparing payload values against `Truth.json` min / max ranges. These verdicts are injected verbatim into the prompt with explicit instructions not to re-evaluate them.

**Consequences:** Numeric comparisons are always correct and reproducible. The LLM's role is limited to narrating pre-computed verdicts in natural language, which eliminates the main source of factual hallucination in responses. The system prompt becomes longer, but the 16 384-token context window (`num_ctx`) is sufficient.
