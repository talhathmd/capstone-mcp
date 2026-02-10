"""
Wikidata MCP tools — grounding and SPARQL execution.

Grounding tools (MUST be called before writing SPARQL):
  search_entity       — find QIDs by text
  search_property     — find PIDs by text
  get_schema_context  — fetch labels, descriptions, datatypes for known IDs

Execution tools:
  run_sparql_wikidata   — lint → dry-run → execute, with structured results
  normalize_sparql_error— classify a raw error into a stable code
  debug_ping_wikidata   — quick connectivity check

The grounding-first workflow prevents the LLM from hallucinating
entity / property IDs.  The linter enforces LIMIT, blocks dangerous
constructs, and validates that every wd:Q… / wdt:P… in the query was
actually returned by the grounding tools.
"""

import os
import re
import time
import asyncio
from typing import Any, Dict, List, Optional

import httpx

from .shared import (
    UA,
    http2_enabled,
    exec_sparql_json,
    normalize_error,
    normalize_sparql_for_cache,
    lint_sparql,
    has_wikibase_label_service,
    strip_wikibase_label_service,
    entity_cache,
    property_cache,
    schema_cache,
    sparql_cache,
)

# ---------------------------------------------------------------------------
# Wikidata endpoints
# ---------------------------------------------------------------------------

# WDQS — the public Wikidata Query Service
WD_SPARQL = "https://query.wikidata.org/sparql"

# MediaWiki API — used for entity/property search and metadata
WD_API = "https://www.wikidata.org/w/api.php"


# ---------------------------------------------------------------------------
# Rate-limit throttle for WDQS
# WDQS is aggressive with 429s — we self-throttle to stay safe.
# ---------------------------------------------------------------------------

_last_wdqs_call: float = 0.0
_WDQS_MIN_INTERVAL: float = 1.0          # seconds between requests

# Exponential backoff state (reset after a successful call)
_consecutive_429s: int = 0
_MAX_BACKOFF: float = 32.0               # cap the wait at 32 s


async def _wdqs_throttle() -> None:
    """
    Sleep just long enough to respect WDQS rate limits.

    Uses a simple strategy:
      - Always wait at least _WDQS_MIN_INTERVAL since the last call.
      - If we've been getting 429s, do exponential backoff.
    """
    global _last_wdqs_call, _consecutive_429s
    now = time.time()

    # base gap
    gap = _WDQS_MIN_INTERVAL
    # exponential backoff if we've been throttled recently
    if _consecutive_429s > 0:
        gap = min(2 ** _consecutive_429s, _MAX_BACKOFF)

    wait = gap - (now - _last_wdqs_call)
    if wait > 0:
        await asyncio.sleep(wait)

    _last_wdqs_call = time.time()


def _record_429() -> None:
    """Bump the backoff counter after a 429."""
    global _consecutive_429s
    _consecutive_429s = min(_consecutive_429s + 1, 6)  # cap exponent


def _record_success() -> None:
    """Reset the backoff counter after a successful response."""
    global _consecutive_429s
    _consecutive_429s = 0


# ---------------------------------------------------------------------------
# Wikidata MediaWiki API helper
# ---------------------------------------------------------------------------

