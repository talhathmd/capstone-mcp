# server.py — Bio-only MCP (UniProt + Rhea), robust HTTP, no hard-coded NL intents
import os, re, asyncio
from typing import Any, Dict
import httpx
from mcp.server.fastmcp import FastMCP

# ---- Endpoints ----
UNIPROT = os.getenv("UNIPROT_SPARQL", "https://sparql.uniprot.org/sparql")
RHEA    = os.getenv("RHEA_SPARQL",    "https://sparql.rhea-db.org/sparql")

# ---- Identity / headers ----
UA   = os.getenv("BIO_UA",  "TalhaCapstone/0.7 (contact: you@example.com)")
FROM = os.getenv("BIO_FROM", "")  # optional email; helps with rate limiting

# ---- Timeouts ----
HTTP_CONNECT = float(os.getenv("BIO_HTTP_TIMEOUT_CONNECT", "8"))
HTTP_READ    = float(os.getenv("BIO_HTTP_TIMEOUT_READ",    "25"))
HTTP_WRITE   = float(os.getenv("BIO_HTTP_TIMEOUT_WRITE",   "10"))
RETRY_ON     = (429, 500, 502, 503, 504)

mcp = FastMCP("graph-bio")
mcp.settings.streamable_http_path = "/"

def _timeout():
    return httpx.Timeout(connect=HTTP_CONNECT, read=HTTP_READ, write=HTTP_WRITE)

def _headers():
    h = {
        "Accept": "application/sparql-results+json",
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if FROM:
        h["From"] = FROM
    return h

async def _post_sparql(endpoint: str, query: str, retries: int = 1) -> Dict[str, Any]:
    """
    Robust SPARQL: try POST; on 5xx/429/timeout do a GET fallback.
    Returns JSON or {"error": {...}}. No exceptions bubble to MCP.
    """
    # 1) POST
    try:
        async with httpx.AsyncClient(timeout=_timeout(), http2=True) as client:
            r = await client.post(endpoint, data={"query": query}, headers=_headers())
        if r.status_code in RETRY_ON and retries:
            await asyncio.sleep(0.5)
            # 2) GET fallback
            async with httpx.AsyncClient(timeout=_timeout(), http2=True) as client:
                r = await client.get(endpoint, params={"query": query}, headers=_headers())
        if r.status_code >= 400:
            return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
        return r.json()
    except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
        # GET fallback on network timeout
        try:
            async with httpx.AsyncClient(timeout=_timeout(), http2=True) as client:
                r = await client.get(endpoint, params={"query": query}, headers=_headers())
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": r.text[:2000]}}
            return r.json()
        except Exception as e2:
            return {"error": {"status_code": 504, "body": f"Timeout after POST+GET: {type(e2).__name__}: {e2}"}}
    except Exception as e:
        return {"error": {"status_code": 500, "body": f"{type(e).__name__}: {e}"}}

# ------------- Exposed tools: raw SPARQL + simple helpers -------------

@mcp.tool()
async def execute_sparql_uniprot(query_string: str, format: str = "json") -> Dict[str, Any]:
    """Run a SPARQL query against the UniProt endpoint. Returns JSON or error."""
    return await _post_sparql(UNIPROT, query_string)

@mcp.tool()
async def execute_sparql_rhea(query_string: str, format: str = "json") -> Dict[str, Any]:
    """Run a SPARQL query against the Rhea endpoint. Returns JSON or error."""
    return await _post_sparql(RHEA, query_string)

# Minimal search helpers (kept for convenience; they still hit SPARQL)
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

def _s(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')

@mcp.tool(name="search", description="Search UniProt and/or Rhea by label, mnemonic, or ID.")
async def search(query: str, limit: int = 10, language: str = "en", source: str = "both"):
    src = (source or "both").lower()
    tasks = []
    if src in ("uniprot","both","all"):
        tasks.append(_post_sparql(UNIPROT, UNIPROT_LABEL_SEARCH % (_s(query), limit)))
    if src in ("rhea","both","all"):
        tasks.append(_post_sparql(RHEA,   RHEA_LABEL_SEARCH   % (_s(query), limit)))

    results, errors = [], []
    for coro in tasks:
        try:
            data = await asyncio.wait_for(coro, timeout=HTTP_READ + HTTP_CONNECT + 2)
            results.append(data)
        except Exception as e:
            errors.append({"source":"search","error":str(e)})

    out = {"results": [], "errors": errors} if errors else {"results": []}
    # Flatten into a unified list of simple cards
    for data in results:
        if isinstance(data, dict) and "results" in data:
            for b in data["results"].get("bindings", []):
                card = {}
                if "acc" in b and "eq" in b:  # Rhea card
                    iri  = b.get("id", {}).get("value")
                    acc  = b.get("acc", {}).get("value")
                    eq   = b.get("eq",  {}).get("value", "")
                    card = {"type":"rhea:reaction","id":iri,"title":f"{acc} — {eq[:160]}","url":iri,"source":"rhea"}
                else:  # UniProt card
                    iri  = b.get("id", {}).get("value")
                    lab  = b.get("label", {}).get("value") or b.get("acc", {}).get("value") or iri.rsplit("/",1)[-1]
                    card = {"type":"uniprot:protein","id":iri,"title":lab,"url":iri,"source":"uniprot"}
                out["results"].append(card)
    # De-dup
    seen, dedup = set(), []
    for r in out["results"]:
        k = (r.get("id"), r.get("source"))
        if k not in seen:
            seen.add(k); dedup.append(r)
    out["results"] = dedup[:limit]
    return out

@mcp.tool(name="fetch", description="Fetch content by URL, UniProt accession, or RHEA:<id>.")
async def fetch(id: str, language: str = "en"):
    s = (id or "").strip()
    try:
        if re.match(r"^https?://", s, re.IGNORECASE):
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r = await client.get(s, headers={"User-Agent": UA})
            return {"id": s, "url": s, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        m = re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
        if m:
            iri = f"https://rdf.rhea-db.org/{m.group(1)}"
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        if re.match(r"^[A-Z0-9]{6,10}(?:-\\d+)?$", s):  # UniProt accession or isoform
            iri = f"https://purl.uniprot.org/uniprot/{s}"
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        return {"error": "Pass a URL, a UniProt accession (e.g., P00533 or P00533-2), or a Rhea ID like RHEA:12345."}
    except Exception as e:
        return {"error": f"Fetch failed: {type(e).__name__}: {e}"}

BIO_HINTS_UNIPROT = ("uniprot","protein","proteome","isoform","mnemonic","go:","ec ","ec:","enzyme","kinase","receptor","domain")
BIO_HINTS_RHEA    = ("rhea","reaction","substrate","product","equation","balanced","transport","stoichiometry","reversible","irreversible")

@mcp.tool(name="choose_endpoint", description="Return the best KG: 'uniprot'|'rhea'.")
async def choose_endpoint(question: str) -> Dict[str, Any]:
    q = (question or "").lower()
    if any(k in q for k in BIO_HINTS_RHEA):    return {"target":"rhea",   "reason":"reaction cues detected"}
    if any(k in q for k in BIO_HINTS_UNIPROT): return {"target":"uniprot","reason":"protein cues detected"}
    return {"target":"uniprot","reason":"default fallback"}

# Simple health check
@mcp.tool(name="debug_ping", description="Trivial SELECT 1 against UniProt and Rhea.")
async def debug_ping():
    async def ping(ep):
        return await _post_sparql(ep, "SELECT (1 AS ?x) WHERE {}")
    return {"uniprot": await ping(UNIPROT), "rhea": await ping(RHEA)}

# ---- ASGI app ----
app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
