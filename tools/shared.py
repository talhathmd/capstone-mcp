"""
Shared utilities used by both Rhea and Wikidata tool modules.

Contains:
  - HTTP client config (User-Agent, HTTP/2 toggle)
  - SPARQL POST/GET fallback executor
  - TTL in-memory cache (avoids repeat API calls)
  - SPARQL query linter (safety checks before hitting any endpoint)
  - Error normalization (maps raw endpoint errors to stable codes)
  - Small helpers (string escaping, limit clamping)
"""

import os
import re
import time
import hashlib
from typing import Any, Dict, List, Optional, Tuple

import httpx


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# User-Agent string sent with every outgoing request.
# Both WDQS and Rhea recommend a meaningful UA to avoid throttling.
UA = os.getenv(
    "BIO_UA",
    "GraphBio/3.0 (contact: txt0304@mavs.uta.edu)",
)

# Check if the 'h2' package is installed (needed for HTTP/2 support)
try:
    import h2  # type: ignore  # noqa: F401
    H2_AVAILABLE = True
except Exception:
    H2_AVAILABLE = False


def http2_enabled() -> bool:
    """
    Should we use HTTP/2?  Controlled by the BIO_HTTP2 env var:
      off / false / 0 / no  → always HTTP/1.1
      on  / true  / 1 / yes → HTTP/2 if h2 is installed
      auto (default)        → HTTP/2 only when h2 is installed
    """
    mode = (os.getenv("BIO_HTTP2", "auto") or "").lower()
    if mode in ("off", "false", "0", "no"):
        return False
    if mode in ("on", "true", "1", "yes"):
        return H2_AVAILABLE
    return H2_AVAILABLE  # auto


# ---------------------------------------------------------------------------
# TTL Cache — keeps search / schema / query results in memory
# ---------------------------------------------------------------------------

