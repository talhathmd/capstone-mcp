# server.py — Bio-only MCP with single-shot "answer" tool to avoid chatty calls
import os
import re
import asyncio
from typing import Any, Dict, List, Optional, Tuple
import httpx
from functools import lru_cache
from mcp.server.fastmcp import FastMCP

UNIPROT = "https://sparql.uniprot.org/sparql"
RHEA = "https://sparql.rhea-db.org/sparql"
UA = os.getenv("BIO_UA", "TalhaCapstone/0.4 (contact: talhatah2022@gmail.com)")

mcp = FastMCP("graph-bio")
mcp.settings.streamable_http_path = "/"

# -------------------- Helpers --------------------

def _sparql_str(s: str) -> str:
    """Minimal escape for embedding Python strings as SPARQL string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')

async def _post_sparql(
    endpoint: str,
    query: str,
    timeout: float = 60.0,
    retries: int = 2
) -> Dict[str, Any]:
    """POST a SPARQL query and return parsed JSON or a structured error."""
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    backoff = 0.75
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(endpoint, data={"query": query}, headers=headers)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == retries:
                return {"error": {"status_code": 599, "body": f"{type(e).__name__}: {e}"}}
            await asyncio.sleep(backoff)
            backoff *= 2

# -------------------- Raw SPARQL tools (exposed, but not required) --------------------

@mcp.tool()
async def execute_sparql_uniprot(query_string: str, format: str = "json") -> Dict[str, Any]:
    return await _post_sparql(UNIPROT, query_string, timeout=60.0)

@mcp.tool()
async def execute_sparql_rhea(query_string: str, format: str = "json") -> Dict[str, Any]:
    return await _post_sparql(RHEA, query_string, timeout=60.0)

# -------------------- Health check & simple cache --------------------

@lru_cache(maxsize=32)
def _cache_key(endpoint: str, q: str) -> str:
    return f"{endpoint}:{hash(q)}"

# Circuit breaker flags (very lightweight)
_circuit = {"uniprot": {"open": False, "until": 0.0}}

async def _health(endpoint: str, timeout: float = 10.0) -> bool:
    res = await _post_sparql(endpoint, "SELECT (1 as ?x) WHERE {}", timeout=timeout, retries=0)
    return "results" in res

# -------------------- Label search builders (kept for back-compat) --------------------

UNIPROT_LABEL_SEARCH = """
PREFIX up:   <http://purl.uniprot.org/core/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?label ?acc WHERE {
  BIND(\"\"\"%s\"\"\" AS ?needle)
  ?id a up:Protein .
  OPTIONAL { ?id up:mnemonic ?acc . }
  OPTIONAL { ?id up:recommendedName/up:fullName ?fn . }
  OPTIONAL { ?id rdfs:label ?rl . }
  OPTIONAL { ?id up:alternativeName/up:fullName ?alt . }
  BIND(COALESCE(?fn, ?rl, ?alt, ?acc) AS ?label)
  FILTER(
    CONTAINS(LCASE(STR(?label)), LCASE(?needle)) ||
    (BOUND(?acc) && CONTAINS(LCASE(STR(?acc)), LCASE(?needle)))
  )
}
ORDER BY ?label
LIMIT %d
"""

RHEA_LABEL_SEARCH = """
PREFIX rh:   <http://rdf.rhea-db.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?acc ?eq WHERE {
  BIND(\"\"\"%s\"\"\" AS ?needle)
  ?id rdfs:subClassOf rh:Reaction ;
      rh:accession ?acc ;
      rdfs:label ?eq .
  FILTER(
    CONTAINS(LCASE(STR(?eq)), LCASE(?needle)) ||
    CONTAINS(LCASE(STR(?acc)), LCASE(?needle))
  )
}
ORDER BY ?acc
LIMIT %d
"""

async def _search_uniprot_labels(needle: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = UNIPROT_LABEL_SEARCH % (_sparql_str(needle), limit)
    data = await _post_sparql(UNIPROT, q)
    if "error" in data:
        return [{
            "type": "error",
            "id": "uniprot",
            "title": "UniProt SPARQL error",
            "snippet": f"{data['error'].get('status_code')} — {data['error'].get('body','')[:160]}",
            "url": UNIPROT,
            "source": "uniprot",
        }]
    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri = b["id"]["value"]
        title = b.get("label", {}).get("value") or b.get("acc", {}).get("value") or iri.rsplit("/", 1)[-1]
        out.append({"type": "uniprot:protein", "id": iri, "title": title, "snippet": "UniProtKB protein", "url": iri, "source": "uniprot"})
    return out

async def _search_rhea_labels(needle: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = RHEA_LABEL_SEARCH % (_sparql_str(needle), limit)
    data = await _post_sparql(RHEA, q)
    if "error" in data:
        return [{
            "type": "error",
            "id": "rhea",
            "title": "Rhea SPARQL error",
            "snippet": f"{data['error'].get('status_code')} — {data['error'].get('body','')[:160]}",
            "url": RHEA,
            "source": "rhea",
        }]
    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri = b["id"]["value"]
        acc = b.get("acc", {}).get("value", "")
        eq  = b.get("eq", {}).get("value", "")
        title = f"{acc} — {eq[:160]}"
        out.append({"type": "rhea:reaction", "id": iri, "title": title, "snippet": "Rhea reaction", "url": iri, "source": "rhea"})
    return out

# -------------------- Public search/fetch (compat) --------------------

@mcp.tool(name="search", description="Search UniProt and/or Rhea by label, mnemonic, or ID.")
async def search(query: str, limit: int = 10, language: str = "en", source: str = "both"):
    results: List[Dict[str, Any]] = []
    src = (source or "both").lower()
    if src in ("uniprot", "both", "all"):
        results += await _search_uniprot_labels(query, limit=limit)
    if src in ("rhea", "both", "all"):
        results += await _search_rhea_labels(query, limit=limit)
    errors, ok = [], []
    for r in results:
        if r.get("type") == "error":
            errors.append({"source": r.get("id"), "message": r.get("snippet"), "endpoint": r.get("url")})
        else:
            ok.append(r)
    seen = set()
    dedup = []
    for r in ok:
        rid = r.get("id") or r.get("url")
        if rid and rid not in seen:
            seen.add(rid); dedup.append(r)
    out: Dict[str, Any] = {"results": dedup[:limit]}
    if errors: out["errors"] = errors
    return out

@mcp.tool(name="fetch", description="Fetch content by URL, UniProt accession, or RHEA:<id>.")
async def fetch(id: str, language: str = "en"):
    s = (id or "").strip()
    if re.match(r"^https?://", s, re.IGNORECASE):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(s, headers={"User-Agent": UA})
            return {"id": s, "url": s, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for URL: {e}"}
    m = re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
    if m:
        iri = f"https://rdf.rhea-db.org/{m.group(1)}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for Rhea ID: {e}"}
    if re.match(r"^[A-Z0-9]{6,10}(?:-\\d+)?$", s):
        iri = f"https://purl.uniprot.org/uniprot/{s}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for UniProt accession: {e}"}
    return {"error": "Pass a URL, a UniProt accession (e.g., P00533 or P00533-2), or a Rhea ID like RHEA:12345."}

# -------------------- Endpoint chooser (compat) --------------------

BIO_HINTS_UNIPROT = (
    "uniprot", "protein", "proteome", "isoform", "mnemonic", "go:", "ec ", "ec:", "enzyme",
    "kinase", "receptor", "domain", "signal peptide", "transmembrane",
)
BIO_HINTS_RHEA = (
    "rhea", "reaction", "substrate", "product", "equation", "balanced", "transport",
    "stoichiometry", "reversible", "irreversible",
)

@mcp.tool(name="choose_endpoint", description="Return the best KG: 'uniprot'|'rhea'.")
async def choose_endpoint(question: str) -> Dict[str, Any]:
    q = (question or "").lower()
    if any(k in q for k in BIO_HINTS_RHEA):
        return {"target": "rhea", "reason": "biochemical reaction cues detected"}
    if any(k in q for k in BIO_HINTS_UNIPROT):
        return {"target": "uniprot", "reason": "protein/enzyme cues detected"}
    return {"target": "uniprot", "reason": "default fallback"}

# -------------------- ONE-CALL high-level tool (use this!) --------------------

def _match_intent(q: str) -> Tuple[str, Dict[str, Any]]:
    ql = q.lower()
    # 1) Count reactions that consume ATP and produce ADP
    if "consume atp" in ql and "produce adp" in ql or ("atp" in ql and "adp" in ql and "count" in ql):
        return "rhea_count_atp_to_adp", {}
    # 2) Count how many directional members each bidirectional reaction has
    if ("directional" in ql and "bidirectional" in ql and ("count" in ql or "how many" in ql)) or \
       ("directional members" in ql and "bidirectional" in ql):
        return "rhea_members_per_bidir", {}
    # 3) Show equation and accession for RHEA:NNNNN
    m = re.search(r"rhea:(\d+)", ql)
    if m and ("equation" in ql or "accession" in ql or "show" in ql):
        return "rhea_equation_for_id", {"id": f"RHEA:{m.group(1)}"}
    # Fallback: ask Rhea search
    if "rhea" in ql or "reaction" in ql:
        return "rhea_search", {"needle": q}
    # Otherwise UniProt search
    return "uniprot_search", {"needle": q}

def _sparql_for_intent(intent: str, args: Dict[str, Any]) -> Tuple[str, str]:
    """Return (endpoint, query)."""
    if intent == "rhea_count_atp_to_adp":
        return RHEA, """
