# server.py — Rhea-only MCP, root "/" only
import os
import re
import asyncio
from typing import Any, Dict, List
import httpx
from mcp.server.fastmcp import FastMCP

RHEA_SPARQL = os.getenv("RHEA_SPARQL", "https://sparql.rhea-db.org/sparql")
UA          = os.getenv("BIO_UA", "GraphBio-RheaOnly/1.2 (contact: you@example.com)")

mcp = FastMCP("graph-bio-rhea")
mcp.settings.streamable_http_path = "/"  # MCP lives at root

# -------------------- Helpers --------------------

_MAX_TOKENS = 6           # keep queries modest for reliability
_MAX_TOKEN_LEN = 64

def _sparql_str(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')

def _nl_tokens(q: str) -> List[str]:
    """
    Tokenize safely:
      - letters/digits/_/+/- (keep simple ASCII; avoid punctuation)
      - drop short tokens (<=2)
      - lowercase
      - cap token length and total tokens
    """
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9_+-]+", q or "")
    toks = [t.lower()[:_MAX_TOKEN_LEN] for t in toks if len(t) >= 3]
    out, seen = [], set()
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
        if len(out) >= _MAX_TOKENS:
            break
    return out

# -------------------- HTTP/SPARQL --------------------

async def _post_sparql(
    endpoint: str,
    query: str,
    timeout: float = 60.0,
    retries: int = 2,
    prefer_get_fallback: bool = True,
) -> Dict[str, Any]:
    """
    POST SPARQL with optional GET fallback, exponential backoff, HTTP/2 enabled.
    Adds 'format=json' which some gateways prefer.
    """
    headers_post = {
        "Accept": "application/sparql-results+json",
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    headers_get = {
        "Accept": "application/sparql-results+json",
        "User-Agent": UA,
    }

    t = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)
    backoff = 0.75

    async def _try_post():
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=True) as client:
            r = await client.post(endpoint, data={"query": query, "format": "json"}, headers=headers_post)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    async def _try_get():
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=True) as client:
            r = await client.get(endpoint, params={"query": query, "format": "json"}, headers=headers_get)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    for attempt in range(retries + 1):
        try:
            return await _try_post()
        except (httpx.TimeoutException, httpx.TransportError):
            pass
        await asyncio.sleep(backoff)
        backoff *= 2

    if prefer_get_fallback:
        try:
            return await _try_get()
        except (httpx.TimeoutException, httpx.TransportError) as e2:
            return {"error": {"status_code": 599, "body": f"GET fallback {type(e2).__name__}: {e2}"}}
    return {"error": {"status_code": 599, "body": "SPARQL POST failed and GET not attempted"}}

# -------------------- Rhea label search --------------------

def _build_rhea_label_query(q: str, limit: int) -> str:
    """
    Search Rhea reaction classes by free text:
    - match tokens against rdfs:label (human-readable equation) OR rh:accession
    - AND across tokens
    """
    tokens = _nl_tokens(q)
    token_filter = "\n".join(
        f'  FILTER(CONTAINS(LCASE(STR(?eq)), "{_sparql_str(t)}") || CONTAINS(LCASE(STR(?acc)), "{_sparql_str(t)}"))'
        for t in tokens
    ) or "  # no tokens; return limited results\n"

    return f"""
PREFIX rh:   <http://rdf.rhea-db.org/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?id ?acc ?eq WHERE {{
  ?id rdfs:subClassOf rh:Reaction ;
      rh:accession ?acc ;
      rdfs:label ?eq .
{token_filter}
}}
ORDER BY ?acc
LIMIT {int(limit)}
""".strip()

async def _search_rhea_labels(needle: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = _build_rhea_label_query(needle, limit)
    data = await _post_sparql(RHEA_SPARQL, q)
    if "error" in data:
        return [{
            "type": "error",
            "id": "rhea",
            "title": "Rhea SPARQL error",
            "snippet": f"{data['error'].get('status_code')} — {data['error'].get('body','')[:160]}",
            "url": RHEA_SPARQL,
            "source": "rhea",
        }]
    out: List[Dict[str, Any]] = []
    for b in data.get("results", {}).get("bindings", []):
        iri = b["id"]["value"]
        acc = b.get("acc", {}).get("value", "")
        eq  = b.get("eq", {}).get("value", "")
        title = f"{acc} — {eq[:160]}"
        out.append({
            "type": "rhea:reaction",
            "id": iri, "title": title, "snippet": "Rhea reaction",
            "url": iri, "source": "rhea"
        })
    return out

# -------------------- Public tools --------------------

@mcp.tool(
    name="search",
    description="Searches Rhea reactions by label (equation) or accession."
)
async def search(query: str, limit: int = 10, language: str = "en", source: str = "rhea"):
    """
    Understands free text and Rhea accessions. Ignores 'source' (kept for compatibility).
    """
    s = (query or "").strip()

    # Exact-ID routing for Rhea accessions
    m = re.fullmatch(r"(?i)RHEA:(\d+)", s)
    if m:
        rid = m.group(1)
        iri = f"https://rdf.rhea-db.org/{rid}"
        return {"results": [{
            "type": "rhea:reaction", "id": iri, "title": f"RHEA:{rid}",
            "snippet": "Rhea reaction", "url": iri, "source": "rhea"
        }][:limit]}

    # Label search
    results = await _search_rhea_labels(s, limit=limit)

    # Surface errors if any; otherwise return deduped list (Rhea usually unique already)
    ok = [r for r in results if r.get("type") != "error"]
    out: Dict[str, Any] = {"results": ok[:limit]}
    errs = [r for r in results if r.get("type") == "error"]
    if errs:
        out["errors"] = [{
            "source": e.get("id"), "message": e.get("snippet"), "endpoint": e.get("url")
        } for e in errs]
    return out

@mcp.tool(
    name="fetch",
    description="Fetches raw content for a Rhea accession or an HTTP(S) URL."
)
async def fetch(id: str, language: str = "en"):
    s = (id or "").strip()
    if re.match(r"^https?://", s, re.IGNORECASE):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=True) as client:
                r = await client.get(s, headers={"User-Agent": UA})
            return {"id": s, "url": s, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for URL: {e}"}
    m = re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
    if m:
        iri = f"https://rdf.rhea-db.org/{m.group(1)}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=True) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for Rhea accession: {e}"}
    return {"error": "Provide a URL or a Rhea accession."}

@mcp.tool(
    name="choose_endpoint",
    description="Always returns 'rhea' (this server implements Rhea only)."
)
async def choose_endpoint(question: str) -> Dict[str, Any]:
    return {"target": "rhea", "reason": "protein-centric features are not included in this server"}

@mcp.tool(name="debug_ping", description="Simple SELECT 1 check for the Rhea endpoint.")
async def debug_ping():
    rh = await _post_sparql(RHEA_SPARQL, "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    return {"rhea": rh}

# -------------------- ASGI app --------------------

app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)