async def _wikidata_api(params: dict, timeout: float = 15.0) -> dict:
    """
    Call the Wikidata MediaWiki API (wbsearchentities, wbgetentities, etc.)
    and return the parsed JSON.  On failure return {"error": {...}}.
    """
    use_h2 = http2_enabled()
    t = httpx.Timeout(connect=10.0, read=timeout, write=10.0, pool=10.0)
    headers = {"User-Agent": UA, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(
            timeout=t, follow_redirects=True, http2=use_h2
        ) as c:
            r = await c.get(WD_API, params=params, headers=headers)
            if r.status_code >= 400:
                return {
                    "error": {
                        "status_code": r.status_code,
                        "body": r.text[:2000],
                    }
                }
            return r.json()
    except Exception as e:
        return {"error": {"status_code": 0, "body": str(e)[:500]}}


# ---------------------------------------------------------------------------
# Helper: execute SPARQL against WDQS with throttle + backoff awareness
# ---------------------------------------------------------------------------

async def _exec_wdqs(query: str, timeout: float = 30.0) -> Dict[str, Any]:
    """
    Run a SPARQL query against WDQS with throttle + backoff.
    Wraps exec_sparql_json and updates the backoff counters.
    """
    await _wdqs_throttle()
    result = await exec_sparql_json(WD_SPARQL, query, timeout=timeout)

    # Check if we got rate-limited
    err = result.get("error", {})
    status = err.get("status_code", 0) if isinstance(err, dict) else 0
    if status == 429:
        _record_429()
    elif "error" not in result:
        _record_success()

    return result


# ===================================================================
# Registration — called by server.py
# ===================================================================

def register(mcp) -> None:
    """Attach every Wikidata tool to the given MCP server."""

    # ==============================================================
    # A.  GROUNDING TOOLS
    # ==============================================================

    @mcp.tool(
        name="search_entity",
        description=(
            "Search Wikidata for entities matching a text string.\n"
            "MUST be called before writing any SPARQL that uses wd:Q… IDs.\n"
            "Returns ranked candidates with: id (QID), label, description, "
            "concepturi.\n\n"
            "Args:\n"
            "  text:    search string, e.g. 'Albert Einstein'\n"
            "  k:       max results (default 5, max 20)\n"
            "  context: optional extra context to disambiguate "
            "(e.g. 'physicist' or 'the protein')"
        ),
    )
    async def search_entity(
        text: str,
        k: int = 5,
        context: str = "",
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"error": "Provide search text."}
        k = max(1, min(int(k), 20))

        # Check cache first — avoids duplicate API calls for the same search
        cache_key = entity_cache.make_key("wd_ent", text.lower(), k)
        cached = entity_cache.get(cache_key)
        if cached is not None:
            return cached

        # Call the Wikidata entity search API
        params = {
            "action": "wbsearchentities",
            "format": "json",
            "language": "en",
            "search": text,
            "limit": k,
            "type": "item",
        }
        raw = await _wikidata_api(params)
        if "error" in raw:
            return raw

        # Format the response so the LLM can easily pick the right QID
        candidates = []
        for item in raw.get("search", []):
            candidates.append({
                "id": item.get("id", ""),
                "label": item.get("label", ""),
                "description": item.get("description", ""),
                "concepturi": item.get("concepturi", ""),
            })

        response = {"candidates": candidates, "query": text}
        entity_cache.set(cache_key, response)
        return response

    # ------------------------------------------------------------------

    @mcp.tool(
        name="search_property",
        description=(
            "Search Wikidata for properties matching a text string.\n"
            "MUST be called before writing SPARQL that uses wdt:P… / p:P… "
            "IDs.\n"
            "Returns ranked candidates with: id (PID), label, description.\n\n"
            "Args:\n"
            "  text:    search string, e.g. 'instance of' or 'date of birth'\n"
            "  k:       max results (default 5, max 20)\n"
            "  context: optional extra context to disambiguate"
        ),
    )
    async def search_property(
        text: str,
        k: int = 5,
        context: str = "",
    ) -> Dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"error": "Provide search text."}
        k = max(1, min(int(k), 20))

        cache_key = property_cache.make_key("wd_prop", text.lower(), k)
        cached = property_cache.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "action": "wbsearchentities",
            "format": "json",
            "language": "en",
            "search": text,
            "limit": k,
            "type": "property",
        }
        raw = await _wikidata_api(params)
        if "error" in raw:
            return raw

        candidates = []
        for item in raw.get("search", []):
            candidates.append({
                "id": item.get("id", ""),
                "label": item.get("label", ""),
                "description": item.get("description", ""),
            })

        response = {"candidates": candidates, "query": text}
        property_cache.set(cache_key, response)
        return response

    # ------------------------------------------------------------------

    @mcp.tool(
        name="get_schema_context",
        description=(
            "Fetch schema information for Wikidata entities and properties.\n"
            "Returns labels, descriptions, datatypes (for properties), and "
            "instance-of types (for entities).\n"
            "Use this to understand what properties mean before writing "
            "SPARQL.\n\n"
            "Args:\n"
            "  entity_ids:    list of QIDs, e.g. ['Q42', 'Q5']\n"
            "  property_ids:  list of PIDs, e.g. ['P31', 'P569']\n"
            "  budget_tokens: approx token budget for the response "
            "(default 2000)"
        ),
    )
    async def get_schema_context(
        entity_ids: List[str] = [],
        property_ids: List[str] = [],
        budget_tokens: int = 2000,
    ) -> Dict[str, Any]:
        all_ids = list(entity_ids or []) + list(property_ids or [])
        if not all_ids:
            return {"error": "Provide at least one entity_id or property_id."}

        # Check cache
        cache_key = schema_cache.make_key(
            "wd_schema", "|".join(sorted(all_ids))
        )
        cached = schema_cache.get(cache_key)
        if cached is not None:
            return cached

        # Batch-fetch metadata with wbgetentities (max 50 per call)
        entities_data: Dict[str, Any] = {}
        for i in range(0, len(all_ids), 50):
            chunk = all_ids[i : i + 50]
            params = {
                "action": "wbgetentities",
                "format": "json",
                "ids": "|".join(chunk),
                "props": "labels|descriptions|datatype|claims",
                "languages": "en",
            }
            raw = await _wikidata_api(params, timeout=20.0)
            if "error" not in raw:
                entities_data.update(raw.get("entities", {}))

        # Build compact schema snippets within the token budget
        # (rough ratio: 1 token ≈ 4 chars)
        char_budget = budget_tokens * 4
        lines: List[str] = []

        # Entities — show label, description, and first few instance-of types
        for eid in (entity_ids or []):
            if char_budget <= 0:
                break
            data = entities_data.get(eid, {})
            label = (
                data.get("labels", {}).get("en", {}).get("value", "?")
            )
            desc = (
                data.get("descriptions", {}).get("en", {}).get("value", "")
            )
            # Grab P31 (instance of) claims for quick typing info
            p31_claims = data.get("claims", {}).get("P31", [])
            type_ids = []
            for claim in p31_claims[:3]:
                tid = (
                    claim.get("mainsnak", {})
                    .get("datavalue", {})
                    .get("value", {})
                    .get("id", "")
                )
                if tid:
                    type_ids.append(tid)

            line = f"  {eid}: {label}"
            if desc:
                line += f" — {desc[:120]}"
            if type_ids:
                line += f"  [instance of: {', '.join(type_ids)}]"
            lines.append(line)
            char_budget -= len(line)

        # Properties — show label, datatype, description
        for pid in (property_ids or []):
            if char_budget <= 0:
                break
            data = entities_data.get(pid, {})
            label = (
                data.get("labels", {}).get("en", {}).get("value", "?")
            )
            desc = (
                data.get("descriptions", {}).get("en", {}).get("value", "")
            )
            datatype = data.get("datatype", "")

            line = f"  {pid}: {label}"
            if datatype:
                line += f"  (datatype: {datatype})"
            if desc:
                line += f" — {desc[:120]}"
            lines.append(line)
            char_budget -= len(line)

        response = {
            "schema": "\n".join(lines),
            "entity_count": len(entity_ids or []),
            "property_count": len(property_ids or []),
        }
        schema_cache.set(cache_key, response)
        return response

    # ==============================================================
    # B.  EXECUTION TOOLS
    # ==============================================================

    # Maximum internal auto-repair attempts (1 initial + up to 2 repairs)
    _MAX_REPAIRS = 2

    @mcp.tool(
        name="run_sparql_wikidata",
        description=(
            "Execute a SPARQL query against the Wikidata Query Service.\n\n"
            "Safety pipeline (runs automatically):\n"
            "  1. Lint — checks LIMIT, blocked constructs, grounding IDs\n"
            "  2. Dry-run — LIMIT 1 execution catches syntax errors fast\n"
            "  3. Full execution with bounded auto-repair:\n"
            "     • TIMEOUT  → strips SERVICE wikibase:label and/or halves "
            "LIMIT, retries up to 2×\n"
            "     • RATE_LIMIT → waits with backoff, retries up to 2×\n"
            "     • SYNTAX / other → returned immediately (can't auto-fix)\n\n"
            "IMPORTANT: Before calling this tool, you MUST:\n"
            "  • Call search_entity for every wd:Q… ID in your query\n"
            "  • Call search_property for every wdt:P… / p:P… ID\n"
            "  • Pass those IDs in allowed_entities / allowed_properties\n"
            "  (If you skip this, the linter will block the query.)\n\n"
            "Prefixes available:\n"
            "  wd:   <http://www.wikidata.org/entity/>\n"
            "  wdt:  <http://www.wikidata.org/prop/direct/>\n"
            "  p:    <http://www.wikidata.org/prop/>\n"
            "  ps:   <http://www.wikidata.org/prop/statement/>\n"
            "  pq:   <http://www.wikidata.org/prop/qualifier/>\n"
            "  rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n\n"
            "Templates you should prefer:\n"
            "  1-hop:  ?s wdt:P… ?o\n"
            "  2-hop:  ?s wdt:P1 ?mid . ?mid wdt:P2 ?o\n"
            "  Agg:    SELECT (COUNT(?x) AS ?c) WHERE { ... } GROUP BY ...\n"
            "  Filter: FILTER(?date > '1900-01-01'^^xsd:date)\n"
            "  Labels: SERVICE wikibase:label "
            "{ bd:serviceParam wikibase:language 'en'. }\n\n"
            "Args:\n"
            "  query:              SPARQL SELECT or ASK string\n"
            "  timeout_ms:         max execution time in ms "
            "(default 30000, max 60000)\n"
            "  limit_cap:          max LIMIT allowed (default 200, max 500)\n"
            "  allowed_entities:   QIDs from search_entity, e.g. ['Q42']\n"
            "  allowed_properties: PIDs from search_property, e.g. ['P31']\n\n"
            "Returns:\n"
            "  ok:            bool\n"
            "  rows:          list of {var: value} dicts\n"
            "  row_count:     int\n"
            "  error_message: str (if ok=false)\n"
            "  error_code:    one of SYNTAX, TIMEOUT, RATE_LIMIT, "
            "LINTER_BLOCK, ENDPOINT_ERROR, UNKNOWN\n"
            "  hint:          repair suggestion (if ok=false)\n"
            "  stats:         {elapsed_ms, row_count, attempts}\n"
            "  repairs:       list of auto-repairs attempted\n"
            "  lint_warnings: list of non-blocking notes"
        ),
    )
    async def run_sparql_wikidata(
        query: str,
        timeout_ms: int = 30000,
        limit_cap: int = 200,
        allowed_entities: List[str] = [],
        allowed_properties: List[str] = [],
    ) -> Dict[str, Any]:
        start = time.time()
        query = (query or "").strip()

        # Basic sanity checks
        if not query:
            return {
                "ok": False,
                "error_message": "Empty query.",
                "error_code": "SYNTAX",
            }
        if re.search(r"\b(CONSTRUCT|DESCRIBE)\b", query, re.IGNORECASE):
            return {
                "ok": False,
                "error_message": "Only SELECT or ASK queries are supported.",
                "error_code": "SYNTAX",
            }

        # Clamp parameters to safe ranges
        timeout_s = max(5, min(int(timeout_ms), 60_000)) / 1000.0
        limit_cap = max(1, min(int(limit_cap), 500))

        # ---- Step 1: Lint the query ----
        lint = lint_sparql(
            query,
            allowed_entity_ids=(
                set(allowed_entities) if allowed_entities else None
            ),
            allowed_property_ids=(
                set(allowed_properties) if allowed_properties else None
            ),
            limit_cap=limit_cap,
            source="wikidata",
        )
        if not lint["ok"]:
            return {
                "ok": False,
                "error_message": "; ".join(lint["errors"]),
                "error_code": "LINTER_BLOCK",
                "lint_errors": lint["errors"],
                "lint_warnings": lint["warnings"],
                "stats": {
                    "elapsed_ms": int((time.time() - start) * 1000),
                },
            }

        clean_query = lint["query"]   # may have LIMIT injected / capped

        # ---- Step 2: Check the SPARQL result cache ----
        # Normalize whitespace so "SELECT ?x  WHERE" and
        # "SELECT ?x\nWHERE" hit the same cache entry.
        cache_key = sparql_cache.make_key(
            "wdqs", normalize_sparql_for_cache(clean_query)
        )
        cached = sparql_cache.get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            return cached

        # ---- Step 3: Dry-run with LIMIT 1 to catch syntax errors ----
        dry_query = re.sub(
            r"\bLIMIT\s+\d+",
            "LIMIT 1",
            clean_query,
            count=1,
            flags=re.IGNORECASE,
        )
        dry_result = await _exec_wdqs(
            dry_query, timeout=min(timeout_s, 15.0)
        )

        if "error" in dry_result:
            err_body = str(dry_result["error"].get("body", ""))
            norm = normalize_error(err_body)
            return {
                "ok": False,
                "error_message": f"Dry-run failed: {err_body[:300]}",
                "error_code": norm["code"],
                "hint": norm["hint"],
                "lint_warnings": lint["warnings"],
                "stats": {
                    "elapsed_ms": int((time.time() - start) * 1000),
                },
            }

        # ---- Step 4: Execute with bounded auto-repair loop ----
        #
        # Attempt the full query.  On certain failures we can auto-repair
        # and retry up to _MAX_REPAIRS times:
        #
        #   TIMEOUT →
        #     repair 1: strip SERVICE wikibase:label (if present)
        #     repair 2: halve the LIMIT
        #
        #   RATE_LIMIT →
        #     wait with exponential backoff, then retry the same query
        #
        #   Anything else (SYNTAX, ENDPOINT_ERROR, UNKNOWN) →
        #     can't auto-fix, return immediately
        #
        current_query = clean_query
        repairs_log: List[str] = []
        last_error_code: Optional[str] = None
        last_error_body: str = ""
        attempts = 0

        for attempt in range(1 + _MAX_REPAIRS):
            attempts = attempt + 1
            result = await _exec_wdqs(current_query, timeout=timeout_s)

            # ---- Success ----
            if "error" not in result:
                last_error_code = None
                last_error_body = ""
                break

            # ---- Failure — classify and decide whether to repair ----
            err_body = str(result["error"].get("body", ""))
            norm = normalize_error(err_body)
            last_error_code = norm["code"]
            last_error_body = err_body

            # No more retries left — bail out
            if attempt >= _MAX_REPAIRS:
                break

            # -- TIMEOUT repair strategies --
            if last_error_code == "TIMEOUT":
                # Strategy A: strip SERVICE wikibase:label (only try once)
                if has_wikibase_label_service(current_query):
                    current_query = strip_wikibase_label_service(
                        current_query
                    )
                    repairs_log.append(
                        f"Attempt {attempts}: TIMEOUT — removed "
                        "SERVICE wikibase:label"
                    )
                    continue

                # Strategy B: halve the LIMIT
                cur_limit = re.search(
                    r"\bLIMIT\s+(\d+)", current_query, re.IGNORECASE
                )
                if cur_limit:
                    new_lim = max(1, int(cur_limit.group(1)) // 2)
                    current_query = (
                        current_query[: cur_limit.start(1)]
                        + str(new_lim)
                        + current_query[cur_limit.end(1) :]
                    )
                    repairs_log.append(
                        f"Attempt {attempts}: TIMEOUT — halved LIMIT "
                        f"to {new_lim}"
                    )
                    continue
                break  # nothing left to try

            # -- RATE_LIMIT repair: exponential backoff wait --
            if last_error_code == "RATE_LIMIT":
                wait_s = min(2 ** (attempt + 1), 16)
                repairs_log.append(
                    f"Attempt {attempts}: RATE_LIMIT — waiting "
                    f"{wait_s}s before retry"
                )
                await asyncio.sleep(wait_s)
                continue

            # -- Anything else: can't auto-fix --
            break

        elapsed_ms = int((time.time() - start) * 1000)

        # ---- If we exited the loop with an error, return it ----
        if last_error_code is not None:
            norm = normalize_error(last_error_body)
            return {
                "ok": False,
                "error_message": last_error_body[:500],
                "error_code": norm["code"],
                "hint": norm["hint"],
                "repairs": repairs_log,
                "lint_warnings": lint["warnings"],
                "stats": {
                    "elapsed_ms": elapsed_ms,
                    "attempts": attempts,
                },
            }

        # ---- Step 5: Parse bindings into simple rows ----
        bindings = result.get("results", {}).get("bindings", [])
        rows = []
        for b in bindings:
            row: Dict[str, str] = {}
            for var, val in b.items():
                row[var] = val.get("value", "")
            rows.append(row)

        response: Dict[str, Any] = {
            "ok": True,
            "rows": rows,
            "row_count": len(rows),
            "stats": {
                "elapsed_ms": elapsed_ms,
                "row_count": len(rows),
                "attempts": attempts,
            },
            "repairs": repairs_log,
            "lint_warnings": lint["warnings"],
        }

        # Let the LLM know if zero results came back
        if not rows:
            response["warning"] = (
                "Query returned zero results. "
                "Check entity/property IDs or try broadening the query."
            )

        # Cache successful results (use the *original* clean_query as the
        # cache key so future identical requests hit it, even if we
        # internally repaired this one)
        sparql_cache.set(cache_key, response)
        return response

    # ------------------------------------------------------------------

    @mcp.tool(
        name="normalize_sparql_error",
        description=(
            "Classify a raw SPARQL error message into a stable error code.\n"
            "Codes: SYNTAX, TIMEOUT, RATE_LIMIT, EMPTY, ENDPOINT_ERROR, "
            "UNKNOWN.\n"
            "Use this to understand what went wrong and decide how to "
            "repair the query.\n\n"
            "Args:\n"
            "  error_message: the raw error string from a failed execution"
        ),
    )
    async def normalize_sparql_error(
        error_message: str,
    ) -> Dict[str, str]:
        return normalize_error(error_message or "")

    # ------------------------------------------------------------------

    @mcp.tool(
        name="debug_ping_wikidata",
        description=(
            "Test connectivity to the Wikidata Query Service.\n"
            "Returns ok status and HTTP/2 info."
        ),
    )
    async def debug_ping_wikidata() -> Dict[str, Any]:
        result = await _exec_wdqs(
            "SELECT (1 AS ?x) WHERE {} LIMIT 1", timeout=10.0
        )
        ok = "error" not in result
        return {
            "wikidata_endpoint": WD_SPARQL,
            "ok": ok,
            "detail": result if not ok else "Connected successfully.",
            "http2_enabled": http2_enabled(),
        }