PREFIX rh:   <http://rdf.rhea-db.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT (COUNT(DISTINCT ?id) AS ?count) WHERE {
  ?id rdfs:subClassOf rh:Reaction ; rdfs:label ?eq .
  BIND(STRBEFORE(?eq, " = ") AS ?lhs)
  BIND(STRAFTER(?eq, " = ") AS ?rhs)
  FILTER(CONTAINS(?lhs, "ATP"))
  FILTER(CONTAINS(?rhs, "ADP"))
}
""".strip()
    if intent == "rhea_members_per_bidir":
        return RHEA, """
PREFIX rh:   <http://rdf.rhea-db.org/>
SELECT ?n (COUNT(*) AS ?reactions) WHERE {
  { SELECT ?bidir (COUNT(DISTINCT ?dir) AS ?n) WHERE { ?bidir rh:directionalReaction ?dir . } GROUP BY ?bidir }
}
GROUP BY ?n
ORDER BY ?n
""".strip()
    if intent == "rhea_equation_for_id":
        rhid = _sparql_str(args["id"])
        return RHEA, f"""
PREFIX rh:   <http://rdf.rhea-db.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?acc ?eq WHERE {{
  ?id rh:accession ?acc ; rdfs:label ?eq .
  FILTER(?acc = "{rhid}")
}}
""".strip()
    if intent == "rhea_search":
        needle = _sparql_str(args["needle"])
        return RHEA, RHEA_LABEL_SEARCH % (needle, 10)
    if intent == "uniprot_search":
        needle = _sparql_str(args["needle"])
        return UNIPROT, UNIPROT_LABEL_SEARCH % (needle, 10)
    # Default
    return RHEA, "SELECT (0 AS ?x) WHERE {}"

@mcp.tool(name="answer", description="Single-call QA over Rhea/UniProt. Provide a natural-language question; returns final result with one SPARQL execution.")
async def answer(question: str) -> Dict[str, Any]:
    """
    One-shot entry point:
      - infers intent
      - routes to the correct endpoint
      - executes exactly one SPARQL
      - returns structured results (and the exact SPARQL used)
    """
    if not question or not question.strip():
        return {"error": "Ask a question, e.g., 'Count reactions that consume ATP and produce ADP'."}

    intent, args = _match_intent(question)
    endpoint, query = _sparql_for_intent(intent, args)

    # Circuit-break UniProt if recently down by switching to Rhea-only intents (if possible)
    if endpoint == UNIPROT and _circuit["uniprot"]["open"]:
        return {"intent": intent, "endpoint": "uniprot", "error": "UniProt temporarily unavailable (circuit open)"}

    # Try the single query
    res = await _post_sparql(endpoint, query)

    # If UniProt failed, open circuit for a short time
    if endpoint == UNIPROT and "error" in res:
        _circuit["uniprot"]["open"] = True

    out: Dict[str, Any] = {"intent": intent, "endpoint": endpoint, "sparql": query}
    if "error" in res:
        out["error"] = res["error"]
    else:
        out["results"] = res.get("results", {})
        out["head"] = res.get("head", {})
    return out

# -------------------- Diagnostics --------------------

@mcp.tool(name="debug_ping", description="Quick endpoint health-check with a trivial SELECT 1.")
async def debug_ping():
    up = await _post_sparql(UNIPROT, "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    rh = await _post_sparql(RHEA,   "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    return {"uniprot": up, "rhea": rh}

# -------------------- ASGI app --------------------

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