class TTLCache:
    """
    Simple in-memory key→value store with per-entry expiration.

    Usage:
        cache = TTLCache(ttl_seconds=300)
        cache.set("key", value)
        hit = cache.get("key")  # returns None if expired or missing
    """

    def __init__(self, ttl_seconds: int = 300):
        self._store: Dict[str, Tuple[Any, float]] = {}
        self._ttl = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or None if missing / expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.time() - ts > self._ttl:
            del self._store[key]   # lazy eviction
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Store a value with the current timestamp."""
        self._store[key] = (value, time.time())

    def make_key(self, *parts) -> str:
        """Build a deterministic cache key from arbitrary parts."""
        raw = "|".join(str(p) for p in parts)
        return hashlib.md5(raw.encode()).hexdigest()


def normalize_sparql_for_cache(query: str) -> str:
    """
    Collapse whitespace so semantically identical SPARQL queries share
    a single cache key.  "SELECT ?x  WHERE { ... }" and
    "SELECT  ?x\n  WHERE { ... }" become the same string.
    """
    return re.sub(r"\s+", " ", query.strip())


# Global cache instances (shared across the whole process)
entity_cache   = TTLCache(ttl_seconds=600)   # 10 min — entity search results
property_cache = TTLCache(ttl_seconds=600)   # 10 min — property search results
schema_cache   = TTLCache(ttl_seconds=900)   # 15 min — schema fragments
sparql_cache   = TTLCache(ttl_seconds=300)   #  5 min — successful SPARQL results


# ---------------------------------------------------------------------------
# SPARQL Execution — POST / GET fallback matrix
# ---------------------------------------------------------------------------

async def exec_sparql_json(
    endpoint: str,
    query: str,
    timeout: float = 60.0,
    user_agent: str | None = None,
) -> Dict[str, Any]:
    """
    Execute a SPARQL SELECT / ASK query and return the parsed JSON.

    Tries four methods in sequence to handle endpoint quirks:
      1) POST  (no format param)
      2) POST  (format=json)
      3) GET   (no format param)
      4) GET   (format=json)

    Returns the first successful JSON, or {"error": {...}} on failure.
    """
    ua = user_agent or UA
    use_h2 = http2_enabled()
    t = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)

    accept = "application/sparql-results+json"
    post_headers = {
        "Accept": accept,
        "User-Agent": ua,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    get_headers = {"Accept": accept, "User-Agent": ua}

    # --- helper closures ---
    async def _try_post(with_format: bool):
        data: Dict[str, str] = {"query": query}
        if with_format:
            data["format"] = "json"
        async with httpx.AsyncClient(
            timeout=t, follow_redirects=True, http2=use_h2
        ) as c:
            r = await c.post(endpoint, data=data, headers=post_headers)
            if r.status_code >= 400:
                return {
                    "error": {
                        "status_code": r.status_code,
                        "body": (r.text or "")[:2000],
                    }
                }
            return r.json()

    async def _try_get(with_format: bool):
        params: Dict[str, str] = {"query": query}
        if with_format:
            params["format"] = "json"
        async with httpx.AsyncClient(
            timeout=t, follow_redirects=True, http2=use_h2
        ) as c:
            r = await c.get(endpoint, params=params, headers=get_headers)
            if r.status_code >= 400:
                return {
                    "error": {
                        "status_code": r.status_code,
                        "body": (r.text or "")[:2000],
                    }
                }
            return r.json()

    # Try each method; first success wins
    for coro in [
        _try_post(False),
        _try_post(True),
        _try_get(False),
        _try_get(True),
    ]:
        try:
            res = await coro
            if "error" not in res:
                return res
        except (httpx.TimeoutException, httpx.TransportError):
            continue

    return {"error": {"status_code": 599, "body": "All SPARQL request attempts failed"}}


# ---------------------------------------------------------------------------
# Error Normalization — map raw errors to stable, machine-readable codes
# ---------------------------------------------------------------------------

# These codes are what the LLM uses to decide its repair strategy.
ERROR_CODES = {
    "SYNTAX":         "Query has a syntax error",
    "TIMEOUT":        "Query execution timed out",
    "RATE_LIMIT":     "Endpoint rate-limited the request (HTTP 429)",
    "EMPTY":          "Query returned zero results",
    "ENDPOINT_ERROR": "Endpoint returned an HTTP error",
    "LINTER_BLOCK":   "Query was blocked by the safety linter",
    "UNKNOWN":        "Unclassified error",
}


def normalize_error(error_message: str) -> Dict[str, str]:
    """
    Turn a raw SPARQL endpoint error string into a stable error code + hint.

    The LLM can read the code and decide how to fix the query:
      SYNTAX        → fix the SPARQL grammar
      TIMEOUT       → simplify or reduce LIMIT
      RATE_LIMIT    → wait and retry
      ENDPOINT_ERROR→ endpoint issue, retry later
      UNKNOWN       → inspect the raw message
    """
    msg = (error_message or "").lower()

    if any(kw in msg for kw in ["parse error", "syntax", "malformed", "lexical error"]):
        return {"code": "SYNTAX", "hint": "Fix the SPARQL syntax and retry."}

    if any(kw in msg for kw in ["timeout", "timed out", "deadline", "too long"]):
        return {"code": "TIMEOUT", "hint": "Simplify the query or reduce LIMIT."}

    if "429" in msg or "rate" in msg or "throttl" in msg:
        return {"code": "RATE_LIMIT", "hint": "Wait a moment before retrying."}

    if any(kw in msg for kw in ["500", "502", "503", "504"]):
        return {"code": "ENDPOINT_ERROR", "hint": "The endpoint may be down; retry later."}

    return {"code": "UNKNOWN", "hint": f"Unexpected error: {error_message[:200]}"}


# ---------------------------------------------------------------------------
# SPARQL Linter — validates queries BEFORE they hit any endpoint
# ---------------------------------------------------------------------------

# Blocked constructs: FROM / FROM NAMED / GRAPH (expensive or unexpected scope)
_BLOCKED_KEYWORDS_RE = re.compile(
    r"\b(FROM\s+NAMED|FROM\s+<|GRAPH\s+[?<])\b",
    re.IGNORECASE,
)

# Unbounded property paths (e.g. wdt:P279* or (rh:a|rh:b)+)
# Matches * or + immediately after a word char, '>', or ')' — which is
# how SPARQL path modifiers look.  Won't match COUNT(*) because '*'
# follows '(' there, not one of our trigger characters.
_UNBOUNDED_PATH_RE = re.compile(r"[\w>)][*+]")

# SERVICE clause detector + allow-listed wikibase:label service
_SERVICE_RE = re.compile(r"\bSERVICE\b", re.IGNORECASE)
_WIKIBASE_LABEL_RE = re.compile(r"SERVICE\s+wikibase:label", re.IGNORECASE)


def _strip_sparql_strings(q: str) -> str:
    """
    Remove the *contents* of string literals from a SPARQL query so that
    regex safety checks don't false-positive on text inside quotes.

    e.g.  FILTER(?x = "10*2")  →  FILTER(?x = "")
    This is only used for linting; the real query is left untouched.
    """
    # Triple-quoted strings first (rare but legal SPARQL)
    q = re.sub(r'""".*?"""', '""', q, flags=re.DOTALL)
    q = re.sub(r"'''.*?'''", "''", q, flags=re.DOTALL)
    # Then normal single/double quoted (handle escaped chars)
    q = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', '""', q)
    q = re.sub(r"'[^'\\]*(?:\\.[^'\\]*)*'", "''", q)
    return q


def _extract_effective_limit(q: str) -> Optional[int]:
    """Pull out the numeric LIMIT value from a query, or None if absent."""
    m = re.search(r"\bLIMIT\s+(\d+)", q, re.IGNORECASE)
    return int(m.group(1)) if m else None


def has_wikibase_label_service(q: str) -> bool:
    """Return True if the query contains SERVICE wikibase:label."""
    return bool(_WIKIBASE_LABEL_RE.search(q))


def strip_wikibase_label_service(q: str) -> str:
    """
    Remove the SERVICE wikibase:label { ... } block from a query.
    Used by the auto-repair loop when the label service is suspected
    of causing timeouts on large result sets.
    """
    # Match  SERVICE wikibase:label { ... }  including nested braces-free body
    return re.sub(
        r"SERVICE\s+wikibase:label\s*\{[^}]*\}\s*\.?",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()


def lint_sparql(
    query: str,
    allowed_entity_ids: Optional[set] = None,
    allowed_property_ids: Optional[set] = None,
    limit_cap: int = 200,
    max_triples: int = 12,
    source: str = "wikidata",
    label_service_limit: int = 50,
) -> Dict[str, Any]:
    """
    Check a SPARQL query against safety rules before execution.

    What it does:
      1. Injects LIMIT if missing, caps it if too high
      2. Blocks FROM / FROM NAMED / GRAPH
      3. Blocks unbounded property paths (* / +) — after stripping string
         literals to avoid false positives on things like "10*2"
      4. Allows only SERVICE wikibase:label; blocks all other SERVICE
      5. For Wikidata: **mandatory** grounding check — if the query contains
         wd:Q… or wdt:/p:/ps:/pq:P… IDs, the corresponding allowed list
         MUST be provided.  Forgetting to pass them = hard block.
      6. Warns if SERVICE wikibase:label is used with a large LIMIT
      7. Warns if triple-pattern count looks high

    Args:
      label_service_limit: when the effective LIMIT exceeds this AND the
          query uses SERVICE wikibase:label, emit a warning (the repair
          loop may strip it on timeout).

    Returns:
      {
        "ok":       bool,      # False = query is blocked
        "query":    str,       # possibly modified (LIMIT injected / capped)
        "warnings": [str],     # non-blocking notes
        "errors":   [str],     # non-empty → query is blocked
      }
    """
    warnings: List[str] = []
    errors: List[str] = []
    q = query.strip()

    # ---- LIMIT enforcement ----
    limit_match = re.search(r"\bLIMIT\s+(\d+)", q, re.IGNORECASE)
    if not limit_match:
        # No LIMIT found → inject one at the end
        q = q.rstrip().rstrip(";") + f"\nLIMIT {limit_cap}"
        warnings.append(f"Injected LIMIT {limit_cap} (was missing).")
    else:
        current = int(limit_match.group(1))
        if current > limit_cap:
            # Replace the number in-place
            q = (
                q[: limit_match.start(1)]
                + str(limit_cap)
                + q[limit_match.end(1) :]
            )
            warnings.append(f"Capped LIMIT from {current} to {limit_cap}.")

    # ---- Blocked keywords (FROM / GRAPH) ----
    if _BLOCKED_KEYWORDS_RE.search(q):
        errors.append(
            "Query uses FROM / FROM NAMED / GRAPH — these are blocked."
        )

    # ---- Unbounded property paths ----
    # Strip string literals first so "10*2" inside a FILTER doesn't
    # trigger a false positive.
    q_no_strings = _strip_sparql_strings(q)
    if _UNBOUNDED_PATH_RE.search(q_no_strings):
        errors.append(
            "Unbounded property path (* or +) detected. "
            "Use a fixed-length path instead "
            "(e.g. wdt:P31/wdt:P279 instead of wdt:P279*)."
        )

    # ---- SERVICE handling (only wikibase:label allowed) ----
    all_services = list(_SERVICE_RE.finditer(q))
    allowed_services = list(_WIKIBASE_LABEL_RE.finditer(q))
    if len(all_services) > len(allowed_services):
        errors.append(
            "Only SERVICE wikibase:label is allowed. "
            "Other SERVICE clauses are blocked for safety."
        )

    # ---- Label-service + large LIMIT warning ----
    eff_limit = _extract_effective_limit(q) or limit_cap
    if has_wikibase_label_service(q) and eff_limit > label_service_limit:
        warnings.append(
            f"SERVICE wikibase:label with LIMIT {eff_limit} may cause "
            f"timeouts. Consider removing it or reducing LIMIT to "
            f"≤ {label_service_limit}."
        )

    # ---- Entity / Property validation (Wikidata) ----
    # MANDATORY: if the query references wd:Q… or wdt:P… IDs, the caller
    # MUST have provided the corresponding allowed-list.  This prevents the
    # LLM from skipping grounding and hallucinating IDs.
    if source == "wikidata":
        used_entities = set(re.findall(r"\bwd:(Q\d+)\b", q))
        used_properties: set = set()
        for prefix in ("wdt", "p", "ps", "pq"):
            used_properties |= set(
                re.findall(rf"\b{prefix}:(P\d+)\b", q)
            )

        # Block if query has entity IDs but no allowed list was passed
        if used_entities and not allowed_entity_ids:
            errors.append(
                f"Query references entity IDs ({', '.join(sorted(used_entities))}) "
                "but no allowed_entities list was provided. "
                "Call search_entity first and pass the results."
            )
        elif allowed_entity_ids and used_entities:
            bad = used_entities - allowed_entity_ids
            if bad:
                errors.append(
                    f"Entity IDs not from grounding tools: "
                    f"{', '.join(sorted(bad))}. "
                    "Call search_entity first."
                )

        # Block if query has property IDs but no allowed list was passed
        if used_properties and not allowed_property_ids:
            errors.append(
                f"Query references property IDs ({', '.join(sorted(used_properties))}) "
                "but no allowed_properties list was provided. "
                "Call search_property first and pass the results."
            )
        elif allowed_property_ids and used_properties:
            bad = used_properties - allowed_property_ids
            if bad:
                errors.append(
                    f"Property IDs not from grounding tools: "
                    f"{', '.join(sorted(bad))}. "
                    "Call search_property first."
                )

    # ---- Triple-pattern count heuristic ----
    tp_count = len(re.findall(r"\?\w+\s+\S+\s+\S+", q))
    if tp_count > max_triples:
        warnings.append(
            f"Query has ~{tp_count} triple patterns "
            f"(soft limit {max_triples}). "
            "Consider simplifying if it times out."
        )

    return {
        "ok": len(errors) == 0,
        "query": q,
        "warnings": warnings,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Small Helpers
# ---------------------------------------------------------------------------

def escape_sparql_string(s: str) -> str:
    """Escape backslashes and double-quotes for SPARQL string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def clamp_limit(
    limit: Optional[int],
    default: int = 200,
    maximum: int = 2000,
) -> int:
    """
    Clamp a user-provided LIMIT to a safe range [1 .. maximum].
    Falls back to `default` if the value is None or garbage.
    """
    try:
        n = int(limit) if limit is not None else default
        return max(1, min(n, maximum))
    except Exception:
        return default
