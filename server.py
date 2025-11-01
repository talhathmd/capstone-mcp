# server.py — Rhea-only MCP, root "/" only
import os
import re
import asyncio
from typing import Dict, List
import httpx
from mcp.server.fastmcp import FastMCP

RHEA_SPARQL = os.getenv("RHEA_SPARQL", "https://sparql.rhea-db.org/sparql")
UA          = os.getenv("BIO_UA", "GraphBio-RheaOnly/1.3 (contact: you@example.com)")

mcp = FastMCP("graph-bio-rhea")
mcp.settings.streamable_http_path = "/"  # MCP lives at root

# -------------------- HTTP/2 detection --------------------
try:
    import h2  # type: ignore
    _H2_AVAILABLE = True
except Exception:
    _H2_AVAILABLE = False

def _http2_enabled() -> bool:
    """
    HTTP/2 policy controlled by BIO_HTTP2 env:
      - off/false/0/no  -> force HTTP/1.1
      - on/true/1/yes   -> use HTTP/2 if installed, else HTTP/1.1
      - auto (default)  -> use HTTP/2 only if 'h2' is installed
    """
    mode = (os.getenv("BIO_HTTP2", "auto") or "").lower()
    if mode in ("off", "false", "0", "no"):
        return False
    if mode in ("on", "true", "1", "yes"):
        return _H2_AVAILABLE
    return _H2_AVAILABLE  # auto

# -------------------- Query helpers --------------------
_MAX_TOKENS = 6
_MAX_TOKEN_LEN = 64

def _sparql_str(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')

def _nl_tokens(q: str) -> List[str]:
    """
    Safe tokenization:
      - letters/digits/_/+/- only
      - drop tokens <= 2 chars
      - lowercase
      - cap length and total count
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

# -------------------- HTTP/SPARQL core --------------------
async def _post_sparql(
    endpoint: str,
    query: str,
    timeout: float = 60.0,
    retries: int = 2,
    prefer_get_fallback: bool = True,
) -> Dict:
    """
    POST SPARQL with optional GET fallback, exponential backoff.
    Uses HTTP/2 iff available/configured; always includes format=json.
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
    use_h2 = _http2_enabled()

    async def _try_post():
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=use_h2) as client:
            r = await client.post(endpoint, data={"query": query, "format": "json"}, headers=headers_post)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    async def _try_get():
        async with httpx.AsyncClient(timeout=t, follow_redirects=True, http2=use_h2) as client:
            r = await client.get(endpoint, params={"query": query, "format": "json"}, headers=headers_get)
            if r.status_code >= 400:
                return {"error": {"status_code": r.status_code, "body": (r.text or "")[:2000]}}
            return r.json()

    for _ in range(retries + 1):
        try:
            return await _try_post()
        except (httpx.TimeoutException, httpx.TransportError):
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
    Match tokens against rdfs:label (equation) OR rh:accession; AND across tokens.
    """
    tokens = _nl_tokens(q)
    token_filter = "\n".join(
        f'  FILTER(CONTAINS(LCASE(STR(?eq)), "{_sparql_str(t)}") '
        f'      OR CONTAINS(LCASE(STR(?acc)), "{_sparql_str(t)}"))'
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

async def _search_rhea_labels(needle: str, limit: int = 10) -> List[Dict]:
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
    out: List[Dict] = []
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
    description="Search Rhea reactions by label (equation) or accession."
)
async def search(query: str, limit: int = 10, language: str = "en", source: str = "rhea"):
    """
    Understands free text and Rhea accessions. 'source' is accepted for compatibility but ignored.
    """
    s = (query or "").strip()

    # Exact accession: RHEA:<digits>
    m = re.fullmatch(r"(?i)RHEA:(\d+)", s)
    if m:
        rid = m.group(1)
        iri = f"https://rdf.rhea-db.org/{rid}"
        return {"results": [{
            "type": "rhea:reaction", "id": iri, "title": f"RHEA:{rid}",
            "snippet": "Rhea reaction", "url": iri, "source": "rhea"
        }][:limit]}

    # Free-text label search
    results = await _search_rhea_labels(s, limit=limit)

    ok = [r for r in results if r.get("type") != "error"]
    out: Dict = {"results": ok[:limit]}
    errs = [r for r in results if r.get("type") == "error"]
    if errs:
        out["errors"] = [{
            "source": e.get("id"), "message": e.get("snippet"), "endpoint": e.get("url")
        } for e in errs]
    return out

@mcp.tool(
    name="fetch",
    description="Fetch raw content for a Rhea accession or an HTTP(S) URL."
)
async def fetch(id: str, language: str = "en"):
    s = (id or "").strip()
    use_h2 = _http2_enabled()

    if re.match(r"^https?://", s, re.IGNORECASE):
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=use_h2) as client:
                r = await client.get(s, headers={"User-Agent": UA})
            return {"id": s, "url": s, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for URL: {e}"}

    m = re.match(r"^RHEA:(\d+)$", s, re.IGNORECASE)
    if m:
        iri = f"https://rdf.rhea-db.org/{m.group(1)}"
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=use_h2) as client:
                r = await client.get(iri, headers={"User-Agent": UA})
            return {"id": s, "url": iri, "mime": r.headers.get("content-type"), "content": r.text[:200000]}
        except Exception as e:
            return {"error": f"Fetch failed for Rhea accession: {e}"}

    return {"error": "Provide a URL or a Rhea accession."}

@mcp.tool(
    name="debug_ping",
    description="Simple SELECT 1 check and transport info for the Rhea endpoint."
)
async def debug_ping():
    use_h2 = _http2_enabled()
    rh = await _post_sparql(RHEA_SPARQL, "SELECT (1 AS ?x) WHERE {}", timeout=10.0, retries=0)
    return {"rhea": rh, "http2_enabled": use_h2, "h2_installed": _H2_AVAILABLE}

# -------------------- ASGI app --------------------
app = mcp.streamable_http_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")), reload=True)
